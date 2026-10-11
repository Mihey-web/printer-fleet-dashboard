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
    # An errored printer must report WHAT is wrong, not just go red. The vendored
    # lib keeps the code in "<i>-Code" and only the (Russian) text in "<i>-Error".
    result = _bambu_result(gcode_state="failed", print_error=50348044)
    result.hms = SimpleNamespace(
        error_count=2,
        errors={
            "Count": 2,
            "1-Code": "HMS_0300_0100_0001_0007",
            "1-Error": "Неисправность датчика температуры сопла",
            "1-Severity": "fatal",
            "1-Wiki": "https://wiki.bambulab.com/...",
            "2-Code": "HMS_0C00_0100_0002_0001",
            "2-Error": "Температура стола вне нормы",
            "2-Severity": "serious",
        },
    )
    s = normalize_bambu("bambu-1", "P1", result)
    assert s.state == PrinterState.ERROR
    assert s.last_error.split(" | ") == [
        "HMS_0300_0100_0001_0007: Неисправность датчика температуры сопла [fatal]",
        "HMS_0C00_0100_0002_0001: Температура стола вне нормы [serious]",
        "Код ошибки печати: 0x0300400C",   # print_error rendered in hex
    ]


def test_bambu_hms_entries_ordered_by_numeric_index():
    errors = {"Count": 10}
    for i in range(1, 11):
        errors.update({f"{i}-Code": f"HMS_{i:04d}_0000_0000_0000", f"{i}-Error": f"текст {i}"})
    result = _bambu_result(gcode_state="failed")
    result.hms = SimpleNamespace(error_count=10, errors=errors)
    s = normalize_bambu("bambu-1", "P1", result)
    assert [p.split(":")[0] for p in s.last_error.split(" | ")] == [
        f"HMS_{i:04d}_0000_0000_0000" for i in range(1, 11)]


def test_bambu_unknown_hms_code_appends_wiki_link():
    # A code missing from Bambu's tables resolves to "unknown" — keep the code
    # and a wiki link so it is still actionable instead of a dead "unknown".
    # The lib only has the generic HMS home page for such codes; link the
    # code's own page, as pybambu 1.0.1 did.
    result = _bambu_result(gcode_state="failed")
    result.hms = SimpleNamespace(
        error_count=1,
        errors={
            "Count": 1,
            "1-Code": "HMS_0FFF_0000_0002_0001",
            "1-Error": "unknown",
            "1-Severity": "serious",
            "1-Wiki": "https://wiki.bambulab.com/en/hms/home",
        },
    )
    s = normalize_bambu("bambu-1", "P1", result)
    assert s.last_error == ("HMS_0FFF_0000_0002_0001: unknown [serious] (см. "
                            "https://wiki.bambulab.com/en/x1/troubleshooting/hmscode/0FFF_0000_0002_0001)")


def test_bambu_unknown_hms_code_keeps_the_libs_own_wiki_page():
    wiki = "https://wiki.bambulab.com/en/h2/troubleshooting/hmscode/0700_2000_0002_0025"
    result = _bambu_result(gcode_state="failed")
    result.hms = SimpleNamespace(error_count=1, errors={
        "Count": 1, "1-Code": "HMS_0700_2100_0002_0025", "1-Error": "unknown",
        "1-Severity": "serious", "1-Wiki": wiki})
    s = normalize_bambu("bambu-1", "P1", result)
    assert s.last_error == f"HMS_0700_2100_0002_0025: unknown [serious] (см. {wiki})"


def test_bambu_hms_entry_without_code_is_text_only():
    result = _bambu_result(gcode_state="failed")
    result.hms = SimpleNamespace(error_count=1, errors={
        "Count": 1, "1-Error": "Открыта дверца", "1-Severity": "common"})
    s = normalize_bambu("bambu-1", "P1", result)
    assert s.last_error == "Открыта дверца [common]"


def test_bambu_healthy_printer_has_no_last_error():
    result = _bambu_result(gcode_state="printing", print_percentage=10, print_error=0)
    result.hms = SimpleNamespace(error_count=0, errors={"Count": 0})
    s = normalize_bambu("bambu-1", "P1", result)
    assert s.last_error is None


# --------------------------------------------------------------------------
# normalize_bambu: AMS
# --------------------------------------------------------------------------
def _bambu_result_with_ams(ams):
    pj = SimpleNamespace(gcode_state="RUNNING")
    return SimpleNamespace(print_job=pj, ams=ams)


