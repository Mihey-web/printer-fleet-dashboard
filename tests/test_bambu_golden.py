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


def _fan_pct(raw) -> int:
    raw = int(raw or 0)
    return 0 if not raw else math.ceil(raw / 15 * 100 / 10) * 10


def _hms_code(attr: int, code: int) -> str:
    return f"{attr >> 16:04X}_{attr & 0xFFFF:04X}_{code >> 16:04X}_{code & 0xFFFF:04X}"


def _tray_loaded(tray: dict) -> bool:
    if "state" in tray:
        state = int(tray["state"])
        if not state & 1:
            return False
        return state == 3 if state <= 3 else bool(state & 8)
    return bool(tray.get("tray_info_idx")) or tray.get("tray_type", "") not in ("", "Empty")


def expected(p: dict) -> dict:
    """Expected PrinterStatus fields from the merged `print` block alone."""
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
    e["chamber_temp"] = float(cur) if 0 < cur < 200 else None
    e["target_chamber_temp"] = float(tgt) if e["chamber_temp"] is not None and 0 < tgt < 200 else None
    e["stage"] = STAGES[int(p["stg_cur"])]
    lights = {x["node"]: x["mode"] for x in p.get("lights_report", [])}
    e["light_on"] = {"on": True, "off": False}.get(lights.get("chamber_light"))
    e["fan_speed_pct"] = _fan_pct(p.get("cooling_fan_speed"))
    e["fans"] = {"aux": _fan_pct(p.get("big_fan1_speed")),
                 "chamber": _fan_pct(p.get("big_fan2_speed")),
                 "heatbreak": _fan_pct(p.get("heatbreak_fan_speed"))}
    nvs = (p.get("upgrade_state") or {}).get("new_version_state", 0)
    e["fw_update"] = None if nvs == 0 else nvs == 1
    e["job_name"] = p.get("subtask_name") or p.get("gcode_file") or None
    e["firmware_version"] = "unknown"    # no fixture carries a get_version reply
    e["print_time_seconds"] = None       # captured mid-print: no idle->busy transition
    hms = [(_hms_code(h["attr"], h["code"]), SEVERITY.get(h["code"] >> 16, "unknown")) for h in p.get("hms", [])]
    e["hms"] = [(code, sev, code in SILENT_CODES) for code, sev in hms
                if code not in SILENT_CODES or sev in ("fatal", "serious")]

    ams = p.get("ams") or {}
    units = []
    for u in sorted(ams.get("ams", []), key=lambda u: int(u["id"])):
        raw_pct = int(u.get("humidity_raw", 0))
        slots = []
        for t in u["tray"]:
            if _tray_loaded(t):
                slots.append({"type": t["tray_type"], "color": t["tray_color"][:6],
                              "name": FILAMENTS[t["tray_info_idx"]], "remain_pct": t.get("remain", -1),
                              "empty": False})
            else:
                slots.append({"type": None, "color": None, "name": None, "remain_pct": -1, "empty": True})
        units.append({"index": int(u["id"]), "humidity": int(u["humidity"]),
                      "humidity_pct": raw_pct if 1 <= raw_pct <= 100 else None,
                      "dry_time": int(u.get("dry_time", 0)) or None,
                      "temp": float(u["temp"]), "slots": slots})
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
        for key, want in expected(merged).items():
            if key == "hms":
                if not _hms_matches(status.last_error, want):
                    mismatches.append((i, "last_error", want, status.last_error))
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

    h2s = statuses["H2S"]
    assert {s.chamber_temp for s in h2s} <= {34.0, 35.0}
    assert h2s[-1].ams["units"][0]["humidity_pct"] == 14
    assert h2s[-1].ams["tray_now"] == 1
    assert {s.stage for s in h2s} <= {"auto_bed_leveling", "cleaning_nozzle_tip"}

    p1s = statuses["P1S"]
    assert len(p1s) == 24
    assert all(s.chamber_temp == 5.0 and s.ams is None for s in p1s)
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
