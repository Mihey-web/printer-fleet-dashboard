"""Auto-dry API: GET /api/ams/autodry (any user), PUT /api/admin/ams/autodry,
run-now / stop-all / release (admin), the generic settings guard and the
dashboard drying hook. Auth is stubbed like test_ams_api /
test_admin_command_api; the settings DB lives in tmp_path."""
import copy
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api import admin as api_admin
from app.api import auth as api_auth
from app.api import main as api_main
from app.domain.models import PrinterKind, PrinterState, PrinterStatus
from app.domain.user_models import UserRole
from app.services import ams_autodry as ad
from app.services import ams_autodry_engine as eng
from app.services import ams_drying, auth_service, printer_commands, printer_registry, settings_service


def _status(now):
    slots = [{"type": "PETG", "color": "FFF144", "name": None, "remain_pct": 80, "empty": False,
              "nozzle_temp": [220, 260]}] + [{"type": None, "color": None, "name": None, "remain_pct": None,
                                              "empty": True, "nozzle_temp": None}] * 3
    dry = {"type": "n3f", "status": "off", "phase": None, "active": False, "remaining_min": None, "temp": None,
           "hours": None, "filament": None, "blockers": [], "feeding": False}
    dry.update(ams_drying._policy("n3f", slots, False))
    return PrinterStatus(id="bambu-1", label="P1", kind=PrinterKind.BAMBU, online=True, state=PrinterState.IDLE,
                         last_update_ts=now, device_type="P2S", ams_dry_ok=True,
                         ams={"tray_now": None, "units": [{"index": 0, "humidity": 5, "humidity_pct": 30,
                                                           "temp": 26.0, "dry_time": None, "slots": slots,
                                                           "dry": dry}]})


class _Store:
    def __init__(self):
        import time
        self.now = time.time

    def get_all(self):
        return [_status(self.now())]

    def get_one(self, pid):
        return _status(self.now()) if pid == "bambu-1" else None


@pytest.fixture
def api(monkeypatch, tmp_path):
    import config as cfg

    monkeypatch.setattr(cfg, "JWT_SECRET", "test-secret", raising=False)
    role = {"r": UserRole.ADMIN}
    monkeypatch.setattr(auth_service, "get_user_from_token",
                        lambda *a, **k: ("admin" if role["r"] == UserRole.ADMIN else "viewer", role["r"]))
    monkeypatch.setattr(auth_service, "decode_token", lambda *a, **k: {"iat": 0})
    monkeypatch.setattr(api_auth, "token_subject_active", lambda *a, **k: True)
    monkeypatch.setattr(settings_service, "DB_PATH", str(tmp_path / "settings.db"))
    settings_service.init_db()
    settings_service.reset_cache()
    monkeypatch.setattr(printer_registry, "get_label", lambda pid: {"bambu-1": "P1"}.get(pid))
    audit = []
    monkeypatch.setattr(api_admin, "get_audit_service",
                        lambda: SimpleNamespace(log=lambda user, action, *a, **k: audit.append((user, action))))
    monkeypatch.setattr(eng, "get_audit_service", lambda: SimpleNamespace(log=lambda *a, **k: None))
    eng._reset()
    store = _Store()
    eng.set_sources(store, None)
    client = TestClient(api_main.create_app(store))
    client.cookies.set("access_token", "stub")

    def as_viewer():
        role["r"] = UserRole.VIEWER

    yield SimpleNamespace(client=client, audit=audit, as_viewer=as_viewer)
    eng._reset()
    settings_service.reset_cache()


def _cfg(**kw):
    c = copy.deepcopy(ad.DEFAULT_CONFIG)
    c.update(kw)
    return c


# --- GET -------------------------------------------------------------------------------

def test_get_for_viewers(api):
    api.as_viewer()
    r = api.client.get("/api/ams/autodry")
    assert r.status_code == 200
    data = r.json()
    assert set(data) == {"now", "config", "limits", "engine", "units", "journal"}
    assert data["config"] == ad.DEFAULT_CONFIG and data["config"]["dry_run"] is True
    u = data["units"][0]
    assert u["key"] == "bambu-1__0" and u["label"] == "P1" and u["status"] == {"kind": "pending"}
    assert u["plan"]["temp"] == 65 and u["plan_now"]["temp"] == 65 and u["plans"]["pla"]["temp"] == 45
    assert u["plan_ctx"]["cap"] == 65
    text = r.text
    for secret in ("telegram_token", "proxy_list", "access_code", "serial", "MATERIALS", "presets"):
        assert secret not in text


