"""Golden replay: real captured MQTT reports -> vendored pybambu -> normalize_bambu.

Every message of the sanitized fixtures (tests/fixtures/bambu, 198 reports from
four printing printers) goes through the client's on_message exactly as paho
would deliver it. After each message the resulting PrinterStatus is checked
field by field against an oracle computed from the payload alone, so the test
does not trust the library's parsing. A second test pins literal values from
the fixture README as an independent anchor.
"""
import json
import math
import os
import re

import pytest

import bambu_testkit as kit
from app.domain.models import PrinterState
from app.services.normalizer import normalize_bambu
from app.services.utils import map_state

MODELS = ("X1C", "P2S", "H2S", "P1S")
# The stg_cur values present in the fixtures, with their CURRENT_STAGE_IDS slugs.
STAGES = {0: "printing", 1: "auto_bed_leveling", 14: "cleaning_nozzle_tip", 255: "idle", -1: "idle"}
# filaments.json names for the tray_info_idx values present in the fixtures.
FILAMENTS = {"GFG00": "Bambu PETG Basic", "GFG99": "Generic PETG", "GFN98": "Generic PA-CF"}
SEVERITY = {1: "fatal", 2: "serious", 3: "common", 4: "info"}
# Bambu's own RU and EN tables have an empty text for this code (the P2S
# reports it permanently): the library drops it from hms.errors, and the
# normalizer shows it as "unknown" with its wiki page because it is serious.
SILENT_CODES = {"0500_0600_0002_0070"}
WIKI_PAGE = "https://wiki.bambulab.com/en/x1/troubleshooting/hmscode/"
CYRILLIC = re.compile("[А-Яа-яЁё]")
# ams.ams[].info: type bits 0-3 (3 AMS 2 Pro, 4 AMS HT), dry status bits 4-7,
# dry phase bits 22-23. Status only when fun2 bit 5 (remote drying) is set.
DRY_TYPES = {3: "n3f", 4: "n3s"}
DRY_STATUS = {0: "off", 1: "checking", 2: "drying", 3: "cooling", 4: "stopping", 5: "error",
              6: "overheat", 7: "testing"}
DRY_KEYS = ("type", "status", "phase", "active", "remaining_min", "temp", "hours", "filament",
            "blocker_codes", "feeding")


def _fan_pct(raw) -> int:
    raw = int(raw or 0)
    return 0 if not raw else math.ceil(raw / 15 * 100 / 10) * 10


def _hms_code(attr: int, code: int) -> str:
    return f"{attr >> 16:04X}_{attr & 0xFFFF:04X}_{code >> 16:04X}_{code & 0xFFFF:04X}"


def _nozzle_slug(code: str) -> str:
    """Nozzle type code ("HS01") -> slug: 2nd letter U = TPU high flow, H/E =
    high flow; digits 3-4 = material."""
    if code[1] == "U":
        return "tpu_high_flow"
    flow = "high_flow_" if code[1] in ("H", "E") else ""
    return flow + {"00": "stainless_steel", "01": "hardened_steel", "05": "tungsten_carbide"}.get(code[2:4], "unknown")


def _tray_loaded(tray: dict) -> bool:
    if "state" in tray:
        state = int(tray["state"])
        if not state & 1:
            return False
        return state == 3 if state <= 3 else bool(state & 8)
    return bool(tray.get("tray_info_idx")) or tray.get("tray_type", "") not in ("", "Empty")


# Models without a chamber sensor: their chamber_temper is bogus (the P1S
# reports a constant 5), so the status carries no chamber reading.
NO_CHAMBER_SENSOR = ("P1P", "P1S", "A1", "A1MINI")


