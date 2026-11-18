from types import SimpleNamespace as NS
import pytest
from app.collectors.bambu_raw import RawReport
from app.services import printer_commands as cmd, creality_features as cf, bambu_extras as be


def test_bambu_preserves_rfid_options_across_delta_reports():
    raw = RawReport()
    raw.update({"home_flag": 128, "ams": {"insert_flag": True, "power_on_flag": False}})
    raw.update({"ams": {"tray_now": "0"}})
    view = NS(status=NS(feeder_settings={"estimate_remaining": True}), raw=raw.snapshot())
    payload = cmd.build_feeder_setting({"setting": "estimate_remaining", "enabled": False}, view)["print"]
    assert payload["startup_read_option"] is False
    assert payload["tray_read_option"] is True
    assert payload["calibrate_remain_flag"] is False
    assert payload["ams_id"] == -1


def test_bambu_does_not_invent_initial_settings():
    dev = NS(home_flag=NS(ams_auto_switch_filament=False, ams_calibrate_remaining=False))
    assert be.feeder_settings(dev, {}) is None
    assert be.feeder_settings(dev, {"home_flag_seen": True}) == {"auto_refill": False}


def test_creality_preserves_all_other_settings():
    config = {"autoRefill": 1, "cAutoFeed": 0, "cSelfTest": 0, "cAutoUpdateFilament": 0}
    data = {"cfs": [{}], "box_config": config}
    assert cf.feeder_settings(data) == {"auto_refill": True}
    frame = cmd._build_creality("feeder_setting", {"setting": "auto_refill", "enabled": False}, "k2plus", data)[0]
    assert frame == {"boxConfig": dict(config, autoRefill=0)}
    assert config["autoRefill"] == 1
    assert cmd._cap_refusal("K2", "creality", "feeder_setting") is None


@pytest.mark.parametrize("params", [{"setting": "auto_refill", "enabled": 1}, {"setting": "unknown", "enabled": True}, {"setting": "estimate_remaining", "enabled": True}])
def test_missing_or_invalid_settings_refused(params):
    with pytest.raises(ValueError):
        cf.settings_frame(params, {"cfs": [{}], "box_config": {"autoRefill": 1, "cAutoFeed": 0}})
    with pytest.raises(ValueError):
        cmd.build_feeder_setting(params, NS(status=NS(feeder_settings={"auto_refill": True}), raw={}))
