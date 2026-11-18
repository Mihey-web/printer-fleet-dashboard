"""QA 17.11.2026, part A: Creality, Klipper, MKS, telemetry parsing, the job card.

The testers' confirming tests (qa/code, qa/func, qa/hunt branches, docs/dev/qa-2026-11-17), moved
here with their imports fixed; each failed on main 0f67c79. Where a test pinned a detail the fix
does differently, the note says what changed. Mocked HTTP / WebSocket only, never a printer.
"""
import asyncio
import copy
import datetime as dt_mod
import json
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

import bambu_testkit as kit
from app.clients import creality_client as cc, moonraker
from app.clients.creality_client import CrealityK1Client
from app.clients.klipper_client import KlipperClient
from app.clients.mks_wifi_client import MksWifiClient
from app.collectors.bambu_collector import BambuCollector
from app.domain.models import PrinterKind, PrinterState
from app.services import job_previews, klipper_project as kp, print_material as pm, project_summary as ps
from app.services.normalizer import normalize_bambu, normalize_ws_dict
from test_klipper_project import HEAD, LISTING as K2_LISTING, ROOT, gcode
from test_print_material import _MemoryAndFtp, _support_archive

ROOT_DIR = Path(__file__).resolve().parents[1]


# --- Н2 / Н3, F3, hunt 20: the K2 Plus job card from its G-code -------------------------------

HOST = "10.0.0.100"
GCODES = "/mnt/UDISK/printer_data/gcodes"
JOB = GCODES + "/Корпус_PETG_10h0m0s.gcode"
ITEM = {"path": JOB, "name": "Корпус_PETG_10h0m0s.gcode", "material": "PETG", "materialColors": "#FFF144",
        "filamentWeight": "120.5", "timeCost": 36000, "nozzleTemp": 24500, "bedTemp": 8000}
SETTINGS = {"layer_height": "0.2", "wall_loops": "3", "sparse_infill_density": "20%",
            "enable_support": "1", "use_relative_e_distances": "1", "support_type": "tree(auto)"}


class FakeKClient:
    """Stands in for the vendored WebSocket client: counts the file-listing requests."""
    reconnect_count = 0
    is_connected = True

    def __init__(self):
        self.listing_requests = 0

    async def request_gcode_file_info(self):
        self.listing_requests += 1

    async def _send_json(self, message):
        pass


@pytest.fixture
def k2(tmp_path, monkeypatch):
    monkeypatch.setattr(job_previews, "DIRECTORY", tmp_path)
    monkeypatch.setattr(moonraker, "known", lambda host: True)          # Moonraker answers: a K2 Plus
    c = CrealityK1Client(HOST, "K2")
    c._client = FakeKClient()
    return c


def _drive(client, messages):
    """Feed WS messages through the real _receive on an event loop; let the executor jobs finish."""
    async def run():
        for m in messages:
            client._material_read_at = -1e9          # as if FILE_INFO_RETRY_S had passed since the last ask
            await client._receive(dict(m))
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.3)
    asyncio.run(run())


def _stored(client):
    key = client.data.get("project_key")
    return json.loads((job_previews.DIRECTORY / (key + ".json")).read_text(encoding="utf-8")) if key else None


def test_k2_card_is_read_again_after_one_failed_read(k2, monkeypatch):
    """qa-code Н2: «read again with the next listing» — but the next listing was only asked for
    while project_key is None, and the listing's own card had just set it."""
    attempts = []

    def flaky(host, path):
        attempts.append(path)
        if len(attempts) == 1:
            raise moonraker.MoonrakerError("URLError")          # Wi-Fi blip / Moonraker restarting
        return ("Корпус_PETG_10h0m0s.gcode", dict(SETTINGS, enable_support="0"), "")
    monkeypatch.setattr(kp, "read_settings", flaky)

    status = {"printFileName": JOB, "state": 1}
    _drive(k2, [status, {"retGcodeFileInfo2": [ITEM], **status}] + [status] * 20)   # ~40 min of telemetry
    assert len(attempts) >= 2, "the settings were never read again after one failure"
    assert _stored(k2).get("layer_mm") == 0.2


