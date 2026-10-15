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
    from app.vendor.pybambu.utils import get_print_error_text

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
        # print_error with its text from Bambu's (Russian) tables
        "Ошибка печати 0300_400C: " + get_print_error_text("0300400C", "", "ru"),
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
        "remain_pct": -1, "empty": False, "nozzle_temp": None,   # fakes carry no profile range
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
    assert slots[0] == {"type": None, "color": None, "name": None, "remain_pct": -1, "empty": True,
                        "nozzle_temp": None}
    assert slots[1] == {"type": None, "color": "FF0000", "name": None, "remain_pct": -1, "empty": False,
                        "nozzle_temp": None}


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


# --- filament-change progress (device.extruder / ams_status) -------------------

def _ext(state, **entry):
    return {"device": {"extruder": {"state": state, "info": [dict({"id": 0}, **entry)]}}}


@pytest.mark.parametrize("msg,want", [
    # new protocol: state bit 19 = change running; star = (ams << 8) | slot of the target
    (dict(_ext(1 | 1 << 19, stat=3, snow=0xFFFF, star=2, info=8)),
     {"action": "load", "step": "cut", "ams": 0, "slot": 2}),
    (dict(_ext(1 | 1 << 19, stat=4, snow=0x0001, star=0x00FF, info=10)),
     {"action": "unload", "step": "pull", "ams": 0, "slot": None}),
    # ams_status (mirrored in stat) wins for the step; 0x0B = check, 9 = waiting (no step)
    (dict(_ext(1 | 1 << 19, stat=0x010B, star=0x0102), ams_status=0x010B),
     {"action": "load", "step": "check", "ams": 1, "slot": 2}),
    (dict(_ext(1 | 1 << 19, stat=0x0109, star=0x8000), ams_status=0x0109),
     {"action": "load", "step": None, "ams": 128, "slot": 0}),
    # loading into the extruder from the external spool: step 6 = confirm the extrusion
    (dict(_ext(1 | 1 << 19, stat=0x0106, star=0xFF00), ams_status=0x0106),
     {"action": "load", "step": "confirm", "ams": 255, "slot": 0}),
    (dict(_ext(1 | 1 << 19, stat=0x0106, star=0x0003), ams_status=0x0106),
     {"action": "load", "step": "push", "ams": 0, "slot": 3}),
    # target unknown (0xFFFF): the action stays open
    (dict(_ext(1 | 1 << 19, stat=2, star=0xFFFF)), {"action": None, "step": "heat", "ams": None, "slot": None}),
    # not busy, or a cold pull (ams_status main 7): no progress
    (dict(_ext(1, stat=3, star=2)), None),
    (dict(_ext(1 | 1 << 19, stat=0x0703, star=2), ams_status=0x0703), None),
])
def test_filament_change_new_protocol(make_collector, msg, want):
    _d, s = _normalized(make_collector, dict(msg, gcode_state="IDLE"), model="H2S")
    assert s.filament_change == want


@pytest.mark.parametrize("sub,step", [(2, "heat"), (3, None), (4, None), (5, "push"), (6, "confirm"),
                                      (7, "purge"), (8, None), (0x0B, None)])
def test_filament_change_from_the_external_spool_has_studios_steps(make_collector, sub, step):
    # Loading from the external spool Bambu Studio has only these steps:
    # nothing is cut or pulled back into an AMS.
    for star, ams in ((0xFF00, 255), (0xFE00, 254)):
        msg = dict(_ext(1 | 1 << 19, stat=0x0100 | sub, star=star), ams_status=0x0100 | sub)
        _d, s = _normalized(make_collector, dict(msg, gcode_state="IDLE"), model="H2S")
        assert s.filament_change == {"action": "load", "step": step, "ams": ams, "slot": 0}
    _d, s = _normalized(make_collector, {"gcode_state": "IDLE", "ams_status": 0x0100 | sub,
                                         "ams": {"tray_now": "255", "tray_tar": "254"}}, model="P1S")
    assert s.filament_change == {"action": "load", "step": step, "ams": 255, "slot": 0}