def expected(p: dict, model: str = "") -> dict:
    """Expected PrinterStatus fields from the merged `print` block alone (and
    the printer model, which decides whether there is a chamber sensor)."""
    e = {
        "state": map_state(p["gcode_state"]),
        "progress_pct": float(p["mc_percent"]),
        "current_layer": int(p["layer_num"]),
        "total_layers": int(p["total_layer_num"]),
        "eta_seconds": int(p["mc_remaining_time"]) * 60,
        "feedrate_pct": int(p["spd_mag"]),
        "wifi_signal": int(p["wifi_signal"].replace("dBm", "")),
    }
    dev = p.get("device") or {}
    ext = dev.get("extruder") or {}
    active = (ext.get("state", 0) >> 4) & 0xF
    if "info" in ext:
        packed = next(i["temp"] for i in ext["info"] if i["id"] == active)
        e["nozzle_temp"], e["target_nozzle_temp"] = float(packed & 0xFFFF), float(packed >> 16)
    else:
        e["nozzle_temp"] = float(round(p["nozzle_temper"]))
        e["target_nozzle_temp"] = float(round(p["nozzle_target_temper"]))
    if "bed" in dev:
        packed = dev["bed"]["info"]["temp"]
        e["bed_temp"], e["target_bed_temp"] = float(packed & 0xFFFF), float(packed >> 16)
    else:
        e["bed_temp"] = float(round(p["bed_temper"]))
        e["target_bed_temp"] = float(math.floor(p["bed_target_temper"]))
    if "ctc" in dev:
        packed = dev["ctc"]["info"]["temp"]
        cur, tgt = packed & 0xFFFF, packed >> 16
    else:
        cur, tgt = round(p["chamber_temper"]), 0
    e["chamber_temp"] = float(cur) if 0 < cur < 200 and model not in NO_CHAMBER_SENSOR else None
    e["target_chamber_temp"] = float(tgt) if e["chamber_temp"] is not None and 0 < tgt < 200 else None
    e["stage"] = STAGES[int(p["stg_cur"])]
    lights = {x["node"]: x["mode"] for x in p.get("lights_report", [])}
    # Either lamp lit = lit (H2/X2 have two); off only when lamp 2 is not on either.
    l1, l2 = lights.get("chamber_light"), lights.get("chamber_light2")
    e["light_on"] = True if "on" in (l1, l2) else False if l1 == "off" and l2 in (None, "off", "unknown") else None
    e["fan_speed_pct"] = _fan_pct(p.get("cooling_fan_speed"))
    e["fans"] = {"aux": _fan_pct(p.get("big_fan1_speed")),
                 "chamber": _fan_pct(p.get("big_fan2_speed")),
                 "heatbreak": _fan_pct(p.get("heatbreak_fan_speed"))}
    nvs = (p.get("upgrade_state") or {}).get("new_version_state", 0)
    e["fw_update"] = None if nvs == 0 else nvs == 1
    e["job_name"] = p.get("subtask_name") or p.get("gcode_file") or None
    e["firmware_version"] = "unknown"    # no fixture carries a get_version reply
    e["print_time_seconds"] = None       # captured mid-print: no idle->busy transition
    # A filament change runs while device.extruder.state bit 19 is set (legacy: ams_status main 1).
    busy = bool(ext.get("state", 0) >> 19 & 1) if "info" in ext else (p.get("ams_status", 0) >> 8) & 0xFF == 1
    assert not busy, "the captures carry no filament change"
    e["filament_change"] = None
    # Filament in the extruder: info bit 1 of the active extruder (legacy: tray_now != 255).
    if "info" in ext:
        e["filament_loaded"] = bool(next(i["info"] for i in ext["info"] if i["id"] == active) & 0b10)
    else:
        raw_now = (p.get("ams") or {}).get("tray_now")
        e["filament_loaded"] = None if raw_now is None else int(raw_now) != 255
    e["dual_nozzle"] = (ext.get("state", 0) & 0xF) > 1
    e["filament_swap_ok"] = None          # firmware version unknown: no get_version in the captures
    hms = [(_hms_code(h["attr"], h["code"]), SEVERITY.get(h["code"] >> 16, "unknown")) for h in p.get("hms", [])]
    e["hms"] = [(code, sev, code in SILENT_CODES) for code, sev in hms
                if code not in SILENT_CODES or sev in ("fatal", "serious")]

    ams = p.get("ams") or {}
    fun2 = int(p["fun2"], 16) if "fun2" in p else None
    e["ams_dry_ok"] = None if fun2 is None else bool(fun2 >> 5 & 1)
    # print.fun bit 29: unsigned print-class commands are refused (None without fun: P1).
    e["signature_required"] = bool(int(p["fun"], 16) & 0x20000000) if "fun" in p else None
    # Indicators. The door sensor needs the firmware version (no get_version in
    # the captures). SD card: home_flag bits 8-9; a missing card on a printer
    # that prints from eMMC (fun2 bit 0) is not shown. Nozzle: the active
    # extruder's device.nozzle entry, else the flat fields (P1).
    e["door_open"] = None
    hf = p.get("home_flag")
    sd = None if hf is None else ("missing", "normal", "abnormal", "abnormal")[(hf >> 8) & 3]
    e["sdcard"] = None if sd == "missing" and fun2 is not None and fun2 & 1 else sd
    nozzles = (dev.get("nozzle") or {}).get("info")
    if isinstance(nozzles, list):
        n = next(x for x in nozzles if x["id"] == active)
        e["nozzle"] = {"diameter": float(n["diameter"]), "type": _nozzle_slug(n["type"])}
    else:
        e["nozzle"] = {"diameter": float(p["nozzle_diameter"]), "type": p["nozzle_type"]}
    feeding_unit = None
    if e["state"] in (PrinterState.PRINTING, PrinterState.PAUSED) and "info" in ext:
        feeding_unit = next(i["snow"] for i in ext["info"] if i["id"] == active) >> 8
    units = []
    for u in sorted(ams.get("ams", []), key=lambda u: int(u["id"])):
        raw_pct = int(u.get("humidity_raw", 0))
        slots = []
        for t in u["tray"]:
            if _tray_loaded(t):
                lo, hi = int(t.get("nozzle_temp_min") or 0), int(t.get("nozzle_temp_max") or 0)
                slots.append({"type": t["tray_type"], "color": t["tray_color"][:6],
                              "name": FILAMENTS[t["tray_info_idx"]], "remain_pct": t.get("remain", -1),
                              "empty": False, "nozzle_temp": [lo, hi] if lo > 0 and hi > 0 else None})
            else:
                slots.append({"type": None, "color": None, "name": None, "remain_pct": -1, "empty": True,
                              "nozzle_temp": None})
        units.append({"index": int(u["id"]), "humidity": int(u["humidity"]),
                      "humidity_pct": raw_pct if 1 <= raw_pct <= 100 else None,
                      "dry_time": int(u.get("dry_time", 0)) or None,
                      "temp": float(u["temp"]), "slots": slots,
                      "dry": _expected_dry(p, u, feeding_unit)})
    if units:
        if "info" in ext:
            snow = next(i["snow"] for i in ext["info"] if i["id"] == active)
            a, t = snow >> 8, snow & 3
        else:
            raw = int(ams["tray_now"])
            a, t = (255, 255) if raw == 255 else (255, 0) if raw == 254 else (raw >> 2, raw & 3)
        if a == 255:
            tray_now = 255 if t in (3, 255) else 254
        else:
            base, tray_now = 0, None
            for u in units:
                if u["index"] == a:
                    tray_now = base + t
                    break
                base += len(u["slots"])
        e["ams"] = {"tray_now": tray_now, "units": units}
    else:
        e["ams"] = None
    return e


