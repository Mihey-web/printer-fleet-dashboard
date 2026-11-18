"""QA 17.11.2026, part D: files on the printers, the shared file pool, Docker and the docs.

Tests ported from the testers' branches (qa/code-2026-11-17: Н5, Н7; qa/hunt-2026-11-17:
p6 №14, №15, №39, №40) plus the ones written for the findings without a test (Н12, Н15, the
hard-coded upload port and timeout, the docs). Nothing here touches a printer: listings,
live state and HTTP are stand-ins.
"""
import io
import logging
import re
import sqlite3
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import file_pool as fp, job_previews, printer_commands, printer_files as pf, printer_registry
from app.vendor.pybambu import media_sources as ms

ROOT_DIR = Path(__file__).resolve().parents[1]


# --- Н5. Docker: data/ is kept across rebuilds and never baked into the image -------------------

def _data_dirs():
    from app.services import hms_catalog
    return {"job_previews": job_previews.DIRECTORY, "file_pool": fp.DIRECTORY, "hms": Path(hms_catalog.CACHE_DIR)}


def test_docker_keeps_the_job_cards_and_the_file_pool():
    """data/job_previews (previews + job cards the history refers to) and data/file_pool (print
    files) must live on the host: `docker compose up --build` would drop them otherwise."""
    compose = (ROOT_DIR / "docker-compose.yml").read_text(encoding="utf-8")
    mounted = re.findall(r"^\s*-\s*\./([^:\s]+):/app/", compose, re.M)
    missing = [name for name, d in _data_dirs().items()
               if not any(Path(d).relative_to(ROOT_DIR).as_posix().startswith(m.rstrip("/")) for m in mounted)]
    assert missing == [], "mounted: %r" % mounted


def test_docker_image_does_not_bake_in_local_data():
    """.dockerignore: «Local runtime state — must NOT be copied into the image»: data/ holds real
    job names, previews and copies of print files."""
    ignore = [line.strip().rstrip("/") for line in (ROOT_DIR / ".dockerignore").read_text(encoding="utf-8").splitlines()
              if line.strip() and not line.startswith("#")]
    assert "data" in ignore


def test_docker_first_run_creates_the_data_folder():
    """The first-run lines (compose header, README, the docs' Docker page) create data/ next to the
    databases, so the bind mount is the user's folder and not one Docker makes as root."""
    compose = (ROOT_DIR / "docker-compose.yml").read_text(encoding="utf-8")
    readme = (ROOT_DIR / "README.md").read_text(encoding="utf-8")
    for text in (compose, readme,
                 (ROOT_DIR / "docs-site/docs/ru/install/docker.md").read_text(encoding="utf-8"),
                 (ROOT_DIR / "docs-site/docs/en/install/docker.md").read_text(encoding="utf-8")):
        assert "mkdir -p data" in text
    assert re.search(r"^\s*- \./data:/app/data\s*$", compose, re.M)


# --- the docs: where the data lives, how often the cards refresh ----------------------------------

@pytest.mark.parametrize("lang", ["ru", "en"])
def test_docs_list_every_data_folder_and_back_them_up(lang):
    page = (ROOT_DIR / ("docs-site/docs/%s/install/index.md" % lang)).read_text(encoding="utf-8")
    for folder in ("data/job_previews/", "data/file_pool/", "data/hms/"):
        assert "`%s`" % folder in page, folder
    after_table = page[page.index("| `data/hms/`"):]
    backup = after_table[after_table.index("\n\n"):after_table.index("\n## ")]
    assert "`.db`" in backup and "data/job_previews" in backup      # the backup advice names the job cards too


def test_readme_and_docs_say_the_cards_refresh_every_3_s():
    """app.js polls /api/printers every POLL_MS = 3000 (a named constant since the C1 fixes)."""
    js = (ROOT_DIR / "app/web/app.js").read_text(encoding="utf-8")
    assert "var POLL_MS = 3000" in js and "setInterval(loadTick, POLL_MS)" in js
    readme = (ROOT_DIR / "README.md").read_text(encoding="utf-8")
    assert "Cards follow live telemetry every 3 s" in readme
    assert "every 2 s" not in (ROOT_DIR / "docs-site/docs/en/index.md").read_text(encoding="utf-8")
    assert "каждые 2 секунды" not in (ROOT_DIR / "docs-site/docs/ru/index.md").read_text(encoding="utf-8")


