"""Printer registry: fleet configuration in SQLite instead of config.py lists.

Printers get a stable id that never changes and is never reused, so history
(keyed by printer_id in printer_history) survives renames, IP changes and
insertions. The config.py lists remain only as a one-time migration seed with
their legacy positional ids (bambu-1, creality-2, ...).

Edits accumulate in the table while the poll loop keeps running on the
configuration it was started with (the "running snapshot", captured by
load_for_startup). The admin panel diffs table vs snapshot to show pending
changes; applying them is a service restart.
"""
import logging
import os
import sqlite3
import time
import uuid
from threading import Lock
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "printer_history.db")

KINDS = ("bambu", "creality", "klipper", "mks")

# Fields compared for the pending-"modified" diff (everything editable).
_CFG_FIELDS = ("kind", "label", "host", "port", "model", "access_code", "serial")

_lock = Lock()
# Rows the collectors were built from at startup — baseline for pending diffs.
_running: Optional[List[Dict[str, Any]]] = None

# Short-TTL cache of pid -> current label. _display_label is called on the poll
# hot path (per printer, per event) and by the Telegram status render; without
# this each call hit SQLite. Renames go through update_printer/discard_changes,
# which invalidate the cache, so a rename shows up immediately; the TTL is just a
# backstop so any missed write path self-heals within a few seconds.
_label_cache: Dict[str, Any] = {}   # pid -> (label, ts)
_label_cache_lock = Lock()
_LABEL_TTL = 15.0


def get_label(printer_id: str, db_path: Optional[str] = None) -> Optional[str]:
    """Current label for a printer id, cached with a short TTL + write invalidation."""
    now = time.time()
    with _label_cache_lock:
        hit = _label_cache.get(printer_id)
        if hit is not None and now - hit[1] < _LABEL_TTL:
            return hit[0]
    row = get_printer(printer_id, db_path)
    label = row.get("label") if row else None
    with _label_cache_lock:
        _label_cache[printer_id] = (label, now)
    return label


def _invalidate_label(printer_id: Optional[str] = None) -> None:
    with _label_cache_lock:
        if printer_id is None:
            _label_cache.clear()
        else:
            _label_cache.pop(printer_id, None)


