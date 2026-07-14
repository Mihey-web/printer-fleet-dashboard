"""Server settings in SQLite instead of config.py constants.

Telegram bot and proxy configuration lives in the `settings` table
(printer_history.db) and is edited from the settings page. config.py values
remain only as a one-time migration seed. Unlike the printer registry there is
no pending/apply cycle: changes take effect immediately (telegram_manager
hot-applies them).

Values are JSON-encoded so booleans, ints and lists round-trip with their
types. Reads go through an in-memory cache under a lock — the poll loop asks
for notification settings on every print event.
"""
import json
import logging
import os
import sqlite3
import time
from threading import Lock
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "printer_history.db")

DEFAULTS: Dict[str, Any] = {
    "telegram_enabled": False,
    "telegram_token": "",
    "telegram_chat_id": None,
    "telegram_update_interval": 2,
    "telegram_notify_on_finish": False,
    "telegram_finish_template": "✅ Печать завершена: {label}",
    "telegram_notify_on_error": False,
    "telegram_error_template": "\U0001f534 Ошибка: {label}",
    "telegram_notify_on_paused": False,
    "telegram_paused_template": "⚠️ Пауза: {label}",
    "proxy_list": [],
    "proxy_check_interval": 600,
    # printer_id -> bool: принимает ли прошивка print-класс (вердикт зонда).
    # Персистится, чтобы после рестарта не считать capability «неизвестной»
    # (=по умолчанию можно) и не предлагать управление там, где оно отклонится.
    "printer_capability": {},
}

# config.py attribute -> settings key, for the one-time migration seed.
_CONFIG_MAP = {
    "TELEGRAM_ENABLED": "telegram_enabled",
    "TELEGRAM_TOKEN": "telegram_token",
    "TELEGRAM_ALLOWED_CHAT_ID": "telegram_chat_id",
    "TELEGRAM_UPDATE_INTERVAL": "telegram_update_interval",
    "TELEGRAM_NOTIFY_ON_FINISH": "telegram_notify_on_finish",
    "TELEGRAM_FINISH_MESSAGE_TEMPLATE": "telegram_finish_template",
    "TELEGRAM_NOTIFY_ON_ERROR": "telegram_notify_on_error",
    "TELEGRAM_ERROR_MESSAGE_TEMPLATE": "telegram_error_template",
    "TELEGRAM_NOTIFY_ON_PAUSED": "telegram_notify_on_paused",
    "TELEGRAM_PAUSED_MESSAGE_TEMPLATE": "telegram_paused_template",
    "PROXY_LIST": "proxy_list",
    "PROXY_CHECK_INTERVAL": "proxy_check_interval",
}

_lock = Lock()
_cache: Optional[Dict[str, Any]] = None


def _connect(db_path: Optional[str] = None) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path or DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Optional[str] = None) -> None:
    conn = _connect(db_path)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at REAL NOT NULL
            )
        """)
        conn.commit()
    finally:
        conn.close()


def migrate_from_config(cfg: Any, db_path: Optional[str] = None) -> int:
    """One-time import of TELEGRAM_*/PROXY_* from config.py.

    Runs only while the table is empty. A legacy single PROXY_URL is folded
    into proxy_list if the list itself is missing/empty. Returns the number of
    imported keys.
    """
    conn = _connect(db_path)
    try:
        if conn.execute("SELECT COUNT(*) FROM settings").fetchone()[0] > 0:
            return 0
        now = time.time()
        imported = 0
        for attr, key in _CONFIG_MAP.items():
            if not hasattr(cfg, attr):
                continue
            value = getattr(cfg, attr)
            if key == "proxy_list":
                value = list(value or [])
            conn.execute(
                "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
                (key, json.dumps(value), now),
            )
            imported += 1
        if not json.loads(_fetch(conn, "proxy_list") or "[]"):
            legacy = getattr(cfg, "PROXY_URL", None)
            if legacy:
                conn.execute(
                    "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
                    ("proxy_list", json.dumps([legacy]), now),
                )
                imported += 1
        conn.commit()
        if imported:
            logger.info("Settings migrated from config.py: %d keys", imported)
        return imported
    finally:
        conn.close()


def _fetch(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def _read_all(db_path: Optional[str] = None) -> Dict[str, Any]:
    conn = _connect(db_path)
    try:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    finally:
        conn.close()
    values = dict(DEFAULTS)
    for r in rows:
        if r["key"] in DEFAULTS:
            try:
                values[r["key"]] = json.loads(r["value"])
            except ValueError:
                logger.warning("Settings key %s holds invalid JSON, using default", r["key"])
    return values


def get_all(db_path: Optional[str] = None) -> Dict[str, Any]:
    """All settings with defaults overlaid. Cached after load()."""
    global _cache
    with _lock:
        if _cache is not None and db_path is None:
            return dict(_cache)
    values = _read_all(db_path)
    if db_path is None:
        with _lock:
            _cache = dict(values)
    return values


def get(key: str, db_path: Optional[str] = None) -> Any:
    if key not in DEFAULTS:
        raise KeyError(key)
    return get_all(db_path)[key]


def set_many(values: Dict[str, Any], db_path: Optional[str] = None) -> None:
    """Persist a partial settings dict. Keys must be known."""
    unknown = [k for k in values if k not in DEFAULTS]
    if unknown:
        raise KeyError("Unknown settings keys: %s" % ", ".join(unknown))
    if not values:
        return
    now = time.time()
    conn = _connect(db_path)
    try:
        for key, value in values.items():
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
                (key, json.dumps(value), now),
            )
        conn.commit()
    finally:
        conn.close()
    global _cache
    with _lock:
        if _cache is not None and db_path is None:
            _cache.update(values)


def reset_cache() -> None:
    """Drop the in-memory cache (tests)."""
    global _cache
    with _lock:
        _cache = None


def load(cfg: Any, db_path: Optional[str] = None) -> Dict[str, Any]:
    """Startup entry point: init, one-time migration, warm the cache."""
    init_db(db_path)
    migrate_from_config(cfg, db_path)
    reset_cache()
    return get_all(db_path)
