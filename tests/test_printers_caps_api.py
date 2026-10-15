"""/api/printers: per-printer capabilities (caps / caps_note), print_cmds and chamber_ctl.

The UI shows a control only where caps says True; a Klipper's caps come from
the first-class ctl_ready / light_ctl fields, which a non-printing status keeps
(its debug blob is empty). Auth is stubbed as in test_ams_api.
"""
import pytest
from fastapi.testclient import TestClient

from app.api import auth as api_auth
from app.api import main as api_main
from app.domain.models import PrinterKind, PrinterState, PrinterStatus
from app.domain.user_models import UserRole
from app.services import auth_service, printer_commands, settings_service
from app.services.normalizer import normalize_ws_dict

ALL_FALSE = dict.fromkeys(printer_commands.CAP_KEYS, False)
PRINT_CLASS = ("pause", "resume", "stop", "cooldown", "eject", "tune", "ams")

REGISTRY = [
    {"id": "b-ok", "label": "P1", "kind": "bambu", "model": "P2S", "access_code": "12345678", "serial": "SN-1"},
    {"id": "b-blocked", "label": "P2", "kind": "bambu", "model": "P2S"},
    {"id": "b-new", "label": "P1S1", "kind": "bambu", "model": "P1S"},
    {"id": "b-h2", "label": "H1", "kind": "bambu", "model": "H2S"},
    {"id": "c-1", "label": "K1", "kind": "creality", "model": "k1c"},
    {"id": "k-ready", "label": "Ghost 6", "kind": "klipper", "model": None},
    {"id": "k-down", "label": "Varan", "kind": "klipper", "model": None},
    {"id": "k-off", "label": "Klipper 3", "kind": "klipper", "model": None},
    {"id": "m-1", "label": "Reborn 2", "kind": "mks", "model": None},
]


class _Store:
    def __init__(self, statuses):
        self._s = statuses

    def get_all(self):
        return list(self._s)

    def get_one(self, pid):
        return next((s for s in self._s if s.id == pid), None)


def _bambu(pid, device_type="P2S"):
    return PrinterStatus(id=pid, label="x", kind=PrinterKind.BAMBU, online=True, state=PrinterState.PRINTING,
                         device_type=device_type)


def _klipper(pid, webhooks="ready", pin="caselight"):
    data = {"state": "standby", "progress": 0, "remaining_time": 0, "debug": {"webhooks_state": webhooks}}
    if pin is not None:
        data["light_pin"] = pin
    return normalize_ws_dict(pid, "x", PrinterKind.KLIPPER, data, device_type="generic")


STATUSES = [
    _bambu("b-ok"), _bambu("b-blocked"), _bambu("b-new", "P1S"), _bambu("b-h2", device_type=None),
    PrinterStatus(id="c-1", label="x", kind=PrinterKind.CREALITY, online=True, state=PrinterState.IDLE),
    _klipper("k-ready"), _klipper("k-down", webhooks="shutdown"),
    normalize_ws_dict("k-off", "x", PrinterKind.KLIPPER, {}),
    PrinterStatus(id="m-1", label="x", kind=PrinterKind.MKS, online=False, state=PrinterState.OFFLINE),
]


@pytest.fixture
def rows(monkeypatch):
    import config as cfg

    monkeypatch.setattr(cfg, "JWT_SECRET", "test-secret", raising=False)
    # /api/printers is not admin-only: a viewer sees the same caps
    monkeypatch.setattr(auth_service, "get_user_from_token", lambda *a, **k: ("viewer", UserRole.VIEWER))
    monkeypatch.setattr(auth_service, "decode_token", lambda *a, **k: {"iat": 0})
    monkeypatch.setattr(api_auth, "token_subject_active", lambda *a, **k: True)
    monkeypatch.setattr("app.services.printer_registry.list_printers", lambda *a, **k: [dict(r) for r in REGISTRY])

    def get(light_enabled=()):
        monkeypatch.setattr(settings_service, "get",
                            lambda key, **k: list(light_enabled) if key == "printer_light_enabled" else {})
        client = TestClient(api_main.create_app(_Store(STATUSES)))
        client.cookies.set("access_token", "stub")
        r = client.get("/api/printers")
        assert r.status_code == 200
        return {row["id"]: row for row in r.json()}

    printer_commands._capability.clear()
    printer_commands._capability.update({
        "b-ok": {"print_cmds": True, "via": "fun", "ts": 0},
        "b-h2": {"print_cmds": True, "via": "fun", "ts": 0},
        "b-blocked": {"print_cmds": False, "via": "local", "ts": 0},
    })
    yield get
    printer_commands._capability.clear()


def test_bambu_caps_follow_the_print_class_verdict(rows):
    r = rows()
    assert r["b-ok"]["caps"] == dict({k: True for k in PRINT_CLASS}, light=True)
    assert (r["b-ok"]["caps_note"], r["b-ok"]["print_cmds"]) == (None, True)
    assert r["b-blocked"]["caps"] == dict(ALL_FALSE, light=True)
    assert (r["b-blocked"]["caps_note"], r["b-blocked"]["print_cmds"]) == (
        "прошивка блокирует (нет Developer Mode)", False)
    assert r["b-new"]["caps"] == dict({k: None for k in PRINT_CLASS}, light=True)
    assert (r["b-new"]["caps_note"], r["b-new"]["print_cmds"]) == ("доступ к управлению ещё проверяется", None)


def test_creality_caps_and_the_light_setting(rows):
    r = rows()["c-1"]
    assert r["caps"] == dict(ALL_FALSE, pause=True, resume=True, stop=True, light=None)
    assert (r["caps_note"], r["print_cmds"]) == (None, True)
    assert rows(light_enabled=["c-1"])["c-1"]["caps"]["light"] is True


def test_klipper_caps_while_idle(rows):
    r = rows()
    ready = r["k-ready"]
    assert ready["state"] == "idle" and "debug" not in ready
    assert ready["caps"] == dict(ALL_FALSE, pause=True, resume=True, stop=True, light=True)
    assert (ready["caps_note"], ready["print_cmds"], ready["ctl_ready"], ready["light_ctl"]) == (None, True, True, True)
    down = r["k-down"]
    assert (down["caps"], down["caps_note"], down["print_cmds"]) == (ALL_FALSE, "Klipper не готов", False)
    off = r["k-off"]
    assert off["caps"] == dict(ALL_FALSE, pause=None, resume=None, stop=None, light=None)
    assert (off["caps_note"], off["print_cmds"]) == (None, None)


def test_mks_has_no_commands(rows):
    r = rows()["m-1"]
    assert (r["caps"], r["caps_note"]) == (ALL_FALSE, "управление не подключено")
    assert "print_cmds" not in r


def test_chamber_ctl_falls_back_to_the_registry_model(rows):
    r = rows()
    assert r["b-h2"]["device_type"] is None and r["b-h2"]["chamber_ctl"] is True
    assert "chamber_ctl" not in r["b-ok"] and "chamber_ctl" not in r["k-ready"]


def test_no_registry_secrets_or_debug_in_the_rows(rows):
    for row in rows().values():
        assert not {"access_code", "serial", "debug", "host"} & set(row)
