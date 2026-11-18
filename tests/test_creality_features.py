import pytest

from app.services import creality_features as cf, printer_commands as commands


def test_k1c_reports_a_chamber_target_despite_having_no_heater():
    data = {"max_box_temp": 60, "target_box_temp": 0}
    assert cf.chamber_limit("k1c", data) is None
    assert commands._build_creality("chamber", {"temp": 50}, "k2plus", data) == [{"boxTempControl": 50}]
    with pytest.raises(ValueError):
        commands._build_creality("chamber", {"temp": 65}, "k2plus", data)


def test_cfs_uses_physical_slot_ids_and_preserves_rfid_on_edit():
    material = {"id": 2, "state": 1, "selected": 1, "type": "PETG",
                "color": "#0ff0014", "rfid": "existing-tag"}
    boxes = cf.feeder({"materialBoxs": [{"type": 0, "id": 1, "materials": [material]}]})
    data = {"cfs": boxes}
    assert boxes[0]["slots"][0]["color"] == "#ff0014"
    p = {"ams": 1, "slot": 2}
    assert cf.slot_frame("cfs_set", dict(p, material="PLA"), data) == {
        "modifyMaterial": {"boxId": 1, "id": 2, "type": "PLA"}}
    assert cf.slot_frame("cfs_unload", p, data)["feedInOrOut"]["isFeed"] == 0
    with pytest.raises(ValueError):
        cf.slot_frame("cfs_load", p, data)
    assert cf.history_units(boxes)[0]["index"] == 1


def test_e5_fans_use_binary_firmware_switches():
    data = {"fan_speed_pct": 0, "case_fan_pct": 100}
    assert commands._build_creality("fans", {"part": 100, "chamber": 0}, "ender5max", data) == [
        {"fan": 1}, {"fanCase": 0}]


def test_error_answers_are_the_k1c_pages_frames_and_restart_never_mid_print():
    from types import SimpleNamespace as NS
    from app.domain.models import PrinterState
    from app.services import printer_commands as pc
    assert pc._build_creality("clear_error", {}, "k1c", {}) == [{"cleanErr": 1}]
    assert pc._build_creality("restart_firmware", {}, "k1c", {}) == [{"restartFirmware": 1}]
    prn = {"kind": "creality"}
    for state, refused in ((PrinterState.PRINTING, True), (PrinterState.PAUSED, True), (PrinterState.IDLE, False)):
        live = (NS(state=state), {})
        assert (pc._state_check("k1", prn, "restart_firmware", live) == pc.MSG_RESTART_PRINTING) is refused
        assert pc._state_check("k1", prn, "clear_error", live) is None
