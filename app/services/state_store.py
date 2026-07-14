import os
import logging
import sqlite3
import time
from threading import Lock
from typing import Dict, List, Optional, Any, TYPE_CHECKING

from app.domain.models import PrinterStatus

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from app.collectors.bambu_collector import BambuCollector

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "printer_history.db")


class StateStore:
    def __init__(self):
        self._lock = Lock()
        self._statuses: Dict[str, PrinterStatus] = {}
        self._collectors: Dict[str, Any] = {}
        # One long-lived write connection reused for every snapshot, instead of
        # opening/closing a fresh sqlite3 connection 16x/minute. On the Pi's SD
        # card that connect/close churn (plus a full fsync per commit) is the main
        # write-amplification source. All writes go through it under self._lock, so
        # check_same_thread is safe to disable. synchronous=NORMAL is crash-safe
        # under WAL (may lose only the last commit on power loss — acceptable for
        # a 60s snapshot).
        self._db = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._init_db()

    def _init_db(self):
        conn = self._db
        conn.execute("""
            CREATE TABLE IF NOT EXISTS printer_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                printer_id TEXT NOT NULL,
                label TEXT NOT NULL,
                kind TEXT NOT NULL,
                online INTEGER NOT NULL,
                state TEXT NOT NULL,
                progress REAL,
                job_name TEXT,
                eta_sec INTEGER,
                print_time INTEGER,
                nozzle_temp REAL,
                bed_temp REAL,
                layer INTEGER,
                total_layer INTEGER,
                last_error TEXT,
                device_type TEXT,
                recorded_at REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_history_printer_time
            ON printer_history(printer_id, recorded_at)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_history_time
            ON printer_history(recorded_at)
        """)
        # Covering composite for the LAG()/LEAD() window scans in
        # /api/history/events and /api/history/states-summary: they partition
        # by printer_id, order by recorded_at, and read state. Including state
        # lets SQLite satisfy those queries from the index alone.
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_history_printer_time_state
            ON printer_history(printer_id, recorded_at, state)
        """)
        # Grace-period columns migration
        existing = {row[1] for row in conn.execute("PRAGMA table_info(printer_history)").fetchall()}
        if "grace_period_active" not in existing:
            conn.execute("ALTER TABLE printer_history ADD COLUMN grace_period_active INTEGER DEFAULT 0")
        if "last_successful_fetch" not in existing:
            conn.execute("ALTER TABLE printer_history ADD COLUMN last_successful_fetch REAL DEFAULT 0")
        conn.commit()

    def upsert(self, status: PrinterStatus):
        with self._lock:
            self._statuses[status.id] = status

    def record_snapshot(self, status: PrinterStatus):
        # A storage failure (disk full, locked DB) must NOT bubble up into the
        # poll loop, where it would be misread as a printer fetch failure and
        # send every printer into the offline/grace path. Log and move on.
        try:
            with self._lock:
                self._db.execute("""
                    INSERT INTO printer_history
                    (printer_id, label, kind, online, state, progress, job_name,
                     eta_sec, print_time, nozzle_temp, bed_temp, layer, total_layer,
                     last_error, device_type, recorded_at, grace_period_active, last_successful_fetch)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    status.id, status.label, status.kind.value, int(status.online),
                    status.state.value, status.progress_pct, status.job_name,
                    status.eta_seconds, status.print_time_seconds, status.nozzle_temp,
                    status.bed_temp, status.current_layer, status.total_layers,
                    status.last_error, status.device_type, time.time(),
                    int(status.grace_period_active), status.last_successful_fetch
                ))
                self._db.commit()
        except Exception:
            logger.error("record_snapshot failed for %s", status.id, exc_info=True)

    def get_all(self) -> List[PrinterStatus]:
        with self._lock:
            return list(self._statuses.values())

    def get_one(self, printer_id: str) -> Optional[PrinterStatus]:
        with self._lock:
            return self._statuses.get(printer_id)

    def register_collector(self, printer_id: str, collector):
        """Register a collector so commands can be routed to the printer."""
        with self._lock:
            self._collectors[printer_id] = collector

    def get_collector(self, printer_id: str):
        """Return the collector for a printer, or None."""
        with self._lock:
            return self._collectors.get(printer_id)
