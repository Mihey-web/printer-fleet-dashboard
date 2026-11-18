"""«Файлы на принтере»: listings, 3MF plates, AMS mapping, the start payloads and guards.

Listing shapes are the ones the printers sent live on 16.11.2026 (names made up).
"""
import io
import json
import zipfile
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.domain.models import PrinterState
from app.services import job_previews, printer_commands, printer_files as pf
from app.vendor.pybambu.media_sources import RemoteMediaFile

SLICE = b"""<?xml version="1.0" encoding="UTF-8"?>
<config>
  <plate>
    <metadata key="index" value="1"/>
    <metadata key="prediction" value="5705"/>
    <metadata key="weight" value="51.78"/>
    <metadata key="nozzle_diameters" value="0.4"/>
    <object identify_id="10" name="Cap.stl" skipped="false"/>
    <filament id="1" tray_info_idx="GFG99" type="PETG" color="#FFF144" used_m="17" used_g="51.78"/>
  </plate>
  <plate>
    <metadata key="index" value="2"/>
    <metadata key="prediction" value="600"/>
    <metadata key="weight" value="10"/>
    <object identify_id="11" name="Hook.stl" skipped="false"/>
    <object identify_id="12" name="Hook.stl" skipped="false"/>
    <filament id="3" type="PLA" color="#FF0000FF" used_g="6"/>
    <filament id="1" type="PETG" color="#FFF144" used_g="4"/>
  </plate>
</config>"""


def png():
    from PIL import Image
    out = io.BytesIO()
    Image.new("RGBA", (8, 8), (255, 0, 0, 255)).save(out, format="PNG")
    return out.getvalue()


def archive(plates=(1, 2)):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("Metadata/slice_info.config", SLICE)
        for n in plates:
            z.writestr("Metadata/plate_%d.gcode" % n, "G28\n")
            z.writestr("Metadata/plate_%d.png" % n, png())
    return out.getvalue()


UNITS = [{"index": 0, "slots": [{"type": "PETG", "color": "FFF000", "empty": False},
                                {"type": "PLA", "color": "00FF00", "empty": False},
                                {"type": "PLA", "color": "EE1111", "empty": False},
                                {"type": None, "color": None, "empty": True}]},
         {"index": 128, "slots": [{"type": "PETG", "color": "000000", "empty": False}]}]


# --- parsing ---

def test_bambu_listing_keeps_projects_once_newest_first_demos_last():
    t = lambda s: datetime.fromtimestamp(s, timezone.utc)
    files = [RemoteMediaFile("Old.gcode.3mf", "/userdata/model/history/Old.gcode.3mf", 10, "model", "tcp6000", "emmc", t(100)),
             RemoteMediaFile("New.gcode.3mf", "/userdata/model/history/New.gcode.3mf", 20, "model", "tcp6000", "emmc", t(200)),
             RemoteMediaFile("New.gcode.3mf", "/userdata/model/history/New.gcode.3mf", 20, "model", "tcp6000", "udisk", t(200)),
             RemoteMediaFile("3DBenchy.gcode.3mf", "/userdata/model/bbl/3DBenchy.gcode.3mf", 5, "model", "tcp6000", "emmc", t(900)),
             RemoteMediaFile("clip.mp4", "/timelapse/clip.mp4", 5, "model", "tcp6000", "emmc", t(900))]
    out = pf.bambu_entries(files)
    assert [e["title"] for e in out] == ["New", "Old", "3DBenchy"]
    assert out[0]["storage"] == "emmc" and out[0]["mtime"] == 200 and out[-1]["demo"] is True


