"""Klipper's own API on a Creality (Moonraker :7125, the K2 Plus): the client, files through it,
the notification's «Продолжить печать» and «Сервис». Mocked HTTP only, never a real printer."""
import io
import json
import os
import urllib.error
from types import SimpleNamespace

import pytest

from app.clients import moonraker
from app.domain.models import PrinterState
from app.services import (file_pool as fp, klipper_service, notification_actions as na, notifications as nf,
                          printer_commands as pc, printer_files as pf)

HOST = "10.0.0.100"
ROOT = "/mnt/UDISK/printer_data/gcodes"


# --- a fake Moonraker ---------------------------------------------------------------------

class Fake:
    """Routes urllib requests by (method, path); records every request."""

    def __init__(self):
        self.routes = {}
        self.requests = []
        self.files = {}

    def __call__(self, req, timeout=None):
        url = req.full_url
        assert url.startswith("http://%s:7125/" % HOST) and timeout is not None
        path, _, query = url[len("http://%s:7125" % HOST):].partition("?")
        self.requests.append((req.get_method(), path, query))
        if path.startswith("/server/files/gcodes/") and req.get_method() == "GET":
            body = self.files.get(path)
            if body is None:
                raise urllib.error.HTTPError(url, 404, "nf", {}, None)
            return io.BytesIO(body)
        answer = self.routes.get((req.get_method(), path))
        if answer is None:
            raise urllib.error.HTTPError(url, 404, "nf", {}, None)
        if isinstance(answer, Exception):
            raise answer
        return io.BytesIO(json.dumps({"result": answer(query) if callable(answer) else answer}).encode())


@pytest.fixture
def fake(monkeypatch):
    f = Fake()
    f.routes[("GET", "/server/info")] = {"klippy_state": "ready", "components": ["file_manager"]}
    monkeypatch.setattr(moonraker, "_open", f)
    moonraker.forget()
    return f


# --- the client -----------------------------------------------------------------------------

def test_probe_detects_and_caches(fake):
    assert moonraker.known(HOST) is None or moonraker.known(HOST) is True   # the probe ran (synchronous in tests)
    assert moonraker.available(HOST) is True
    calls = len(fake.requests)
    assert moonraker.available(HOST) is True and len(fake.requests) == calls   # cached


def test_no_moonraker_is_false_and_cached(monkeypatch):
    seen = []

    def refused(req, timeout=None):
        seen.append(timeout)
        raise ConnectionRefusedError()
    monkeypatch.setattr(moonraker, "_open", refused)
    assert moonraker.available("10.0.0.138") is False
    assert moonraker.known("10.0.0.138") is False and seen == [moonraker.PROBE_TIMEOUT_S]


def test_known_never_waits_and_rechecks_in_the_background(fake, monkeypatch):
    later = []
    monkeypatch.setattr(moonraker, "_probe_later", lambda host: later.append(host))
    assert moonraker.known(HOST) is None and later == [HOST]          # first: the probe is only scheduled
    moonraker._cache[HOST] = (moonraker._monotonic() - moonraker.RECHECK_S - 1, True)
    assert moonraker.known(HOST) is True and later == [HOST, HOST]    # stale: the old answer, a new probe


def test_a_server_without_klippy_state_is_not_moonraker(fake):
    fake.routes[("GET", "/server/info")] = {"hello": 1}
    assert moonraker.available(HOST) is False


@pytest.mark.parametrize("bad", ["", "/etc/passwd", "../x.gcode", "a/../b.gcode", "a//b", "x\n.gcode", "a\\b", None, 5])
def test_paths_outside_the_root_are_refused(bad):
    with pytest.raises(moonraker.MoonrakerError):
        moonraker.check_path(bad)


def test_list_roots_delete_and_quoting(fake):
    fake.routes[("GET", "/server/files/roots")] = [{"name": "gcodes", "path": ROOT}, {"name": "x"}]
    fake.routes[("GET", "/server/files/list")] = [
        {"path": "Корпус 20.gcode", "size": 5, "modified": 1.5}, {"path": "../evil.gcode"}, "junk"]
    fake.routes[("DELETE", "/server/files/gcodes/%D0%9A%D0%BE%D1%80%D0%BF%D1%83%D1%81%2020.gcode")] = {"item": {}}
    assert moonraker.roots(HOST) == {"gcodes": ROOT}
    assert moonraker.list_gcodes(HOST) == [{"path": "Корпус 20.gcode", "size": 5, "modified": 1.5}]
    moonraker.delete(HOST, "Корпус 20.gcode")
    assert fake.requests[-1][0] == "DELETE"
    assert ("GET", "/server/files/list", "root=gcodes") in fake.requests


