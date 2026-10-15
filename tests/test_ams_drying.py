"""AMS drying policy (app/services/ams_drying.py): pure functions, literal numbers.

Material values: Bambu Studio's BBL filament profiles (drying temperature for
AMS 2 Pro "n3f" / AMS HT "n3s" when idle and while that AMS feeds a print,
hours, heat-distortion and softening temperatures). AMS info bit layout:
type bits 0-3, dry status 4-7, dry sub status 22-23.
"""
import pytest

from app.services import ams_drying as d


@pytest.mark.parametrize("tray_type,want", [
    ("PLA", "PLA"), ("PLA-CF", "PLA"), ("pla basic", "PLA"),
    ("PETG", "PETG"), ("PETG HF", "PETG"), ("PCTG", "PETG"), ("PET-CF", "PETG"),
    ("PA-CF", "PA"), ("PA6-GF", "PA"), ("PAHT-CF", "PA"), ("PPA-CF", "PA"),
    ("PC", "PC"), ("PC-ABS", "PC"), ("ABS", "ABS"), ("ABS-GF", "ABS"), ("ASA", "ASA"),
    ("TPU 95A", "TPU"), ("TPE", "TPU"), ("PVA", "PVA"), ("PPS", "PPS"), ("PPS-CF", "PPS"),
    ("HIPS", None), ("PE-CF", None), ("", None), ("?", None), ("Empty", None), (None, None),
])
def test_material_of(tray_type, want):
    assert d.material_of(tray_type) == want


@pytest.mark.parametrize("info,want", [
    ("10942023", {"type": "n3f", "status": "drying", "phase": "dehumidify"}),
    ("10402013", {"type": "n3f", "status": "checking", "phase": "heating"}),
    ("10142003", {"type": "n3f", "status": "off", "phase": None}),
    (0x10002004, {"type": "n3s", "status": "off", "phase": None}),
    ("10002033", {"type": "n3f", "status": "cooling", "phase": None}),
    ("10002063", {"type": "n3f", "status": "overheat", "phase": None}),
    ("100020F3", {"type": "n3f", "status": None, "phase": None}),     # unknown status code
    ("10002001", {"type": None, "status": "off", "phase": None}),     # classic AMS: cannot dry
])
def test_decode_info(info, want):
    got = d.decode_info(info)
    assert {k: got[k] for k in want} == want


@pytest.mark.parametrize("junk", [None, "", "zz", [], 1.5, True])
def test_decode_info_garbage(junk):
    assert d.decode_info(junk) is None


def _slot(ttype, empty=False):
    return {"type": None if empty else ttype, "empty": empty}


def _dry(slots=(), *, type_info="10002003", fun2=0x3973, sf=(), feeding=False, dry_time=None,
         setting=(0, 0, ""), index=0, model="Unknown"):
    unit = {"index": index, "dry_time": dry_time, "slots": [_slot(t, t is None) for t in slots]}
    raw = None if type_info is None else {"info": int(type_info, 16), "sf": tuple(sf)}
    return d.build_dry_info(unit, model, raw, fun2, feeding, setting)


def test_live_drying_unit():
    dry = _dry(("PETG", "PETG"), type_info="10942023", sf=(6,), dry_time=633, setting=(55, 12, "PETG"))
    assert {k: dry[k] for k in ("type", "status", "phase", "active", "remaining_min", "temp", "hours",
                                "filament", "blockers", "feeding")} == {
        "type": "n3f", "status": "drying", "phase": "dehumidify", "active": True, "remaining_min": 633,
        "temp": 55, "hours": 12, "filament": "PETG", "blockers": [], "feeding": False}


def test_stopped_unit_and_cooling_is_not_active():
    off = _dry(("PLA",), type_info="10142003")
    assert (off["status"], off["active"], off["remaining_min"], off["temp"], off["filament"]) == (
        "off", False, None, None, None)
    # Bambu Studio treats cooling as idle: a new drying may start
    assert _dry(("PLA",), type_info="10002033")["active"] is False
    for code, status in ((1, "checking"), (2, "drying"), (5, "error"), (6, "overheat")):
        assert _dry(("PLA",), type_info="1000200%d3" % code)["active"] is True, status


def test_without_fun2_status_is_unknown_and_activity_comes_from_dry_time():
    # Studio reads the dry status (and the reasons) only when fun2 bit 5 says
    # remote drying is supported.
    dry = _dry(("PLA",), type_info="10942023", fun2=None, sf=(3,), dry_time=90)
    assert (dry["status"], dry["phase"], dry["active"], dry["remaining_min"], dry["blockers"]) == (
        None, None, True, 90, [])
    assert _dry(("PLA",), type_info="10942023", fun2=0x1, dry_time=None)["active"] is False


def test_remote_drying_is_fun2_bit_5_alone():
    # The fixtures' fun2 (P2S 0x3973, H2S 0x1177) have bits 4 and 5 both set:
    # only these single-bit values tell bit 5 from its neighbours.
    assert [d.remote_dry_supported(v) for v in (0x20, 0x10, 0x40, 0x0, None)] == [True, False, False, False, None]
    on = _dry(("PLA",), type_info="10942023", fun2=0x20, sf=(3,), dry_time=90)
    assert (on["status"], on["phase"], [b["code"] for b in on["blockers"]]) == ("drying", "dehumidify", [3])
    off = _dry(("PLA",), type_info="10942023", fun2=0x10, sf=(3,), dry_time=90)
    assert (off["status"], off["phase"], off["active"], off["blockers"]) == (None, None, True, [])