def test_creality_v2_listing_k1c_k2():
    item = {"name": "Part.stl_PETG_1h47m42s.gcode", "path": "/usr/data/printer_data/gcodes/Part.stl_PETG_1h47m42s.gcode",
            "file_size": "2527466", "create_time": "1794712179", "timeCost": "6462", "material": "PETG",
            "materialColors": "#FFFF00FF", "filamentWeight": "53.63",
            "preview": "/usr/data//creality/local_gcode/original/Part.stl_PETG_1h47m42s.png",
            "thumbnail": "/usr/data//creality/local_gcode/humbnail/Part.stl_PETG_1h47m42s.png"}
    [e] = pf.creality_entries([item, {"name": "x", "path": "relative"}, "junk"])
    assert e["path"] == item["path"] and e["title"] == "Part" and e["size"] == 2527466
    assert e["time_s"] == 6462 and e["grams"] == 53.63 and e["filaments"][0]["color"] == "#FFFF00"
    assert e["thumb"] == "humbnail/Part.stl_PETG_1h47m42s.png"


def test_creality_v1_listing_ender5max():
    listing = {"totalNum": 2, "fileInfo": "/usr/data/printer_data/gcodes:Tube.STL_PETG_10h50m6s.gcode:73760053:0.28:"
                                         "1794635800:117036:/tmp/creality/local_gcode/humbnail/Tube.png;"
                                         "/usr/data/printer_data/gcodes:New.gcode:10:0.2:1794700000:1:/tmp/x/New.png;"}
    out = pf.creality_entries(listing)
    assert [e["name"] for e in out] == ["New.gcode", "Tube.STL_PETG_10h50m6s.gcode"]
    assert out[1]["path"] == "/usr/data/printer_data/gcodes/Tube.STL_PETG_10h50m6s.gcode"
    assert out[1]["size"] == 73760053 and out[1]["thumb"] == "humbnail/Tube.png"


def test_plates_from_slice_info():
    plates = pf.plates_from_slice_info(SLICE)
    assert [p["index"] for p in plates] == [1, 2]
    assert plates[0] == {"index": 1, "time_s": 5705, "grams": 51.78, "title": "Cap", "nozzle_mm": 0.4,
                         "filaments": [{"id": 1, "type": "PETG", "color": "FFF144", "grams": 51.78,
                                        "tray_info_idx": "GFG99"}]}
    assert [f["id"] for f in plates[1]["filaments"]] == [1, 3] and plates[1]["title"] == "Hook"
    with pytest.raises(ValueError):
        pf.plates_from_slice_info(b"<!DOCTYPE x [<!ENTITY a 'b'>]><config/>")


def test_archive_detail_keeps_sliced_plates_and_saves_thumbs(tmp_path, monkeypatch):
    monkeypatch.setattr(job_previews, "DIRECTORY", tmp_path)
    with zipfile.ZipFile(io.BytesIO(archive(plates=(2,)))) as z:
        detail = pf.archive_detail(z)
    assert [p["index"] for p in detail["plates"]] == [2]
    assert job_previews.valid_key(detail["plates"][0]["thumb"])


def test_ranges_reader_is_a_bounded_seekable_zip_source():
    body = archive()
    calls = []
    reader = pf._Ranges(len(body), lambda start, n: calls.append(start) or body[start:start + n])
    with zipfile.ZipFile(reader) as z:
        assert z.read("Metadata/slice_info.config") == SLICE
    assert calls and all(c % pf.BLOCK == 0 for c in calls)


# --- AMS ---

def test_ams_slots_and_default_mapping():
    slots = pf.ams_slots(UNITS)
    assert [(s["slot"], s["name"]) for s in slots] == [(0, "1A"), (1, "1B"), (2, "1C"), (3, "1D"), (16, "HT1")]
    filaments = [{"id": 1, "type": "PETG", "color": "000000"}, {"id": 3, "type": "PLA", "color": "FF0000"},
                 {"id": 4, "type": "ABS", "color": "FFFFFF"}]
    assert pf.default_mapping(filaments, slots) == [16, 2, pf.EXTERNAL_SPOOL]


# --- payloads ---

ENTRY = {"path": "/userdata/model/history/Cap.gcode.3mf", "name": "Cap.gcode.3mf", "storage": "emmc"}
ONE = [{"id": 1, "type": "PETG"}]


