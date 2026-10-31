"""Hidden printer notifications («Скрыть», owner 27.10: C1's «fatal» lidar-lens
HMS hung for weeks and kept the card on top).

An admin mutes one record of one printer by its key (notifications._record's
"key": hms:0C00_0100_0001_0004, pe:0500_8062, klipper:shutdown, …). Nothing is
sent to the printer: the dashboard only stops drawing attention to the record
(nf-ui). The setting notification_mutes = {printer_id: [key, …]}; a mute is
kept by key, so a code that goes away and comes back is still muted, and
nothing is pruned. /api/printers marks every record with "muted" (mark).
"""
import copy
from threading import Lock
from typing import Any, Dict, Iterable, List, Tuple

from app.services import settings_service

SETTING = "notification_mutes"
MAX_PER_PRINTER = 64
KEY_MAX = 80

MSG_BAD_KEY = "Неверный ключ уведомления"
MSG_TOO_MANY = "Скрыть можно не больше %d уведомлений одного принтера" % MAX_PER_PRINTER
MSG_SETTINGS_PUT = "Скрытые уведомления меняются кнопками «Скрыть» / «Вернуть»: notification_mutes"

# One read-modify-write of the setting at a time (two admins clicking at once).
_lock = Lock()


def valid_key(key: Any) -> bool:
    return isinstance(key, str) and 0 < len(key) <= KEY_MAX and key.isprintable()


def valid(value: Any) -> bool:
    """The setting's shape: a dict of printer ids to at most MAX_PER_PRINTER
    printable keys of at most KEY_MAX characters."""
    if not isinstance(value, dict):
        return False
    for pid, keys in value.items():
        if not isinstance(pid, str) or not pid:
            return False
        if not isinstance(keys, list) or len(keys) > MAX_PER_PRINTER or not all(valid_key(k) for k in keys):
            return False
    return True


def current() -> Dict[str, List[str]]:
    """The setting (settings_service's cache), {} when unreadable or malformed."""
    try:
        value = settings_service.get(SETTING)
    except Exception:
        return {}
    return value if valid(value) else {}


def mark(printer_id: Any, records: Iterable[Dict[str, Any]], mutes: Any) -> None:
    """Sets "muted" (True / False) on each of the printer's records, in place.
    mutes: the setting as read once per request (anything else = none muted)."""
    keys = mutes.get(printer_id) if isinstance(mutes, dict) else None
    muted = {k for k in keys if isinstance(k, str)} if isinstance(keys, list) else set()
    for n in records or []:
        if isinstance(n, dict):
            n["muted"] = n.get("key") in muted


def set_muted(printer_id: str, key: str, muted: bool) -> Tuple[bool, List[str]]:
    """Mutes / unmutes one record key of a printer. -> (changed, the printer's
    muted keys now); a repeat changes nothing. Raises ValueError (its Russian
    text) on a bad key or when the printer's list is full."""
    if not valid_key(key):
        raise ValueError(MSG_BAD_KEY)
    with _lock:
        mutes = copy.deepcopy(current())      # get_all() copies only the top level
        keys = list(mutes.get(printer_id) or [])
        if bool(muted) == (key in keys):
            return False, keys
        if muted:
            if len(keys) >= MAX_PER_PRINTER:
                raise ValueError(MSG_TOO_MANY)
            keys.append(key)
        else:
            keys = [k for k in keys if k != key]
        if keys:
            mutes[printer_id] = keys
        else:
            mutes.pop(printer_id, None)
        settings_service.set_many({SETTING: mutes})
        return True, keys
