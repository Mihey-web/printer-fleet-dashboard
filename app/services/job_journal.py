"""Print-job journal: one print_jobs row per print, derived from polled status.

JobMachine turns one printer's status samples into prints. The live tracker
feeds it every poll (Sample from PrinterStatus), the backfill feeds it
printer_history rows (Sample from a row): the rules are shared, so both modes
give the same prints. The machine is pure: no I/O, no clock (all time comes
from Sample.ts), and it never logs a job name. feed() returns operations
(open / update / close / reopen / amend) for the storage layer to apply.

Rules (spec_job_journal §4, taken from 3 months of real history):
- same print or a new one: job_seq (live Bambu), a different real name, a
  progress reset after ~100 %, a progress reset with an ETA jump or with the
  layer / print-time counter starting over (judged on the drop and the next
  DROP_SAMPLES samples). A changed total_layers alone is not a boundary (an
  echo of the previous print), nor is a layer counter that goes on after a
  power loss;
- frozen Creality telemetry (printing, ETA / print time / layers 0) means the
  print has stopped;
- a data gap (offline, maintenance, no rows) never ends a print by itself:
  gaps >= JOBS_LOST_GAP_S are counted, a print without data for
  JOBS_MAX_GAP_S is lost, and one found replaced by another print after a
  gap is lost too (its end was not seen);
- the print ends on its first terminal sample; within JOBS_TERM_GRACE_S an
  active sample of the same print reopens it (P1S idle blips; a Bambu FAILED
  never resumes: what follows it is a new print) and a late error code refines
  «stopped» into failed / cancelled. After a blank idle (a Bambu reconnect:
  no job, no progress) the same print coming back where it was reopens it up
  to JOBS_MAX_GAP_S later;
- a print whose last pause was a power loss (0300_8007) and that made no
  progress after it ends as power_loss, whatever the printer said.

Storage (spec §3, §5.3): print_jobs and job_journal_meta live in
printer_history.db. init_db() creates them (StateStore calls it), JournalDB
applies a machine's ops through StateStore.db_write and restores the open
prints after a restart, prune() is the retention. set_verdict() stores the
owner's «Годно / Брак» mark (the admin API, its own connection).

Live tracker (spec §5): main() calls start(store) once, then the poll loop
calls observe(status, now) for every polled status (live, grace, offline).

Backfill (spec §6): on the first start (job_journal_meta.backfill_version
missing or older) a background thread feeds the whole printer_history to the
same machines, writes the prints in one transaction and hands the prints still
open at the end of the history over to the live tracker.
"""
import bisect
import copy
import functools
import json
import logging
import operator
import os
import pathlib
import re
import sqlite3
import threading
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

# --- constants (spec §4.2) ---
JOBS_LOST_GAP_S = 600       # shorter data gaps are ignored (95 % of drop-outs last < 5 min)
JOBS_TERM_GRACE_S = 600     # a closed print can still be reopened / refined this long (P1S blips: 1.5-4 min)
JOBS_MAX_GAP_S = 86400      # no data for a day: the print is lost (longest ETA on the farm: 17.6 h)
JOBS_CHECKPOINT_S = 600     # an open print's state is saved at least this often while it runs
PAUSES_MAX = 20             # pause entries kept in pauses_json; the counters keep growing past it
CANCEL_CODES = frozenset({"0300_400C", "0500_400E"})
POWER_LOSS_CODE = "0300_8007"
CLOCK_BACK_S = 60           # a sample older than the newest one by more than this is skipped
DROP_SAMPLES = 2            # a progress drop is judged again on this many samples (Bambu sends the new ETA / layers late)
ERROR_TEXT_MAX = 300

ACTIVE = frozenset({"printing", "paused"})
TERMINAL = frozenset({"finished", "cancelled", "error", "idle", "unknown"})
NODATA = frozenset({"offline", "maintenance"})
RESULTS = ("finished", "cancelled", "failed", "power_loss", "stopped", "lost")
# Base results (before the power-loss override) that a blip may still undo.
_REOPENABLE = frozenset({"stopped", "failed", "lost"})

# print_jobs columns the machine owns (id, verdict*, updated_at belong to the storage layer).
COLUMNS = (
    "printer_id", "kind", "model", "job_name", "started_at", "start_exact", "ended_at", "end_exact",
    "last_seen", "result", "error_code", "error_text", "planned_sec", "planned_src", "actual_sec",
    "paused_sec", "pauses", "pauses_json", "offline_sec", "offline_n", "power_losses", "blips",
    "end_progress", "layer", "total_layers", "source", "maint", "ctx",
)

# --- error text parsing (spec §4.1) ---
# last_error since 12.10: "... | Ошибка печати 0300_8007: <text>" (bambu_extras.print_error_line).
_RX_PE = re.compile(r"Ошибка печати ([0-9A-F]{4}_[0-9A-F]{4})(?::\s*([^|]+))?")
# last_error before 12.10: "Код ошибки печати: 0x0300400C".
_RX_PE_OLD = re.compile(r"Код ошибки печати:\s*0x([0-9A-Fa-f]{8})")
# HMS entries "<code>: <text> [<severity>]": only fatal ones. serious / common
# HMS hang around for days on healthy printers and explain nothing.
_RX_HMS_FATAL = re.compile(r"(HMS_[0-9A-F_]{19}):\s*([^|\[]+)\[fatal\]")
# Klipper / Creality texts (spec_notifications §6).
_RX_OTHER = (
    (re.compile(r"Klipper остановлен \(shutdown\)(?::\s*([^|]+))?"), "klipper:shutdown"),
    (re.compile(r"Ошибка Klipper(?::\s*([^|]+))?"), "klipper:error"),
    (re.compile(r"(Klippy отключился от Moonraker)"), "klipper:disconnected"),
)
_RX_CREALITY = re.compile(r"Ошибка Creality (-?\d+)/([^\s|]+)")
# The reason column / notifications.top_reason: "<code>: <text>" (code without "HMS_").
_RX_REASON = re.compile(r"^([0-9A-F]{4}(?:_[0-9A-F]{4}){1,3}):\s*(.*)$", re.S)
# Bambu Studio names the gcode inside every .3mf like this: not a job identity.
_RX_GENERIC_NAME = re.compile(r"(^|/)Metadata/plate_\d+\.gcode$", re.I)


def _clip(text: Any) -> Optional[str]:
    if not isinstance(text, str):
        return None
    text = text.strip()
    if not text or text.lower() == "unknown":
        return None
    return text[:ERROR_TEXT_MAX]


def parse_error(text: Any) -> Tuple[Optional[str], Optional[str]]:
    """(code, text) of the error that explains a pause or an end, else (None, None).

    Priority: print_error (new format, then the pre-12.10 one), fatal HMS,
    Klipper / Creality texts. Codes: "0300_400C", "HMS_0C00_0100_0001_0004",
    "klipper:shutdown", "creality:<code>/<key>".
    """
    if not isinstance(text, str) or not text:
        return None, None
    return _parse_error(text)


@functools.lru_cache(maxsize=1024)   # last_error repeats for hours: the backfill parses 1.4M rows
def _parse_error(text: str) -> Tuple[Optional[str], Optional[str]]:
    m = _RX_PE.search(text)
    if m:
        return m.group(1), _clip(m.group(2))
    m = _RX_PE_OLD.search(text)
    if m:
        h = m.group(1).upper()
        return h[:4] + "_" + h[4:], None
    m = _RX_HMS_FATAL.search(text)
    if m:
        return m.group(1), _clip(m.group(2))
    for rx, code in _RX_OTHER:
        m = rx.search(text)
        if m:
            return code, _clip(m.group(1))
    m = _RX_CREALITY.search(text)
    if m:
        return "creality:%s/%s" % (m.group(1), m.group(2)), _clip(m.group(0))
    return None, None


def parse_reason(reason: Any) -> Tuple[Optional[str], Optional[str]]:
    """(code, text) from a history `reason` value ("<code>: <text>"); "stage:<slug>" has no code."""
    if not isinstance(reason, str) or not reason.strip() or reason.startswith("stage:"):
        return None, None
    code, text = parse_error(reason)
    if code:
        return code, text
    m = _RX_REASON.match(reason.strip())
    if m:
        code = m.group(1)
        return (code if len(code) == 9 else "HMS_" + code), _clip(m.group(2))
    return None, None


def clean_name(name: Any) -> Optional[str]:
    if not isinstance(name, str):
        return None
    name = name.strip()
    return name or None


def norm_name(name: Any) -> Optional[str]:
    """The name as a job identity: None when empty or the generic Metadata/plate_N.gcode."""
    name = clean_name(name)
    if name is None or _RX_GENERIC_NAME.search(name):
        return None
    return name


