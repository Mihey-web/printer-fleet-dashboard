"""Read side of the print-job journal (spec_job_journal §7): list, stats, CSV.

Pure functions over the printer_history.db path, so the API routes stay thin
(like _query_events / _query_timeline in app/api/main.py). Every call opens its
own short read-only connection; a missing file or a database without
print_jobs (older code wrote it) reads as an empty journal.

Job names are sensitive (spec §10): list_jobs(), get_job() and jobs_csv()
return them (the API serves the list to logged-in users only, the CSV to
admins only), job_stats() never reads the column, and nothing here logs.

A print belongs to the window [fr, to] when it overlaps it: it started by `to`
and is still open or ended at `fr` or later. idx_jobs_started bounds the window
from above only, so the stats and a list filtered by result read it through
idx_jobs_ended: their cost follows the window, not the retention.
"""
import csv
import io
import json
import math
import os
import pathlib
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

try:
    from zoneinfo import ZoneInfo
except ImportError:   # Python < 3.9: the server's zone only
    ZoneInfo = None

from app.services import creality_models, sqlite_db
from app.services.job_journal import RESULTS

RESULT_FILTERS = RESULTS + ("open",)          # `result` query values; open = result IS NULL
CSV_LIMIT = 20000
PAUSE_CODES_MAX = 3                           # pause codes per list row
TOP_N = 10                                    # failure codes / pause reasons in the stats
# success_pct = finished / these: lost (outcome unknown) and open prints are left out.
_SUCCESS_BASE = ("finished", "cancelled", "failed", "power_loss", "stopped")
# Error codes that explain a bad print; cancels are the owner's choice (cancel_n).
_FAILURE_RESULTS = ("failed", "power_loss", "stopped")
# Only a printer's own forecast with an exactly known start and end measures the forecast.
_ACC_SOURCES = ("slicer", "printer")
_DAY_FILL_MAX = 366                           # per_day lists every day of a window up to this long

# Local time for days and CSV times; None = the server's zone (Europe/Moscow on
# the Pi). Tests set a fixed offset: time.tzset() does not exist on Windows.
_TZ: Optional[tzinfo] = None
# An IANA zone name from the browser (Intl ... timeZone): "Asia/Omsk", "America/Argentina/Buenos_Aires".
_TZ_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_+\-]*(/[A-Za-z0-9_+\-]+){0,2}")
_TZ_NAME_MAX = 64
_UTC_NAMES = ("UTC", "Etc/UTC", "GMT", "Etc/GMT")

# List row fields, in the order the API returns them (pause_codes and pause_list
# replace pauses_json; ctx is never returned).
_ROW_FIELDS = (
    "id", "printer_id", "label", "kind", "model", "model_name", "job_name", "started_at", "ended_at", "start_exact",
    "end_exact", "last_seen", "result", "error_code", "error_text", "planned_sec", "planned_src", "actual_sec",
    "paused_sec", "pauses", "pause_codes", "pause_list", "offline_sec", "offline_n", "power_losses", "end_progress",
    "layer", "total_layers", "maint", "verdict", "planned_filament_g", "preview_key", "project_key",
)
_LIST_COLS = tuple(f for f in _ROW_FIELDS if f not in ("label", "model_name", "pause_codes", "pause_list")) + ("pauses_json",)
# The stats read no job_name.
_STATS_COLS = ("id", "printer_id", "model", "started_at", "ended_at", "start_exact", "end_exact", "result",
               "error_code", "error_text", "planned_sec", "actual_sec", "planned_src", "paused_sec",
               "pauses_json", "verdict", "maint", "planned_filament_g", "end_progress", "last_seen")
_BY_END = "idx_jobs_ended"                    # init_db creates it; a journal of older code may lack it

# IA §10's dictionary «Результаты печати», the same words as the web page's list (JOB_RESULTS)
RESULT_LABELS = {
    "finished": "Успешно", "cancelled": "Отменено", "failed": "Ошибка", "power_loss": "Питание",
    "stopped": "Остановлено", "lost": "Нет связи", "open": "Идёт",
}
# No «Годно / Брак» column (the owner, 29.10): the verdict stays in the journal, the export leaves it out.
CSV_HEADER = ("Принтер", "Модель", "Задание", "Начало", "Конец", "План, мин", "Факт, мин", "Пауз",
              "Пауза, мин", "Без связи, мин", "Результат", "Код", "Описание", "Слоёв", "Расчётный расход, г")