def _expected_dry(p: dict, u: dict, feeding_unit) -> dict:
    """The payload-derived part of a unit's `dry` block (the policy part —
    limits, suggestion, presets — is pinned by test_replay_anchors)."""
    info = int(u["info"], 16)
    fun2 = int(p["fun2"], 16) if "fun2" in p else None
    remote = fun2 is not None and bool(fun2 >> 5 & 1)
    status = DRY_STATUS.get((info >> 4) & 0xF) if remote else None
    setting = u.get("dry_setting") or {}
    remaining = int(u.get("dry_time", 0)) or None
    return {
        "type": DRY_TYPES[info & 0xF],
        "status": status,
        "phase": {1: "heating", 2: "dehumidify"}.get(info >> 22 & 3) if status in ("checking", "drying") else None,
        "active": status in ("checking", "drying", "error", "overheat") if status else bool(remaining),
        "remaining_min": remaining,
        "temp": max(setting.get("dry_temperature", 0), 0) or None,
        "hours": max(setting.get("dry_duration", 0), 0) or None,
        "filament": setting.get("dry_filament") or None,
        "blocker_codes": [c for c in u.get("dry_sf_reason", []) if c != 6] if remote else [],
        "feeding": feeding_unit == int(u["id"]),
    }


def _dry_view(ams):
    """status.ams with each unit's dry block reduced to DRY_KEYS."""
    if not ams:
        return ams
    units = []
    for unit in ams["units"]:
        dry = unit.get("dry")
        if dry is not None:
            dry = {k: dry[k] for k in DRY_KEYS if k in dry}
            dry["blocker_codes"] = [b["code"] for b in unit["dry"]["blockers"]]
        units.append(dict(unit, dry=dry))
    return dict(ams, units=units)


