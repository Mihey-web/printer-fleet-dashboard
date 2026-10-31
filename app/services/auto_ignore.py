"""Auto «Ignore and resume» for print errors the owner always waves through.

P3 (P2S) pauses before every print at build-plate identification with
print_error 0500_8062 («strong light on the build plate»); the owner had to
press «Ignore, continue» on the screen each time (14.10.2026). For the codes in
the `auto_ignore_print_errors` setting this thread sends the same «ignore»
Bambu Studio sends for that dialog button: once per (printer, job, error),
again after RETRY_S if the pause is still there, at most MAX_TRIES times.
"""
import logging
import math
import threading
import time
from typing import Any, Callable, Dict, Iterable, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_CODES = ["0500_8062", "0C00_8009"]   # build-plate marker not detected (worn marker / light)
CHECK_S = 15
RETRY_S = 120
MAX_TRIES = 3
FORGET_S = 600          # out of the pause this long since the last try: the next pause is a new one

_tries: Dict[Tuple[str, str, int], Tuple[int, float]] = {}   # (pid, job, err) -> (count, last at)
_skips: Dict[Tuple[str, str, int, str], float] = {}           # one audit row per skipped pause and reason
_lock = threading.Lock()


def code_of(err: int) -> str:
    """0x05008062 -> "0500_8062" (Bambu's HMS-wiki spelling)."""
    h = "%08X" % (int(err) & 0xFFFFFFFF)
    return h[:4] + "_" + h[4:]


def should_ignore(ctx: Optional[Dict[str, Any]], codes: Iterable[str], key_pid: str, now: float) -> bool:
    """Pure decision: paused on a listed code, and this job's tries allow one more."""
    if not ctx or ctx.get("gcode_state") != "PAUSE" or not ctx.get("err") or not ctx.get("job_id"):
        return False
    if code_of(ctx["err"]) not in set(codes):
        return False
    key = (key_pid, str(ctx["job_id"]), int(ctx["err"]))
    with _lock:
        count, last = _tries.get(key, (0, 0.0))
    return count < MAX_TRIES and (count == 0 or now - last >= RETRY_S)


def skip_reason(ctx: Optional[Dict[str, Any]], codes: Iterable[str], key_pid: str) -> Optional[str]:
    """Why a pause on a listed code is not waved through (None: not such a pause,
    or it will be). P3 sat paused on 0500_8062 on 17.10 18:29 with no try
    journaled — the reason is recorded from now on."""
    if not ctx or not ctx.get("err") or code_of(ctx["err"]) not in set(codes):
        return None
    if ctx.get("gcode_state") != "PAUSE":
        return "state_%s" % (ctx.get("gcode_state") or "none")
    if not ctx.get("job_id"):
        return "no_job_id"
    with _lock:
        count, _ = _tries.get((key_pid, str(ctx["job_id"]), int(ctx["err"])), (0, 0.0))
    return "tries_used" if count >= MAX_TRIES else None


def forget(key_pid: str, now: float) -> None:
    """Drop a printer's tries and skip notes once it has been out of the pause for
    FORGET_S. Network prints report job_id "0" (17.10, all 10 online Bambu), so
    (printer, job, err) alone pools every pause of a printer: after 3 tries ever,
    never again. Not at once: a plate that is still unreadable re-pauses within
    seconds of an ignore, and that must keep counting toward MAX_TRIES."""
    with _lock:
        for k in [k for k, (_, last) in _tries.items() if k[0] == key_pid and now - last >= FORGET_S]:
            del _tries[k]
        if any(k[0] == key_pid for k in _tries):
            return
        for k in [k for k in _skips if k[0] == key_pid]:
            del _skips[k]


def view(key_pid: str, err: int, now: Optional[float] = None) -> Dict[str, Any]:
    """The thread's state for a printer paused on listed code `err`, for the
    dashboard (/api/printers "auto"): {"enabled", "tries" (of the current
    pause: forget() drops them after FORGET_S out of it), "max", "next_in_s"
    (None unless another try is due), "skip" (the newest reason it did not
    press, or None)}. Reads only; copies under _lock, computes outside."""
    now = time.time() if now is None else now
    err = int(err)
    with _lock:
        tries = [v for k, v in _tries.items() if k[0] == key_pid and k[2] == err]
        skips = [(t, k[3]) for k, t in _skips.items() if k[0] == key_pid and k[2] == err]
    count, last = max(tries, key=lambda v: v[1]) if tries else (0, 0.0)
    next_in = max(0, int(math.ceil(RETRY_S - (now - last)))) if 0 < count < MAX_TRIES else None
    return {"enabled": True, "tries": count, "max": MAX_TRIES, "next_in_s": next_in,
            "skip": max(skips)[1] if skips else None}


