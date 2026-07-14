from fastapi import FastAPI, HTTPException, Response, Request, Cookie, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
import sqlite3
import os
import time as time_module
import logging
import threading

from starlette.middleware.base import BaseHTTPMiddleware

from app.services.state_store import StateStore

logger = logging.getLogger(__name__)


# Everything else — including /static/ and the app shell — is auth-gated so an
# anonymous visitor can't learn anything about what this dashboard monitors.
# Prefix matches (whole subtree is public — every /api/auth/* route).
PUBLIC_PREFIXES = ('/api/auth/',)
# Exact matches only — must NOT accidentally cover a future /api/health-*
# route that could carry real data. Compared with ==, not startswith.
PUBLIC_EXACT = ('/api/health', '/favicon.ico')


def _resolve_app_version():
    """Snapshot the deployed git commit once at startup for /api/version."""
    import subprocess
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    try:
        out = subprocess.run(['git', 'log', '-1', '--format=%h %cs'],
                             cwd=repo_root, capture_output=True, text=True, timeout=5)
        line = out.stdout.strip()
        if out.returncode == 0 and ' ' in line:
            commit, date = line.split(' ', 1)
            return {'commit': commit, 'date': date}
    except Exception:
        pass
    return {'commit': 'dev', 'date': ''}

# states-summary runs a full LEAD() window scan over printer_history; on the
# Pi this grows linearly with DB size. The aggregation only changes as new
# snapshots land (every few seconds) and dashboards poll it far more often, so
# memoize the per-window SQL result for a short TTL. Live current_state is NOT
# cached here — it is overlaid fresh on every request.
_STATES_SUMMARY_TTL = 10.0  # seconds
_states_summary_cache = {}   # key -> (monotonic_expiry, printers_dict)
_states_summary_lock = threading.Lock()


def _history_db_path():
    """Absolute path to the printer_history SQLite DB (project root)."""
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        "printer_history.db",
    )


def _current_labels():
    """Map printer_id -> current display name from the registry.

    History rows (and the collector's own status.label) store the name
    denormalised at snapshot/startup time, so a rename leaves stale names in
    events, timeline, summary and even live cards until a restart. The registry
    table is the single source of truth for display names — resolve it live by
    id at read time and let it win everywhere. Falls back to the row's own
    stored label for ids no longer in the registry (e.g. hard-deleted printers).

    include_deleted=True so soft-deleted printers still resolve to their name in
    historical views.
    """
    try:
        from app.services import printer_registry
        return {p["id"]: p["label"]
                for p in printer_registry.list_printers(include_deleted=True)}
    except Exception:
        logger.debug("current labels lookup failed", exc_info=True)
        return {}


_EVENT_STATES = {"printing", "idle", "paused", "finished", "error", "offline", "unknown"}