def test_k2_card_keeps_settings_when_the_support_count_fails(k2, monkeypatch):
    """qa-code Н3: a network error during the whole-file read for the supports' grams replaced the
    full card with the bare listing card."""
    monkeypatch.setattr(kp, "read_settings", lambda host, path: ("Корпус_PETG_10h0m0s.gcode", SETTINGS, ""))

    def wifi_drop(host, rel):                     # the whole-file read (minutes over Wi-Fi) breaks off
        raise TimeoutError("timed out")
    monkeypatch.setattr(kp, "support_grams", wifi_drop)
    k2._parse_report({"printFileName": JOB, "state": 1})
    k2._save_summary(JOB, ps.creality(ITEM))
    card = _stored(k2)
    assert card.get("layer_mm") == 0.2 and card.get("walls") == 3


F3_JOB = GCODES + "/Корпус.gcode"
F3_LISTING = {"title": "Корпус", "time_s": 36000, "grams": 120.5, "slicer": "Creality Print",
              "filaments": [{"type": "PETG", "color": "#FFF144", "grams": 120.5}]}
F3_SETTINGS = {"layer_height": "0.2", "enable_support": "1", "use_relative_e_distances": "1", "wall_loops": "3",
               "sparse_infill_density": "20%"}


def _f3_client(tmp_path, monkeypatch):
    monkeypatch.setattr(job_previews, "DIRECTORY", tmp_path)
    monkeypatch.setattr(moonraker, "known", lambda host: True)
    c = CrealityK1Client(HOST, "K2")
    c._parse_report({"printFileName": F3_JOB, "state": 1})
    return c


def test_f3_k2_supports_count_failure_keeps_the_settings_card(tmp_path, monkeypatch):
    c = _f3_client(tmp_path, monkeypatch)
    monkeypatch.setattr(kp, "read_settings",
                        lambda host, path: ("Корпус.gcode", F3_SETTINGS, "; generated by Creality_Print V6.1\n"))

    def broken(host, rel):
        raise moonraker.MoonrakerError("timeout")     # the 300 s budget of a big file, a Wi-Fi drop
    monkeypatch.setattr(kp, "support_grams", broken)
    c._save_summary(F3_JOB, F3_LISTING)
    assert _stored(c).get("layer_mm") == 0.2


def test_f3b_k2_failed_read_is_repeated_by_the_client(tmp_path, monkeypatch):
    """qa-func F3b: «Read once per job, retried after a failure», and no retry came. The tester's
    test counted listing requests; the retry now reads the G-code again from the listing's card
    the client already holds (no second ~150 KiB listing), so the reads are counted."""
    c = _f3_client(tmp_path, monkeypatch)
    asks, reads = [], []

    class FakeWs:
        reconnect_count = 0

        async def request_gcode_file_info(self):
            asks.append(time.monotonic())

        async def _send_json(self, payload):
            pass
    c._client = FakeWs()
    asyncio.run(c._receive({"printFileName": F3_JOB, "state": 1}))
    assert len(asks) == 1                                                  # the listing is asked for

    def broken(host, path):
        reads.append(path)
        raise moonraker.MoonrakerError("timeout")
    monkeypatch.setattr(kp, "read_settings", broken)
    c._save_summary(F3_JOB, F3_LISTING)                                    # the listing came, the read failed
    assert c._details_path is None and len(reads) == 1
    c._material_read_at -= 10 * 3600                                       # long after FILE_INFO_RETRY_S
    asyncio.run(c._receive({"state": 1}))
    assert len(reads) == 2                                                 # read again
    for _ in range(5):                                                     # FILE_INFO_ATTEMPTS bounds it
        c._material_read_at -= 10 * 3600
        asyncio.run(c._receive({"state": 1}))
    assert len(reads) == cc.FILE_INFO_ATTEMPTS
    assert _stored(c) == F3_LISTING                                        # the listing's card meanwhile


def test_f3_card_shows_no_impossible_values():
    """qa-func F3, «мелочь там же»: «nan%» infill and «-3» walls came onto the card as written."""
    card = ps.with_gcode(F3_LISTING, dict(F3_SETTINGS, sparse_infill_density="nan%", wall_loops="-3"), "")
    assert "infill" not in card and "walls" not in card and card["layer_mm"] == 0.2
    card = ps.with_gcode(F3_LISTING, dict(F3_SETTINGS, sparse_infill_density="15.5%", wall_loops="0"), "")
    assert (card["infill"], card["walls"]) == ("15.5%", 0)


