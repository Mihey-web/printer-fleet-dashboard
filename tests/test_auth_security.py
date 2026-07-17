"""Security regression tests for app/api/auth.py helpers.

Covers the X-Real-IP rate-limit bypass fix (client_ip trusted-proxy gating)
and the persistent refresh-token (JTI) revocation that must survive restarts.
"""
import time
import types

from app.api import auth
from app.services import audit_service


def _req(direct_host, headers=None):
    headers = headers or {}

    class _Headers:
        def get(self, key, default=None):
            return headers.get(key, default)

    return types.SimpleNamespace(
        client=types.SimpleNamespace(host=direct_host),
        headers=_Headers(),
    )


def test_client_ip_ignores_spoofed_header_from_untrusted_source():
    # Direct connection from an arbitrary host: a forged X-Real-IP must be ignored
    # so the attacker cannot rotate it to dodge the login rate limit.
    r = _req("203.0.113.9", {"x-real-ip": "1.2.3.4"})
    assert auth.client_ip(r) == "203.0.113.9"


def test_client_ip_trusts_header_from_known_proxy():
    r = _req("127.0.0.1", {"x-real-ip": "198.51.100.7"})
    assert auth.client_ip(r) == "198.51.100.7"


def test_client_ip_falls_back_to_direct_when_no_header():
    r = _req("127.0.0.1", {})
    assert auth.client_ip(r) == "127.0.0.1"


def test_jti_revocation_persists_and_purges(tmp_path, monkeypatch):
    db = str(tmp_path / "users.db")
    monkeypatch.setattr(auth, "USERS_DB", db)
    auth.init_users_db()

    assert auth._is_jti_revoked("token-a") is False
    auth._revoke_jti("token-a", time.time() + 3600)
    # Survives a "restart": a brand-new query against the same file still sees it.
    assert auth._is_jti_revoked("token-a") is True

    # Expired entries are purged; live ones are kept.
    auth._revoke_jti("token-old", time.time() - 10)
    auth._purge_expired_jtis()
    assert auth._is_jti_revoked("token-old") is False
    assert auth._is_jti_revoked("token-a") is True


def test_revoke_jti_is_single_use_atomic(tmp_path, monkeypatch):
    # The first claim wins (True); a second claim of the same token loses (False).
    # This is what makes a concurrent refresh-token double-use impossible.
    db = str(tmp_path / "users.db")
    monkeypatch.setattr(auth, "USERS_DB", db)
    auth.init_users_db()

    assert auth._revoke_jti("tok", time.time() + 3600) is True
    assert auth._revoke_jti("tok", time.time() + 3600) is False


def test_count_failed_attempts_by_username(tmp_path, monkeypatch):
    db = str(tmp_path / "users.db")
    monkeypatch.setattr(audit_service, "USERS_DB", db)
    svc = audit_service.AuditService()

    assert svc.count_failed_attempts_by_username("alice") == 0
    for _ in range(3):
        svc.log("alice", "login_fail", "1.1.1.1", "ua")
    svc.log("bob", "login_fail", "2.2.2.2", "ua")
    # Counts per-username across all IPs, independent of the per-IP counter.
    assert svc.count_failed_attempts_by_username("alice") == 3
    assert svc.count_failed_attempts_by_username("bob") == 1


def _insert_user(db, username, role="viewer", tokens_valid_after=0):
    import sqlite3
    now = time.time()
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO users (username, password_hash, role, created_at, updated_at, tokens_valid_after)"
        " VALUES (?,?,?,?,?,?)",
        (username, "x", role, now, now, tokens_valid_after),
    )
    conn.commit()
    conn.close()


def test_token_subject_active_rejects_deleted_user(tmp_path, monkeypatch):
    # A token whose subject no longer exists (deleted) must be rejected — this is
    # what revokes a deleted user's still-valid access token.
    db = str(tmp_path / "users.db")
    monkeypatch.setattr(auth, "USERS_DB", db)
    auth.init_users_db()
    assert auth.token_subject_active("ghost", time.time()) is False