@pytest.mark.parametrize("ams_status,tray_tar,want", [
    (0x0102, "255", {"action": "unload", "step": "heat", "ams": None, "slot": None}),
    (0x0105, "6", {"action": "load", "step": "push", "ams": 1, "slot": 2}),
    (0x0106, "254", {"action": "load", "step": "confirm", "ams": 255, "slot": 0}),
    (0x0107, "128", {"action": "load", "step": "purge", "ams": 128, "slot": 0}),
    (0x0108, "0", {"action": "load", "step": "check", "ams": 0, "slot": 0}),
    (0x010C, "0", {"action": "load", "step": None, "ams": 0, "slot": 0}),
    (768, "0", None),           # printing from the AMS (assist), no change
    (0x0700, "255", None),      # cold pull
])
def test_filament_change_legacy_p1(make_collector, ams_status, tray_tar, want):
    _d, s = _normalized(make_collector, {"gcode_state": "IDLE", "ams_status": ams_status,
                                         "ams": {"tray_now": "255", "tray_tar": tray_tar}}, model="P1S")
    assert s.filament_change == want


def test_filament_change_none_without_raw_report():
    # SimpleNamespace devices (no RawReport) degrade to None.
    s = normalize_bambu("bambu-1", "P1", _bambu_result(gcode_state="IDLE"))
    assert s.filament_change is None


# --- AMS load / unload inputs: slot nozzle range, firmware gate, extruder ------

def _get_version(device, sw="01.02.00.00"):
    device.info_update(data={"command": "get_version", "sequence_id": "0", "module": [
        {"name": "ota", "sw_ver": sw, "hw_ver": "OTA", "sn": "X"}]})


def _tray_msg(i, ttype, lo, hi):
    return {"id": str(i), "tray_type": ttype, "tray_color": "FF0000FF", "tray_info_idx": "GFG99",
            "nozzle_temp_min": lo, "nozzle_temp_max": hi, "remain": 80, "state": 11}


def test_slot_nozzle_temp_range(make_collector):
    _d, s = _normalized(make_collector, {"ams": {"ams": [{"id": "0", "humidity": "3", "temp": "25.0", "tray": [
        _tray_msg(0, "PETG", "230", "270"),
        {"id": "1", "state": 0},
        _tray_msg(2, "PLA", "0", "0"),
        _tray_msg(3, "PLA", "x", "220"),
    ]}]}})
    assert [sl["nozzle_temp"] for sl in s.ams["units"][0]["slots"]] == [[230, 270], None, None, None]


def test_filament_swap_ok_needs_the_firmware_version(make_collector, caplog):
    import logging

    caplog.set_level(logging.ERROR)
    device, s = _normalized(make_collector, {"gcode_state": "IDLE"}, model="P2S")
    assert s.filament_swap_ok is None
    _get_version(device)
    assert normalize_bambu("t-1", "t", device, device_type="P2S").filament_swap_ok is True
    # X1C firmware below 01.05.06.01 has no ams_change_filament
    device, _s = _normalized(make_collector, {"gcode_state": "IDLE"}, model="X1C")
    _get_version(device, "01.05.00.00")
    assert normalize_bambu("t-1", "t", device, device_type="X1C").filament_swap_ok is False
    # the lib logs an ERROR when a feature is queried before the version is known
    assert "supports_feature queried" not in caplog.text


@pytest.mark.parametrize("ext_info,snow,want", [
    (0b1010, 0x0000, True),     # info bit 1: filament in the extruder
    (0b1000, 0x0000, False),
    (0b1010, 0xFF00, True),     # from the external spool
    (0b1000, 0xFFFF, False),
])
def test_filament_loaded_new_protocol(make_collector, ext_info, snow, want):
    _d, s = _normalized(make_collector, {"gcode_state": "IDLE", "device": {"extruder": {"state": 1, "info": [
        {"id": 0, "snow": snow, "info": ext_info}]}}}, model="H2S")
    assert s.filament_loaded is want


@pytest.mark.parametrize("tray_now,want", [("254", True), ("255", False), ("1", None)])
def test_filament_loaded_legacy_from_tray_now(make_collector, tray_now, want):
    # P1 without an extruder block: the legacy tray_now (255 = nothing loaded).
    # "1" points into an AMS that is not reported: unknown.
    _d, s = _normalized(make_collector, {"gcode_state": "IDLE", "ams": {"tray_now": tray_now}}, model="P1S")
    assert s.filament_loaded is want


def test_filament_loaded_unknown_without_raw_report():
    assert normalize_bambu("bambu-1", "P1", _bambu_result(gcode_state="IDLE")).filament_loaded is None