def _connect(db_path: Optional[str] = None) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path or DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Optional[str] = None) -> None:
    conn = _connect(db_path)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS printers (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                label TEXT NOT NULL,
                host TEXT NOT NULL,
                port INTEGER,
                model TEXT,
                access_code TEXT,
                serial TEXT,
                deleted INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
        """)
        # discover_hostname was a Creality-only auto-relocate field for printers
        # behind a MikroTik pseudobridge (9 & 10). Those printers are gone and the
        # discovery feature was removed — drop the orphan column from older DBs.
        # SQLite >= 3.35 has DROP COLUMN; on anything older it stays as an inert
        # NULL column (never read/written), so the guard just no-ops.
        cols = [r[1] for r in conn.execute("PRAGMA table_info(printers)")]
        if "discover_hostname" in cols:
            try:
                conn.execute("ALTER TABLE printers DROP COLUMN discover_hostname")
            except sqlite3.OperationalError:
                pass
        conn.commit()
    finally:
        conn.close()


def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    d = dict(row)
    d["deleted"] = bool(d.get("deleted"))
    return d


def migrate_from_config(cfg: Any, db_path: Optional[str] = None) -> int:
    """One-time import of the config.py lists with their legacy positional ids.

    Runs only while the table is empty, so existing history rows keep pointing
    at the same printer ids. Returns the number of imported printers.
    """
    conn = _connect(db_path)
    try:
        if conn.execute("SELECT COUNT(*) FROM printers").fetchone()[0] > 0:
            return 0
        now = time.time()
        rows = []
        for i, p in enumerate(getattr(cfg, "PRINTERS", []), start=1):
            rows.append((f"bambu-{i}", "bambu", p.get("label", f"Bambu {i}"), p["host"],
                         None, p.get("device_type", "X1C"), p.get("access_code"), p.get("serial")))
        for i, p in enumerate(getattr(cfg, "CREALITY_PRINTERS", []), start=1):
            rows.append((f"creality-{i}", "creality", p.get("label", f"Creality {i}"), p["host"],
                         None, p.get("model", "k1max"), None, None))
        for i, p in enumerate(getattr(cfg, "KLIPPER_PRINTERS", []), start=1):
            rows.append((f"klipper-{i}", "klipper", p.get("label", f"Klipper {i}"), p["host"],
                         int(p.get("port", 7125)), p.get("model"), None, None))
        for i, p in enumerate(getattr(cfg, "MKS_PRINTERS", []), start=1):
            rows.append((f"mks-{i}", "mks", p.get("label", f"MKS {i}"), p["host"],
                         int(p.get("port", 8080)), p.get("model"), None, None))
        conn.executemany("""
            INSERT INTO printers (id, kind, label, host, port, model, access_code, serial,
                                  deleted, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, %f, %f)
        """ % (now, now), rows)
        conn.commit()
        if rows:
            logger.info("printer registry: migrated %d printers from config.py", len(rows))
        return len(rows)
    finally:
        conn.close()


def list_printers(db_path: Optional[str] = None, include_deleted: bool = True) -> List[Dict[str, Any]]:
    conn = _connect(db_path)
    try:
        sql = "SELECT * FROM printers"
        if not include_deleted:
            sql += " WHERE deleted = 0"
        sql += " ORDER BY label"
        return [_row_to_dict(r) for r in conn.execute(sql).fetchall()]
    finally:
        conn.close()


def get_printer(printer_id: str, db_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    conn = _connect(db_path)
    try:
        row = conn.execute("SELECT * FROM printers WHERE id = ?", (printer_id,)).fetchone()
        return _row_to_dict(row) if row else None
    finally:
        conn.close()


def create_printer(data: Dict[str, Any], db_path: Optional[str] = None) -> Dict[str, Any]:
    now = time.time()
    conn = _connect(db_path)
    try:
        for _ in range(5):
            pid = "p-" + uuid.uuid4().hex[:8]
            if conn.execute("SELECT 1 FROM printers WHERE id = ?", (pid,)).fetchone() is None:
                break
        conn.execute("""
            INSERT INTO printers (id, kind, label, host, port, model, access_code, serial,
                                  deleted, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
        """, (pid, data["kind"], data["label"], data["host"], data.get("port"),
              data.get("model"), data.get("access_code"), data.get("serial"),
              now, now))
        conn.commit()
    finally:
        conn.close()
    return get_printer(pid, db_path)


def update_printer(printer_id: str, data: Dict[str, Any], db_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Update editable fields (kind is immutable). Only keys present in data change."""
    allowed = ("label", "host", "port", "model", "access_code", "serial")
    sets, params = [], []
    for f in allowed:
        if f in data:
            sets.append(f"{f} = ?")
            params.append(data[f])
    if not sets:
        return get_printer(printer_id, db_path)
    sets.append("updated_at = ?")
    params.append(time.time())
    params.append(printer_id)
    conn = _connect(db_path)
    try:
        cur = conn.execute("UPDATE printers SET %s WHERE id = ?" % ", ".join(sets), params)
        conn.commit()
        if cur.rowcount == 0:
            return None
    finally:
        conn.close()
    _invalidate_label(printer_id)
    return get_printer(printer_id, db_path)


def delete_printer(printer_id: str, db_path: Optional[str] = None) -> Optional[str]:
    """Soft-delete a running printer (history stays); hard-delete a pending-new one.

    Returns 'soft', 'hard' or None (not found).
    """
    existing = get_printer(printer_id, db_path)
    if existing is None:
        return None
    running_ids = {r["id"] for r in (get_running() or [])}
    conn = _connect(db_path)
    try:
        if printer_id in running_ids:
            conn.execute("UPDATE printers SET deleted = 1, updated_at = ? WHERE id = ?",
                         (time.time(), printer_id))
            mode = "soft"
        else:
            # Never polled — it has no history worth keeping.
            conn.execute("DELETE FROM printers WHERE id = ?", (printer_id,))
            mode = "hard"
        conn.commit()
        _invalidate_label(printer_id)
        return mode
    finally:
        conn.close()


def restore_printer(printer_id: str, db_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    conn = _connect(db_path)
    try:
        cur = conn.execute("UPDATE printers SET deleted = 0, updated_at = ? WHERE id = ?",
                           (time.time(), printer_id))
        conn.commit()
        if cur.rowcount == 0:
            return None
    finally:
        conn.close()
    _invalidate_label(printer_id)
    return get_printer(printer_id, db_path)


def discard_changes(db_path: Optional[str] = None) -> int:
    """Reset the table to the running snapshot. Returns number of restored rows."""
    running = get_running()
    if running is None:
        return 0
    now = time.time()
    conn = _connect(db_path)
    try:
        conn.execute("DELETE FROM printers")
        conn.executemany("""
            INSERT INTO printers (id, kind, label, host, port, model, access_code, serial,
                                  deleted, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
        """, [(r["id"], r["kind"], r["label"], r["host"], r.get("port"), r.get("model"),
               r.get("access_code"), r.get("serial"),
               r.get("created_at", now), now) for r in running])
        conn.commit()
        _invalidate_label()  # bulk reset — clear the whole cache
        return len(running)
    finally:
        conn.close()


def pending_state(row: Dict[str, Any]) -> Optional[str]:
    """'new' | 'modified' | 'deleted' | None for a table row vs the running snapshot."""
    running = get_running()
    if running is None:
        return None
    baseline = {r["id"]: r for r in running}
    base = baseline.get(row["id"])
    if base is None:
        return "new"
    if row.get("deleted"):
        return "deleted"
    for f in _CFG_FIELDS:
        if row.get(f) != base.get(f):
            return "modified"
    return None


def set_running(rows: Optional[List[Dict[str, Any]]]) -> None:
    global _running
    with _lock:
        _running = [dict(r) for r in rows] if rows is not None else None


def get_running() -> Optional[List[Dict[str, Any]]]:
    with _lock:
        return [dict(r) for r in _running] if _running is not None else None


def load_for_startup(cfg: Any, db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """Init + one-time config migration; capture the running snapshot.

    Returns the active (non-deleted) rows the collectors should be built from.
    """
    init_db(db_path)
    migrate_from_config(cfg, db_path)
    # Перезапуск и есть «применить изменения»: строки, помеченные на удаление,
    # здесь вычищаются окончательно. Без этого они переживали рестарт и
    # pending_state показывал их как «новые» (их нет в свежем снапшоте).
    # История в printer_history не трогается.
    conn = _connect(db_path)
    try:
        purged = conn.execute("DELETE FROM printers WHERE deleted = 1").rowcount
        conn.commit()
    finally:
        conn.close()
    if purged:
        logger.info("printer registry: purged %d deleted printers on startup", purged)
    rows = list_printers(db_path, include_deleted=False)
    set_running(rows)
    return rows
