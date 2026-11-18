"""QA 17.11.2026, part B1 — printer commands, sign-in and users, the API.

The testers' confirming tests (docs/dev/qa-2026-11-17 of the qa/func-2026-11-17 and
qa/hunt-2026-11-17 branches), moved here, plus the controls around each fix:

* F1  a double «Добавить пользователя» left users.db write-locked (every login 500);
* F2 / hunt №3  «Нагрев 1–169 °C» mid-print went out, only exactly 0 was a cooldown;
* F4  a password over bcrypt's 72 bytes: 500 at login and at «Добавить» (bcrypt 5.0);
* F5  integers too big for SQLite in history / AMS / verdict / user addresses: 500;
* hunt №9  «Перечитать метку» passed while a sent load was on its way (and back);
* hunt №24 Creality «Ось Z +30» went out where «Опустить стол» was refused;
* hunt №25 Creality K1 held at 260 °C nozzle;
* hunt №36 /api/history/states-summary took an infinite window;
* hunt №2  /api/history/jobs/stats passes the browser's zone (tz) to job_stats.
No network, no real printer, temporary databases.
"""
import sqlite3
import threading
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from conftest import bambu_threads_clean, make_collector, offline_collector  # noqa: F401
from test_printer_commands import (  # noqa: F401
    CONFIRMED, K1C_IDLE, K1C_PRINTING, REFUSED, SENT, _report, cmd_env, creality, live)

from app.services import printer_commands

COOL = printer_commands.STATE_MSG["cooldown"]


# --- F1: a duplicate «Добавить пользователя» must not lock users.db ------------------------------

@pytest.fixture
def users_db(tmp_path, monkeypatch):
    from app.api import admin, auth
    from app.services import audit_service
    db = str(tmp_path / "users.db")
    for mod in (auth, admin, audit_service):
        monkeypatch.setattr(mod, "USERS_DB", db)
    monkeypatch.setattr(audit_service, "_audit", None)
    auth.init_users_db()
    audit_service.get_audit_service()
    return db


def _request(user="admin"):
    return SimpleNamespace(state=SimpleNamespace(username=user), headers={"user-agent": "qa"},
                           client=SimpleNamespace(host="127.0.0.1"), scope={"client": ("127.0.0.1", 1)})


def _writable(db):
    try:
        c = sqlite3.connect(db, timeout=1)
        c.execute("INSERT INTO audit_log (user_id, action, ip_address, user_agent, created_at)"
                  " VALUES ('qa', 'probe', '', '', 0)")
        c.commit()
        c.close()
        return True
    except sqlite3.OperationalError as e:
        return str(e)


def test_f1_users_db_stays_writable_after_a_duplicate_create_race(users_db, monkeypatch):
    """Both requests pass get_user_by_username, the second INSERT hits UNIQUE: a 409, and the
    failed transaction is rolled back at once (it kept the write lock as long as the exception
    object lived: every login 500 for minutes on the farm)."""
    from app.api import admin
    body = admin.CreateUserRequest(username="dup", password="pass-12345", role="viewer")
    admin.create_user(body, _request())                                    # the first one wins
    monkeypatch.setattr(admin, "get_user_by_username", lambda name: None)  # the second passed the check too
    kept = None
    try:
        admin.create_user(body, _request())
    except Exception as e:                    # uvicorn logs it; anyio / starlette keep it in a cycle
        kept = e
    assert isinstance(kept, HTTPException) and kept.status_code == 409, kept
    assert _writable(users_db) is True        # was 'database is locked'