@pytest.mark.parametrize("msg,model,with_version,want", [
    ({"device": {"extruder": {"state": 1, "info": [{"id": 0}]}}}, "H2S", True, False),
    ({"device": {"extruder": {"state": 2, "info": [{"id": 0}, {"id": 1}]}}}, "H2S", False, True),   # 2 extruders
    ({"ams": {"ams": [{"id": "0", "humidity": "3", "temp": "25", "info": "10000E03", "tray": []}]}},
     "P2S", False, True),                                                                         # track switch
    ({"gcode_state": "IDLE"}, "H2D", True, True),                                                 # lib: dual nozzle model
    ({"gcode_state": "IDLE"}, "P1S", False, False),
])
def test_dual_nozzle(make_collector, msg, model, with_version, want):
    device, _s = _normalized(make_collector, msg, model=model)
    if with_version:
        _get_version(device)
    assert normalize_bambu("t-1", "t", device, device_type=model).dual_nozzle is want


# --- AMS drying: unit `dry` block, printer ams_dry_ok -------------------------

def _drying_report(fun2="3973", gcode="IDLE", snow=0xFFFF, **unit):
    u = {"id": "0", "humidity": "3", "humidity_raw": "20", "temp": "48.0", "info": "10942023",
         "dry_time": 633, "dry_sf_reason": [6],
         "dry_setting": {"dry_temperature": 55, "dry_duration": 12, "dry_filament": "PETG"},
         "tray": [_tray_msg(0, "PETG", "230", "270"), _tray_msg(1, "PETG", "230", "270"),
                  {"id": "2", "state": 0}, {"id": "3", "state": 0}]}
    u.update(unit)
    msg = {"gcode_state": gcode, "ams": {"ams": [u]},
           "device": {"extruder": {"state": 1, "info": [{"id": 0, "snow": snow, "info": 10}]}}}
    if fun2 is not None:
        msg["fun2"] = fun2
    return msg


def test_drying_unit_from_a_live_shaped_report(make_collector):
    _d, s = _normalized(make_collector, _drying_report())
    dry = s.ams["units"][0]["dry"]
    assert {k: dry[k] for k in ("type", "status", "phase", "active", "remaining_min", "temp", "hours",
                                "filament", "blockers", "feeding")} == {
        "type": "n3f", "status": "drying", "phase": "dehumidify", "active": True, "remaining_min": 633,
        "temp": 55, "hours": 12, "filament": "PETG", "blockers": [], "feeding": False}
    assert (dry["max_temp"], dry["max_by"]) == (65, None)
    assert s.ams_dry_ok is True
    assert s.ams["units"][0]["dry_time"] == 633          # kept for the AMS history and older clients


def test_drying_status_needs_fun2(make_collector):
    _d, s = _normalized(make_collector, _drying_report(fun2=None))
    dry = s.ams["units"][0]["dry"]
    assert (dry["status"], dry["active"], dry["remaining_min"]) == (None, True, 633)
    assert s.ams_dry_ok is None
    _d, s = _normalized(make_collector, _drying_report(fun2="1"))
    assert (s.ams_dry_ok, s.ams["units"][0]["dry"]["status"]) == (False, None)


@pytest.mark.parametrize("gcode,state", [("RUNNING", PrinterState.PRINTING), ("PAUSE", PrinterState.PAUSED)])
def test_drying_unit_feeding_a_print_gets_the_print_caps(make_collector, gcode, state):
    # A paused print still has its filament in this AMS: the print caps stay.
    _d, s = _normalized(make_collector, _drying_report(gcode=gcode, snow=0x0001, info="10142003",
                                                       dry_time=0, dry_sf_reason=[]))
    assert s.state == state
    dry = s.ams["units"][0]["dry"]
    assert (dry["status"], dry["active"], dry["feeding"], dry["max_temp"], dry["max_by"]) == (
        "off", False, True, 55, "PETG")
    assert dry["suggest"] == {"filament": "PETG", "temp": 55, "hours": 12}


@pytest.mark.parametrize("gcode", ["IDLE", "FINISH"])
def test_drying_unit_loaded_but_not_printing_gets_the_idle_caps(make_collector, gcode):
    # The extruder still points at this AMS (snow), but no print runs.
    _d, s = _normalized(make_collector, _drying_report(gcode=gcode, snow=0x0001, info="10142003",
                                                       dry_time=0, dry_sf_reason=[]))
    dry = s.ams["units"][0]["dry"]
    assert (dry["feeding"], dry["max_temp"], dry["max_by"]) == (False, 65, None)
    assert dry["suggest"] == {"filament": "PETG", "temp": 65, "hours": 12}


