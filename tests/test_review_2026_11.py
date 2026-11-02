"""Regression tests for the findings of REVIEW_2026-11.md.

Each test failed before its fix; the finding's number in the report is in the
test's name (f1 = finding 1, …).
"""
import sqlite3
import threading
import time
import types

import pytest
from fastapi import HTTPException
from starlette.responses import Response

from app.api import auth
from app.services import audit_service
from app.services.auth_service import create_access_token
from app.domain.user_models import UserRole


SECRET = "s" * 64


@pytest.fixture
def auth_db(tmp_path, monkeypatch):
    """users.db (accounts + audit) in tmp, a fresh AuditService, a known JWT secret."""
    db = str(tmp_path / "users.db")
    monkeypatch.setattr(auth, "USERS_DB", db)
    monkeypatch.setattr(audit_service, "USERS_DB", db)
    monkeypatch.setattr(audit_service, "_audit", None)
    monkeypatch.setattr(auth, "JWT_SECRET", SECRET)
    monkeypatch.setattr(auth, "COOKIE_SECURE", False)
    auth.init_users_db()
    return db


def _add_user(db, username, role="viewer", tokens_valid_after=0.0):
    now = time.time()
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO users (username, password_hash, role, created_at, updated_at, tokens_valid_after)"
                 " VALUES (?, ?, ?, ?, ?, ?)", (username, "unused", role, now, now, tokens_valid_after))
    conn.commit()
    conn.close()


def _req(host, headers=None):
    return types.SimpleNamespace(client=types.SimpleNamespace(host=host), headers=dict(headers or {}),
                                 state=types.SimpleNamespace())


# --- f1: X-Forwarded-For — the client writes its left end ---------------------------------------

def test_f1_client_ip_takes_the_proxys_own_xff_entry_not_the_clients():
    # A proxy that sets only X-Forwarded-For APPENDS the peer address (nginx
    # $proxy_add_x_forwarded_for, Traefik, HAProxy option forwardfor): the left
    # end is whatever the client sent. Taking it lets one client rotate the
    # header and dodge the per-IP login limit.
    spoofed = _req("127.0.0.1", {"x-forwarded-for": "10.9.8.7, 198.51.100.7"})
    assert auth.client_ip(spoofed) == "198.51.100.7"
    # a single entry and X-Real-IP keep working as before
    assert auth.client_ip(_req("127.0.0.1", {"x-forwarded-for": "198.51.100.7"})) == "198.51.100.7"
    assert auth.client_ip(_req("127.0.0.1", {"x-real-ip": "198.51.100.8",
                                             "x-forwarded-for": "1.1.1.1, 2.2.2.2"})) == "198.51.100.8"
    # still ignored from an untrusted peer
    assert auth.client_ip(_req("203.0.113.9", {"x-forwarded-for": "1.2.3.4"})) == "203.0.113.9"


# --- f2: parallel logins overrun the per-IP limit -----------------------------------------------

def test_f2_parallel_logins_from_one_ip_get_at_most_five_password_checks(auth_db, monkeypatch):
    _add_user(auth_db, "admin", role="admin")
    checks = []

    def slow_verify(password, password_hash):
        checks.append(password)
        time.sleep(0.2)                     # bcrypt rounds=12 is ~0.25 s on a Pi
        return False

    monkeypatch.setattr(auth, "verify_password", slow_verify)
    results = []
    start = threading.Barrier(15)

    def attempt(i):
        start.wait()
        try:
            auth.login(auth.LoginRequest(username="admin", password="guess%d" % i),
                       _req("203.0.113.5"), Response())
            results.append(200)
        except HTTPException as e:
            results.append(e.status_code)

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(15)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert len(results) == 15
    # The limit is 5 failed attempts per IP per 15 min: no more guesses than that,
    # however many requests arrive at once (FastAPI runs ~40 of them in parallel).
    assert len(checks) <= 5, "%d password checks" % len(checks)
    assert results.count(429) >= 10


# --- f3: /api/auth/me answers for a revoked session --------------------------------------------

def test_f3_me_rejects_a_token_issued_before_a_password_reset(auth_db):
    # A password reset / role change bumps tokens_valid_after: every other route
    # (the middleware) then rejects the old access token. /api/auth/me is public
    # to the middleware and must check the watermark itself.
    _add_user(auth_db, "alice", tokens_valid_after=time.time() + 5)
    token = create_access_token("alice", UserRole.VIEWER, SECRET, 900)
    with pytest.raises(HTTPException) as e:
        auth.me(_req("127.0.0.1"), access_token_cookie=token)
    assert e.value.status_code == 401


def test_f3_me_still_answers_a_live_session(auth_db):
    _add_user(auth_db, "alice")
    token = create_access_token("alice", UserRole.VIEWER, SECRET, 900)
    assert auth.me(_req("127.0.0.1"), access_token_cookie=token)["user"]["username"] == "alice"


# --- f4: NaN / Infinity in telemetry take /api/printers down for the whole farm -----------------

import json
import math

import bambu_testkit as kit
from app.domain.models import PrinterKind
from app.services.normalizer import normalize_bambu, normalize_ws_dict
from app.services.utils import as_float