def test_f1_triple_click_creates_one_user_and_locks_nothing(users_db):
    """Three «Добавить» at once: one user, the others «already exists», users.db writable."""
    from app.api import admin, auth
    body = admin.CreateUserRequest(username="triple", password="pass-12345", role="viewer")
    gate, out = threading.Barrier(3), []

    def click():
        gate.wait()
        try:
            out.append(admin.create_user(body, _request())["username"])
        except HTTPException as e:
            out.append(e.status_code)
    threads = [threading.Thread(target=click) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert sorted(out, key=str) == [409, 409, "triple"]
    assert [u.username for u in auth.get_all_users()] == ["triple"]
    assert _writable(users_db) is True


def test_f1_user_update_and_delete_leave_users_db_writable(users_db):
    from app.api import admin, auth
    uid = admin.create_user(admin.CreateUserRequest(username="v1", password="pass-12345", role="viewer"),
                            _request())["id"]
    with pytest.raises(HTTPException) as e:
        admin.update_user(uid, admin.UpdateUserRequest(role="viewer", password="short"), _request())
    assert e.value.status_code == 400 and auth.get_user_by_id(uid).role.value == "viewer"
    updated = admin.update_user(uid, admin.UpdateUserRequest(role="admin", password="pass-67890"), _request())
    assert updated["role"] == "admin"
    from app.services.auth_service import verify_password
    assert verify_password("pass-67890", auth.get_user_by_username("v1").password_hash)
    assert admin.delete_user(uid, _request()) == {"status": "ok"}
    assert auth.get_user_by_id(uid) is None
    assert _writable(users_db) is True


# --- F2 / hunt №3: «Нагрев» under the working floor mid-print is a cooldown ------------------------

@pytest.mark.parametrize("params", [{"nozzle": 1}, {"bed": 1}, {"nozzle": 220, "bed": 1}])
def test_f2_preheat_to_one_degree_mid_print_is_refused_like_zero(live, params):
    store = live(_report(gcode="RUNNING"))
    out = printer_commands.send("p1", "preheat", params)
    assert out == REFUSED(COOL), (out, store.published)       # was: M104 S1 / M140 S1 went out


@pytest.mark.parametrize("nozzle", [0, 1, 20, 100, 169])
def test_bambu_nozzle_below_extrusion_floor_mid_print_is_a_cooldown(live, nozzle):
    store = live(_report(gcode="RUNNING"))
    r = printer_commands.send("p1", "preheat", {"nozzle": nozzle})
    assert r["status"] == "refused" and r["detail"] == COOL, r
    assert store.published == []


@pytest.mark.parametrize("nozzle", [0, 1, 20, 100, 169])
def test_creality_nozzle_below_extrusion_floor_mid_print_is_a_cooldown(creality, nozzle):
    store = creality(K1C_PRINTING)
    r = printer_commands.send("c1", "preheat", {"nozzle": nozzle})
    assert r["status"] == "refused" and r["detail"] == COOL, r
    assert store.calls == []


@pytest.mark.parametrize("bed", [0, 6, 29])
def test_creality_bed_below_its_floor_mid_print_is_a_cooldown(creality, bed):
    store = creality(K1C_PRINTING)
    assert printer_commands.send("c1", "preheat", {"bed": bed}) == REFUSED(COOL)
    assert store.calls == []


def test_working_temperatures_mid_print_still_go(live, creality):
    store = live(_report(gcode="RUNNING"))
    assert printer_commands.send("p1", "preheat", {"nozzle": 170, "bed": 30}) == CONFIRMED
    assert printer_commands.send("p1", "preheat", {"nozzle": 215, "bed": 55}) == CONFIRMED
    assert [p["print"]["param"] for p in store.published] == ["M104 S170\nM140 S30\n", "M104 S215\nM140 S55\n"]
    cr = creality(K1C_PRINTING)
    assert printer_commands.send("c1", "preheat", {"nozzle": 230, "bed": 60}) == SENT
    assert cr.calls == [("set", [{"nozzleTempControl": 230}, {"bedTempControl": {"num": 0, "val": 60}}])]


def test_low_preheat_outside_a_print_still_goes(live, creality):
    store = live(_report(gcode="IDLE"))
    assert printer_commands.send("p1", "preheat", {"nozzle": 20, "bed": 1}) == CONFIRMED
    assert store.published[0]["print"]["param"] == "M104 S20\nM140 S1\n"
    cr = creality(K1C_IDLE)
    assert printer_commands.send("c1", "preheat", {"nozzle": 20}) == SENT
    assert cr.calls == [("set", [{"nozzleTempControl": 20}])]


def test_preheat_cools_thresholds():
    pc = printer_commands
    assert pc.PREHEAT_NOZZLE_MIN == pc.EXTRUDE_TEMP_MIN == 170 and pc.PREHEAT_BED_MIN == 30
    assert pc.preheat_cools({"nozzle": 169}) and pc.preheat_cools({"bed": 29}) and pc.preheat_cools({"nozzle": 0})
    assert not pc.preheat_cools({"nozzle": 170, "bed": 30}) and not pc.preheat_cools({})


# --- F4: passwords over bcrypt's 72 bytes ------------------------------------------------------------

@pytest.fixture
def app_client(tmp_path, monkeypatch):
    from starlette.testclient import TestClient
    import config
    from app.api import admin, auth
    from app.api import main as api_main
    from app.services import audit_service
    import app.services.state_store as ss
    monkeypatch.setattr(ss, "DB_PATH", str(tmp_path / "printer_history.db"))
    monkeypatch.setattr(api_main, "_history_db_path", lambda: str(tmp_path / "printer_history.db"))
    monkeypatch.setattr(api_main, "_ams_db_path", lambda: str(tmp_path / "ams_history.db"))
    for mod in (auth, admin, audit_service):
        monkeypatch.setattr(mod, "USERS_DB", str(tmp_path / "users.db"))
    monkeypatch.setattr(audit_service, "_audit", None)
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "qa-pass-123", raising=False)
    monkeypatch.setattr(config, "ADMIN_USERNAME", "admin", raising=False)
    app = api_main.create_app(ss.StateStore())     # its first-run admin: admin / qa-pass-123, in tmp
    client = TestClient(app, base_url="https://testserver", raise_server_exceptions=False)   # Secure cookies
    assert client.post("/api/auth/login", json={"username": "admin", "password": "qa-pass-123"}).status_code == 200
    assert client.get("/api/printers").status_code == 200
    return client


