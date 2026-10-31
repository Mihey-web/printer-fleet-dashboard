"""Maintenance mode over the API (spec_maintenance §4): the `maintenance` field
of /api/printers, /api/printers/{id} and /api/ams/current, and the admin
switch PUT /api/admin/printers/{id}/maintenance (audit, out-of-band snapshot).

The registry is a real SQLite file under tmp (printer_registry.DB_PATH
patched); auth is stubbed as in test_admin_command_api, the role switchable.
"""
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api import auth as api_auth
from app.api import main as api_main
from app.domain.models import PrinterKind, PrinterState, PrinterStatus
from app.domain.user_models import UserRole
from app.services import auth_service, printer_registry, settings_service

PRINTERS = [("k1", "klipper", "Varan"), ("b1", "bambu", "P1"), ("b2", "bambu", "P2")]


def _ams(pid):
    return PrinterStatus(id=pid, label="x", kind=PrinterKind.BAMBU, online=True, state=PrinterState.IDLE,
                         device_type="P2S", ams={"tray_now": None, "units": [{"humidity": 2, "slots": [{"empty": True}]}]})


class _Store:
    def __init__(self, statuses):
        self._s = {s.id: s for s in statuses}

    def get_all(self):
        return list(self._s.values())

    def get_one(self, pid):
        return self._s.get(pid)


@pytest.fixture
def env(tmp_path, monkeypatch):
    import config as cfg

    db = str(tmp_path / "printer_history.db")
    monkeypatch.setattr(printer_registry, "DB_PATH", db)
    printer_registry._invalidate_maintenance()
    printer_registry._invalidate_label()
    printer_registry.init_db(db)
    conn = sqlite3.connect(db)
    conn.executemany("INSERT INTO printers (id, kind, label, host, deleted, created_at, updated_at)"
                     " VALUES (?, ?, ?, 'h', 0, 1.0, 1.0)", PRINTERS)
    conn.commit()
    conn.close()

    role = {"r": UserRole.ADMIN}
    monkeypatch.setattr(cfg, "JWT_SECRET", "test-secret", raising=False)
    monkeypatch.setattr(auth_service, "get_user_from_token", lambda *a, **k: ("admin", role["r"]))
    monkeypatch.setattr(auth_service, "decode_token", lambda *a, **k: {"iat": 0})
    monkeypatch.setattr(api_auth, "token_subject_active", lambda *a, **k: True)
    monkeypatch.setattr(settings_service, "get", lambda key, **k: None)
    store = _Store([PrinterStatus(id="k1", label="x", kind=PrinterKind.KLIPPER, online=False,
                                  state=PrinterState.OFFLINE), _ams("b1"), _ams("b2")])
    client = TestClient(api_main.create_app(store))
    client.cookies.set("access_token", "stub")
    yield SimpleNamespace(client=client, db=db, role=role, store=store)
    printer_registry._invalidate_maintenance()
    printer_registry._invalidate_label()


def test_printers_carry_the_maintenance_mode_and_keep_the_live_state(env):
    printer_registry.set_maintenance("k1", True, note="меняю хотенд", by="admin", now=1000.0)
    env.role["r"] = UserRole.VIEWER                          # every logged-in user sees the plaque
    rows = {r["id"]: r for r in env.client.get("/api/printers").json()}
    assert rows["k1"]["maintenance"] == {"since": 1000.0, "note": "меняю хотенд", "by": "admin"}
    assert (rows["k1"]["state"], rows["k1"]["label"]) == ("offline", "Varan")    # the live state stays
    assert rows["b1"]["maintenance"] is None and rows["b2"]["maintenance"] is None
    one = env.client.get("/api/printers/k1").json()
    assert one["maintenance"] == {"since": 1000.0, "note": "меняю хотенд", "by": "admin"}
    assert one["label"] == "Varan" and "debug" not in one
    assert env.client.get("/api/printers/b1").json()["maintenance"] is None


