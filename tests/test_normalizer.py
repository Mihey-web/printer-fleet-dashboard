"""Unit tests for telemetry normalization (app/services/normalizer.py + utils.py).

These cover the trickiest, bug-prone logic: progress fallbacks, layer-derived
progress, minutes->seconds ETA conversion, the UNKNOWN->IDLE / IDLE->PRINTING
promotions, and the "looks finished" heuristic for Creality/Klipper.

No real printers or network required — telemetry is faked with SimpleNamespace
(for the pybambu-style object graph) and plain dicts (for WebSocket brands).
"""
from types import SimpleNamespace

import pytest

from app.domain.models import PrinterKind, PrinterState
from app.services.normalizer import normalize_bambu, normalize_ws_dict
from app.services.utils import map_state, as_float, as_int, safe_job_name


# --------------------------------------------------------------------------
# utils.map_state
# --------------------------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    ("printing", PrinterState.PRINTING),
    ("running", PrinterState.PRINTING),
    ("prepare", PrinterState.PRINTING),
    ("slicing", PrinterState.PRINTING),
    ("PAUSED", PrinterState.PAUSED),
    ("pause", PrinterState.PAUSED),
    ("finish", PrinterState.FINISHED),
    ("completed", PrinterState.FINISHED),
    ("idle", PrinterState.IDLE),
    ("ready", PrinterState.IDLE),
    ("standby", PrinterState.IDLE),
    ("error", PrinterState.ERROR),
    ("failed", PrinterState.ERROR),
    ("offline", PrinterState.OFFLINE),
    ("disconnected", PrinterState.OFFLINE),
    ("   Running  ", PrinterState.PRINTING),  # whitespace + case
    ("wat", PrinterState.UNKNOWN),
    (None, PrinterState.UNKNOWN),
    ("", PrinterState.UNKNOWN),
])
def test_map_state(raw, expected):
    assert map_state(raw) == expected


def test_as_float_and_as_int():
    assert as_float("3.5") == 3.5
    assert as_float(None) is None
    assert as_float("nan-ish") is None
    assert as_int("42") == 42
    assert as_int(3.9) == 3          # truncates
    assert as_int(None) is None
    assert as_int("oops") is None


def test_safe_job_name_priority_and_empty():
    assert safe_job_name({"filename": "a.gcode", "message": "x"}) == "a.gcode"
    assert safe_job_name({"message": "hello"}) == "hello"
    assert safe_job_name({}) is None
    assert safe_job_name({"filename": ""}) is None  # falsy skipped


# --------------------------------------------------------------------------
# normalize_bambu
# --------------------------------------------------------------------------
def _bambu_result(**pj_attrs):
    """Build a minimal pybambu-like result object with a print_job."""
    pj = SimpleNamespace(**pj_attrs)
    return SimpleNamespace(print_job=pj)


def test_bambu_none_result_is_offline():
    s = normalize_bambu("bambu-1", "P1", None, device_type="X1C")
    assert s.online is False
    assert s.state == PrinterState.OFFLINE
    assert "fetch returned None" in (s.last_error or "")
    assert s.device_type == "X1C"


def test_bambu_no_print_job_is_offline():
    result = SimpleNamespace(print_job=None)
    s = normalize_bambu("bambu-1", "P1", result)
    assert s.online is False
    assert s.state == PrinterState.OFFLINE
    assert "print_job" in (s.last_error or "")


def test_bambu_printing_basic_fields_and_eta_minutes_to_seconds():
    result = _bambu_result(
        gcode_state="printing",
        print_percentage=42,
        remaining_time=30,        # pybambu reports MINUTES
        gcode_file="cube.gcode",
    )
    s = normalize_bambu("bambu-1", "P1", result)
    assert s.online is True
    assert s.state == PrinterState.PRINTING
    assert s.progress_pct == 42.0
    assert s.eta_seconds == 30 * 60   # converted to seconds
    assert s.job_name == "cube.gcode"


def test_bambu_job_name_prefers_subtask_name_over_plate_gcode():
    result = _bambu_result(
        gcode_state="printing",
        gcode_file="/data/Metadata/plate_1.gcode",
        subtask_name="Крепление NVR v3",
    )
    s = normalize_bambu("bambu-1", "P1", result)
    assert s.job_name == "Крепление NVR v3"


def test_bambu_job_name_falls_back_to_gcode_file_when_no_subtask():
    # SD-card reprints arrive without subtask_name — gcode_file is all we have.
    result = _bambu_result(
        gcode_state="printing",
        gcode_file="plate_2.gcode",
        subtask_name="",
    )
    s = normalize_bambu("bambu-1", "P1", result)
    assert s.job_name == "plate_2.gcode"