# --- input ---
@dataclass(repr=False)
class Sample:
    """One status observation of one printer. repr() never shows the job name."""
    ts: float
    state: str
    online: bool = True
    grace: bool = False
    progress: Optional[float] = None
    eta: Optional[int] = None
    print_time: Optional[int] = None
    layer: Optional[int] = None
    total_layers: Optional[int] = None
    name: Optional[str] = None
    err_code: Optional[str] = None
    err_text: Optional[str] = None
    reason: Optional[str] = None
    stage: Optional[str] = None
    model: Optional[str] = None
    kind: Optional[str] = None
    seq: Optional[int] = None
    seq_started_at: Optional[float] = None
    maint: bool = False

    def __repr__(self) -> str:
        return "Sample(ts=%r, state=%r, progress=%r, eta=%r, err_code=%r, seq=%r, name=%s)" % (
            self.ts, self.state, self.progress, self.eta, self.err_code, self.seq,
            "<set>" if self.name else None)


def _num(v: Any) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None
    if type(v) is float:
        return v
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int(v: Any) -> Optional[int]:
    if type(v) is int:
        return v
    f = _num(v)
    return None if f is None else int(f)


def _enum(v: Any) -> Optional[str]:
    v = getattr(v, "value", v)
    return None if v is None else str(v)


def sample_from_status(status: Any, now: float, maint: bool = False) -> Sample:
    """Live mode: a Sample from a PrinterStatus taken at `now`."""
    state = _enum(status.state) or "unknown"
    code, text = parse_error(getattr(status, "last_error", None))
    stage = getattr(status, "stage", None)
    reason = code
    if reason is None and state == "paused" and stage:
        reason = "stage:%s" % stage
    return Sample(
        ts=float(now), state=state, online=bool(status.online),
        grace=bool(getattr(status, "grace_period_active", False)),
        progress=_num(status.progress_pct), eta=_int(status.eta_seconds),
        print_time=_int(status.print_time_seconds), layer=_int(status.current_layer),
        total_layers=_int(status.total_layers), name=clean_name(status.job_name),
        err_code=code, err_text=text, reason=reason, stage=stage,
        model=getattr(status, "device_type", None), kind=_enum(status.kind),
        seq=_int(getattr(status, "job_seq", None)), seq_started_at=_num(getattr(status, "job_started_at", None)),
        maint=bool(maint))


def sample_from_row(row: Mapping[str, Any]) -> Sample:
    """Backfill mode: a Sample from a printer_history row (dict or sqlite3.Row).

    The optional `reason` column (spec_notifications §9) wins over parsing
    last_error. Grace rows (clones of the last good status) are flagged, so the
    machine skips them as it does live (the backfill feeds the ones that show a
    state the history never had, _backfill_grace). state='maintenance' rows
    carry no state (no data) but mark the print as touched by maintenance.
    """
    get = row.get if hasattr(row, "get") else dict(zip(row.keys(), tuple(row))).get   # sqlite3.Row: no .get
    return _history_sample(*(get(c) for c in _SAMPLE_COLS))


# The printer_history columns of _history_sample(), in its argument order.
_SAMPLE_COLS = ("recorded_at", "state", "online", "grace_period_active", "progress", "eta_sec", "print_time",
                "layer", "total_layer", "job_name", "last_error", "reason", "device_type", "kind")


def _history_sample(ts: Any, state: Any, online: Any, grace: Any, progress: Any, eta: Any, print_time: Any,
                    layer: Any, total: Any, name: Any, last_error: Any, reason: Any, model: Any, kind: Any) -> Sample:
    state = str(state or "unknown")
    code, text = parse_reason(reason) if reason else (None, None)
    if code is None:
        code, text = parse_error(last_error)
    if code:
        reason = code
    elif not (isinstance(reason, str) and reason.startswith("stage:")):
        reason = None
    return Sample(
        ts=float(ts), state=state, online=True if online is None else bool(online), grace=bool(grace),
        progress=_num(progress), eta=_int(eta), print_time=_int(print_time), layer=_int(layer),
        total_layers=_int(total), name=clean_name(name), err_code=code, err_text=text, reason=reason,
        model=model, kind=kind, maint=state == "maintenance")


def _tuple_sampler(cols: List[str]) -> Callable[[Tuple[Any, ...]], Sample]:
    """sample_from_row for history tuples with these columns, without a dict per row (the backfill)."""
    idx = [cols.index(c) if c in cols else None for c in _SAMPLE_COLS]
    if None not in idx:
        pick = operator.itemgetter(*idx)
        return lambda values: _history_sample(*pick(values))
    return lambda values: _history_sample(*(None if i is None else values[i] for i in idx))


def _backfill_grace(last: Optional[str], s: Sample) -> Tuple[Optional[str], bool]:
    """(the printer's last non-grace state, whether to skip s) for a history row.

    A grace row clones the last live status. The history keeps one row per
    60 s, so that status itself may never have been written: a print that
    ended (FINISH, then the printer powered off) often shows its end only in
    the grace clone. A grace row whose state the history has not shown yet is
    fed like any row; the rest are skipped as live does (it saw the original).
    """
    if not s.grace:
        return s.state, False
    if s.state != last and s.state not in NODATA:
        return s.state, False
    return last, True


# --- output ---
class Op:
    """One storage operation: op in open | update | close | reopen | amend.

    `fields` holds the print_jobs columns that changed since the job's previous
    op (all of them for open), DB-ready (pauses_json and ctx as JSON text).
    """
    __slots__ = ("op", "job", "fields")

    def __init__(self, op: str, job: "Job", fields: Dict[str, Any]):
        self.op, self.job, self.fields = op, job, fields

    def __iter__(self) -> Iterator[Any]:
        return iter((self.op, self.job, self.fields))

    def __repr__(self) -> str:   # fields may hold the job name: keys only
        return "Op(%s, %r, fields=%s)" % (self.op, self.job, sorted(self.fields))