@pytest.mark.parametrize("model,entry,url", [
    ("P2S", ENTRY, "file:///userdata/model/history/Cap.gcode.3mf"),
    ("X1C", {"path": "/mnt/sdcard/Cap.gcode.3mf", "name": "Cap.gcode.3mf", "storage": "external"},
     "file:///sdcard/Cap.gcode.3mf"),
    ("P1S", {"path": "/Cap.gcode.3mf", "name": "Cap.gcode.3mf", "storage": "sdcard"}, "file:///sdcard/Cap.gcode.3mf"),
    ("H2S", {"path": "/Cap.gcode.3mf", "name": "Cap.gcode.3mf", "storage": "udisk"}, "ftp:///Cap.gcode.3mf"),
])
def test_project_file_url(model, entry, url):
    p = pf.build_project_file(model, entry, 2, ONE, [0], pf.ams_slots(UNITS), True, False)["print"]
    assert p["command"] == "project_file" and p["url"] == url and p["param"] == "Metadata/plate_2.gcode"
    assert p["subtask_name"] == "Cap" and p["use_ams"] is True and p["ams_mapping"] == [0]
    assert p["bed_leveling"] is True and p["auto_bed_leveling"] == 1 and p["timelapse"] is False
    assert p["flow_cali"] is False and p["extrude_cali_flag"] == 0


def test_project_file_mapping_is_indexed_by_filament_id():
    slots = pf.ams_slots(UNITS)
    p = pf.build_project_file("X1C", ENTRY, 2, [{"id": 1}, {"id": 3}], [16, 2], slots, False, True)["print"]
    assert p["ams_mapping"] == [16, -1, 2]
    assert p["ams_mapping2"] == [{"ams_id": 128, "slot_id": 0}, {"ams_id": 255, "slot_id": 255},
                                 {"ams_id": 0, "slot_id": 2}]
    assert p["auto_bed_leveling"] == 0 and p["timelapse"] is True


def test_project_file_external_spool_only_is_not_an_ams_print():
    p = pf.build_project_file("P1S", ENTRY, 1, ONE, [pf.EXTERNAL_SPOOL], [], True, False)["print"]
    assert p["use_ams"] is False and p["ams_mapping"] == [pf.EXTERNAL_SPOOL] and "ams_mapping2" not in p


# --- guards ---

def view(gcode="IDLE", change=None, raw=None, units=UNITS):
    return SimpleNamespace(gcode_state=gcode, status=SimpleNamespace(filament_change=change),
                           raw=raw or {}, units=units)


@pytest.fixture
def farm(monkeypatch, tmp_path):
    from app.services import printer_registry
    printers = {"b1": {"id": "b1", "kind": "bambu", "model": "X1C", "label": "C1"},
                "k1": {"id": "k1", "kind": "creality", "model": "k1c", "label": "K1", "host": "10.0.0.9"}}
    monkeypatch.setattr(printer_registry, "get_printer", lambda pid, *a, **k: printers.get(pid))
    monkeypatch.setattr(printer_commands, "bambu_model", lambda pid, prn: prn.get("model"))
    monkeypatch.setattr(pf, "_listings", {})
    monkeypatch.setattr(pf, "_details", {})
    state = SimpleNamespace(view=view(), live=(SimpleNamespace(state=PrinterState.IDLE), {"device_state": 0}),
                            sent=[], capability=True, printers=printers)
    monkeypatch.setattr(printer_commands, "_live_view", lambda pid, prn: state.view)
    monkeypatch.setattr(printer_commands, "_ws_live", lambda pid, prn: state.live)
    monkeypatch.setattr(printer_commands, "get_capability", lambda pid: state.capability)
    monkeypatch.setattr(printer_commands, "_inflight_blocks", lambda pid, raw: False)
    monkeypatch.setattr(printer_commands, "_start_fast_refresh", lambda *a, **k: None)
    monkeypatch.setattr(printer_commands, "_note_reply", lambda *a, **k: None)

    def send_local(pid, payload, guard=None):
        if guard is not None and guard():
            return {"success": False, "detail": "skip", "replied": False, "skipped": True}
        state.sent.append(payload)
        return {"success": True, "detail": "подтверждено принтером", "replied": True}
    monkeypatch.setattr(printer_commands, "_send_local", send_local)
    monkeypatch.setattr(printer_commands, "_send_creality",
                        lambda pid, action, frames: state.sent.append(frames) or {"success": True, "detail": "отправлено"})
    entry = {"path": "/mnt/sdcard/Cap.gcode.3mf", "name": "Cap.gcode.3mf", "storage": "external", "size": 9, "mtime": 5}
    pf._listings["b1"] = {"at": pf.time.monotonic(), "files": {entry["path"]: entry}}
    pf._details[("b1", entry["path"], 9, 5)] = {"plates": pf.plates_from_slice_info(SLICE)}
    k_entry = {"path": "/usr/data/printer_data/gcodes/A.gcode", "name": "A.gcode"}
    pf._listings["k1"] = {"at": pf.time.monotonic(), "files": {k_entry["path"]: k_entry}}
    return state


