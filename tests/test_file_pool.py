"""«Общие файлы по моделям»: groups, merging, the plan, copying, the panel's own storage."""
import http.server
import json
import threading
import time
from types import SimpleNamespace

import pytest

from app.domain.models import PrinterState
from app.services import file_pool as fp, printer_commands, printer_files

PRINTERS = [
    {"id": "c1", "kind": "bambu", "model": "X1C", "label": "C1", "host": "h1"},
    {"id": "c2", "kind": "bambu", "model": "X1C", "label": "C2", "host": "h2"},
    {"id": "c3", "kind": "bambu", "model": "X1C", "label": "C3", "host": "h3"},
    {"id": "p1", "kind": "bambu", "model": "P2S", "label": "P1", "host": "h4"},
    {"id": "p2", "kind": "bambu", "model": "P2S", "label": "P2", "host": "h7"},
    {"id": "k1", "kind": "creality", "model": "k1c", "label": "K1", "host": "127.0.0.1"},
    {"id": "v1", "kind": "klipper", "model": "Varan", "label": "Varan", "host": "h6"},
]


def entry(name, size, path=None):
    return {"name": name, "title": name.split(".")[0], "size": size, "mtime": 100, "path": path or "/mnt/sdcard/" + name}


@pytest.fixture
def farm(monkeypatch, tmp_path):
    from app.services import printer_registry
    monkeypatch.setattr(printer_registry, "list_printers", lambda include_deleted=True: [dict(p) for p in PRINTERS])
    monkeypatch.setattr(printer_registry, "get_printer", lambda pid, *a, **k: next((dict(p) for p in PRINTERS if p["id"] == pid), None))
    monkeypatch.setattr(fp, "DIRECTORY", tmp_path / "pool")
    monkeypatch.setattr(fp, "_plans", {})
    monkeypatch.setattr(fp, "_job", {})
    state = SimpleNamespace(
        listings={"c1": [entry("A.gcode.3mf", 10), entry("B.gcode.3mf", 20)],
                  "c2": [entry("A.gcode.3mf", 10), entry("B.gcode.3mf", 99)],     # a different B
                  "c3": [], "p1": [entry("Q.gcode.3mf", 5, "/userdata/model/history/Q.gcode.3mf"),
                                   dict(entry("U.gcode.3mf", 6, "/U.gcode.3mf"), via="ftps", storage="udisk")],
                  "p2": [],
                  "k1": [entry("K.gcode", 7, "/usr/data/printer_data/gcodes/K.gcode")]},
        busy=set(), offline=set(), uploads=[], no_card={"p1", "p2"})   # P2S: no USB stick unless a test puts one in

    def list_files(pid):
        if pid in state.offline:
            raise LookupError(printer_files.MSG_OFFLINE)
        return {"files": [dict(e) for e in state.listings[pid]]}
    monkeypatch.setattr(printer_files, "list_files", list_files)
    view = lambda pid: None if pid in state.offline else SimpleNamespace(
        gcode_state="RUNNING" if pid in state.busy else "IDLE", status=SimpleNamespace(filament_change=None), raw={})
    monkeypatch.setattr(printer_commands, "_live_view", lambda pid, prn: view(pid))
    monkeypatch.setattr(printer_files, "_collector", lambda pid: SimpleNamespace(live_device=lambda: None if pid in state.offline else
        SimpleNamespace(home_flag=SimpleNamespace(sdcard_status="missing" if pid in state.no_card else "normal"))))
    monkeypatch.setattr(printer_commands, "_ws_live", lambda pid, prn: None if pid in state.offline else (
        SimpleNamespace(state=PrinterState.PRINTING if pid in state.busy else PrinterState.IDLE), {"device_state": 0}))
    return state


def test_groups_by_model_with_what_each_printer_allows(farm):
    got = {g["key"]: g for g in fp.groups()}
    assert set(got) == {"bambu-X1C", "bambu-P2S", "creality-k1c"}          # Klipper is not in the pool
    assert [p["label"] for p in got["bambu-X1C"]["printers"]] == ["C1", "C2", "C3"]
    assert got["bambu-X1C"]["syncable"] and got["creality-k1c"]["syncable"]
    assert got["bambu-P2S"]["syncable"] and got["bambu-P2S"]["printers"][0]["read"] is False   # with a stick