def test_bambu_progress_fallback_to_percent():
    result = _bambu_result(gcode_state="printing", percent=55)
    s = normalize_bambu("bambu-1", "P1", result)
    assert s.progress_pct == 55.0


def test_bambu_progress_derived_from_layers_when_no_percent():
    result = _bambu_result(gcode_state="printing", layer_num=50, total_layer_num=200)
    s = normalize_bambu("bambu-1", "P1", result)
    assert s.current_layer == 50
    assert s.total_layers == 200
    assert s.progress_pct == 25.0   # 50/200*100


def test_bambu_unknown_state_promoted_to_idle():
    result = _bambu_result(gcode_state="unknown")
    s = normalize_bambu("bambu-1", "P1", result)
    assert s.state == PrinterState.IDLE


def test_bambu_no_remaining_time_leaves_eta_none():
    result = _bambu_result(gcode_state="printing", print_percentage=10)
    s = normalize_bambu("bambu-1", "P1", result)
    assert s.eta_seconds is None


def test_bambu_hms_errors_surface_as_last_error():
    # An errored printer must report WHAT is wrong, not just go red.
    result = _bambu_result(gcode_state="failed", print_error=50348044)
    result.hms = SimpleNamespace(
        count=2,
        errors={
            "0-Error": "HMS_0300_0100_0001_0007: Nozzle temperature malfunction",
            "0-Severity": "fatal",
            "0-Wiki": "https://wiki.bambulab.com/...",
            "1-Error": "HMS_0C00_0100_0002_0001: Heatbed temperature abnormal",
            "1-Severity": "serious",
        },
    )
    s = normalize_bambu("bambu-1", "P1", result)
    assert s.state == PrinterState.ERROR
    assert "Nozzle temperature malfunction [fatal]" in s.last_error
    assert "Heatbed temperature abnormal [serious]" in s.last_error
    assert "0x0300400C" in s.last_error  # print_error rendered in hex


def test_bambu_unknown_hms_code_appends_wiki_link():
    # Newer models (P2S) hit codes pybambu can't name — keep the wiki link so the
    # code is still actionable instead of a dead "unknown".
    result = _bambu_result(gcode_state="failed")
    wiki = "https://wiki.bambulab.com/en/x1/troubleshooting/hmscode/0500_0600_0002_0070"
    result.hms = SimpleNamespace(
        count=1,
        errors={
            "0-Error": "HMS_0500_0600_0002_0070: unknown",
            "0-Severity": "serious",
            "0-Wiki": wiki,
        },
    )
    s = normalize_bambu("bambu-1", "P1", result)
    assert wiki in s.last_error
    assert "HMS_0500_0600_0002_0070" in s.last_error


def test_bambu_healthy_printer_has_no_last_error():
    result = _bambu_result(gcode_state="printing", print_percentage=10, print_error=0)
    result.hms = SimpleNamespace(count=0, errors={})
    s = normalize_bambu("bambu-1", "P1", result)
    assert s.last_error is None


# --------------------------------------------------------------------------
# normalize_bambu: AMS
# --------------------------------------------------------------------------
def _bambu_result_with_ams(ams):
    pj = SimpleNamespace(gcode_state="RUNNING")
    return SimpleNamespace(print_job=pj, ams=ams)


def _p2s_ams(tray_now=3):
    """Live-shaped P2S payload: one real unit (PETG in slot 4) + a placeholder."""
    def empty_tray():
        # pybambu reports empty=False even for empty slots — the normalizer
        # must derive emptiness from color/type instead.
        return SimpleNamespace(color="00000000", type="", name="unknown", remain=0, empty=False)

    real_unit = SimpleNamespace(
        tray=[empty_tray(), empty_tray(), empty_tray(),
              SimpleNamespace(color="FFF144FF", type="PETG", name="Generic PETG", remain=-1, empty=False)],
        humidity_index=2,
        humidity_raw=24,  # grafted by bambu_collector's print_update wrapper
        dry_time=0,       # ditto; >0 while the drying cycle runs
        temperature=39.9,
    )
    placeholder = SimpleNamespace(tray=[], humidity_index=None, temperature=None)
    return SimpleNamespace(tray_now=tray_now, data=[real_unit, placeholder])