# --- Н7. A sync error mid-run ends every printer still waiting as failed --------------------------

def test_file_pool_sync_error_marks_the_printers_failed(monkeypatch, tmp_path):
    """Any exception outside _copy_one / _fetch (here the registry read, «database is locked»)
    must not leave the rows «waiting» with running=False and no word on them."""
    monkeypatch.setattr(fp, "DIRECTORY", tmp_path)
    monkeypatch.setattr(fp, "_job", {})
    plan = {"group": "bambu-X1C", "title": "Bambu X1C", "at": 0,
            "files": {"f1": {"id": "f1", "name": "a.gcode.3mf", "size": 10, "sources": []}},
            "targets": [{"printer": "p1", "label": "C1", "files": ["f1"], "bytes": 10},
                        {"printer": "p2", "label": "C2", "files": ["f1"], "bytes": 10}]}
    fp._job.update({"running": True, "plan": plan, "printers": [
        {"printer": t["printer"], "label": t["label"], "state": "waiting", "files_total": 1, "files_done": 0,
         "bytes_total": 10, "bytes_done": 0, "current": None, "errors": []} for t in plan["targets"]]})

    def locked(pid):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(fp, "_printer", locked)
    fp._run(plan)
    st = fp.status()
    assert st["running"] is False
    assert [(r["printer"], r["state"], r.get("why")) for r in st["printers"]] == [
        ("p1", "failed", fp.WHY_SYNC_BROKEN), ("p2", "failed", fp.WHY_SYNC_BROKEN)]


def test_copy_failure_says_offline_when_the_printer_dropped_and_warns(monkeypatch, tmp_path, caplog):
    """Н7, nearby: a printer that dropped off between the check and the copy is «не на связи», not
    «принтер не принял файл»; a failed copy is a warning in the log, not INFO."""
    monkeypatch.setattr(fp, "_job", {"printers": [{"printer": "c1", "state": "waiting", "files_done": 0,
                                                   "bytes_done": 0, "current": None, "errors": []}]})
    monkeypatch.setattr(pf, "_collector", lambda pid: SimpleNamespace(client=None))
    local = tmp_path / "a.gcode.3mf"
    local.write_bytes(b"x")
    with caplog.at_level(logging.INFO, logger=fp.logger.name):
        fp._copy_one({"id": "c1", "kind": "bambu"}, {"name": "a.gcode.3mf", "size": 1}, str(local))
    assert fp._job["printers"][0]["errors"] == [{"name": "a.gcode.3mf", "why": fp.WHY_OFFLINE}]
    assert [r.levelno for r in caplog.records if "copy to c1" in r.getMessage()] == [logging.WARNING]


# --- Н12. The USB stick's listing failing is said, not turned into «storage closed» -------------

def _sources(monkeypatch, tcp, ftps, result):
    """The printer's file channel (internal storage) and FTPS (the stick); ftps=Exception class: it fails."""
    class Source:
        def __init__(self, proxy):
            pass

        def close(self):
            pass

    class Tcp(Source):
        def list_files(self, kind, ext, paths=None):
            return list(tcp)

        def _connect(self):
            pass

        def _send_ctrl_request(self, cmd, req):
            return 1

        def _read_matching_reply(self, cmd, seq, timeout=None):
            return {"result": result}, b""

    class Ftps(Source):
        def list_files(self, kind, ext, paths=None):
            if isinstance(ftps, type):
                raise ftps("login timed out")
            return list(ftps)
    monkeypatch.setattr(ms, "Tcp6000MediaSource", Tcp)
    monkeypatch.setattr(ms, "Ftps990MediaSource", Ftps)


def _file(name, path, size=10, storage="emmc"):
    return ms.RemoteMediaFile(name, path, size, "model", "x", storage, datetime.fromtimestamp(100, timezone.utc))


