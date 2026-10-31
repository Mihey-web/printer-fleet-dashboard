"""Hidden notifications («Скрыть», owner 27.10): the notification_mutes setting,
PUT /api/admin/printers/{id}/notification-mute (admin only, audit) and the
"muted" flag of every record in /api/printers and /api/printers/{id}.

The registry and the settings are real SQLite files under tmp; auth is stubbed
as in test_maintenance_api, the role switchable.
"""
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api import auth as api_auth
from app.api import main as api_main
from app.domain.models import PrinterKind, PrinterState, PrinterStatus
from app.domain.user_models import UserRole
from app.services import auth_service, notification_mutes as nm, printer_registry, settings_service

LIDAR = "hms:0C00_0100_0001_0004"
MEM = "hms:0500_0100_0003_0015"
PE = "pe:0500_8062"


def _rec(key, severity="fatal"):
    return {"key": key, "src": key.split(":")[0], "code": key.split(":", 1)[1], "severity": severity,
            "text": "x", "actions": [], "blocking": False, "routine": False}


class _Store:
    def __init__(self, statuses):
        self._s = {s.id: s for s in statuses}

    def get_all(self):
        return list(self._s.values())

    def get_one(self, pid):
        return self._s.get(pid)


@pytest.fixture
def settings_db(tmp_path, monkeypatch):
    path = str(tmp_path / "settings.db")
    monkeypatch.setattr(settings_service, "DB_PATH", path)
    settings_service.init_db(path)
    settings_service.reset_cache()
    yield settings_service
    settings_service.reset_cache()


@pytest.fixture
def env(tmp_path, monkeypatch, settings_db):
    import config as cfg
    from app.api import admin as api_admin

    db = str(tmp_path / "printer_history.db")
    monkeypatch.setattr(printer_registry, "DB_PATH", db)
    printer_registry._invalidate_maintenance()
    printer_registry._invalidate_label()
    printer_registry.init_db(db)
    conn = sqlite3.connect(db)
    conn.executemany("INSERT INTO printers (id, kind, label, host, deleted, created_at, updated_at)"
                     " VALUES (?, ?, ?, 'h', 0, 1.0, 1.0)", [("c1", "bambu", "C1"), ("p3", "bambu", "P3")])
    conn.commit()
    conn.close()

    role = {"r": UserRole.ADMIN}
    monkeypatch.setattr(cfg, "JWT_SECRET", "test-secret", raising=False)
    monkeypatch.setattr(auth_service, "get_user_from_token", lambda *a, **k: ("admin", role["r"]))
    monkeypatch.setattr(auth_service, "decode_token", lambda *a, **k: {"iat": 0})
    monkeypatch.setattr(api_auth, "token_subject_active", lambda *a, **k: True)
    audit = []
    monkeypatch.setattr(api_admin, "get_audit_service",
                        lambda: SimpleNamespace(log=lambda user, action, *a, **k: audit.append((user, action))))
    c1 = PrinterStatus(id="c1", label="x", kind=PrinterKind.BAMBU, online=True, state=PrinterState.PRINTING,
                       device_type="X1C", notifications=[_rec(LIDAR), _rec(MEM, "common")])
    p3 = PrinterStatus(id="p3", label="x", kind=PrinterKind.BAMBU, online=True, state=PrinterState.PAUSED,
                       device_type="P2S", notifications=[_rec(PE, "pause")])
    store = _Store([c1, p3])
    client = TestClient(api_main.create_app(store))
    client.cookies.set("access_token", "stub")
    yield SimpleNamespace(client=client, role=role, audit=audit, store=store, c1=c1)
    printer_registry._invalidate_maintenance()
    printer_registry._invalidate_label()


def _put(env, pid, key, muted):
    return env.client.put("/api/admin/printers/%s/notification-mute" % pid, json={"key": key, "muted": muted})


def _flags(env, pid=None):
    if pid is not None:
        return {n["key"]: n["muted"] for n in env.client.get("/api/printers/%s" % pid).json()["notifications"]}
    return {r["id"]: {n["key"]: n["muted"] for n in r["notifications"]} for r in env.client.get("/api/printers").json()}


# --- the setting -------------------------------------------------------------------------

def test_the_setting_defaults_to_nothing_muted(settings_db):
    assert settings_service.DEFAULTS["notification_mutes"] == {}
    assert settings_db.get("notification_mutes") == {} and nm.current() == {}