def test_get_journal_without_dedupe_keys(api):
    eng._emit("bambu-1__0", [{"kind": "blocked", "text": "AMS занят", "dk": "AMS занят"}], 1.0)
    rows = api.client.get("/api/ams/autodry").json()["journal"]
    assert rows[0]["kind"] == "blocked" and "dk" not in rows[0]


# --- PUT --------------------------------------------------------------------------------

def test_put_is_admin_only(api):
    api.as_viewer()
    r = api.client.put("/api/admin/ams/autodry", json=_cfg(dry_run=False))
    assert r.status_code == 403


def test_put_saves_audits_and_returns_the_snapshot(api):
    body = _cfg(dry_run=False, start_rh=30, units={"bambu-1__0": {"mode": "pla"}})
    r = api.client.put("/api/admin/ams/autodry", json=body)
    assert r.status_code == 200
    data = r.json()
    assert data["config"]["dry_run"] is False and data["config"]["start_rh"] == 30
    assert data["config"]["units"]["bambu-1__0"] == {"enabled": True, "mode": "pla", "temp": 55, "hours": 8}
    assert data["units"][0]["plan"]["filament"] == "PLA"
    assert api.audit == [("admin", "autodry_config")]
    settings_service.reset_cache()
    assert settings_service.get("ams_autodry")["start_rh"] == 30


@pytest.mark.parametrize("body,detail", [
    (_cfg(start_rh=70), "Порог старта: целое 6–60 %"),
    (dict(_cfg(), rev=3), "Неизвестное поле настроек автосушки: rev"),
    (dict(_cfg(), telegram=True), "Неизвестное поле настроек автосушки: telegram"),
    ([1, 2], "Ожидается объект настроек автосушки"),
    ("x", "Ожидается объект настроек автосушки"),
])
def test_put_validation(api, body, detail):
    r = api.client.put("/api/admin/ams/autodry", json=body)
    assert r.status_code == 400 and r.json()["detail"] == detail
    assert api.audit == []


# --- the generic settings endpoints never touch the engine keys ---------------------------

@pytest.mark.parametrize("key", ["ams_autodry", "ams_autodry_state", "ams_autodry_journal"])
def test_generic_settings_put_refuses_engine_keys(api, key):
    r = api.client.put("/api/admin/settings", json={key: {}, "proxy_check_interval": 600})
    assert r.status_code == 400
    assert r.json()["detail"] == "Автосушка настраивается на вкладке AMS: " + key


def test_generic_settings_get_hides_engine_keys(api, monkeypatch):
    from app.services import telegram_manager
    monkeypatch.setattr(telegram_manager, "status", lambda: {})
    monkeypatch.setattr(telegram_manager, "apply_settings", lambda keys: {"bot_restarting": False})
    values = api.client.get("/api/admin/settings").json()["values"]
    assert not any(k.startswith("ams_autodry") for k in values) and "proxy_list" in values
    values = api.client.put("/api/admin/settings", json={"proxy_check_interval": 900}).json()["values"]
    assert not any(k.startswith("ams_autodry") for k in values)


# --- run-now, stop-all, release ------------------------------------------------------------

@pytest.fixture
def ops(api, monkeypatch):
    calls = []
    plan = {"mode": "auto", "name": "PETG", "filament": "PETG", "temp": 65, "hours": 12, "why": "x", "warn": False,
            "limit": None, "empty": False}

    def run_now(keys, now=None):
        calls.append(("run_now", keys))
        return [{"key": k, "label": "P1", "name": "AMS 1", "ok": k != "bambu-2__0",
                 "status": "confirmed" if k != "bambu-2__0" else "skipped",
                 "detail": "подтверждено принтером" if k != "bambu-2__0" else "Уже сушится — пропускаем",
                 "plan": plan if k != "bambu-2__0" else None} for k in keys]

    def stop_all(keys, now=None):
        calls.append(("stop_all", keys))
        return [{"key": "bambu-1__0", "label": "P1", "name": "AMS 1", "ok": True, "status": "confirmed",
                 "detail": "подтверждено принтером", "plan": None}]

    def release(keys, now=None):
        calls.append(("release", keys))
        return [k for k in keys if k == "bambu-1__0"]

    monkeypatch.setattr(eng, "run_now", run_now)
    monkeypatch.setattr(eng, "stop_all", stop_all)
    monkeypatch.setattr(eng, "release", release)
    api.calls = calls
    return api