def test_h2s_stick_listing_failure_is_said_and_logged_as_a_warning(monkeypatch, caplog):
    _sources(monkeypatch, [], TimeoutError, pf.STORAGE_UNAVAILABLE)
    with caplog.at_level(logging.WARNING, logger=pf.logger.name):
        with pytest.raises(LookupError) as e:
            pf.list_bambu(object(), "H2S")
    assert str(e.value) == pf.MSG_STICK_UNREAD           # not «the printer keeps its storage closed»
    assert [r.levelno for r in caplog.records if "USB" in r.getMessage()] == [logging.WARNING]


def test_p2s_stick_listing_failure_keeps_the_internal_files_and_warns(monkeypatch):
    _sources(monkeypatch, [_file("A.gcode.3mf", "/userdata/model/history/A.gcode.3mf")], TimeoutError, 0)
    monkeypatch.setattr(printer_registry, "get_printer", lambda pid, *a, **k: {"id": pid, "kind": "bambu", "model": "P2S"})
    monkeypatch.setattr(printer_commands, "bambu_model", lambda pid, prn: "P2S")
    monkeypatch.setattr(pf, "_listings", {})
    monkeypatch.setattr(pf, "_store", SimpleNamespace(get_collector=lambda pid: SimpleNamespace(
        client=SimpleNamespace(connected=True))))
    out = pf.list_files("p1")
    assert [f["name"] for f in out["files"]] == ["A.gcode.3mf"] and out["warning"] == pf.MSG_STICK_UNREAD
    # the pool never writes to a stick it could not list: a file of the same name may be on it
    monkeypatch.setattr(pf, "list_files", lambda pid: out)
    assert fp._listing({"id": "p1", "kind": "bambu", "model": "P2S"}) == (None, fp.WHY_NOT_LISTED)


def test_a_stick_that_lists_fine_gives_no_warning(monkeypatch):
    _sources(monkeypatch, [], [_file("U.gcode.3mf", "/U.gcode.3mf", storage="udisk")], pf.STORAGE_UNAVAILABLE)
    assert [e["path"] for e in pf.list_bambu(object(), "H2S")] == ["/U.gcode.3mf"]
    _sources(monkeypatch, [], [], pf.STORAGE_UNAVAILABLE)        # no stick: the closed storage, as before
    with pytest.raises(LookupError) as e:
        pf.list_bambu(object(), "H2S")
    assert str(e.value) == pf.MSG_STORAGE_CLOSED


# --- hunt №14. An H2S with an empty USB stick is a sync target ------------------------------------

H2S = [{"id": "h1", "kind": "bambu", "model": "H2S", "label": "H1", "host": "h1"},
       {"id": "h2", "kind": "bambu", "model": "H2S", "label": "H2", "host": "h2"}]


@pytest.fixture
def h2s_farm(monkeypatch, tmp_path):
    monkeypatch.setattr(printer_registry, "list_printers", lambda include_deleted=True: [dict(p) for p in H2S])
    monkeypatch.setattr(printer_registry, "get_printer", lambda pid, *a, **k: next((dict(p) for p in H2S if p["id"] == pid), None))
    monkeypatch.setattr(printer_commands, "bambu_model", lambda pid, prn: prn.get("model").upper())
    monkeypatch.setattr(pf, "_listings", {})
    monkeypatch.setattr(fp, "DIRECTORY", tmp_path / "pool")
    monkeypatch.setattr(fp, "_plans", {})
    sticks = {"h1": [_file("Have.gcode.3mf", "/Have.gcode.3mf", storage="udisk")], "h2": []}   # h2: a fresh, empty stick
    current = {}

    class Tcp:                       # every H2S: the internal storage answers STORAGE_UNAVAILABLE
        def __init__(self, proxy):
            pass

        def list_files(self, kind, ext, paths=None):
            return []

        def _connect(self):
            pass

        def _send_ctrl_request(self, cmd, req):
            return 1

        def _read_matching_reply(self, cmd, seq, timeout=None):
            return {"result": pf.STORAGE_UNAVAILABLE}, b""

        def close(self):
            pass

    class Ftps(Tcp):
        def list_files(self, kind, ext, paths=None):
            return list(sticks[current["pid"]])

    monkeypatch.setattr(ms, "Tcp6000MediaSource", Tcp)
    monkeypatch.setattr(ms, "Ftps990MediaSource", Ftps)
    real_list_bambu = pf.list_bambu

    def list_bambu(client, model):
        current["pid"] = client.pid
        return real_list_bambu(client, model)
    monkeypatch.setattr(pf, "list_bambu", list_bambu)
    state = {"h1": "normal", "h2": "normal"}                  # home_flag: a stick in both
    monkeypatch.setattr(pf, "_get_store", lambda: SimpleNamespace(get_collector=lambda pid: SimpleNamespace(
        client=SimpleNamespace(connected=True, pid=pid),
        live_device=lambda: SimpleNamespace(home_flag=SimpleNamespace(sdcard_status=state[pid])))))
    monkeypatch.setattr(printer_commands, "_live_view", lambda pid, prn: SimpleNamespace(
        gcode_state="IDLE", status=SimpleNamespace(filament_change=None), raw={}))
    return state


