"""Contract parity with PyPI pybambu 1.0.1.

tests/fixtures/bambu/contract_golden_pybambu_1.0.1.json holds the PrinterStatus
fields the app produced on pybambu 1.0.1 (plus its former monkeypatches) for
synthetic payload scenarios. The same scenarios are replayed through the
vendored library here. Every difference must be listed in ALLOWED with its new
value (or a predicate), and every ALLOWED entry must still occur, so the list
cannot go stale.
"""
import json
import re
import time

import bambu_testkit as kit
from app.services.normalizer import normalize_bambu

NOW = int(time.time())


def tray(i, color="FF0000FF", ttype="PLA", idx="GFL99", remain=80):
    return {"id": str(i), "tray_color": color, "tray_type": ttype,
            "tray_info_idx": idx, "remain": remain, "tag_uid": "0" * 16,
            "nozzle_temp_min": "190", "nozzle_temp_max": "240"}


# Verbatim from the probe that produced the golden file (synthetic payloads).
SCENARIOS = {
    # X1C printing, classic AMS, flat chamber_temper, HMS + print_error, lights.
    "x1c_printing": ("X1C", [{
        "msg": 0, "gcode_state": "RUNNING", "mc_percent": 42, "mc_remaining_time": 75,
        "layer_num": 50, "total_layer_num": 200, "gcode_file": "/data/Metadata/plate_1.gcode",
        "subtask_name": "probe_part", "gcode_start_time": str(NOW - 3600),
        "bed_temper": 60.4, "bed_target_temper": 60, "nozzle_temper": 219.6,
        "nozzle_target_temper": 220, "chamber_temper": 31,
        "wifi_signal": "-55dBm", "spd_lvl": 2, "spd_mag": 100, "stg_cur": 0,
        "print_type": "local", "big_fan1_speed": "10", "big_fan2_speed": "6",
        "cooling_fan_speed": "15", "heatbreak_fan_speed": "15",
        "lights_report": [{"node": "chamber_light", "mode": "on"},
                          {"node": "work_light", "mode": "flashing"}],
        "upgrade_state": {"new_version_state": 2},
        "nozzle_diameter": "0.4", "nozzle_type": "hardened_steel",
        "home_flag": 0, "print_error": 0,
        "ipcam": {"ipcam_record": "enable", "timelapse": "disable"},
        "hms": [{"attr": 50331904, "code": 65543}],
        "ams": {"tray_now": "1", "ams": [{"id": "0", "humidity": "4", "temp": "28.5",
                "tray": [tray(0), tray(1, "00FF00FF", "PETG", "GFG99", 55),
                         {"id": "2"}, tray(3, "0C0C0CFF", "Empty", "", 0)]}]},
    }]),
    # P2S: chamber only nested in device.ctc, AMS 2 Pro with humidity_raw + dry_time.
    "p2s_ctc_ams2pro": ("P2S", [{
        "msg": 0, "gcode_state": "IDLE", "mc_percent": 0, "mc_remaining_time": 0,
        "bed_temper": 25, "nozzle_temper": 26, "wifi_signal": "-61dBm",
        "spd_lvl": 2, "spd_mag": 100, "stg_cur": 255, "print_type": "idle",
        "device": {"ctc": {"info": {"temp": 43}, "state": 0},
                   "bed": {"info": {"temp": 4587590}}},
        "upgrade_state": {"new_version_state": 1},
        "ams": {"tray_now": "3", "ams": [{"id": "0", "humidity": "2", "humidity_raw": "24",
                "temp": "39.9", "dry_time": 0,
                "tray": [{"id": "0"}, {"id": "1"}, {"id": "2"},
                         tray(3, "FFF144FF", "PETG", "GFG99", -1)]}]},
    }, {
        # second delta message: drying started
        "ams": {"tray_now": "3", "ams": [{"id": "0", "humidity": "2", "humidity_raw": "22",
                "temp": "55.0", "dry_time": 240, "tray": []}]},
    }]),
    # H2S with active chamber heating: packed (65 << 16) | 63.
    "h2s_ctc_heating": ("H2S", [{
        "msg": 0, "gcode_state": "PREPARE", "mc_percent": 0, "mc_remaining_time": 120,
        "nozzle_temper": 150, "bed_temper": 90, "wifi_signal": "-48dBm",
        "spd_lvl": 3, "spd_mag": 124, "stg_cur": 2,
        "device": {"ctc": {"info": {"temp": 4259903}, "state": 2}},
    }]),
    # Paused on filament runout (stage 6), failed print_error rendered in hex.
    "x1c_paused_runout": ("X1C", [{
        "msg": 0, "gcode_state": "PAUSE", "mc_percent": 13, "mc_remaining_time": 300,
        "stg_cur": 6, "print_error": 117473282, "chamber_temper": 35,
    }]),
    # Fresh device, no telemetry content except an unknown state.
    "x1c_unknown_state": ("X1C", [{"msg": 0, "gcode_state": "WAT"}]),
    # AMS HT (id 128) — documents how the lib copes with index >= 4.
    "p2s_ams_ht": ("P2S", [{
        "msg": 0, "gcode_state": "IDLE",
        "ams": {"tray_now": "255", "ams": [{"id": "128", "humidity": "3", "humidity_raw": "30",
                "temp": "30.0", "dry_time": 0, "tray": [tray(0)]}]},
    }]),
    # Added later, run through the same probe on 1.0.1: HMS codes outside the
    # tables. A code missing from every table.
    "x1c_hms_unknown_code": ("X1C", [{
        "msg": 0, "gcode_state": "IDLE", "hms": [{"attr": 0x0FFF0000, "code": 0x00020001}],
    }]),
    # A serious code whose text is empty in Bambu's tables (a P2S reports it permanently).
    "p2s_hms_silent_serious": ("P2S", [{
        "msg": 0, "gcode_state": "IDLE", "hms": [{"attr": 0x05000600, "code": 0x00020070}],
    }]),
    # The real P2S order: that code first, then one with text (common).
    "p2s_hms_silent_first": ("P2S", [{
        "msg": 0, "gcode_state": "IDLE", "hms": [{"attr": 0x05000600, "code": 0x00020070},
                                                 {"attr": 0x05000100, "code": 0x00030007}],
    }]),
    # A common code whose text is empty in Bambu's tables.
    "x1c_hms_silent_common": ("X1C", [{
        "msg": 0, "gcode_state": "IDLE", "hms": [{"attr": 0x05000200, "code": 0x00030009}],
    }]),
}