def test_errors_are_moonraker_errors(fake):
    fake.routes[("GET", "/x")] = OSError("boom")
    with pytest.raises(moonraker.MoonrakerError):
        moonraker.request(HOST, "GET", "/x")
    with pytest.raises(moonraker.MoonrakerError):
        moonraker.request(HOST, "GET", "/missing")


def test_download_is_bounded_by_the_listed_size(fake, tmp_path):
    fake.files["/server/files/gcodes/A.gcode"] = b"G28\nG1\n"
    local = str(tmp_path / "a")
    moonraker.download(HOST, "A.gcode", local, 7)
    assert open(local, "rb").read() == b"G28\nG1\n"
    with pytest.raises(moonraker.MoonrakerError):
        moonraker.download(HOST, "A.gcode", local, 3)          # longer than listed
    with pytest.raises(moonraker.MoonrakerError):
        moonraker.download(HOST, "A.gcode", local, 99)         # short


class FakeConnection:
    instances = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.timeout, self.sent, self.headers = host, port, timeout, [], {}
        FakeConnection.instances.append(self)

    def putrequest(self, method, path):
        self.request = (method, path)

    def putheader(self, k, v):
        self.headers[k] = v

    def endheaders(self):
        pass

    def send(self, data):
        self.sent.append(data)

    def getresponse(self):
        return SimpleNamespace(status=201, read=lambda n: b'{"result": {}}')

    def close(self):
        pass


def test_upload_is_multipart_into_the_gcodes_root(monkeypatch, tmp_path):
    FakeConnection.instances = []
    monkeypatch.setattr(moonraker, "_connection", FakeConnection)
    local = tmp_path / "f"
    local.write_bytes(b"G28\n")
    done = []
    moonraker.upload(HOST, "Деталь.gcode", str(local), done.append)
    [c] = FakeConnection.instances
    body = b"".join(c.sent)
    assert (c.host, c.port, c.request) == (HOST, 7125, ("POST", "/server/files/upload"))
    assert int(c.headers["Content-Length"]) == len(body) and done == [4]
    assert b'name="root"\r\n\r\ngcodes\r\n' in body and 'filename="Деталь.gcode"'.encode() in body
    with pytest.raises(moonraker.MoonrakerError):
        moonraker.upload(HOST, "dir/x.gcode", str(local))


# --- files ------------------------------------------------------------------------------------

K2 = {"id": "k2", "kind": "creality", "model": "k2plus", "label": "K2", "host": HOST}
K1 = {"id": "k1", "kind": "creality", "model": "k1c", "label": "K1", "host": "10.0.0.138"}


@pytest.fixture
def k2(monkeypatch, fake):
    from app.services import printer_registry
    printers = {"k2": dict(K2), "k1": dict(K1)}
    monkeypatch.setattr(printer_registry, "get_printer", lambda pid, *a, **k: printers.get(pid))
    monkeypatch.setattr(printer_registry, "list_printers", lambda include_deleted=True: list(printers.values()))
    monkeypatch.setattr(pf, "_listings", {})
    fake.routes[("GET", "/server/files/roots")] = [{"name": "gcodes", "path": ROOT}]
    fake.routes[("GET", "/server/files/list")] = [
        {"path": "Part.gcode", "size": 70, "modified": 300.0},
        {"path": "Crealitylogo_PLA_1.gcode", "size": 5, "modified": 10.0},
        {"path": "notes.txt", "size": 1, "modified": 1.0}]
    state = SimpleNamespace(ws=None, live=(SimpleNamespace(state=PrinterState.IDLE), {"device_state": 0}))

    def ws_listing(printer_id, wait=pf.LIST_WAIT_S):
        if state.ws is None:
            raise LookupError(pf.MSG_NO_LISTING)
        return state.ws
    monkeypatch.setattr(pf, "_creality_listing", ws_listing)
    monkeypatch.setattr(pc, "_ws_live", lambda pid, prn: state.live)
    monkeypatch.setattr(pf, "hide_samples", lambda: True)
    return state