def test_h2s_with_an_empty_stick_is_a_sync_target(h2s_farm):
    assert fp.storage_refusal(H2S[1]) is None          # the panel itself knows the stick is in
    plan = fp.make_plan("bambu-H2S")
    targets = {t["printer"]: [f["name"] for f in t["files"]] for t in plan["targets"]}
    skipped = {s["printer"]: s["why"] for s in plan["skipped"]}
    assert targets.get("h2") == ["Have.gcode.3mf"], "h2 skipped: %r" % skipped.get("h2")


def test_h2s_without_a_stick_still_says_a_stick_is_needed(h2s_farm):
    h2s_farm["h2"] = "missing"
    plan = fp.make_plan("bambu-H2S")
    assert {s["printer"]: s["why"] for s in plan["skipped"]} == {"h2": fp.WHY_NO_STICK}


# --- hunt №15 + №40: the shared farm of X1C / P2S for the plan and the API -------------------------

FARM = [{"id": "c1", "kind": "bambu", "model": "X1C", "label": "C1", "host": "h1"},
        {"id": "c2", "kind": "bambu", "model": "X1C", "label": "C2", "host": "h2"},
        {"id": "p1", "kind": "bambu", "model": "P2S", "label": "P1", "host": "h3"},
        {"id": "k1", "kind": "creality", "model": "k1c", "label": "K1", "host": "127.0.0.1"},
        {"id": "k2", "kind": "creality", "model": "k1c", "label": "K2", "host": "127.0.0.2"}]


def entry(name, size, path=None):
    return {"name": name, "title": name.split(".")[0], "size": size, "mtime": 100, "path": path or "/mnt/sdcard/" + name}


@pytest.fixture
def farm(monkeypatch, tmp_path):
    from app.domain.models import PrinterState
    monkeypatch.setattr(printer_registry, "list_printers", lambda include_deleted=True: [dict(p) for p in FARM])
    monkeypatch.setattr(printer_registry, "get_printer", lambda pid, *a, **k: next((dict(p) for p in FARM if p["id"] == pid), None))
    monkeypatch.setattr(fp, "DIRECTORY", tmp_path / "pool")
    monkeypatch.setattr(fp, "_plans", {})
    monkeypatch.setattr(fp, "_job", {})
    monkeypatch.setattr(fp, "_writing", {}, raising=False)
    listings ={"c1": [entry("Part.gcode.3mf", 10)], "c2": [], "p1": [],
                "k1": [entry("Part.gcode", 7, "/usr/data/printer_data/gcodes/Part.gcode")], "k2": []}
    monkeypatch.setattr(pf, "list_files", lambda pid: {"files": [dict(e) for e in listings[pid]]})
    monkeypatch.setattr(printer_commands, "_live_view", lambda pid, prn: SimpleNamespace(
        gcode_state="IDLE", status=SimpleNamespace(filament_change=None), raw={}))
    monkeypatch.setattr(printer_commands, "_ws_live", lambda pid, prn: (
        SimpleNamespace(state=PrinterState.IDLE), {"device_state": 0}))
    monkeypatch.setattr(pf, "_collector", lambda pid: SimpleNamespace(
        live_device=lambda: SimpleNamespace(home_flag=SimpleNamespace(sdcard_status="normal"))))
    return listings