GOOD = {"path": "/mnt/sdcard/Cap.gcode.3mf", "plate": 2, "mapping": [0, 2]}


def test_bambu_start_sends_project_file(farm):
    r = pf.start("b1", dict(GOOD, timelapse=True))
    assert r["success"] and r["status"] == "confirmed"
    [payload] = farm.sent
    assert payload["print"]["url"] == "file:///sdcard/Cap.gcode.3mf"
    assert payload["print"]["ams_mapping"] == [0, -1, 2] and payload["print"]["timelapse"] is True


@pytest.mark.parametrize("params,message", [
    ({"path": "/mnt/sdcard/Other.gcode.3mf", "plate": 1, "mapping": [0]}, pf.MSG_UNKNOWN_FILE),
    ({"path": "/mnt/sdcard/Cap.gcode.3mf", "plate": 7, "mapping": [0]}, pf.MSG_BAD_PLATE),
    ({"path": "/mnt/sdcard/Cap.gcode.3mf", "plate": "1", "mapping": [0]}, pf.MSG_BAD_PLATE),
    ({"path": "/mnt/sdcard/Cap.gcode.3mf", "plate": 2, "mapping": [0]}, pf.MSG_BAD_MAPPING),
    ({"path": "/mnt/sdcard/Cap.gcode.3mf", "plate": 1, "mapping": [True]}, pf.MSG_BAD_MAPPING),
    ({"path": "/mnt/sdcard/Cap.gcode.3mf", "plate": 1, "mapping": [9]}, pf.MSG_BAD_SLOT % 9),
    ({"path": "/mnt/sdcard/Cap.gcode.3mf", "plate": 1, "mapping": [3]}, pf.MSG_EMPTY_SLOT % "1D"),
])
def test_bambu_start_parameter_refusals(farm, params, message):
    r = pf.start("b1", params)
    assert r == {"success": False, "status": "refused", "detail": message, "replied": False}
    assert farm.sent == []


@pytest.mark.parametrize("v,message", [
    (view(gcode="RUNNING"), pf.MSG_BUSY),
    (view(gcode="PAUSE"), pf.MSG_BUSY),
    (view(gcode=""), pf.MSG_BUSY),
    (view(change={"phase": "load"}), printer_commands.MSG_CHANGE_RUNNING),
    (view(raw={"busy": True}), printer_commands.MSG_CHANGE_RUNNING),
    (None, printer_commands.MSG_NO_TELEMETRY),
])
def test_bambu_start_state_refusals(farm, v, message):
    farm.view = v
    r = pf.start("b1", dict(GOOD) if v is not None else {"path": GOOD["path"], "plate": 1, "mapping": [pf.EXTERNAL_SPOOL]})
    assert r["status"] == "refused" and r["detail"] == message and farm.sent == []


