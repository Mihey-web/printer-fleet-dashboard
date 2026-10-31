"""Raw Bambu report fields that the vendored pybambu drops or keeps private.

The lib parses each `print` report into its Device model but does not keep:
`fun2`, the AMS units' `info` bit field and `dry_sf_reason`, `ams_status`,
`ams.tray_tar`, and the per-extruder `device.extruder` fields other than the
target slot and temperatures. `fun` is kept only as a private string.
RawReport keeps them as plain ints, merged like the lib merges P1 deltas (a
key missing from a report keeps its previous value).

For printer notifications it also keeps what the lib resets or drops: the raw
`print_error` (the lib zeroes it a few seconds after a cancel) with the time it
first appeared, `hms[]` with `ts_unix` / `ts_boot` and the time each code was
first seen, `subtask_id`, `job_attr`, a cancel latch over the FAILED episode
that follows a user cancel, and a few raw error fields for the admin debug view.

update() runs on the paho thread for every report and must never raise or
stop the lib from parsing: each field is read on its own, and a malformed one
keeps its previous value. snapshot() hands the poll thread a private copy.
"""
import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# job_seq (print-job journal): gcode_state leaving one of these for a working
# state is a new job. Values in neither set (INIT, UNKNOWN, ...) are not
# remembered, so they cannot hide a start or fake one.
_SEQ_IDLE = frozenset({"IDLE", "FINISH", "FAILED"})
_SEQ_WORK = frozenset({"PREPARE", "SLICING", "RUNNING", "PAUSE"})

# device.extruder.state bit 19: a filament change (load / unload) is running.
EXT_STATE_BUSY_BIT = 19
# ams_status (legacy protocol, mirrored on new firmware): main state in bits 8-15.
AMS_MAIN_FILAMENT_CHANGE = 1
_EXTRUDER_KEYS = ("stat", "snow", "star", "info")

# print_error echoed when the user cancels a job: 0300_400C, 0500_400E.
CANCEL_CODES = (0x0300400C, 0x0500400E)
# A code that leaves hms[] for less than this keeps its first-seen time (codes
# flap: in the P2S capture one disappears and comes back every 10-30 s).
FLAP_GRACE_S = 120
# gcode_state of a job in progress. Entering a start state from outside it is a
# new job; the cancel latch ends there, and on IDLE / FINISH.
_JOB_ACTIVE = frozenset({"RUNNING", "PREPARE", "SLICING", "PAUSE"})
_JOB_START = frozenset({"RUNNING", "PREPARE", "SLICING"})
_JOB_DONE = frozenset({"IDLE", "FINISH"})
# Raw error fields, only for the admin debug view.
_DEBUG_ERROR_KEYS = ("fail_reason", "mc_err", "mc_print_error_code", "err")


def _int(value: Any) -> Optional[int]:
    """An int, or a decimal string of one ("254"); anything else is None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip(), 10)
        except ValueError:
            return None
    return None


def _hex(value: Any) -> Optional[int]:
    """A hex bit-field string ("10942023"), or an int as is."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return int(value.strip(), 16)
        except ValueError:
            return None
    return None


def _scalar(value: Any) -> bool:
    return isinstance(value, (str, int, float)) and not isinstance(value, bool)


def change_busy(ext_state: Optional[int], extruders: Dict[int, Dict[str, int]],
                ams_status: Optional[int]) -> bool:
    """True while the printer runs a filament change (load or unload).

    New protocol (an extruder block is reported): device.extruder.state bit 19.
    Legacy protocol (P1): ams_status main state 1.
    """
    if ext_state is not None and extruders:
        return bool(ext_state >> EXT_STATE_BUSY_BIT & 1)
    if ams_status is not None:
        return (ams_status >> 8) & 0xFF == AMS_MAIN_FILAMENT_CHANGE
    return False


