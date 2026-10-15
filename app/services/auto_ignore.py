"""Auto «Ignore and resume» for print errors the owner always waves through.

P3 (P2S) pauses before every print at build-plate identification with
print_error 0500_8062 («strong light on the build plate»); the owner had to
press «Ignore, continue» on the screen each time (14.10.2026). For the codes in
the `auto_ignore_print_errors` setting this thread sends the same «ignore»
Bambu Studio sends for that dialog button: once per (printer, job, error),
again after RETRY_S if the pause is still there, at most MAX_TRIES times.
"""
import logging
import threading
import time
from typing import Any, Callable, Dict, Iterable, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_CODES = ["0500_8062", "0C00_8009"]   # build-plate marker not detected (worn marker / light)
CHECK_S = 15
RETRY_S = 120
MAX_TRIES = 3

_tries: Dict[Tuple[str, str, int], Tuple[int, float]] = {}   # (pid, job, err) -> (count, last at)
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


def note_try(key_pid: str, ctx: Dict[str, Any], now: float) -> int:
    key = (key_pid, str(ctx["job_id"]), int(ctx["err"]))
    with _lock:
        count, _ = _tries.get(key, (0, 0.0))
        _tries[key] = (count + 1, now)
        return count + 1


def check_once(store, codes: Iterable[str], live: Callable[[str], Optional[Dict[str, Any]]],
               ignore: Callable[[str, int], Dict[str, Any]], audit: Callable[[str], None],
               now: Optional[float] = None) -> None:
    """One pass over the online Bambu printers."""
    now = time.time() if now is None else now
    codes = list(codes or [])
    if not codes:
        return
    for st in store.get_all():
        d = st.to_dict()
        if d.get("kind") != "bambu" or not d.get("online") or d.get("state") != "paused":
            continue
        ctx = live(d["id"])
        if not should_ignore(ctx, codes, d["id"], now):
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
    from app.services import printer_commands, settings_service
    from app.services.audit_service import get_audit_service

    time.sleep(60)   # let the collectors connect and send a full report
    while True:
        try:
            codes = settings_service.get("auto_ignore_print_errors")
            check_once(store, codes, printer_commands.live_print_error, printer_commands.ignore_print_error,
                       lambda action: get_audit_service().log("auto-ignore", action))
        except Exception:
            logger.error("auto-ignore check failed", exc_info=True)
        time.sleep(CHECK_S)


def start(store) -> None:
    threading.Thread(target=_loop, args=(store,), daemon=True, name="auto-ignore").start()