def test_bambu_start_rechecks_state_under_the_publish_lock(farm, monkeypatch):
    views = iter([view(), view(gcode="PREPARE")])
    monkeypatch.setattr(printer_commands, "_live_view", lambda pid, prn: next(views))
    r = pf.start("b1", GOOD)
    assert r["status"] == "refused" and r["detail"] == pf.MSG_BUSY and farm.sent == []


def test_bambu_start_refused_without_print_commands_or_detail(farm):
    farm.capability = False
    assert pf.start("b1", GOOD)["detail"] == pf.MSG_NO_PRINT_CMDS
    farm.capability = True
    pf._details.clear()
    assert pf.start("b1", GOOD)["detail"] == pf.MSG_UNREADABLE


def test_dual_nozzle_refused(farm):
    farm.printers["b1"]["model"] = "H2D"
    assert pf.start("b1", GOOD)["detail"] == printer_commands.MSG_DUAL_NOZZLE


def test_stale_listing_is_not_startable(farm, monkeypatch):
    pf._listings["b1"]["at"] -= pf.LIST_TTL_S + 1
    assert pf.start("b1", GOOD)["detail"] == pf.MSG_UNKNOWN_FILE


def test_creality_start_sends_printprt(farm):
    r = pf.start("k1", {"path": "/usr/data/printer_data/gcodes/A.gcode"})
    assert r["success"] and farm.sent == [[{"opGcodeFile": "printprt:/usr/data/printer_data/gcodes/A.gcode"}]]


@pytest.mark.parametrize("live", [
    None,
    (SimpleNamespace(state=PrinterState.PRINTING), {"device_state": 0}),
    (SimpleNamespace(state=PrinterState.PAUSED), {"device_state": 0}),
    (SimpleNamespace(state=PrinterState.IDLE), {"device_state": 7}),
    (SimpleNamespace(state=PrinterState.FINISHED), {"device_state": 0, "feed_state": 2}),
])
def test_creality_start_refusals(farm, live):
    farm.live = live
    r = pf.start("k1", {"path": "/usr/data/printer_data/gcodes/A.gcode"})
    assert r["status"] == "refused" and farm.sent == []
    assert pf.start("k1", {"path": "/etc/passwd"})["detail"] == pf.MSG_UNKNOWN_FILE


def test_other_kinds_refused(farm):
    assert pf.start("nope", {"path": "/x"})["detail"] == pf.MSG_NOT_SUPPORTED


def test_creality_listing_waits_for_a_fresh_reply(farm, monkeypatch):
    item = {"name": "A.gcode", "path": "/usr/data/printer_data/gcodes/A.gcode", "create_time": "5"}

    class Client:
        connected = True
        file_listing = (0.0, [{"name": "Old.gcode", "path": "/old.gcode"}])   # an older reply: not used

        def request_file_listing(self):
            self.file_listing = (pf.time.monotonic() + 0.01, [item])
    store = SimpleNamespace(get_collector=lambda pid: SimpleNamespace(client=Client()))
    monkeypatch.setattr(pf, "_store", store)
    out = pf.list_files("k1")
    assert [f["name"] for f in out["files"]] == ["A.gcode"] and pf.listed("k1", item["path"])


def test_creality_client_keeps_the_listing():
    import asyncio
    from app.clients.creality_client import CrealityK1Client
    client = CrealityK1Client("10.0.0.9", "K1")
    asyncio.run(client._receive({"retGcodeFileInfo": {"fileInfo": ""}}))
    assert client.file_listing[1] == {"fileInfo": ""}


# --- API ---

