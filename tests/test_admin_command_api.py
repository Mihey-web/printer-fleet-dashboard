"""POST /api/admin/printers/{id}/command: request fields reach printer_commands.send.

Auth is bypassed by stubbing the token helpers the JWT middleware imports (the
pattern of test_ams_api), with the admin role the /api/admin/* gate requires.
"""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api import admin as api_admin
from app.api import auth as api_auth
from app.api import main as api_main
from app.domain.user_models import UserRole
from app.services import auth_service, printer_commands

_REAL_SEND = printer_commands.send


class _Store:
    def get_all(self):
        return []

    def get_one(self, pid):
        return None


@pytest.fixture
def api(monkeypatch):
    import config as cfg

    monkeypatch.setattr(cfg, "JWT_SECRET", "test-secret", raising=False)
    monkeypatch.setattr(auth_service, "get_user_from_token", lambda *a, **k: ("admin", UserRole.ADMIN))
    monkeypatch.setattr(auth_service, "decode_token", lambda *a, **k: {"iat": 0})
    monkeypatch.setattr(api_auth, "token_subject_active", lambda *a, **k: True)
    calls, audit = [], []
    reply = {"success": True, "status": "confirmed", "detail": "подтверждено принтером", "replied": True}
    monkeypatch.setattr(printer_commands, "send",
                        lambda pid, action, params: calls.append((pid, action, params)) or dict(reply))
    monkeypatch.setattr(api_admin, "get_audit_service",
                        lambda: SimpleNamespace(log=lambda user, action, *a, **k: audit.append(action)))
    client = TestClient(api_main.create_app(_Store()))
    client.cookies.set("access_token", "stub")
    return SimpleNamespace(client=client, calls=calls, audit=audit, reply=reply)


@pytest.mark.parametrize("body,params,audit", [
    ({"action": "ams_load", "ams": 1, "slot": 2}, {"ams": 1, "slot": 2},
     "printer_command:ams_load:b1:ams=1,slot=2,st=confirmed"),
    ({"action": "ams_unload"}, {}, "printer_command:ams_unload:b1:st=confirmed"),
    ({"action": "dry_start", "ams": 128, "temp": 65, "hours": 12, "filament": "PETG"},
     {"ams": 128, "temp": 65, "hours": 12, "filament": "PETG"},
     "printer_command:dry_start:b1:ams=128,temp=65,hours=12,filament=PETG,st=confirmed"),
    ({"action": "dry_stop", "ams": 0}, {"ams": 0}, "printer_command:dry_stop:b1:ams=0,st=confirmed"),
    # other commands: no parameters in the audit line, only the outcome
    ({"action": "chamber", "temp": 50}, {"temp": 50}, "printer_command:chamber:b1:st=confirmed"),
    # object skip is gone: obj_list is no request field any more (ignored)
    ({"action": "pause", "obj_list": [3, 7]}, {}, "printer_command:pause:b1:st=confirmed"),
])
def test_command_fields_reach_send_and_the_audit(api, body, params, audit):
    r = api.client.post("/api/admin/printers/b1/command", json=body)
    assert r.status_code == 200
    assert r.json() == {"success": True, "status": "confirmed", "detail": "подтверждено принтером", "replied": True}
    assert api.calls == [("b1", body["action"], params)]
    assert api.audit == [audit]


def test_sent_without_a_reply_is_a_200(api):
    api.reply.update(status="sent", detail="отправлено (без подтверждения)", replied=False)
    r = api.client.post("/api/admin/printers/b1/command", json={"action": "light_on"})
    assert r.status_code == 200
    assert r.json() == {"success": True, "status": "sent", "detail": "отправлено (без подтверждения)",
                        "replied": False}
    assert api.audit == ["printer_command:light_on:b1:st=sent"]


@pytest.mark.parametrize("status,detail", [
    ("refused", "Во время печати остудить нельзя — сначала пауза или стоп"),
    ("rejected", "🔒 прошивка блокирует сторонние команды"),
])
def test_refusal_is_a_400_with_the_reason_and_status(api, status, detail):
    api.reply.clear()
    api.reply.update(success=False, status=status, detail=detail, replied=status == "rejected")
    r = api.client.post("/api/admin/printers/b1/command", json={"action": "cooldown"})
    assert r.status_code == 400
    assert r.json() == {"detail": detail, "status": status}
    assert api.audit == ["printer_command:cooldown:b1:st=%s" % status]


def test_refused_command_is_a_400_with_the_reason(api):
    api.reply.clear()
    api.reply.update(success=False, detail="Слот 3 пуст")          # a reply without a status
    r = api.client.post("/api/admin/printers/b1/command", json={"action": "ams_load", "ams": 0, "slot": 2})
    assert r.status_code == 400
    assert r.json() == {"detail": "Слот 3 пуст", "status": "rejected"}