# A spreadsheet runs a cell that starts like this as a formula (CSV injection).
_CSV_FORMULA = ("=", "+", "-", "@", "\t", "\r")


def parse_results(value: Optional[str]) -> Optional[List[str]]:
    """The `result` filter: comma-separated RESULT_FILTERS values, None = all.

    Raises ValueError on an unknown value (the API answers 400).
    """
    if value is None:
        return None
    items = [v.strip() for v in value.split(",") if v.strip()]
    if any(v not in RESULT_FILTERS for v in items):
        raise ValueError("unknown result")
    return list(dict.fromkeys(items)) or None


# --- reading ---
def _connect(db: str) -> Optional[sqlite3.Connection]:
    """A read-only connection, or None when there is no journal to read."""
    if not os.path.exists(db):
        return None
    conn = sqlite_db.connect(pathlib.Path(db).absolute().as_uri() + "?mode=ro", uri=True)
    try:
        found = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'print_jobs'").fetchone()
    except Exception:
        conn.close()
        raise
    if found is None:
        conn.close()
        return None
    return conn


def _where(fr: float, to: float, printer: Optional[str], results: Optional[Sequence[str]],
           include_maint: bool) -> Tuple[str, List[Any]]:
    # An open print (ended_at NULL; a closed one always has it) overlaps every
    # window it started in, whatever its lagging last_seen says.
    where = ["started_at <= ?", "(ended_at >= ? OR ended_at IS NULL)"]
    params: List[Any] = [to, fr]
    if printer:
        where.append("printer_id = ?")
        params.append(printer)
    if results:
        closed = [r for r in results if r != "open"]
        either = []
        if closed:
            either.append("result IN (%s)" % ", ".join("?" * len(closed)))
            params.extend(closed)
        if "open" in results:
            either.append("result IS NULL")
        where.append("(%s)" % " OR ".join(either))
    if not include_maint:
        where.append("maint = 0")
    return " AND ".join(where), params


def _select(db: str, cols: Iterable[str], where: str, params: List[Any], tail: str = "",
            index: Optional[str] = None) -> List[Dict[str, Any]]:
    """Rows of print_jobs; `index`: read through it (INDEXED BY) when the database has it."""
    conn = _connect(db)
    if conn is None:
        return []
    try:
        table = "print_jobs"
        if index and conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = ?",
                                  (index,)).fetchone():
            table += " INDEXED BY " + index   # the planner keeps idx_jobs_started even after ANALYZE
        cur = conn.execute("SELECT %s FROM %s WHERE %s %s" % (", ".join(cols), table, where, tail), params)
        names = [d[0] for d in cur.description]
        return [dict(zip(names, row)) for row in cur.fetchall()]
    finally:
        conn.close()


def _pauses(pauses_json: Any) -> List[Dict[str, Any]]:
    try:
        items = json.loads(pauses_json) if pauses_json else []
    except (TypeError, ValueError):
        return []
    return [p for p in items if isinstance(p, dict)] if isinstance(items, list) else []


def _pause_list(pauses_json: Any) -> List[Dict[str, Any]]:
    """Every pause the journal kept (up to job_journal.PAUSES_MAX): {at, sec, code, text}, in order.

    For the row's details on the page («История → Печати»): when each pause began,
    how long it held and why. Odd values come out as None.
    """
    def num(v: Any) -> Optional[int]:
        return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None

    def word(v: Any) -> Optional[str]:
        return v if isinstance(v, str) and v else None

    return [{"at": num(p.get("at")), "sec": num(p.get("sec")), "code": word(p.get("code")), "text": word(p.get("text"))}
            for p in _pauses(pauses_json)]


def _pause_codes(pauses_json: Any) -> List[str]:
    codes: List[str] = []
    for p in _pauses(pauses_json):
        code = p.get("code")
        if isinstance(code, str) and code and code not in codes:
            codes.append(code)
            if len(codes) == PAUSE_CODES_MAX:
                break
    return codes