def _unit(trays, humidity_index=2, humidity=24, remaining_drying_time=0, temperature=39.9):
    """AMSInstance-shaped fake (vendored lib): humidity = % from humidity_raw,
    remaining_drying_time = dry_time minutes; both default to 0 when unreported."""
    return SimpleNamespace(tray=trays, humidity_index=humidity_index, humidity=humidity,
                           remaining_drying_time=remaining_drying_time, temperature=temperature)


def _p2s_ams(active_tray=3):
    """Live-shaped P2S AMS: one real unit (PETG in slot 4) + a tray-less unit."""
    def empty_tray():
        # The emptiness rule must not trust the tray's own flag: it is derived
        # from the material type / colour instead.
        return SimpleNamespace(color="00000000", type="", name="unknown", remain=0, empty=False)

    real_unit = _unit([empty_tray(), empty_tray(), empty_tray(),
                       SimpleNamespace(color="FFF144FF", type="PETG", name="Generic PETG", remain=-1, empty=False)])
    trayless = _unit([], humidity_index=0, humidity=0, temperature=0)
    return SimpleNamespace(data={0: real_unit, 1: trayless}, active_ams_index=0, active_tray_index=active_tray)


def test_bambu_ams_real_unit_extracted():
    s = normalize_bambu("bambu-2", "P2S", _bambu_result_with_ams(_p2s_ams()))
    assert s.ams is not None
    assert s.ams["tray_now"] == 3
    assert len(s.ams["units"]) == 1  # tray-less unit dropped
    unit = s.ams["units"][0]
    assert unit["index"] == 0
    assert unit["humidity"] == 2
    assert unit["humidity_pct"] == 24
    assert unit["dry_time"] is None   # 0 = not drying
    assert unit["temp"] == 39.9
    assert unit["slots"][3] == {
        "type": "PETG", "color": "FFF144", "name": "Generic PETG",
        "remain_pct": -1, "empty": False,
    }


def test_bambu_ams_humidity_pct_absent_is_none():
    # Classic AMS units report no humidity_raw: the lib keeps its default 0 —
    # "never reported", not 0 %. Only the 5-level index is real.
    ams = _p2s_ams()
    ams.data[0].humidity = 0
    s = normalize_bambu("bambu-2", "P2S", _bambu_result_with_ams(ams))
    assert s.ams["units"][0]["humidity_pct"] is None
    assert s.ams["units"][0]["humidity"] == 2


def test_bambu_ams_drying_time_reported():
    ams = _p2s_ams()
    ams.data[0].remaining_drying_time = 240
    s = normalize_bambu("bambu-2", "P2S", _bambu_result_with_ams(ams))
    assert s.ams["units"][0]["dry_time"] == 240


def test_bambu_ams_empty_slot_derived_from_color_not_flag():
    s = normalize_bambu("bambu-2", "P2S", _bambu_result_with_ams(_p2s_ams()))
    empty_slot = s.ams["units"][0]["slots"][0]
    assert empty_slot["empty"] is True   # despite the tray's empty=False
    assert empty_slot["color"] is None
    assert empty_slot["type"] is None


def test_bambu_ams_placeholders_only_is_none():
    # X1C without AMS: no units at all; a unit without trays counts as none too.
    for data in ({}, {0: _unit([])}):
        ams = SimpleNamespace(data=data, active_ams_index=255, active_tray_index=0)
        s = normalize_bambu("bambu-6", "X1C", _bambu_result_with_ams(ams))
        assert s.ams is None


def test_bambu_ams_attribute_missing_is_none():
    s = normalize_bambu("bambu-1", "P1", _bambu_result(gcode_state="RUNNING"))
    assert s.ams is None


def test_bambu_ams_unknown_material_sentinel():
    # The vendored lib marks a loaded spool with no material assigned as "?".
    no_colour = SimpleNamespace(color="00000000", type="?", name="?", remain=-1)
    coloured = SimpleNamespace(color="FF0000FF", type="?", name="?", remain=-1)
    ams = SimpleNamespace(data={0: _unit([no_colour, coloured])}, active_ams_index=0, active_tray_index=1)
    slots = normalize_bambu("bambu-2", "P2S", _bambu_result_with_ams(ams)).ams["units"][0]["slots"]
    assert slots[0] == {"type": None, "color": None, "name": None, "remain_pct": -1, "empty": True}
    assert slots[1] == {"type": None, "color": "FF0000", "name": None, "remain_pct": -1, "empty": False}