def test_hunt20_settings_survive_a_failed_support_count(tmp_path, monkeypatch):
    monkeypatch.setattr(job_previews, "DIRECTORY", tmp_path)
    monkeypatch.setattr(moonraker, "known", lambda host: True)
    c = CrealityK1Client(HOST, "K2")
    c._parse_report({"printFileName": ROOT + "/Корпус.gcode", "state": 1})
    settings = ps.gcode_config(gcode(support=1).decode())
    monkeypatch.setattr(kp, "read_settings", lambda host, path: ("Корпус.gcode", settings, HEAD.decode()))

    def drop(host, rel):              # the whole file (tens of MB) did not come through
        raise moonraker.MoonrakerError("timeout")
    monkeypatch.setattr(kp, "support_grams", drop)
    c._save_summary(ROOT + "/Корпус.gcode", K2_LISTING)
    card = _stored(c)
    assert card.get("layer_mm") == 0.2 and card.get("walls") == 3 and card.get("supports") == "древовидные, авто"
    assert "support_grams" not in card


# --- Н9, hunt 16 / 30: Klipper status parsing ----------------------------------------------------

def test_klipper_message_after_a_bad_value_is_still_applied():
    """qa-code Н9: one value it could not convert aborted the sections after it (webhooks: the
    host's shutdown), while on_message still refreshed last_message_ts."""
    c = KlipperClient("127.0.0.1", 7125, "t")
    c.on_message(None, json.dumps({"method": "notify_status_update", "params": [{
        "print_stats": {"state": "printing"},
        "extruder": {"temperature": None},                       # a thermistor without a reading
        "webhooks": {"state": "shutdown", "state_message": "MCU 'mcu' shutdown: Timer too close"}}]}))
    assert c.data.get("host_state") == "shutdown"
    assert c.data.get("nozzle_temp") == 0                          # the bad value is skipped, not stored


def test_klipper_new_job_clears_previous_layers():
    """hunt 16: print_stats.reset() sends info {current_layer: null, total_layer: null}; null was
    read as «absent» and the last job's 250/250 stayed on the new one."""
    k = KlipperClient("host", 7125, "Varan")
    k.parse_status({"print_stats": {"state": "complete", "filename": "a.gcode", "print_duration": 7200,
                                    "info": {"current_layer": 250, "total_layer": 250}},
                    "virtual_sdcard": {"progress": 1.0}, "webhooks": {"state": "ready"}})
    k.parse_status({"print_stats": {"state": "printing", "filename": "b.gcode", "print_duration": 300,
                                    "info": {"current_layer": None, "total_layer": None}},
                    "virtual_sdcard": {"progress": 0.05}})
    st = normalize_ws_dict("k1", "Varan", PrinterKind.KLIPPER, k.get_data())
    assert (st.current_layer, st.total_layers) in ((None, None), (0, 0))
    k.parse_status({"print_stats": {"info": {"current_layer": 3}}})          # a diff: the other one unchanged
    assert (k.data["current_layer"], k.data["total_layers"]) == (3, None)
    k.parse_status({"print_stats": {"state": "standby", "filename": ""}})    # SDCARD_RESET_FILE: no file
    assert k.data["filename"] is None


def test_klipper_temperature_and_fractions_rounded():
    """hunt 30: int(float(x)) and int(f * 100) cut 214.97 °C to 214 and 0.57 to 56 %."""
    k = KlipperClient("host", 7125, "Varan")
    k.parse_status({"print_stats": {"state": "printing"},
                    "extruder": {"temperature": 214.97, "target": 215},
                    "heater_bed": {"temperature": 59.96, "target": 60},
                    "fan": {"speed": 0.29}, "display_status": {"progress": 0.57}})
    d = k.get_data()
    assert (d["nozzle_temp"], d["bed_temp"], d["fan_speed_pct"], d["progress"]) == (215, 60, 29, 57)
    k.parse_status({"display_status": {"progress": 0.996}})
    assert k.data["progress"] == 99                                # never 100 % before the end


# --- hunt 1 / 30 / 31, qa-ui 5: the Creality client ----------------------------------------------