def test_bambu_ams_real_unit_extracted():
    s = normalize_bambu("bambu-2", "P2S", _bambu_result_with_ams(_p2s_ams()))
    assert s.ams is not None
    assert s.ams["tray_now"] == 3
    assert len(s.ams["units"]) == 1  # placeholder unit dropped
    unit = s.ams["units"][0]
    assert unit["humidity"] == 2
    assert unit["humidity_pct"] == 24
    assert unit["dry_time"] == 0
    assert unit["temp"] == 39.9
    assert unit["slots"][3] == {
        "type": "PETG", "color": "FFF144", "name": "Generic PETG",
        "remain_pct": -1, "empty": False,
    }


def test_bambu_ams_humidity_pct_absent_is_none():
    # Classic AMS units have no humidity_raw — only the 5-level index
    ams = _p2s_ams()
    del ams.data[0].humidity_raw
    s = normalize_bambu("bambu-2", "P2S", _bambu_result_with_ams(ams))
    assert s.ams["units"][0]["humidity_pct"] is None
    assert s.ams["units"][0]["humidity"] == 2


def test_bambu_ams_empty_slot_derived_from_color_not_flag():
    s = normalize_bambu("bambu-2", "P2S", _bambu_result_with_ams(_p2s_ams()))
    empty_slot = s.ams["units"][0]["slots"][0]
    assert empty_slot["empty"] is True   # despite pybambu's empty=False
    assert empty_slot["color"] is None
    assert empty_slot["type"] is None


def test_bambu_ams_placeholders_only_is_none():
    # X1C without AMS: 4 placeholder units, external spool
    ams = SimpleNamespace(
        tray_now=254,
        data=[SimpleNamespace(tray=[], humidity_index=None, temperature=None) for _ in range(4)],
    )
    s = normalize_bambu("bambu-6", "X1C", _bambu_result_with_ams(ams))
    assert s.ams is None


def test_bambu_ams_attribute_missing_is_none():
    s = normalize_bambu("bambu-1", "P1", _bambu_result(gcode_state="RUNNING"))
    assert s.ams is None


def test_bambu_debug_has_no_ams_summary():
    # RUNNING → PRINTING → debug is kept; ams_summary must be gone from it
    s = normalize_bambu("bambu-2", "P2S", _bambu_result_with_ams(_p2s_ams()))
    assert s.state == PrinterState.PRINTING
    assert "ams_summary" not in s.debug


def test_bambu_fans_and_fw_update():
    from pybambu.const import FansEnum

    class _Fans:
        PART_COOLING = "part"

        def get_fan_speed(self, member):
            return {"part": 100, FansEnum.AUXILIARY: 70,
                    FansEnum.CHAMBER: 40, FansEnum.HEATBREAK: 100}[member]

    result = _bambu_result(gcode_state="RUNNING")
    result.fans = _Fans()
    result.info = SimpleNamespace(wifi_signal=-50, sw_ver="01.08.05.00", new_version_state=1)
    s = normalize_bambu("bambu-7", "X1C", result)
    assert s.fan_speed_pct == 100
    assert s.fans == {"aux": 70, "chamber": 40, "heatbreak": 100}
    assert s.fw_update is True


def test_bambu_fw_update_unknown_and_no_fans():
    result = _bambu_result(gcode_state="RUNNING")
    result.info = SimpleNamespace(wifi_signal=-50, sw_ver="01.07.01.00", new_version_state=0)
    s = normalize_bambu("bambu-6", "X1C", result)
    assert s.fw_update is None
    assert s.fans is None


# --------------------------------------------------------------------------
# normalize_ws_dict (Creality / Klipper / MKS)
# --------------------------------------------------------------------------
def test_ws_empty_dict_is_offline():
    s = normalize_ws_dict("creality-1", "K1", PrinterKind.CREALITY, {})
    assert s.online is False
    assert s.state == PrinterState.OFFLINE
    assert s.last_error == "No telemetry data received"


def test_ws_printing_basic_mapping():
    data = {
        "state": "printing",
        "progress": 33.0,
        "remaining_time": 600,
        "nozzle_temp": 210.0,
        "bed_temp": 60.0,
        "filename": "part.gcode",
        "current_layer": 5,
        "total_layers": 100,
    }
    s = normalize_ws_dict("creality-1", "K1", PrinterKind.CREALITY, data)
    assert s.online is True
    assert s.state == PrinterState.PRINTING
    assert s.progress_pct == 33.0
    assert s.eta_seconds == 600
    assert s.nozzle_temp == 210.0
    assert s.job_name == "part.gcode"