def test_token_subject_active_accepts_live_user(tmp_path, monkeypatch):
    db = str(tmp_path / "users.db")
    monkeypatch.setattr(auth, "USERS_DB", db)
    auth.init_users_db()
    _insert_user(db, "alice", tokens_valid_after=0)
    assert auth.token_subject_active("alice", time.time()) is True


def test_token_subject_active_honours_tokens_valid_after_watermark(tmp_path, monkeypatch):
    # A role/password change bumps tokens_valid_after; tokens minted before that
    # watermark are rejected, tokens minted after it are accepted.
    db = str(tmp_path / "users.db")
    monkeypatch.setattr(auth, "USERS_DB", db)
    auth.init_users_db()
    now = time.time()
    _insert_user(db, "bob", role="admin", tokens_valid_after=now + 100)
    assert auth.token_subject_active("bob", now) is False
    assert auth.token_subject_active("bob", now + 200) is True


def test_jwt_secret_env_override_wins(tmp_path, monkeypatch):
    # An explicit FORGE_JWT_SECRET takes priority over anything stored/generated.
    db = str(tmp_path / "users.db")
    monkeypatch.setattr(auth, "USERS_DB", db)
    auth.init_users_db()
    monkeypatch.setenv("FORGE_JWT_SECRET", "explicit-env-secret")
    assert auth.get_or_create_jwt_secret() == "explicit-env-secret"
    # Env wins even if a different secret is already persisted.
    auth._set_app_secret("jwt_secret", "stored-secret")
    assert auth.get_or_create_jwt_secret() == "explicit-env-secret"


def test_jwt_secret_generated_when_absent(tmp_path, monkeypatch):
    db = str(tmp_path / "users.db")
    monkeypatch.setattr(auth, "USERS_DB", db)
    monkeypatch.delenv("FORGE_JWT_SECRET", raising=False)
    auth.init_users_db()
    secret = auth.get_or_create_jwt_secret()
    assert isinstance(secret, str) and len(secret) >= 32
    # It was persisted to the app_secrets table.
    assert auth._get_app_secret("jwt_secret") == secret


def test_jwt_secret_persists_across_restarts(tmp_path, monkeypatch):
    # Second resolution (simulating a restart) returns the SAME persisted value,
    # so existing sessions survive a reboot.
    db = str(tmp_path / "users.db")
    monkeypatch.setattr(auth, "USERS_DB", db)
    monkeypatch.delenv("FORGE_JWT_SECRET", raising=False)
    auth.init_users_db()
    first = auth.get_or_create_jwt_secret()
    second = auth.get_or_create_jwt_secret()
    assert first == second


def test_init_users_db_creates_app_secrets_table(tmp_path, monkeypatch):
    import sqlite3
    db = str(tmp_path / "users.db")
    monkeypatch.setattr(auth, "USERS_DB", db)
    auth.init_users_db()
    conn = sqlite3.connect(db)
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    finally:
        conn.close()
    assert "app_secrets" in tables


def test_init_users_db_adds_tokens_valid_after_column(tmp_path, monkeypatch):
    import sqlite3
    db = str(tmp_path / "users.db")
    monkeypatch.setattr(auth, "USERS_DB", db)
    auth.init_users_db()
    conn = sqlite3.connect(db)
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
    finally:
        conn.close()
    assert "tokens_valid_after" in cols


# --- Endpoint-level regressions (login lockout DoS + refresh watermark) -------

import pytest
from fastapi import HTTPException

from app.services.auth_service import hash_password, create_refresh_token
from app.domain.user_models import UserRole


class _FakeResponse:
    def __init__(self):
        self.cookies = {}

    def set_cookie(self, key, value, **kwargs):
        self.cookies[key] = value

    def delete_cookie(self, key, **kwargs):
        self.cookies.pop(key, None)