def test_f4_long_password_login_is_401_not_500(app_client):
    import bcrypt
    app_client.cookies.clear()
    r = app_client.post("/api/auth/login", json={"username": "admin", "password": "й" * 40})
    assert r.status_code == 401, (bcrypt.__version__, r.status_code, r.text)   # was 500 with bcrypt 5.x
    r = app_client.post("/api/auth/login", json={"username": "nobody", "password": "x" * 100})
    assert r.status_code == 401, r.text


def test_f4_long_password_is_refused_where_it_is_set(app_client):
    from app.api import admin
    r = app_client.post("/api/admin/users", json={"username": "qa_long", "password": "й" * 37, "role": "viewer"})
    assert (r.status_code, r.json()["detail"]) == (400, admin.MSG_PASSWORD_TOO_LONG)   # was 500
    ok = app_client.post("/api/admin/users", json={"username": "qa_long", "password": "й" * 36, "role": "viewer"})
    assert ok.status_code == 200, ok.text                                               # 72 bytes exactly
    uid = ok.json()["id"]
    r = app_client.put("/api/admin/users/%d" % uid, json={"password": "x" * 73})
    assert (r.status_code, r.json()["detail"]) == (400, admin.MSG_PASSWORD_TOO_LONG)
    app_client.cookies.clear()
    assert app_client.post("/api/auth/login", json={"username": "qa_long", "password": "й" * 36}).status_code == 200


def test_f4_a_hash_bcrypt_4_made_of_a_long_password_still_verifies():
    """bcrypt 4.x hashed the first 72 bytes of a longer password: that password keeps working."""
    import bcrypt
    from app.services import auth_service
    long_pw = "п" * 40                                                    # 80 bytes
    old_hash = bcrypt.hashpw(long_pw.encode("utf-8")[:72], bcrypt.gensalt(rounds=4)).decode()
    assert auth_service.verify_password(long_pw, old_hash) is True
    assert auth_service.verify_password("п" * 35, old_hash) is False
    assert auth_service.password_too_long(long_pw) and not auth_service.password_too_long("п" * 36)
    assert auth_service.verify_password(long_pw, auth_service.hash_password(long_pw)) is True