def test_ws_looks_finished_on_full_progress_no_time():
    # Firmware still says "printing" but progress is 100% and no time remains.
    data = {"state": "printing", "progress": 100, "remaining_time": 0}
    s = normalize_ws_dict("creality-1", "K1", PrinterKind.CREALITY, data)
    assert s.state == PrinterState.FINISHED


def test_ws_looks_finished_by_layers():
    data = {"state": "printing", "current_layer": 120, "total_layers": 120,
            "remaining_time": 0}
    s = normalize_ws_dict("klipper-1", "Varan", PrinterKind.KLIPPER, data)
    assert s.state == PrinterState.FINISHED


def test_ws_paused_at_full_progress_is_finished():
    # Creality K1/K1C/Ender Klipper auto-pause at end of job -> state="paused"
    # while progress is 100% and no time remains. Must read as FINISHED, not "Пауза".
    data = {"state": "paused", "progress": 100, "remaining_time": 0}
    s = normalize_ws_dict("creality-3", "K1C", PrinterKind.CREALITY, data)
    assert s.state == PrinterState.FINISHED


def test_ws_paused_by_layers_is_finished():
    data = {"state": "paused", "current_layer": 883, "total_layers": 883,
            "remaining_time": 0}
    s = normalize_ws_dict("creality-3", "K1C", PrinterKind.CREALITY, data)
    assert s.state == PrinterState.FINISHED


def test_ws_genuine_midprint_pause_stays_paused():
    # A real pause mid-print keeps progress < 100 and time remaining -> stays PAUSED.
    data = {"state": "paused", "progress": 47, "remaining_time": 1800}
    s = normalize_ws_dict("creality-3", "K1C", PrinterKind.CREALITY, data)
    assert s.state == PrinterState.PAUSED


def test_ws_idle_promoted_to_printing_with_active_progress():
    data = {"state": "idle", "progress": 50, "remaining_time": 300}
    s = normalize_ws_dict("creality-1", "K1", PrinterKind.CREALITY, data)
    assert s.state == PrinterState.PRINTING


def test_ws_idle_not_promoted_when_only_hot_nozzle():
    # A cooling nozzle is NOT evidence of an active job — must stay IDLE.
    data = {"state": "idle", "nozzle_temp": 200}
    s = normalize_ws_dict("creality-1", "K1", PrinterKind.CREALITY, data)
    assert s.state == PrinterState.IDLE


def test_ws_creality_filament_runout_midprint_shows_paused():
    # K1 firmware keeps state="printing" during a filament runout; the only
    # signal is materialStatus != 0. The card must flip to PAUSED with a
    # runout stage instead of pretending the job is still printing.
    data = {"state": "printing", "progress": 23, "remaining_time": 15453,
            "material_status": 1}
    s = normalize_ws_dict("creality-1", "K1", PrinterKind.CREALITY, data)
    assert s.state == PrinterState.PAUSED
    assert s.stage == "paused_filament_runout"


def test_ws_creality_filament_runout_with_zero_eta_shows_paused():
    # Observed live (creality-3, 2026-07-03): runout froze progress at 2% and
    # zeroed printLeftTime while state stayed "printing".
    data = {"state": "printing", "progress": 2, "remaining_time": 0,
            "material_status": 1}
    s = normalize_ws_dict("creality-3", "K1C", PrinterKind.CREALITY, data)
    assert s.state == PrinterState.PAUSED
    assert s.stage == "paused_filament_runout"


def test_ws_creality_runout_stage_added_to_firmware_pause():
    # If firmware does report "paused" during a runout, keep PAUSED but
    # surface the reason via the stage.
    data = {"state": "paused", "progress": 47, "remaining_time": 1800,
            "material_status": 1}
    s = normalize_ws_dict("creality-3", "K1C", PrinterKind.CREALITY, data)
    assert s.state == PrinterState.PAUSED
    assert s.stage == "paused_filament_runout"


def test_ws_creality_material_ok_stays_printing():
    data = {"state": "printing", "progress": 23, "remaining_time": 15453,
            "material_status": 0}
    s = normalize_ws_dict("creality-1", "K1", PrinterKind.CREALITY, data)
    assert s.state == PrinterState.PRINTING
    assert s.stage is None


def test_ws_creality_runout_on_idle_printer_stays_idle():
    # An idle printer with no filament loaded must not show a bogus "Пауза".
    data = {"state": "idle", "material_status": 1}
    s = normalize_ws_dict("creality-1", "K1", PrinterKind.CREALITY, data)
    assert s.state == PrinterState.IDLE


