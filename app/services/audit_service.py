import os
import sqlite3
import time
from typing import Optional, List
from threading import Lock

from app.domain.user_models import AuditEntry

USERS_DB = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "users.db")


class AuditService:
    def __init__(self):
        self._lock = Lock()
        self._init_db()

    def _init_db(self):
        conn = sqlite3.connect(USERS_DB, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT,
                action TEXT NOT NULL,
                ip_address TEXT,
                user_agent TEXT,
                created_at REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_audit_user
            ON audit_log(user_id, created_at DESC)
        """)
        # Speeds up the per-IP / per-username failed-login counters run on every login.
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_audit_action_ip
            ON audit_log(action, ip_address, created_at)
        """)
        conn.commit()
        conn.close()

    def log(self, user_id: Optional[str], action: str,
            ip_address: Optional[str] = None,
            user_agent: Optional[str] = None):
        # Cap the user-agent so a hostile client can't bloat the DB with a 64KB
        # header on every request.
        user_agent = (user_agent or "")[:512]
        with self._lock:
            conn = sqlite3.connect(USERS_DB, timeout=10)
            conn.execute("""
                INSERT INTO audit_log (user_id, action, ip_address, user_agent, created_at)
                VALUES (?, ?, ?, ?, ?)
            """, (user_id, action, ip_address, user_agent, time.time()))
            conn.commit()
            conn.close()

    def get_logs(self, limit: int = 100, offset: int = 0,
                 user_id: Optional[str] = None,
                 action: Optional[str] = None) -> List[AuditEntry]:
        conn = sqlite3.connect(USERS_DB, timeout=10)
        conn.row_factory = sqlite3.Row
        where = []
        params = []
        if user_id:
            where.append("user_id = ?")
            params.append(user_id)
        if action:
            where.append("action = ?")
            params.append(action)
        where_clause = " AND ".join(where) if where else "1=1"
        rows = conn.execute(f"""
            SELECT id, user_id, action, ip_address, user_agent, created_at
            FROM audit_log
            WHERE {where_clause}
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?
        """, (*params, limit, offset)).fetchall()
        conn.close()
        return [AuditEntry(
            id=r["id"], user_id=r["user_id"], action=r["action"],
            ip_address=r["ip_address"], user_agent=r["user_agent"],
            created_at=r["created_at"]
        ) for r in rows]

    def count_failed_attempts(self, ip_address: str, window_sec: int = 900) -> int:
        conn = sqlite3.connect(USERS_DB, timeout=10)
        row = conn.execute("""
            SELECT COUNT(*) as cnt FROM audit_log
            WHERE action = 'login_fail' AND ip_address = ?
            AND created_at > ?
        """, (ip_address, time.time() - window_sec)).fetchone()
        conn.close()
        return row[0] if row else 0

    def count_failed_attempts_by_username(self, username: str, window_sec: int = 900) -> int:
        """Count recent failed logins for a username across ALL source IPs.

        Complements the per-IP counter so credential-stuffing from a pool of
        proxies (one attempt per IP) still trips a per-account lockout.
        """
        if not username:
            return 0
        conn = sqlite3.connect(USERS_DB, timeout=10)
        try:
            row = conn.execute("""
                SELECT COUNT(*) as cnt FROM audit_log
                WHERE action = 'login_fail' AND user_id = ?
                AND created_at > ?
            """, (username, time.time() - window_sec)).fetchone()
            return row[0] if row else 0
        finally:
            conn.close()


_audit: Optional[AuditService] = None
_audit_lock = Lock()


def get_audit_service() -> AuditService:
    global _audit
    if _audit is None:
        # Double-checked locking: FastAPI dispatches sync routes on a thread pool,
        # so two first-requests could otherwise both construct the service and race
        # the schema init.
        with _audit_lock:
            if _audit is None:
                _audit = AuditService()
    return _audit