A = "/usr/data/printer_data/gcodes/a.gcode"
B = "/usr/data/printer_data/gcodes/b.gcode"
JOB_A_END = {"state": 1, "printFileName": A, "printProgress": 100, "printJobTime": 3600,
             "printLeftTime": 0, "layer": 250, "TotalLayer": 250, "err": {"errcode": 0, "key": 0}}
FINISH = {"state": 3}


def _ws_client(label="K1C"):
    c = CrealityK1Client("host", label)
    c._client._ws = object()
    c._client._ws_ready.set()
    return c


def _feed(c, *frames):
    for f in frames:
        c._parse_report(c._client._absorb(f))   # cumulative, as KClient hands it on


def _creality_status(c):
    return normalize_ws_dict("c1", "K1C", PrinterKind.CREALITY, c.get_data(), device_type="k1c")


def test_creality_new_job_start_frame_is_not_the_old_100_percent():
    c = _ws_client()
    _feed(c, JOB_A_END, FINISH, {"state": 1, "printFileName": B, "printProgress": 0,
                                 "printJobTime": 0, "printLeftTime": 0, "layer": 0, "TotalLayer": 0})
    st = _creality_status(c)
    assert (st.state, st.progress_pct) == (PrinterState.PRINTING, 0)


def test_creality_new_job_with_echoed_layers_is_not_finished():
    c = _ws_client()
    _feed(c, JOB_A_END, FINISH, {"state": 1, "printFileName": B, "printProgress": 0,
                                 "printJobTime": 0, "printLeftTime": 0})
    st = _creality_status(c)
    assert (st.state, st.progress_pct) == (PrinterState.PRINTING, 0)


def test_creality_zeroed_time_left_mid_print_is_not_finished():
    # hunt «не подтверждено»: a runout zeroes printLeftTime while state stays 1 (live, creality-3)
    c = _ws_client()
    _feed(c, {"state": 1, "printFileName": B, "printProgress": 10, "printJobTime": 600,
              "printLeftTime": 5400, "materialStatus": 0},
          {"printJobTime": 660, "printLeftTime": 0, "materialStatus": 1})
    st = _creality_status(c)
    assert st.state == PrinterState.PAUSED and st.progress_pct < 100


def test_creality_progress_is_the_printers_own():
    # live 17.11: Ender-5 Max 22 % on the card (layers) against 37 % on the printer
    c = _ws_client()
    _feed(c, {"state": 1, "printFileName": B, "printProgress": 60, "printJobTime": 3600,
              "printLeftTime": 2400, "layer": 10, "TotalLayer": 100})
    assert _creality_status(c).progress_pct == 60
    c2 = _ws_client()                                              # firmware without it: the layers, then the time
    _feed(c2, {"state": 1, "printJobTime": 3600, "printLeftTime": 2400, "layer": 10, "TotalLayer": 100})
    assert c2.get_data()["progress"] == 10


def test_creality_temperature_rounded():
    c = _ws_client()
    c._parse_report(c._client._absorb({"state": 1, "nozzleTemp": "219.950000", "bedTemp0": "59.960000"}))
    d = c.get_data()
    assert (d["nozzle_temp"], d["bed_temp"]) == (220, 60)


def test_creality_bad_fan_value_does_not_freeze_time_left():
    """hunt 31: one odd value rode on every later (cumulative) frame and aborted the parse at it."""
    c = _ws_client()
    feed = lambda f: c._parse_report(c._client._absorb(f))      # noqa: E731
    feed({"state": 1, "printJobTime": 600, "printLeftTime": 3000})
    feed({"modelFanPct": ""})                       # one odd value from the firmware
    feed({"printJobTime": 1800, "printLeftTime": 1800, "layer": 50, "TotalLayer": 100})
    d = c.get_data()
    assert (d["remaining_time"], d["current_layer"]) == (1800, 50)