def _units(*sizes_by_index):
    return [{"index": idx, "slots": [{}] * size} for idx, size in sizes_by_index]


@pytest.mark.parametrize("active_ams,active_tray,units,expected", [
    (255, 255, _units((0, 4)), 255),            # legacy tray_now 255: nothing loaded
    (255, 3, _units((0, 4)), 255),              # snow 0xFFFF: nothing loaded
    (255, 0, _units((0, 4)), 254),              # legacy 254 / snow 0xFF00: external spool
    (254, 0, _units((0, 4)), 254),              # second external spool (H2D vir_slot)
    (0, 2, _units((0, 4)), 2),
    (1, 0, _units((0, 4), (1, 4)), 4),          # cumulative slot index, like app.js amsChip
    (128, 0, _units((0, 4), (128, 1)), 4),      # AMS HT (1 slot) after a 4-slot unit
    (1, 3, _units((1, 4)), 3),                  # sparse set: unit 1 is the first one shown
    (0, 5, _units((0, 4)), None),               # tray index out of range
    (7, 0, _units((0, 4)), None),               # unit not reported
    (None, 0, _units((0, 4)), None),
])
def test_tray_now_reconstruction(active_ams, active_tray, units, expected):
    from app.services.normalizer import _tray_now

    ams = SimpleNamespace(active_ams_index=active_ams, active_tray_index=active_tray)
    assert _tray_now(ams, units) == expected


def test_tray_now_survives_odd_extruder_index():
    # active_*_index look the active extruder up in a dict keyed 0/1/15 and raise
    # KeyError for any other index — getattr's default only covers AttributeError.
    from app.services.normalizer import _tray_now

    class _Ams:
        @property
        def active_ams_index(self):
            raise KeyError(7)

        active_tray_index = 0

    assert _tray_now(_Ams(), _units((0, 4))) is None


def test_bambu_ams_snapshot_survives_concurrent_insert():
    # The MQTT thread inserts AMS units into ams.data while the poller reads it.
    from app.services.normalizer import _build_ams

    ht_unit = _unit([SimpleNamespace(color="FF0000FF", type="PLA", name="Generic PLA", remain=80)])

    class _GrowingDict(dict):
        calls = 0

        def items(self):
            _GrowingDict.calls += 1
            view = super().items()
            if _GrowingDict.calls > 1:
                return view

            def resized_mid_copy():
                it = iter(view)
                first = next(it)
                dict.__setitem__(self, 200, ht_unit)   # the paho thread wins the race
                yield first
                yield from it                         # -> dictionary changed size

            return resized_mid_copy()

    data = _GrowingDict({0: _p2s_ams().data[0]})
    ams = SimpleNamespace(data=data, active_ams_index=0, active_tray_index=3)
    out = _build_ams(ams)
    assert [u["index"] for u in out["units"]] == [0, 200]
    assert _GrowingDict.calls == 2


def test_bambu_debug_has_no_ams_summary():
    # RUNNING → PRINTING → debug is kept; ams_summary must be gone from it
    s = normalize_bambu("bambu-2", "P2S", _bambu_result_with_ams(_p2s_ams()))
    assert s.state == PrinterState.PRINTING
    assert "ams_summary" not in s.debug


def test_bambu_fans_and_fw_update():
    from app.vendor.pybambu.const import FansEnum

    class _Fans:
        def get_fan_speed(self, member):
            return {FansEnum.PART_COOLING: 100, FansEnum.AUXILIARY: 70,
                    FansEnum.CHAMBER: 40, FansEnum.HEATBREAK: 100}[member]

    result = _bambu_result(gcode_state="RUNNING")
    result.fans = _Fans()
    result.info = SimpleNamespace(wifi_signal=-50, sw_ver="01.08.05.00", new_version_state=1)
    s = normalize_bambu("bambu-7", "X1C", result)
    assert s.fan_speed_pct == 100
    assert s.fans == {"aux": 70, "chamber": 40, "heatbreak": 100}
    assert s.fw_update is True


def test_bambu_part_cooling_fan_read_via_fans_enum():
    # The library's Fans object has no PART_COOLING attribute — the member lives
    # on FansEnum. Reading it off the fans object raised AttributeError, which was
    # swallowed, so the part-cooling fan speed was never shown for Bambu.
    from app.vendor.pybambu.const import FansEnum

    class _Fans:
        def get_fan_speed(self, member):
            return {FansEnum.PART_COOLING: 60, FansEnum.AUXILIARY: 0,
                    FansEnum.CHAMBER: 0, FansEnum.HEATBREAK: 100}[member]

    fans = _Fans()
    assert not hasattr(fans, "PART_COOLING")
    result = _bambu_result(gcode_state="RUNNING")
    result.fans = fans
    s = normalize_bambu("bambu-7", "X1C", result)
    assert s.fan_speed_pct == 60
    assert s.fans == {"aux": 0, "chamber": 0, "heatbreak": 100}


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