def list_jobs(db: str, fr: float, to: float, printer: Optional[str] = None,
              results: Optional[Sequence[str]] = None, include_maint: bool = True,
              limit: int = 200, offset: int = 0, labels: Optional[Mapping[str, str]] = None
              ) -> Dict[str, Any]:
    """One page of prints overlapping [fr, to], newest first: {"rows", "has_more"}.

    LIMIT+1 gives has_more without a COUNT(*). `labels`: printer_id -> the
    registry's current name (the printer id when missing).
    """
    labels = labels or {}
    where, params = _where(fr, to, printer, results, include_maint)
    # Without a result filter the newest-first walk of idx_jobs_started stops at
    # the LIMIT; a filter that matches few rows would walk it to the beginning.
    rows = _select(db, _LIST_COLS, where, params + [int(limit) + 1, int(offset)],
                   "ORDER BY started_at DESC, id DESC LIMIT ? OFFSET ?",
                   index=_BY_END if results and not printer else None)
    return {"rows": [_list_row(r, labels) for r in rows[:limit]], "has_more": len(rows) > limit}


def get_job(db: str, job_id: int, labels: Optional[Mapping[str, str]] = None) -> Optional[Dict[str, Any]]:
    """One print as a list row (the verdict route answers with it), None when there is none."""
    rows = _select(db, _LIST_COLS, "id = ?", [int(job_id)])
    return _list_row(rows[0], labels or {}) if rows else None


def _list_row(r: Dict[str, Any], labels: Mapping[str, str]) -> Dict[str, Any]:
    r["label"] = labels.get(r["printer_id"]) or r["printer_id"]
    r["model_name"] = creality_models.display_name(r["model"])     # «K1C», not k1c
    pauses = r.pop("pauses_json")
    r["pause_codes"] = _pause_codes(pauses)
    r["pause_list"] = _pause_list(pauses)
    return {f: r[f] for f in _ROW_FIELDS}


# --- stats ---
def _quantile(sorted_vals: Sequence[float], q: float) -> Optional[float]:
    """Linear-interpolated quantile of an ascending list (numpy's default), None when empty."""
    if not sorted_vals:
        return None
    pos = (len(sorted_vals) - 1) * q
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def _round(v: Optional[float], nd: int) -> Optional[float]:
    return None if v is None else round(v, nd)


def _ratio(r: Mapping[str, Any]) -> Optional[float]:
    """Actual print time / plan (spec §4.6) of a print that measures the forecast, else None."""
    if (r["result"] != "finished" or r["planned_src"] not in _ACC_SOURCES or not r["start_exact"]
            or not r["end_exact"] or not r["planned_sec"] or r["planned_sec"] <= 0 or r["actual_sec"] is None):
        return None
    return (r["actual_sec"] - (r["paused_sec"] or 0)) / float(r["planned_sec"])