def test_pool_merges_by_name_and_size_and_counts_the_panels_files(farm):
    fp.store_upload("bambu-X1C", "Pi.gcode.3mf", [b"x" * 4])
    pool = fp.build_pool("bambu-X1C")
    by = {(f["name"], f["size"]): f for f in pool["files"]}
    assert by[("A.gcode.3mf", 10)]["on"] == ["c1", "c2"]
    assert by[("B.gcode.3mf", 20)]["on"] == ["c1"] and by[("B.gcode.3mf", 99)]["on"] == ["c2"]
    assert by[("Pi.gcode.3mf", 4)]["pi"] is True and by[("Pi.gcode.3mf", 4)]["sources"][0] == {"pi": True, "name": "Pi.gcode.3mf"}
    with pytest.raises(LookupError):
        fp.build_pool("bambu-NOPE")
    with pytest.raises(LookupError):
        fp.build_pool("../etc")


def test_plan_copies_missing_files_never_overwrites_and_says_why_it_skips(farm):
    farm.busy.add("c2")
    plan = fp.make_plan("bambu-X1C")
    targets = {t["printer"]: t for t in plan["targets"]}
    assert set(targets) == {"c1", "c3"}
    assert [(f["name"], f["size"]) for f in targets["c3"]["files"]] == [("A.gcode.3mf", 10), ("B.gcode.3mf", 20)]
    assert targets["c3"]["conflicts"] == ["B.gcode.3mf"]                      # never two files of one name
    assert targets["c1"]["files"] == [] and targets["c1"]["conflicts"] == ["B.gcode.3mf"]   # c2's other B
    assert plan["skipped"] == [{"printer": "c2", "label": "C2", "why": fp.WHY_BUSY}]
    assert plan["copies"] == 2 and plan["total_bytes"] == 30 and plan["free_space"] is None


def test_plan_skips_offline_and_firmware_without_upload(farm):
    farm.offline.add("c3")
    plan = fp.make_plan("bambu-X1C")
    assert {"printer": "c3", "label": "C3", "why": printer_files.MSG_OFFLINE} in plan["skipped"]
    p2s = fp.make_plan("bambu-P2S")
    assert p2s["targets"] == [] and {s["why"] for s in p2s["skipped"]} == {fp.WHY_NO_STICK}
    farm.no_card.add("c1")
    assert {"printer": "c1", "label": "C1", "why": fp.WHY_NO_CARD} in fp.make_plan("bambu-X1C")["skipped"]


def test_plan_of_creality_uses_only_the_panels_files(farm):
    plan = fp.make_plan("creality-k1c")
    assert plan["targets"] == [] and plan["copies"] == 0   # K.gcode is on the only K1C; Creality gives nothing out
    fp.store_upload("creality-k1c", "New.gcode", [b"G28\n"])
    plan = fp.make_plan("creality-k1c")
    assert [f["name"] for f in plan["targets"][0]["files"]] == ["New.gcode"]


def test_sync_copies_with_progress_and_rechecks_each_printer(farm, monkeypatch, tmp_path):
    plan = fp.make_plan("bambu-X1C")
    source = tmp_path / "src.bin"
    source.write_bytes(b"0123456789")

    def fetch(group, f, workdir):
        return str(source)
    monkeypatch.setattr(fp, "_fetch", fetch)

    def upload(prn, name, local, progress):
        farm.uploads.append((prn["id"], name))
        progress(5)
        if name == "B.gcode.3mf":
            raise OSError("550")
    monkeypatch.setattr(fp, "_upload_bambu", upload)
    started = fp.start_sync(plan["id"])
    assert started["running"] is True
    with pytest.raises(ValueError, match="устарел"):
        fp.start_sync(plan["id"])                             # a plan runs once
    for _ in range(100):
        if not fp.status()["running"]:
            break
        time.sleep(0.02)
    st = fp.status()
    row = next(r for r in st["printers"] if r["printer"] == "c3")
    assert ("c3", "A.gcode.3mf") in farm.uploads and row["files_done"] == 1
    assert row["errors"] and row["errors"][0]["name"] == "B.gcode.3mf" and row["state"] == "done"