def _query_events(db, fr, to, limit, offset, printer_id=None, state=None):
    """State-change events in [fr, to], newest first, one page of `limit`.

    Fetches limit+1 rows so the caller can report `has_more` without a second
    full window-function scan (the old COUNT(*) variant doubled the work).

    printer_id narrows the inner scan (safe: LAG partitions by printer_id);
    state must filter the outer query — it applies to new_state, which only
    exists after LAG is computed.
    """
    inner_where = "recorded_at >= ? AND recorded_at <= ?"
    params = [fr, to]
    if printer_id:
        inner_where += " AND printer_id = ?"
        params.append(printer_id)
    outer_where = "prev_state IS NOT NULL AND prev_state != state"
    if state:
        outer_where += " AND state = ?"
        params.append(state)
    params.extend([limit + 1, offset])
    conn = sqlite3.connect(db, timeout=10)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            WITH ordered AS (
                SELECT printer_id, label, state, job_name, last_error, recorded_at,
                       LAG(state) OVER (PARTITION BY printer_id ORDER BY recorded_at) AS prev_state
                FROM printer_history
                WHERE %s
            )
            SELECT printer_id, label, prev_state AS old_state, state AS new_state,
                   job_name, last_error, recorded_at AS time
            FROM ordered
            WHERE %s
            ORDER BY recorded_at DESC
            LIMIT ? OFFSET ?
        """ % (inner_where, outer_where), params).fetchall()
    finally:
        conn.close()
    has_more = len(rows) > limit
    labels = _current_labels()
    out = []
    for r in rows[:limit]:
        d = dict(r)
        d["label"] = labels.get(d["printer_id"], d["label"])
        out.append(d)
    return {"rows": out, "has_more": has_more}


def _query_state_durations(db, fr, to):
    """Per-printer time spent in each state across [fr, to].

    Returns {printer_id: {"printer_id", "label", "states": {state: secs}}}.
    Expensive: a full LEAD() window scan over printer_history. Memoized by
    _cached_state_durations.
    """
    conn = sqlite3.connect(db, timeout=10)
    try:
        conn.row_factory = sqlite3.Row
        # Группировка — по printer_id, а НЕ по (printer_id, label): иначе после
        # переименования один принтер расщепляется на две строки (старое и новое
        # имя), а их длительности затирают друг друга. MAX(label) — только запас
        # на случай id, которого уже нет в реестре; актуальное имя накладывается
        # из _current_labels() в эндпоинте.
        rows = conn.execute("""
            WITH intervals AS (
                SELECT printer_id, label, state, recorded_at,
                       LEAD(recorded_at, 1, ?) OVER (
                           PARTITION BY printer_id ORDER BY recorded_at
                       ) AS next_ts
                FROM printer_history
                WHERE recorded_at >= ? AND recorded_at <= ?
            )
            SELECT printer_id, MAX(label) AS label, state,
                   CAST(SUM(CASE WHEN next_ts > recorded_at THEN next_ts - recorded_at ELSE 0 END) AS INTEGER) AS duration_sec
            FROM intervals
            GROUP BY printer_id, state
            ORDER BY printer_id, duration_sec DESC
        """, (to, fr, to)).fetchall()
    finally:
        conn.close()

    printers = {}
    for r in rows:
        pid = r["printer_id"]
        if pid not in printers:
            printers[pid] = {"printer_id": pid, "label": r["label"], "states": {}}
        printers[pid]["states"][r["state"]] = r["duration_sec"]
    return printers


def _cached_state_durations(db, fr, to):
    """_query_state_durations memoized per (fr, to) window for a short TTL."""
    cache_key = (round(fr, 3), round(to, 3))
    now = time_module.monotonic()
    with _states_summary_lock:
        entry = _states_summary_cache.get(cache_key)
        if entry is not None and entry[0] > now:
            return entry[1]

    printers = _query_state_durations(db, fr, to)

    with _states_summary_lock:
        _states_summary_cache[cache_key] = (now + _STATES_SUMMARY_TTL, printers)
        # Bound memory: drop entries that have already expired.
        for k in [k for k, v in _states_summary_cache.items() if v[0] <= now]:
            del _states_summary_cache[k]
    return printers


class JWTAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        public = path in PUBLIC_EXACT or any(path.startswith(p) for p in PUBLIC_PREFIXES)
        # Validate the cookie for every request — the '/' route needs the result
        # to choose between the app shell and the anonymous login page.
        token = request.cookies.get('access_token')
        username = role = None
        if token:
            import config as cfg
            secret = getattr(cfg, 'JWT_SECRET', '')
            if not secret:
                if not public and path != '/':
                    return JSONResponse(status_code=500, content={'detail': 'Server not configured'})
            else:
                from app.services.auth_service import get_user_from_token, decode_token
                from app.api.auth import token_subject_active
                result = get_user_from_token(token, secret)
                if result is not None:
                    # Stateless access tokens are otherwise valid until they expire
                    # (~15 min), so a deleted user — or one whose role/password just
                    # changed — would keep access. Re-validate the subject against
                    # the DB: reject if the user no longer exists or the token
                    # predates their tokens_valid_after watermark.
                    payload = decode_token(token, secret)
                    iat = payload.get('iat', 0) if payload else 0
                    if token_subject_active(result[0], iat):
                        username, role = result
        if username is not None:
            request.state.username = username
            request.state.role = role
        if public or path == '/':
            return await call_next(request)
        if username is None:
            if path.startswith('/api/'):
                return JSONResponse(status_code=401, content={'detail': 'Not authenticated'})
            # Static assets (and anything else) stay hidden from anonymous visitors.
            return Response(status_code=401)
        if path.startswith('/api/admin/') and role.value != 'admin':
            return JSONResponse(status_code=403, content={'detail': 'Admin access required'})
        return await call_next(request)


def create_app(store: StateStore) -> FastAPI:
    # Disable the interactive docs in production — they sit outside the JWT
    # middleware's /api gate and would otherwise be publicly reachable.
    app = FastAPI(title="Printer Dashboard", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get('/api/health')
    def health():
        # Public (uptime checks) — must not leak anything about the fleet.
        return {"status": "ok"}

    @app.get('/api/printers')
    def printers():
        from app.services import printer_commands
        labels = _current_labels()
        rows = []
        for s in store.get_all():
            d = s.to_dict()
            # debug-блоб (raw_state, HMS-коды, сырые атрибуты AMS) закрыт до
            # admin-only /api/debug/printers/{id}; в общий листинг не отдаём —
            # фронт его не использует, а viewer-роль не должна его видеть.
            d.pop("debug", None)
            # Имя — из реестра (актуальное), а не из снимка коллектора: так
            # переименование видно на карточках и в браузерных уведомлениях
            # (фронт шлёт p.label) сразу, без ожидания рестарта.
            d["label"] = labels.get(d["id"], d["label"])
            if d.get("kind") == "bambu":
                # True/False/None: принимает ли прошивка print-класс команд
                # (пауза/стоп/сушка) — фронт прячет кнопки при False
                d["print_cmds"] = printer_commands.get_capability(d["id"])
            rows.append(d)
        return rows

    @app.get('/api/printers/{printer_id}')
    def printer(printer_id: str):
        item = store.get_one(printer_id)
        if item is None:
            raise HTTPException(status_code=404, detail='Printer not found')
        d = item.to_dict()
        d.pop("debug", None)  # см. /api/printers: debug только через admin-эндпоинт
        d["label"] = _current_labels().get(d["id"], d["label"])
        return d

    @app.get('/api/debug/printers/{printer_id}')
    def debug_printer(printer_id: str, request: Request):
        # The debug blob exposes internal device state (AMS, HMS error codes, raw
        # attrs) — restrict it to admins, like /command. Viewers may not read it.
        role = getattr(request.state, 'role', None)
        if role is None or getattr(role, 'value', None) != 'admin':
            raise HTTPException(status_code=403, detail='Admin role required')
        item = store.get_one(printer_id)
        if item is None:
            raise HTTPException(status_code=404, detail='Printer not found')
        return {'id': item.id, 'label': _current_labels().get(item.id, item.label),
                'state': item.state.value, 'online': item.online, 'debug': item.debug}

    @app.get('/api/cameras')
    def cameras_list():
        # Camera streaming was removed (it was already disabled via config and the
        # feature is unused). The full implementation is preserved on the
        # archive/camera branch. This stub keeps the frontend's camera view cleanly
        # empty instead of 404-ing.
        return {'ffmpeg_available': False, 'cuda': False, 'cameras': []}

    @app.get("/api/history/timeline")
    def get_timeline(fr: float, to: float = None, printer: str = None,
                     limit: int = Query(1000, ge=1, le=50000),
                     offset: int = Query(0, ge=0), desc: bool = False):
        if to is None:
            to = time_module.time()
        if fr > to:
            raise HTTPException(status_code=400, detail="fr must be <= to")
        db = _history_db_path()
        conn = sqlite3.connect(db, timeout=10)
        try:
            conn.row_factory = sqlite3.Row
            # ORDER BY direction is chosen from a bool into a fixed literal — never
            # interpolate user input into SQL text.
            order_clause = "ORDER BY recorded_at DESC" if desc else "ORDER BY recorded_at ASC"

            if printer:
                count_row = conn.execute("""
                    SELECT COUNT(*) AS cnt FROM printer_history
                    WHERE printer_id = ? AND recorded_at >= ? AND recorded_at <= ?
                """, (printer, fr, to)).fetchone()
                rows = conn.execute(
                    "SELECT printer_id, label, state, recorded_at, job_name, "
                    "progress, eta_sec, print_time, nozzle_temp, bed_temp "
                    "FROM printer_history "
                    "WHERE printer_id = ? AND recorded_at >= ? AND recorded_at <= ? "
                    + order_clause + " LIMIT ? OFFSET ?",
                    (printer, fr, to, limit, offset)).fetchall()
            else:
                count_row = conn.execute("""
                    SELECT COUNT(*) AS cnt FROM printer_history
                    WHERE recorded_at >= ? AND recorded_at <= ?
                """, (fr, to)).fetchone()
                rows = conn.execute(
                    "SELECT printer_id, label, state, recorded_at, job_name, "
                    "progress, eta_sec, print_time, nozzle_temp, bed_temp "
                    "FROM printer_history "
                    "WHERE recorded_at >= ? AND recorded_at <= ? "
                    + order_clause + " LIMIT ? OFFSET ?",
                    (fr, to, limit, offset)).fetchall()
        finally:
            conn.close()
        total = count_row["cnt"]
        labels = _current_labels()
        out = []
        for r in rows:
            d = dict(r)
            d["label"] = labels.get(d["printer_id"], d["label"])
            out.append(d)
        return {
            "rows": out,
            "has_more": (offset + limit) < total,
            "total": total
        }

    @app.get("/api/history/events")
    def get_events(fr: float, to: float = None,
                   limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0),
                   printer_id: str = None, state: str = None):
        if to is None:
            to = time_module.time()
        if fr > to:
            raise HTTPException(status_code=400, detail="fr must be <= to")
        if state is not None and state not in _EVENT_STATES:
            raise HTTPException(status_code=400, detail="unknown state")
        return _query_events(_history_db_path(), fr, to, limit, offset,
                             printer_id=printer_id, state=state)

    @app.get("/api/history/states-summary")
    def get_states_summary(fr: float, to: float = None):
        if to is None:
            to = time_module.time()
        if fr > to:
            raise HTTPException(status_code=400, detail="fr must be <= to")

        printers = _cached_state_durations(_history_db_path(), fr, to)

        # current_state is always live, never cached.
        current_states = {}
        for s in store.get_all():
            current_states[s.id] = s.state.value

        # Имя — из реестра, свежее (durations кэшируются на TTL, имя нет).
        labels = _current_labels()
        result = []
        for pid, data in printers.items():
            resp = {"states": {}}
            for s in ("printing", "idle", "paused", "error", "finished", "offline", "unknown"):
                if data["states"].get(s, 0) > 0:
                    resp["states"][s] = data["states"][s]
            if resp["states"]:
                resp["printer_id"] = pid
                resp["label"] = labels.get(pid, data["label"])
                resp["current_state"] = current_states.get(pid, "unknown")
                result.append(resp)
        return result

    app.mount('/static', StaticFiles(directory='app/web'), name='static')

    app_version = _resolve_app_version()

    @app.get('/api/version')
    def version():
        return app_version

    @app.get('/')
    def index(request: Request):
        # Anonymous visitors get a self-contained neutral login page; the app
        # shell (and everything it reveals) is only served to a valid session.
        authed = getattr(request.state, 'username', None) is not None
        page = 'app/web/index.html' if authed else 'app/web/login.html'
        return FileResponse(page, headers={'Cache-Control': 'no-store'})

    from app.api.auth import init_users_db, init_auth, get_or_create_jwt_secret
    from app.services.auth_service import hash_password
    import config as cfg

    # Create the auth DB (incl. the app_secrets table) BEFORE resolving the secret,
    # since the generated-and-persisted path needs that table.
    init_users_db()

    # JWT secret resolution: env FORGE_JWT_SECRET → secret stored in users.db →
    # freshly generated + persisted. No secret literal lives in the source tree.
    # Publish the resolved value back onto the config module so JWTAuthMiddleware
    # (which reads config.JWT_SECRET per request) verifies against the same key.
    jwt_secret = os.environ.get('FORGE_JWT_SECRET') or get_or_create_jwt_secret()
    cfg.JWT_SECRET = jwt_secret
    init_auth(
        jwt_secret,
        access_expires=getattr(cfg, 'JWT_ACCESS_EXPIRES', 15 * 60),
        refresh_expires=getattr(cfg, 'JWT_REFRESH_EXPIRES', 7 * 24 * 3600),
        cookie_secure=getattr(cfg, 'COOKIE_SECURE', True),
        trusted_proxies=getattr(cfg, 'TRUSTED_PROXIES', None),
    )

    conn = sqlite3.connect(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "users.db"), timeout=10)
    row = conn.execute("SELECT COUNT(*) as cnt FROM users").fetchone()
    if row and row[0] == 0:
        # First-run admin bootstrap. Username from FORGE_ADMIN_USERNAME (default
        # 'admin'). Password from FORGE_ADMIN_PASSWORD if set; otherwise a random
        # one is generated and logged ONCE so the app is usable out of the box
        # without shipping any default credentials. Only the bcrypt hash is stored.
        import secrets as _secrets
        admin_user = getattr(cfg, 'ADMIN_USERNAME', None) or 'admin'
        admin_pass = getattr(cfg, 'ADMIN_PASSWORD', None)
        generated = not admin_pass
        if generated:
            admin_pass = _secrets.token_urlsafe(18)
        now = time_module.time()
        conn.execute(
            "INSERT INTO users (username, password_hash, role, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (admin_user, hash_password(admin_pass), 'admin', now, now),
        )
        conn.commit()
        log = logging.getLogger(__name__)
        if generated:
            log.warning(
                "\n============================================================\n"
                " FIRST-RUN ADMIN ACCOUNT CREATED\n"
                "   username: %s\n"
                "   password: %s\n"
                " This password is shown ONCE. Log in and change it now.\n"
                " (Set FORGE_ADMIN_USERNAME / FORGE_ADMIN_PASSWORD to choose\n"
                "  your own credentials before first run.)\n"
                "============================================================",
                admin_user, admin_pass,
            )
        else:
            log.info('Admin user created from FORGE_ADMIN_* env: %s', admin_user)
    conn.close()

    from app.api.auth import router as auth_router
    from app.api.admin import router as admin_router
    app.include_router(auth_router)
    app.include_router(admin_router)
    app.add_middleware(JWTAuthMiddleware)

    return app
# ----------------------------------------------------------------------------
# ---------------------------
