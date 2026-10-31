import os
import logging
import sqlite3
import time
from threading import Lock
from typing import Callable, Dict, List, Optional, Any, TYPE_CHECKING

from app.domain.models import PrinterStatus
from app.services import job_journal, notification_tracker, notifications

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from app.collectors.bambu_collector import BambuCollector

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "printer_history.db")

# History older than this is pruned so printer_history.db can't grow without
# bound and eventually SQLITE_FULL (at which point record_snapshot would fail
# silently). ~90 days keeps the history views useful while bounding the file.
RETENTION_SECONDS = int(os.environ.get("HISTORY_RETENTION_DAYS", "90")) * 86400
_PRUNE_INTERVAL = 3600  # prune at most hourly

# printer_history columns added for the temperature chart (temp_history).
HISTORY_TEMP_COLUMNS = ("chamber_temp", "target_nozzle_temp", "target_bed_temp", "target_chamber_temp")


def snapshot_reason(status: PrinterStatus) -> Optional[str]:
    """The history row's reason (notifications.top_reason); a failure there
    costs the reason, never the row."""
    try:
        return notifications.top_reason(status)
    except Exception:
        logger.error("top_reason failed for %s", getattr(status, "id", "?"), exc_info=True)
        return None


class StateStore:
    def __init__(self, history_state: Optional[Callable[[str], Optional[str]]] = None):
        # pid -> the state its history rows get instead of the live one (the
        # maintenance mode, wired by main()), or None for the live state. The
        # store itself knows nothing of the registry.
        self._history_state = history_state
        self._lock = Lock()
        # Separate lock for the SQLite write connection so a slow fsync during a
        # snapshot can't block the in-memory reads (get_all/get_one) that serve
        # /api/printers.
        self._db_lock = Lock()
        self._last_prune = 0.0
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
        # Temperature chart columns. Nullable REAL without a DEFAULT: SQLite
        # only edits the schema, the ~400 MB table on the Pi's SD card is not
        # rewritten. Names come from the fixed tuple, never from input.
        for col in HISTORY_TEMP_COLUMNS:
            if col not in existing:
                conn.execute("ALTER TABLE printer_history ADD COLUMN %s REAL" % col)
        # Why a printer stands (notifications.top_reason): a nullable column
        # without a default, so SQLite only edits the schema, no table rewrite.
        if "reason" not in existing:
            conn.execute("ALTER TABLE printer_history ADD COLUMN reason TEXT")
        conn.commit()
        # Print-job journal tables (print_jobs, job_journal_meta). A failure here
        # must not take the history down with it: the journal just stays empty.
        try:
            job_journal.init_db(conn)
        except Exception:
            logger.error("print_jobs init failed", exc_info=True)

    def upsert(self, status: PrinterStatus):
        # Outside self._lock: the tracker has its own lock and, on a new or
        # cleared notification, a SQLite write that must not block the readers.
        notification_tracker.annotate(status)
        with self._lock:
            self._statuses[status.id] = status

    def _row_state(self, status: PrinterStatus):
        """(state, reason) of the history row: the history_state hook's state
        with no reason (a maintenance row is no pause or error), else the live
        state and its reason. A failing hook costs the substitution, never the row."""
        hook = getattr(self, "_history_state", None)
        if hook is not None:
            try:
                state = hook(status.id)
            except Exception:
                logger.debug("history_state hook failed for %s", status.id, exc_info=True)
                state = None
            if isinstance(state, str) and state:
                return state, None
        return status.state.value, snapshot_reason(status)

    def record_snapshot(self, status: PrinterStatus):
        # A storage failure (disk full, locked DB) must NOT bubble up into the
        # poll loop, where it would be misread as a printer fetch failure and
        # send every printer into the offline/grace path. Log and move on.
        try:
            now = time.time()
            # Outside _db_lock: the hook may read the registry.
            state, reason = self._row_state(status)
            with self._db_lock:
                self._db.execute("""
                    INSERT INTO printer_history
                    (printer_id, label, kind, online, state, progress, job_name,
                     eta_sec, print_time, nozzle_temp, bed_temp, layer, total_layer,
                     last_error, device_type, recorded_at, grace_period_active, last_successful_fetch,
                     reason, chamber_temp, target_nozzle_temp, target_bed_temp, target_chamber_temp)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    status.id, status.label, status.kind.value, int(status.online),
                    state, status.progress_pct, status.job_name,
                    status.eta_seconds, status.print_time_seconds, status.nozzle_temp,
                    status.bed_temp, status.current_layer, status.total_layers,
                    status.last_error, status.device_type, now,
                    int(status.grace_period_active), status.last_successful_fetch,
                    reason, status.chamber_temp, status.target_nozzle_temp,
                    status.target_bed_temp, status.target_chamber_temp
                ))
                self._db.commit()
                self._prune_locked(now)
        except Exception:
            logger.error("record_snapshot failed for %s", status.id, exc_info=True)

    def db_write(self, fn: Callable[[sqlite3.Connection], Any]) -> bool:
        """Run fn(conn) on the write connection under _db_lock, then commit.

        For the other writers of printer_history.db (the print-job journal).
        Never raises, like record_snapshot: on any error the transaction is
        rolled back, the error logged and False returned.
        """
        try:
            with self._db_lock:
                try:
                    fn(self._db)
                    self._db.commit()
                except BaseException:
                    self._db.rollback()
                    raise
            return True
        except Exception:
            logger.error("db_write failed", exc_info=True)
            return False

    def _prune_locked(self, now: float):
        """Delete rows older than the retention window. Caller holds _db_lock."""
        if now - self._last_prune < _PRUNE_INTERVAL:
            return
        self._last_prune = now
        try:
            self._db.execute("DELETE FROM printer_history WHERE recorded_at < ?",
                             (now - RETENTION_SECONDS,))
            job_journal.prune(self._db, now)   # same transaction; never raises
            self._db.commit()
        except Exception:
            logger.error("printer_history retention prune failed", exc_info=True)

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