def test_sync_skips_a_printer_that_got_busy(farm, monkeypatch, tmp_path):
    plan = fp.make_plan("bambu-X1C")
    farm.busy.add("c3")
    monkeypatch.setattr(fp, "_fetch", lambda *a: str(tmp_path))
    monkeypatch.setattr(fp, "_upload_bambu", lambda *a: farm.uploads.append(a))
    fp.start_sync(plan["id"])
    for _ in range(100):
        if not fp.status()["running"]:
            break
        time.sleep(0.02)
    row = next(r for r in fp.status()["printers"] if r["printer"] == "c3")
    assert row["state"] == "skipped" and row["why"] == fp.WHY_BUSY and farm.uploads == []


@pytest.mark.parametrize("group,name,data,message", [
    ("bambu-X1C", "../x.gcode.3mf", [b"x"], fp.MSG_BAD_NAME),
    ("bambu-X1C", ".hidden.gcode.3mf", [b"x"], fp.MSG_BAD_NAME),
    ("bambu-X1C", "model.stl", [b"x"], fp.MSG_BAD_TYPE_BAMBU),
    ("creality-k1c", "x.gcode.3mf", [b"x"], fp.MSG_BAD_TYPE_CREALITY),
    ("bambu-X1C", "empty.gcode.3mf", [], fp.MSG_BAD_NAME),
])
def test_upload_checks(farm, group, name, data, message):
    with pytest.raises(ValueError) as e:
        fp.store_upload(group, name, data)
    assert str(e.value) == message
    assert fp.pi_files(group) == []


def test_upload_bounds_and_no_overwrite(farm, monkeypatch):
    monkeypatch.setattr(fp, "MAX_FILE_BYTES", 8)
    with pytest.raises(ValueError, match="512"):
        fp.store_upload("bambu-X1C", "big.gcode.3mf", [b"12345", b"6789"])
    fp.store_upload("bambu-X1C", "ok.gcode.3mf", [b"1234"])
    with pytest.raises(ValueError) as e:
        fp.store_upload("bambu-X1C", "ok.gcode.3mf", [b"1"])
    assert str(e.value) == fp.MSG_EXISTS
    monkeypatch.setattr(fp, "MAX_POOL_BYTES", 6)
    with pytest.raises(ValueError) as e:
        fp.store_upload("bambu-X1C", "two.gcode.3mf", [b"123"])
    assert str(e.value) == fp.MSG_POOL_FULL
    assert [f["name"] for f in fp.pi_files("bambu-X1C")] == ["ok.gcode.3mf"]   # no leftovers
    fp.delete_pi_file("bambu-X1C", "ok.gcode.3mf")
    with pytest.raises(LookupError):
        fp.delete_pi_file("bambu-X1C", "ok.gcode.3mf")


def test_creality_upload_is_the_web_pages_multipart_post(tmp_path):
    got = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            got["path"] = self.path
            got["type"] = self.headers["Content-Type"]
            got["body"] = self.rfile.read(int(self.headers["Content-Length"]))
            body = json.dumps({"code": 200}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.handle_request, daemon=True).start()
    local = tmp_path / "f.gcode"
    local.write_bytes(b"G28\nG1 X1\n")
    import http.client as hc
    real = hc.HTTPConnection
    sent = []
    try:
        fp.http.client.HTTPConnection = lambda host, port, timeout: real("127.0.0.1", server.server_port, timeout=timeout)
        fp._upload_creality({"host": "printer"}, "Деталь 1.gcode", str(local), sent.append)
    finally:
        fp.http.client.HTTPConnection = real
        server.server_close()
    assert got["path"] == "/upload/%D0%94%D0%B5%D1%82%D0%B0%D0%BB%D1%8C%201.gcode"
    assert got["type"].startswith("multipart/form-data; boundary=")
    assert b'name="file"; filename="\xd0\x94\xd0\xb5\xd1\x82\xd0\xb0\xd0\xbb\xd1\x8c 1.gcode"' in got["body"]
    assert b"G28\nG1 X1\n" in got["body"] and sum(sent) == 10


# --- API ---

@pytest.fixture
def api(monkeypatch, farm):
    from fastapi.testclient import TestClient
    import config as cfg
    from app.api import admin as api_admin, auth as api_auth, main as api_main
    from app.domain.user_models import UserRole
    from app.services import auth_service
    monkeypatch.setattr(cfg, "JWT_SECRET", "test-secret", raising=False)
    monkeypatch.setattr(auth_service, "get_user_from_token", lambda *a, **k: ("admin", UserRole.ADMIN))
    monkeypatch.setattr(auth_service, "decode_token", lambda *a, **k: {"iat": 0})
    monkeypatch.setattr(api_auth, "token_subject_active", lambda *a, **k: True)
    audit = []
    monkeypatch.setattr(api_admin, "get_audit_service",
                        lambda: SimpleNamespace(log=lambda user, action, *a, **k: audit.append(action)))
    store = SimpleNamespace(get_all=lambda: [], get_one=lambda pid: None)
    client = TestClient(api_main.create_app(store))
    client.cookies.set("access_token", "stub")
    return SimpleNamespace(client=client, audit=audit)