def test_printers_without_a_readable_registry_show_no_maintenance(env, monkeypatch):
    printer_registry.set_maintenance("k1", True)

    def broken(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(printer_registry, "list_printers", broken)
    r = env.client.get("/api/printers")
    assert r.status_code == 200 and all(p["maintenance"] is None for p in r.json())


def test_ams_current_flags_the_printer_in_maintenance(env):
    printer_registry.set_maintenance("b2", True)
    units = {u["printer_id"]: u for u in env.client.get("/api/ams/current").json()}
    assert (units["b1"]["maintenance"], units["b2"]["maintenance"]) == (False, True)


# --- PUT /api/admin/printers/{id}/maintenance (§4.2) ------------------------------

@pytest.fixture
def audit(monkeypatch):
    from app.api import admin as api_admin

    log = []
    monkeypatch.setattr(api_admin, "get_audit_service",
                        lambda: SimpleNamespace(log=lambda user, action, *a, **k: log.append((user, action))))
    return log


def _put(env, pid, **body):
    return env.client.put("/api/admin/printers/%s/maintenance" % pid, json=body)


def test_put_on_note_off_answers_the_change_and_audits_without_the_note(env, audit):
    r = _put(env, "k1", on=True, note="  меняю\nхотенд\t\t сопло 0.4\x00 ")
    assert r.status_code == 200
    body = r.json()
    since = body["maintenance"]["since"]
    assert body == {"printer_id": "k1", "change": "on",
                    "maintenance": {"since": since, "note": "меняю хотенд сопло 0.4", "by": "admin"}}
    assert isinstance(since, float)
    r = _put(env, "k1", on=True, note="жду детали")
    assert r.json() == {"printer_id": "k1", "change": "note",
                        "maintenance": {"since": since, "note": "жду детали", "by": "admin"}}
    assert _put(env, "k1", on=True, note="жду детали").json()["change"] == "none"     # idempotent
    assert _put(env, "k1", on=True, note="   ").json()["maintenance"]["note"] is None   # blank -> NULL
    assert _put(env, "k1", on=False, note="ignored").json() == {"printer_id": "k1", "change": "off",
                                                                "maintenance": None}
    assert _put(env, "k1", on=False).json()["change"] == "none"
    assert audit == [("admin", "printer_maintenance:k1:on"), ("admin", "printer_maintenance:k1:note"),
                     ("admin", "printer_maintenance:k1:note"), ("admin", "printer_maintenance:k1:off")]
    assert not any("хотенд" in a or "детали" in a for _u, a in audit)   # the note never reaches the audit
    assert printer_registry.maintenance_of("k1") is None


def test_put_switch_shows_in_the_printer_list_at_once(env, audit):
    _put(env, "b1", on=True, note="чистка")
    rows = {p["id"]: p for p in env.client.get("/api/printers").json()}
    assert rows["b1"]["maintenance"]["note"] == "чистка" and rows["b1"]["maintenance"]["by"] == "admin"
    # the admin table: the registry row carries the columns, no pending change
    adm = {p["id"]: p for p in env.client.get("/api/admin/printers").json()}
    assert adm["b1"]["maintenance_note"] == "чистка" and adm["b1"]["maintenance_by"] == "admin"
    assert adm["b1"]["maintenance_since"] is not None and adm["k1"]["maintenance_since"] is None
    _put(env, "b1", on=False)
    rows = {p["id"]: p for p in env.client.get("/api/printers").json()}
    assert rows["b1"]["maintenance"] is None


def test_put_refusals(env, audit):
    assert _put(env, "nope", on=True).status_code == 404
    assert _put(env, "nope", on=True).json()["detail"] == "Принтер не найден"
    r = _put(env, "k1", on=True, note="x" * 201)
    assert (r.status_code, r.json()["detail"]) == (400, "Заметка — не длиннее 200 символов")
    assert _put(env, "k1", on=True, note="x" * 200).status_code == 200
    assert _put(env, "k1", on=True, note="\n".join(["y" * 100, "y" * 99])).status_code == 200  # 200 after the join
    conn = sqlite3.connect(env.db)
    conn.execute("UPDATE printers SET deleted = 1 WHERE id = 'b2'")
    conn.commit()
    conn.close()
    r = _put(env, "b2", on=True)
    assert (r.status_code, r.json()["detail"]) == (400, "Принтер помечен на удаление")
    assert _put(env, "k1", note="x").status_code == 422                 # `on` is required
    assert audit == [("admin", "printer_maintenance:k1:on"), ("admin", "printer_maintenance:k1:note")]
    assert not printer_registry.in_maintenance("b2")


def test_put_is_admin_only(env, audit):
    env.role["r"] = UserRole.VIEWER
    r = _put(env, "k1", on=True)
    assert r.status_code == 403
    assert audit == [] and printer_registry.maintenance_map() == {}


def test_put_writes_a_history_row_at_the_switch(env, audit, tmp_path, monkeypatch):
    """The out-of-band snapshot: the segment bound on the timeline is the switch
    itself, through the StateStore hook main() wires."""
    import main
    from app.services import printer_commands, state_store

    monkeypatch.setattr(state_store, "DB_PATH", env.db)       # one file, as in production
    store = state_store.StateStore(history_state=main._history_state)
    store.upsert(PrinterStatus(id="k1", label="Varan", kind=PrinterKind.KLIPPER, online=False,
                               state=PrinterState.OFFLINE))
    monkeypatch.setattr(printer_commands, "get_store", lambda: store)

    def rows():
        return store._db.execute("SELECT printer_id, state FROM printer_history ORDER BY id").fetchall()

    try:
        _put(env, "k1", on=True)
        assert rows() == [("k1", "maintenance")]
        _put(env, "k1", on=True, note="заметка")          # a note edit moves no bound
        _put(env, "k1", on=True, note="заметка")          # nor does a repeat
        assert rows() == [("k1", "maintenance")]
        _put(env, "k1", on=False)
        assert rows() == [("k1", "maintenance"), ("k1", "offline")]
        _put(env, "b1", on=True)                          # not in the store: no row, still a 200
        assert len(rows()) == 2
    finally:
        store._db.close()


def test_put_survives_a_failing_snapshot(env, audit, monkeypatch):
    from app.services import printer_commands

    def broken():
        raise RuntimeError("no store")

    monkeypatch.setattr(printer_commands, "get_store", broken)
    r = _put(env, "k1", on=True)
    assert r.status_code == 200 and r.json()["change"] == "on"
    assert printer_registry.in_maintenance("k1")