class Job:
    """One print: the print_jobs columns plus the machine's working state (ctx)."""

    def __init__(self, printer_id: str, kind: Optional[str], model: Optional[str],
                 started_at: float, start_exact: int, source: str = "live"):
        self.id: Optional[int] = None          # set by the storage layer on open
        self.printer_id = printer_id
        self.kind = kind
        self.model = model
        self.job_name: Optional[str] = None
        self.started_at = float(started_at)
        self.start_exact = int(start_exact)
        self.ended_at: Optional[float] = None
        self.end_exact: Optional[int] = None
        self.last_seen = float(started_at)
        self.result: Optional[str] = None
        self.error_code: Optional[str] = None
        self.error_text: Optional[str] = None
        self.planned_sec: Optional[int] = None
        self.planned_src: Optional[str] = None
        self.actual_sec: Optional[int] = None
        self.paused_sec = 0
        self.pauses = 0
        self.pause_list: List[Dict[str, Any]] = []
        self.offline_sec = 0
        self.offline_n = 0
        self.power_losses = 0
        self.blips = 0
        self.end_progress: Optional[float] = None
        self.layer: Optional[int] = None
        self.total_layers: Optional[int] = None
        self.source = source
        self.maint = 0
        # working state, saved as ctx while the print is open
        self.lp: Optional[float] = None         # last progress
        self.le: Optional[int] = None           # last ETA
        self.lpt: Optional[int] = None          # last print time (Creality, Klipper, MKS)
        self.state: Optional[str] = None        # last active state
        self.pause_open_at: Optional[float] = None
        self.pause_code: Optional[str] = None
        self.pause_gap = 0.0                    # data gaps inside the open pause
        self.first_plan_done = False
        self.offline_since: Optional[float] = None
        self.pl_lp: Optional[float] = None      # progress at the last pause if it was a power loss
        self.pl_text: Optional[str] = None
        # memory only
        self.last_ckpt = float(started_at)
        self._emitted: Dict[str, Any] = {}

    def __repr__(self) -> str:   # never the job name
        return "Job(id=%r, printer=%r, started_at=%r, result=%r)" % (
            self.id, self.printer_id, self.started_at, self.result)

    def ctx(self) -> Dict[str, Any]:
        return {"lp": self.lp, "le": self.le, "lpt": self.lpt, "state": self.state,
                "pause_open_at": self.pause_open_at,
                "pause_code": self.pause_code, "pause_gap": self.pause_gap,
                "first_plan_done": self.first_plan_done, "offline_since": self.offline_since,
                "pl_lp": self.pl_lp, "pl_text": self.pl_text}

    def row(self) -> Dict[str, Any]:
        """The machine-owned print_jobs columns, DB-ready."""
        return {
            "printer_id": self.printer_id, "kind": self.kind, "model": self.model,
            "job_name": self.job_name, "started_at": self.started_at, "start_exact": self.start_exact,
            "ended_at": self.ended_at, "end_exact": self.end_exact, "last_seen": self.last_seen,
            "result": self.result, "error_code": self.error_code, "error_text": self.error_text,
            "planned_sec": self.planned_sec, "planned_src": self.planned_src, "actual_sec": self.actual_sec,
            "paused_sec": self.paused_sec, "pauses": self.pauses,
            "pauses_json": json.dumps(self.pause_list, ensure_ascii=False, separators=(",", ":"))
            if self.pause_list else None,
            "offline_sec": self.offline_sec, "offline_n": self.offline_n, "power_losses": self.power_losses,
            "blips": self.blips, "end_progress": self.end_progress, "layer": self.layer,
            "total_layers": self.total_layers, "source": self.source, "maint": self.maint,
            "ctx": json.dumps(self.ctx(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if self.result is None else None,
        }

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "Job":
        """A Job from a print_jobs row (an open one carries its machine state in ctx)."""
        data = dict(row)
        job = cls(data["printer_id"], data.get("kind"), data.get("model"), float(data["started_at"]),
                  int(data.get("start_exact") or 0), data.get("source") or "live")
        job.id = data.get("id")
        for col in COLUMNS:
            if col in data and col not in ("pauses_json", "ctx"):
                setattr(job, col, data[col])
        for col in ("paused_sec", "pauses", "offline_sec", "offline_n", "power_losses", "blips", "maint"):
            setattr(job, col, int(getattr(job, col) or 0))
        job.started_at, job.last_seen = float(job.started_at), float(job.last_seen or job.started_at)
        try:
            pauses = json.loads(data.get("pauses_json") or "[]")
            job.pause_list = [p for p in pauses if isinstance(p, dict)] if isinstance(pauses, list) else []
        except (TypeError, ValueError):
            job.pause_list = []
        try:
            ctx = json.loads(data.get("ctx") or "{}")
        except (TypeError, ValueError):
            ctx = {}
        ctx = ctx if isinstance(ctx, dict) else {}
        job.lp = _num(ctx.get("lp", job.end_progress))
        job.le = _int(ctx.get("le"))
        job.lpt = _int(ctx.get("lpt"))
        job.state = ctx.get("state") if ctx.get("state") in ACTIVE else ("printing" if job.result is None else None)
        job.pause_open_at = _num(ctx.get("pause_open_at"))
        job.pause_code = ctx.get("pause_code")
        job.pause_gap = _num(ctx.get("pause_gap")) or 0.0
        job.first_plan_done = bool(ctx.get("first_plan_done", job.planned_src is not None))
        job.offline_since = _num(ctx.get("offline_since"))
        job.pl_lp = _num(ctx.get("pl_lp"))
        job.pl_text = ctx.get("pl_text")
        if job.state == "paused" and job.pause_open_at is None:
            job.pause_open_at = job.last_seen
        job.last_ckpt = float(job.last_seen)
        job._emitted = job.row()
        return job

    def snapshot(self) -> Dict[str, Any]:
        return copy.deepcopy({k: v for k, v in vars(self).items() if k not in ("id", "_emitted")})

    def restore(self, snap: Dict[str, Any]) -> None:
        vars(self).update(copy.deepcopy(snap))


# --- rules ---
def new_job_reason(job: Job, s: Sample, prev_seq: Optional[int] = None) -> Optional[str]:
    """Why the active sample `s` belongs to a new print rather than `job` (§4.3), or None.

    prev_seq: the job_seq this process saw last (None after a restart). A
    counter that went down (the raw report was rebuilt) is no boundary.
    """
    if s.seq is not None and prev_seq is not None and s.seq > prev_seq:
        return "seq"
    a, b = job.job_name, s.name
    if a and b and a != b:   # the regex only when the raw names differ (rare)
        a, b = norm_name(a), norm_name(b)
        if a is not None and b is not None and a != b:
            return "name"
    return _reset_reason(_ref(job), s)


def _ref(job: Job) -> Tuple[Any, ...]:
    """What a progress reset is judged against: (lp, le, layer, total_layers, lpt, pl_lp)."""
    return job.lp, job.le, job.layer, job.total_layers, job.lpt, job.pl_lp


def _reset_reason(ref: Tuple[Any, ...], s: Sample) -> Optional[str]:
    """Why `s` starts over (progress back to <= 5) from the print `ref` (_ref() before the drop), or None."""
    lp, le, layer, total, lpt, pl_lp = ref
    p = s.progress
    if lp is None or p is None or p > 5 or lp - p < 3:
        return None
    if (pl_lp is not None and layer is not None and layer >= 5 and s.layer is not None and s.layer >= layer - 2
            and not (total and layer >= total)):
        return None   # resumed after a power loss: progress and ETA start over, the layer counter goes on
    if lp >= 95:
        return "reset_done"
    e = s.eta
    eta_jump = le is not None and e is not None and e > le + max(600, 0.25 * le)
    if lp - p >= 20 and eta_jump:
        return "reset_eta"
    # The counters start over with a new print (no 20-point minimum): the layer
    # with a new ETA or a new layer count, the print time with a new ETA.
    if layer is not None and layer >= 5 and s.layer is not None and s.layer <= 1 and (
            eta_jump or (total and s.total_layers and s.total_layers != total)):
        return "reset_layer"
    if (lpt is not None and lpt >= 600 and s.print_time is not None and s.print_time < 300
            and le is not None and e is not None and e > le + 600):
        return "reset_time"
    return None


def same_job(job: Job, s: Sample, prev_seq: Optional[int] = None) -> bool:
    return new_job_reason(job, s, prev_seq) is None


def classify_split(job: Job, s: Sample, at: Optional[float] = None) -> Tuple[str, float]:
    """(result, ended_at) of a print cut by a boundary at `at` (default s.ts): its end was never seen."""
    ts = s.ts if at is None else at
    lp, le = job.lp, job.le
    if lp is not None and lp >= 95:
        return "finished", min(ts, job.last_seen + (le or 0))
    # A real countdown of <= 2 min, once the print is under way: an ETA of 0
    # during preparation or from frozen Creality telemetry is none.
    if le is not None and 0 < le <= 120 and lp is not None and lp > 5:
        return "finished", min(ts, job.last_seen + le)
    if ts - job.last_seen >= JOBS_LOST_GAP_S:
        return "lost", job.last_seen   # back after an outage on another print: how it ended was not seen
    return "stopped", job.last_seen


def classify_terminal(job: Job, s: Sample, silent: float
                      ) -> Tuple[str, float, int, Optional[str], Optional[str]]:
    """(result, ended_at, end_exact, error_code, error_text) for a terminal sample (§4.4).

    silent: seconds since the print's last active sample.
    """
    long_gap = silent >= JOBS_LOST_GAP_S
    code, text = s.err_code, s.err_text
    if s.state == "finished":
        if not long_gap:
            return "finished", s.ts, 1, None, None
        return "finished", min(s.ts, job.last_seen + (job.le or 0)), 0, None, None
    end, exact = (job.last_seen, 0) if long_gap else (s.ts, 1)
    if s.state == "cancelled" or code in CANCEL_CODES:
        return "cancelled", end, exact, code, text
    if s.state == "error":
        if code:
            return "failed", end, exact, code, text
        if _all_done(job):   # P2S goes FAILED without a code after the last layer
            return "finished", end, exact, None, None
        return "stopped", end, exact, None, None
    if long_gap:
        return "lost", job.last_seen, 0, None, None
    if job.lp is not None and job.lp >= 99:   # MKS and Creality end like this
        return "finished", s.ts, 1, None, None
    return "stopped", s.ts, 1, None, None


def _all_done(job: Job) -> bool:
    """Every layer printed: progress >= 99 with no time left (and the last layer, when known)."""
    if job.lp is None or job.lp < 99 or (job.le or 0) != 0:
        return False
    return not (job.layer and job.total_layers) or job.layer >= job.total_layers


def _frozen(kind: Optional[str], s: Sample) -> bool:
    """Creality K1C / Ender 5 Max after a print stopped: state printing, the progress frozen, ETA,
    print time and layers 0 (25 runs in 3 months, every one up to the next print). Not printing."""
    return (kind == "creality" and s.state == "printing" and s.eta == 0 and s.print_time == 0
            and not s.total_layers and s.progress is not None and 0 < s.progress < 99)


def _power_loss(job: Job, base: str, code: Optional[str], text: Optional[str]
                ) -> Tuple[str, Optional[str], Optional[str]]:
    """The power-loss override: a print that died in / right after a 0300_8007 pause."""
    if base in ("cancelled", "failed", "stopped", "lost"):
        if code == POWER_LOSS_CODE:
            return "power_loss", code, text
        if job.pl_lp is not None and (job.lp is None or job.lp < job.pl_lp + 1):
            return "power_loss", POWER_LOSS_CODE, job.pl_text
    return base, code, text


def _plan(job: Job, s: Sample) -> bool:
    """Record the plan once, on the first suitable sample (§4.6). True when set."""
    p, eta, pt = s.progress, s.eta, s.print_time
    planned = src = None
    if job.kind == "bambu":
        if job.start_exact and eta is not None and eta > 0 and p is not None and p <= 2:
            planned, src = (s.ts - job.started_at) + eta, "slicer"
        elif not job.start_exact and pt and pt > 0 and eta is not None and p is not None and p <= 5:
            planned, src = pt + eta, "printer"
    elif job.kind == "creality":
        if pt and pt > 0 and eta is not None and eta >= 0:
            planned, src = pt + eta, "printer"
    elif job.kind in ("klipper", "mks"):
        if pt and pt > 0 and p is not None and p >= 5:
            planned, src = pt / (p / 100.0), "linear"
    if src is None:
        return False
    job.planned_sec, job.planned_src, job.first_plan_done = int(round(planned)), src, True
    return True


class _Closed:
    """A print closed by a terminal sample, kept JOBS_TERM_GRACE_S for a reopen or an amend.

    state: the closing sample's state. A Bambu print that went FAILED (state
    error) never resumes: the next RUNNING is a new print (bambu_raw counts it
    in job_seq too), so it can only be amended, not reopened.
    blank: the closing sample carried no job (no name, no progress), as a Bambu
    reconnect does. Such an idle close stays reopenable up to JOBS_MAX_GAP_S for
    the same print coming back where it was (resumes()).
    """
    __slots__ = ("job", "snap", "at", "base", "state", "blank")

    def __init__(self, job: Job, snap: Dict[str, Any], at: float, base: str, state: Optional[str] = None,
                 blank: bool = False):
        self.job, self.snap, self.at, self.base, self.state, self.blank = job, snap, at, base, state, blank

    def reopenable(self) -> bool:
        return self.base in _REOPENABLE and not (self.state == "error" and self.job.kind == "bambu")

    def long_lived(self) -> bool:
        return (self.blank and self.state in ("idle", "unknown") and self.base in ("stopped", "lost")
                and self.job.lp is not None and self.job.lp > 5)

    def resumes(self, s: Sample) -> bool:
        """s carries on from where the print was: a print never advances without running."""
        job = self.job
        return (self.long_lived() and s.progress is not None and s.progress >= job.lp - 1
                and job.job_name is not None and clean_name(s.name) == job.job_name)


class _Drop:
    """A progress drop that was no boundary on its own sample: the print as it was before it."""
    __slots__ = ("ref", "snap", "at", "left")

    def __init__(self, job: Job, at: float):
        self.ref, self.snap, self.at, self.left = _ref(job), job.snapshot(), at, DROP_SAMPLES


class JobMachine:
    """Prints of one printer from its samples. Not thread-safe: one feeder at a time."""

    def __init__(self, printer_id: str, kind: Optional[str] = None, model: Optional[str] = None,
                 source: str = "live"):
        self.printer_id = printer_id
        self.kind = kind
        self.model = model
        self.source = source
        self.job: Optional[Job] = None
        self.last_closed: Optional[_Closed] = None
        self.seq: Optional[int] = None          # last job_seq seen in this process
        self.diffs = True                       # False (the backfill): ops carry no fields, job.row() is written once
        self._drop: Optional[_Drop] = None      # a progress drop still being judged (memory only)
        self._newest: Optional[float] = None    # newest accepted sample (clock check)
        self._data_ts: Optional[float] = None   # last sample with a state (not offline / maintenance)
        self._data_state: Optional[str] = None

    @classmethod
    def restore(cls, row: Mapping[str, Any], source: str = "live") -> "JobMachine":
        """A machine continuing the open print stored in `row` (after a restart).

        job_seq is not restored: the counter restarts with the process.
        """
        job = Job.from_row(row)
        m = cls(job.printer_id, job.kind, job.model, source)
        m.job = job
        return m

    # -- public --
    def feed(self, s: Sample) -> List[Op]:
        if s.grace:   # a clone of the last good status: nothing new in it
            return []
        if self._newest is not None and s.ts < self._newest - CLOCK_BACK_S:
            logger.debug("[jobs] %s: clock went back %d s, sample skipped", self.printer_id, self._newest - s.ts)
            return []
        self._newest = s.ts if self._newest is None else max(self._newest, s.ts)
        self.kind = self.kind or s.kind
        self.model = self.model or s.model
        if _frozen(self.kind, s):
            s = copy.copy(s)
            s.state = "idle"
        ops = self.expire(s.ts)
        if s.state in NODATA or not s.online:
            job = self.job
            if job is not None:
                if job.offline_since is None:
                    job.offline_since = s.ts
                if s.maint and not job.maint:
                    job.maint = 1
                    ops.append(self._emit("update", job, s.ts))
            return ops
        ops += self._active(s) if s.state in ACTIVE else self._terminal(s)
        if s.seq is not None:
            self.seq = s.seq
        self._data_ts, self._data_state = s.ts, s.state
        return ops

    def expire(self, now: float) -> List[Op]:
        """Close the open print as lost when it has had no data for JOBS_MAX_GAP_S."""
        job = self.job
        if job is None or now - job.last_seen <= JOBS_MAX_GAP_S:
            return []
        logger.debug("[jobs] %s: print #%s without data for %d s", self.printer_id, job.id, now - job.last_seen)
        return [self._close(job, "lost", job.last_seen, 0, None, None, now, keep=False)]

    # -- active samples --
    def _active(self, s: Sample) -> List[Op]:
        job, reopened = self.job, False
        if job is not None:
            why, at, drop = new_job_reason(job, s, self.seq), None, self._drop
            if why is None and drop is not None:
                why = _reset_reason(drop.ref, s)
                if why is not None:   # the new print began at the drop: undo what its samples did to this one
                    job.restore(drop.snap)
                    at = drop.at
            if why is not None:
                started, exact = self._start_of(s, split_from=job) if at is None else (at, 0)
                result, end = classify_split(job, s, at)
                if exact:
                    end = min(end, started)
                logger.debug("[jobs] %s: boundary (%s) after print #%s", self.printer_id, why, job.id)
                return [self._close(job, result, end, 0, None, None, s.ts, keep=False), self._open(s, started, exact)]
            if drop is not None:
                drop.left -= 1
                if drop.left <= 0:
                    self._drop = None
            elif s.progress is not None and s.progress <= 5 and job.lp is not None and job.lp - s.progress >= 3:
                self._drop = _Drop(job, s.ts)
        else:
            lc = self._closed_alive(s.ts)
            self.last_closed = None
            if lc is None or not lc.reopenable() or not same_job(lc.job, s, self.seq) or not (
                    s.ts - lc.at <= JOBS_TERM_GRACE_S or lc.resumes(s)):
                started, exact = self._start_of(s)
                return [self._open(s, started, exact)]
            job = lc.job
            job.restore(lc.snap)
            job.blips += 1
            self.job, reopened = job, True
            logger.debug("[jobs] %s: print #%s reopened (false end)", self.printer_id, job.id)
        changed = self._absorb(job, s)
        if reopened:
            return [self._emit("reopen", job, s.ts)]
        if changed or s.ts - job.last_ckpt >= JOBS_CHECKPOINT_S:
            return [self._emit("update", job, s.ts)]
        return []

    def _start_of(self, s: Sample, split_from: Optional[Job] = None) -> Tuple[float, int]:
        """(started_at, start_exact) of a print whose first sample is `s`."""
        sa = s.seq_started_at
        if (s.seq is not None and self.seq is not None and s.seq > self.seq and sa is not None
                and s.ts - JOBS_LOST_GAP_S <= sa <= s.ts
                and (split_from is None or sa >= split_from.last_seen)):
            return float(sa), 1   # the paho thread saw the printer start it
        if split_from is not None:
            return s.ts, 0
        # Right after an idle, unless it is first seen far into its run (a Bambu
        # reconnect idle): progress past 5 % and no print time saying it just began
        # (Klipper jumps to 5-18 % at once: the gcode header).
        early = (s.progress is None or s.progress <= 5
                 or (s.print_time is not None and s.print_time < JOBS_LOST_GAP_S))
        exact = (self._data_ts is not None and self._data_state not in ACTIVE
                 and 0 <= s.ts - self._data_ts < JOBS_LOST_GAP_S and early)
        return s.ts, int(exact)

    def _open(self, s: Sample, started: float, exact: int) -> Op:
        job = Job(self.printer_id, s.kind or self.kind, s.model or self.model, started, exact, self.source)
        self._absorb(job, s)
        self.job, self.last_closed, self._drop = job, None, None
        return self._emit("open", job, s.ts)

    def _absorb(self, job: Job, s: Sample) -> bool:
        """Apply an active sample of `job`. True when something worth saving changed."""
        silent = max(0.0, s.ts - job.last_seen)
        long_gap = silent >= JOBS_LOST_GAP_S
        reason = s.reason or s.err_code
        text = s.err_text if reason == s.err_code else None
        changed = job.state != s.state
        if s.state == "paused" and job.state != "paused":
            power = reason == POWER_LOSS_CODE
            if long_gap and not power:
                self._offline(job, silent)
            # A power loss right after a gap: the printer was off since we last saw it.
            self._pause_open(job, job.last_seen if power else s.ts)
            if reason:
                self._pause_reason(job, s, reason, text)
        else:
            if long_gap:
                self._offline(job, silent)
                changed = True
            if job.state == "paused":
                if long_gap:
                    job.pause_gap += silent
                if s.state == "paused":
                    if job.pause_code is None and reason:
                        self._pause_reason(job, s, reason, text)
                        changed = True
                else:
                    self._pause_close(job, s.ts)
        if s.progress is not None:
            job.lp = job.end_progress = float(s.progress)
        if s.eta is not None:
            job.le = int(s.eta)
        if s.print_time is not None:
            job.lpt = int(s.print_time)
        if s.layer is not None:
            job.layer = int(s.layer)
        if s.total_layers is not None and s.progress is not None and s.progress > 5:
            job.total_layers = int(s.total_layers)   # early values echo the previous print
        name = clean_name(s.name)
        if name is not None and name != job.job_name and (
                job.job_name is None or (norm_name(job.job_name) is None and norm_name(name) is not None)):
            job.job_name = name   # a real name replaces the generic plate_N.gcode, never the other way
            changed = True
        if not job.first_plan_done and _plan(job, s):
            changed = True
        if s.maint and not job.maint:
            job.maint = 1
            changed = True
        job.state, job.last_seen, job.offline_since = s.state, s.ts, None
        return changed

    @staticmethod
    def _offline(job: Job, silent: float) -> None:
        job.offline_n += 1
        job.offline_sec += int(round(silent))

    @staticmethod
    def _pause_open(job: Job, at: float) -> None:
        job.pauses += 1
        job.pause_open_at, job.pause_gap, job.pause_code = at, 0.0, None
        job.pl_lp = job.pl_text = None   # «the last pause» is this one now
        if len(job.pause_list) < PAUSES_MAX:
            job.pause_list.append({"at": int(round(at)), "sec": None, "code": None, "text": None})

    @staticmethod
    def _pause_reason(job: Job, s: Sample, code: str, text: Optional[str]) -> None:
        job.pause_code = code
        if job.pauses <= len(job.pause_list):
            job.pause_list[job.pauses - 1].update(code=code, text=_clip(text))
        if code == POWER_LOSS_CODE:
            job.power_losses += 1
            seen = [v for v in (job.lp, s.progress) if v is not None]
            job.pl_lp, job.pl_text = (max(seen) if seen else 0.0), _clip(text)

    @staticmethod
    def _pause_close(job: Job, end: float) -> None:
        sec = max(0, int(round(end - job.pause_open_at - job.pause_gap)))
        job.paused_sec += sec
        if job.pauses <= len(job.pause_list):
            job.pause_list[job.pauses - 1]["sec"] = sec
        job.pause_open_at, job.pause_gap, job.pause_code = None, 0.0, None

    # -- terminal samples --
    def _terminal(self, s: Sample) -> List[Op]:
        job = self.job
        if job is None:
            lc = self._closed_alive(s.ts)
            return self._amend(lc, s) if lc is not None and s.ts - lc.at <= JOBS_TERM_GRACE_S else []
        base, end, exact, code, text = classify_terminal(job, s, max(0.0, s.ts - job.last_seen))
        if base == "finished" and s.progress is not None and s.progress > (job.end_progress or 0):
            job.end_progress = float(s.progress)
        if s.maint:
            job.maint = 1
        blank = clean_name(s.name) is None and not s.progress
        return [self._close(job, base, end, exact, code, text, s.ts, keep=True, state=s.state, blank=blank)]

    def _closed_alive(self, ts: float) -> Optional[_Closed]:
        """The last closed print while it may still be reopened or amended: JOBS_TERM_GRACE_S
        (a blank idle close: JOBS_MAX_GAP_S, for resumes() only)."""
        lc = self.last_closed
        if lc is not None:
            age = ts - lc.at
            if age > JOBS_MAX_GAP_S or (age > JOBS_TERM_GRACE_S and not lc.long_lived()):
                lc = self.last_closed = None
        return lc

    def _amend(self, lc: _Closed, s: Sample) -> List[Op]:
        """A code that arrived after the end refines «stopped» (0300_400C lives seconds)."""
        if lc.base != "stopped":
            return []
        code, text = s.err_code, s.err_text
        if s.state == "finished":
            base, code, text = "finished", None, None
        elif s.state == "cancelled" or code in CANCEL_CODES:
            base = "cancelled"
        elif s.state == "error" and code:
            base = "failed"
        else:
            return []
        job = lc.job
        lc.base = base
        if base not in _REOPENABLE:
            self.last_closed = None   # the printer said how it ended: no reopen any more
        result, code, text = _power_loss(job, base, code, text)
        if (result, code) == (job.result, job.error_code):
            return []
        job.result, job.error_code, job.error_text = result, code, _clip(text)
        logger.debug("[jobs] %s: print #%s refined to %s", self.printer_id, job.id, result)
        return [self._emit("amend", job, s.ts)]

    def _close(self, job: Job, base: str, end: float, exact: int, code: Optional[str], text: Optional[str],
               at: float, keep: bool, state: Optional[str] = None, blank: bool = False) -> Op:
        snap = job.snapshot() if keep and base in _REOPENABLE else None
        result, code, text = _power_loss(job, base, code, text)
        if job.pause_open_at is not None:
            self._pause_close(job, end)
        job.result, job.ended_at, job.end_exact = result, float(end), int(exact)
        job.actual_sec = max(0, int(round(end - job.started_at)))
        job.error_code, job.error_text = code, _clip(text)
        self.job, self._drop = None, None
        self.last_closed = _Closed(job, snap, at, base, state, blank) if snap is not None else None
        logger.debug("[jobs] %s: print #%s closed: %s", self.printer_id, job.id, result)
        return self._emit("close", job, at)

    def _emit(self, op: str, job: Job, ts: float) -> Op:
        if not self.diffs:   # the backfill: job.row() is written once, at the end
            job.last_ckpt = ts
            return Op(op, job, {})
        row = job.row()
        if op == "open":
            fields = dict(row)
        else:
            prev = job._emitted
            fields = {k: v for k, v in row.items() if k not in prev or prev[k] != v}
        job._emitted, job.last_ckpt = row, ts
        return Op(op, job, fields)


# --- storage (spec §3, §5.3, §5.4) ---
def _env_days(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, default)))
    except ValueError:
        return default


