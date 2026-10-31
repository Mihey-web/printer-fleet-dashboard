"""Since when a notification stands, kept across restarts (notifications spec §2.5).

A status' notifications carry `seen_at`: when RawReport first saw the code on
the MQTT thread. That time dies with the process, and the Pi restarts often.
The tracker keeps one row per (printer, key) while the key is active, in
printer_history.db, and sets every record's `since` to the row's first_seen:
a code that stood before a restart keeps its «с DD.MM HH:MM».

- A key missing from a status counts as cleared only after FLAP_GRACE_S: the
  P2S fixture's code blinks every 10-30 s and must not start over each time.
- Offline and grace-period statuses tell nothing about the notifications (a
  grace status is the last good one cloned): they are skipped, so nothing is
  cleared while a printer is away.
- A row is written only when a key appears or clears, never per poll: the Pi
  runs from an SD card.
- State flags ("state:cancelled") keep a printer's state across a restart
  (§3.2: the cancel code is gone from the reports seconds after a cancel).
  They are rows of the same table with src "flag", and they leave after
  FLAP_GRACE_S too (at once when a job runs again).

Called from StateStore.upsert, the one door of the poll loop and of the fast
refresh after commands. A storage failure is logged and costs only the row:
`since` still works from memory until the next restart.
"""
import logging
import os
import sqlite3
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from app.collectors.bambu_raw import FLAP_GRACE_S

logger = logging.getLogger(__name__)

# Cleared rows older than this are deleted (the history's own retention).
RETENTION_SECONDS = int(os.environ.get("HISTORY_RETENTION_DAYS", "90")) * 86400
_PRUNE_INTERVAL = 3600  # prune at most hourly

FLAG_SRC = "flag"
CANCELLED_FLAG = "state:cancelled"


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value > 0 else None


def _state_value(state: Any) -> Any:
    return getattr(state, "value", state)