# --- F5 / hunt №36: numbers SQLite cannot bind, windows that are not finite --------------------------

@pytest.mark.parametrize("url", ["/api/history/events?fr=0&offset=9223372036854775808",
                                 "/api/history/timeline?fr=0&offset=99999999999999999999",
                                 "/api/history/jobs?offset=9223372036854775808",
                                 "/api/ams/history?printer_id=x&unit=99999999999999999999",
                                 "/api/admin/audit?offset=9223372036854775808"])
def test_f5_huge_numbers_are_a_client_error_not_a_crash(app_client, url):
    r = app_client.get(url)
    assert r.status_code == 422, (r.status_code, r.text)        # was 500 (OverflowError in SQLite)


@pytest.mark.parametrize("method,url,body", [
    ("put", "/api/admin/history/jobs/%d/verdict" % 2 ** 70, {"verdict": "good"}),
    ("put", "/api/admin/users/%d" % 2 ** 70, {"role": "viewer"}),
    ("delete", "/api/admin/users/%d" % 2 ** 70, None),
])
def test_f5_huge_ids_in_admin_paths_are_a_client_error(app_client, method, url, body):
    r = getattr(app_client, method)(url, **({"json": body} if body is not None else {}))
    assert r.status_code == 422, (r.status_code, r.text)


def test_f5_ordinary_offsets_and_ids_still_work(app_client):
    assert app_client.get("/api/history/events?fr=0&offset=9223372036854775807").status_code == 200
    assert app_client.get("/api/history/timeline?fr=0&offset=5").status_code == 200
    assert app_client.get("/api/ams/history?printer_id=x&unit=128").status_code == 200
    assert app_client.put("/api/admin/users/999", json={"role": "viewer"}).status_code == 404


@pytest.mark.parametrize("url", ["/api/history/states-summary?fr=0&to=inf",
                                 "/api/history/states-summary?fr=nan",
                                 "/api/history/states-summary?fr=-inf&to=0",
                                 "/api/history/timeline?fr=nan",
                                 "/api/history/events?fr=0&to=inf",
                                 "/api/ams/history?printer_id=x&fr=-inf"])
def test_hunt36_a_window_that_is_not_finite_is_a_400(app_client, url):
    r = app_client.get(url)
    assert (r.status_code, r.json()["detail"]) == (400, "fr and to must be finite"), r.text


def test_hunt36_a_finite_window_still_answers(app_client):
    assert app_client.get("/api/history/states-summary?fr=0").status_code == 200
    assert app_client.get("/api/history/states-summary?fr=10&to=5").json()["detail"] == "fr must be <= to"


# --- hunt №2: the browser's zone reaches job_stats ----------------------------------------------------

