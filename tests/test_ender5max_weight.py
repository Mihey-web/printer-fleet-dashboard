"""The Ender-5 Max's planned grams from the filament length in its file listing (live 17.11)."""
import pytest

from app.services import print_material as pm

GCODES = "/usr/data/printer_data/gcodes"
LISTING = {"totalNum": 3, "fileInfo":
           GCODES + ":Tube.STL_PETG_10h50m6s.gcode:73760053:0.28:1794635800:117036:/tmp/creality/local_gcode/humbnail/Tube.png;"
           + GCODES + ":Hook_PLA_1h2m.gcode:1000:0.2:1794700000:10000:/tmp/x/Hook.png;"
           + GCODES + ":Clip: v2.gcode:1000:0.2:1794700001:10000:/tmp/x/Clip.png;"}


@pytest.mark.parametrize("length, weight", [(28186, 86.10), (24045, 73.45), (26679, 81.50), (61568, 188.08)])
def test_length_to_grams_matches_creality_prints_own_weight(length, weight):
    # the K1C's listing has both, filamentWeight and consumables (live 17.11, PETG)
    assert pm.length_grams(length, "Part_PETG_2h.gcode") == pytest.approx(weight, abs=0.06)


def test_density_from_the_material_in_the_name():
    assert pm.length_grams(10000, "a_PLA_1h.gcode") == 29.8
    assert pm.length_grams(10000, "a_pla+_1h.gcode") == 29.8
    assert pm.length_grams(10000, "a_ABS_1h.gcode") == 25.0
    assert pm.length_grams(10000, "a_PETG-CF_1h.gcode") == pm.length_grams(10000, "a_PETG_1h.gcode") == 30.5
    assert pm.length_grams(10000, "no material.gcode") == 30.5        # PETG: most of the farm's files


@pytest.mark.parametrize("value", [None, "", "0", "-5", "x", "nan", "inf"])
def test_unknown_length_is_not_zero(value):
    assert pm.length_grams(value, "a_PETG_1h.gcode") is None


def test_running_file_from_the_v1_listing():
    report = {"printFileName": GCODES + "/Tube.STL_PETG_10h50m6s.gcode", "retGcodeFileInfo": LISTING}
    assert pm.creality_grams(report) == 357.5          # 117 m of PETG
    report["printFileName"] = GCODES + "/Hook_PLA_1h2m.gcode"
    assert pm.creality_grams(report) == 29.8
    report["printFileName"] = GCODES + "/Clip: v2.gcode"     # a colon in the name
    assert pm.creality_grams(report) == 30.5
    report["printFileName"] = GCODES + "/Other.gcode"
    assert pm.creality_grams(report) is None
    assert pm.creality_grams({"printFileName": GCODES + "/Tube.gcode", "retGcodeFileInfo": {"fileInfo": ""}}) is None
    assert pm.creality_grams({"printFileName": GCODES + "/Tube.gcode", "retGcodeFileInfo": "broken"}) is None


def test_ender5max_client_keeps_the_weight_until_the_next_file():
    from app.clients.creality_client import CrealityK1Client
    client = CrealityK1Client("example.invalid", "test")
    base = {"printFileName": GCODES + "/Tube.STL_PETG_10h50m6s.gcode", "state": 1}
    client._parse_report(dict(base, retGcodeFileInfo=LISTING))
    client._parse_report(dict(base, printProgress=25))
    assert client.data["planned_filament_g"] == 357.5
    client._parse_report(dict(base, printFileName=GCODES + "/Other.gcode"))
    assert client.data["planned_filament_g"] is None
    client._parse_report(dict(base, printFileName=GCODES + "/Other.gcode", retGcodeFileInfo=LISTING))
    assert client.data["planned_filament_g"] is None                  # not in the listing: unknown, not 0