def test_unknown_action_reaches_send_and_is_refused(monkeypatch, api):
    # the real send(): no printer kind check passes for an unknown id, so stub the registry
    monkeypatch.setattr(printer_commands, "send", _REAL_SEND)
    monkeypatch.setattr("app.services.printer_registry.get_printer",
                        lambda pid: {"id": pid, "kind": "bambu", "model": "P2S"})
    r = api.client.post("/api/admin/printers/b1/command", json={"action": "skip_objects", "obj_list": [3]})
    assert r.status_code == 400
    assert r.json() == {"detail": "Неизвестная команда: skip_objects", "status": "refused"}


# --- settings: printer_light_enabled ---------------------------------------------------

@pytest.fixture
def settings_db(monkeypatch, tmp_path):
    from app.services import settings_service

    path = str(tmp_path / "settings.db")
    monkeypatch.setattr(settings_service, "DB_PATH", path)
    settings_service.init_db(path)
    settings_service.reset_cache()
    yield settings_service
    settings_service.reset_cache()


def test_printer_light_enabled_setting(api, settings_db):
    assert settings_db.get("printer_light_enabled") == []
    r = api.client.put("/api/admin/settings", json={"printer_light_enabled": ["creality-1"]})
    assert r.status_code == 200
    assert r.json()["values"]["printer_light_enabled"] == ["creality-1"]
    assert settings_db.get("printer_light_enabled") == ["creality-1"]
    assert r.json()["changed"] == ["printer_light_enabled"] and r.json()["bot_restarting"] is False


@pytest.mark.parametrize("value", ["creality-1", ["creality-1", 5], [""], ["x" * 65], [None],
                                   ["c%d" % i for i in range(201)]])
def test_printer_light_enabled_must_be_a_list_of_ids(api, settings_db, value):
    r = api.client.put("/api/admin/settings", json={"printer_light_enabled": value})
    assert r.status_code == 400
    assert r.json() == {"detail": "printer_light_enabled: ожидается список id принтеров"}
    assert settings_db.get("printer_light_enabled") == []


def test_debug_endpoint_carries_the_live_raw_report_in_any_state(monkeypatch, offline_collector):
    # For checking AMS / filament-change telemetry on a real printer: the bit
    # fields the lib drops, also while idle (the debug blob is printing-only).
    import bambu_testkit as kit
    import config as cfg
    from app.domain.models import PrinterKind, PrinterState, PrinterStatus

    collector = offline_collector("P2S")
    kit.deliver(collector, {"print": {"command": "push_status", "msg": 0, "gcode_state": "IDLE", "fun2": "3973",
                                      "ams_status": 768, "ams": {"ams": [{"id": "0", "humidity": "3", "temp": "25",
                                                                         "info": "10942023", "dry_sf_reason": [6],
                                                                         "tray": []}]},
                                      "device": {"extruder": {"state": 1, "info": [{"id": 0, "snow": 1, "info": 10}]}}}})

    class _Store:
        def get_all(self):
            return []

        def get_one(self, pid):
            return PrinterStatus(id=pid, label="B1", kind=PrinterKind.BAMBU, online=True, state=PrinterState.IDLE)

        def get_collector(self, pid):
            return collector

    monkeypatch.setattr(cfg, "JWT_SECRET", "test-secret", raising=False)
    monkeypatch.setattr(auth_service, "get_user_from_token", lambda *a, **k: ("admin", UserRole.ADMIN))
    monkeypatch.setattr(auth_service, "decode_token", lambda *a, **k: {"iat": 0})
    monkeypatch.setattr(api_auth, "token_subject_active", lambda *a, **k: True)
    client = TestClient(api_main.create_app(_Store()))
    client.cookies.set("access_token", "stub")
    body = client.get("/api/debug/printers/b1").json()
    raw = body["raw"]
    assert body["debug"] == {} and body["state"] == "idle"
    assert (raw["fun2"], raw["ams_status"], raw["ext_state"], raw["busy"]) == (0x3973, 768, 1, False)
    assert raw["ams"] == {"0": {"info": 0x10942023, "sf": [6]}}
    assert raw["extruders"] == {"0": {"snow": 1, "info": 10}}
    assert (raw["active_ams_index"], raw["active_tray_index"], raw["gcode_state"]) == (0, 1, "IDLE")


# --- bulk commands: POST /api/admin/printers/bulk-command ------------------------------

from app.domain.models import PrinterKind, PrinterState, PrinterStatus
from app.services import fleet_commands


class _FleetStore:
    def __init__(self, statuses):
        self._s = statuses

    def get_all(self):
        return list(self._s)

    def get_one(self, pid):
        return next((s for s in self._s if s.id == pid), None)

    def get_collector(self, pid):
        return None


_BULK_REPLIES = {
    "b1": {"success": True, "status": "confirmed", "detail": "подтверждено принтером", "replied": True},
    "b2": {"success": False, "status": "rejected", "detail": "🔒 прошивка блокирует сторонние команды",
           "replied": True},
}


