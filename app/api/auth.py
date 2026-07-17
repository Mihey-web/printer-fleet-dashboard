from fastapi import APIRouter, Request, Response, HTTPException, Cookie
from pydantic import BaseModel
from typing import Optional
import sqlite3
import time
import os

from app.domain.user_models import UserRole, User
from app.services.auth_service import (
    hash_password, verify_password, create_access_token,
    create_refresh_token, decode_token, get_user_from_token, generate_secret,
)
from app.services.audit_service import get_audit_service

USERS_DB = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "users.db")

router = APIRouter(prefix="/api/auth", tags=["auth"])

JWT_SECRET: str = ""
JWT_ACCESS_EXPIRES: int = 15 * 60
JWT_REFRESH_EXPIRES: int = 7 * 24 * 3600
COOKIE_SECURE: bool = True
# Only these source addresses (i.e. the reverse proxy in front of us) are
# allowed to set the real client IP via X-Real-IP / X-Forwarded-For. Anyone
# else's spoofed header is ignored, so the login rate-limit can't be bypassed
# by rotating the header.
TRUSTED_PROXIES: frozenset = frozenset({"127.0.0.1", "::1"})

# A fixed bcrypt hash to verify against when the username doesn't exist, so the
# login path spends the same ~bcrypt time whether or not the account is real.
# Computed lazily on first use to avoid paying the cost at import time.
_DUMMY_PASSWORD_HASH: str = ""


def _dummy_hash() -> str:
    global _DUMMY_PASSWORD_HASH
    if not _DUMMY_PASSWORD_HASH:
        _DUMMY_PASSWORD_HASH = hash_password("invalid-placeholder-never-matches")
    return _DUMMY_PASSWORD_HASH


def init_auth(secret: str, access_expires: int = 15 * 60, refresh_expires: int = 7 * 24 * 3600,
              cookie_secure: bool = True, trusted_proxies=None):
    global JWT_SECRET, JWT_ACCESS_EXPIRES, JWT_REFRESH_EXPIRES, COOKIE_SECURE, TRUSTED_PROXIES
    JWT_SECRET = secret
    JWT_ACCESS_EXPIRES = access_expires
    JWT_REFRESH_EXPIRES = refresh_expires
    COOKIE_SECURE = cookie_secure
    if trusted_proxies is not None:
        TRUSTED_PROXIES = frozenset(trusted_proxies)


def client_ip(request: Request) -> str:
    """Resolve the real client IP, trusting proxy headers only from a known proxy."""
    direct = request.client.host if request.client else "unknown"
    if direct in TRUSTED_PROXIES:
        forwarded = request.headers.get("x-real-ip") or request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return direct


def _revoke_jti(jti: str, exp: float) -> bool:
    """Atomically claim a refresh-token id as revoked.

    Returns True if this call was the one that revoked it (row newly inserted),
    False if it was already revoked. Using INSERT OR IGNORE + rowcount collapses
    the check-and-revoke into one atomic step, closing the TOCTOU window where two
    concurrent refreshes with the same token could both succeed.
    """
    if not jti:
        return False
    conn = sqlite3.connect(USERS_DB, timeout=10)
    try:
        cur = conn.execute("INSERT OR IGNORE INTO revoked_tokens (jti, exp) VALUES (?, ?)",
                           (jti, float(exp or 0)))
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def _is_jti_revoked(jti: str) -> bool:
    if not jti:
        return False
    conn = sqlite3.connect(USERS_DB, timeout=10)
    try:
        row = conn.execute("SELECT 1 FROM revoked_tokens WHERE jti = ?", (jti,)).fetchone()
        return row is not None
    finally:
        conn.close()


def _purge_expired_jtis():
    conn = sqlite3.connect(USERS_DB, timeout=10)
    try:
        conn.execute("DELETE FROM revoked_tokens WHERE exp < ?", (time.time(),))
        conn.commit()
    finally:
        conn.close()