def test_api_upload_raw_body_pool_and_plan(api):
    r = api.client.post("/api/admin/file-pool/creality-k1c/upload", params={"name": "New.gcode"}, content=b"G28\n",
                        headers={"Content-Type": "application/octet-stream"})
    assert r.status_code == 200 and r.json() == {"name": "New.gcode", "size": 4}
    assert api.audit == ["file_pool_upload:creality-k1c:size=4"]
    r = api.client.post("/api/admin/file-pool/creality-k1c/upload", params={"name": "x.stl"}, content=b"x")
    assert r.status_code == 400 and r.json()["detail"] == fp.MSG_BAD_TYPE_CREALITY
    pool = api.client.get("/api/admin/file-pool/creality-k1c").json()
    assert {f["name"]: f["readable"] for f in pool["files"]} == {"New.gcode": True, "K.gcode": False}
    plan = api.client.post("/api/admin/file-pool/creality-k1c/plan").json()
    assert plan["copies"] == 1
    assert api.client.get("/api/admin/file-pool").json()["groups"][0]["key"]
    assert api.client.post("/api/admin/file-pool/creality-k1c/sync", json={"plan": "nope"}).status_code == 409
    r = api.client.delete("/api/admin/file-pool/creality-k1c/stored", params={"name": "New.gcode"})
    assert r.status_code == 200 and api.audit[-1] == "file_pool_delete:creality-k1c"


def test_plan_of_ticked_files_still_respects_every_name_on_the_printer(farm):
    pool = {(f["name"], f["size"]): f["id"] for f in fp.build_pool("bambu-X1C")["files"]}
    plan = fp.make_plan("bambu-X1C", [pool[("B.gcode.3mf", 99)]])     # only c2's B
    targets = {t["printer"]: t for t in plan["targets"]}
    assert targets["c1"]["files"] == [] and targets["c1"]["conflicts"] == ["B.gcode.3mf"]   # c1 has another B
    assert [(f["name"], f["size"]) for f in targets["c3"]["files"]] == [("B.gcode.3mf", 99)]
    assert plan["copies"] == 1


def test_p2s_copies_only_from_a_stick_and_says_why_not_from_its_memory(farm):
    pool = {f["name"]: f for f in fp.build_pool("bambu-P2S")["files"]}
    assert pool["U.gcode.3mf"]["sources"] == [{"printer": "p1", "path": "/U.gcode.3mf", "name": "U.gcode.3mf", "via": "ftps"}]
    assert pool["Q.gcode.3mf"]["sources"] == [] and pool["Q.gcode.3mf"]["why"] == fp.WHY_INTERNAL
    farm.no_card.discard("p2")                        # a stick in P2
    plan = fp.make_plan("bambu-P2S")
    assert [(t["label"], [f["name"] for f in t["files"]]) for t in plan["targets"]] == [("P2", ["U.gcode.3mf"])]
    assert plan["unavailable"] == [{"name": "Q.gcode.3mf", "why": fp.WHY_INTERNAL}]
    assert {"printer": "p1", "label": "P1", "why": fp.WHY_NO_STICK} in plan["skipped"]


def test_creality_files_say_why_they_cannot_be_copied(farm):
    [f] = fp.build_pool("creality-k1c")["files"]
    assert f["sources"] == [] and f["why"] == fp.WHY_CREALITY


def test_samples_are_never_copied(farm):
    farm.listings["c1"].append(dict(entry("3DBenchy.gcode.3mf", 3), demo=True))   # shown: the setting is off
    pool = {f["name"]: f for f in fp.build_pool("bambu-X1C")["files"]}
    assert pool["3DBenchy.gcode.3mf"]["sample"] is True
    plan = fp.make_plan("bambu-X1C")
    assert all(f["name"] != "3DBenchy.gcode.3mf" for t in plan["targets"] for f in t["files"])
    assert all(u["name"] != "3DBenchy.gcode.3mf" for u in plan["unavailable"])