@pytest.mark.parametrize("raw,shown", [
    ("printer hw ver:;printer sw ver:;DWIN hw ver:CR4CU220812S11;DWIN sw ver:1.3.5.22;", "1.3.5.22"),   # K1
    ("printer hw ver:;printer sw ver:;DWIN hw ver:F004;DWIN sw ver:V1.2.0.24;", "1.2.0.24"),           # E5_2
    ("printer hw ver:CR0CN240110C10;printer sw ver:V1.1.2.10;DWIN hw ver:;DWIN sw ver:;", "1.1.2.10"),
    ("printer hw ver:;printer sw ver:;DWIN hw ver:;DWIN sw ver:;", None),
    ("1.3.5.22", "1.3.5.22"),
])
def test_creality_firmware_is_the_version_not_the_raw_string(raw, shown):
    """qa-ui 5: «Админ → Парк» showed modelVersion as it came."""
    c = _ws_client()
    _feed(c, {"state": 0, "modelVersion": raw})
    assert c.get_data()["firmware_version"] == shown


def test_creality_dead_helpers_and_magic_numbers_gone(monkeypatch):
    """qa-code Н13 and the hardcode table: no dead translate_* / format_remaining_minutes; the 30 s
    staleness is a named constant."""
    from app.clients import klipper_client
    for module, name in ((cc, "translate_k1_state"), (cc, "format_remaining_minutes"),
                         (klipper_client, "translate_klipper_state"), (klipper_client, "format_remaining_minutes")):
        assert not hasattr(module, name), name
    c = _ws_client()
    _feed(c, {"state": 1})
    assert c.get_data()
    monkeypatch.setattr(cc, "STALE_S", -1)
    assert c.get_data() == {}


# --- hunt 32: MKS --------------------------------------------------------------------------------

def test_mks_print_time_over_99_hours():
    d = MksWifiClient._parse_raw("M997 PRINTING\r\nM27 80\r\nT:210 /210 B:60 /60\r\nM992 100:00:00\r\n")
    assert (d["print_time"], d["remaining_time"]) == (360000, 90000)


# --- hunt 6: «Стол» on the job card is the bed of the filament printed ----------------------------

def test_k2_card_keeps_the_bed_temperature_of_the_filament_it_prints():
    # PETG (bed 80) and PLA (bed 55) in the project; only PLA prints — the printer's listing says 55 °C
    listing = {"title": "Корпус", "bed_temp": 55.0, "nozzle_temp": 220.0,
               "filaments": [{"type": "PLA", "grams": 20.5}]}
    settings = {"curr_bed_type": "Epoxy Resin Plate", "epoxy_resin_plate_temp": ["80", "55"],
                "filament_type": ["PETG", "PLA"], "layer_height": "0.2", "enable_support": "0"}
    assert ps.with_gcode(listing, settings, "").get("bed_temp") == 55.0
    # no listing value and no sure match: no guess; one value for every filament: that one
    assert "bed_temp" not in ps.with_gcode(dict(listing, bed_temp=None), settings, "")
    same = dict(settings, epoxy_resin_plate_temp=["60", "60"])
    assert ps.with_gcode(dict(listing, bed_temp=None), same, "").get("bed_temp") == 60.0


def test_bambu_card_bed_temperature_of_a_plate_printed_with_filament_2():
    document = {"plate": None, "files": {
        "slice_info.config": {"config": {"header": [], "plate": [{
            "metadata": [{"key": "index", "value": "1"}, {"key": "prediction", "value": "3600"},
                         {"key": "weight", "value": "12.5"}],
            "filament": [{"id": "2", "type": "PETG", "color": "#000000", "used_g": "12.5", "used_m": "4.1"}],
            "object": [{"name": "Кронштейн", "identify_id": "10", "skipped": "false"}]}]}},
        "project_settings.config": {"curr_bed_type": "Textured PEI Plate",
                                    "textured_plate_temp": ["55", "70"],
                                    "filament_type": ["PLA", "PETG"], "nozzle_temperature": ["220", "255"],
                                    "layer_height": "0.2"},
    }}
    card = ps.bambu(document)
    assert card["filaments"][0]["nozzle_temp"] == 255.0
    assert card.get("bed_temp") == 70.0


# --- hunt 8 / 21 / 22 / 23: Bambu telemetry in the normalizer ---------------------------------------

def _p2s_status(mutate=None):
    c = BambuCollector("fx", "192.0.2.10", "00000000", kit.FAKE_SERIALS["P2S"], "P2S")
    c.client.client = kit.StubPaho()
    try:
        for line in kit.load_fixture("P2S"):
            msg = copy.deepcopy(line["msg"])
            if mutate:
                mutate(msg)
            kit.deliver(c, msg)
        return c.client.get_device(), normalize_bambu("fx", "P2S", c.fetch(), device_type="P2S")
    finally:
        c.close()