def test_k2_lists_through_moonraker_with_the_ws_pictures(k2):
    k2.ws = [{"name": "Part.gcode", "path": "/mnt/UDISK//printer_data/gcodes/Part.gcode", "create_time": "1",
              "file_size": "1", "thumbnail": "/x/humbnail/Part.png", "estimatedTime": 600},
             {"name": "Only.gcode", "path": "/usr/data/other/Only.gcode"}]
    out = pf.list_files("k2")
    assert out["klipper"] is True and out["hidden_samples"] == 1
    part, only = out["files"]
    assert part["mr"] == "Part.gcode" and part["size"] == 70 and part["mtime"] == 300
    assert part["path"] == "/mnt/UDISK//printer_data/gcodes/Part.gcode" and part["thumb"] == "humbnail/Part.png"
    assert "mr" not in only                       # only the WS has it: not readable, not deletable
    assert pf.listed("k2", part["path"])["mr"] == "Part.gcode"


def test_a_ws_file_under_another_folder_spelling_merges_by_name_and_size(k2):
    k2.ws = [{"name": "Part.gcode", "path": "/usr/data/printer_data/gcodes/Part.gcode", "file_size": "70",
              "thumbnail": "/x/humbnail/Part.png"}]
    [part] = pf.list_files("k2")["files"]
    assert part["mr"] == "Part.gcode" and part["thumb"] == "humbnail/Part.png"
    assert part["path"] == "/usr/data/printer_data/gcodes/Part.gcode"    # what printprt got before


def test_k2_lists_without_the_ws_listing_and_k1_keeps_the_ws(k2, monkeypatch):
    out = pf.list_files("k2")
    assert [f["path"] for f in out["files"]] == [ROOT + "/Part.gcode"]
    monkeypatch.setattr(moonraker, "_open", lambda req, timeout=None: (_ for _ in ()).throw(ConnectionRefusedError()))
    k2.ws = [{"name": "A.gcode", "path": "/usr/data/printer_data/gcodes/A.gcode"}]
    out = pf.list_files("k1")
    assert out["klipper"] is False and [f.get("mr") for f in out["files"]] == [None]


def test_moonraker_listing_failure_falls_back_to_the_ws(k2, fake):
    fake.routes[("GET", "/server/files/list")] = OSError("x")
    k2.ws = [{"name": "A.gcode", "path": "/usr/data/printer_data/gcodes/A.gcode"}]
    out = pf.list_files("k2")
    assert out["klipper"] is False and [f["name"] for f in out["files"]] == ["A.gcode"]


def test_delete_guards_and_success(k2, fake):
    pf.list_files("k2")
    path = ROOT + "/Part.gcode"
    fake.routes[("DELETE", "/server/files/gcodes/Part.gcode")] = {"item": {"path": "Part.gcode"}}
    assert pf.delete_file("k2", "/etc/passwd")["detail"] == pf.MSG_UNKNOWN_FILE
    k2.live = (SimpleNamespace(state=PrinterState.PAUSED), {"device_state": 0})
    assert pf.delete_file("k2", path) == {"success": False, "status": "refused", "detail": pf.MSG_DELETE_BUSY,
                                          "replied": False}
    assert not any(r[0] == "DELETE" for r in fake.requests)
    k2.live = (SimpleNamespace(state=PrinterState.IDLE), {"device_state": 0})
    r = pf.delete_file("k2", path)
    assert r["success"] and r["status"] == "confirmed" and pf.listed("k2", path) is None
    assert fake.requests[-1][:2] == ("DELETE", "/server/files/gcodes/Part.gcode")


def test_delete_of_a_ws_only_file_is_refused(k2):
    k2.ws = [{"name": "Only.gcode", "path": "/usr/data/other/Only.gcode"}]
    pf.list_files("k2")
    assert pf.delete_file("k2", "/usr/data/other/Only.gcode")["detail"] == pf.MSG_NO_DELETE