def _hms_matches(last_error, want) -> bool:
    if not want:
        return last_error is None
    parts = (last_error or "").split(" | ")
    return len(parts) == len(want) and all(
        (part == f"HMS_{code}: unknown [{sev}] (см. {WIKI_PAGE}{code})") if silent else
        (part.startswith(f"HMS_{code}: ") and part.endswith(f" [{sev}]") and CYRILLIC.search(part))
        for part, (code, sev, silent) in zip(parts, want))


def _replay(collector, model):
    """Yield (index, merged print block, PrinterStatus) after every message."""
    state = {}
    for i, line in enumerate(kit.load_fixture(model)):
        kit.deliver(collector, line["msg"])
        kit.merge(state, line["msg"]["print"])
        yield i, state, normalize_bambu("fx", model, collector.fetch(), device_type=model)


@pytest.mark.parametrize("model", MODELS)
def test_replay_matches_payload(model, offline_collector):
    collector = offline_collector(model)
    mismatches = []
    count = 0
    for i, merged, status in _replay(collector, model):
        count += 1
        for key, want in expected(merged, model).items():
            if key == "hms":
                if not _hms_matches(status.last_error, want):
                    mismatches.append((i, "last_error", want, status.last_error))
            elif key == "ams":
                if _dry_view(status.ams) != want:
                    mismatches.append((i, key, want, _dry_view(status.ams)))
            elif getattr(status, key) != want:
                mismatches.append((i, key, want, getattr(status, key)))
    assert count == len(kit.load_fixture(model))
    assert mismatches[:10] == []


def test_replay_anchors(offline_collector):
    # Literal values from fixtures/bambu/README.md — independent of the oracle above.
    statuses = {}
    for model in MODELS:
        collector = offline_collector(model)
        statuses[model] = [status for _i, _p, status in _replay(collector, model)]

    x1c = statuses["X1C"][-1]
    assert x1c.chamber_temp == 35.0
    assert x1c.last_error.startswith("HMS_0500_0100_0003_0004: ")
    assert x1c.ams is None

    p2s = statuses["P2S"][-1]
    unit = p2s.ams["units"][0]
    assert unit["humidity_pct"] == 6
    assert p2s.ams["tray_now"] == 0
    loaded = [s for s in unit["slots"] if not s["empty"]]
    assert [(s["type"], s["name"]) for s in loaded] == [("PETG", "Bambu PETG Basic")] * 2
    silent = f"HMS_0500_0600_0002_0070: unknown [serious] (см. {WIKI_PAGE}0500_0600_0002_0070)"
    assert all(s.last_error.split(" | ")[0] == silent for s in statuses["P2S"])

    # Drying limits while that AMS feeds the print (Bambu Studio's print caps):
    # PETG at most 55 °C; on the H2S the PETG spool caps the PA-CF one too.
    for model in ("P2S", "H2S"):
        dry = statuses[model][-1].ams["units"][0]["dry"]
        assert (dry["type"], dry["status"], dry["feeding"]) == ("n3f", "off", True)
        assert (dry["max_temp"], dry["max_by"]) == (55, "PETG")
        assert dry["suggest"] == {"filament": "PETG", "temp": 55, "hours": 12}
        assert statuses[model][-1].ams_dry_ok is True

    h2s = statuses["H2S"]
    assert {s.chamber_temp for s in h2s} <= {34.0, 35.0}
    assert h2s[-1].ams["units"][0]["humidity_pct"] == 14
    assert h2s[-1].ams["tray_now"] == 1
    assert {s.stage for s in h2s} <= {"auto_bed_leveling", "cleaning_nozzle_tip"}

    p1s = statuses["P1S"]
    assert len(p1s) == 24
    # The P1S has no chamber sensor: its constant chamber_temper 5 is not shown.
    assert all(s.chamber_temp is None and s.target_chamber_temp is None and s.ams is None for s in p1s)
    # 23 delta-only messages after the pushall: missing keys keep their values.
    assert p1s[-1].state == PrinterState.PRINTING
    assert p1s[-1].bed_temp == 70.0