# Closed prints older than this are pruned with the hourly history prune.
JOBS_RETENTION_DAYS = _env_days("JOBS_RETENTION_DAYS", 730)
JOBS_RETENTION_S = JOBS_RETENTION_DAYS * 86400

# print_jobs, column by column (spec §3.1). init_db adds a column missing from
# an existing table, so a column added later must be ALTER-able: nullable or
# NOT NULL with a DEFAULT. The NOT NULL ones without a DEFAULT exist since v1.
_JOBS_TABLE = (
    ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
    ("printer_id", "TEXT NOT NULL"),       # stable registry id; the label is looked up on read
    ("kind", "TEXT NOT NULL"),             # bambu | creality | klipper | mks
    ("model", "TEXT"),
    ("job_name", "TEXT"),                  # sensitive (spec §10): never logged
    ("started_at", "REAL NOT NULL"),
    ("start_exact", "INTEGER NOT NULL DEFAULT 1"),
    ("ended_at", "REAL"),
    ("end_exact", "INTEGER"),
    ("last_seen", "REAL NOT NULL"),
    ("result", "TEXT"),                    # NULL while the print is open
    ("error_code", "TEXT"),
    ("error_text", "TEXT"),
    ("planned_sec", "INTEGER"),
    ("planned_src", "TEXT"),
    ("actual_sec", "INTEGER"),
    ("paused_sec", "INTEGER NOT NULL DEFAULT 0"),
    ("pauses", "INTEGER NOT NULL DEFAULT 0"),
    ("pauses_json", "TEXT"),
    ("offline_sec", "INTEGER NOT NULL DEFAULT 0"),
    ("offline_n", "INTEGER NOT NULL DEFAULT 0"),
    ("power_losses", "INTEGER NOT NULL DEFAULT 0"),
    ("blips", "INTEGER NOT NULL DEFAULT 0"),
    ("end_progress", "REAL"),
    ("layer", "INTEGER"),
    ("total_layers", "INTEGER"),
    ("source", "TEXT NOT NULL"),           # live | backfill: who opened the row
    ("maint", "INTEGER NOT NULL DEFAULT 0"),
    ("verdict", "TEXT"),
    ("verdict_by", "TEXT"),
    ("verdict_at", "REAL"),
    ("ctx", "TEXT"),                       # machine state while open (JSON), NULL once closed
    ("updated_at", "REAL NOT NULL"),
)
# Columns a write may set (updated_at is set by every write itself).
_WRITABLE = frozenset(COLUMNS) | {"verdict", "verdict_by", "verdict_at"}