def test_delete_failure_is_rejected(k2):
    pf.list_files("k2")
    r = pf.delete_file("k2", ROOT + "/Part.gcode")          # no DELETE route: HTTP 404
    assert r["status"] == "rejected" and r["detail"] == pf.MSG_DELETE_FAILED


# --- the pool -------------------------------------------------------------------------------------

def test_k2_joins_the_pool_as_a_readable_source(k2, monkeypatch, tmp_path):
    monkeypatch.setattr(fp, "DIRECTORY", tmp_path / "pool")
    pf.list_files("k2")                                        # the probe's answer is cached
    group = fp.group_of(K2)
    printers = {p["id"]: p for p in next(g for g in fp.groups() if g["key"] == group)["printers"]}
    assert printers["k2"]["read"] is True and printers["k2"]["upload"] is True
    [f] = fp.build_pool(group)["files"]
    assert f["sources"] == [{"printer": "k2", "path": ROOT + "/Part.gcode", "name": "Part.gcode", "via": None,
                             "mr": "Part.gcode"}]
    seen = []
    monkeypatch.setattr(moonraker, "download", lambda host, rel, local, size=None: seen.append((host, rel, size))
                        or open(local, "wb").write(b"x" * 70))
    workdir = tmp_path / "w"
    workdir.mkdir()
    local = fp._fetch(group, f, str(workdir))
    assert seen == [(HOST, "Part.gcode", 70)] and os.path.getsize(local) == 70


def test_copy_to_a_k2_goes_through_moonraker(k2, monkeypatch, tmp_path):
    pf.list_files("k2")
    up = []
    monkeypatch.setattr(moonraker, "upload", lambda host, name, local, progress=None: up.append((host, name)))
    monkeypatch.setattr(fp, "_upload_creality", lambda *a: pytest.fail("not the web page's POST"))
    monkeypatch.setattr(fp, "_job", {"printers": [{"printer": "k2", "state": "waiting", "files_done": 0, "bytes_done": 0,
                                                   "errors": [], "current": None}]})
    local = tmp_path / "x.gcode"
    local.write_bytes(b"G28")
    fp._copy_one(dict(K2), {"name": "x.gcode", "size": 3}, str(local))
    assert up == [(HOST, "x.gcode")] and fp._job["printers"][0]["files_done"] == 1


# --- the notification ----------------------------------------------------------------------------------

def test_errcode_101_says_what_it_is_and_k2_on_pause_gets_resume():
    data = {"err_code": 101, "err_key": 101, "klipper_api": True}
    [n] = nf.build_creality(data, "paused")
    assert n["text"] == "Ошибка Creality 101/101: " + nf.CREALITY_CODES[101]
    assert [a["key"] for a in n["actions"]] == ["cr_resume", "cr_clear"] and n["actions"][0]["label"] == "Продолжить печать"
    assert n["hint"] == nf.CREALITY_KLIPPER_HINT + " " + nf.CREALITY_HINT and n["severity"] == "pause"
    assert not any("CANCEL" in str(a) or a.get("style") == "danger" for a in n["actions"])


@pytest.mark.parametrize("data,state", [
    ({"err_code": 101, "err_key": 101}, "paused"),                     # no Klipper API (K1C): no button
    ({"err_code": 101, "err_key": 101, "klipper_api": None}, "paused"),
    ({"err_code": 101, "err_key": 101, "klipper_api": True}, "idle"),  # not on a pause
    ({"err_code": 101, "err_key": 101, "klipper_api": True}, "printing"),
])
def test_no_resume_without_klipper_or_off_a_pause(data, state):
    # «Сбросить ошибку» (cleanErr, 16.11) is on every Creality error; «Продолжить» only with Klipper on a pause
    [n] = nf.build_creality(data, state)
    assert [a["key"] for a in n["actions"]] == ["cr_clear"] and n["hint"] == nf.CREALITY_HINT