@pytest.mark.parametrize("path", ["run-now", "release"])
@pytest.mark.parametrize("body,detail", [
    ({"units": []}, "Выберите хотя бы один блок"),
    ({}, "Выберите хотя бы один блок"),
    ({"units": "bambu-1__0"}, "Некорректный список блоков"),
    ({"units": [1]}, "Некорректный список блоков"),
    ({"units": ["p%d__0" % i for i in range(33)]}, "Некорректный список блоков"),
    ({"units": ["x" * 200]}, "Некорректный список блоков"),
    ([1], "Некорректный список блоков"),
])
def test_unit_list_validation(ops, path, body, detail):
    r = ops.client.post("/api/admin/ams/autodry/" + path, json=body)
    assert r.status_code == 400 and r.json()["detail"] == detail
    assert ops.calls == []


def test_run_now_passes_results_and_audits_attempts(ops):
    r = ops.client.post("/api/admin/ams/autodry/run-now", json={"units": ["bambu-1__0", "bambu-2__0", "bambu-1__0"]})
    assert r.status_code == 200
    data = r.json()
    assert ops.calls == [("run_now", ["bambu-1__0", "bambu-2__0"])]
    assert [x["status"] for x in data["results"]] == ["confirmed", "skipped"] and "snapshot" in data
    assert ops.audit == [("admin", "autodry_run_now:bambu-1:0:65/12/PETG")]


def test_stop_all_empty_body_means_every_drying_unit(ops):
    for body in ({}, None):
        r = ops.client.post("/api/admin/ams/autodry/stop-all", json=body)
        assert r.status_code == 200 and r.json()["results"][0]["status"] == "confirmed"
    assert ops.calls == [("stop_all", None), ("stop_all", None)]
    assert ops.audit == [("admin", "autodry_stop:bambu-1:0")] * 2
    r = ops.client.post("/api/admin/ams/autodry/stop-all", json={"units": ["bambu-1__0"]})
    assert r.status_code == 200 and ops.calls[-1] == ("stop_all", ["bambu-1__0"])
    r = ops.client.post("/api/admin/ams/autodry/stop-all", json={"units": []})
    assert r.status_code == 400 and r.json()["detail"] == "Выберите хотя бы один блок"


def test_release_returns_the_snapshot_and_audits(ops):
    r = ops.client.post("/api/admin/ams/autodry/release", json={"units": ["bambu-1__0", "zzz__0"]})
    assert r.status_code == 200 and "units" in r.json()
    assert ops.audit == [("admin", "autodry_release:bambu-1:0")]


def test_manual_endpoints_are_admin_only(ops):
    ops.as_viewer()
    for path in ("run-now", "stop-all", "release"):
        assert ops.client.post("/api/admin/ams/autodry/" + path, json={"units": ["bambu-1__0"]}).status_code == 403
    assert ops.calls == []


# --- the dashboard drying command: busy check and hook --------------------------------------

@pytest.fixture
def cmd(api, monkeypatch):
    sent, noted = [], []
    reply = {"success": True, "status": "confirmed", "detail": "подтверждено принтером", "replied": True}
    monkeypatch.setattr(printer_commands, "send", lambda pid, action, params: sent.append(action) or dict(reply))
    monkeypatch.setattr(eng, "note_user_command", lambda pid, action, params: noted.append((pid, action, params)))
    api.sent, api.noted, api.reply = sent, noted, reply
    return api


@pytest.mark.parametrize("body", [{"action": "dry_start", "ams": 0, "temp": 45, "hours": 1, "filament": "PLA"},
                                  {"action": "dry_stop", "ams": 0}])
def test_successful_drying_command_reaches_the_engine(cmd, body):
    r = cmd.client.post("/api/admin/printers/bambu-1/command", json=body)
    assert r.status_code == 200
    params = {k: v for k, v in body.items() if k != "action"}
    assert cmd.noted == [("bambu-1", body["action"], params)]