@pytest.fixture
def api(monkeypatch):
    from fastapi.testclient import TestClient
    import config as cfg
    from app.api import admin as api_admin, auth as api_auth, main as api_main
    from app.domain.user_models import UserRole
    from app.services import auth_service
    role = {"r": UserRole.ADMIN}
    monkeypatch.setattr(cfg, "JWT_SECRET", "test-secret", raising=False)
    monkeypatch.setattr(auth_service, "get_user_from_token", lambda *a, **k: ("admin", role["r"]))
    monkeypatch.setattr(auth_service, "decode_token", lambda *a, **k: {"iat": 0})
    monkeypatch.setattr(api_auth, "token_subject_active", lambda *a, **k: True)
    audit, calls = [], []
    monkeypatch.setattr(api_admin, "get_audit_service",
                        lambda: SimpleNamespace(log=lambda user, action, *a, **k: audit.append(action)))
    monkeypatch.setattr(pf, "start", lambda pid, params: calls.append((pid, params)) or
                        {"success": False, "status": "refused", "detail": pf.MSG_BUSY, "replied": False})
    monkeypatch.setattr(pf, "list_files", lambda pid: {"kind": "bambu", "files": []})
    store = SimpleNamespace(get_all=lambda: [], get_one=lambda pid: None)
    client = TestClient(api_main.create_app(store))
    client.cookies.set("access_token", "stub")
    return SimpleNamespace(client=client, audit=audit, calls=calls, role=role, UserRole=UserRole)


def test_api_start_is_audited_and_refusal_is_400(api):
    r = api.client.post("/api/admin/printers/b1/print", json={"path": "/x.3mf", "plate": 2, "mapping": [0, 16],
                                                              "bed_leveling": False})
    assert r.status_code == 400 and r.json()["detail"] == pf.MSG_BUSY
    assert api.calls == [("b1", {"path": "/x.3mf", "plate": 2, "mapping": [0, 16], "bed_leveling": False})]
    assert api.audit == ["printer_print:b1:file=?,plate=2,mapping=0/16,bed_leveling=False,st=refused"]


def test_api_is_admin_only(api):
    api.role["r"] = api.UserRole.VIEWER if hasattr(api.UserRole, "VIEWER") else list(api.UserRole)[-1]
    assert api.client.get("/api/admin/printers/b1/files").status_code == 403
    assert api.client.post("/api/admin/printers/b1/print", json={"path": "/x"}).status_code == 403
    assert api.calls == []


# --- H2S: the file channel keeps the internal storage closed (live 16.11, every H2S) ---

class _Source:
    files, ftps_files, closed_result = [], [], 0

    def __init__(self, proxy):
        pass

    def close(self):
        pass


def _sources(monkeypatch, tcp, ftps, result):
    from app.vendor.pybambu import media_sources as ms

    class Tcp(_Source):
        def list_files(self, kind, ext, paths=None):
            return list(tcp)

        def _connect(self):
            pass

        def _send_ctrl_request(self, cmd, req):
            assert req["storage"] == "emmc" and req["type"] == "model"
            return 1

        def _read_matching_reply(self, cmd, seq, timeout=None):
            return {"result": result}, b""

    class Ftps(_Source):
        def list_files(self, kind, ext, paths=None):
            assert paths == ["/"]
            return list(ftps)
    monkeypatch.setattr(ms, "Tcp6000MediaSource", Tcp)
    monkeypatch.setattr(ms, "Ftps990MediaSource", Ftps)


def _file(name, path, size=10, storage="emmc"):
    return RemoteMediaFile(name, path, size, "model", "x", storage, datetime.fromtimestamp(100, timezone.utc))


def test_h2s_closed_storage_says_so_instead_of_an_empty_list(monkeypatch):
    _sources(monkeypatch, [], [], pf.STORAGE_UNAVAILABLE)
    with pytest.raises(LookupError) as e:
        pf.list_bambu(object(), "H2S")
    assert str(e.value) == pf.MSG_STORAGE_CLOSED
    _sources(monkeypatch, [], [], 0)
    assert pf.list_bambu(object(), "H2S") == []          # truly empty: no reason made up


