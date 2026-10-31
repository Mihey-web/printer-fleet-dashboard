"""Temperature history of one printer for the chart in the chip popover.

GET /api/printers/{id}/temps reads printer_history (current and target
temperatures of the nozzle, bed and chamber; the targets and the chamber are
recorded since state_store.HISTORY_TEMP_COLUMNS) and answers a small
columnar JSON: bucket times `t` and per series `v` (bucket mean), `target`
(the highest target > 0 in the bucket, null when the heater is off), `lo`/`hi`
(min/max, only for windows coarser than the raw step, so averaging does not
hide heat-ups and spikes) and `stats` over the raw samples of the window.

Offline rows carry no temperatures and grace rows a stale copy of the last
good status (a false flat shelf on the chart): both are left out. A printer
with two nozzles (H2D, none in the fleet) records the active one, so its
nozzle series jumps on an extruder switch.

Pure functions of the DB, no FastAPI: the endpoint calls cached_temps_json
from its threadpool thread and returns the string as it is.
"""
import json
import math
import os
import pathlib
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

# (series name, current column, target column)
SERIES = (("nozzle", "nozzle_temp", "target_nozzle_temp"),
          ("bed", "bed_temp", "target_bed_temp"),
          ("chamber", "chamber_temp", "target_chamber_temp"))
RAW_STEP_S = 90        # real step of the history rows (60 s throttle, 30 s poll)
BUCKETS = 240          # at most this many points per series
MAX_HOURS = 168        # 7 days: a week window is ~6.7k rows, ~0.3 s cold on the Pi
CACHE_TTL_S = 60       # a new history row lands every ~90 s
CACHE_MAX = 64

_cache: Dict[Tuple[str, str, int], Tuple[float, str]] = {}   # key -> (monotonic expiry, JSON)
_cache_lock = threading.Lock()


def _num(v: float) -> float:
    return round(v, 1)


def _rows(db_path: str, printer_id: str, fr: float, to: float) -> List[tuple]:
    """(recorded_at, cur, target, cur, target, cur, target) rows of the window, in
    SERIES order, oldest first; [] when there is no history to read."""
    if not os.path.exists(db_path):
        return []
    conn = sqlite3.connect(pathlib.Path(db_path).absolute().as_uri() + "?mode=ro", uri=True, timeout=10)
    try:
        have = {r[1] for r in conn.execute("PRAGMA table_info(printer_history)").fetchall()}
        if not have:
            return []
        # A column the table does not have yet (an old DB) reads as NULL. The
        # names come from SERIES, never from the request.
        cols = ", ".join(c if c in have else "NULL" for _, cur, tgt in SERIES for c in (cur, tgt))
        return conn.execute(
            "SELECT recorded_at, %s FROM printer_history "
            "WHERE printer_id = ? AND recorded_at >= ? AND recorded_at <= ? "
            "AND online = 1 AND IFNULL(grace_period_active, 0) = 0 "
            "ORDER BY recorded_at" % cols,
            (printer_id, fr, to)).fetchall()
    finally:
        conn.close()


def query_temps(db_path: str, printer_id: str, fr: float, to: float) -> Dict[str, Any]:
    """The chart payload of [fr, to] (at most MAX_HOURS back from `to`).

    {printer_id, fr, to, step, t: [int], series: {name: {v, target, stats[, lo, hi]}}};
    a series without a single value in the window is left out.
    """
    fr = max(float(fr), float(to) - MAX_HOURS * 3600)
    to = float(to)
    span = max(0.0, to - fr)
    step = max(RAW_STEP_S, span / BUCKETS)
    last = max(0, math.ceil(span / step) - 1)     # a row at exactly `to` joins the last bucket
    envelope = step > RAW_STEP_S

    # Group the rows by bucket; they come sorted, so the bucket index never decreases.
    buckets: List[List[tuple]] = []
    k_prev = None
    for row in _rows(db_path, printer_id, fr, to):
        k = min(int((row[0] - fr) // step), last)
        if k != k_prev:
            buckets.append([])
            k_prev = k
        buckets[-1].append(row)

    t = [int(round(sum(r[0] for r in b) / len(b))) for b in buckets]
    series: Dict[str, Dict[str, Any]] = {}
    for i, (name, _, _) in enumerate(SERIES):
        ci, ti = 1 + 2 * i, 2 + 2 * i
        v: List[Optional[float]] = []
        lo: List[Optional[float]] = []
        hi: List[Optional[float]] = []
        target: List[Optional[int]] = []
        raw: List[float] = []
        for b in buckets:
            vals = [r[ci] for r in b if r[ci] is not None]
            raw.extend(vals)
            v.append(_num(sum(vals) / len(vals)) if vals else None)
            if envelope:
                lo.append(_num(min(vals)) if vals else None)
                hi.append(_num(max(vals)) if vals else None)
            # 0 / NULL target = heater off: no target line for that bucket.
            tgts = [r[ti] for r in b if r[ti] is not None and r[ti] > 0]
            target.append(int(round(max(tgts))) if tgts else None)
        if not raw:
            continue
        s: Dict[str, Any] = {"v": v, "target": target,
                             "stats": {"min": _num(min(raw)), "avg": _num(sum(raw) / len(raw)),
                                       "max": _num(max(raw)), "n": len(raw)}}
        if envelope:
            s["lo"], s["hi"] = lo, hi
        series[name] = s
    return {"printer_id": printer_id, "fr": int(round(fr)), "to": int(round(to)),
            "step": int(step) if step == int(step) else round(step, 3), "t": t, "series": series}


def cached_temps_json(db_path: str, printer_id: str, hours: int, now: Optional[float] = None) -> str:
    """query_temps for the last `hours` (1..MAX_HOURS) up to `now`, as a ready JSON
    string cached for CACHE_TTL_S per (printer, hours)."""
    hours = max(1, min(int(hours), MAX_HOURS))
    key = (db_path, printer_id, hours)
    mono = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None and hit[0] > mono:
            return hit[1]

    to = time.time() if now is None else float(now)
    payload = query_temps(db_path, printer_id, to - hours * 3600, to)
    payload["hours"] = hours
    text = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)

    with _cache_lock:
        for k in [k for k, e in _cache.items() if e[0] <= mono]:
            del _cache[k]
        while len(_cache) >= CACHE_MAX:              # hard cap: drop the soonest to expire
            del _cache[min(_cache, key=lambda k: _cache[k][0])]
        _cache[key] = (mono + CACHE_TTL_S, text)
    return text