COMPARE_KEYS = [
    "online", "state", "progress_pct", "job_name", "eta_seconds", "print_time_min",
    "nozzle_temp", "bed_temp", "chamber_temp", "target_nozzle_temp", "target_bed_temp",
    "target_chamber_temp", "current_layer", "total_layers", "wifi_signal",
    "firmware_version", "stage", "fan_speed_pct", "feedrate_pct", "last_error",
    "ams", "fans", "fw_update", "light_on",
]

CYRILLIC = re.compile("[А-Яа-яЁё]")


def ru_hms(code, severity):
    """HMS line rendered from Bambu's Russian table: '<code>: <ru text> [<severity>]'."""
    def check(value):
        return (isinstance(value, str) and value.startswith(f"{code}: ")
                and value.endswith(f" [{severity}]") and CYRILLIC.search(value) is not None)
    check.__name__ = f"ru_hms({code}, {severity})"
    return check


def hms_parts(*checks):
    """last_error of ' | '-separated parts, each equal to a literal or passing a predicate."""
    def check(value):
        parts = value.split(" | ") if isinstance(value, str) else []
        return len(parts) == len(checks) and all(
            c(part) if callable(c) else part == c for part, c in zip(parts, checks))
    check.__name__ = "hms_parts"
    return check


SILENT_P2S = ("HMS_0500_0600_0002_0070: unknown [serious] "
              "(см. https://wiki.bambulab.com/en/x1/troubleshooting/hmscode/0500_0600_0002_0070)")


def ht_unit(value):
    """The AMS HT scenario: the 1.0.1 literal plus the drying block of an idle
    AMS HT holding PLA (capped at PLA's 45 °C heat-distortion temperature)."""
    want = {"tray_now": 255, "units": [{
        "index": 128, "humidity": 3, "humidity_pct": 30, "dry_time": None, "temp": 30.0,
        "slots": [{"type": "PLA", "color": "FF0000", "name": "Generic PLA",
                   "remain_pct": 80, "empty": False, "nozzle_temp": [190, 240]}]}]}
    if not isinstance(value, dict) or len(value.get("units") or []) != 1:
        return False
    unit = dict(value["units"][0])
    dry = unit.pop("dry", None) or {}
    return (dict(value, units=[unit]) == want and dry.get("type") == "n3s" and dry.get("status") is None
            and dry.get("active") is False and (dry.get("max_temp"), dry.get("max_by")) == (45, "PLA"))