def test_ws_creality_runout_at_full_progress_still_finished():
    # End-of-job auto-pause wins over the runout flag: the job is done.
    data = {"state": "printing", "progress": 100, "remaining_time": 0,
            "material_status": 1}
    s = normalize_ws_dict("creality-1", "K1", PrinterKind.CREALITY, data)
    assert s.state == PrinterState.FINISHED


def test_ws_klipper_ignores_material_status():
    # material_status is a Creality-only field; Klipper printers must not
    # be demoted by a stray key.
    data = {"state": "printing", "progress": 50, "remaining_time": 300,
            "material_status": 1}
    s = normalize_ws_dict("klipper-1", "Varan", PrinterKind.KLIPPER, data)
    assert s.state == PrinterState.PRINTING


def test_ws_chamber_temp_zero_becomes_none():
    data = {"state": "printing", "chamber_temp": 0}
    s = normalize_ws_dict("creality-1", "K1", PrinterKind.CREALITY, data)
    assert s.chamber_temp is None


def test_p2s_chamber_temp_from_ctc_device_path():
    """P2S шлёт температуру камеры вложенно в device.ctc.info.temp — коллектор
    патчит pybambu, чтобы её подхватить. У X1C приоритет плоского chamber_temper."""
    import app.collectors.bambu_collector  # noqa: F401 — применяет монкипатч
    from pybambu import models as pbm

    t = pbm.Temperature()
    t.print_update({"bed_temper": 70, "nozzle_temper": 255,
                    "device": {"ctc": {"info": {"temp": 43}, "state": 0}}})
    assert t.chamber_temp == 43

    # плоский chamber_temper (X1C) имеет приоритет над device.ctc
    t2 = pbm.Temperature()
    t2.print_update({"chamber_temper": 30, "device": {"ctc": {"info": {"temp": 43}}}})
    assert t2.chamber_temp == 30

    # бит-паковка device.bed.info.temp игнорируется (нет ctc → камеры нет)
    t3 = pbm.Temperature()
    t3.print_update({"device": {"bed": {"info": {"temp": 4587590}}}})
    assert t3.chamber_temp == 0


# --- Phase-3 normalizer regressions ------------------------------------------

def test_bambu_ams_empty_slot_marked_by_type_string():
    # X1C reports an empty slot as type="Empty" while still carrying a leftover
    # colour. It must read as empty (no filament chip), not a real material.
    empty = SimpleNamespace(color="0C0C0CFF", type="Empty", name="", remain=0)
    real = SimpleNamespace(color="FF0000FF", type="PLA", name="Red", remain=80)
    unit = SimpleNamespace(tray=[empty, real], humidity_index=1,
                           temperature=30.0, humidity_raw=20, dry_time=0)
    ams = SimpleNamespace(tray_now=1, data=[unit])
    s = normalize_bambu("bambu-x", "X1C", _bambu_result_with_ams(ams))
    slot0 = s.ams["units"][0]["slots"][0]
    assert slot0["empty"] is True
    assert slot0["color"] is None
    assert slot0["type"] is None
    slot1 = s.ams["units"][0]["slots"][1]
    assert slot1["empty"] is False
    assert slot1["type"] == "PLA"
    assert slot1["color"] == "FF0000"


def test_bambu_ams_unit_index_is_physical_not_list_position():
    # data[0] = tray-less placeholder (dropped), data[1] = the real unit. The
    # survivor must carry physical index 1, so its history never collides with a
    # different physical unit that later occupies filtered-list position 0.
    placeholder = SimpleNamespace(tray=[], humidity_index=None, temperature=None)
    real_tray = SimpleNamespace(color="FF0000FF", type="PLA", name="Red", remain=50)
    real = SimpleNamespace(tray=[real_tray], humidity_index=1, temperature=30.0,
                           humidity_raw=20, dry_time=0)
    ams = SimpleNamespace(tray_now=1, data=[placeholder, real])
    s = normalize_bambu("bambu-x", "X1C", _bambu_result_with_ams(ams))
    assert len(s.ams["units"]) == 1
    assert s.ams["units"][0]["index"] == 1


def test_ws_new_print_start_with_stale_layers_not_finished():
    # At the very start of a new print firmware still echoes the previous job's
    # completed layer count and hasn't set an ETA yet. A reported (low) progress
    # must keep it PRINTING, not flip it to FINISHED on stale layers.
    data = {"state": "printing", "progress": 3, "current_layer": 250,
            "total_layers": 250, "remaining_time": 0}
    s = normalize_ws_dict("creality-1", "K1", PrinterKind.CREALITY, data)
    assert s.state == PrinterState.PRINTING
