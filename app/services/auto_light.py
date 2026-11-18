"""Chamber light on when a printer stops on its own: an error, a pause, a cancel.

The owner looks at the printers through the cameras; a printer that paused or
failed in the dark shows nothing (03.11.2026). On the step into one of the
`auto_light_states` (paused, error, cancelled by default) this thread turns the
lamp on, only when the printer reports it off — once per step, so a lamp the
owner switches off again stays off until the next one. Not on a pause the
auto-ignore thread presses through (its codes): that one is gone in seconds —
unless the thread gives up on it (its tries used, maintenance mode): the lamp
goes on then, while the printer still stands in that pause.
"""
import logging
import threading
import time
from typing import Any, Callable, Dict, Iterable, Optional, Set

logger = logging.getLogger(__name__)

DEFAULT_STATES = ["paused", "error", "cancelled"]
CHECK_S = 5

_last: Dict[str, str] = {}      # printer_id -> the last state seen online
_held: Set[str] = set()         # printers paused on an auto-ignore code: the lamp waits for that thread
_lock = threading.Lock()


def step_in(pid: str, state: Optional[str], online: bool, states: Iterable[str]) -> bool:
    """Pure-ish decision: the printer has just stepped into one of `states`
    from a state outside them. The first state seen (service start) and an
    offline spell set the baseline only: a printer back online still paused
    did not just stop."""
    if not online or not state or state == "offline":
        return False
    states = set(states)
    with _lock:
        prev = _last.get(pid)
        _last[pid] = state
    return prev is not None and prev not in states and state in states


def check_once(store, states: Iterable[str], ignore_codes: Iterable[str],
               send: Callable[[str, str], Dict[str, Any]], audit: Callable[[str], None],
               caps: Callable[[Dict[str, Any]], Dict[str, Any]],
               live: Callable[[str], Optional[Dict[str, Any]]]) -> None:
    """One pass over the printers. caps(d): the card's capabilities (only
    caps["light"] is True gets a command); live(pid): the Bambu print-error
    context (auto_ignore's), to leave its pauses alone while it presses them."""
    from app.services.auto_ignore import code_of, gave_up

    states = list(states or [])
    codes = set(ignore_codes or [])
    for st in store.get_all():
        d = st.to_dict()
        pid = d["id"]
        stepped = step_in(pid, d.get("state"), bool(d.get("online")), states)
        if not stepped:
            # a pause left to auto-ignore: looked at again until it is over or the thread gives up
            with _lock:
                if pid in _held and d.get("online") and d.get("state") != "paused":
                    _held.discard(pid)
                waiting = pid in _held
            if not waiting or not d.get("online"):
                continue
        name = d.get("label") or pid
        if d.get("light_on") is not False:
            if stepped:
                logger.info("[auto-light] %s: %s, light already %s", name, d.get("state"),
                            "on" if d.get("light_on") else "unknown")
            _release(pid)
            continue
        try:
            if caps(d).get("light") is not True:
                _release(pid)
                continue
        except Exception:
            logger.debug("auto-light caps failed", exc_info=True)
            _release(pid)
            continue
        if d.get("kind") == "bambu" and d.get("state") == "paused" and codes:
            ctx = live(pid)
            if ctx and ctx.get("err") and code_of(ctx["err"]) in codes and not gave_up(pid, ctx["err"]):
                if stepped:
                    logger.info("[auto-light] %s: paused on %s, left to auto-ignore", name, code_of(ctx["err"]))
                with _lock:
                    _held.add(pid)
                continue
        _release(pid)
        r = send(pid, "light_on")
        logger.info("[auto-light] %s: %s -> light_on %s %s", name, d.get("state"), r.get("status"),
                    r.get("detail") or "")
        try:
            audit("auto_light:%s:%s:%s" % (pid, d.get("state"), r.get("status")))
        except Exception:
            logger.debug("auto-light audit failed", exc_info=True)


def _release(pid: str) -> None:
    with _lock:
        _held.discard(pid)


def _loop(store) -> None:
    from app.services import printer_commands, settings_service
    from app.services.audit_service import get_audit_service

    time.sleep(60)   # let the collectors connect and report the lamps
    logger.info("[auto-light] watching for %s", ", ".join(settings_service.get("auto_light_states") or []) or "nothing")
    while True:
        try:
            check_once(store, settings_service.get("auto_light_states"),
                       settings_service.get("auto_ignore_print_errors"),
                       printer_commands.send, lambda action: get_audit_service().log("auto-light", action),
                       lambda d: printer_commands.caps_for(d)[0], printer_commands.live_print_error)
        except Exception:
            logger.error("auto-light check failed", exc_info=True)
        time.sleep(CHECK_S)


def start(store) -> None:
    threading.Thread(target=_loop, args=(store,), daemon=True, name="auto-light").start()