# Every intended difference from 1.0.1, with the new value.
ALLOWED = {
    # The part-cooling fan is read at all now (the 1.0.1-era normalizer bug);
    # cooling_fan_speed "15" -> 100 %, absent -> 0 %.
    ("x1c_printing", "fan_speed_pct"): 100,
    ("p2s_ctc_ams2pro", "fan_speed_pct"): 0,
    ("h2s_ctc_heating", "fan_speed_pct"): 0,
    ("x1c_paused_runout", "fan_speed_pct"): 0,
    ("x1c_unknown_state", "fan_speed_pct"): 0,
    ("p2s_ams_ht", "fan_speed_pct"): 0,
    ("x1c_hms_unknown_code", "fan_speed_pct"): 0,
    ("p2s_hms_silent_serious", "fan_speed_pct"): 0,
    ("p2s_hms_silent_first", "fan_speed_pct"): 0,
    ("x1c_hms_silent_common", "fan_speed_pct"): 0,
    # gcode_start_time is not parsed any more; start_time comes from an observed
    # idle -> busy transition (None after a restart mid-print, as on current firmware).
    ("x1c_printing", "print_time_min"): None,
    # HMS text from Bambu's Russian table, code kept as the prefix.
    ("x1c_printing", "last_error"): ru_hms("HMS_0300_0100_0001_0007", "fatal"),
    ("p2s_hms_silent_first", "last_error"): hms_parts(SILENT_P2S, ru_hms("HMS_0500_0100_0003_0007", "common")),
    # A common code without text is no longer shown (fatal / serious ones still are).
    ("x1c_hms_silent_common", "last_error"): None,
    # print_error reads "Ошибка печати 0700_8002: <text>" from Bambu's Russian
    # table instead of "Код ошибки печати: 0x07008002".
    ("x1c_paused_runout", "last_error"): lambda v: (isinstance(v, str) and v.startswith("Ошибка печати 0700_8002: ")
                                                    and CYRILLIC.search(v) is not None),
    # Empty slots report remain -1 (unknown) instead of 0; the UI hides it for empty slots.
    ("x1c_printing", "ams.units[0].slots[2].remain_pct"): -1,
    ("x1c_printing", "ams.units[0].slots[3].remain_pct"): -1,
    ("p2s_ctc_ams2pro", "ams.units[0].slots[0].remain_pct"): -1,
    ("p2s_ctc_ams2pro", "ams.units[0].slots[1].remain_pct"): -1,
    ("p2s_ctc_ams2pro", "ams.units[0].slots[2].remain_pct"): -1,
    # The packed device.bed value (70/70) wins over the flat bed_temper (25).
    ("p2s_ctc_ams2pro", "bed_temp"): 70.0,
    ("p2s_ctc_ams2pro", "target_bed_temp"): 70.0,
    # AMS HT units are parsed (1.0.1 raised IndexError and lost the whole AMS).
    ("p2s_ams_ht", "ams"): ht_unit,
    # The drying block: without the info bit field or a get_version module list
    # a 4-slot unit's type is unknown, so it gets none.
    ("x1c_printing", "ams.units[0].dry"): None,
    ("p2s_ctc_ams2pro", "ams.units[0].dry"): None,
    # Slots carry the spool profile's nozzle range (AMS load / unload temperatures).
    ("x1c_printing", "ams.units[0].slots[0].nozzle_temp"): [190, 240],
    ("x1c_printing", "ams.units[0].slots[1].nozzle_temp"): [190, 240],
    ("x1c_printing", "ams.units[0].slots[2].nozzle_temp"): None,
    ("x1c_printing", "ams.units[0].slots[3].nozzle_temp"): None,
    ("p2s_ctc_ams2pro", "ams.units[0].slots[0].nozzle_temp"): None,
    ("p2s_ctc_ams2pro", "ams.units[0].slots[1].nozzle_temp"): None,
    ("p2s_ctc_ams2pro", "ams.units[0].slots[2].nozzle_temp"): None,
    ("p2s_ctc_ams2pro", "ams.units[0].slots[3].nozzle_temp"): [190, 240],
}


def _diff(old, new, path=""):
    out = []
    if isinstance(old, dict) and isinstance(new, dict):
        for key in sorted(set(old) | set(new)):
            sub = f"{path}.{key}" if path else key
            out += _diff(old.get(key, "<absent>"), new.get(key, "<absent>"), sub)
    elif isinstance(old, list) and isinstance(new, list) and len(old) == len(new):
        for i, (a, b) in enumerate(zip(old, new)):
            out += _diff(a, b, f"{path}[{i}]")
    elif old != new:
        out.append((path, old, new))
    return out


def _status(make_collector, model, messages) -> dict:
    collector = make_collector("probe", "192.0.2.1", "00000000", "PROBE0000000000", model)
    device = collector.client.get_device()
    for msg in messages:
        device.print_update(data=msg)
    device.info.set_online(True)
    status = normalize_bambu("probe-1", "probe", device, device_type=model).to_dict()
    elapsed = status.pop("print_time_seconds", None)
    status["print_time_min"] = None if elapsed is None else round(elapsed / 60)
    return {key: status.get(key) for key in COMPARE_KEYS}


def test_contract_parity_with_pypi_1_0_1(make_collector):
    golden = json.loads((kit.FIXTURES / "contract_golden_pybambu_1.0.1.json").read_text(encoding="utf-8"))
    assert golden["compare_keys"] == COMPARE_KEYS
    assert sorted(golden["scenarios"]) == sorted(SCENARIOS)

    seen, unexpected = set(), []
    for name, (model, messages) in SCENARIOS.items():
        new = _status(make_collector, model, messages)
        for path, old_value, new_value in _diff(golden["scenarios"][name], new):
            key = (name, path)
            if key in ALLOWED:
                allowed = ALLOWED[key]
                if allowed(new_value) if callable(allowed) else new_value == allowed:
                    seen.add(key)
                    continue
            unexpected.append((name, path, old_value, new_value))
    assert unexpected == []
    assert sorted(set(ALLOWED) - seen) == []