def test_loaded_spool_without_material_is_not_shown_empty():
    """hunt 8: tray state 11 without type and colour (a spool without RFID, nothing set) read
    «пусто», and load / re-read the tag were refused «Слот N пуст»."""
    def unset_slot3(msg):
        for unit in msg["print"].get("ams", {}).get("ams", []):
            for tray in unit["tray"]:
                if tray["id"] == "2":          # fixture: {"id": "2", "state": 0} = empty
                    tray["state"] = 11         # same bits as the fixture's loaded slot 1
    dev, status = _p2s_status(unset_slot3)
    lib_tray = dev.ams.data[0].tray[2]
    assert lib_tray.empty is False and lib_tray.name == "?"   # the lib: loaded, material unknown
    slot = status.ams["units"][0]["slots"][2]
    assert slot["empty"] is False and slot["type"] is None and slot["color"] is None, slot
    _dev, plain = _p2s_status()
    assert plain.ams["units"][0]["slots"][2]["empty"] is True      # a really empty slot stays empty


def _js_hum_class(pct, lvl):
    src = (ROOT_DIR / "app/web/app.js").read_text(encoding="utf-8")
    fn = re.search(r"function amsHumClass\(pct, lvl\) \{.*?\n\s*\}", src, re.S).group(0)
    # The card's own RH thresholds (app.js: pct <= 25 ok, <= 40 warn) stand in for amsHumZone.
    js = ("function amsZonesNow(){return null}"
          "function amsHumZone(p){return p<=25?'ok':p<=40?'warn':'bad'}"
          + fn + ";process.stdout.write(JSON.stringify(amsHumClass(%s,%s)))" % (json.dumps(pct), json.dumps(lvl)))
    return json.loads(subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_humidity_level_and_percent_of_the_same_unit_agree():
    """hunt 21: the printer's index runs 5 = driest (the P2S capture: index 5 at 6 % RH); the
    panel reads 1-2 as dry. The tester's test also pinned the index as sent (5); the server now
    hands the panel the level in its direction (1), app.js unchanged."""
    _dev, status = _p2s_status()
    unit = status.ams["units"][0]
    assert (unit["humidity"], unit["humidity_pct"]) == (1, 6)       # one reading, two scales
    by_pct = _js_hum_class(unit["humidity_pct"], None)               # what an AMS 2 Pro shows
    by_index = _js_hum_class(None, unit["humidity"])                 # what a classic AMS shows
    assert by_pct == "ok"
    assert by_index == by_pct, "level %s (6 %% RH) classed %r" % (unit["humidity"], by_index)


def _tray(i):
    return {"id": str(i), "state": 11, "tray_type": "PLA", "tray_color": "FF0000FF", "tray_info_idx": "GFA00"}


def test_unit_without_valid_readings_has_no_humidity_or_temp():
    """hunt 23: an AMS powering on sends humidity 0 / temp -20; the lib keeps its 0 defaults."""
    c = BambuCollector("fx", "192.0.2.10", "00000000", kit.FAKE_SERIALS["P1S"], "P1S")
    c.client.client = kit.StubPaho()
    try:
        for line in kit.load_fixture("P1S"):
            kit.deliver(c, line["msg"])
        kit.deliver(c, {"print": {"ams": {"tray_now": "0", "ams": [
            {"id": "0", "humidity": "0", "temp": "-20.0", "tray": [_tray(i) for i in range(4)]}]}}})
        unit = normalize_bambu("fx", "P1S", c.fetch(), device_type="P1S").ams["units"][0]
    finally:
        c.close()
    assert unit["humidity_pct"] is None
    assert (unit["humidity"], unit["temp"]) == (None, None)


REAL_DT = dt_mod.datetime
CLOCK = {"now": REAL_DT(2026, 11, 17, 12, 0, 40, tzinfo=dt_mod.timezone.utc)}


class FakeDT(REAL_DT):
    @classmethod
    def now(cls, tz=None):
        n = CLOCK["now"]
        return n.astimezone(tz) if tz else n.replace(tzinfo=None)


def test_elapsed_print_time_is_not_inflated_or_growing(monkeypatch):
    """hunt 22: the lib's start_time is rounded down to the minute and never cleared: 41 s one
    second into a job, and 4 h for a 1 h job seen 3 h after FINISH. The tester's test faked
    datetime only and wanted exactly (1, 3600); the exact start is the raw report's (time.time,
    faked here too) and the end the lib's end_time (rounded down): ≈3600."""
    import app.vendor.pybambu.utils as lib_utils
    import time as time_mod
    CLOCK["now"] = REAL_DT(2026, 11, 17, 12, 0, 40, tzinfo=dt_mod.timezone.utc)
    monkeypatch.setattr(dt_mod, "datetime", FakeDT)        # the normalizer's local import
    monkeypatch.setattr(lib_utils, "datetime", FakeDT)     # the lib's get_end_time
    monkeypatch.setattr(time_mod, "time", lambda: CLOCK["now"].timestamp())   # the raw report's start
    c = BambuCollector("fx", "192.0.2.10", "00000000", kit.FAKE_SERIALS["P1S"], "P1S")
    c.client.client = kit.StubPaho()
    try:
        lines = kit.load_fixture("P1S")
        first = copy.deepcopy(lines[0]["msg"])
        first["print"]["gcode_state"] = "IDLE"
        kit.deliver(c, first)
        for line in lines:                                  # the job starts at 12:00:40
            kit.deliver(c, line["msg"])
        CLOCK["now"] += dt_mod.timedelta(seconds=1)
        running = normalize_bambu("fx", "P1S", c.fetch(), device_type="P1S").print_time_seconds
        CLOCK["now"] += dt_mod.timedelta(hours=1)
        kit.deliver(c, {"print": {"gcode_state": "FINISH", "mc_percent": 100, "mc_remaining_time": 0}})
        CLOCK["now"] += dt_mod.timedelta(hours=3)           # sits finished on the bed for 3 h
        finished = normalize_bambu("fx", "P1S", c.fetch(), device_type="P1S").print_time_seconds
        kit.deliver(c, {"print": {"gcode_state": "IDLE"}})
        idle = normalize_bambu("fx", "P1S", c.fetch(), device_type="P1S").print_time_seconds
    finally:
        c.close()
    assert running == 1
    assert finished is not None and abs(finished - 3600) < 60, finished
    assert idle is None                                     # the last job's start is no clock


# --- Н11: the planned grams do not wait for the supports' count -----------------------------------

def test_p1_grams_and_card_come_before_the_supports_count(tmp_path, monkeypatch):
    """qa-code Н11: archive_grams handed the grams back only after the whole gcode was read for the
    supports (up to 300 s), in the 2-thread pool every printer's read shares."""
    monkeypatch.setattr(job_previews, "DIRECTORY", tmp_path)
    _xml, content = _support_archive("true")
    client = _MemoryAndFtp(content)
    client._device_type = "A1"                         # no memory read: the archive is the only source
    gate, threads = threading.Event(), []
    real = pm.support_grams

    def slow(stream, deadline, chunk=pm._CHUNK):
        threads.append(threading.current_thread().name)
        assert gate.wait(10)
        return real(stream, deadline, chunk)
    monkeypatch.setattr(pm, "support_grams", slow)
    reader = pm.MaterialReader()
    key = (1, "j", "Job", "Metadata/plate_1.gcode")
    reader.key = key
    worker = threading.Thread(target=reader._read, args=(client, key), daemon=True)
    worker.start()
    worker.join(5)
    try:
        assert not worker.is_alive(), "the job's read waits for its supports' count"
        assert reader.value == 9.9 and reader.error is None and reader.project
    finally:
        gate.set()
    deadline = time.monotonic() + 10
    card = None
    while time.monotonic() < deadline:
        card = json.loads((tmp_path / (reader.project + ".json")).read_text(encoding="utf-8"))
        if card.get("support_grams") is not None:
            break
        time.sleep(0.05)
    assert card.get("support_grams") == 5.6 and card.get("layer_mm") == 0.2
    assert threads and all(name.startswith("print-supports") for name in threads), threads
    assert pm._POOL._max_workers == pm.READ_WORKERS and pm._SUPPORT_POOL is not pm._POOL
