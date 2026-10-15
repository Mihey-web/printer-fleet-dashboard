"""Fleet-wide (bulk) commands: one admin request, many printers, sent concurrently.

Eligibility is decided from the stored status (the same rules the UI uses);
printer_commands.send() then re-checks each printer on its live telemetry,
and its refusals come back as "skipped".
"""
import logging
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor, wait
from typing import Any, Dict, List, Optional

from app.services import printer_commands

logger = logging.getLogger(__name__)

BULK_ACTIONS = ("light_on", "light_off", "pause", "resume")
BULK_MAX_PRINTERS = 64
# One worker per printer (<= 64): every send starts at once, so a few silent
# printers holding a worker for REPLY_TIMEOUT never push the others past the
# deadline. Patched small only in tests.
BULK_MAX_WORKERS = BULK_MAX_PRINTERS
# A send already waits up to REPLY_TIMEOUT (8 s) for its reply.
BULK_DEADLINE = printer_commands.REPLY_TIMEOUT + 4.0

MSG_BAD_ACTION = "Групповое действие: light_on, light_off, pause или resume"
MSG_NO_PRINTERS = "Не выбран ни один принтер"
MSG_TOO_MANY = "За раз — не больше %d принтеров" % BULK_MAX_PRINTERS
MSG_BUSY = "Групповая команда уже выполняется — дождитесь окончания"
MSG_NOT_FOUND = "принтер не найден"
MSG_OFFLINE = "офлайн"
MSG_GRACE = "нет связи — данные устарели"
MSG_NO_LIGHT = "нет управляемой подсветки"
MSG_LATE = "ответа не дождались — итог покажет статус принтера"
MSG_NOT_STARTED = "не отправлено — истёк срок ожидания"
MSG_DRY = "будет отправлено"


class BulkError(ValueError):
    """A bad request (-> 400)."""


class BulkBusy(RuntimeError):
    """A bulk command of the same family is still running (-> 409)."""


# One lock per action family: an urgent «Пауза всех» is never blocked by a light-all.
_busy = {"light": threading.Lock(), "print": threading.Lock()}


def _family(action: str) -> str:
    return "light" if action in printer_commands.LIGHT_ACTIONS else "print"


def _lc_first(s: Optional[str]) -> Optional[str]:
    return s[:1].lower() + s[1:] if s else s


def skip_reason(action: str, d: Dict[str, Any], caps: Dict[str, Any], note: Optional[str]) -> Optional[str]:
    """Why this printer is left out of a bulk `action` (ru, lower case), or None.
    Mirrored by app.js bulkSkipReason."""
    cap = printer_commands.ACTION_CAP[action]
    if not d.get("online") or d.get("state") == "offline":
        return MSG_OFFLINE + (" · " + note if caps.get(cap) is False and note else "")
    if d.get("grace_period_active"):
        return MSG_GRACE
    if caps.get(cap) is not True:
        if cap == "light":
            # a known "no" with a reason (Klipper not ready, no control at all) says why, as
            # printer_commands._cap_refusal does for a single command
            return note if caps.get(cap) is False and note else MSG_NO_LIGHT
        return note or printer_commands.NOTE_NOT_WIRED
    return _lc_first(printer_commands.state_refusal(action, d.get("state")))


def _row(pid: str, label: str, status: str, detail: str) -> Dict[str, Any]:
    return {"id": pid, "label": label, "ok": status in ("confirmed", "sent", "dry_run"),
            "status": status, "detail": detail}


def run_bulk(action: str, printer_ids: Optional[List[str]], *, store, labels: Optional[Dict[str, str]] = None,
             dry_run: bool = False, send=None) -> Dict[str, Any]:
    """Send `action` to every eligible printer at once; never blocks past
    BULK_DEADLINE. Returns {"action", "batch", "dry_run", "results", "summary"}
    with one result row per requested id, in request order."""
    if action not in BULK_ACTIONS:
        raise BulkError(MSG_BAD_ACTION)
    ids = list(dict.fromkeys(printer_ids or []))        # dedupe, keep order
    if not ids:
        raise BulkError(MSG_NO_PRINTERS)
    if len(ids) > BULK_MAX_PRINTERS:
        raise BulkError(MSG_TOO_MANY)
    lock = _busy[_family(action)]
    if not lock.acquire(blocking=False):
        raise BulkBusy(MSG_BUSY)
    pending = None                                      # sends still running when we return
    try:
        send = send or printer_commands.send
        labels = labels or {}
        batch = secrets.token_hex(4)
        statuses = {s.id: s.to_dict() for s in store.get_all()} if store is not None else {}
        res: Dict[str, Dict[str, Any]] = {}
        todo = []
        for pid in ids:
            d = statuses.get(pid)
            label = labels.get(pid) or (d or {}).get("label") or pid
            if d is None:
                res[pid] = _row(pid, label, "skipped", MSG_NOT_FOUND)
                continue
            caps, note = printer_commands.caps_for(d)
            why = skip_reason(action, d, caps, note)
            if why:
                res[pid] = _row(pid, label, "skipped", why)
            elif dry_run:
                res[pid] = _row(pid, label, "dry_run", MSG_DRY)
            else:
                todo.append((pid, label))
        if todo:
            ex = ThreadPoolExecutor(max_workers=min(BULK_MAX_WORKERS, len(todo)), thread_name_prefix="bulk")
            futs = {ex.submit(send, pid, action, {}): (pid, label) for pid, label in todo}
            done, not_done = wait(futs, timeout=BULK_DEADLINE)
            # never block past the deadline; queued (never started) sends are cancelled, not sent later
            ex.shutdown(wait=False, cancel_futures=True)
            pending = [f for f in not_done if not f.cancelled()]
            for f, (pid, label) in futs.items():
                if f not in done:
                    if f.cancelled():
                        res[pid] = _row(pid, label, "skipped", MSG_NOT_STARTED)
                    else:
                        res[pid] = _row(pid, label, "sent", MSG_LATE)   # in flight: published or about to be
                    continue
                try:
                    r = f.result()
                except Exception as e:
                    logger.error("[bulk] %s %s failed", pid, action, exc_info=True)
                    r = {"success": False, "status": "rejected", "detail": str(e), "replied": False}
                st = r.get("status") or ("sent" if r.get("success") else "rejected")
                if st == "refused":
                    res[pid] = _row(pid, label, "skipped", _lc_first(r.get("detail")) or "")
                else:
                    res[pid] = _row(pid, label, st, r.get("detail") or "")
        rows = [res[pid] for pid in ids]
        summary = {k: sum(1 for r in rows if r["status"] == k)
                   for k in ("confirmed", "sent", "rejected", "skipped", "dry_run")}
        return {"action": action, "batch": batch, "dry_run": bool(dry_run), "results": rows, "summary": summary}
    finally:
        _release_when_done(lock, pending)


def _release_when_done(lock, pending) -> None:
    """Release the family lock now or, when sends are still in flight past the
    deadline, when the last of them finishes: a «Повторить» or a new bulk
    command never overlaps them."""
    if not pending:
        lock.release()
        return
    left = [len(pending)]
    guard = threading.Lock()

    def _one_done(_f):
        with guard:
            left[0] -= 1
            last = left[0] == 0
        if last:
            lock.release()

    for f in pending:
        f.add_done_callback(_one_done)        # runs at once if f finished meanwhile