@pytest.mark.parametrize("type_info,model,index,want", [
    ("10002003", "Unknown", 0, "n3f"),
    ("10002004", "Unknown", 128, "n3s"),
    (None, "AMS 2 Pro", 0, "n3f"),          # no info bit field: the lib's model (get_version)
    (None, "AMS HT", 128, "n3s"),
    (None, "Unknown", 128, "n3s"),          # ids 128+ are always an AMS HT
    (None, "Unknown", 0, None),             # unknown 4-slot unit: cannot tell
    ("10002001", "AMS", 0, None),           # classic AMS
    ("10002002", "AMS Lite", 0, None),      # AMS Lite
])
def test_unit_type(type_info, model, index, want):
    dry = _dry(("PLA",), type_info=type_info, model=model, index=index)
    assert (dry["type"] if dry else None) == want


def test_blockers_drop_6_dedupe_and_follow_studio_priority():
    dry = _dry(("PLA",), sf=(6, 1, 0, 3, 2, 3, 99, 10))
    assert [b["code"] for b in dry["blockers"]] == [2, 3, 10, 1, 0, 99]
    assert dry["blockers"][0]["text"] == d.BLOCKER_TEXT[2]
    assert dry["blockers"][-1]["text"] == d.BLOCKER_UNKNOWN
    assert _dry(("PLA",), sf=(6,))["blockers"] == []


# dry_sf_reason codes as Bambu Studio names them (CannotDryReason); 6 = drying
# in progress, 9 has no name there.
@pytest.mark.parametrize("code,phrase", [
    (0, "задачей"),                        # TaskOccupied
    (1, "сушится слишком много"),          # InsufficientPower
    (2, "AMS занят"),                      # AmsBusy
    (3, "при нагреве"),                    # ConsumableAtAmsOutlet
    (4, "запускает сушку"),                # InitiatingAmsDrying
    (5, "2D"),                             # NotSupportedIn2dMode
    (7, "обновление прошивки"),            # Upgrading
    (8, "подключите блок питания AMS"),    # InsufficientPowerNeedPluginPower
    (10, "вручную"),                       # FilamentAtAmsOutletManualUnload
])
def test_blocker_text_names_its_own_reason(code, phrase):
    [blocker] = _dry(("PLA",), sf=(code,))["blockers"]
    assert blocker["code"] == code and phrase in blocker["text"]
    # ... and no other reason's text has it
    assert [c for c, text in d.BLOCKER_TEXT.items() if phrase in text] == [code]


def test_blocker_code_without_a_reason_gets_the_generic_text():
    # 9: seen live for a few seconds right after a stop
    assert _dry(("PLA",), sf=(9,))["blockers"] == [{"code": 9, "text": d.BLOCKER_UNKNOWN}]
    assert d.BLOCKER_UNKNOWN not in d.BLOCKER_TEXT.values()


@pytest.mark.parametrize("slots,type_info,feeding,max_temp,max_by,suggest", [
    # fixture anchors, printing from that AMS (print caps)
    (("PETG", "PETG", None, None), "10002003", True, 55, "PETG", ("PETG", 55, 12)),
    (("PA-CF", "PETG", None, None), "10001003", True, 55, "PETG", ("PETG", 55, 12)),
    # idle
    (("PETG",), "10002003", False, 65, None, ("PETG", 65, 12)),
    (("PLA", "PETG"), "10002003", False, 45, "PLA", ("PLA", 45, 12)),
    (("ABS",), "10002004", False, 85, None, ("ABS", 80, 8)),
    (("HIPS",), "10002003", False, 45, "HIPS", ("PLA", 45, 12)),        # unknown material: PLA caps
    (("?",), "10002003", False, 45, "?", ("PLA", 45, 12)),              # spool without a material
    (("PPS-CF",), "10002003", False, 65, None, ("PPS", 65, 12)),
    ((), "10002003", False, 65, None, ("PLA", 45, 12)),                 # empty unit
    ((None, None, None, None), "10002004", False, 85, None, ("PLA", 45, 12)),
    # an AMS HT feeding a print with PA: the print cap is the type max
    (("PA",), "10002004", True, 85, None, ("PA", 85, 12)),
    (("TPU",), "10002004", False, 45, "TPU", ("TPU", 45, 18)),
])
def test_limits_and_suggestion(slots, type_info, feeding, max_temp, max_by, suggest):
    dry = _dry(slots, type_info=type_info, feeding=feeding)
    assert (dry["max_temp"], dry["max_by"], dry["feeding"]) == (max_temp, max_by, feeding)
    assert dry["suggest"] == dict(zip(("filament", "temp", "hours"), suggest))


def test_presets_cover_every_material_capped_at_max_temp():
    dry = _dry(("PETG",), type_info="10002004")
    assert [p["filament"] for p in dry["presets"]] == list(d.MATERIALS)
    assert dry["presets"][0] == {"filament": "PLA", "temp": 45, "hours": 12}
    assert {p["filament"]: (p["temp"], p["hours"]) for p in dry["presets"]}["PVA"] == (75, 18)   # 85 capped
    feeding = _dry(("PETG",), type_info="10002003", feeding=True)
    assert all(p["temp"] <= 55 for p in feeding["presets"])
    assert {p["filament"]: p["temp"] for p in feeding["presets"]}["TPU"] == 45    # TPU print temp


def test_canonical_filament():
    assert d.canonical_filament("petg") == "PETG"
    assert d.canonical_filament(" PLA ") == "PLA"
    assert d.canonical_filament("PETG HF") is None
    assert d.canonical_filament(None) is None
    assert d.canonical_filament(5) is None


def test_limits_are_studios_ranges():
    assert (d.TEMP_MIN, d.TYPE_MAX, d.HOURS_MIN, d.HOURS_MAX) == (45, {"n3f": 65, "n3s": 85}, 1, 24)