def test_classic_ams_has_no_drying(make_collector):
    _d, s = _normalized(make_collector, _drying_report(info="10002001"))
    assert s.ams["units"][0]["dry"] is None


# --- print_error: text from Bambu's tables ---------------------------------------

def test_print_error_line_texts():
    from app.services.bambu_extras import print_error_line
    from app.vendor.pybambu.utils import get_print_error_text

    cancelled = get_print_error_text("0300400C", "", "ru")
    assert CYRILLIC_RE.search(cancelled)
    assert print_error_line(50348044, "") == "Ошибка печати 0300_400C: " + cancelled
    # codes >= 0x10000000: the lib's own formatting breaks on them (8 hex digits here)
    line = print_error_line(0x12008001, "P2S")
    assert line.startswith("Ошибка печати 1200_8001: ") and CYRILLIC_RE.search(line)
    # empty table text (ru and en) and a code missing from the tables: code only
    assert print_error_line(0x05008030, "") == "Ошибка печати 0500_8030"
    assert print_error_line(0x0FFF0001, "X1C") == "Ошибка печати 0FFF_0001"


def test_print_error_shown_where_the_lib_has_no_text(make_collector):
    device, s = _normalized(make_collector, {"gcode_state": "FAILED", "print_error": 0x12008001})
    assert device.print_error.error is None     # the lib formats it as "0120_0800": no text
    assert s.last_error.startswith("Ошибка печати 1200_8001: ")


def test_print_error_uses_the_detected_model(make_collector, monkeypatch):
    from app.services import bambu_extras

    seen = []
    monkeypatch.setattr(bambu_extras, "get_print_error_text",
                        lambda code, dt, lang: seen.append((code, str(dt), lang)) or "текст")
    bambu_extras.print_error_line.cache_clear()
    _d, s = _normalized(make_collector, {"gcode_state": "FAILED", "print_error": 0x07008002}, model="H2S")
    assert s.last_error == "Ошибка печати 0700_8002: текст"
    assert seen == [("07008002", "H2S", "ru")]
    bambu_extras.print_error_line.cache_clear()


CYRILLIC_RE = __import__("re").compile("[А-Яа-яЁё]")


# --- indicators: door, SD card, nozzle ---------------------------------------------

_FW = {"X1C": "01.11.02.00", "P2S": "01.02.00.00", "H2S": "01.02.00.00", "P1S": "01.09.00.00"}


@pytest.mark.parametrize("model,door,sdcard,nozzle", [
    ("X1C", False, "normal", {"diameter": 0.4, "type": "hardened_steel"}),     # HX01
    ("P2S", False, None, {"diameter": 0.4, "type": "hardened_steel"}),         # eMMC: no card needed
    ("H2S", False, None, {"diameter": 0.4, "type": "hardened_steel"}),
    ("P1S", None, "normal", {"diameter": 0.4, "type": "stainless_steel"}),     # P1: no door sensor
])
def test_indicators_from_the_fixtures(offline_collector, model, door, sdcard, nozzle):
    import bambu_testkit as kit

    collector = offline_collector(model)
    for line in kit.load_fixture(model):
        kit.deliver(collector, line["msg"])
    s = normalize_bambu("t-1", "t", collector.fetch(), device_type=model)
    assert s.door_open is None                  # the door sensor needs the firmware version
    assert (s.sdcard, s.nozzle) == (sdcard, nozzle)
    _get_version(collector.client.get_device(), _FW[model])
    s = normalize_bambu("t-1", "t", collector.fetch(), device_type=model)
    assert (s.door_open, s.sdcard, s.nozzle) == (door, sdcard, nozzle)


@pytest.mark.parametrize("model,msg,want", [
    ("P2S", {"stat": "1001040A18030"}, True),          # stat bit 23
    ("P2S", {"stat": "1001040218030"}, False),
    ("H2S", {"stat": "46A58008"}, True),
    ("X1C", {"home_flag": 0x00800000, "stat": "0"}, True),   # X1: home_flag bit 23
    ("X1C", {"home_flag": 0, "stat": "800000"}, False),      # ... and not stat
])
def test_door_open(make_collector, model, msg, want):
    device, _s = _normalized(make_collector, dict(msg, gcode_state="IDLE"), model=model)
    _get_version(device, _FW[model])
    assert normalize_bambu("t-1", "t", device, device_type=model).door_open is want