def test_failed_or_other_commands_do_not_reach_the_engine(cmd):
    cmd.client.post("/api/admin/printers/bambu-1/command", json={"action": "light_on"})
    cmd.reply.update(success=False, status="refused", detail="Сушка уже идёт — сначала остановите её")
    r = cmd.client.post("/api/admin/printers/bambu-1/command", json={"action": "dry_start", "ams": 0, "temp": 45,
                                                                       "hours": 1, "filament": "PLA"})
    assert r.status_code == 400
    assert cmd.noted == []


def test_a_raising_hook_does_not_break_the_reply(cmd, monkeypatch):
    monkeypatch.setattr(eng, "note_user_command", lambda *a: 1 / 0)
    r = cmd.client.post("/api/admin/printers/bambu-1/command", json={"action": "dry_stop", "ams": 0})
    assert r.status_code == 200 and r.json()["status"] == "confirmed"


def test_busy_unit_gets_409_and_nothing_is_sent(cmd):
    eng._inflight.add("bambu-1__0")
    try:
        r = cmd.client.post("/api/admin/printers/bambu-1/command", json={"action": "dry_stop", "ams": 0})
    finally:
        eng._inflight.discard("bambu-1__0")
    assert r.status_code == 409
    assert r.json()["detail"] == "Автосушка сейчас отправляет команду этому блоку — повторите через несколько секунд"
    assert cmd.sent == [] and cmd.noted == []
    r = cmd.client.post("/api/admin/printers/bambu-1/command", json={"action": "dry_stop", "ams": 1})
    assert r.status_code == 200                     # another unit of the printer is free


def test_dashboard_drying_command_claims_the_unit(cmd, monkeypatch):
    # D17: while the dashboard waits for the printer, the engine (tick, run-now, stop-all)
    # and a second command leave the unit alone; the claim covers the engine hook too
    seen = []

    def send(pid, action, params):
        seen.append(("send", eng.is_busy(pid, params["ams"]), eng.claim(pid, params["ams"])))
        return dict(cmd.reply)

    def note(pid, action, params):
        seen.append(("note", eng.is_busy(pid, params["ams"])))

    monkeypatch.setattr(printer_commands, "send", send)
    monkeypatch.setattr(eng, "note_user_command", note)
    r = cmd.client.post("/api/admin/printers/bambu-1/command",
                        json={"action": "dry_start", "ams": 0, "temp": 45, "hours": 1, "filament": "PLA"})
    assert r.status_code == 200
    assert seen == [("send", True, False), ("note", True)] and eng.is_busy("bambu-1", 0) is False
    monkeypatch.setattr(printer_commands, "send", lambda *a: 1 / 0)
    with pytest.raises(ZeroDivisionError):
        cmd.client.post("/api/admin/printers/bambu-1/command", json={"action": "dry_stop", "ams": 0})
    assert eng.is_busy("bambu-1", 0) is False      # released however the send ended
    monkeypatch.setattr(printer_commands, "send", lambda *a: dict(cmd.reply))
    assert cmd.client.post("/api/admin/printers/bambu-1/command", json={"action": "light_on"}).status_code == 200
    assert not eng._inflight


def test_dashboard_stop_is_no_interruption_when_a_check_lands_mid_send(api, monkeypatch):
    # our auto run; the dashboard stops it and a check runs while the printer has not replied
    import time as _time
    now = _time.time()
    eng._states["bambu-1__0"] = ad.load_state({
        "phase": "running", "origin": "auto", "series": 1, "run_started_at": now - 2 * 3600,
        "run_expected_end": now + 10 * 3600, "run_temp": 65, "run_hours": 12, "run_filament": "PETG",
        "confirm_by": now - 2 * 3600 + 300, "seen_active": True, "last_seen_at": now - 60,
        "last_remaining_min": 600, "remaining_changed_at": now - 60, "runs": [now - 2 * 3600]})

    def send(pid, action, params):              # the store already shows the AMS dark
        eng.tick()
        return {"success": True, "status": "confirmed", "detail": "подтверждено принтером", "replied": True}

    monkeypatch.setattr(printer_commands, "send", send)
    r = api.client.post("/api/admin/printers/bambu-1/command", json={"action": "dry_stop", "ams": 0})
    assert r.status_code == 200
    eng.tick()
    st = eng._states["bambu-1__0"]
    assert [e["kind"] for e in eng.snapshot()["journal"]] == ["manual_stop"]
    assert st["interrupts"] == 0 and st["hold_reason"] == "manual_stop" and st["end_kind"] == "stop"