def init_db(conn: sqlite3.Connection) -> None:
    """Create print_jobs, its indexes and job_journal_meta; idempotent, commits.

    Called by StateStore._init_db. Columns newer code knows are added to an
    existing table (PRAGMA table_info + ALTER TABLE). Older code just doesn't
    see the tables, so a rollback of the app is safe.
    """
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS print_jobs (%s)"
                     % ", ".join("%s %s" % col for col in _JOBS_TABLE))
        have = {row[1] for row in conn.execute("PRAGMA table_info(print_jobs)").fetchall()}
        for name, decl in _JOBS_TABLE:
            if name not in have:
                conn.execute("ALTER TABLE print_jobs ADD COLUMN %s %s" % (name, decl))
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_started ON print_jobs(started_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_printer_started ON print_jobs(printer_id, started_at)")
        # The read side's window bound from below: (ended_at >= fr OR ended_at IS NULL), job_queries.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_ended ON print_jobs(ended_at)")
        # One open print per printer: neither the live tracker nor the backfill can open a second one.
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_one_open ON print_jobs(printer_id)"
                     " WHERE result IS NULL")
        conn.execute("CREATE TABLE IF NOT EXISTS job_journal_meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def prune(conn: sqlite3.Connection, now: float) -> None:
    """Delete closed prints that started before the retention window; open ones stay.

    Runs inside the caller's transaction (StateStore._prune_locked commits it
    together with the history prune). Never raises.
    """
    try:
        conn.execute("DELETE FROM print_jobs WHERE result IS NOT NULL AND started_at < ?",
                     (now - JOBS_RETENTION_S,))
    except Exception:
        logger.error("[jobs] print_jobs retention prune failed", exc_info=True)


def read_meta(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM job_journal_meta WHERE key = ?", (key,)).fetchone()
    return None if row is None else row[0]


def write_meta(conn: sqlite3.Connection, items: Mapping[str, Any]) -> None:
    """INSERT OR REPLACE job_journal_meta keys (the caller commits). Counts only, never names."""
    conn.executemany("INSERT OR REPLACE INTO job_journal_meta (key, value) VALUES (?, ?)",
                     [(k, None if v is None else str(v)) for k, v in items.items()])


def _dicts(cur: sqlite3.Cursor) -> List[Dict[str, Any]]:
    names = [d[0] for d in cur.description]
    return [dict(zip(names, row)) for row in cur.fetchall()]


def _open_row(conn: sqlite3.Connection, printer_id: str) -> Optional[Dict[str, Any]]:
    rows = _dicts(conn.execute("SELECT * FROM print_jobs WHERE printer_id = ? AND result IS NULL", (printer_id,)))
    return rows[0] if rows else None


def insert_job(conn: sqlite3.Connection, fields: Mapping[str, Any], now: float) -> int:
    """INSERT one print_jobs row from DB-ready fields; returns its id.

    Raises sqlite3.IntegrityError when an open row is inserted for a printer
    that already has one (idx_jobs_one_open).
    """
    cols = [c for c in fields if c in _WRITABLE]
    vals = [fields[c] for c in cols]
    if "kind" in cols and vals[cols.index("kind")] is None:
        vals[cols.index("kind")] = "unknown"   # NOT NULL; the collectors always know it
    cur = conn.execute("INSERT INTO print_jobs (%s) VALUES (%s)"
                       % (", ".join(cols + ["updated_at"]), ", ".join("?" * (len(cols) + 1))),
                       vals + [now])
    return int(cur.lastrowid)


def _update_job(conn: sqlite3.Connection, job_id: int, fields: Mapping[str, Any], now: float) -> bool:
    """UPDATE the changed columns of row job_id. False when the row is gone."""
    # kind is fixed at the insert (in memory it may still be None: NOT NULL here).
    cols = [c for c in fields if c in _WRITABLE and c != "kind"]
    if not cols:
        return True
    cur = conn.execute("UPDATE print_jobs SET %s, updated_at = ? WHERE id = ?"
                       % ", ".join("%s = ?" % c for c in cols),
                       [fields[c] for c in cols] + [now, job_id])
    return cur.rowcount > 0


class JournalDB:
    """Writes a JobMachine's ops to print_jobs through StateStore.db_write (spec §5.3).

    - one transaction per apply(); a Job gets its id only once it committed;
    - a failed write is not lost: the next apply() for that printer writes the
      jobs it touched in full (closed ones first, they free the open slot);
    - an open row refused by idx_jobs_one_open (an open print is already
      stored, e.g. by the backfill) makes the machine continue the stored print.
    Logs carry printer ids and print numbers only, never job names. Not
    thread-safe: one feeding thread (the poll loop) per JournalDB.
    """

    def __init__(self, store: Any, clock: Callable[[], float] = time.time):
        self._store = store
        self._clock = clock
        self._retry: Dict[str, List[Job]] = {}

    def apply(self, machine: JobMachine, ops: List[Op], now: Optional[float] = None) -> bool:
        """Write the ops `machine.feed()` returned. False when the write failed (retried next time)."""
        pid = machine.printer_id
        retry = self._retry.pop(pid, [])
        if not ops and not retry:
            return True
        now = self._clock() if now is None else float(now)
        retry.sort(key=lambda j: j.result is None)   # stable: closed prints first
        in_full = {id(j) for j in retry}
        # The machine's state is ahead of every op of this batch, so a job
        # written in full skips its ops here.
        steps = [(None, j) for j in retry] + [(op, op.job) for op in ops if id(op.job) not in in_full]
        new_ids: Dict[int, int] = {}
        full: Dict[int, Dict[str, Any]] = {}
        conflict: List[Dict[str, Any]] = []

        def write(conn: sqlite3.Connection) -> None:
            dropped = set()

            def insert(job: Job, fields: Mapping[str, Any]) -> None:
                try:
                    new_ids[id(job)] = insert_job(conn, fields, now)
                except sqlite3.IntegrityError as e:
                    row = None
                    if fields.get("result") is None and "UNIQUE" in str(e):
                        row = _open_row(conn, job.printer_id)
                    if row is None:
                        raise
                    conflict.append(row)
                    dropped.add(id(job))

            for op, job in steps:
                key = id(job)
                if key in dropped or key in full:
                    continue
                rid = new_ids.get(key, job.id)
                if op is not None and op.op == "open" and rid is None:
                    insert(job, op.fields)
                elif op is not None and rid is not None and _update_job(conn, rid, op.fields, now):
                    pass
                else:   # retried, or its row never reached the DB / is gone: write it in full
                    full[key] = job.row()
                    if rid is None or not _update_job(conn, rid, full[key], now):
                        insert(job, full[key])

        if not self._store.db_write(write):
            keep: List[Job] = []
            for _, job in steps:
                if all(job is not k for k in keep):
                    keep.append(job)
            self._retry[pid] = keep
            logger.warning("[jobs] %s: print_jobs write failed, %d print(s) will be written again",
                           pid, len(keep))
            return False
        for _, job in steps:
            key = id(job)
            if key in new_ids:
                job.id = new_ids[key]
            if key in full:
                job._emitted = full[key]   # later diffs are relative to what the DB holds now
        if conflict:
            row = conflict[0]
            machine.job, machine.last_closed = Job.from_row(row), None
            logger.warning("[jobs] %s: print #%s is already open in the journal, continuing it", pid, row.get("id"))
        return True

    def load_open(self, now: Optional[float] = None) -> Dict[str, JobMachine]:
        """Machines continuing the prints the previous process left open (spec §5.4).

        A stored last_seen lags up to JOBS_CHECKPOINT_S behind; the newest
        active, non-grace printer_history row after it is the better value.
        A print without data for JOBS_MAX_GAP_S is closed as lost right away.
        """
        now = self._clock() if now is None else float(now)
        rows: List[Dict[str, Any]] = []

        def read(conn: sqlite3.Connection) -> None:
            # The partial index (init_db made it): the planner would scan the whole table.
            rows[:] = _dicts(conn.execute("SELECT * FROM print_jobs INDEXED BY idx_jobs_one_open"
                                          " WHERE result IS NULL ORDER BY id"))
            for row in rows:
                seen = conn.execute(
                    "SELECT MAX(recorded_at) FROM printer_history WHERE printer_id = ?"
                    " AND recorded_at > ? AND recorded_at <= ? AND state IN ('printing', 'paused')"
                    " AND COALESCE(grace_period_active, 0) = 0",
                    (row["printer_id"], row["last_seen"] or row["started_at"], now)).fetchone()[0]
                if seen is not None:
                    row["last_seen"] = seen

        if not self._store.db_write(read):
            return {}
        machines: Dict[str, JobMachine] = {}
        for row in rows:
            try:
                m = JobMachine.restore(row)
            except Exception:
                logger.error("[jobs] open print #%s could not be restored", row.get("id"), exc_info=True)
                continue
            self.apply(m, m.expire(now), now)
            machines[m.printer_id] = m
        return machines


# --- «Годно / Брак» (spec §7.4, §9) ---
VERDICTS = ("good", "defect")


def set_verdict(db: str, job_id: int, verdict: Optional[str], by: Optional[str],
                now: Optional[float] = None) -> str:
    """Store the owner's «Годно / Брак» mark of a print that has ended.

    verdict None clears the mark together with its author and time. Runs on its
    own short connection: marks are rare, and under WAL with a busy timeout it
    coexists with the poll loop's writer. The machines never write the verdict
    columns, so a later amend or a retried write of the print keeps the mark.
    Returns "ok", "missing" (no such print, no journal) or "open" (the print
    still runs: no marks then). Raises ValueError on an unknown verdict. Logs
    nothing (spec §10).
    """
    if verdict is not None and verdict not in VERDICTS:
        raise ValueError("unknown verdict")
    if not os.path.exists(db):
        return "missing"   # mode=rw below never creates the file
    at = time.time() if now is None else float(now)
    conn = sqlite3.connect(pathlib.Path(db).absolute().as_uri() + "?mode=rw", uri=True, timeout=10)
    try:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'print_jobs'").fetchone() is None:
            return "missing"
        cur = conn.execute("UPDATE print_jobs SET verdict = ?, verdict_by = ?, verdict_at = ?, updated_at = ?"
                           " WHERE id = ? AND result IS NOT NULL",
                           (verdict, by if verdict else None, at if verdict else None, at, int(job_id)))
        if cur.rowcount:
            conn.commit()
            return "ok"
        found = conn.execute("SELECT 1 FROM print_jobs WHERE id = ?", (int(job_id),)).fetchone()
        return "missing" if found is None else "open"
    finally:
        conn.close()   # an uncommitted (empty) UPDATE is rolled back


# --- backfill (spec §6) ---
# Raise it when the rules change: the next start rebuilds the backfilled prints
# from the history that retention has left (spec §6.3).
BACKFILL_VERSION = 1
BACKFILL_CHUNK = 5000       # history rows per read transaction
BACKFILL_PAUSE_S = 0.05     # between chunks: the poll loop and uvicorn get their turn
BACKFILL_KEEP_S = 86400     # on a re-run, backfilled prints of the oldest history day stay as they are
# printer_history columns the machines read; the optional ones only when the table has them.
_HISTORY_COLS = ("id", "printer_id", "kind", "state", "online", "progress", "eta_sec", "print_time", "layer",
                 "total_layer", "job_name", "last_error", "device_type", "recorded_at")
_HISTORY_OPTIONAL = ("grace_period_active", "reason")


class BackfillCancelled(Exception):
    """stop() was called while the backfill ran: nothing has been written."""


def _db_file(store: Any) -> Optional[str]:
    """The file behind the store's write connection (the backfill reads it on its own connection)."""
    rows: List[Tuple[Any, ...]] = []
    if not store.db_write(lambda conn: rows.extend(conn.execute("PRAGMA database_list").fetchall())):
        return None
    return next((r[2] for r in rows if r[1] == "main" and r[2]), None)


def _connect_ro(path: str) -> sqlite3.Connection:
    """A read-only connection: the backfill can never write the history by mistake."""
    uri = pathlib.Path(path).absolute().as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=10)


def _replay(conn: sqlite3.Connection, chunk: int, pause: float, cancel: Optional[threading.Event]
            ) -> Tuple[List[Job], Dict[str, Any]]:
    """Feed printer_history to one machine per printer in insertion (rowid) order.

    Rowid order instead of (printer_id, recorded_at): the rows of all printers
    share the pages, a per-printer walk would read the table once per printer;
    the insertion order is also immune to clock jumps. Every chunk is its own
    short read transaction, so no WAL snapshot is held for minutes. The pass
    goes on past the starting MAX(id) to the end of the table: the poll loop
    keeps writing history meanwhile (the catch-up pass).
    """
    have = {r[1] for r in conn.execute("PRAGMA table_info(printer_history)").fetchall()}
    cols = list(_HISTORY_COLS) + list(_HISTORY_OPTIONAL)
    # An optional column the table lacks reads as NULL: every row is a full tuple.
    select = [c if c in have or c not in _HISTORY_OPTIONAL else "NULL AS %s" % c for c in cols]
    cutoff = conn.execute("SELECT MAX(id) FROM printer_history").fetchone()[0] or 0
    oldest = conn.execute("SELECT MIN(recorded_at) FROM printer_history").fetchone()[0]
    sql = "SELECT %s FROM printer_history WHERE id > ? ORDER BY id LIMIT ?" % ", ".join(select)
    make = _tuple_sampler(cols)
    i_pid, i_kind, i_model = cols.index("printer_id"), cols.index("kind"), cols.index("device_type")
    machines: Dict[str, JobMachine] = {}
    last_state: Dict[str, Optional[str]] = {}
    jobs: List[Job] = []
    rows = late = last_id = 0
    while True:
        if cancel is not None and cancel.is_set():
            raise BackfillCancelled()
        batch = conn.execute(sql, (last_id, chunk)).fetchall()
        for values in batch:
            pid = values[i_pid]
            m = machines.get(pid)
            if m is None:
                m = machines[pid] = JobMachine(pid, kind=values[i_kind], model=values[i_model], source="backfill")
                m.diffs = False
            s = make(values)
            last_state[pid], s.grace = _backfill_grace(last_state.get(pid), s)
            for op in m.feed(s):
                if op.op == "open":
                    jobs.append(op.job)
        if batch:
            rows += len(batch)
            last_id = batch[-1][0]
            late += sum(1 for values in batch if values[0] > cutoff)
        if len(batch) < chunk:
            break
        if pause:
            time.sleep(pause)
    return jobs, {"rows": rows, "late_rows": late, "max_id": last_id, "oldest": oldest}


class _Kept:
    """The prints a re-run must not duplicate, per printer: [start, end) with a prefix max of the ends."""

    def __init__(self, intervals: List[Tuple[float, float]]):
        intervals.sort()
        self.starts = [s for s, _ in intervals]
        self.max_end: List[float] = []
        top = float("-inf")
        for _, e in intervals:
            top = max(top, e)
            self.max_end.append(top)

    def overlaps(self, start: float, end: float) -> bool:
        k = bisect.bisect_left(self.starts, end)   # kept prints that start before this one ends
        return k > 0 and self.max_end[k - 1] > start


def _span(started_at: Any, ended_at: Any) -> Tuple[float, float]:
    start = float(started_at)
    end = float("inf") if ended_at is None else float(ended_at)
    return start, max(end, start + 1e-3)   # a zero-length print still occupies its instant


def _write_backfill(conn: sqlite3.Connection, jobs: List[Job], info: Dict[str, Any], now: float,
                    out: Dict[str, Any]) -> None:
    """The backfill's single transaction (spec §6.1 p. 6, §6.3); runs inside StateStore.db_write.

    - another process already wrote this version: nothing to do;
    - a re-run (new BACKFILL_VERSION) replaces the closed backfilled prints that
      started from the oldest history day + BACKFILL_KEEP_S on; older ones are
      beyond what the history still shows and stay. Their «Годно/Брак» verdicts
      come back by (printer, start second);
    - a rebuilt print overlapping a print that stays (live ones are never
      touched, an open one is being continued by the tracker) is not inserted.
    """
    if read_meta(conn, "backfill_version") == str(BACKFILL_VERSION):
        out["done_elsewhere"] = True
        return
    oldest = info.get("oldest")
    keep_before = float("inf") if oldest is None else float(oldest) + BACKFILL_KEEP_S
    verdicts = {(pid, int(round(st))): (v, by, at) for pid, st, v, by, at in conn.execute(
        "SELECT printer_id, started_at, verdict, verdict_by, verdict_at FROM print_jobs WHERE source = 'backfill'"
        " AND result IS NOT NULL AND started_at >= ? AND verdict IS NOT NULL", (keep_before,)).fetchall()}
    replaced = conn.execute("DELETE FROM print_jobs WHERE source = 'backfill' AND result IS NOT NULL"
                            " AND started_at >= ?", (keep_before,)).rowcount
    spans: Dict[str, List[Tuple[float, float]]] = defaultdict(list)
    for pid, st, en in conn.execute("SELECT printer_id, started_at, ended_at FROM print_jobs").fetchall():
        spans[pid].append(_span(st, en))
    kept = {pid: _Kept(lst) for pid, lst in spans.items()}
    results: Counter = Counter()
    skipped = restored = 0
    for job in sorted(jobs, key=lambda j: j.result is None):   # stable: closed first, then the open ones
        k = kept.get(job.printer_id)
        if k is not None and k.overlaps(*_span(job.started_at, job.ended_at)):
            skipped += 1
            continue
        fields = job.row()
        verdict = verdicts.get((job.printer_id, int(round(job.started_at)))) if job.result is not None else None
        if verdict is not None:
            fields.update(verdict=verdict[0], verdict_by=verdict[1], verdict_at=verdict[2])
            restored += 1
        try:
            job.id = insert_job(conn, fields, now)
        except sqlite3.IntegrityError:
            if job.result is not None:
                raise
            skipped += 1   # an open print is already stored for this printer
            continue
        results[job.result or "open"] += 1
    stats = {"rows": info["rows"], "late_rows": info["late_rows"], "jobs": len(jobs),
             "inserted": sum(results.values()), "skipped": skipped, "replaced": replaced,
             "verdicts": restored, "results": dict(sorted(results.items()))}
    write_meta(conn, {"backfill_version": BACKFILL_VERSION, "backfill_done_at": now,
                      "backfill_max_id": info["max_id"],
                      "backfill_stats": json.dumps(stats, sort_keys=True, separators=(",", ":"))})
    out.update(stats)


def backfill(store: Any, chunk: Optional[int] = None, pause: Optional[float] = None,
             now: Optional[float] = None, cancel: Optional[threading.Event] = None) -> Optional[Dict[str, Any]]:
    """Rebuild the journal from printer_history once (spec §6). Synchronous; start() runs it in a thread.

    Reads on its own read-only connection, writes the prints and the meta in
    one transaction through store.db_write: a crash or a restart midway leaves
    nothing behind, and the next start runs it again. Returns the stats written
    to job_journal_meta (counts only), or None when nothing was written (the
    version is already there, or the write failed and was logged). Raises on a
    read error and BackfillCancelled on cancel; nothing is written then.
    """
    t0 = time.monotonic()
    path = _db_file(store)
    if not path:
        raise RuntimeError("printer_history.db is not a file")
    chunk = BACKFILL_CHUNK if chunk is None else chunk
    pause = BACKFILL_PAUSE_S if pause is None else pause
    conn = _connect_ro(path)
    try:
        jobs, info = _replay(conn, max(1, int(chunk)), pause, cancel)
    finally:
        conn.close()
    if cancel is not None and cancel.is_set():
        raise BackfillCancelled()
    out: Dict[str, Any] = {}
    at = time.time() if now is None else float(now)
    if not store.db_write(lambda c: _write_backfill(c, jobs, info, at, out)) or out.get("done_elsewhere"):
        return None
    out["sec"] = round(time.monotonic() - t0, 1)
    logger.info("[jobs] бэкфилл: строк истории %d, печатей %d (записано %d, открытых %d, пропущено %d) за %.1f с",
                out["rows"], out["jobs"], out["inserted"], out["results"].get("open", 0), out["skipped"], out["sec"])
    return out


def _backfill_state(store: Any) -> bool:
    """True when the history still has to be replayed. An empty history is marked done right away."""
    need: List[bool] = []

    def check(conn: sqlite3.Connection) -> None:
        if read_meta(conn, "backfill_version") == str(BACKFILL_VERSION):
            need.append(False)
        elif conn.execute("SELECT 1 FROM printer_history LIMIT 1").fetchone() is None:
            write_meta(conn, {"backfill_version": BACKFILL_VERSION, "backfill_done_at": time.time(),
                              "backfill_max_id": 0, "backfill_stats": json.dumps({"rows": 0, "jobs": 0})})
            need.append(False)   # a new install: nothing to rebuild, the tracker starts at once
        else:
            need.append(True)

    return store.db_write(check) and bool(need) and need[0]


# --- live tracker (spec §5) ---
# observe() runs on the poll loop's thread only (as_completed is drained one
# future at a time), so the machines need no lock of their own. _lock guards
# the module state against start() / stop() and the backfill's handover (§6.4).
_lock = threading.Lock()
_jdb: Optional[JournalDB] = None
_machines: Dict[str, JobMachine] = {}
_ready = False
_failures: set = set()   # (printer, error type, place) already logged at ERROR
_bf_thread: Optional[threading.Thread] = None
_bf_cancel = threading.Event()


def start(store: Any, now: Optional[float] = None) -> None:
    """Continue the prints the previous process left open (spec §5.4), then observe.

    Called once by main() before the poll loop. When the history has not been
    replayed yet (spec §6), a background thread does it first and observe()
    stays a no-op until it hands over (a few minutes, once). Never raises: the
    farm runs without the journal.
    """
    global _jdb, _machines, _ready, _bf_thread, _bf_cancel
    jdb = JournalDB(store)
    with _lock:
        _bf_cancel.set()   # a backfill of an earlier start() (tests) must not hand over
    try:
        need = _backfill_state(store)
    except Exception as e:
        _log_failure("start", e)
        need = False
    if need:
        cancel = threading.Event()
        with _lock:
            _jdb, _machines, _ready, _bf_cancel = jdb, {}, False, cancel
            _failures.clear()
        _bf_thread = threading.Thread(target=_backfill_main, args=(store, jdb, cancel, now),
                                      name="jobs-backfill", daemon=True)
        _bf_thread.start()
        logger.info("[jobs] журнал заданий: восстанавливаю печати из истории в фоне")
        return
    _bf_thread = None
    _handover(jdb, None, now)


def _backfill_main(store: Any, jdb: JournalDB, cancel: threading.Event, now: Optional[float]) -> None:
    """The jobs-backfill thread: replay the history, then let the live tracker in (spec §6.4)."""
    try:
        os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 10)   # Linux: nice for this thread only
    except Exception:
        pass
    try:
        backfill(store, cancel=cancel)
    except BackfillCancelled:
        logger.info("[jobs] бэкфилл остановлен, ничего не записано")
        return
    except Exception as e:   # nothing written, no version: the next start tries again
        _log_failure("backfill", e)
    _handover(jdb, cancel, now)