# --- normalize_bambu over a real vendored Device ------------------------------

def _normalized(make_collector, *messages, model="P2S"):
    """Feed print payloads into a real vendored Device, then normalize it."""
    device = make_collector("t", "192.0.2.10", "00000000", "22E00A000000000", model).client.get_device()
    for msg in messages:
        device.print_update(data=msg)
    return device, normalize_bambu("t-1", "t", device, device_type=model)


def test_p2s_chamber_temp_from_ctc_device_path(make_collector):
    """P2S/H2S шлют температуру камеры вложенно в device.ctc.info.temp,
    X1/P1 — плоским chamber_temper; вендоренный pybambu читает оба."""
    _d, s = _normalized(make_collector, {"bed_temper": 70, "nozzle_temper": 255,
                                         "device": {"ctc": {"info": {"temp": 43}, "state": 0}}})
    assert (s.chamber_temp, s.target_chamber_temp) == (43.0, None)

    _d, s = _normalized(make_collector, {"chamber_temper": 30})
    assert s.chamber_temp == 30.0

    # Осознанное изменение: при обоих источниках теперь побеждает ctc (раньше —
    # плоский). Реальные принтеры шлют что-то одно.
    _d, s = _normalized(make_collector, {"chamber_temper": 30, "device": {"ctc": {"info": {"temp": 43}}}})
    assert s.chamber_temp == 43.0

    # бит-паковка device.bed.info.temp — это стол, не камера
    _d, s = _normalized(make_collector, {"device": {"bed": {"info": {"temp": 4587590}}}})
    assert s.chamber_temp is None
    assert (s.bed_temp, s.target_bed_temp) == (70.0, 70.0)


def test_nozzle_temp_from_packed_extruder(make_collector):
    # H2/P2/new X1 firmware: device.extruder.info[].temp = (target << 16) | current,
    # active extruder = (state >> 4) & 0xF.
    _d, s = _normalized(make_collector, {"gcode_state": "RUNNING", "nozzle_temper": 160.6,
                                         "device": {"extruder": {"state": 1, "info": [
                                             {"id": 0, "temp": (220 << 16) | 215}]}}}, model="H2S")
    assert (s.nozzle_temp, s.target_nozzle_temp) == (215.0, 220.0)


def test_nozzle_temp_from_flat_keys(make_collector):
    # Classic P1 protocol: flat nozzle_temper / nozzle_target_temper, rounded.
    _d, s = _normalized(make_collector, {"gcode_state": "RUNNING", "nozzle_temper": 219.6,
                                         "nozzle_target_temper": 220}, model="P1S")
    assert (s.nozzle_temp, s.target_nozzle_temp) == (220.0, 220.0)


def test_odd_active_extruder_index_degrades_to_none(make_collector):
    # (0x71 >> 4) & 0xF = 7: the lib's per-extruder dicts have no key 7 and its
    # active_* properties raise KeyError — normalize must not fail the fetch.
    _d, s = _normalized(make_collector, {"gcode_state": "RUNNING", "nozzle_temper": 200,
                                         "device": {"extruder": {"state": 0x71, "info": []}}}, model="H2S")
    assert s.state == PrinterState.PRINTING     # debug is only kept while printing
    assert (s.nozzle_temp, s.target_nozzle_temp) == (None, None)
    assert s.debug["nozzle_info"] == {"diameter": None, "type": None}


_WIKI_PAGE = "https://wiki.bambulab.com/en/x1/troubleshooting/hmscode/"