def _viewer(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    import config as cfg
    from app.api import auth as api_auth
    from app.api import main as api_main
    from app.domain.user_models import UserRole
    from app.services import auth_service
    monkeypatch.setattr(cfg, "JWT_SECRET", "test-secret", raising=False)
    monkeypatch.setattr(auth_service, "get_user_from_token", lambda *a, **k: ("u", UserRole.VIEWER))
    monkeypatch.setattr(auth_service, "decode_token", lambda *a, **k: {"iat": 0})
    monkeypatch.setattr(api_auth, "token_subject_active", lambda *a, **k: True)
    monkeypatch.setattr(api_main, "_history_db_path", lambda: str(tmp_path / "ph.db"))
    monkeypatch.setattr(api_main, "_current_labels", lambda rows=None: {})

    class _Store:
        def get_all(self):
            return []
    c = TestClient(api_main.create_app(_Store()))
    c.cookies.set("access_token", "stub")
    return c


def test_hunt2_stats_pass_the_zone_to_job_stats(monkeypatch, tmp_path):
    from app.services import job_queries
    seen = []

    def job_stats(db, fr, to, **kw):
        seen.append(kw.get("tz"))
        return {"ok": True}
    monkeypatch.setattr(job_queries, "job_stats", job_stats)
    c = _viewer(monkeypatch, tmp_path)
    assert c.get("/api/history/jobs/stats", params={"fr": 0, "to": 10, "tz": "Asia/Omsk"}).json() == {"ok": True}
    assert c.get("/api/history/jobs/stats", params={"fr": 0, "to": 10}).status_code == 200
    assert seen == ["Asia/Omsk", None]
    assert c.get("/api/history/jobs/stats", params={"fr": 0, "to": 10, "tz": "x" * 65}).status_code == 422


# --- hunt №9: a tag re-read and a load wait for each other -------------------------------------------

def test_rfid_reread_waits_for_a_sent_load(live):
    store = live(_report(snow=0x0000, gcode="IDLE"))
    assert printer_commands.send("p1", "ams_load", {"ams": 1, "slot": 2})["success"]
    # the printer has not reported «busy» yet; the AMS is about to feed slot 7
    r = printer_commands.send("p1", "ams_rfid", {"ams": 0, "slot": 1})
    assert r["status"] == "refused" and r["detail"] == printer_commands.MSG_INFLIGHT, r
    assert len(store.published) == 1


def test_load_waits_for_a_sent_rfid_reread(live):
    store = live(_report(snow=0x0000, gcode="IDLE"))
    assert printer_commands.send("p1", "ams_rfid", {"ams": 0, "slot": 1})["success"]
    # the AMS is turning slot 2 to read its tag; no report of that yet
    for action, params in (("ams_load", {"ams": 1, "slot": 2}), ("ams_unload", {}), ("ams_rfid", {"ams": 0, "slot": 1})):
        assert printer_commands.send("p1", action, params) == REFUSED(printer_commands.MSG_AMS_BUSY), action
    assert len(store.published) == 1


def test_rfid_reread_mark_lapses_and_holds_back_no_eject(live, monkeypatch):
    store = live(_report(snow=0x0000, gcode="IDLE"))
    assert printer_commands.send("p1", "ams_rfid", {"ams": 0, "slot": 1})["success"]
    assert printer_commands.send("p1", "eject")["success"]                # not an AMS spool: not held back
    now = printer_commands._monotonic()
    monkeypatch.setattr(printer_commands, "_monotonic", lambda: now + printer_commands.INFLIGHT_SECONDS)
    assert printer_commands.send("p1", "ams_load", {"ams": 1, "slot": 2})["success"]
    assert len(store.published) == 3


# --- hunt №24: Creality axis move obeys the eject's busy rule -----------------------------------------

HOMED = dict(K1C_IDLE, axes_homed="X:1 Y:1 Z:1", feed_state=0)


@pytest.mark.parametrize("busy", [dict(device_state=7), dict(device_state=0, feed_state=1)],
                         ids=["homing", "cfs_feeding"])
@pytest.mark.parametrize("action,params", [("move", {"axis": "z", "dist": 30}), ("home", {})])
def test_move_z_obeys_the_same_busy_rule_as_eject(creality, busy, action, params):
    store = creality(dict(HOMED, **busy))
    eject = printer_commands.send("c1", "eject")
    assert eject["status"] == "refused"                 # the guard works for eject
    move = printer_commands.send("c1", action, params)
    assert move == REFUSED(printer_commands.MSG_CREALITY_PREPARING), (move, store.calls)
    assert store.calls == []


@pytest.mark.parametrize("snapshot", [dict(HOMED, device_state=0), HOMED], ids=["idle", "no_device_state"])
def test_move_on_an_idle_creality_still_goes(creality, snapshot):
    store = creality(snapshot)
    assert printer_commands.send("c1", "move", {"axis": "z", "dist": 30}) == SENT
    assert store.calls == [("set", [{"setPosition": "Z30 F600"}])]


# --- hunt №25: Creality nozzle ceilings ---------------------------------------------------------------

def test_k1_nozzle_ceiling_is_its_own_300(creality):
    assert printer_commands.creality_temp_limits("k1", K1C_IDLE) == (300, 100)
    assert printer_commands.creality_temp_limits("k1se", {}) == (300, 100)


def test_k1_preheat_280_is_sent(creality):
    store = creality(K1C_IDLE, model="k1")
    r = printer_commands.send("c1", "preheat", {"nozzle": 280})
    assert r["success"], r
    assert store.calls == [("set", [{"nozzleTempControl": 280}])]


@pytest.mark.parametrize("model,reported,limits", [
    ("k2pro", {"max_nozzle_temp": 300, "max_bed_temp": 100}, (300, 100)),        # the printer's own
    ("ender3v3", {"max_nozzle_temp": 300, "max_bed_temp": 110}, (300, 110)),
    ("k2", {"max_nozzle_temp": 500, "max_bed_temp": 200}, (350, 120)),           # at most the highest listed
    ("k2", {}, (260, 100)),                                                      # none reported: the fallback
    (None, {"max_nozzle_temp": 0, "max_bed_temp": True}, (260, 100)),
])
def test_unlisted_model_takes_the_reported_ceiling(model, reported, limits):
    assert printer_commands.creality_temp_limits(model, reported) == limits


# === second batch: F6, F7, F8, F10, F11 of the qa-func report ========================================

# --- F6: settings refuse a null interval, a chat id past 2^53, a template past 1000 characters --------

def _settings_refusal(values):
    from app.api import admin
    try:
        admin._validate_settings(values)
    except HTTPException as e:
        return e.status_code, e.detail
    return None


@pytest.mark.parametrize("key", ["telegram_update_interval", "telegram_finish_repeat_interval_min",
                                 "proxy_check_interval"])
def test_f6_a_null_interval_is_refused(key):
    out = _settings_refusal({key: None})
    assert out is not None and out[0] == 400 and key in out[1], out     # was accepted: sleep(None) later


def test_f6_chat_id_and_template_bounds():
    from app.api import admin
    assert _settings_refusal({"telegram_chat_id": 10 ** 30})[0] == 400
    assert _settings_refusal({"telegram_chat_id": -(2 ** 53)})[0] == 400
    for ok in (None, -1001234567890, 123456789, admin.TELEGRAM_CHAT_ID_MAX):
        assert _settings_refusal({"telegram_chat_id": ok}) is None, ok
    long_one = "✅ {label} " + "x" * admin.TELEGRAM_TEMPLATE_MAX
    assert _settings_refusal({"telegram_finish_template": long_one}) == (
        400, "telegram_finish_template: не длиннее %d символов" % admin.TELEGRAM_TEMPLATE_MAX)
    assert _settings_refusal({"telegram_paused_template": long_one[:admin.TELEGRAM_TEMPLATE_MAX]}) is None
    assert _settings_refusal({"telegram_update_interval": 5, "proxy_check_interval": 600}) is None


def test_f6_proxy_checker_survives_a_stored_null_interval():
    from app.services.proxy_checker import ProxyChecker
    pc = ProxyChecker([], None)
    assert pc._check_interval == ProxyChecker.DEFAULT_INTERVAL
    for bad in (None, "600", True, -5, float("nan")):
        pc.set_interval(bad)
        assert pc._check_interval == ProxyChecker.DEFAULT_INTERVAL, bad
    pc.set_interval(30)
    assert pc._check_interval == 30
    pc.set_interval(0)
    assert pc._check_interval == 0


def test_f6_settings_api_refuses_null(app_client, tmp_path, monkeypatch):
    from app.services import settings_service
    monkeypatch.setattr(settings_service, "DB_PATH", str(tmp_path / "settings.db"))
    settings_service.init_db()
    settings_service.reset_cache()
    try:
        r = app_client.put("/api/admin/settings", json={"telegram_update_interval": None})
        assert (r.status_code, r.json()["detail"]) == (400, "telegram_update_interval must be an integer 1-3600")
        assert settings_service.get("telegram_update_interval") == 2           # nothing saved
    finally:
        settings_service.reset_cache()


# --- F7: new user names --------------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["  ", "qa_viewer ", " qa", "qa\tx", "q a", "qa\u202eevil", "qa\x00x", "x\n",
                                  "a" * 33, "а" * 5000, "аdmin", "Ivan_Иван", "qa!"])
