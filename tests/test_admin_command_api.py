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


def test_creality_command_end_to_end_is_sent_and_audited(api, monkeypatch):
    # the real send(): the request fields become Creality "set" frames, the verdict is
    # «sent» (the printer never acknowledges), the audit line is the one Bambu gets
    from app.services import settings_service

    frames = []
    collector = SimpleNamespace(
        client=SimpleNamespace(get_data=lambda: {"state": "printing", "progress": 40, "remaining_time": 600,
                                                 "nozzle_temp": 215, "fan_speed_pct": 100, "case_fan_pct": 0,
                                                 "aux_fan_pct": 0, "max_nozzle_temp": 300, "max_bed_temp": 100}),
        send_command=lambda command, param=None: frames.append((command, param)) or {"success": True})
    monkeypatch.setattr(printer_commands, "send", _REAL_SEND)
    monkeypatch.setattr(printer_commands, "_store", SimpleNamespace(get_collector=lambda pid: collector,
                                                                    get_one=lambda pid: None))
    monkeypatch.setattr(settings_service, "get", lambda key, **k: [] if key == "printer_light_disabled" else {})
    monkeypatch.setattr("app.services.printer_registry.get_printer",
                        lambda pid: {"id": pid, "kind": "creality", "label": "K1", "model": "k1c"})
    r = api.client.post("/api/admin/printers/creality-3/command", json={"action": "preheat", "nozzle": 230})
    assert r.status_code == 200
    assert r.json() == {"success": True, "status": "sent", "detail": "отправлено (без подтверждения)", "replied": False}
    r = api.client.post("/api/admin/printers/creality-3/command", json={"action": "speed", "level": 4})
    assert r.status_code == 200 and r.json()["status"] == "sent"
    r = api.client.post("/api/admin/printers/creality-3/command", json={"action": "fans", "aux": 100})
    assert r.status_code == 200 and r.json()["status"] == "sent"
    assert frames == [("set", [{"nozzleTempControl": 230}]), ("set", [{"setFeedratePct": 166}]),
                      ("set", [{"gcodeCmd": "M106 P2 S255"}])]
    r = api.client.post("/api/admin/printers/creality-3/command", json={"action": "cooldown"})
    assert r.status_code == 400
    assert r.json() == {"detail": "Во время печати остудить нельзя — сначала пауза или стоп", "status": "refused"}
    assert len(frames) == 3
    assert api.audit == ["printer_command:preheat:creality-3:st=sent", "printer_command:speed:creality-3:st=sent",
                         "printer_command:fans:creality-3:st=sent", "printer_command:cooldown:creality-3:st=refused"]


# --- notification buttons: POST /api/admin/printers/{id}/notification-action ------------------

@pytest.fixture
def notif(api, monkeypatch):
    from app.services import notification_actions
    api.runs = []
    api.result = dict(api.reply)
    monkeypatch.setattr(notification_actions, "run", lambda pid, key, action, confirmed=False, source="user":
                        api.runs.append((pid, key, action, confirmed)) or dict(api.result))
    return api


def test_notification_action_runs_and_is_audited(notif):
    r = notif.client.post("/api/admin/printers/b1/notification-action", json={"key": "pe:0500_8062", "action": "ignore"})
    assert r.status_code == 200
    assert r.json() == {"success": True, "status": "confirmed", "detail": "подтверждено принтером", "replied": True}
    assert notif.runs == [("b1", "pe:0500_8062", "ignore", False)]
    assert notif.audit == ["printer_command:notif_ignore:b1:code=0500_8062,st=confirmed"]
    r = notif.client.post("/api/admin/printers/k1/notification-action",
                          json={"key": "klipper:shutdown", "action": "fw_restart", "confirm": True})
    assert r.status_code == 200 and notif.runs[-1] == ("k1", "klipper:shutdown", "fw_restart", True)
    assert notif.audit[-1] == "printer_command:notif_fw_restart:k1:code=shutdown,st=confirmed"


def test_notification_action_refusal_is_a_400_with_the_status(notif):
    notif.result = {"success": False, "status": "refused", "detail": "Нужно подтверждение", "replied": False}
    r = notif.client.post("/api/admin/printers/b1/notification-action", json={"key": "pe:0500_8062", "action": "stop"})
    assert (r.status_code, r.json()) == (400, {"detail": "Нужно подтверждение", "status": "refused"})
    assert notif.audit == ["printer_command:notif_stop:b1:code=0500_8062,st=refused"]