def _summary(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    counts = Counter(r["result"] or "open" for r in rows)
    out: Dict[str, Any] = {"jobs": len(rows)}
    for k in RESULT_FILTERS:
        out[k] = counts.get(k, 0)
    base = sum(out[k] for k in _SUCCESS_BASE)
    out["success_pct"] = round(100.0 * out["finished"] / base, 1) if base else None
    busy = sum(max(0, r["actual_sec"] - (r["paused_sec"] or 0))
               for r in rows if r["result"] is not None and r["actual_sec"] is not None)
    out["print_hours"] = round(busy / 3600.0, 1)
    ratios = sorted(x for x in map(_ratio, rows) if x is not None)
    out["acc_median"] = _round(_quantile(ratios, 0.5), 3)
    out["acc_p90"] = _round(_quantile(ratios, 0.9), 3)
    out["acc_n"] = len(ratios)
    return out


def _top_text(texts: Iterable[Any]) -> Optional[str]:
    c = Counter(t for t in texts if isinstance(t, str) and t)
    return c.most_common(1)[0][0] if c else None


def _failure_codes(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_code: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in rows:
        if r["error_code"] and r["result"] in _FAILURE_RESULTS:
            by_code[r["error_code"]].append(r)
    items = [{"code": code, "text": _top_text(r["error_text"] for r in rs), "n": len(rs),
              "result": Counter(r["result"] for r in rs).most_common(1)[0][0]}
             for code, rs in by_code.items()]
    items.sort(key=lambda d: (-d["n"], d["code"]))
    return items[:TOP_N]


def _pause_reasons(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_code: Dict[str, List[Any]] = defaultdict(list)
    for r in rows:
        for p in _pauses(r["pauses_json"]):
            code = p.get("code")
            if isinstance(code, str) and code:
                by_code[code].append(p.get("text"))
    items = [{"code": code, "text": _top_text(texts), "n": len(texts)} for code, texts in by_code.items()]
    items.sort(key=lambda d: (-d["n"], d["code"]))
    return items[:TOP_N]


def zone(name: Any) -> Optional[tzinfo]:
    """The IANA zone `name` (the browser's, e.g. "Asia/Omsk") for the days of the stats.

    An empty, too long, malformed or unknown name is the server's zone (_TZ), as
    before. So is every name but UTC on a Windows without the tzdata package
    (zoneinfo has no zone database there): never an error.
    """
    if not isinstance(name, str) or not name or len(name) > _TZ_NAME_MAX or not _TZ_NAME.fullmatch(name):
        return _TZ
    if name in _UTC_NAMES:
        return timezone.utc
    if ZoneInfo is None:
        return _TZ
    try:
        return ZoneInfo(name)
    except Exception:   # ZoneInfoNotFoundError (no such zone, no tz database), ValueError, OSError
        return _TZ


def _local(ts: Any, tz: Optional[tzinfo] = None) -> Optional[datetime]:
    """`ts` as a local time: in `tz`, else in _TZ (None: the server's zone)."""
    try:
        return datetime.fromtimestamp(float(ts), tz or _TZ)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _per_day(rows: List[Dict[str, Any]], fr: float, to: float, tz: Optional[tzinfo] = None
             ) -> List[Dict[str, Any]]:
    """Prints per local day (in `tz`, else the server's zone) of the window [fr, to].

    A closed print counts on the day it ended, an open one on its start day; a
    print reaching out of the window counts on its first or last day. A window
    ending at a midnight (the page's «Свой…» days) does not reach the day that
    begins there.
    """
    days: Dict[str, Dict[str, Any]] = {}

    def day(key: str) -> Dict[str, Any]:
        return days.setdefault(key, {"day": key, "jobs": 0, "finished": 0, "bad": 0})

    try:
        lo, hi = float(fr), float(to)
        hi = max(lo, hi - 1)
    except (TypeError, ValueError):
        lo = hi = None
    first, last = _local(lo, tz), _local(hi, tz)
    if first is not None and last is not None and 0 <= (last.date() - first.date()).days <= _DAY_FILL_MAX:
        d = first.date()
        while d <= last.date():
            day(d.isoformat())
            d += timedelta(days=1)
    for r in rows:
        closed = r["result"] is not None
        ts = r["ended_at"] if closed and r["ended_at"] is not None else r["started_at"]
        if lo is not None and isinstance(ts, (int, float)) and not isinstance(ts, bool):
            ts = min(max(ts, lo), hi)
        when = _local(ts, tz)
        if when is None:
            continue
        entry = day(when.date().isoformat())
        entry["jobs"] += 1
        if r["result"] == "finished":
            entry["finished"] += 1
        elif closed:
            entry["bad"] += 1
    return [days[k] for k in sorted(days)]


def _num(v: Any) -> Optional[float]:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    return float(v)


def _used_grams(r: Mapping[str, Any]) -> Optional[float]:
    """The plastic a print used: the slicer's planned grams (planned_filament_g, recorded
    since 14.11.2026) times the share printed — all of a finished print, the progress
    reached of a cancelled / failed / still open one. None: the weight is unknown — except
    for a print that ended at 0 %: nothing used, whatever the file weighs."""
    g = _num(r.get("planned_filament_g"))
    if g is None or g <= 0:
        p = _num(r.get("end_progress"))
        return 0.0 if r.get("result") not in (None, "finished") and p is not None and p <= 0 else None
    if r.get("result") == "finished":
        return g
    p = _num(r.get("end_progress"))
    return g * min(max(p, 0.0), 100.0) / 100.0 if p is not None else 0.0


def _midnight(d, tz: Optional[tzinfo]) -> float:
    """The epoch of local midnight starting day `d` (in `tz`, else the server's zone)."""
    if tz is None:
        return datetime(d.year, d.month, d.day).timestamp()
    return datetime(d.year, d.month, d.day, tzinfo=tz).timestamp()


def _grams_per_day(rows: List[Dict[str, Any]], fr: float, to: float, tz: Optional[tzinfo] = None
                   ) -> Dict[str, Dict[str, float]]:
    """Plastic used per local day of the window [fr, to]: {day: {"grams": g, "no_weight": n, "weighed": n}}.

    A print's grams (_used_grams) are spread evenly over the time it ran — start to its
    end, an open one to when it was last seen — and only the part inside the window
    counts, so a print over midnight splits between the two days. no_weight: the prints
    that ran that day without a known weight (the panel had no slicer data for them),
    weighed: those with one — the page trusts a day whose prints nearly all have a weight.
    """
    out: Dict[str, Dict[str, float]] = {}
    try:
        lo, hi = float(fr), float(to)
    except (TypeError, ValueError):
        return out
    for r in rows:
        start = _num(r.get("started_at"))
        if start is None:
            continue
        end = _num(r.get("ended_at")) if r.get("result") is not None else _num(r.get("last_seen"))
        end = max(start, end if end is not None else start)
        a, b = max(start, lo), min(end, hi)
        if b < a:
            continue
        grams = _used_grams(r)
        first, last = _local(a, tz), _local(b if b == a else b - 1e-6, tz)
        if first is None or last is None:
            continue
        d = first.date()
        while d <= last.date():
            day = out.setdefault(d.isoformat(), {"grams": 0.0, "no_weight": 0, "weighed": 0})
            if grams is None:
                day["no_weight"] += 1
            elif end > start:
                day["weighed"] += 1
                d0 = max(a, _midnight(d, tz))
                d1 = min(b, _midnight(d + timedelta(days=1), tz))
                day["grams"] += grams * max(0.0, d1 - d0) / (end - start)
            else:
                day["weighed"] += 1
                day["grams"] += grams                  # an instant print: all on its day
            d += timedelta(days=1)
    return out


def job_stats(db: str, fr: float, to: float, printer: Optional[str] = None, include_maint: bool = False,
              labels: Optional[Mapping[str, str]] = None, tz: Optional[str] = None) -> Dict[str, Any]:
    """Aggregates over the prints overlapping [fr, to] (spec §7.2). No job names.

    total.maint: the prints of the window in maintenance mode these stats leave
    out (include_maint=False; the list shows them). tz: the IANA zone of the
    page (the owner's PC is UTC+7, the Pi Moscow) its per_day days are cut in;
    None or an unknown name: the server's zone (zone()).
    """
    labels = labels or {}
    where, params = _where(fr, to, printer, None, True)
    rows = _select(db, _STATS_COLS, where, params, index=None if printer else _BY_END)
    rows.sort(key=lambda r: (r["started_at"], r["id"]))
    maint = sum(1 for r in rows if r["maint"])
    if not include_maint:
        rows = [r for r in rows if not r["maint"]]
    total = _summary(rows)
    total["maint"] = 0 if include_maint else maint
    verdicts = Counter(r["verdict"] for r in rows)
    total["good"], total["defect"] = verdicts.get("good", 0), verdicts.get("defect", 0)

    tzi = zone(tz)
    by_printer: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_printer[r["printer_id"]].append(r)
    printers = []
    for pid, rs in by_printer.items():
        # «Пластик» by printer: the same window-clipped grams as per_day, and its prints without a weight
        used = _grams_per_day(rs, fr, to, tzi)
        item: Dict[str, Any] = {"printer_id": pid, "label": labels.get(pid) or pid,
                                "model": next((r["model"] for r in reversed(rs) if r["model"]), None)}
        item["model_name"] = creality_models.display_name(item["model"])
        item.update(_summary(rs))
        done = [r["actual_sec"] for r in rs if r["result"] == "finished" and r["actual_sec"] is not None]
        item["avg_sec"] = int(round(sum(done) / len(done))) if done else None
        top = _failure_codes(rs)
        item["top_code"] = top[0]["code"] if top else None
        item["top_code_text"] = top[0]["text"] if top else None
        item["grams"] = round(sum(v["grams"] for v in used.values()), 1)
        item["no_weight"] = sum(1 for r in rs if _used_grams(r) is None)
        printers.append(item)
    printers.sort(key=lambda d: (-d["jobs"], str(d["label"]).casefold(), d["printer_id"]))

    per_day = _per_day(rows, fr, to, tzi)
    # «Пластик в день»: the grams each day used (spread over the prints' run time) and the
    # prints that ran without a known weight; total.grams is the window's sum
    grams = _grams_per_day(rows, fr, to, tzi)
    known = {d["day"] for d in per_day}
    per_day += [{"day": k, "jobs": 0, "finished": 0, "bad": 0} for k in grams if k not in known]
    per_day.sort(key=lambda d: d["day"])
    for d in per_day:
        g = grams.get(d["day"], {})
        d["grams"] = round(g.get("grams", 0.0), 1)
        d["no_weight"] = int(g.get("no_weight", 0))
        d["weighed"] = int(g.get("weighed", 0))
    total["grams"] = round(sum(v["grams"] for v in grams.values()), 1)
    total["no_weight"] = sum(1 for r in rows if _used_grams(r) is None)

    return {"fr": fr, "to": to, "total": total, "printers": printers,
            "failure_codes": _failure_codes(rows), "cancel_n": total["cancelled"],
            "pause_reasons": _pause_reasons(rows), "per_day": per_day}


# --- CSV ---
def csv_cell(value: Any) -> str:
    """A CSV cell a spreadsheet shows as text: a leading = + - @ tab or CR gets a quote mark."""
    if value is None:
        return ""
    text = str(value)
    return "'" + text if text.startswith(_CSV_FORMULA) else text


def _when(ts: Any, exact: Any) -> str:
    when = _local(ts) if ts is not None else None
    if when is None:
        return ""
    return ("" if exact else "~") + when.strftime("%d.%m.%Y %H:%M")


def _minutes(sec: Any) -> Optional[int]:
    return None if sec is None else int(round(sec / 60.0))


def _decimal(value: Any) -> Any:
    """A fractional number with a decimal comma: Russian Excel reads «23.4» as the date 23 April."""
    if isinstance(value, float) and math.isfinite(value):
        return str(value).replace(".", ",")
    return value


def _csv_row(r: Mapping[str, Any], lang: Optional[str] = None) -> List[str]:
    from app.services import i18n
    return [csv_cell(v) for v in (
        r["label"], r.get("model_name") or r["model"], r["job_name"],     # «Модель» is for people: «K2 Plus»
        _when(r["started_at"], r["start_exact"]), _when(r["ended_at"], r["end_exact"]),
        _minutes(r["planned_sec"]), _minutes(r["actual_sec"]), r["pauses"], _minutes(r["paused_sec"]),
        _minutes(r["offline_sec"]), i18n.t(RESULT_LABELS.get(r["result"] or "open", r["result"]), lang or i18n.SOURCE),
        r["error_code"], r["error_text"], r["total_layers"], _decimal(r.get("planned_filament_g")),
    )]


def jobs_csv(db: str, fr: float, to: float, printer: Optional[str] = None,
             results: Optional[Sequence[str]] = None, include_maint: bool = True, limit: int = CSV_LIMIT,
             labels: Optional[Mapping[str, str]] = None, lang: Optional[str] = None) -> str:
    """The list (same filters, newest first) as CSV for Russian Excel: BOM, `;`, local times.
    lang: the panel's language for the column names and the result words (the data as it is)."""
    from app.services import i18n
    lang = lang if lang in i18n.languages() else i18n.SOURCE
    page = list_jobs(db, fr, to, printer=printer, results=results, include_maint=include_maint,
                     limit=limit, offset=0, labels=labels)
    buf = io.StringIO()
    buf.write("﻿")
    w = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    w.writerow([i18n.t(h, lang) for h in CSV_HEADER])
    w.writerows(_csv_row(r, lang) for r in page["rows"])
    return buf.getvalue()


def csv_filename(fr: float, to: float) -> str:
    """forge-jobs-YYYYMMDD-YYYYMMDD.csv by the local dates of the window."""
    def ymd(ts: float) -> str:
        when = _local(ts)
        return when.strftime("%Y%m%d") if when is not None else "x"
    return "forge-jobs-%s-%s.csv" % (ymd(fr), ymd(to))