def test_f7_a_bad_new_name_is_a_400(users_db, name):
    from app.api import admin, auth
    with pytest.raises(HTTPException) as e:
        admin.create_user(admin.CreateUserRequest(username=name, password="pass-12345", role="viewer"), _request())
    assert (e.value.status_code, e.value.detail) == (400, admin.MSG_USERNAME), repr(name)
    assert auth.get_all_users() == []


@pytest.mark.parametrize("first,second", [("qa_viewer", "QA_VIEWER"), ("Иван", "иван"), ("Bob", "bob")])
def test_f7_the_same_name_in_another_case_is_taken(users_db, first, second):
    from app.api import admin, auth
    admin.create_user(admin.CreateUserRequest(username=first, password="pass-12345", role="viewer"), _request())
    with pytest.raises(HTTPException) as e:
        admin.create_user(admin.CreateUserRequest(username=second, password="pass-12345", role="viewer"), _request())
    assert (e.value.status_code, e.value.detail) == (409, admin.MSG_USERNAME_TAKEN % first)
    assert [u.username for u in auth.get_all_users()] == [first]
    assert _writable(users_db) is True


@pytest.mark.parametrize("name", ["Иван_2", "qa.viewer@x", "a" * 32, "admin2", "ёлка-1"])
def test_f7_a_good_name_is_created(users_db, name):
    from app.api import admin
    out = admin.create_user(admin.CreateUserRequest(username=name, password="pass-12345", role="viewer"), _request())
    assert out["username"] == name