@pytest.mark.parametrize("body", [
    {"key": "x:0500_8062", "action": "ignore"}, {"key": "pe:", "action": "ignore"},
    {"key": "pe:0500 8062", "action": "ignore"}, {"key": "pe:0500_8062\n", "action": "ignore"},
    {"key": "pe:" + "1" * 65, "action": "ignore"}, {"key": "pe:0500_8062", "action": "Ignore"},
    {"key": "pe:0500_8062", "action": "ignore:x"}, {"key": "pe:0500_8062", "action": ""},
])
def test_notification_action_bad_request(notif, body):
    r = notif.client.post("/api/admin/printers/b1/notification-action", json=body)
    assert (r.status_code, r.json()) == (400, {"detail": "Неверный ключ уведомления или действие"})
    assert notif.runs == [] and notif.audit == []


def test_notification_action_reaches_the_real_run(monkeypatch, api):
    # no live telemetry: refused by run() itself, and audited
    monkeypatch.setattr("app.services.printer_registry.get_printer", lambda pid: {"id": pid, "kind": "bambu"})
    monkeypatch.setattr(printer_commands, "_store", None)
    r = api.client.post("/api/admin/printers/b1/notification-action", json={"key": "pe:0500_8062", "action": "ignore"})
    assert (r.status_code, r.json()) == (400, {"detail": printer_commands.MSG_NO_TELEMETRY, "status": "refused"})
    assert api.audit == ["printer_command:notif_ignore:b1:code=0500_8062,st=refused"]


def test_viewer_cannot_press_notification_buttons(notif, monkeypatch):
    monkeypatch.setattr(auth_service, "get_user_from_token", lambda *a, **k: ("viewer", UserRole.VIEWER))
    r = notif.client.post("/api/admin/printers/b1/notification-action", json={"key": "pe:0500_8062", "action": "ignore"})
    assert r.status_code == 403 and notif.runs == [] and notif.audit == []


# --- settings: printer_light_disabled ---------------------------------------------------

@pytest.fixture
def settings_db(monkeypatch, tmp_path):
    from app.services import settings_service

    path = str(tmp_path / "settings.db")
    monkeypatch.setattr(settings_service, "DB_PATH", path)
    settings_service.init_db(path)
    settings_service.reset_cache()
    yield settings_service
    settings_service.reset_cache()


def test_printer_light_disabled_setting(api, settings_db):
    assert settings_db.get("printer_light_disabled") == []
    r = api.client.put("/api/admin/settings", json={"printer_light_disabled": ["creality-1"]})
    assert r.status_code == 200
    assert r.json()["values"]["printer_light_disabled"] == ["creality-1"]
    assert settings_db.get("printer_light_disabled") == ["creality-1"]
    assert r.json()["changed"] == ["printer_light_disabled"] and r.json()["bot_restarting"] is False
    # the old opt-in list is gone: a Creality has its light unless listed here
    r = api.client.put("/api/admin/settings", json={"printer_light_enabled": ["creality-1"]})
    assert r.status_code == 400 and r.json() == {"detail": "Unknown settings keys: printer_light_enabled"}


@pytest.mark.parametrize("value", ["creality-1", ["creality-1", 5], [""], ["x" * 65], [None],
                                   ["c%d" % i for i in range(201)]])
def test_printer_light_disabled_must_be_a_list_of_ids(api, settings_db, value):
    r = api.client.put("/api/admin/settings", json={"printer_light_disabled": value})
    assert r.status_code == 400
    assert r.json() == {"detail": "printer_light_disabled: ожидается список id принтеров"}
    assert settings_db.get("printer_light_disabled") == []


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


def test_debug_endpoint_carries_the_notification_fields(monkeypatch, offline_collector):
    # print_error and its time, hms[] as a list with ts_* and first-seen times, the
    # cancel latch: in any state (here FAILED after a cancel, where the debug blob is empty).
    import bambu_testkit as kit
    import config as cfg
    from app.domain.models import PrinterKind, PrinterState, PrinterStatus

    collector = offline_collector("P2S")
    kit.deliver(collector, {"print": {"command": "push_status", "msg": 0, "gcode_state": "RUNNING",
                                      "print_error": 0x0300400C, "subtask_id": "", "job_attr": 18,
                                      "fail_reason": "0", "err": "0",
                                      "hms": [{"attr": 0x05000600, "code": 0x00020070,
                                               "ts_unix": "20261003152025", "ts_boot": 34642}]}})
    kit.deliver(collector, {"print": {"command": "push_status", "msg": 0, "gcode_state": "FAILED", "print_error": 0}})

    class _Store:
        def get_all(self):
            return []

        def get_one(self, pid):
            return PrinterStatus(id=pid, label="B1", kind=PrinterKind.BAMBU, online=True, state=PrinterState.CANCELLED)

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
    assert body["state"] == "cancelled"
    assert (raw["print_error"], raw["print_error_since"], raw["cancel_latched"]) == (0, None, True)
    assert (raw["subtask_id"], raw["job_attr"], raw["fail_reason"], raw["err"]) == ("", 18, "0", "0")
    assert raw["gcode_state"] == "FAILED"
    [hms] = raw["hms"]
    assert {k: hms[k] for k in ("attr", "code", "ts_unix", "ts_boot")} == \
        {"attr": 0x05000600, "code": 0x00020070, "ts_unix": "20261003152025", "ts_boot": 34642}
    assert isinstance(hms["first_seen"], float)