def test_name_differing_only_in_case_is_a_conflict_not_a_copy(farm):
    """A Bambu SD card / USB stick (FAT32, exFAT) does not tell Part from part: STOR would replace it."""
    fp.store_upload("bambu-X1C", "part.gcode.3mf", [b"x" * 4])     # another file, another size
    plan = fp.make_plan("bambu-X1C")
    by = {t["printer"]: t for t in plan["targets"]}
    sent_c1 = [f["name"] for f in by["c1"]["files"]] if "c1" in by else []
    sent_c2 = [f["name"] for f in by["c2"]["files"]]
    assert "part.gcode.3mf" not in sent_c1 and by["c1"]["conflicts"] == ["part.gcode.3mf"]
    assert len({n.lower() for n in sent_c2}) == len(sent_c2) == 1      # one of the two, never both


def test_creality_names_stay_case_sensitive(farm):
    """A Creality keeps its files on Linux (ext4): Part.gcode and part.gcode are two files."""
    fp.store_upload("creality-k1c", "part.gcode", [b"G28\n"])
    plan = fp.make_plan("creality-k1c")
    by = {t["printer"]: t for t in plan["targets"]}
    assert [f["name"] for f in by["k1"]["files"]] == ["part.gcode"] and by["k1"]["conflicts"] == []


@pytest.fixture
def api(monkeypatch, farm):
    from fastapi.testclient import TestClient
    import config as cfg
    from app.api import admin as api_admin, auth as api_auth, main as api_main
    from app.domain.user_models import UserRole
    from app.services import auth_service
    monkeypatch.setattr(fp, "_run", lambda plan: None)                    # no copying: only who runs what
    monkeypatch.setattr(cfg, "JWT_SECRET", "test-secret", raising=False)
    monkeypatch.setattr(auth_service, "get_user_from_token", lambda *a, **k: ("admin", UserRole.ADMIN))
    monkeypatch.setattr(auth_service, "decode_token", lambda *a, **k: {"iat": 0})
    monkeypatch.setattr(api_auth, "token_subject_active", lambda *a, **k: True)
    audit = []
    monkeypatch.setattr(api_admin, "get_audit_service", lambda: SimpleNamespace(log=lambda u, action, *a, **k: audit.append(action)))
    client = TestClient(api_main.create_app(SimpleNamespace(get_all=lambda: [], get_one=lambda pid: None)))
    client.cookies.set("access_token", "stub")
    return SimpleNamespace(client=client, audit=audit)


def test_sync_of_another_groups_plan_is_refused(api):
    plan = api.client.post("/api/admin/file-pool/bambu-X1C/plan").json()
    assert plan["group"] == "bambu-X1C" and plan["copies"] == 1
    r = api.client.post("/api/admin/file-pool/bambu-P2S/sync", json={"plan": plan["id"]})
    assert r.status_code == 409 and r.json()["detail"] == fp.MSG_PLAN_OTHER_GROUP
    assert api.audit == [] and fp.status() == {"running": False}
    r = api.client.post("/api/admin/file-pool/bambu-X1C/sync", json={"plan": plan["id"]})    # the plan is still there
    assert r.status_code == 200 and r.json()["group"] == "bambu-X1C"
    assert api.audit == ["file_pool_sync:bambu-X1C:files=1,printers=1"]


# --- hunt №39. A project 3MF without plate G-code is not startable --------------------------------

SLICE = b"""<?xml version="1.0" encoding="UTF-8"?>
<config><plate>
  <metadata key="index" value="1"/><metadata key="prediction" value="600"/><metadata key="weight" value="10"/>
  <object identify_id="1" name="Cap.stl" skipped="false"/>
  <filament id="1" type="PLA" color="#FF0000" used_g="10"/>
</plate></config>"""