def _handover(jdb: JournalDB, cancel: Optional[threading.Event], now: Optional[float]) -> None:
    """Machines for the open prints in the DB (the previous process's, the backfill's), then observe."""
    global _jdb, _machines, _ready
    with _lock:
        if cancel is not None and (cancel.is_set() or _jdb is not jdb):
            return   # stop() / a newer start() came first
        try:
            machines = jdb.load_open(now)
        except Exception as e:
            _log_failure("start", e)
            machines = {}
        _jdb, _machines, _ready = jdb, machines, True
        if cancel is None:
            _failures.clear()
    logger.info("[jobs] журнал заданий: открытых печатей после старта — %d",
                sum(1 for m in machines.values() if m.job is not None))


def wait_backfill(timeout: Optional[float] = None) -> bool:
    """Wait for the backfill thread of the last start(); True when none is running (tests)."""
    t = _bf_thread
    if t is not None and t is not threading.current_thread():
        t.join(timeout)
    return t is None or not t.is_alive()


def stop() -> None:
    """Stop observing and drop the machines (tests; the next start() restores them from the DB).

    A running backfill is cancelled: it stops at its next chunk without writing.
    """
    global _jdb, _machines, _ready
    with _lock:
        _bf_cancel.set()
        _jdb, _machines, _ready = None, {}, False
    wait_backfill(10)