def test_debug_endpoint_carries_the_notification_tracker(monkeypatch, tmp_path):
    # What `since` shows, kept across restarts, and the state flags: for the live check (spec §10.2).
    import config as cfg
    from app.domain.models import PrinterKind, PrinterState, PrinterStatus
    from app.services import notification_tracker as nt

    tracker = nt.NotificationTracker(str(tmp_path / "ph.db"))
    monkeypatch.setattr(nt, "_tracker", tracker)
    status = PrinterStatus(id="b1", label="B1", kind=PrinterKind.BAMBU, online=True, state=PrinterState.CANCELLED,
                           notifications=[{"key": "pe:0300_400C", "src": "print_error", "code": "0300_400C",
                                           "severity": "common", "seen_at": 1000.0}])
    tracker.annotate(status, now=1010.0)

    class _Store:
        def get_all(self):
            return []

        def get_one(self, pid):
            return status

    monkeypatch.setattr(cfg, "JWT_SECRET", "test-secret", raising=False)
    monkeypatch.setattr(auth_service, "get_user_from_token", lambda *a, **k: ("admin", UserRole.ADMIN))
    monkeypatch.setattr(auth_service, "decode_token", lambda *a, **k: {"iat": 0})
    monkeypatch.setattr(api_auth, "token_subject_active", lambda *a, **k: True)
    client = TestClient(api_main.create_app(_Store()))
    client.cookies.set("access_token", "stub")
    try:
        body = client.get("/api/debug/printers/b1").json()
    finally:
        tracker.close()
    assert body["tracker"] == {"active": [{"key": "pe:0300_400C", "first_seen": 1000.0, "gone_at": None}],
                               "flags": ["state:cancelled"]}
    assert body["raw"] is None


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
    ({"action": "stop", "printer_ids": ["b1"]}, 400, "Групповое действие: light_on, light_off, pause, resume или reset_errors"),
    ({"action": "pause", "printer_ids": []}, 400, "Не выбран ни один принтер"),
    ({"action": "pause", "printer_ids": ["p%d" % i for i in range(65)]}, 400, "За раз — не больше 64 принтеров"),
])
def test_bulk_command_bad_requests(bulk, body, code, detail):
    r = bulk.client.post("/api/admin/printers/bulk-command", json=body)
    assert (r.status_code, r.json()) == (code, {"detail": detail})
    assert bulk.calls == [] and bulk.audit == []


def test_bulk_reset_errors_dry_run(bulk):
    r = bulk.client.post("/api/admin/printers/bulk-command",
                         json={"action": "reset_errors", "printer_ids": ["b1", "b3"], "dry_run": True})
    assert r.status_code == 200
    assert [(x["status"], x["detail"]) for x in r.json()["results"]] == [
        ("skipped", "уведомлений нет"), ("skipped", "уведомлений нет")]
    assert bulk.calls == [] and bulk.audit == []


def test_bulk_reset_errors_passes_the_confirmed_records(bulk, monkeypatch):
    seen = {}

    def run_bulk(action, ids, **kw):
        seen.update(kw, action=action, ids=ids)
        return {"action": action, "batch": "b", "dry_run": True, "results": [], "summary": {}}

    monkeypatch.setattr(fleet_commands, "run_bulk", run_bulk)
    r = bulk.client.post("/api/admin/printers/bulk-command",
                         json={"action": "reset_errors", "printer_ids": ["b1"], "keys": {"b1": "pe:0300_400C"}})
    assert r.status_code == 200 and seen["keys"] == {"b1": "pe:0300_400C"}
    r = bulk.client.post("/api/admin/printers/bulk-command", json={"action": "pause", "printer_ids": ["b1"]})
    assert r.status_code == 200 and seen["keys"] is None
    r = bulk.client.post("/api/admin/printers/bulk-command",
                         json={"action": "reset_errors", "printer_ids": ["b1"], "keys": ["pe:x"]})
    assert r.status_code == 422


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


def test_hms_catalog_status_is_admin_only(api, monkeypatch):
    from app.services import hms_catalog
    st = {"22E": {"action_ver": "1", "text_ver": "2", "checked_at": 1.0, "n_actions": 3, "n_err": 4,
                  "n_hms": 5, "error": None, "printers": 1}}
    monkeypatch.setattr(hms_catalog, "status", lambda: st)
    r = api.client.get("/api/admin/hms-catalog")
    assert (r.status_code, r.json()) == (200, st)
    monkeypatch.setattr(auth_service, "get_user_from_token", lambda *a, **k: ("viewer", UserRole.VIEWER))
    assert api.client.get("/api/admin/hms-catalog").status_code == 403
    assert api.audit == []