def _unsliced_detail():
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:          # project 3MF: slice info, no plate gcode
        z.writestr("Metadata/slice_info.config", SLICE)
        z.writestr("3D/3dmodel.model", "<model/>")
    with zipfile.ZipFile(io.BytesIO(out.getvalue())) as z:
        return pf.archive_detail(z)


def test_plates_without_gcode_are_not_startable(monkeypatch):
    detail = _unsliced_detail()
    assert detail["plates"] == []
    e = {"path": "/mnt/sdcard/Cap.3mf", "name": "Cap.3mf", "storage": "external", "size": 9, "mtime": 5}
    monkeypatch.setattr(pf, "_listings", {"b1": {"at": pf.time.monotonic(), "files": {e["path"]: e}}})
    monkeypatch.setattr(pf, "_details", {("b1", e["path"], 9, 5): detail})
    monkeypatch.setattr(printer_commands, "bambu_model", lambda pid, prn: "X1C")
    view = SimpleNamespace(units=[{"index": 0, "slots": [{"type": "PLA", "color": "FF0000", "empty": False}]}])
    with pytest.raises(ValueError) as err:
        pf.plan_bambu("b1", {"model": "X1C"}, {"path": e["path"], "plate": 1, "mapping": [0]}, view)
    assert str(err.value) == pf.MSG_NOT_SLICED


def test_unsliced_file_detail_says_why_it_cannot_start(api, monkeypatch):
    """The «Файлы на принтере» window shows the refusal and keeps «Печать» off."""
    monkeypatch.setattr(pf, "file_detail", lambda pid, path: dict(_unsliced_detail(), entry={"path": path}))
    monkeypatch.setattr(printer_commands, "_live_view", lambda pid, prn: SimpleNamespace(
        gcode_state="IDLE", status=SimpleNamespace(filament_change=None), raw={}, units=[]))
    monkeypatch.setattr(printer_commands, "get_capability", lambda pid: True)
    r = api.client.get("/api/admin/printers/c1/files/detail", params={"path": "/mnt/sdcard/Cap.3mf"})
    assert r.status_code == 200 and r.json()["refusal"] == pf.MSG_NOT_SLICED


# --- Н15. The pool's limits: named, configurable, the free disk, parallel uploads -----------------

def test_limits_come_from_the_environment_then_config(monkeypatch):
    import config as cfg
    monkeypatch.delenv("FORGE_FILE_POOL_MAX_GB", raising=False)
    monkeypatch.delattr(cfg, "FILE_POOL_MAX_GB", raising=False)
    assert fp._env_int("FORGE_FILE_POOL_MAX_GB", 8) == 8
    monkeypatch.setattr(cfg, "FILE_POOL_MAX_GB", 3, raising=False)
    assert fp._env_int("FORGE_FILE_POOL_MAX_GB", 8) == 3
    monkeypatch.setenv("FORGE_FILE_POOL_MAX_GB", "2")
    assert fp._env_int("FORGE_FILE_POOL_MAX_GB", 8) == 2
    for bad in ("abc", "-1", "0"):
        monkeypatch.setenv("FORGE_FILE_POOL_MAX_GB", bad)
        assert fp._env_int("FORGE_FILE_POOL_MAX_GB", 8) == 8
    monkeypatch.setenv("FORGE_FILE_POOL_KEEP_FREE_MB", "0")          # 0 = no disk check: allowed
    assert fp._env_int("FORGE_FILE_POOL_KEEP_FREE_MB", 1024, minimum=0) == 0
    assert fp.MAX_POOL_BYTES == fp.POOL_MAX_GB * 1024 ** 3 and fp.MAX_FILE_BYTES == fp.FILE_MAX_MB * 1024 ** 2
    assert fp.MSG_POOL_FULL == "В общем хранилище нет места (%d ГБ) — удалите ненужные файлы" % fp.POOL_MAX_GB
    assert fp.MSG_TOO_BIG == "Файл больше %d МБ" % fp.FILE_MAX_MB


