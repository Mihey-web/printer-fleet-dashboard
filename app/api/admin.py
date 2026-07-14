from fastapi import APIRouter, Request, HTTPException, Cookie, Query
from pydantic import BaseModel
from typing import List, Optional
import sqlite3
import time

from app.domain.user_models import UserRole
from app.services.auth_service import hash_password
from app.services.audit_service import get_audit_service
from app.services import printer_registry
from app.api.auth import (
    get_user_by_username, get_user_by_id, get_all_users,
    USERS_DB, client_ip,
)

router = APIRouter(prefix="/api/admin", tags=["admin"])

# NOTE: authentication and the admin-role check for every /api/admin/* route are
# enforced once, centrally, by JWTAuthMiddleware (app/api/main.py). It rejects
# unauthenticated (401) and non-admin (403) requests before any handler runs and
# sets request.state.username / request.state.role. Handlers below trust that.


class CreateUserRequest(BaseModel):
    username: str
    password: str
    role: str


class UpdateUserRequest(BaseModel):
    role: Optional[str] = None
    password: Optional[str] = None


@router.get("/users")
def list_users(request: Request,
               access_token_cookie: Optional[str] = Cookie(None, alias="access_token")):
    users = [u.to_dict() for u in get_all_users()]
    # Last successful login per user from the audit log (SQLite picks the bare
    # columns from the MAX(created_at) row — documented aggregate behaviour).
    conn = sqlite3.connect(USERS_DB, timeout=10)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT user_id, ip_address, user_agent, MAX(created_at) AS created_at
            FROM audit_log WHERE action = 'login_ok' GROUP BY user_id
        """).fetchall()
    finally:
        conn.close()
    last = {r["user_id"]: {"created_at": r["created_at"], "ip_address": r["ip_address"],
                           "user_agent": r["user_agent"]} for r in rows}
    for u in users:
        u["last_login"] = last.get(u["username"])
    return users


@router.post("/users")
def create_user(body: CreateUserRequest, request: Request,
                access_token_cookie: Optional[str] = Cookie(None, alias="access_token")):
    admin_username = request.state.username
    if not body.username or len(body.username) < 2:
        raise HTTPException(status_code=400, detail="Username must be at least 2 characters")
    if not body.password or len(body.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    if body.role not in (UserRole.ADMIN.value, UserRole.VIEWER.value):
        raise HTTPException(status_code=400, detail="Role must be 'admin' or 'viewer'")

    existing = get_user_by_username(body.username)
    if existing is not None:
        raise HTTPException(status_code=409, detail="Username already exists")

    now = time.time()
    conn = sqlite3.connect(USERS_DB, timeout=10)
    conn.execute("""
        INSERT INTO users (username, password_hash, role, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
    """, (body.username, hash_password(body.password), body.role, now, now))
    conn.commit()
    conn.close()

    new_user = get_user_by_username(body.username)
    source_ip = client_ip(request)
    audit = get_audit_service()
    audit.log(admin_username, "user_create", source_ip, request.headers.get("user-agent", ""))
    return new_user.to_dict()


@router.put("/users/{user_id}")
def update_user(user_id: int, body: UpdateUserRequest, request: Request,
                access_token_cookie: Optional[str] = Cookie(None, alias="access_token")):
    admin_username = request.state.username
    user = get_user_by_id(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    # Self-demotion would invalidate the admin's own tokens mid-session (and could
    # leave the panel without admins) — same rule as self-delete.
    if body.role is not None and user.username == admin_username and body.role != user.role.value:
        raise HTTPException(status_code=400, detail="Cannot change your own role")

    now = time.time()
    conn = sqlite3.connect(USERS_DB, timeout=10)

    if body.role is not None:
        if body.role not in (UserRole.ADMIN.value, UserRole.VIEWER.value):
            conn.close()
            raise HTTPException(status_code=400, detail="Role must be 'admin' or 'viewer'")
        # Bump tokens_valid_after so the role baked into any live access token is
        # invalidated immediately (a demoted admin must not keep admin rights for
        # the remainder of the token's TTL).
        conn.execute("UPDATE users SET role = ?, updated_at = ?, tokens_valid_after = ? WHERE id = ?",
                     (body.role, now, now, user_id))

    if body.password is not None:
        if len(body.password) < 8:
            conn.close()
            raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
        # A password reset must terminate existing sessions.
        conn.execute("UPDATE users SET password_hash = ?, updated_at = ?, tokens_valid_after = ? WHERE id = ?",
                     (hash_password(body.password), now, now, user_id))

    conn.commit()
    conn.close()

    source_ip = client_ip(request)
    audit = get_audit_service()
    audit.log(admin_username, "user_update", source_ip, request.headers.get("user-agent", ""))
    return get_user_by_id(user_id).to_dict()


@router.delete("/users/{user_id}")
def delete_user(user_id: int, request: Request,
                access_token_cookie: Optional[str] = Cookie(None, alias="access_token")):
    admin_username = request.state.username
    user = get_user_by_id(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    if user.username == admin_username:
        raise HTTPException(status_code=400, detail="Cannot delete your own account")

    # Removing the row revokes the user's still-valid access tokens: the auth
    # middleware re-checks the subject against users.db on every request
    # (token_subject_active), so a deleted user is rejected on their next call
    # instead of lingering until the access token expires.
    conn = sqlite3.connect(USERS_DB, timeout=10)
    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()

    source_ip = client_ip(request)
    audit = get_audit_service()
    audit.log(admin_username, "user_delete", source_ip, request.headers.get("user-agent", ""))
    return {"status": "ok"}


# --- Printer registry CRUD ---------------------------------------------------
# Edits accumulate in the printers table; the poll loop keeps running on the
# startup snapshot until "apply" restarts the service (systemd Restart=on-failure).

class PrinterCreateRequest(BaseModel):
    kind: str
    label: str
    host: str
    port: Optional[int] = None
    model: Optional[str] = None
    access_code: Optional[str] = None
    serial: Optional[str] = None


class PrinterUpdateRequest(BaseModel):
    label: Optional[str] = None
    host: Optional[str] = None
    port: Optional[int] = None
    model: Optional[str] = None
    access_code: Optional[str] = None
    serial: Optional[str] = None


def _validate_printer_fields(kind: str, data: dict, creating: bool):
    if creating and kind not in printer_registry.KINDS:
        raise HTTPException(status_code=400, detail="kind must be one of: " + ", ".join(printer_registry.KINDS))
    for f in ("label", "host"):
        if (creating or data.get(f) is not None) and not str(data.get(f) or "").strip():
            raise HTTPException(status_code=400, detail=f"{f} must not be empty")
    port = data.get("port")
    if port is not None and not (1 <= port <= 65535):
        raise HTTPException(status_code=400, detail="port must be 1-65535")
    if kind == "bambu":
        for f in ("access_code", "serial"):
            if (creating or data.get(f) is not None) and not str(data.get(f) or "").strip():
                raise HTTPException(status_code=400, detail=f"{f} is required for Bambu printers")


def _audit(request: Request, action: str):
    get_audit_service().log(request.state.username, action, client_ip(request),
                            request.headers.get("user-agent", ""))


def _printer_with_pending(row: dict) -> dict:
    row = dict(row)
    row["pending"] = printer_registry.pending_state(row)
    return row


@router.get("/printers")
def list_printers_cfg(request: Request,
                      access_token_cookie: Optional[str] = Cookie(None, alias="access_token")):
    # Access codes are returned as-is: this endpoint is admin-only (middleware)
    # and the UI masks them behind an eye toggle.
    return [_printer_with_pending(r) for r in printer_registry.list_printers()]


@router.post("/printers")
def create_printer_cfg(body: PrinterCreateRequest, request: Request,
                       access_token_cookie: Optional[str] = Cookie(None, alias="access_token")):
    data = body.dict()
    _validate_printer_fields(body.kind, data, creating=True)
    data["label"] = data["label"].strip()
    data["host"] = data["host"].strip()
    row = printer_registry.create_printer(data)
    _audit(request, "printer_create")
    return _printer_with_pending(row)


@router.put("/printers/{printer_id}")
def update_printer_cfg(printer_id: str, body: PrinterUpdateRequest, request: Request,
                       access_token_cookie: Optional[str] = Cookie(None, alias="access_token")):
    existing = printer_registry.get_printer(printer_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Printer not found")
    data = {k: v for k, v in body.dict().items() if v is not None}
    _validate_printer_fields(existing["kind"], data, creating=False)
    if "label" in data:
        data["label"] = data["label"].strip()
    if "host" in data:
        data["host"] = data["host"].strip()
    row = printer_registry.update_printer(printer_id, data)
    _audit(request, "printer_update")
    return _printer_with_pending(row)


@router.delete("/printers/{printer_id}")
def delete_printer_cfg(printer_id: str, request: Request,
                       access_token_cookie: Optional[str] = Cookie(None, alias="access_token")):
    mode = printer_registry.delete_printer(printer_id)
    if mode is None:
        raise HTTPException(status_code=404, detail="Printer not found")
    _audit(request, "printer_delete")
    return {"status": "ok", "mode": mode}


@router.post("/printers/{printer_id}/restore")
def restore_printer_cfg(printer_id: str, request: Request,
                        access_token_cookie: Optional[str] = Cookie(None, alias="access_token")):
    row = printer_registry.restore_printer(printer_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Printer not found")
    _audit(request, "printer_restore")
    return _printer_with_pending(row)


@router.post("/printers/discard")
def discard_printer_changes(request: Request,
                            access_token_cookie: Optional[str] = Cookie(None, alias="access_token")):
    restored = printer_registry.discard_changes()
    _audit(request, "printers_discard")
    return {"status": "ok", "restored": restored}


@router.post("/printers/apply")
def apply_printer_changes(request: Request,
                          access_token_cookie: Optional[str] = Cookie(None, alias="access_token")):
    _audit(request, "printers_apply")
    # Exit non-zero AFTER the response is flushed; systemd (Restart=on-failure,
    # RestartSec=5) brings the service back up on the new configuration. The
    # history DB is WAL + synchronous=NORMAL, so a hard exit is crash-safe.
    import os
    import threading
    threading.Timer(0.7, os._exit, args=(1,)).start()
    return {"status": "restarting"}


@router.get("/audit")
def get_audit_log(request: Request,
                  access_token_cookie: Optional[str] = Cookie(None, alias="access_token"),
                  limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
                  user: Optional[str] = None, action: Optional[str] = None):
    audit = get_audit_service()
    entries = audit.get_logs(limit=limit + 1, offset=offset, user_id=user, action=action)
    has_more = len(entries) > limit
    if has_more:
        entries = entries[:limit]
    return {"rows": [e.to_dict() for e in entries], "has_more": has_more}


# --- Server settings (Telegram bot, proxies) -------------------------------
# Stored in the settings table; telegram_manager hot-applies edits, so no
# service restart is involved (unlike the printer registry).

def _validate_settings(values: dict):
    def _int_in(key, lo, hi):
        v = values.get(key)
        if v is None:
            return
        if not isinstance(v, int) or isinstance(v, bool) or not (lo <= v <= hi):
            raise HTTPException(status_code=400, detail=f"{key} must be an integer {lo}-{hi}")

    _int_in("telegram_update_interval", 1, 3600)
    _int_in("proxy_check_interval", 30, 86400)

    if "telegram_chat_id" in values:
        v = values["telegram_chat_id"]
        if v is not None and (not isinstance(v, int) or isinstance(v, bool)):
            raise HTTPException(status_code=400, detail="telegram_chat_id must be an integer or null")

    for key in ("telegram_enabled", "telegram_notify_on_finish",
                "telegram_notify_on_error", "telegram_notify_on_paused"):
        if key in values and not isinstance(values[key], bool):
            raise HTTPException(status_code=400, detail=f"{key} must be a boolean")

    for key in ("telegram_finish_template", "telegram_error_template", "telegram_paused_template"):
        if key in values:
            v = values[key]
            if not isinstance(v, str) or not v.strip():
                raise HTTPException(status_code=400, detail=f"{key} must be a non-empty string")
            try:
                v.format(label="X")
            except (KeyError, IndexError, ValueError):
                raise HTTPException(status_code=400, detail=f"{key}: в шаблоне доступен только {{label}}")

    if "telegram_token" in values and not isinstance(values["telegram_token"], str):
        raise HTTPException(status_code=400, detail="telegram_token must be a string")

    if "proxy_list" in values:
        from urllib.parse import urlparse
        v = values["proxy_list"]
        if not isinstance(v, list) or not all(isinstance(p, str) for p in v):
            raise HTTPException(status_code=400, detail="proxy_list must be a list of URLs")
        if len(v) != len(set(v)):
            raise HTTPException(status_code=400, detail="proxy_list contains duplicates")
        for p in v:
            u = urlparse(p)
            if u.scheme not in ("http", "https") or not u.hostname or not u.port:
                raise HTTPException(status_code=400,
                                    detail=f"Некорректный прокси-URL (нужно http(s)://[логин:пароль@]хост:порт): {u.hostname or p}")


@router.get("/settings")
def get_settings(request: Request,
                 access_token_cookie: Optional[str] = Cookie(None, alias="access_token")):
    from app.services import settings_service, telegram_manager
    # The raw token/proxy credentials go out as-is: this endpoint is admin-only
    # and the settings page needs them for the reveal-eye editing UX.
    return {"values": settings_service.get_all(), "telegram": telegram_manager.status()}


@router.put("/settings")
def update_settings(body: dict, request: Request,
                    access_token_cookie: Optional[str] = Cookie(None, alias="access_token")):
    from app.services import settings_service, telegram_manager
    if not isinstance(body, dict) or not body:
        raise HTTPException(status_code=400, detail="Expected a non-empty settings object")
    unknown = [k for k in body if k not in settings_service.DEFAULTS]
    if unknown:
        raise HTTPException(status_code=400, detail="Unknown settings keys: " + ", ".join(unknown))
    current = settings_service.get_all()
    changed = {k: v for k, v in body.items() if current.get(k) != v}
    _validate_settings(changed)
    if changed:
        settings_service.set_many(changed)
        applied = telegram_manager.apply_settings(changed.keys())
        # Audit only WHICH keys changed — values would leak the bot token and
        # proxy credentials into the audit log.
        _audit(request, "settings_update")
    else:
        applied = {"bot_restarting": False}
    return {"values": settings_service.get_all(), "telegram": telegram_manager.status(),
            "changed": sorted(changed.keys()), **applied}


@router.post("/settings/proxy-check")
def proxy_check_now(request: Request,
                    access_token_cookie: Optional[str] = Cookie(None, alias="access_token")):
    from app.services import telegram_manager
    telegram_manager.check_proxies_now()  # blocking, ≤~10s (parallel checks)
    return {"telegram": telegram_manager.status()}


class PrinterCommandRequest(BaseModel):
    action: str
    temp: Optional[int] = None
    hours: Optional[int] = None
    level: Optional[int] = None
    part: Optional[int] = None
    aux: Optional[int] = None
    chamber: Optional[int] = None
    nozzle: Optional[int] = None
    bed: Optional[int] = None
    slot: Optional[int] = None
    obj_list: Optional[List[int]] = None


@router.post("/printers/{printer_id}/command")
def printer_command(printer_id: str, body: PrinterCommandRequest, request: Request,
                    access_token_cookie: Optional[str] = Cookie(None, alias="access_token")):
    from app.services import printer_commands
    params = {k: v for k, v in body.__dict__.items() if k != "action" and v is not None}
    r = printer_commands.send(printer_id, body.action, params)
    _audit(request, "printer_command:%s:%s" % (body.action, printer_id))
    if not r["success"]:
        raise HTTPException(status_code=400, detail=r["detail"])
    return r