def test_bulk_command_skips_a_printer_in_maintenance(bulk, monkeypatch):
    # spec_maintenance §4.1: the dry run and the real one say the same, nothing reaches it
    monkeypatch.setattr("app.services.printer_registry.maintenance_map",
                        lambda *a, **k: {"b1": {"since": 1000.0, "note": None, "by": "admin"}})
    r = bulk.client.post("/api/admin/printers/bulk-command",
                         json={"action": "pause", "printer_ids": ["b1", "b2"], "dry_run": True})
    assert [(x["id"], x["status"], x["detail"]) for x in r.json()["results"]] == [
        ("b1", "skipped", "на обслуживании"), ("b2", "dry_run", "будет отправлено")]
    r = bulk.client.post("/api/admin/printers/bulk-command", json={"action": "pause", "printer_ids": ["b1", "b2"]})
    assert r.json()["results"][0] == {"id": "b1", "label": "P1", "ok": False, "status": "skipped",
                                      "detail": "на обслуживании"}
    assert bulk.calls == [("b2", "pause", {})]
    assert not any(":b1:" in a for a in bulk.audit)                     # skipped: no command row for it


# --- «Автоматика → Автопропуск ошибок» (IA §8, plan step 13) --------------------------------

def test_auto_ignore_codes_are_saved_and_checked_by_the_settings_put(api, settings_db):
    # the editor only calls the existing check (settings PUT): a good list is stored as is
    r = api.client.put("/api/admin/settings", json={"auto_ignore_print_errors": ["0500_8062", "0C00_8009", "0300_400C"]})
    assert r.status_code == 200 and r.json()["values"]["auto_ignore_print_errors"] == ["0500_8062", "0C00_8009", "0300_400C"]
    for bad in (["0500-8062"], ["0500_806"], ["0500_806g"], "0500_8062", ["0500_%04X" % i for i in range(33)]):
        r = api.client.put("/api/admin/settings", json={"auto_ignore_print_errors": bad})
        assert r.status_code == 400 and r.json() == {"detail": "auto_ignore_print_errors: список кодов вида 0500_8062"}, bad
    assert settings_db.get("auto_ignore_print_errors") == ["0500_8062", "0C00_8009", "0300_400C"]


def test_print_error_texts_in_the_fleet_models_words(api, monkeypatch):
    from app.services import notifications, printer_registry as reg
    rows = [{"id": "b1", "kind": "bambu", "model": "P2S", "serial": "22E00A000000001"},
            {"id": "b2", "kind": "bambu", "model": "X1C", "serial": None},
            {"id": "b3", "kind": "bambu", "model": "P2S", "serial": "22E00A000000002"},   # same prefix: asked once
            {"id": "k1", "kind": "klipper", "model": "Varan", "serial": None}]
    monkeypatch.setattr(reg, "list_printers", lambda *a, **k: rows)
    asked = []

    def fake(value, prefix, model):
        asked.append(("%08X" % value, prefix, model))
        return {("05008062", "00M"): "Маркер стола не найден", ("0C008009", "22E"): "Нет маркера"}.get(("%08X" % value, prefix))

    monkeypatch.setattr(notifications, "_pe_text", fake)
    r = api.client.get("/api/admin/print-error-texts?codes=0500_8062,0c00_8009,FFFF_FFFF,0500_8062")
    assert r.status_code == 200
    assert r.json() == {"texts": {"0500_8062": "Маркер стола не найден", "0C00_8009": "Нет маркера", "FFFF_FFFF": None}}
    # the P2S prefix first (a model's text when it has one), the X1C next; no printer is asked anything
    assert asked[:2] == [("05008062", "22E", "P2S"), ("05008062", "00M", "X1C")] and ("0C008009", "22E", "P2S") in asked
    assert api.calls == []
    assert api.client.get("/api/admin/print-error-texts").json() == {"texts": {}}


@pytest.mark.parametrize("codes", ["0500-8062", "x", "0500_8062," + ",".join("0500_%04X" % i for i in range(32))])
def test_print_error_texts_refuses_bad_codes(api, codes):
    r = api.client.get("/api/admin/print-error-texts?codes=" + codes)
    assert r.status_code == 400 and r.json() == {"detail": "codes: до 32 кодов вида 0500_8062"}


def test_print_error_texts_is_admins_only(api, monkeypatch):
    monkeypatch.setattr(auth_service, "get_user_from_token", lambda *a, **k: ("viewer", UserRole.VIEWER))
    assert api.client.get("/api/admin/print-error-texts?codes=0500_8062").status_code == 403