def note_try(key_pid: str, ctx: Dict[str, Any], now: float) -> int:
    key = (key_pid, str(ctx["job_id"]), int(ctx["err"]))
    with _lock:
        count, _ = _tries.get(key, (0, 0.0))
        _tries[key] = (count + 1, now)
        return count + 1


def _note_skip(d: Dict[str, Any], ctx: Dict[str, Any], why: str, now: float,
               audit: Callable[[str], None]) -> None:
    """A listed pause left alone: one log line and audit row per pause and reason."""
    key = (d["id"], str(ctx.get("job_id")), int(ctx["err"]), why)
    with _lock:                 # view() reads _skips from the API threads
        new = key not in _skips
        if new:
            _skips[key] = now
    if new:
        logger.info("[auto-ignore] %s: %s paused, not ignored: %s", d.get("label") or d["id"],
                    code_of(ctx["err"]), why)
        try:
            audit("auto_ignore:%s:%s:skip:%s" % (d["id"], code_of(ctx["err"]), why))
        except Exception:
            logger.debug("auto-ignore audit failed", exc_info=True)


def _in_maintenance(printer_id: str) -> bool:
    from app.services import printer_registry
    return printer_registry.in_maintenance(printer_id)


def check_once(store, codes: Iterable[str], live: Callable[[str], Optional[Dict[str, Any]]],
               ignore: Callable[[str, int], Dict[str, Any]], audit: Callable[[str], None],
               now: Optional[float] = None, maint: Optional[Callable[[str], bool]] = None) -> None:
    """One pass over the online Bambu printers. maint(pid): the printer is in
    maintenance mode (None: printer_registry.in_maintenance; an error counts
    as no) — its listed pause is never pressed and no try is spent; that is
    said once per pause, like any other skip."""
    now = time.time() if now is None else now
    codes = list(codes or [])
    if not codes:
        return
    maint = maint or _in_maintenance
    for st in store.get_all():
        d = st.to_dict()
        if d.get("kind") != "bambu" or not d.get("online") or d.get("state") != "paused":
            if d.get("online") and d.get("state") not in (None, "paused"):
                forget(d["id"], now)        # the pause is over: the next one gets fresh tries
            continue
        ctx = live(d["id"])
        try:
            in_maint = bool(maint(d["id"]))
        except Exception:
            logger.debug("auto-ignore maintenance lookup failed", exc_info=True)
            in_maint = False
        if in_maint:
            if ctx and ctx.get("err") and code_of(ctx["err"]) in set(codes):
                _note_skip(d, ctx, "maintenance", now, audit)
            continue
        with _lock:                         # out of the mode: its note no longer explains this pause (view)
            for k in [k for k in _skips if k[0] == d["id"] and k[3] == "maintenance"]:
                del _skips[k]
        if not should_ignore(ctx, codes, d["id"], now):
            why = skip_reason(ctx, codes, d["id"])
            if why:
                _note_skip(d, ctx, why, now, audit)
            continue
        n = note_try(d["id"], ctx, now)
        r = ignore(d["id"], ctx["err"])
        logger.info("[auto-ignore] %s: %s, try %d -> %s %s", d.get("label") or d["id"], code_of(ctx["err"]),
                    n, r.get("status"), r.get("detail") or "")
        try:
            audit("auto_ignore:%s:%s:%s" % (d["id"], code_of(ctx["err"]), r.get("status")))
        except Exception:
            logger.debug("auto-ignore audit failed", exc_info=True)


def _loop(store) -> None:
    from app.services import printer_commands, printer_registry, settings_service
    from app.services.audit_service import get_audit_service

    time.sleep(60)   # let the collectors connect and send a full report
    logger.info("[auto-ignore] watching paused Bambu printers for %s",
                ", ".join(settings_service.get("auto_ignore_print_errors") or []) or "nothing")
    while True:
        try:
            codes = settings_service.get("auto_ignore_print_errors")
            check_once(store, codes, printer_commands.live_print_error, printer_commands.ignore_print_error,
                       lambda action: get_audit_service().log("auto-ignore", action),
                       maint=printer_registry.in_maintenance)
        except Exception:
            logger.error("auto-ignore check failed", exc_info=True)
        time.sleep(CHECK_S)


def start(store) -> None:
    threading.Thread(target=_loop, args=(store,), daemon=True, name="auto-ignore").start()