@pytest.fixture
def notif(monkeypatch):
    from app.services import printer_registry
    monkeypatch.setattr(printer_registry, "get_printer", lambda pid, *a, **k: dict(K2) if pid == "k2" else None)
    monkeypatch.setattr(na, "_marks", {})
    state = SimpleNamespace(state=PrinterState.PAUSED, sent=[])

    def live_status(pid, prn=None):
        return SimpleNamespace(state=state.state, notifications=nf.build_creality(
            {"err_code": 101, "err_key": 101, "klipper_api": True}, state.state.value))
    monkeypatch.setattr(pc, "live_status", live_status)
    monkeypatch.setattr(pc, "send", lambda pid, action, params=None: state.sent.append((pid, action)) or
                        {"success": True, "status": "sent", "detail": "отправлено (без подтверждения)", "replied": False})
    monkeypatch.setattr(pc, "_start_fast_refresh", lambda *a, **k: None)
    return state


def test_resume_button_sends_the_cards_resume(notif):
    r = na.run("k2", "creality:101/101", "cr_resume")
    assert r["status"] == "sent" and notif.sent == [("k2", "resume")]


def test_resume_button_refused_off_the_pause_and_for_other_kinds(notif, monkeypatch):
    notif.state = PrinterState.PRINTING
    assert na.run("k2", "creality:101/101", "cr_resume")["detail"] == na.MSG_NOT_LISTED   # no button now
    # a record that still has the button while the printer runs again: the live state decides
    monkeypatch.setattr(pc, "live_status", lambda pid, prn=None: SimpleNamespace(
        state=PrinterState.PRINTING, notifications=nf.build_creality({"err_code": 101, "err_key": 101,
                                                                     "klipper_api": True}, "paused")))
    assert na.run("k2", "creality:101/101", "cr_resume")["detail"] == pc.MSG_NOT_PAUSED
    assert notif.sent == []
    assert na.ACTIONS["cr_resume"].kinds == ("creality",)


# --- «Сервис» --------------------------------------------------------------------------------------------

@pytest.fixture
def service(monkeypatch, fake):
    from app.services import printer_registry
    printers = {"k2": dict(K2), "k1": dict(K1)}
    monkeypatch.setattr(printer_registry, "get_printer", lambda pid, *a, **k: printers.get(pid))
    monkeypatch.setattr(klipper_service, "_jobs", {})
    st = SimpleNamespace(live=(SimpleNamespace(state=PrinterState.IDLE), {"device_state": 0, "err_code": 0}),
                         klippy={"webhooks": {"state": "ready"}, "print_stats": {"state": "standby"},
                                 "idle_timeout": {"state": "Idle"}},
                         scripts=[], threads=[])
    monkeypatch.setattr(pc, "_ws_live", lambda pid, prn: st.live)
    fake.routes[("GET", "/printer/objects/query")] = lambda q: {"status": st.klippy}
    fake.routes[("GET", "/server/gcode_store")] = {"gcode_store": [{"message": "old", "time": 5.0, "type": "response"}]}
    fake.routes[("POST", "/printer/gcode/script")] = lambda q: st.scripts.append(q) or "ok"
    fake.routes[("POST", "/printer/firmware_restart")] = lambda q: st.scripts.append("restart") or "ok"

    class Now:                        # the job's thread runs inline
        def __init__(self, target, args, **kw):
            self.target, self.args = target, args

        def start(self):
            st.threads.append(self.args)
            self.target(*self.args)
    monkeypatch.setattr(klipper_service.threading, "Thread", Now)
    return st


@pytest.mark.parametrize("action,script", [
    ("svc_bed_mesh", "script=G29"), ("svc_nozzle_pid", "script=NOZZLE_PID"), ("svc_bed_pid", "script=BEDPID"),
    ("svc_shaper", "script=INPUTSHAPER"), ("svc_belts", "script=BELT_TENSION"), ("svc_fw_restart", "restart"),
])
def test_service_runs_the_macro(service, action, script):
    r = pc.send("k2", action)
    assert r["success"] and r["status"] == "sent" and r["detail"].startswith("запущено")
    assert service.scripts == [script]
    job = klipper_service.status("k2", HOST)["job"]
    assert job["state"] == "done" and job["action"] == action and "since" not in job