def _full_user(db, username, password, role="admin", tokens_valid_after=0):
    import sqlite3
    now = time.time()
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO users (username, password_hash, role, created_at, updated_at, tokens_valid_after)"
        " VALUES (?,?,?,?,?,?)",
        (username, hash_password(password), role, now, now, tokens_valid_after),
    )
    conn.commit()
    conn.close()


def _wire_auth_db(tmp_path, monkeypatch):
    """Point both auth.py and the audit singleton at a fresh temp users.db."""
    db = str(tmp_path / "users.db")
    monkeypatch.setattr(auth, "USERS_DB", db)
    monkeypatch.setattr(auth, "JWT_SECRET", "test-secret-please")
    monkeypatch.setattr(audit_service, "USERS_DB", db)
    monkeypatch.setattr(audit_service, "_audit", None)
    auth.init_users_db()
    return db


def test_login_valid_password_not_locked_out_by_username_flood(tmp_path, monkeypatch):
    # A flood of failed logins for 'admin' from other IPs must NOT stop the real
    # owner (supplying the correct password) from logging in — otherwise anyone
    # can lock out an account by spamming its username. (account-lockout DoS)
    db = _wire_auth_db(tmp_path, monkeypatch)
    _full_user(db, "admin", "correct-horse", role="admin")
    svc = audit_service.get_audit_service()
    for _ in range(15):
        svc.log("admin", "login_fail", "203.0.113.7", "ua")  # attacker, wrong pw

    req = _req("203.0.113.99")  # a different, clean source IP (the real owner)
    resp = _FakeResponse()
    body = auth.LoginRequest(username="admin", password="correct-horse")
    result = auth.login(body, req, resp)
    assert result["user"]["username"] == "admin"
    assert "access_token" in resp.cookies


def test_login_wrong_password_still_locked_after_threshold(tmp_path, monkeypatch):
    # Brute-force protection is preserved: wrong credentials past the per-username
    # threshold get 429, not 401.
    db = _wire_auth_db(tmp_path, monkeypatch)
    _full_user(db, "admin", "correct-horse", role="admin")
    svc = audit_service.get_audit_service()
    for _ in range(12):
        svc.log("admin", "login_fail", "203.0.113.7", "ua")

    req = _req("203.0.113.7")
    resp = _FakeResponse()
    body = auth.LoginRequest(username="admin", password="WRONG")
    with pytest.raises(HTTPException) as exc:
        auth.login(body, req, resp)
    assert exc.value.status_code == 429


def test_refresh_rejects_token_predating_watermark(tmp_path, monkeypatch):
    # A password/role change bumps tokens_valid_after; a refresh token minted
    # before that watermark must be rejected, so the reset terminates sessions on
    # the refresh path too (not just the ~15-min access path).
    db = _wire_auth_db(tmp_path, monkeypatch)
    now = time.time()
    _full_user(db, "bob", "pw", role="viewer", tokens_valid_after=now + 1000)
    # Token issued "now" — before the future watermark.
    token, _jti = create_refresh_token("bob", auth.JWT_SECRET, auth.JWT_REFRESH_EXPIRES)

    req = _req("203.0.113.5")
    resp = _FakeResponse()
    with pytest.raises(HTTPException) as exc:
        auth.refresh_token(req, resp, refresh_token_cookie=token)
    assert exc.value.status_code == 401


def test_refresh_accepts_token_after_watermark(tmp_path, monkeypatch):
    # A token minted after the watermark still refreshes normally and rotates.
    db = _wire_auth_db(tmp_path, monkeypatch)
    _full_user(db, "bob", "pw", role="viewer", tokens_valid_after=0)
    token, _jti = create_refresh_token("bob", auth.JWT_SECRET, auth.JWT_REFRESH_EXPIRES)

    req = _req("203.0.113.5")
    resp = _FakeResponse()
    result = auth.refresh_token(req, resp, refresh_token_cookie=token)
    assert result["user"]["username"] == "bob"
    assert "access_token" in resp.cookies
    assert "refresh_token" in resp.cookies
