import bcrypt
import jwt
import time
import secrets
from typing import Optional, Tuple

from app.domain.user_models import UserRole


# bcrypt reads at most 72 bytes of a password. bcrypt 4.x cut a longer one
# there silently, 5.0 raises ValueError instead (a 500 at login, QA 17.11 F4).
# Both the hash and the check cut it here exactly as 4.x did, so they behave
# the same on either version and a hash 4.x made of a long password still
# verifies; a new password over the limit is refused where it is set
# (password_too_long, app/api/admin.py).
PASSWORD_MAX_BYTES = 72


def _password_bytes(password: str) -> bytes:
    return password.encode("utf-8")[:PASSWORD_MAX_BYTES]


def password_too_long(password: str) -> bool:
    return len(password.encode("utf-8")) > PASSWORD_MAX_BYTES


def hash_password(password: str) -> str:
    return bcrypt.hashpw(_password_bytes(password), bcrypt.gensalt(rounds=12)).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(_password_bytes(password), password_hash.encode("utf-8"))


def create_access_token(username: str, role: UserRole, secret: str, expires_sec: int) -> str:
    now = time.time()
    payload = {
        "sub": username,
        "role": role.value,
        "type": "access",
        # its own id: «Выйти» revokes this token, not only the refresh one
        # (app/api/auth.py logout, token_subject_active)
        "jti": secrets.token_hex(16),
        "iat": int(now),
        "exp": int(now + expires_sec),
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def create_refresh_token(username: str, secret: str, expires_sec: int) -> Tuple[str, str]:
    now = time.time()
    jti = secrets.token_hex(16)
    payload = {
        "sub": username,
        "type": "refresh",
        "jti": jti,
        "iat": int(now),
        "exp": int(now + expires_sec),
    }
    return jwt.encode(payload, secret, algorithm="HS256"), jti


def decode_token(token: str, secret: str) -> Optional[dict]:
    try:
        return jwt.decode(token, secret, algorithms=["HS256"])
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        return None


def get_user_from_token(token: str, secret: str) -> Optional[Tuple[str, UserRole]]:
    payload = decode_token(token, secret)
    if payload is None or payload.get("type") != "access":
        return None
    username = payload.get("sub")
    role_str = payload.get("role")
    if not username or role_str not in (UserRole.ADMIN.value, UserRole.VIEWER.value):
        return None
    return username, UserRole(role_str)


def generate_secret() -> str:
    return secrets.token_hex(32)