def test_upload_stops_before_the_disk_runs_out(farm, monkeypatch):
    disk = {"bytes": fp.KEEP_FREE_BYTES + 10}

    def usage(path):                # the disk shrinks with the stored files and every upload in flight
        used = sum(p.stat().st_size for p in fp.DIRECTORY.glob("*/*") if not p.name.startswith(".")) + sum(fp._writing.values())
        return SimpleNamespace(total=1 << 40, used=used, free=disk["bytes"] - used)
    monkeypatch.setattr(fp.shutil, "disk_usage", usage)
    fp.store_upload("bambu-X1C", "fits.gcode.3mf", [b"123456"])
    with pytest.raises(ValueError) as e:
        fp.store_upload("bambu-X1C", "big.gcode.3mf", [b"123", b"4567"])     # 7 bytes, 4 above the reserve
    assert str(e.value) == fp.MSG_DISK_FULL
    disk["bytes"] = fp.KEEP_FREE_BYTES - 1
    with pytest.raises(ValueError) as e:
        fp.store_upload("bambu-X1C", "none.gcode.3mf", [b"1"])
    assert str(e.value) == fp.MSG_DISK_FULL
    assert [f["name"] for f in fp.pi_files("bambu-X1C")] == ["fits.gcode.3mf"] and fp._writing == {}


def test_parallel_uploads_cannot_pass_the_pool_limit_together(farm, monkeypatch):
    monkeypatch.setattr(fp, "MAX_POOL_BYTES", 10)
    second = {}

    def first_chunks():
        yield b"a" * 6
        try:                                      # another upload starts while this one is in flight
            fp.store_upload("bambu-X1C", "b.gcode.3mf", [b"b" * 6])
        except ValueError as e:
            second["error"] = str(e)
        yield b"a" * 2
    fp.store_upload("bambu-X1C", "a.gcode.3mf", first_chunks())
    assert second == {"error": fp.MSG_POOL_FULL}
    assert [f["name"] for f in fp.pi_files("bambu-X1C")] == ["a.gcode.3mf"] and fp._writing == {}


def test_an_upload_left_by_a_crash_is_swept(farm, monkeypatch):
    import os
    import time
    folder = fp.DIRECTORY / "bambu-X1C"
    folder.mkdir(parents=True)
    dead, fresh = folder / ".upload-dead", folder / ".upload-fresh"
    dead.write_bytes(b"x" * 5)                       # the process died mid-upload two hours ago
    os.utime(dead, (time.time() - 7200,) * 2)
    fresh.write_bytes(b"y" * 3)                      # maybe another process's: too new to tell
    monkeypatch.setattr(fp, "MAX_POOL_BYTES", 7)
    fp.store_upload("bambu-X1C", "ok.gcode.3mf", [b"1234"])      # the dead one's 5 bytes no longer count
    assert sorted(p.name for p in folder.iterdir()) == [".upload-fresh", "ok.gcode.3mf"]
    with pytest.raises(ValueError) as e:             # the fresh one still takes room: 3 + 4 + 1 > 7
        fp.store_upload("bambu-X1C", "two.gcode.3mf", [b"1"])
    assert str(e.value) == fp.MSG_POOL_FULL


# --- the Creality upload: named port and timeout ---------------------------------------------------

def test_creality_upload_port_and_timeout_are_named(monkeypatch, tmp_path):
    seen = {}

    class Conn:
        def __init__(self, host, port, timeout):
            seen.update(host=host, port=port, timeout=timeout)

        def __getattr__(self, name):
            raise ConnectionRefusedError(name)

        def close(self):
            pass
    monkeypatch.setattr(fp, "CREALITY_UPLOAD_TIMEOUT_S", 7)
    monkeypatch.setattr(fp.http.client, "HTTPConnection", Conn)
    local = tmp_path / "f.gcode"
    local.write_bytes(b"G28\n")
    with pytest.raises(ConnectionRefusedError):
        fp._upload_creality({"host": "printer"}, "f.gcode", str(local), lambda n: None)
    assert seen == {"host": "printer", "port": fp.CREALITY_WEB_PORT, "timeout": 7} and fp.CREALITY_WEB_PORT == 80