@pytest.fixture
def bulk(api, monkeypatch):
    statuses = [PrinterStatus(id=pid, label=pid, kind=PrinterKind.BAMBU, online=True, state=state,
                              device_type="P2S")
                for pid, state in (("b1", PrinterState.PRINTING), ("b2", PrinterState.PRINTING),
                                   ("b3", PrinterState.IDLE))]
    printer_commands.set_store(_FleetStore(statuses))
    monkeypatch.setattr("app.services.printer_registry.list_printers",
                        lambda *a, **k: [{"id": "b1", "label": "P1"}, {"id": "b2", "label": "P2"},
                                         {"id": "b3", "label": "P3"}])
    monkeypatch.setattr(printer_commands, "send",
                        lambda pid, action, params: api.calls.append((pid, action, params)) or dict(_BULK_REPLIES[pid]))
    printer_commands._capability.clear()
    for pid in ("b1", "b2", "b3"):
        printer_commands._capability[pid] = {"print_cmds": True, "via": "fun", "ts": 0}
    yield api
    printer_commands._capability.clear()
    printer_commands.set_store(None)


def test_bulk_command_results_and_audit(bulk):
    r = bulk.client.post("/api/admin/printers/bulk-command",
                         json={"action": "pause", "printer_ids": ["b1", "b2", "b3"]})
    assert r.status_code == 200
    body = r.json()
    batch = body["batch"]
    assert (body["action"], body["dry_run"]) == ("pause", False)
    assert body["results"] == [
        {"id": "b1", "label": "P1", "ok": True, "status": "confirmed", "detail": "подтверждено принтером"},
        {"id": "b2", "label": "P2", "ok": False, "status": "rejected",
         "detail": "🔒 прошивка блокирует сторонние команды"},
        {"id": "b3", "label": "P3", "ok": False, "status": "skipped", "detail": "принтер не печатает"}]
    assert body["summary"] == {"confirmed": 1, "sent": 0, "rejected": 1, "skipped": 1, "dry_run": 0}
    assert sorted(bulk.calls) == [("b1", "pause", {}), ("b2", "pause", {})]
    assert bulk.audit == ["printer_command:pause:b1:bulk=%s,st=confirmed" % batch,
                          "printer_command:pause:b2:bulk=%s,st=rejected" % batch,
                          "printer_bulk:pause:%s:confirmed=1,sent=0,rejected=1,skipped=1" % batch]


def test_bulk_dry_run_sends_and_audits_nothing(bulk):
    r = bulk.client.post("/api/admin/printers/bulk-command",
                         json={"action": "pause", "printer_ids": ["b1", "b3"], "dry_run": True})
    assert r.status_code == 200
    assert [(x["status"], x["detail"]) for x in r.json()["results"]] == [
        ("dry_run", "будет отправлено"), ("skipped", "принтер не печатает")]
    assert bulk.calls == [] and bulk.audit == []


@pytest.mark.parametrize("body,code,detail", [
    ({"action": "stop", "printer_ids": ["b1"]}, 400, "Групповое действие: light_on, light_off, pause или resume"),
    ({"action": "pause", "printer_ids": []}, 400, "Не выбран ни один принтер"),
    ({"action": "pause", "printer_ids": ["p%d" % i for i in range(65)]}, 400, "За раз — не больше 64 принтеров"),
])
def test_bulk_command_bad_requests(bulk, body, code, detail):
    r = bulk.client.post("/api/admin/printers/bulk-command", json=body)
    assert (r.status_code, r.json()) == (code, {"detail": detail})
    assert bulk.calls == [] and bulk.audit == []


def test_bulk_command_wrong_types_are_a_422(bulk):
    r = bulk.client.post("/api/admin/printers/bulk-command", json={"action": "pause", "printer_ids": "x"})
    assert r.status_code == 422
    assert bulk.calls == []


def test_bulk_command_busy_is_a_409(bulk):
    lock = fleet_commands._busy["print"]
    assert lock.acquire(blocking=False)
    try:
        r = bulk.client.post("/api/admin/printers/bulk-command", json={"action": "resume", "printer_ids": ["b1"]})
        assert (r.status_code, r.json()) == (409, {"detail": "Групповая команда уже выполняется — дождитесь окончания"})
        # another family is not blocked
        r = bulk.client.post("/api/admin/printers/bulk-command",
                             json={"action": "light_on", "printer_ids": ["b1"], "dry_run": True})
        assert r.status_code == 200
    finally:
        lock.release()


def test_viewer_cannot_send_commands(api, monkeypatch):
    monkeypatch.setattr(auth_service, "get_user_from_token", lambda *a, **k: ("viewer", UserRole.VIEWER))
    r1 = api.client.post("/api/admin/printers/b1/command", json={"action": "pause"})
    r2 = api.client.post("/api/admin/printers/bulk-command", json={"action": "pause", "printer_ids": ["b1"]})
    assert (r1.status_code, r2.status_code) == (403, 403)
    assert api.calls == [] and api.audit == []