def test_usb_stick_over_ftps_is_listed_for_p2_h2_once(monkeypatch):
    _sources(monkeypatch, [_file("A.gcode.3mf", "/userdata/model/history/A.gcode.3mf"),
                           _file("U.gcode.3mf", "/mnt/udisk/U.gcode.3mf", storage="udisk")],
             [_file("U.gcode.3mf", "/U.gcode.3mf")], pf.STORAGE_UNAVAILABLE)
    out = pf.list_bambu(object(), "P2S")
    assert [(e["path"], e.get("via")) for e in out] == [("/U.gcode.3mf", "ftps"), ("/userdata/model/history/A.gcode.3mf", None)]
    p = pf.build_project_file("H2S", out[0], 1, [{"id": 1}], [pf.EXTERNAL_SPOOL], [], True, False)["print"]
    assert p["url"] == "ftp:///U.gcode.3mf"
    _sources(monkeypatch, [_file("S.gcode.3mf", "/mnt/sdcard/S.gcode.3mf", storage="external")], [_file("S.gcode.3mf", "/S.gcode.3mf")], 0)
    assert [e["path"] for e in pf.list_bambu(object(), "X1C")] == ["/mnt/sdcard/S.gcode.3mf"]   # X1C's FTPS is its SD: not twice


def test_usb_file_is_read_over_ftps(monkeypatch):
    body = archive()
    calls = []
    monkeypatch.setattr(pf, "_ftp_range", lambda client, path: calls.append(path) or (lambda s, n: body[s:s + n]))
    monkeypatch.setattr(pf, "_tcp6000_archive", lambda *a: pytest.fail("not the file channel"))
    monkeypatch.setattr(job_previews, "save", lambda data: "0" * 64)
    d = pf.bambu_detail(object(), "H2S", {"path": "/U.gcode.3mf", "name": "U.gcode.3mf", "size": len(body), "via": "ftps"})
    assert calls == ["/U.gcode.3mf"] and [p["index"] for p in d["plates"]] == [1, 2]


# --- factory samples: by exact name, hidden by the general setting ---

@pytest.mark.parametrize("name,sample", [
    ("3DBenchy.gcode.3mf", True), ("Film Sticking Fixture by AMS.gcode.3mf", True), ("Scraper Grip_ams.gcode.3mf", True),
    ("mini tray.gcode.3mf", True), ("Mini tray.gcode.3mf", True), ("Bambulab_Spool part 2.gcode.3mf", True),
    ("Vase 2.gcode.3mf", False), ("My Vase.gcode.3mf", False), ("Vase.3mf", False),
    ("Crealitylogo_PLA_0h31m.gcode", True), ("3DBenchy_PLA_1h2m.gcode", True), ("3DBench_PLA_0h50m.gcode", True),
    ("Деталь_PETG_1h.gcode", False), ("x3DBenchy.gcode", False),
])
def test_samples_by_exact_name(name, sample):
    assert pf.is_sample(name) is sample


def test_samples_not_by_folder_and_hidden_by_the_setting(monkeypatch, farm):
    from app.services import settings_service
    files = [_file("Vase.gcode.3mf", "/userdata/model/bbl/Vase.gcode.3mf"),
             _file("Mine.gcode.3mf", "/userdata/model/bbl/Mine.gcode.3mf")]      # the owner's own file there
    entries = pf.bambu_entries(files)
    assert [(e["name"], e.get("demo")) for e in entries] == [("Mine.gcode.3mf", None), ("Vase.gcode.3mf", True)]
    monkeypatch.setattr(pf, "list_bambu", lambda client, model: entries)
    monkeypatch.setattr(pf, "_store", SimpleNamespace(get_collector=lambda pid: SimpleNamespace(client=SimpleNamespace(connected=True))))
    out = pf.list_files("b1")
    assert [f["name"] for f in out["files"]] == ["Mine.gcode.3mf"] and out["hidden_samples"] == 1
    assert pf.listed("b1", "/userdata/model/bbl/Vase.gcode.3mf")        # still a known file of the listing
    monkeypatch.setattr(settings_service, "peek", lambda key: False if key == "hide_sample_files" else None)
    assert len(pf.list_files("b1")["files"]) == 2