def observe(status: Any, now: float) -> None:
    """Feed one polled PrinterStatus to its printer's machine and write the ops.

    Every poll branch calls it: a live status, a grace clone (skipped by the
    machine) and an offline one (no data). A no-op before start(); never raises.
    """
    if not _ready:
        return
    pid = getattr(status, "id", None)
    try:
        with _lock:
            jdb = _jdb
            if not _ready or jdb is None or not pid:
                return
            m = _machines.get(pid)
            if m is None:
                m = _machines[pid] = JobMachine(pid, kind=_enum(status.kind),
                                                model=getattr(status, "device_type", None))
            ops = m.feed(sample_from_status(status, now, _in_maintenance(pid)))
            jdb.apply(m, ops, now)
        _log_ops(pid, ops)
    except Exception as e:
        _log_failure(pid, e)
        _reload(pid)


def _reload(pid: str) -> None:
    """After an unexpected error the machine's state is suspect: continue from the stored open print."""
    try:
        with _lock:
            jdb = _jdb
            if jdb is None:
                return
            _machines.pop(pid, None)
            rows: List[Optional[Dict[str, Any]]] = []
            if jdb._store.db_write(lambda conn: rows.append(_open_row(conn, pid))) and rows and rows[0]:
                _machines[pid] = JobMachine.restore(rows[0])
    except Exception as e:
        _log_failure(pid, e)