def test_f7_an_existing_odd_name_still_signs_in(app_client):
    """Only «Добавить» checks: a name saved before (a trailing space) keeps working."""
    from app.api import auth
    from app.services.auth_service import hash_password
    conn = sqlite3.connect(auth.USERS_DB)
    conn.execute("INSERT INTO users (username, password_hash, role, created_at, updated_at) VALUES (?, ?, 'viewer', 0, 0)",
                 ("old viewer ", hash_password("pass-12345")))
    conn.commit()
    conn.close()
    names = [u["username"] for u in app_client.get("/api/admin/users").json()]
    assert "old viewer " in names
    app_client.cookies.clear()
    assert app_client.post("/api/auth/login", json={"username": "old viewer ", "password": "pass-12345"}).status_code == 200


# --- F8: «Выйти» revokes the access token, only that session's -----------------------------------------

def test_f8_the_old_access_token_stops_working_after_logout(app_client):
    from starlette.testclient import TestClient
    other = TestClient(app_client.app, base_url="https://testserver")         # the same admin on another device
    assert other.post("/api/auth/login", json={"username": "admin", "password": "qa-pass-123"}).status_code == 200
    copied = app_client.cookies.get("access_token")
    assert app_client.post("/api/auth/logout").status_code == 200
    app_client.cookies.clear()
    app_client.cookies.set("access_token", copied)
    assert app_client.get("/api/printers").status_code == 401                # was 200 until it expired
    assert app_client.get("/api/auth/me").status_code == 401
    assert other.get("/api/printers").status_code == 200                       # the other session stays in


def test_f8_access_tokens_carry_their_own_id():
    from app.domain.user_models import UserRole
    from app.services import auth_service
    a = auth_service.decode_token(auth_service.create_access_token("u", UserRole.VIEWER, "s" * 32, 60), "s" * 32)
    b = auth_service.decode_token(auth_service.create_access_token("u", UserRole.VIEWER, "s" * 32, 60), "s" * 32)
    assert a["jti"] and a["jti"] != b["jti"]