@pytest.mark.parametrize("model", MODELS)
def test_replay_tee_online_and_no_side_effects(model, monkeypatch, no_network, offline_collector):
    from app.services import printer_commands

    teed = []
    monkeypatch.setattr(printer_commands, "note_local_reply", lambda serial, payload: teed.append((serial, payload)))
    # A host different from the address packed into the fixtures' net.info:
    # with force_ip the lib must keep using the configured host.
    collector = offline_collector(model, host="192.0.2.20")
    sent = [kit.deliver(collector, line["msg"]) for line in kit.load_fixture(model)]

    assert teed == [(kit.FAKE_SERIALS[model], payload) for payload in sent]
    device = collector.client.get_device()
    assert device.info.online is True
    assert device.info.ip_address == "192.0.2.20"
    names = [t.name for t in kit.lib_threads()]
    assert not [n for n in names if "-Chamber-" in n or "-Media-" in n]
    assert collector.client.ftp_enabled is False
    assert collector.client.camera_enabled is False
    assert not os.path.exists(collector.client.cache_path)


@pytest.mark.parametrize("model,version,door", [
    ("X1C", "01.09.00.00", False),   # X1: home_flag bit 23 (its stat bit 23 is set throughout)
    ("P2S", "01.02.00.00", False),   # stat bit 23
    ("H2S", "01.02.00.00", False),
    ("P1S", "01.08.00.00", None),    # no door sensor
])
def test_replay_with_a_known_firmware(model, version, door, offline_collector, caplog):
    # The captures carry no get_version reply (see the fixture README), so a
    # synthetic one goes first: the firmware-gated paths (AMS_SWITCH_COMMAND,
    # DOOR_SENSOR) then run on the real reports. The door bits are clear in
    # every captured report.
    collector = offline_collector(model)
    kit.deliver(collector, {"info": {"command": "get_version", "sequence_id": "0", "module": [
        {"name": "ota", "sw_ver": version, "hw_ver": "OTA", "sn": kit.FAKE_SERIALS[model]}]}})
    with caplog.at_level("ERROR"):
        statuses = [status for _i, _p, status in _replay(collector, model)]
    assert {s.firmware_version for s in statuses} == {version}
    assert {s.filament_swap_ok for s in statuses} == {True}
    assert {s.door_open for s in statuses} == {door}
    assert not [r for r in caplog.records if "supports_feature" in r.getMessage()]


def test_get_version_sets_firmware(offline_collector):
    collector = offline_collector("X1C")
    kit.deliver(collector, {"info": {"command": "get_version", "sequence_id": "0", "module": [
        {"name": "ota", "sw_ver": "01.02.03.00", "hw_ver": "OTA", "sn": "X"}]}})
    status = normalize_bambu("fx", "X1C", collector.fetch(), device_type="X1C")
    assert status.firmware_version == "01.02.03.00"


def test_print_start_transition_sets_elapsed(no_network, offline_collector):
    # The lib stamps start_time (UTC-aware, minute-rounded) on an observed
    # idle -> busy transition; the normalizer turns it into elapsed seconds.
    collector = offline_collector("P2S")
    kit.deliver(collector, {"print": {"command": "push_status", "msg": 0, "gcode_state": "IDLE"}})
    kit.deliver(collector, {"print": {"command": "push_status", "msg": 0, "gcode_state": "RUNNING"}})
    status = normalize_bambu("fx", "P2S", collector.fetch(), device_type="P2S")
    assert isinstance(status.print_time_seconds, int)
    assert 0 <= status.print_time_seconds <= 120
    assert collector.client.get_device().print_job.start_time.tzinfo is not None


def test_fixture_payloads_are_valid_json_objects():
    # Cheap structural check: every line is {"t": float, "msg": {"print": {...}}}.
    for model in MODELS:
        for line in kit.load_fixture(model):
            assert isinstance(line["t"], (int, float))
            assert isinstance(line["msg"].get("print"), dict)
            json.dumps(line)
