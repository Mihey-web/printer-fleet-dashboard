"""The audit journal over the API for «История → Действия» (IA §7, plan step 12):
GET /api/admin/audit with several action prefixes, exclusions, a printer and a
window; GET /api/admin/audit/counts (per user and action key) and
GET /api/admin/audit.csv. A real users.db under tmp (audit_service.USERS_DB
patched), auth stubbed as in test_maintenance_api, the role switchable.
"""
import csv
import io
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api import auth as api_auth
from app.api import main as api_main
from app.domain.user_models import UserRole
from app.services import audit_service, auth_service, settings_service

T0 = 1793300000.0
ROWS = [  # (user, action, seconds after T0)
    ("admin", "login_ok", 0),
    ("admin", "token_refresh", 10),
    ("admin", "token_refresh", 20),
    ("admin", "token_refresh", 30),
    ("admin", "printer_command:pause:bambu-1:st=confirmed", 40),
    ("admin", "printer_command:light_on:bambu-10:st=sent", 50),
    ("admin", "printer_bulk:light_on:b7:confirmed=2,sent=0,rejected=0,skipped=1", 60),
    ("auto-dry", "autodry_start:bambu-1:0:55/12/PETG", 70),
    ("auto-ignore", "auto_ignore:bambu-1:0500_8062:confirmed", 80),
    ("auto-ignore", "auto_ignore:bambu-10:0C00_8009:skip:tries_used", 90),
    ("admin", "printer_maintenance:bambu-10:on", 100),
    ("admin", "notif_mute:bambu-1:pe:0500_8062:on", 110),
    (None, "login_fail", 120),
    ("guest", "login_ok", 130),
    ("admin", "settings_update", 140),
    ("admin", "printer_command:stop:x_y-1:st=sent", 150),       # an id with _ and -: no pattern chars
]


@pytest.fixture
def env(tmp_path, monkeypatch):
    import config as cfg

    import sqlite3

    db = str(tmp_path / "users.db")
    monkeypatch.setattr(audit_service, "USERS_DB", db)
    monkeypatch.setattr(audit_service, "_audit", None)
    svc = audit_service.get_audit_service()          # creates the table and its indexes
    conn = sqlite3.connect(db)
    conn.executemany("INSERT INTO audit_log (user_id, action, ip_address, user_agent, created_at) VALUES (?, ?, ?, ?, ?)",
                     [(u, a, "10.0.0.5", "Mozilla/5.0 (Windows NT 10.0) Chrome/130.0", T0 + dt) for u, a, dt in ROWS])
    conn.commit()
    conn.close()

    role = {"r": UserRole.ADMIN}
    monkeypatch.setattr(cfg, "JWT_SECRET", "test-secret", raising=False)
    monkeypatch.setattr(auth_service, "get_user_from_token", lambda *a, **k: ("admin", role["r"]))
    monkeypatch.setattr(auth_service, "decode_token", lambda *a, **k: {"iat": 0})
    monkeypatch.setattr(api_auth, "token_subject_active", lambda *a, **k: True)
    monkeypatch.setattr(settings_service, "get", lambda key, **k: None)
    client = TestClient(api_main.create_app(SimpleNamespace(get_all=lambda: [], get_one=lambda pid: None)))
    client.cookies.set("access_token", "stub")
    yield SimpleNamespace(client=client, role=role, svc=svc, db=db)


def _actions(env, query):
    r = env.client.get("/api/admin/audit?" + query)
    assert r.status_code == 200, r.text
    return [row["action"] for row in r.json()["rows"]]


def test_the_old_reads_stay(env):
    rows = _actions(env, "limit=500")
    assert len(rows) == len(ROWS) and rows[0] == ROWS[-1][1]                      # newest first
    assert _actions(env, "user=auto-ignore&limit=500") == [ROWS[9][1], ROWS[8][1]]  # the journal's presses
    assert _actions(env, "action=login_ok") == ["login_ok", "login_ok"]
    page = env.client.get("/api/admin/audit?limit=3&offset=0").json()
    assert len(page["rows"]) == 3 and page["has_more"] is True


def test_prefixes_comma_separated_or_repeated(env):
    cmd = _actions(env, "prefix=printer_command,printer_bulk&limit=500")
    assert cmd == [ROWS[15][1], ROWS[6][1], ROWS[5][1], ROWS[4][1]]
    assert _actions(env, "prefix=printer_command&prefix=printer_bulk&limit=500") == cmd
    auto = _actions(env, "prefix=autodry,auto_ignore")
    assert auto == [ROWS[9][1], ROWS[8][1], ROWS[7][1]]
    # a start of the action, not a pattern: «_» is a letter here
    assert _actions(env, "prefix=login") == ["login_ok", "login_fail", "login_ok"]
    assert _actions(env, "prefix=printer_") == [a for _, a, _ in reversed(ROWS) if a.startswith("printer_")]


def test_service_rows_are_left_out_on_request(env):
    rows = _actions(env, "exclude=token_refresh&limit=500")
    assert len(rows) == len(ROWS) - 3 and "token_refresh" not in rows
    assert _actions(env, "exclude=token_refresh,login&prefix=login,token_refresh") == []