@pytest.mark.parametrize("value,ok", [
    ({}, True),
    ({"c1": [LIDAR, MEM]}, True),
    ({"c1": []}, True),
    ({"c1": ["k%d" % i for i in range(64)]}, True),
    ({"c1": ["k%d" % i for i in range(65)]}, False),          # at most 64 per printer
    ({"c1": ["x" * 80]}, True),
    ({"c1": ["x" * 81]}, False),                               # a key is at most 80 characters
    ({"c1": [""]}, False),
    ({"c1": ["hms:\n0C00"]}, False),                           # printable only
    ({"c1": ["a\x00b"]}, False),
    ({"c1": [5]}, False),
    ({"c1": LIDAR}, False),                                    # a list, not a string
    ({"": [LIDAR]}, False),
    ([LIDAR], False),
    (None, False),
])
def test_setting_validation(value, ok):
    assert nm.valid(value) is ok


def test_set_muted_keeps_keys_idempotent_and_persistent(settings_db):
    assert nm.set_muted("c1", LIDAR, True) == (True, [LIDAR])
    assert nm.set_muted("c1", LIDAR, True) == (False, [LIDAR])          # a repeat changes nothing
    assert nm.set_muted("c1", MEM, True) == (True, [LIDAR, MEM])
    assert nm.set_muted("p3", PE, False) == (False, [])                 # nothing to unmute
    settings_service.reset_cache()                                      # a restart: it is in the DB
    assert settings_db.get("notification_mutes") == {"c1": [LIDAR, MEM]}
    assert nm.set_muted("c1", LIDAR, False) == (True, [MEM])
    assert nm.set_muted("c1", MEM, False) == (True, [])
    assert settings_db.get("notification_mutes") == {}                  # an empty list goes with its printer
    assert settings_service.DEFAULTS["notification_mutes"] == {}        # the shared default is never touched


def test_set_muted_refuses_a_bad_key_and_a_full_list(settings_db):
    for bad in ("", "x" * 81, "a\nb", None, 7):
        with pytest.raises(ValueError, match=nm.MSG_BAD_KEY):
            nm.set_muted("c1", bad, True)
    for i in range(nm.MAX_PER_PRINTER):
        nm.set_muted("c1", "hms:k%d" % i, True)
    with pytest.raises(ValueError, match="не больше 64"):
        nm.set_muted("c1", "hms:one-more", True)
    assert nm.set_muted("c1", "hms:k0", False)[0] is True               # unmuting still works
    assert nm.set_muted("p3", PE, True) == (True, [PE])                 # per printer
    assert nm.valid(settings_db.get("notification_mutes"))


def test_mark_sets_the_flag_on_every_record():
    recs = [_rec(LIDAR), _rec(MEM), "junk"]
    nm.mark("c1", recs, {"c1": [LIDAR], "p3": [MEM]})
    assert [r["muted"] for r in recs[:2]] == [True, False]
    for mutes in (None, {}, {"c1": LIDAR}, {"c1": None}, "x", {"c1": [None, 5]}):
        recs = [_rec(LIDAR)]
        nm.mark("c1", recs, mutes)
        assert recs[0]["muted"] is False, mutes
    nm.mark("c1", None, {"c1": [LIDAR]})                                # no records: nothing to do


# --- PUT /api/admin/printers/{id}/notification-mute ---------------------------------------

def test_put_mutes_and_unmutes_and_audits_the_change(env):
    r = _put(env, "c1", LIDAR, True)
    assert r.status_code == 200
    assert r.json() == {"printer_id": "c1", "key": LIDAR, "muted": True, "mutes": [LIDAR]}
    assert _put(env, "c1", LIDAR, True).json() == r.json()               # idempotent
    r = _put(env, "c1", LIDAR, False)
    assert r.json() == {"printer_id": "c1", "key": LIDAR, "muted": False, "mutes": []}
    assert _put(env, "c1", LIDAR, False).status_code == 200
    # a repeat is not audited; the line names the printer, the key and the direction
    assert env.audit == [("admin", "notif_mute:c1:%s:on" % LIDAR), ("admin", "notif_mute:c1:%s:off" % LIDAR)]