def _json_ok(status):
    # what Starlette's JSONResponse does with the /api/printers listing
    json.dumps(status.to_dict(), allow_nan=False)


@pytest.mark.parametrize("bad", ["nan", "inf", "-inf", float("nan"), float("inf"), "1e999"])
def test_f4_as_float_drops_non_finite_values(bad):
    assert as_float(bad) is None
    assert as_float("21.5") == 21.5 and as_float(0) == 0.0


def test_f4_ws_printer_with_nan_temperature_still_serialises():
    st = normalize_ws_dict("k1", "K", PrinterKind.KLIPPER,
                           {"state": "printing", "progress": "inf", "nozzle_temp": "nan", "bed_temp": 60,
                            "remaining_time": 0, "debug": {}})
    assert st.online and st.nozzle_temp is None and st.progress_pct is None and st.bed_temp == 60
    _json_ok(st)


def test_f4_bambu_report_with_nan_progress_still_serialises(offline_collector):
    collector = offline_collector("P2S")
    # json.loads (the vendored lib's parser) takes NaN / Infinity and turns 1e999 into inf
    kit.deliver(collector, b'{"print":{"command":"push_status","msg":0,"gcode_state":"RUNNING",'
                           b'"mc_percent":NaN,"nozzle_temper":1e999,"bed_temper":60}}')
    st = normalize_bambu("p1", "x", collector.client.get_device(), device_type="P2S")
    assert st.online
    _json_ok(st)


# --- f5: a non-string AMS tray type / colour makes normalize_bambu raise -----------------------

@pytest.mark.parametrize("tray", [{"tray_type": 1, "tray_color": "FF0000FF"},
                                  {"tray_type": ["PLA"], "tray_color": "FF0000FF"},
                                  {"tray_type": "PLA", "tray_color": 16711935}])
def test_f5_non_string_tray_fields_do_not_take_the_printer_offline(offline_collector, tray):
    collector = offline_collector("P2S")
    t = dict({"id": "0", "tray_info_idx": "GFG99", "remain": 80, "state": 11}, **tray)
    kit.deliver(collector, {"print": {"command": "push_status", "msg": 0, "gcode_state": "IDLE",
                                      "ams": {"ams": [{"id": "0", "humidity": "3", "temp": "25.0",
                                                       "info": "10002003", "tray": [t]}]}}})
    # the poll loop counts an exception here as a failed fetch: grace, then OFFLINE
    st = normalize_bambu("p1", "x", collector.client.get_device(), device_type="P2S")
    assert st.online and st.ams and st.ams["units"][0]["slots"]
    _json_ok(st)


# --- f8: PUT /api/admin/settings stored any printer_capability --------------------------------

@pytest.mark.parametrize("bad", [["p1"], "yes", True, {"p1": "yes"}, {"p1": 1}])
def test_f8_settings_put_refuses_a_malformed_printer_capability(bad):
    from app.api import admin
    with pytest.raises(HTTPException) as e:
        admin._validate_settings({"printer_capability": bad})
    assert e.value.status_code == 400
    admin._validate_settings({"printer_capability": {"p1": True, "p2": False}})


# --- f9: a failed AMS history insert leaves the write transaction open -------------------------

def test_f9_failed_ams_insert_is_rolled_back(tmp_path, monkeypatch):
    from app.domain.models import PrinterState, PrinterStatus
    from app.services import ams_store
    monkeypatch.setattr(ams_store, "DB_PATH", str(tmp_path / "ams_history.db"))
    store = ams_store.AmsStore()
    units = [{"index": 0, "humidity": 3, "humidity_pct": 20, "temp": 25.0, "dry_time": 0},
             {"index": 1, "humidity": 3, "humidity_pct": 20, "temp": 25.0, "dry_time": 10 ** 30}]
    st = PrinterStatus(id="p1", label="x", kind=PrinterKind.BAMBU, online=True, state=PrinterState.IDLE,
                       ams={"units": units})
    store.record_ams(st)                    # unit 1 overflows SQLite INTEGER
    assert not store._db.in_transaction     # unit 0's row must not stay pending in an open txn
    st.ams = {"units": units[:1]}
    store.record_ams(st)
    other = sqlite3.connect(str(tmp_path / "ams_history.db"))
    assert other.execute("SELECT COUNT(*) FROM ams_history").fetchone()[0] == 1
    other.close()


# --- f10: the backup opened the wrong file when the path holds '#' or '?' ----------------------

def test_f10_backup_reads_a_source_path_with_hash(tmp_path):
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "forge_db_backup", Path(__file__).resolve().parent.parent / "deploy" / "forge_db_backup.py")
    backup = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(backup)
    d = tmp_path / "farm #1"
    d.mkdir()
    con = sqlite3.connect(str(d / "users.db"))
    con.execute("create table t (v)")
    con.execute("insert into t values (1)")
    con.commit()
    con.close()
    dst = tmp_path / "copy.db"
    assert backup.backup_db(str(d / "users.db"), str(dst)) == "ok"
    con = sqlite3.connect(str(dst))
    assert con.execute("select count(*) from t").fetchone()[0] == 1   # was: an empty copy, exit 0
    con.close()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["copy.db", "farm #1"]   # no stray "farm " file