def test_a_printer_is_a_whole_part_of_the_action(env):
    # bambu-1 is not bambu-10: the id must be a whole ":"-separated part
    one = _actions(env, "printer=bambu-1&limit=500")
    assert one == [ROWS[11][1], ROWS[8][1], ROWS[7][1], ROWS[4][1]]
    ten = _actions(env, "printer=bambu-10&limit=500")
    assert ten == [ROWS[10][1], ROWS[9][1], ROWS[5][1]]
    assert _actions(env, "printer=x_y-1") == [ROWS[15][1]]
    assert env.client.get("/api/admin/audit?printer=a%25b").status_code == 400  # no % or other junk
    assert _actions(env, "printer=bambu-1&prefix=auto_ignore") == [ROWS[8][1]]


def test_the_window(env):
    rows = _actions(env, "fr=%d&to=%d&limit=500" % (T0 + 40, T0 + 70))      # fr <= t < to
    assert rows == [ROWS[6][1], ROWS[5][1], ROWS[4][1]]


def test_bad_filters_are_refused(env):
    for q in ("prefix=Printer", "prefix=a%3Bb", "exclude=x%25", "prefix=" + ",".join("p%d" % i for i in range(17)),
              "prefix=" + "a" * 41, "printer=" + "p" * 65):
        assert env.client.get("/api/admin/audit?" + q).status_code == 400, q
    assert env.client.get("/api/admin/audit?prefix=,,&limit=1").status_code == 200   # empty parts: no filter


def test_counts_per_user_and_action_key(env):
    r = env.client.get("/api/admin/audit/counts")
    assert r.status_code == 200
    body = r.json()
    got = {(x["user"], x["key"]): x["n"] for x in body["rows"]}
    assert got[("admin", "token_refresh")] == 3 and got[("admin", "printer_command")] == 3
    assert got[("auto-ignore", "auto_ignore")] == 2 and got[(None, "login_fail")] == 1
    assert got[("admin", "login_ok")] == 1 and got[("guest", "login_ok")] == 1
    assert sum(got.values()) == len(ROWS) and body["truncated"] is False
    assert body["rows"][0] == {"user": "admin", "key": "printer_command", "n": 3}     # the largest, then by key
    # the head's window and printer apply
    w = {(x["user"], x["key"]): x["n"] for x in
         env.client.get("/api/admin/audit/counts?printer=bambu-10&fr=%d" % (T0 + 60)).json()["rows"]}
    assert w == {("auto-ignore", "auto_ignore"): 1, ("admin", "printer_maintenance"): 1}
    assert env.client.get("/api/admin/audit/counts?printer=a%20b").status_code == 400


def test_counts_are_capped(env):
    for i in range(600):
        env.svc.log("u%03d" % i, "login_fail")
    body = env.client.get("/api/admin/audit/counts").json()
    assert len(body["rows"]) == 500 and body["truncated"] is True


def test_csv_of_the_filtered_list(env):
    r = env.client.get("/api/admin/audit.csv?prefix=auto_ignore&printer=bambu-10&fr=%d&to=%d" % (T0, T0 + 3600))
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert 'filename="forge-audit-' in r.headers["content-disposition"]
    text = r.content.decode("utf-8")
    assert text.startswith("﻿")
    rows = list(csv.reader(io.StringIO(text[1:]), delimiter=";"))
    assert rows[0] == ["Время", "Пользователь", "Действие", "IP", "Браузер"]
    assert [x[1:4] for x in rows[1:]] == [["auto-ignore", ROWS[9][1], "10.0.0.5"]]
    # a cell a spreadsheet would run stays text
    env.svc.log("=cmd|' /C calc'!A0", "login_fail")
    rows = list(csv.reader(io.StringIO(env.client.get("/api/admin/audit.csv?action=login_fail").content.decode("utf-8")[1:]),
                           delimiter=";"))
    assert rows[1][1].startswith("'=")


def test_csv_column_names_in_the_panels_language(env):
    url = "/api/admin/audit.csv?prefix=auto_ignore&printer=bambu-10&fr=%d&to=%d" % (T0, T0 + 3600)
    ru = list(csv.reader(io.StringIO(env.client.get(url).content.decode("utf-8")[1:]), delimiter=";"))
    en = list(csv.reader(io.StringIO(env.client.get(url + "&lang=en").content.decode("utf-8")[1:]), delimiter=";"))
    assert en[0] == ["Time", "User", "Action", "IP", "Browser"]
    assert en[1:] == ru[1:]
    assert env.client.get(url + "&lang=xx").content == env.client.get(url).content


def test_admins_only(env):
    env.role["r"] = UserRole.VIEWER
    for url in ("/api/admin/audit", "/api/admin/audit/counts", "/api/admin/audit.csv"):
        assert env.client.get(url).status_code == 403, url


def test_the_time_index(env):
    import sqlite3
    conn = sqlite3.connect(env.db)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
    conn.close()
    assert "idx_audit_time" in names