def _in_maintenance(pid: str) -> bool:
    """printer_registry.in_maintenance (spec_maintenance) once it exists; False until then."""
    try:
        from app.services import printer_registry
        fn = getattr(printer_registry, "in_maintenance", None)
        return bool(fn(pid)) if callable(fn) else False
    except Exception:
        return False


def _fmt_dur(sec: Any) -> str:
    if sec is None:
        return "—"
    minutes = max(0, int(sec)) // 60
    return "%d ч %02d мин" % divmod(minutes, 60) if minutes >= 60 else "%d мин" % minutes


def _log_ops(pid: str, ops: List[Op]) -> None:
    """One line per written print event: printer id, print number, result. Never a job name."""
    for op in ops:
        job = op.job
        if job.id is None:   # not written yet (retried with the next poll) or refused
            continue
        if op.op == "open":
            logger.info("[jobs] %s: печать #%d начата", pid, job.id)
        elif op.op == "close":
            logger.info("[jobs] %s: печать #%d — %s (%s)", pid, job.id, job.result, _fmt_dur(job.actual_sec))
        elif op.op == "reopen":
            logger.info("[jobs] %s: печать #%d продолжается (ложный конец)", pid, job.id)
        elif op.op == "amend":
            logger.info("[jobs] %s: печать #%d — уточнено: %s", pid, job.id, job.result)


def _log_failure(pid: Any, exc: BaseException) -> None:
    """An unexpected error: type and place only, never the message (it may quote
    a value, and the values include job names). ERROR once per place, then DEBUG."""
    tb = exc.__traceback__
    while tb is not None and tb.tb_next is not None:
        tb = tb.tb_next
    place = "%s:%d" % (os.path.basename(tb.tb_frame.f_code.co_filename), tb.tb_lineno) if tb else "?"
    key = (pid, type(exc).__name__, place)
    level = logging.DEBUG if key in _failures else logging.ERROR
    _failures.add(key)
    logger.log(level, "[jobs] %s: ошибка журнала %s в %s", pid, type(exc).__name__, place)