def test_f8_a_token_without_an_id_is_judged_as_before(users_db):
    from app.api import auth
    conn = sqlite3.connect(users_db)
    conn.execute("INSERT INTO users (username, password_hash, role, created_at, updated_at) VALUES ('u', 'x', 'viewer', 0, 0)")
    conn.commit()
    conn.close()
    assert auth.token_subject_active("u", 10) is True                          # issued before the deploy
    assert auth.token_subject_active("u", 10, "a" * 32) is True
    auth._revoke_jti("a" * 32, 10 ** 10)
    assert auth.token_subject_active("u", 10, "a" * 32) is False
    assert auth.token_subject_active("u", 10, "b" * 32) is True


# --- F10: the printer's address and name ---------------------------------------------------------------

def _printer_refusal(kind, data, creating=True):
    from app.api import admin
    try:
        admin._validate_printer_fields(kind, dict(data), creating)
    except HTTPException as e:
        return e.status_code, e.detail
    return None


@pytest.mark.parametrize("host", ["http://192.0.2.99:80/path?x=1", "192.0.2.99; reboot", "192.0.2.99:8080",
                                  "host/x", "192.0.2.999", "a..b", "-bad.local", "pri nter", "x" * 254,
                                  "[fe80::1]", "192.0.2.10\nfoo"])
def test_f10_a_bad_address_is_a_400(host):
    from app.api import admin
    assert _printer_refusal("creality", {"label": "K1", "host": host}) == (400, admin.MSG_BAD_HOST), host
    assert _printer_refusal("creality", {"host": host}, creating=False) == (400, admin.MSG_BAD_HOST), host


@pytest.mark.parametrize("host", ["192.0.2.10", " 192.0.2.10 ", "printer-1.local", "K1C_ab12", "localhost",
                                  "fe80::1", "2001:db8::5"])
def test_f10_a_good_address_passes(host):
    assert _printer_refusal("creality", {"label": "K1", "host": host}) is None


def test_f10_the_name_is_at_most_64_characters(app_client):
    from app.api import admin
    assert _printer_refusal("klipper", {"label": "x" * 65, "host": "192.0.2.5"}) == (400, admin.MSG_LABEL_TOO_LONG)
    assert _printer_refusal("klipper", {"label": "x" * 64, "host": "192.0.2.5"}) is None
    assert _printer_refusal("klipper", {"port": 7125}, creating=False) is None     # an edit without them
    r = app_client.post("/api/admin/printers", json={"kind": "klipper", "label": "x" * 20000, "host": "192.0.2.5"})
    assert (r.status_code, r.json()["detail"]) == (400, admin.MSG_LABEL_TOO_LONG)
    r = app_client.post("/api/admin/printers", json={"kind": "klipper", "label": "K", "host": "192.0.2.99; reboot"})
    assert (r.status_code, r.json()["detail"]) == (400, admin.MSG_BAD_HOST)


# --- F11: the adapter tests wait for the connect bridge, not only for paho's connect() -----------------

@pytest.fixture
def slow_bridge(monkeypatch):
    """The bridge thread ends a moment after the listener has called paho connect(), as
    on a loaded machine: its last step (the «listener started» line) takes 0.5 s here."""
    import time as _time
    from app.collectors import bambu_collector
    real = bambu_collector.logger.info

    def info(msg, *a, **k):
        if "listener started" in str(msg):
            _time.sleep(0.5)
        return real(msg, *a, **k)
    monkeypatch.setattr(bambu_collector.logger, "info", info)


@pytest.mark.parametrize("name", ["test_rebuilt_listener_does_not_inherit_a_refused_login",
                                  "test_listener_that_ends_on_its_own_is_rebuilt_with_backoff"])
def test_f11_listener_tests_hold_under_a_slow_bridge(slow_bridge, fake_paho, make_collector, fake_clock, name):
    import test_bambu_adapter as adapter     # its tests, run as they are: not collected again here
    getattr(adapter, name)(fake_paho, make_collector, fake_clock)