class NotificationTracker:
    def __init__(self, db_path: str, clock: Optional[Callable[[], float]] = None):
        self._clock = clock or time.time
        self._lock = threading.Lock()
        self._last_prune = 0.0
        # pid -> key -> {"id": row id | None, "first_seen", "gone_at": first poll it was missing}
        self._active: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self._flags: Dict[str, Dict[str, Optional[int]]] = {}   # pid -> flag -> row id | None
        self._flag_off: Dict[str, Dict[str, float]] = {}        # pid -> flag -> first status without it
        # Its own connection to the history DB (StateStore keeps another): writes
        # are rare, and WAL lets both work side by side.
        self._db = sqlite3.connect(db_path, timeout=10, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._init_db()
        self._load()

    def _init_db(self) -> None:
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS printer_notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                printer_id TEXT NOT NULL, nkey TEXT NOT NULL, src TEXT NOT NULL, code TEXT NOT NULL,
                severity TEXT NOT NULL, first_seen REAL NOT NULL, cleared_at REAL)
        """)
        self._db.execute("""
            CREATE INDEX IF NOT EXISTS idx_pn_active
            ON printer_notifications(printer_id, nkey, cleared_at)
        """)
        self._db.commit()

    def _load(self) -> None:
        """The active rows into memory. A key active twice (two processes over
        one DB) keeps its earliest row; the others are closed."""
        now = self._clock()
        rows = self._db.execute(
            "SELECT id, printer_id, nkey, src, first_seen FROM printer_notifications "
            "WHERE cleared_at IS NULL ORDER BY first_seen, id").fetchall()
        extra: List[int] = []
        for row_id, pid, key, src, first_seen in rows:
            if src == FLAG_SRC:
                flags = self._flags.setdefault(pid, {})
                if key in flags:
                    extra.append(row_id)
                else:
                    flags[key] = row_id
                continue
            active = self._active.setdefault(pid, {})
            if key in active:
                extra.append(row_id)
            else:
                active[key] = {"id": row_id, "first_seen": float(first_seen), "gone_at": None}
        if extra:
            self._db.executemany("UPDATE printer_notifications SET cleared_at = ? WHERE id = ?",
                                 [(now, i) for i in extra])
            self._db.commit()

    # ---- storage (caller holds _lock) ------------------------------------------

    def _insert(self, pid: str, key: str, src: str, code: str, severity: str, first_seen: float) -> Optional[int]:
        try:
            cur = self._db.execute(
                "INSERT INTO printer_notifications (printer_id, nkey, src, code, severity, first_seen) "
                "VALUES (?, ?, ?, ?, ?, ?)", (pid, key, src, code, severity, first_seen))
            self._db.commit()
            return cur.lastrowid
        except Exception:
            logger.error("printer_notifications insert failed for %s", pid, exc_info=True)
            return None

    def _close(self, row_id: Optional[int], cleared_at: float) -> None:
        if row_id is None:
            return
        try:
            self._db.execute("UPDATE printer_notifications SET cleared_at = ? WHERE id = ?", (cleared_at, row_id))
            self._db.commit()
        except Exception:
            logger.error("printer_notifications update failed", exc_info=True)

    def _prune_locked(self, now: float) -> None:
        if now - self._last_prune < _PRUNE_INTERVAL:
            return
        self._last_prune = now
        try:
            self._db.execute("DELETE FROM printer_notifications WHERE cleared_at IS NOT NULL AND cleared_at < ?",
                             (now - RETENTION_SECONDS,))
            self._db.commit()
        except Exception:
            logger.error("printer_notifications retention prune failed", exc_info=True)

    def _set_flag_locked(self, pid: str, name: str, on: bool, now: float) -> None:
        flags = self._flags.setdefault(pid, {})
        self._flag_off.get(pid, {}).pop(name, None)
        if on and name not in flags:
            flags[name] = self._insert(pid, name, FLAG_SRC, "", "", now)
        elif not on and name in flags:
            self._close(flags.pop(name), now)

    def _keep_flag_locked(self, pid: str, name: str, on: bool, at_once: bool, now: float) -> None:
        """A state flag follows the statuses, but leaves only after FLAP_GRACE_S
        without it (closed at the first such status), or at once (at_once): a
        reconnect's first report can be gcode_state "unknown", seen as IDLE,
        right before the FAILED one the flag is kept for."""
        if on or name not in self._flags.get(pid, {}):
            self._set_flag_locked(pid, name, on, now)
            return
        since = self._flag_off.setdefault(pid, {}).setdefault(name, now)
        if at_once or now - since >= FLAP_GRACE_S:
            self._set_flag_locked(pid, name, False, since)

    # ---- API -----------------------------------------------------------------

    def annotate(self, status: Any, now: Optional[float] = None) -> None:
        """Set `since` of every notification of an online status to the time
        its key was first seen (kept across restarts); keep the cancel flag."""
        if not getattr(status, "online", False) or getattr(status, "grace_period_active", False):
            return
        pid = status.id
        now = self._clock() if now is None else float(now)
        items = [n for n in (getattr(status, "notifications", None) or []) if isinstance(n, dict) and n.get("key")]
        with self._lock:
            active = self._active.setdefault(pid, {})
            present = set()
            for n in items:
                key = str(n["key"])
                present.add(key)
                entry = active.get(key)
                if entry is None:
                    seen_at = _number(n.get("seen_at"))
                    first = min(seen_at, now) if seen_at is not None else now
                    entry = {"id": self._insert(pid, key, str(n.get("src") or ""), str(n.get("code") or ""),
                                                str(n.get("severity") or ""), first),
                             "first_seen": first, "gone_at": None}
                    active[key] = entry
                entry["gone_at"] = None
                n["since"] = entry["first_seen"]
            for key, entry in list(active.items()):
                if key in present:
                    continue
                if entry["gone_at"] is None:
                    entry["gone_at"] = now
                elif now - entry["gone_at"] >= FLAP_GRACE_S:
                    self._close(active.pop(key)["id"], entry["gone_at"])
            state = _state_value(getattr(status, "state", None))
            # a job running again (a new one) ends the cancel at once
            self._keep_flag_locked(pid, CANCELLED_FLAG, state == "cancelled", state in ("printing", "paused"), now)
            self._prune_locked(now)

    def flag(self, pid: str, name: str) -> bool:
        # No lock: a dict lookup, and a normalizer must never wait behind a write.
        return name in self._flags.get(pid, {})

    def set_flag(self, pid: str, name: str, on: bool = True, now: Optional[float] = None) -> None:
        with self._lock:
            self._set_flag_locked(pid, name, on, self._clock() if now is None else float(now))

    def view(self, pid: str) -> Dict[str, Any]:
        """What the tracker keeps for one printer (admin debug)."""
        with self._lock:
            active = {k: dict(e) for k, e in self._active.get(pid, {}).items()}
            flags = sorted(self._flags.get(pid, {}))
        return {"active": [{"key": k, "first_seen": e["first_seen"], "gone_at": e["gone_at"]}
                           for k, e in sorted(active.items(), key=lambda kv: kv[1]["first_seen"])],
                "flags": flags}

    def close(self) -> None:
        with self._lock:
            self._db.close()


_tracker: Optional[NotificationTracker] = None


def init(db_path: str) -> Optional[NotificationTracker]:
    """Open the tracker on the history DB and load its active rows (main(),
    before the poll loop). Without it annotate does nothing and no flag is set."""
    global _tracker
    try:
        _tracker = NotificationTracker(db_path)
    except Exception:
        logger.error("notification tracker not started", exc_info=True)
        _tracker = None
    return _tracker


def annotate(status: Any, now: Optional[float] = None) -> None:
    tracker = _tracker
    if tracker is None:
        return
    try:
        tracker.annotate(status, now)
    except Exception:
        # Never in the way of the store: the status goes in without `since`.
        logger.error("notification tracker failed for %s", getattr(status, "id", "?"), exc_info=True)


def flag(pid: str, name: str) -> bool:
    tracker = _tracker
    return tracker.flag(pid, name) if tracker is not None else False


def set_flag(pid: str, name: str, on: bool = True) -> None:
    tracker = _tracker
    if tracker is not None:
        tracker.set_flag(pid, name, on)


def view(pid: str) -> Optional[Dict[str, Any]]:
    tracker = _tracker
    return tracker.view(pid) if tracker is not None else None