def init_users_db():
    conn = sqlite3.connect(USERS_DB, timeout=10)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'viewer',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS revoked_tokens (
            jti TEXT PRIMARY KEY,
            exp REAL NOT NULL
        )
    """)
    # tokens_valid_after: any access token whose iat predates this epoch is
    # rejected by the auth middleware. Lets a role/password change invalidate a
    # user's live sessions immediately instead of waiting out the access-token
    # TTL. (Deletion is handled separately — the row is gone, so the subject
    # check fails outright.)
    existing = {row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
    if "tokens_valid_after" not in existing:
        conn.execute("ALTER TABLE users ADD COLUMN tokens_valid_after REAL NOT NULL DEFAULT 0")
    # app_secrets: small key/value store for server-generated secrets that must
    # persist across restarts (currently only the JWT signing key). Lives in the
    # auth DB so it sits next to the credentials it protects and is covered by the
    # same gitignore/backup rules.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS app_secrets (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


def _get_app_secret(key: str) -> Optional[str]:
    conn = sqlite3.connect(USERS_DB, timeout=10)
    try:
        row = conn.execute("SELECT value FROM app_secrets WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _set_app_secret(key: str, value: str) -> None:
    # INSERT OR IGNORE so a concurrent boot that already wrote the secret wins;
    # the caller re-reads to converge on the single stored value.
    conn = sqlite3.connect(USERS_DB, timeout=10)
    try:
        conn.execute("INSERT OR IGNORE INTO app_secrets (key, value) VALUES (?, ?)", (key, value))
        conn.commit()
    finally:
        conn.close()


def get_or_create_jwt_secret() -> str:
    """Resolve the JWT signing secret, in priority order:

      1. env FORGE_JWT_SECRET, if set — lets ops pin/rotate it explicitly.
      2. a secret previously persisted in users.db (app_secrets) — stable across
         restarts so existing sessions survive a reboot.
      3. a freshly generated secrets.token_hex(32), persisted for next time.

    Consequence (intended): a deployment with neither env nor stored secret mints
    a new random one on first boot, invalidating any pre-existing sessions. That
    is exactly what retires an old/compromised secret. No secret is baked into the
    source tree.

    Requires init_users_db() to have created the app_secrets table first.
    """
    env = os.environ.get("FORGE_JWT_SECRET")
    if env:
        return env
    stored = _get_app_secret("jwt_secret")
    if stored:
        return stored
    new_secret = generate_secret()
    _set_app_secret("jwt_secret", new_secret)
    # Re-read: if another process inserted first, its value is the canonical one.
    return _get_app_secret("jwt_secret") or new_secret


def token_subject_active(username: str, iat: float) -> bool:
    """Is an access token for `username` issued at `iat` still valid?

    False when the user no longer exists (deleted → tokens revoked) or when the
    token was issued before the user's tokens_valid_after watermark (forced
    invalidation on role/password change). This is what closes the window where
    a stateless access token outlives the account it was minted for.
    """
    if not username:
        return False
    conn = sqlite3.connect(USERS_DB, timeout=10)
    try:
        row = conn.execute(
            "SELECT tokens_valid_after FROM users WHERE username = ?", (username,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return False
    return float(iat or 0) >= float(row[0] or 0)


def get_user_by_username(username: str) -> Optional[User]:
    conn = sqlite3.connect(USERS_DB, timeout=10)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    conn.close()
    if row is None:
        return None
    return User(
        id=row["id"], username=row["username"], password_hash=row["password_hash"],
        role=UserRole(row["role"]), created_at=row["created_at"], updated_at=row["updated_at"],
    )


def get_user_by_id(user_id: int) -> Optional[User]:
    conn = sqlite3.connect(USERS_DB, timeout=10)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    if row is None:
        return None
    return User(
        id=row["id"], username=row["username"], password_hash=row["password_hash"],
        role=UserRole(row["role"]), created_at=row["created_at"], updated_at=row["updated_at"],
    )


def get_all_users() -> list[User]:
    conn = sqlite3.connect(USERS_DB, timeout=10)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM users ORDER BY id").fetchall()
    conn.close()
    return [User(
        id=r["id"], username=r["username"], password_hash=r["password_hash"],
        role=UserRole(r["role"]), created_at=r["created_at"], updated_at=r["updated_at"],
    ) for r in rows]


class LoginRequest(BaseModel):
    username: str
    password: str


def _set_token_cookies(response: Response, access: str, refresh: str):
    response.set_cookie(
        key="access_token", value=access,
        httponly=True, secure=COOKIE_SECURE, samesite="strict",
        max_age=JWT_ACCESS_EXPIRES, path="/",
    )
    response.set_cookie(
        key="refresh_token", value=refresh,
        httponly=True, secure=COOKIE_SECURE, samesite="strict",
        max_age=JWT_REFRESH_EXPIRES, path="/api/auth",
    )


@router.post("/login")
def login(body: LoginRequest, request: Request, response: Response):
    audit = get_audit_service()
    source_ip = client_ip(request)
    user_agent = request.headers.get("user-agent", "")

    if not body.username or not body.password:
        audit.log(None, "login_fail", source_ip, user_agent)
        raise HTTPException(status_code=400, detail="Username and password required")

    # Per-IP limit: blocks a single source outright (that's the attacker's OWN
    # IP, so it self-throttles and can't be used to lock out a victim). Safe to
    # enforce before verifying the password.
    if audit.count_failed_attempts(source_ip) >= 5:
        audit.log(body.username or None, "rate_limit_block", source_ip, user_agent)
        raise HTTPException(status_code=429, detail="Too many login attempts. Try again later.")

    # Always run a bcrypt verify — even when the user doesn't exist — so the
    # response time doesn't reveal whether a username is valid (timing oracle).
    user = get_user_by_username(body.username)
    candidate_hash = user.password_hash if user is not None else _dummy_hash()
    password_ok = verify_password(body.password, candidate_hash)
    if user is None or not password_ok:
        # Wrong credentials. The cross-IP per-username lockout is applied ONLY on
        # this failure path — never on a correct password — so a flood of failed
        # attempts for 'admin' throttles guessing but can't lock out the real
        # owner, who supplies the right password and skips this branch entirely.
        audit.log(body.username, "login_fail", source_ip, user_agent)
        if audit.count_failed_attempts_by_username(body.username) >= 10:
            audit.log(body.username or None, "rate_limit_block", source_ip, user_agent)
            raise HTTPException(status_code=429, detail="Too many login attempts. Try again later.")
        raise HTTPException(status_code=401, detail="Invalid username or password")

    access = create_access_token(user.username, user.role, JWT_SECRET, JWT_ACCESS_EXPIRES)
    refresh, _ = create_refresh_token(user.username, JWT_SECRET, JWT_REFRESH_EXPIRES)
    _set_token_cookies(response, access, refresh)
    audit.log(user.username, "login_ok", source_ip, user_agent)
    return {"user": user.to_dict()}


@router.post("/refresh")
def refresh_token(request: Request, response: Response,
                  refresh_token_cookie: Optional[str] = Cookie(None, alias="refresh_token")):
    audit = get_audit_service()
    if not refresh_token_cookie:
        raise HTTPException(status_code=401, detail="No refresh token")

    payload = decode_token(refresh_token_cookie, JWT_SECRET)
    if payload is None or payload.get("type") != "refresh":
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    jti = payload.get("jti")
    _purge_expired_jtis()

    username = payload.get("sub")
    user = get_user_by_username(username)
    if user is None:
        raise HTTPException(status_code=401, detail="User not found")

    # A role/password change bumps the user's tokens_valid_after watermark. A
    # refresh token minted before that watermark must be rejected here too —
    # otherwise a password reset only kills the ~15-min access token (checked in
    # the middleware) while a stolen refresh token keeps minting new ones for up
    # to 7 days. Checked before revoking so a rejected refresh doesn't consume it.
    if not token_subject_active(username, payload.get("iat", 0)):
        raise HTTPException(status_code=401, detail="Refresh token no longer valid")

    # Atomically claim the old refresh token. If the claim fails the token was
    # already used/revoked — reject. This both rotates the token and closes the
    # double-use race in one step. A transient DB error raises before issuing new
    # tokens, so the user's token isn't silently consumed.
    if not _revoke_jti(jti, payload.get("exp", 0)):
        raise HTTPException(status_code=401, detail="Refresh token has been revoked")
    access = create_access_token(user.username, user.role, JWT_SECRET, JWT_ACCESS_EXPIRES)
    new_refresh, new_jti = create_refresh_token(user.username, JWT_SECRET, JWT_REFRESH_EXPIRES)
    _set_token_cookies(response, access, new_refresh)
    source_ip = client_ip(request)
    audit.log(user.username, "token_refresh", source_ip, request.headers.get("user-agent", ""))
    return {"user": user.to_dict()}


@router.post("/logout")
def logout(request: Request, response: Response,
           refresh_token_cookie: Optional[str] = Cookie(None, alias="refresh_token")):
    audit = get_audit_service()
    if refresh_token_cookie:
        payload = decode_token(refresh_token_cookie, JWT_SECRET)
        if payload and payload.get("jti"):
            _revoke_jti(payload["jti"], payload.get("exp", 0))
        if payload and payload.get("sub"):
            source_ip = client_ip(request)
            audit.log(payload["sub"], "logout", source_ip, request.headers.get("user-agent", ""))

    # Match the attributes the cookies were set with (secure/samesite/httponly),
    # otherwise the browser won't match the jar entry and the deletion is ignored,
    # leaving the token alive until it expires.
    response.delete_cookie("access_token", path="/",
                           httponly=True, secure=COOKIE_SECURE, samesite="strict")
    response.delete_cookie("refresh_token", path="/api/auth",
                           httponly=True, secure=COOKIE_SECURE, samesite="strict")
    return {"status": "ok"}


@router.get("/me")
def me(request: Request,
       access_token_cookie: Optional[str] = Cookie(None, alias="access_token")):
    if not access_token_cookie:
        raise HTTPException(status_code=401, detail="Not authenticated")
    result = get_user_from_token(access_token_cookie, JWT_SECRET)
    if result is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    username, role = result
    user = get_user_by_username(username)
    if user is None:
        raise HTTPException(status_code=401, detail="User not found")
    return {"user": user.to_dict()}
