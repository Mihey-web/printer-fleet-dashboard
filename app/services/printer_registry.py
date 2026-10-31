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
from typing import Any, Dict, List, Optional, Tuple

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


# Maintenance mode (spec_maintenance): an admin took the printer out of the
# fleet's numbers, notifications, automation and group commands; it is still
# polled and its card still takes single commands. maintenance_since is the one
# flag (NULL: not in maintenance). Not a config field: it applies at once, is
# never a pending change (_CFG_FIELDS), the regular PUT does not touch it and
# "discard changes" keeps it.
MAINT_COLUMNS = (("maintenance_since", "REAL"), ("maintenance_note", "TEXT"), ("maintenance_by", "TEXT"))

# Short-TTL cache of the maintenance map, per DB path: the poll loop, the
# history hook and the automation ask per printer, per poll. Every write path
# invalidates it; the TTL only bounds what a missed one could cost.
_MAINT_TTL = 5.0
_maint_cache: Dict[str, Tuple[Dict[str, Dict[str, Any]], float]] = {}   # path -> (map, ts)
_maint_good: Dict[str, Dict[str, Dict[str, Any]]] = {}                  # path -> last good read
_maint_gen = 0    # bumped by every invalidation: a read that raced a write is not cached
_maint_lock = Lock()


def maintenance_info(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """{"since", "note", "by"} of a registry row in maintenance mode, else None."""
    since = row.get("maintenance_since")
    if since is None:
        return None
    return {"since": float(since), "note": row.get("maintenance_note"), "by": row.get("maintenance_by")}


def _invalidate_maintenance() -> None:
    global _maint_gen
    with _maint_lock:
        _maint_gen += 1
        _maint_cache.clear()


def _copy_map(m: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {pid: dict(info) for pid, info in m.items()}


def maintenance_map(db_path: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    """{pid: {"since", "note", "by"}} of every printer in maintenance mode,
    soft-deleted rows included (they are polled until the restart).

    Never raises: an unreadable registry answers the last good map, without
    one {} (logged at debug). A printer then counts as not in maintenance for
    a few seconds; no command can reach it anyway, printer_commands.send
    refuses when it cannot read the same registry.
    """
    path = db_path or DB_PATH
    now = time.time()
    with _maint_lock:
        hit = _maint_cache.get(path)
        if hit is not None and 0 <= now - hit[1] < _MAINT_TTL:
            return _copy_map(hit[0])
        gen = _maint_gen
    try:
        conn = _connect(path)
        try:
            rows = conn.execute(
                "SELECT id, maintenance_since, maintenance_note, maintenance_by FROM printers"
                " WHERE maintenance_since IS NOT NULL").fetchall()
        finally:
            conn.close()
        data = {r["id"]: maintenance_info(dict(r)) for r in rows}
        ok = True
    except Exception:
        logger.debug("maintenance map read failed", exc_info=True)
        data, ok = None, False
    with _maint_lock:
        if ok:
            _maint_good[path] = data
        else:
            data = _maint_good.get(path, {})
        if gen == _maint_gen:      # no write landed meanwhile
            _maint_cache[path] = (data, now)
        return _copy_map(data)


def in_maintenance(printer_id: str, db_path: Optional[str] = None) -> bool:
    """True while the printer is in maintenance mode (cached, never raises)."""
    return printer_id in maintenance_map(db_path)


def maintenance_of(printer_id: str, db_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """The printer's {"since", "note", "by"} in maintenance mode, else None (cached, never raises)."""
    return maintenance_map(db_path).get(printer_id)


def set_maintenance(printer_id: str, on: bool, note: Optional[str] = None, by: Optional[str] = None,
                    now: Optional[float] = None,
                    db_path: Optional[str] = None) -> Optional[Tuple[str, Optional[Dict[str, Any]]]]:
    """Turn maintenance mode on / off, or change its note.

    Returns None for an unknown printer, else (change, info):
    "on" (since = now, note, by), "note" (already on: only the note changes,
    since and by stay), "off" (all three NULL) or "none" (nothing to change);
    info is the mode after the call ({"since", "note", "by"}) or None.
    An empty note is stored as NULL. Raises on a storage error.
    """
    note = note or None
    conn = _connect(db_path)
    try:
        # Read and write under one write lock: two admins switching at once
        # must not both see "off" and both answer "on".
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT maintenance_since, maintenance_note, maintenance_by FROM printers"
                           " WHERE id = ?", (printer_id,)).fetchone()
        if row is None:
            conn.rollback()
            return None
        cur = {"maintenance_since": row[0], "maintenance_note": row[1], "maintenance_by": row[2]}
        if on and cur["maintenance_since"] is None:
            change = "on"
            cur = {"maintenance_since": time.time() if now is None else float(now),
                   "maintenance_note": note, "maintenance_by": by}
        elif on:
            change = "note" if note != cur["maintenance_note"] else "none"
            cur["maintenance_note"] = note
        else:
            change = "off" if cur["maintenance_since"] is not None else "none"
            cur = {"maintenance_since": None, "maintenance_note": None, "maintenance_by": None}
        if change != "none":
            conn.execute("UPDATE printers SET maintenance_since = ?, maintenance_note = ?, maintenance_by = ?"
                         " WHERE id = ?", (cur["maintenance_since"], cur["maintenance_note"],
                                           cur["maintenance_by"], printer_id))
        conn.commit()
    finally:
        conn.close()
    if change != "none":
        _invalidate_maintenance()
    return change, maintenance_info(cur)


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
        # Maintenance mode columns (nullable, no default: a schema-only change).
        for col, typ in MAINT_COLUMNS:
            if col not in cols:
                conn.execute("ALTER TABLE printers ADD COLUMN %s %s" % (col, typ))
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
        _invalidate_maintenance()
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
    _invalidate_maintenance()
    return get_printer(printer_id, db_path)


def discard_changes(db_path: Optional[str] = None) -> int:
    """Reset the table to the running snapshot. Returns number of restored rows.

    The maintenance mode is no config change: the surviving printers keep
    their current one (the snapshot holds it as of the start).
    """
    running = get_running()
    if running is None:
        return 0
    now = time.time()
    conn = _connect(db_path)
    try:
        maint = {r[0]: tuple(r[1:]) for r in conn.execute(
            "SELECT id, maintenance_since, maintenance_note, maintenance_by FROM printers"
            " WHERE maintenance_since IS NOT NULL").fetchall()}
        conn.execute("DELETE FROM printers")
        conn.executemany("""
            INSERT INTO printers (id, kind, label, host, port, model, access_code, serial,
                                  deleted, created_at, updated_at,
                                  maintenance_since, maintenance_note, maintenance_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)
        """, [(r["id"], r["kind"], r["label"], r["host"], r.get("port"), r.get("model"),
               r.get("access_code"), r.get("serial"),
               r.get("created_at", now), now) + maint.get(r["id"], (None, None, None)) for r in running])
        conn.commit()
        _invalidate_label()  # bulk reset — clear the whole cache
        _invalidate_maintenance()
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
        _invalidate_maintenance()
    rows = list_printers(db_path, include_deleted=False)
    set_running(rows)
    return rows
