import os
import logging
import sqlite3
import time
from threading import Lock
from typing import List, Dict, Any

from app.domain.models import PrinterStatus

logger = logging.getLogger(__name__)

# Отдельная БД под телеметрию AMS — изолирована от printer_history.db: свой
# темп роста, своя (пока отсутствующая) ретенция, чтобы не раздувать запросы
# истории принтеров.
DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "ams_history.db"
)

# Retention so ams_history.db can't grow unbounded to SQLITE_FULL (where
# record_ams would then fail silently). Shares the printer-history window.
RETENTION_SECONDS = int(os.environ.get("HISTORY_RETENTION_DAYS", "90")) * 86400
_PRUNE_INTERVAL = 3600  # prune at most hourly


class AmsStore:
    """История показателей AMS: влажность, температура, статус сушки.

    По одной строке на AMS-юнит на момент записи. Пишется раз в минуту из
    poll_loop (пиггибэк на тот же троттл, что и printer_history), читается
    вкладкой AMS для графиков.
    """

    def __init__(self):
        self._lock = Lock()
        self._last_prune = 0.0
        # Одно долгоживущее write-соединение под self._lock — как в StateStore,
        # чтобы не плодить connect/close на SD-карте Pi. check_same_thread
        # безопасно отключить: все записи сериализованы локом.
        self._db = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._init_db()

    def _init_db(self):
        conn = self._db
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ams_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                printer_id TEXT NOT NULL,
                label TEXT NOT NULL,
                unit_index INTEGER NOT NULL,
                humidity_idx INTEGER,
                humidity_pct INTEGER,
                temp REAL,
                dry_time INTEGER,
                recorded_at REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_ams_printer_unit_time
            ON ams_history(printer_id, unit_index, recorded_at)
        """)
        conn.commit()

    def record_ams(self, status: PrinterStatus):
        # Как record_snapshot: сбой записи истории AMS не должен всплывать в
        # poll_loop, где его приняли бы за провал фетча принтера. Логируем и
        # идём дальше.
        ams = status.ams
        if not ams or not ams.get("units"):
            return
        try:
            now = time.time()
            with self._lock:
                for idx, unit in enumerate(ams["units"]):
                    # Key history by the STABLE physical unit index (from
                    # _build_ams), falling back to list position for producers
                    # that don't set one — otherwise a changed set of present
                    # units reshuffles which physical unit each row belongs to.
                    unit_index = unit.get("index", idx)
                    self._db.execute("""
                        INSERT INTO ams_history
                        (printer_id, label, unit_index, humidity_idx,
                         humidity_pct, temp, dry_time, recorded_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        status.id, status.label, unit_index,
                        unit.get("humidity"), unit.get("humidity_pct"),
                        unit.get("temp"), unit.get("dry_time"), now,
                    ))
                self._db.commit()
                if now - self._last_prune >= _PRUNE_INTERVAL:
                    self._last_prune = now
                    try:
                        self._db.execute("DELETE FROM ams_history WHERE recorded_at < ?",
                                         (now - RETENTION_SECONDS,))
                        self._db.commit()
                    except Exception:
                        logger.error("ams_history retention prune failed", exc_info=True)
        except Exception:
            logger.error("record_ams failed for %s", status.id, exc_info=True)

    def query(self, printer_id: str, unit_index: int, fr: float, to: float) -> List[Dict[str, Any]]:
        """Временной ряд по юниту в окне [fr, to], по возрастанию времени."""
        with self._lock:
            self._db.row_factory = sqlite3.Row
            rows = self._db.execute("""
                SELECT recorded_at, humidity_idx, humidity_pct, temp, dry_time
                FROM ams_history
                WHERE printer_id = ? AND unit_index = ?
                  AND recorded_at >= ? AND recorded_at <= ?
                ORDER BY recorded_at ASC
            """, (printer_id, unit_index, fr, to)).fetchall()
            self._db.row_factory = None
        return [dict(r) for r in rows]