def test_hms_codes_without_table_text_shown_when_fatal_or_serious(make_collector):
    # 90 codes have an empty text in Bambu's tables (ru and en), 11 of them
    # fatal and 67 serious, and the lib drops them from hms.errors. Fatal and
    # serious ones stay visible as "unknown" with the code's wiki page, as with
    # 1.0.1, in the printer's order; common / info ones stay hidden.
    _d, s = _normalized(make_collector, {"gcode_state": "RUNNING", "hms": [
        {"attr": 0x05000600, "code": 0x00020070},   # serious, no text (a P2S reports it permanently)
        {"attr": 0x05000100, "code": 0x00030007},   # common, with text
        {"attr": 0x05000200, "code": 0x00030009},   # common, no text: hidden
        {"attr": 0x0C000100, "code": 0x00010018},   # fatal, no text
    ]})
    parts = s.last_error.split(" | ")
    assert len(parts) == 3
    assert parts[0] == f"HMS_0500_0600_0002_0070: unknown [serious] (см. {_WIKI_PAGE}0500_0600_0002_0070)"
    assert parts[1].startswith("HMS_0500_0100_0003_0007: ") and parts[1].endswith(" [common]")
    assert "unknown" not in parts[1]
    assert parts[2] == f"HMS_0C00_0100_0001_0018: unknown [fatal] (см. {_WIKI_PAGE}0C00_0100_0001_0018)"


def test_hms_common_code_without_table_text_alone_is_no_error(make_collector):
    _d, s = _normalized(make_collector, {"gcode_state": "RUNNING", "hms": [
        {"attr": 0x05000200, "code": 0x00030009}]})
    assert s.last_error is None


def test_hms_entries_the_raw_list_does_not_explain_are_kept():
    # hms.errors and the raw list are replaced one after the other by the MQTT
    # thread: a poll in between must not lose what the lib reported.
    result = _bambu_result(gcode_state="failed")
    result.hms = SimpleNamespace(
        errors={"Count": 1, "1-Code": "HMS_0300_0100_0001_0007", "1-Error": "текст", "1-Severity": "fatal"},
        _hms_list=[{"attr": 0x05000600, "code": 0x00020070}, {"bad": "entry"}])
    s = normalize_bambu("bambu-1", "P1", result)
    assert s.last_error.split(" | ") == [
        f"HMS_0500_0600_0002_0070: unknown [serious] (см. {_WIKI_PAGE}0500_0600_0002_0070)",
        "HMS_0300_0100_0001_0007: текст [fatal]",
    ]


def test_bambu_debug_fields_from_real_x1c_report(make_collector):
    import bambu_testkit as kit

    device, s = _normalized(make_collector, kit.load_fixture("X1C")[0]["msg"]["print"], model="X1C")
    assert s.state == PrinterState.PRINTING
    assert s.debug["hms_error_count"] == 1
    assert s.debug["nozzle_info"] == {"diameter": 0.4, "type": "hardened_steel"}   # HX01
    assert s.debug["home_flags"]["sdcard"] == device.home_flag.sdcard_status
    assert isinstance(s.debug["home_flags"]["door_open"], bool)


def test_bambu_debug_reads_vendored_attribute_names():
    result = _bambu_result(gcode_state="RUNNING")
    result.info = SimpleNamespace(wifi_signal=-50, sw_ver="01.02.03.00", new_version_state=0,
                                  active_nozzle_diameter=0.4, active_nozzle_type="hardened_steel",
                                  door_open=True)
    result.home_flag = SimpleNamespace(homed=True, sdcard_status="normal", is_220V=True)
    result.hms = SimpleNamespace(error_count=0, errors={"Count": 0})
    s = normalize_bambu("bambu-7", "X1C", result)
    assert s.debug["nozzle_info"] == {"diameter": 0.4, "type": "hardened_steel"}
    assert s.debug["home_flags"] == {"door_open": True, "homed": True, "sdcard": "normal", "is_220v": True}
    assert s.debug["hms_error_count"] == 0


# --- Phase-3 normalizer regressions ------------------------------------------

def test_bambu_ams_empty_slot_marked_by_type_string():
    # X1C reports an empty slot as type="Empty" while still carrying a leftover
    # colour. It must read as empty (no filament chip), not a real material.
    empty = SimpleNamespace(color="0C0C0CFF", type="Empty", name="", remain=0)
    real = SimpleNamespace(color="FF0000FF", type="PLA", name="Red", remain=80)
    unit = _unit([empty, real], humidity_index=1, humidity=20, temperature=30.0)
    ams = SimpleNamespace(data={0: unit}, active_ams_index=0, active_tray_index=1)
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
    # data[0] = tray-less unit (dropped), data[1] = the real unit. The survivor
    # must carry physical index 1, so its history never collides with a
    # different physical unit that later occupies filtered-list position 0.
    # tray_now counts slots of the units shown, like app.js and /api/ams/current.
    placeholder = _unit([])
    real_tray = SimpleNamespace(color="FF0000FF", type="PLA", name="Red", remain=50)
    real = _unit([real_tray], humidity_index=1, humidity=20, temperature=30.0)
    ams = SimpleNamespace(data={0: placeholder, 1: real}, active_ams_index=1, active_tray_index=0)
    s = normalize_bambu("bambu-x", "X1C", _bambu_result_with_ams(ams))
    assert len(s.ams["units"]) == 1
    assert s.ams["units"][0]["index"] == 1
    assert s.ams["tray_now"] == 0