def test_put_refusals(env):
    r = _put(env, "nope", LIDAR, True)
    assert (r.status_code, r.json()["detail"]) == (404, "Принтер не найден")
    for bad in ("", "x" * 81, "hms:\t0C00"):
        r = _put(env, "c1", bad, True)
        assert (r.status_code, r.json()["detail"]) == (400, nm.MSG_BAD_KEY), bad
    assert _put(env, "c1", "x" * 80, True).status_code == 200
    assert env.client.put("/api/admin/printers/c1/notification-mute", json={"key": LIDAR}).status_code == 422
    assert env.client.put("/api/admin/printers/c1/notification-mute", json={"muted": True}).status_code == 422
    for i in range(nm.MAX_PER_PRINTER - 1):
        assert _put(env, "c1", "hms:k%d" % i, True).status_code == 200
    r = _put(env, "c1", LIDAR, True)
    assert r.status_code == 400 and "не больше 64" in r.json()["detail"]
    assert len(env.audit) == nm.MAX_PER_PRINTER                         # refusals are not audited
    assert all(a.startswith("notif_mute:c1:") and a.endswith(":on") for _u, a in env.audit)


def test_put_is_admin_only(env):
    env.role["r"] = UserRole.VIEWER
    r = _put(env, "c1", LIDAR, True)
    assert r.status_code == 403
    assert env.audit == [] and settings_service.get("notification_mutes") == {}
    assert _flags(env)["c1"][LIDAR] is False                            # the viewer still reads the flag


def test_put_touches_no_printer(env, monkeypatch):
    # «Скрыть» is the dashboard's attention, not a command: a busy / paused printer does not hold it
    from app.services import notification_actions, printer_commands

    def boom(*a, **k):
        raise AssertionError("a command was sent")

    monkeypatch.setattr(printer_commands, "send", boom)
    monkeypatch.setattr(notification_actions, "run", boom)
    assert _put(env, "p3", PE, True).status_code == 200


# --- the "muted" flag in /api/printers -----------------------------------------------------

def test_every_record_carries_the_muted_flag(env):
    assert _flags(env) == {"c1": {LIDAR: False, MEM: False}, "p3": {PE: False}}
    _put(env, "c1", LIDAR, True)
    assert _flags(env) == {"c1": {LIDAR: True, MEM: False}, "p3": {PE: False}}
    assert _flags(env, "c1") == {LIDAR: True, MEM: False}
    env.role["r"] = UserRole.VIEWER                                     # every logged-in user sees it
    assert _flags(env, "c1") == {LIDAR: True, MEM: False} and _flags(env)["c1"][LIDAR] is True
    # the store's records stay as the collector built them (to_dict copies)
    assert all("muted" not in n for n in env.c1.notifications)


def test_a_mute_outlives_its_code(env):
    # the printer stops reporting the code and reports it again later: still muted, nothing pruned
    _put(env, "c1", LIDAR, True)
    env.c1.notifications = [_rec(MEM, "common")]
    assert _flags(env)["c1"] == {MEM: False}
    assert settings_service.get("notification_mutes") == {"c1": [LIDAR]}
    env.c1.notifications = [_rec(LIDAR), _rec(MEM, "common")]
    assert _flags(env)["c1"] == {LIDAR: True, MEM: False}


def test_the_listing_reads_the_cached_setting(env, monkeypatch):
    _put(env, "c1", LIDAR, True)

    def no_db(*a, **k):
        raise AssertionError("the settings DB was opened")

    monkeypatch.setattr(settings_service, "_read_all", no_db)
    assert _flags(env)["c1"][LIDAR] is True and _flags(env, "c1")[LIDAR] is True


def test_an_unreadable_setting_mutes_nothing(env, monkeypatch):
    _put(env, "c1", LIDAR, True)

    def broken(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(settings_service, "get", broken)
    r = env.client.get("/api/printers")
    assert r.status_code == 200 and not any(n["muted"] for p in r.json() for n in p["notifications"])
    assert env.client.get("/api/printers/c1").status_code == 200


# --- the generic settings PUT never writes it -----------------------------------------------

@pytest.mark.parametrize("value", [{"c1": [LIDAR]}, {}, {"c1": ["x" * 81]}, None])
def test_generic_settings_put_refuses_the_mutes(env, value):
    # the whole-value write would race set_muted's lock (two admins) and skip the per-key audit
    _put(env, "p3", PE, True)
    env.audit.clear()
    for body in ({"notification_mutes": value}, {"notification_mutes": value, "proxy_check_interval": 900}):
        r = env.client.put("/api/admin/settings", json=body)
        assert (r.status_code, r.json()["detail"]) == (400, nm.MSG_SETTINGS_PUT), body
    assert settings_service.get("notification_mutes") == {"p3": [PE]}        # untouched
    assert settings_service.get("proxy_check_interval") == 600               # nothing of the body written
    assert env.audit == []
    assert _flags(env) == {"c1": {LIDAR: False, MEM: False}, "p3": {PE: True}}
