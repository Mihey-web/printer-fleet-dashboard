"""One way to open the panel's SQLite databases (app/services/data_dir.py names the files).

Every connection waits the same BUSY_TIMEOUT_S for another writer's lock (the poll
loop, the API threads and the backfill share printer_history.db) before SQLite
gives up with "database is locked".
"""
import sqlite3

BUSY_TIMEOUT_S = 10


def connect(database, **kwargs) -> sqlite3.Connection:
    """sqlite3.connect with the shared lock timeout. Any other sqlite3.connect argument
    (check_same_thread, uri, ...) passes through; an explicit timeout= still wins."""
    kwargs.setdefault("timeout", BUSY_TIMEOUT_S)
    return sqlite3.connect(database, **kwargs)