@pytest.mark.parametrize("change,message", [
    (lambda s: setattr(s, "live", None), pc.MSG_NO_TELEMETRY),
    (lambda s: setattr(s, "live", (SimpleNamespace(state=PrinterState.PRINTING), {"device_state": 0})), pc.MSG_SERVICE_BUSY),
    (lambda s: setattr(s, "live", (SimpleNamespace(state=PrinterState.PAUSED), {"device_state": 0})), pc.MSG_SERVICE_BUSY),
    (lambda s: setattr(s, "live", (SimpleNamespace(state=PrinterState.IDLE), {"device_state": 0, "err_code": 101})),
     pc.MSG_SERVICE_BUSY),
    (lambda s: setattr(s, "live", (SimpleNamespace(state=PrinterState.IDLE), {"device_state": 7})), pc.MSG_SERVICE_BUSY),
    (lambda s: s.klippy.update(idle_timeout={"state": "Printing"}), klipper_service.MSG_EXECUTING),
    (lambda s: s.klippy.update(print_stats={"state": "paused"}), klipper_service.MSG_PRINTING),
    (lambda s: s.klippy.update(webhooks={"state": "shutdown"}), klipper_service.MSG_NOT_READY),
])
def test_service_refusals_send_nothing(service, change, message):
    change(service)
    r = pc.send("k2", "svc_bed_mesh")
    assert r == {"success": False, "status": "refused", "detail": message, "replied": False}
    assert service.scripts == [] and service.threads == []


def test_service_refused_without_klipper_api_and_while_one_runs(service, monkeypatch):
    assert pc.send("k1", "svc_bed_mesh")["detail"] == pc.MSG_NO_KLIPPER_API
    klipper_service._jobs["k2"] = {"action": "svc_shaper", "title": "Калибровка резонансов", "state": "running",
                                   "started": 1.0, "finished": None, "detail": None}
    assert pc.send("k2", "svc_bed_mesh")["detail"] == klipper_service.MSG_RUNNING % "Калибровка резонансов"
    assert service.scripts == []


def test_service_klipper_query_failure_fails_closed(service, fake):
    fake.routes[("GET", "/printer/objects/query")] = OSError("x")
    assert pc.send("k2", "svc_belts")["detail"] == klipper_service.MSG_NO_API and service.scripts == []


def test_service_lost_connection_after_save_config(service, fake):
    fake.routes[("POST", "/printer/gcode/script")] = OSError("reset")
    assert pc.send("k2", "svc_bed_pid")["success"]
    job = klipper_service.status("k2", HOST)["job"]
    assert job["state"] == "failed" and job["detail"] == klipper_service.MSG_LOST


def test_service_status_shows_only_the_lines_since_the_start(service, fake):
    klipper_service._jobs["k2"] = {"action": "svc_bed_mesh", "title": "Калибровка стола", "state": "running",
                                   "started": 1.0, "finished": None, "detail": None, "since": 5.0}
    fake.routes[("GET", "/server/gcode_store")] = {"gcode_store": [
        {"message": "old", "time": 5.0}, {"message": "// probing 1/81", "time": 6.0}, {"message": "// probing 2/81", "time": 7.0}]}
    out = klipper_service.status("k2", HOST)
    assert out["lines"] == ["// probing 1/81", "// probing 2/81"] and out["job"]["state"] == "running"
    assert [a["key"] for a in out["actions"]] == list(pc.SERVICE_ACTIONS)


def test_service_is_creality_only():
    assert set(pc.SERVICE_ACTIONS) <= set(pc.CREALITY_ACTIONS)
    assert not set(pc.SERVICE_ACTIONS) & set(pc.KLIPPER_ACTIONS)
    assert set(klipper_service.ACTIONS) == set(pc.SERVICE_ACTIONS)


def test_service_api_and_command_audit(service, monkeypatch):
    from fastapi.testclient import TestClient
    from app.api import admin
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(admin.router)
    audits = []
    monkeypatch.setattr(admin, "_audit", lambda request, action: audits.append(action))
    with TestClient(app) as client:
        r = client.post("/api/admin/printers/k2/command", json={"action": "svc_shaper"})
        assert r.status_code == 200 and r.json()["status"] == "sent"
        assert audits == ["printer_command:svc_shaper:k2:st=sent"]
        moonraker._cache[HOST] = (moonraker._monotonic(), True)
        s = client.get("/api/admin/printers/k2/service").json()
        assert s["klipper"] is True and s["job"]["action"] == "svc_shaper"