def test_ws_new_print_start_with_stale_layers_not_finished():
    # At the very start of a new print firmware still echoes the previous job's
    # completed layer count and hasn't set an ETA yet. A reported (low) progress
    # must keep it PRINTING, not flip it to FINISHED on stale layers.
    data = {"state": "printing", "progress": 3, "current_layer": 250,
            "total_layers": 250, "remaining_time": 0}
    s = normalize_ws_dict("creality-1", "K1", PrinterKind.CREALITY, data)
    assert s.state == PrinterState.PRINTING


# --- H2S: активный подогрев камеры -------------------------------------------

def test_h2s_ctc_temp_is_packed_target_and_current(make_collector):
    """H2S пакует device.ctc.info.temp как (цель << 16) | текущая.

    Проверено live на H3: при нагреве камеры до 65° приходит 4259903 = (65, 63),
    после `M141 S0` — просто 63 (цель 0). Та же упаковка у device.bed.info.temp
    (4587590 = 70/70 при плоских bed_temper 70/70) и у airduct range 6553600 =
    (100, 0). Вендоренный pybambu распаковывает ctc сам."""
    _d, s = _normalized(make_collector, {"device": {"ctc": {"info": {"temp": 4259903}, "state": 2}}},
                        model="H2S")
    assert (s.chamber_temp, s.target_chamber_temp) == (63.0, 65.0)


def test_h2s_ctc_heater_off_keeps_plain_current_temp(make_collector):
    _d, s = _normalized(make_collector, {"device": {"ctc": {"info": {"temp": 38}, "state": 0}}},
                        model="H2S")
    assert (s.chamber_temp, s.target_chamber_temp) == (38.0, None)


def test_ctc_out_of_range_current_is_dropped(make_collector):
    device, s = _normalized(make_collector, {"device": {"ctc": {"info": {"temp": (65 << 16) | 4095}}}},
                            model="H2S")
    # The lib unpacks without a range check ...
    assert (device.temperature.chamber_temp, device.temperature.target_chamber_temp) == (4095, 65)
    # ... the normalizer drops the whole garbage reading.
    assert (s.chamber_temp, s.target_chamber_temp) == (None, None)


@pytest.mark.parametrize("chamber,target,expected", [
    (4095, 65, (None, None)),   # garbage current: the target is meaningless too
    (47, 250, (47.0, None)),    # garbage target only
    (0, 65, (None, None)),
    (63, 65, (63.0, 65.0)),
])
def test_bambu_chamber_range_guard(chamber, target, expected):
    temp = SimpleNamespace(chamber_temp=chamber, target_chamber_temp=target)
    result = SimpleNamespace(print_job=SimpleNamespace(gcode_state="RUNNING"), temperature=temp)
    s = normalize_bambu("p-h3", "H3", result, device_type="H2S")
    assert (s.chamber_temp, s.target_chamber_temp) == expected


def test_bambu_target_chamber_temp_surfaced():
    temp = SimpleNamespace(active_nozzle_temperature=63.0, bed_temp=100.0, chamber_temp=63.0,
                           active_nozzle_target_temperature=0.0, target_bed_temp=100.0,
                           target_chamber_temp=65)
    result = SimpleNamespace(print_job=SimpleNamespace(gcode_state="FAILED"),
                             temperature=temp)
    s = normalize_bambu("p-h3", "H3", result, device_type="H2S")
    assert s.nozzle_temp == 63.0
    assert s.target_nozzle_temp == 0.0
    assert s.chamber_temp == 63.0
    assert s.target_chamber_temp == 65.0


def test_bambu_chamber_target_zero_becomes_none():
    # У P2S/X1C активного подогрева нет — цель всегда 0, стрелку рисовать нечем.
    temp = SimpleNamespace(chamber_temp=47.0, target_chamber_temp=0)
    result = SimpleNamespace(print_job=SimpleNamespace(gcode_state="RUNNING"),
                             temperature=temp)
    s = normalize_bambu("bambu-9", "P4", result, device_type="P2S")
    assert s.chamber_temp == 47.0
    assert s.target_chamber_temp is None
