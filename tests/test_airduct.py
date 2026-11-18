"""Air duct mode (Bambu P2S / H2 / X2): the card's switch and the set_airduct command."""
from types import SimpleNamespace as NS

import pytest

from app.domain.models import PrinterState
from app.services import printer_commands as pc
from app.services.normalizer import _airduct


@pytest.mark.parametrize("info,expected", [
    (NS(airduct_mode=1, airduct_modes_available=["cooling", "heating"]), {"mode": 1, "modes": [0, 1]}),
    (NS(airduct_mode=0, airduct_modes_available=["cooling", "heating", "laser"]), {"mode": 0, "modes": [0, 1]}),
    (NS(airduct_mode=2, airduct_modes_available=["cooling", "heating", "laser"]), None),   # laser: not ours
    (NS(airduct_mode=0, airduct_modes_available=[]), None),                              # X1C: no air duct
    (NS(), None),
    (None, None),
])
def test_airduct_from_the_printer_report(info, expected):
    assert _airduct(info) == expected


def test_payload_is_pybambus_set_airduct():
    assert pc._build_payload("airduct", {"mode": 1}) == {
        "print": {"sequence_id": "0", "command": "set_airduct", "modeId": 1, "submode": -1}}
    for bad in ({}, {"mode": 2}, {"mode": -1}, {"mode": True}, {"mode": "1"}):
        with pytest.raises(ValueError):
            pc._build_payload("airduct", bad)


@pytest.fixture
def bambu(monkeypatch):
    monkeypatch.setattr("app.services.printer_registry.get_printer",
                        lambda pid: {"id": pid, "kind": "bambu", "model": "P2S"})
    sent = []
    monkeypatch.setattr(pc, "_send_local", lambda pid, payload, guard=None: sent.append(payload) or
                        {"ok": True, "replied": True, "result": "success"})
    monkeypatch.setattr(pc, "_outcome", lambda r: {"success": True, "status": "confirmed", "detail": "ok", "replied": True})
    monkeypatch.setattr(pc, "_note_reply", lambda *a: None)
    monkeypatch.setattr(pc, "_start_fast_refresh", lambda *a: None)
    return sent


def test_send_needs_a_printer_with_an_air_duct(bambu, monkeypatch):
    monkeypatch.setattr(pc, "live_status", lambda pid, prn=None: NS(state=PrinterState.PRINTING, airduct=None))
    assert pc.send("p1", "airduct", {"mode": 1}) == {"success": False, "status": "refused",
                                                      "detail": pc.MSG_NO_AIRDUCT, "replied": False}
    monkeypatch.setattr(pc, "live_status", lambda pid, prn=None: None)
    assert pc.send("p1", "airduct", {"mode": 1})["detail"] == pc.MSG_NO_TELEMETRY
    assert bambu == []


def test_send_switches_mode(bambu, monkeypatch):
    monkeypatch.setattr(pc, "live_status", lambda pid, prn=None: NS(state=PrinterState.PRINTING, airduct={"mode": 0, "modes": [0, 1]}))
    assert pc.send("p1", "airduct", {"mode": 1})["success"] is True
    assert bambu == [{"print": {"sequence_id": "0", "command": "set_airduct", "modeId": 1, "submode": -1}}]