@pytest.mark.parametrize("msg,want", [
    ({"home_flag": 0x100}, "normal"),
    ({"home_flag": 0}, "missing"),
    ({"home_flag": 0x200}, "abnormal"),
    ({"home_flag": 0x300}, "abnormal"),
    ({"home_flag": 0, "fun2": "3973"}, None),          # prints from eMMC (fun2 bit 0): a missing card is fine
    ({"home_flag": 0x200, "fun2": "3973"}, "abnormal"),  # ... but a faulty one still shows
    ({"home_flag": 0, "fun2": "1"}, None),             # bit 0 alone (real fun2 values set bits 0 and 1)
    ({"home_flag": 0, "fun2": "2"}, "missing"),        # bit 1 is not the eMMC
    ({"gcode_state": "IDLE"}, None),                   # no home_flag reported yet
])
def test_sdcard_status(make_collector, msg, want):
    _d, s = _normalized(make_collector, msg, model="P1S")
    assert s.sdcard == want


def test_nozzle_unknown_degrades_to_none(make_collector):
    _d, s = _normalized(make_collector, {"gcode_state": "IDLE"}, model="P1S")
    assert s.nozzle is None
    assert normalize_bambu("bambu-1", "P1", _bambu_result(gcode_state="IDLE")).nozzle is None
    _d, s = _normalized(make_collector, {"device": {"nozzle": {"info": [{"id": 0, "diameter": 0.6, "type": "HH01"}]}}},
                        model="H2S")
    assert s.nozzle == {"diameter": 0.6, "type": "high_flow_hardened_steel"}


# --- passive command-signature detection: print.fun bit 29 -------------------------

@pytest.mark.parametrize("model,want", [("X1C", False), ("P2S", False), ("H2S", False), ("P1S", None)])
def test_signature_required_from_the_fixtures(offline_collector, model, want):
    import bambu_testkit as kit

    collector = offline_collector(model)
    for line in kit.load_fixture(model):
        kit.deliver(collector, line["msg"])
    assert normalize_bambu("t-1", "t", collector.fetch(), device_type=model).signature_required is want


@pytest.mark.parametrize("fun,want", [("3EC1AFFF9CFF", True), ("3EC18FFF9CFF", False), (None, None)])
def test_signature_required_bit(make_collector, fun, want):
    # ha-bambulab's examples: Developer Mode off (bit 29 set) / on; no fun: unknown
    msg = {"gcode_state": "IDLE"} if fun is None else {"gcode_state": "IDLE", "fun": fun}
    _d, s = _normalized(make_collector, msg)
    assert s.signature_required is want


# --- lights: both H2 lamps, WS light state, Klipper control fields --------------

@pytest.mark.parametrize("l1,l2,want", [
    ("on", "unknown", True),
    ("off", "on", True),        # H2/X2: either lamp lit = lit (the lib's is_chamber_light_on)
    ("on", "off", True),
    ("off", "off", False),
    ("off", "unknown", False),  # one-lamp models keep chamber_light2 "unknown"
    ("off", None, False),
    ("unknown", "unknown", None),
    ("unknown", "off", None),
    (None, None, None),
])
def test_bambu_light_from_both_lamps(l1, l2, want):
    lights = SimpleNamespace(chamber_light=l1, chamber_light2=l2, work_light="flashing")
    result = SimpleNamespace(print_job=SimpleNamespace(gcode_state="RUNNING"), lights=lights)
    s = normalize_bambu("p-h1", "H1", result, device_type="H2S")
    assert s.light_on is want
    assert s.debug["lights"] == {"chamber_light": l1, "chamber_light2": l2, "work_light": "flashing"}


@pytest.mark.parametrize("value,want", [(True, True), (False, False), (1, None), ("on", None), (None, None)])
def test_ws_light_on_passthrough_bool_only(value, want):
    for kind in (PrinterKind.CREALITY, PrinterKind.KLIPPER):
        s = normalize_ws_dict("x-1", "X", kind, {"state": "standby", "light_on": value})
        assert s.light_on is want