class RawReport:
    def __init__(self, clock: Optional[Callable[[], float]] = None):
        self._lock = threading.Lock()
        self._clock = clock          # None = time.time: first-seen times are shown as dates
        self._fun: Optional[int] = None
        self._fun_seen = False
        self._fun2: Optional[int] = None
        self._home_flag_seen = False
        self._job_id: Optional[str] = None
        self._seq_gstate: Optional[str] = None       # last idle / working gcode_state seen
        self._job_seq = 0                             # job starts seen by this process
        self._job_started_at: Optional[float] = None  # wall clock of the last one
        self._ams_status: Optional[int] = None
        self._tray_tar: Optional[int] = None
        self._ams: Dict[int, Dict[str, Any]] = {}
        self._ext_state: Optional[int] = None
        self._extruders: Dict[int, Dict[str, int]] = {}
        self._busy = False
        self._changes_done = 0   # busy -> not busy transitions seen
        self._now = 0.0          # clock reading for the report being merged
        self._gcode_state: Optional[str] = None
        self._print_error: Optional[int] = None
        self._print_error_since: Optional[float] = None
        self._cancel_latched = False
        self._subtask_id: Optional[str] = None
        self._job_attr: Optional[int] = None
        self._debug_errors: Dict[str, Any] = {}
        self._hms: List[Dict[str, Any]] = []
        self._hms_seen: Dict[Tuple[int, int], float] = {}   # (attr, code) -> first seen
        self._hms_gone: Dict[Tuple[int, int], float] = {}   # (attr, code) -> left hms[] at

    def update(self, p: Any) -> None:
        if not isinstance(p, dict):
            return
        with self._lock:
            try:
                self._now = float((self._clock or time.time)())
            except Exception:
                logger.debug("raw report clock failed", exc_info=True)
            for part in (self._update_flags, self._update_job, self._update_hms,
                         self._update_ams, self._update_extruder):
                try:
                    part(p)
                except Exception:
                    logger.debug("raw report field skipped", exc_info=True)
            busy = change_busy(self._ext_state, self._extruders, self._ams_status)
            if self._busy and not busy:
                self._changes_done += 1
            self._busy = busy

    def _update_flags(self, p: dict) -> None:
        if "fun" in p:
            fun = _hex(p["fun"])
            if fun is not None:
                self._fun, self._fun_seen = fun, True
        if "fun2" in p:
            fun2 = _hex(p["fun2"])
            if fun2 is not None:
                self._fun2 = fun2
        if "home_flag" in p and _int(p["home_flag"]) is not None:
            self._home_flag_seen = True
        # The print-error dialog answers (ignore / resume) carry the job id.
        if p.get("job_id") not in (None, ""):
            self._job_id = str(p["job_id"])
        try:
            self._note_job_start(p.get("gcode_state"))
        except Exception:
            logger.debug("raw report job_seq skipped", exc_info=True)
        if "ams_status" in p:
            status = _int(p["ams_status"])
            if status is not None:
                self._ams_status = status

    def _update_job(self, p: dict) -> None:
        if "subtask_id" in p and _scalar(p["subtask_id"]):
            self._subtask_id = str(p["subtask_id"])        # "" kept as sent
        if "job_attr" in p:
            attr = _int(p["job_attr"])
            if attr is not None:
                self._job_attr = attr
        for key in _DEBUG_ERROR_KEYS:
            if key in p and _scalar(p[key]):
                self._debug_errors[key] = p[key]
        # The cancel report may still say RUNNING (or carry no gcode_state at
        # all), and the code lasts only a few seconds: latch it when it arrives.
        # A new job's start clears the latch, in the same report too, so a stale
        # code at the start of the next job does not latch it. Before the first
        # gcode_state nothing is known to have started.
        if "print_error" in p:
            code = _int(p["print_error"])
            if code is not None:
                if code != self._print_error:
                    self._print_error_since = self._now if code else None
                    if code in CANCEL_CODES:
                        self._cancel_latched = True
                self._print_error = code
        gcode_state = p.get("gcode_state")
        if isinstance(gcode_state, str) and gcode_state.strip():
            new = gcode_state.strip().upper()
            prev = self._gcode_state
            if new in _JOB_DONE or (new in _JOB_START and prev is not None and prev not in _JOB_ACTIVE):
                self._cancel_latched = False
            self._gcode_state = new

    def _update_hms(self, p: dict) -> None:
        entries = p.get("hms")
        if not isinstance(entries, list):
            return
        hms = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            attr, code = _int(entry.get("attr")), _int(entry.get("code"))
            if attr is None or code is None:
                continue
            ts_unix = entry.get("ts_unix")
            hms.append({"attr": attr, "code": code,
                        "ts_unix": str(ts_unix) if _scalar(ts_unix) else None,
                        "ts_boot": _int(entry.get("ts_boot"))})
        if entries and not hms:
            return               # nothing readable: keep the last list
        now = self._now
        present = {(e["attr"], e["code"]) for e in hms}
        for key in present:
            gone = self._hms_gone.pop(key, None)
            if key not in self._hms_seen or (gone is not None and now - gone >= FLAP_GRACE_S):
                self._hms_seen[key] = now
        for key in [k for k in self._hms_seen if k not in present]:
            gone = self._hms_gone.setdefault(key, now)
            if now - gone >= FLAP_GRACE_S:
                del self._hms_seen[key]
                del self._hms_gone[key]
        self._hms = hms

    def _update_ams(self, p: dict) -> None:
        ams = p.get("ams")
        if not isinstance(ams, dict):
            return
        if "tray_tar" in ams:
            tar = _int(ams["tray_tar"])
            if tar is not None:
                self._tray_tar = tar
        units = ams.get("ams")
        if not isinstance(units, list):
            return
        for unit in units:
            if not isinstance(unit, dict):
                continue
            uid = _int(unit.get("id"))
            if uid is None:
                continue
            cur = self._ams.setdefault(uid, {"info": None, "sf": None})
            if "info" in unit:
                info = _hex(unit["info"])
                if info is not None:
                    cur["info"] = info
            sf = unit.get("dry_sf_reason")
            if isinstance(sf, list):
                codes = [_int(c) for c in sf]
                if all(c is not None for c in codes):
                    cur["sf"] = tuple(codes)

    def _update_extruder(self, p: dict) -> None:
        device = p.get("device")
        ext = device.get("extruder") if isinstance(device, dict) else None
        if not isinstance(ext, dict):
            return
        if "state" in ext:
            state = _int(ext["state"])
            if state is not None:
                self._ext_state = state
        info = ext.get("info")
        if not isinstance(info, list):
            return
        for entry in info:
            if not isinstance(entry, dict):
                continue
            eid = _int(entry.get("id"))
            if eid is None:
                continue
            cur = self._extruders.setdefault(eid, {})
            for key in _EXTRUDER_KEYS:
                if key in entry:
                    value = _int(entry[key])
                    if value is not None:
                        cur[key] = value

    def _note_job_start(self, gcode_state: Any) -> None:
        """job_seq: count the job starts in the reports (spec_job_journal §5.2).

        The poll loop samples every 30 s and misses a print that ended while
        the next one began in between; this thread sees every report. The
        first state after the process start is no start (the job may have run
        for hours). A P1 delta without gcode_state changes nothing.
        """
        if not isinstance(gcode_state, str):
            return
        state = gcode_state.strip().upper()
        if state not in _SEQ_IDLE and state not in _SEQ_WORK:
            return
        if self._seq_gstate in _SEQ_IDLE and state in _SEQ_WORK:
            from time import time   # wall clock: the journal stores the start as a date
            self._job_seq += 1
            self._job_started_at = time()
        self._seq_gstate = state

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "fun": self._fun,
                "fun_seen": self._fun_seen,
                "fun2": self._fun2,
                "home_flag_seen": self._home_flag_seen,
                "job_id": self._job_id,
                "job_seq": self._job_seq,
                "job_started_at": self._job_started_at,
                "ams_status": self._ams_status,
                "tray_tar": self._tray_tar,
                "ams": {uid: dict(unit) for uid, unit in self._ams.items()},
                "ext_state": self._ext_state,
                "extruders": {eid: dict(e) for eid, e in self._extruders.items()},
                "busy": self._busy,
                "changes_done": self._changes_done,
                "gcode_state": self._gcode_state,
                "print_error": self._print_error,
                "print_error_since": self._print_error_since,
                "cancel_latched": self._cancel_latched,
                "subtask_id": self._subtask_id,
                "job_attr": self._job_attr,
                "hms": [dict(e, first_seen=self._hms_seen.get((e["attr"], e["code"])))
                        for e in self._hms],
                **{key: self._debug_errors.get(key) for key in _DEBUG_ERROR_KEYS},
            }