def _moonraker(state="standby", webhooks="ready", **extra):
    data = {"state": state, "progress": 0, "remaining_time": 0, "debug": {"webhooks_state": webhooks}}
    data.update(extra)
    return data


@pytest.mark.parametrize("data,state", [
    (_moonraker("standby"), PrinterState.IDLE),
    (_moonraker("paused", progress=40, remaining_time=600), PrinterState.PAUSED),
    (_moonraker("printing", progress=40, remaining_time=600), PrinterState.PRINTING),
])
@pytest.mark.parametrize("pin,light_ctl", [("caselight", True), ("", False), (None, None)])
def test_klipper_control_fields_in_every_state(data, state, pin, light_ctl):
    # Not read from `debug`: the status carries debug only while printing, and
    # an idle or paused Klipper must stay controllable.
    data = dict(data)
    if pin is not None:
        data["light_pin"] = pin
    s = normalize_ws_dict("klipper-1", "Ghost 6", PrinterKind.KLIPPER, data)
    assert s.state == state
    assert (s.ctl_ready, s.light_ctl) == (True, light_ctl)
    if state != PrinterState.PRINTING:
        assert s.debug == {}


@pytest.mark.parametrize("webhooks,want", [("shutdown", False), ("error", False), ("startup", False),
                                           ("Ready", True), (None, None)])
def test_klipper_ctl_ready_from_the_host_state(webhooks, want):
    data = _moonraker()
    if webhooks is None:
        del data["debug"]
    else:
        data["debug"]["webhooks_state"] = webhooks
    assert normalize_ws_dict("klipper-1", "Ghost 6", PrinterKind.KLIPPER, data).ctl_ready is want


def test_control_fields_none_for_creality_and_offline():
    s = normalize_ws_dict("creality-1", "K1", PrinterKind.CREALITY,
                          dict(_moonraker(), light_pin="caselight"))
    assert (s.ctl_ready, s.light_ctl) == (None, None)
    s = normalize_ws_dict("klipper-1", "Ghost 6", PrinterKind.KLIPPER, {})
    assert (s.online, s.ctl_ready, s.light_ctl, s.light_on) == (False, None, None, None)


def test_grace_status_keeps_the_klipper_control_fields():
    import main

    s = normalize_ws_dict("klipper-1", "Ghost 6", PrinterKind.KLIPPER,
                          _moonraker(light_pin="caselight", light_on=True))
    grace = main._build_grace_status(s, reason="timeout", now=1.0, device_type="generic")
    assert (grace.ctl_ready, grace.light_ctl, grace.light_on) == (True, True, True)
    assert grace.to_dict()["ctl_ready"] is True


# --- no chamber reading without a chamber sensor (P1P / P1S / A1) ------------------

@pytest.mark.parametrize("model,chamber", [("P1S", None), ("P1P", None), ("A1", None), ("A1MINI", None),
                                           ("X1C", 35.0), ("P2S", 35.0), ("H2S", 35.0)])
@pytest.mark.parametrize("known_fw", [False, True])
def test_chamber_only_on_models_with_a_sensor(make_collector, model, chamber, known_fw):
    # The P1S reports a constant bogus 5° (no sensor): the lib's
    # Features.CHAMBER_TEMPERATURE once the firmware is known, the same model
    # family test before that.
    device = make_collector("t", "192.0.2.10", "00000000", "22E00A000000000", model).client.get_device()
    if known_fw:
        _get_version(device, "01.09.00.00")
    device.print_update(data={"gcode_state": "RUNNING", "chamber_temper": 35})
    s = normalize_bambu("t-1", "t", device, device_type=model)
    assert (s.chamber_temp, s.target_chamber_temp) == (chamber, None)


def test_chamber_sensor_fallback_without_any_model():
    temp = SimpleNamespace(chamber_temp=30.0, target_chamber_temp=0)
    result = SimpleNamespace(print_job=SimpleNamespace(gcode_state="IDLE"), temperature=temp)
    assert normalize_bambu("b-1", "B", result, device_type="P1S").chamber_temp is None
    assert normalize_bambu("b-1", "B", result, device_type="X1C").chamber_temp == 30.0
    # the live device's own model wins over the registry's
    result.info = SimpleNamespace(device_type="P1S")
    assert normalize_bambu("b-1", "B", result, device_type="X1C").chamber_temp is None
