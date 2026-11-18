"""QA 17.11.2026, phase 2: the «Хардкод» table and Н14 of the code report (qa/code-2026-11-17).

Refactors without a change in behaviour: one data folder (FORGE_DATA_DIR), one SQLite connect
helper, one table of what a Bambu model can do, one table of Creality model names on the server,
one short job name, one list of the K2 service commands, and the nightly backup of the job cards.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP_PY = [p for p in sorted((ROOT / "app").rglob("*.py")) if "vendor" not in p.parts]


def _src(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


# --- 1. One data folder: FORGE_DATA_DIR --------------------------------------------------------

_PATHS_PROBE = r"""
import json, os
from pathlib import Path
from app.services import (ams_store, audit_service, data_dir, file_pool, hms_catalog, job_previews,
                          printer_registry, settings_service, state_store)
from app.api import auth, main as api_main

def root(mod):        # the expression every module used before FORGE_DATA_DIR
    return os.path.dirname(os.path.dirname(os.path.dirname(mod.__file__)))

print(json.dumps({
    "new": {
        "state_store": state_store.DB_PATH, "printer_registry": printer_registry.DB_PATH,
        "settings_service": settings_service.DB_PATH, "api_history": api_main._history_db_path(),
        "ams_store": ams_store.DB_PATH, "api_ams": api_main._ams_db_path(),
        "auth": auth.USERS_DB, "audit": audit_service.USERS_DB,
        "hms": hms_catalog.CACHE_DIR, "job_previews": str(job_previews.DIRECTORY),
        "file_pool": str(file_pool.DIRECTORY),
    },
    "old": {
        "state_store": os.path.join(root(state_store), "printer_history.db"),
        "printer_registry": os.path.join(root(printer_registry), "printer_history.db"),
        "settings_service": os.path.join(root(settings_service), "printer_history.db"),
        "api_history": os.path.join(root(api_main), "printer_history.db"),
        "ams_store": os.path.join(root(ams_store), "ams_history.db"),
        "api_ams": os.path.join(root(api_main), "ams_history.db"),
        "auth": os.path.join(root(auth), "users.db"),
        "audit": os.path.join(root(audit_service), "users.db"),
        "hms": os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(hms_catalog.__file__)))),
                            "data", "hms"),
        "job_previews": str(Path(job_previews.__file__).resolve().parents[2] / "data" / "job_previews"),
        "file_pool": str(Path(file_pool.__file__).resolve().parents[2] / "data" / "file_pool"),
    },
}))
"""


def _probe_paths(data_dir_value=None):
    env = {k: v for k, v in os.environ.items() if k != "FORGE_DATA_DIR"}
    if data_dir_value is not None:
        env["FORGE_DATA_DIR"] = data_dir_value
    out = subprocess.run([sys.executable, "-c", _PATHS_PROBE], cwd=str(ROOT), env=env,
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_data_paths_default_to_the_old_ones_byte_for_byte():
    """FORGE_DATA_DIR unset: every database and data folder is exactly the path it was (on the Pi
    nothing moves)."""
    got = _probe_paths()
    assert got["new"] == got["old"]


def test_every_module_follows_forge_data_dir(tmp_path):
    base = str(tmp_path / "farm data")
    got = _probe_paths(base)["new"]
    expected = {
        "state_store": "printer_history.db", "printer_registry": "printer_history.db",
        "settings_service": "printer_history.db", "api_history": "printer_history.db",
        "ams_store": "ams_history.db", "api_ams": "ams_history.db",
        "auth": "users.db", "audit": "users.db",
        "hms": os.path.join("data", "hms"), "job_previews": os.path.join("data", "job_previews"),
        "file_pool": os.path.join("data", "file_pool"),
    }
    real = os.path.realpath(base)
    for name, rel in expected.items():
        assert os.path.realpath(got[name]) == os.path.join(real, rel), name


def test_data_dir_reads_env_then_config_then_the_project_root(monkeypatch, tmp_path):
    from app.services import data_dir
    import config
    monkeypatch.delenv("FORGE_DATA_DIR", raising=False)
    monkeypatch.delattr(config, "DATA_DIR", raising=False)
    assert data_dir.resolve() == data_dir.PROJECT_ROOT
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path / "from-config"), raising=False)
    assert data_dir.resolve() == str(tmp_path / "from-config")
    monkeypatch.setenv("FORGE_DATA_DIR", str(tmp_path / "from-env"))
    assert data_dir.resolve() == str(tmp_path / "from-env")
    monkeypatch.setenv("FORGE_DATA_DIR", "  ")                 # blank = not set
    assert data_dir.resolve() == str(tmp_path / "from-config")
    monkeypatch.setenv("FORGE_DATA_DIR", "farm")               # relative: from the project root
    assert data_dir.resolve() == os.path.join(data_dir.PROJECT_ROOT, "farm")


def test_each_database_file_name_is_written_once():
    """users.db (and the other two) is named in app/services/data_dir.py only; every module takes
    the path from there."""
    for name in ("printer_history.db", "users.db", "ams_history.db"):
        literal = re.compile(r"""["']%s["']""" % re.escape(name))
        where = [p.relative_to(ROOT).as_posix() for p in APP_PY if literal.search(p.read_text(encoding="utf-8"))]
        assert where == ["app/services/data_dir.py"], (name, where)
    root_expr = re.compile(r"dirname\(os\.path\.dirname\(os\.path\.dirname\((?:os\.path\.abspath\()?__file__")
    data_expr = re.compile(r"""parents\[2\]\s*/\s*["']data["']""")
    for p in APP_PY:
        text = p.read_text(encoding="utf-8")
        if p.name in ("data_dir.py", "i18n.py"):    # i18n.py: the root for app/web, not data
            continue
        if root_expr.search(text):
            assert "repo_root" in text and "git" in text, p        # api/main.py: the git commit, not data
        assert not data_expr.search(text), p


def test_env_example_documents_forge_data_dir():
    example = _src(".env.example")
    assert re.search(r"^# FORGE_DATA_DIR=$", example, re.M)
    block = example[:example.index("# FORGE_DATA_DIR=")]
    block = block[block.rindex("\n\n"):]
    assert "project root" in block and "--src" in block


def test_docker_compose_and_readme_still_match_the_default_folder():
    """FORGE_DATA_DIR unset in Docker: the app writes at /app, which the compose file mounts."""
    compose = _src("docker-compose.yml")
    assert "FORGE_DATA_DIR" not in compose
    for name in ("printer_history.db", "users.db", "ams_history.db"):
        assert re.search(r"^\s*- \./%s:/app/%s\s*$" % (re.escape(name), re.escape(name)), compose, re.M), name
    assert "WORKDIR /app" in _src("Dockerfile")
    readme = _src("README.md")
    assert "| `FORGE_DATA_DIR` | the project root |" in readme


# --- 2. One SQLite connect helper -----------------------------------------------------------------

def test_the_app_opens_sqlite_only_through_the_helper():
    """sqlite3.connect(..., timeout=10) was written out in ~35 places; now only sqlite_db.connect
    calls it, with the one shared lock timeout."""
    direct = [p.relative_to(ROOT).as_posix() for p in APP_PY
              if "sqlite3.connect(" in p.read_text(encoding="utf-8")]
    assert direct == ["app/services/sqlite_db.py"]
    calls = [line for p in APP_PY for line in p.read_text(encoding="utf-8").splitlines()
             if "sqlite_db.connect(" in line]
    assert len(calls) >= 30
    assert not [c for c in calls if "timeout=" in c]


def test_the_connection_modes_stay_where_they_were():
    """check_same_thread=False on the long-lived connections, uri=True on the read-only / rw ones."""
    long_lived = {"app/services/state_store.py": "DB_PATH", "app/services/ams_store.py": "DB_PATH",
                  "app/services/notification_tracker.py": "db_path"}
    for rel, arg in long_lived.items():
        assert "sqlite_db.connect(%s, check_same_thread=False)" % arg in _src(rel), rel
    uri = {"app/services/job_journal.py": 2, "app/services/job_queries.py": 1, "app/services/temp_history.py": 1}
    for rel, count in uri.items():
        assert len(re.findall(r"sqlite_db\.connect\([^\n]*, uri=True\)", _src(rel))) == count, rel


def test_the_helper_passes_the_shared_timeout_and_every_other_argument(monkeypatch):
    from app.services import sqlite_db
    seen = []
    real = sqlite_db.sqlite3.connect
    monkeypatch.setattr(sqlite_db.sqlite3, "connect", lambda db, **kw: (seen.append((db, kw)), real(":memory:"))[1])
    sqlite_db.connect("a.db").close()
    sqlite_db.connect("b.db", check_same_thread=False).close()
    sqlite_db.connect("file:c.db?mode=ro", uri=True).close()
    sqlite_db.connect("d.db", timeout=60).close()
    assert sqlite_db.BUSY_TIMEOUT_S == 10
    assert seen == [("a.db", {"timeout": 10}), ("b.db", {"check_same_thread": False, "timeout": 10}),
                    ("file:c.db?mode=ro", {"uri": True, "timeout": 10}), ("d.db", {"timeout": 60})]


def test_a_helper_connection_waits_for_a_writer_instead_of_failing(tmp_path):
    import sqlite3
    import threading
    from app.services import sqlite_db
    db = str(tmp_path / "lock.db")
    holder = sqlite3.connect(db, check_same_thread=False)
    holder.execute("CREATE TABLE t (x)")
    holder.commit()
    holder.execute("BEGIN EXCLUSIVE")
    release = threading.Timer(0.3, holder.rollback)
    release.start()
    conn = sqlite_db.connect(db)
    try:
        conn.execute("INSERT INTO t VALUES (1)")     # waits until the holder lets go
        conn.commit()
    finally:
        release.join()
        conn.close()
        holder.close()


# --- 3. One table of what a Bambu model can do -------------------------------------------------

# What the code answered for every known model BEFORE the table (main 816ecf2, recorded by calling
# the old functions and the old inline checks), one column per check:
#   sensor  normalizer._has_chamber_sensor (the lib cannot answer)  heater  has_chamber_heater
#   light2  has_second_light   tmax  _swap_temp_max   ftps  printer_files._uses_ftps
#   stick   printer_files._usb_over_ftps   read  file_pool.can_read
#   dual    printer_files.start_print's dual-nozzle refusal   ramjob  print_material.read_bambu's
#           file-channel read of the running job
_T, _F = True, False
BAMBU_BEFORE = {
    #          sensor heater light2 tmax  ftps stick read dual ramjob
    "X1":     (_T,    _F,    _F,    300,  _F,  _F,   _T,  _F,  _T),
    "X1C":    (_T,    _F,    _F,    300,  _F,  _F,   _T,  _F,  _T),
    "X1E":    (_T,    _F,    _F,    300,  _F,  _F,   _T,  _F,  _T),
    "P1P":    (_F,    _F,    _F,    300,  _T,  _F,   _T,  _F,  _F),
    "P1S":    (_F,    _F,    _F,    300,  _T,  _F,   _T,  _F,  _F),
    "P2S":    (_T,    _F,    _F,    300,  _F,  _T,   _F,  _F,  _T),
    "A1":     (_F,    _F,    _F,    300,  _T,  _F,   _T,  _F,  _F),
    "A1MINI": (_F,    _F,    _F,    300,  _T,  _F,   _T,  _F,  _F),
    "A1M":    (_F,    _F,    _F,    300,  _T,  _F,   _T,  _F,  _F),
    "A2L":    (_F,    _F,    _F,    300,  _F,  _F,   _F,  _F,  _F),
    "H2C":    (_T,    _T,    _T,    350,  _F,  _T,   _F,  _T,  _T),
    "H2D":    (_T,    _T,    _T,    350,  _F,  _T,   _F,  _T,  _T),
    "H2DPRO": (_T,    _T,    _T,    350,  _F,  _T,   _F,  _T,  _T),
    "H2S":    (_T,    _T,    _T,    350,  _F,  _T,   _F,  _F,  _T),
    "X2D":    (_T,    _F,    _T,    300,  _F,  _T,   _F,  _T,  _T),
    "x1c":    (_T,    _F,    _F,    300,  _F,  _F,   _T,  _F,  _T),
    "h2d":    (_T,    _T,    _T,    350,  _F,  _T,   _F,  _T,  _T),
    "p1s":    (_F,    _F,    _F,    300,  _T,  _F,   _T,  _F,  _F),
    "":       (_F,    _F,    _F,    300,  _F,  _F,   _F,  _F,  _F),
    None:     (_F,    _F,    _F,    300,  _F,  _F,   _F,  _F,  _F),
    "K1C":    (_F,    _F,    _F,    300,  _F,  _F,   _F,  _F,  _F),
    "UNKNOWN": (_F,   _F,    _F,    300,  _F,  _F,   _F,  _F,  _F),
}


def _bambu_answers(model):
    from types import SimpleNamespace
    from app.services import bambu_models, file_pool, normalizer, printer_commands, printer_files
    up = str(model or "").upper()          # printer_files' callers pass the upper-cased model
    return (normalizer._has_chamber_sensor(SimpleNamespace(), model),
            printer_commands.has_chamber_heater(model),
            printer_commands.has_second_light(model),
            printer_commands._swap_temp_max(model),
            printer_files._uses_ftps(up),
            printer_files._usb_over_ftps(up),
            file_pool.can_read({"kind": "bambu", "model": model}),
            bambu_models.has(up, "dual_nozzle"),
            bambu_models.has(up, "current_job_over_file_channel"))


@pytest.mark.parametrize("model", list(BAMBU_BEFORE), ids=lambda m: repr(m))
def test_every_known_bambu_model_gets_the_same_answers_as_before(model):
    assert _bambu_answers(model) == BAMBU_BEFORE[model]


def test_the_ui_models_are_all_in_the_table():
    """Every model the printer form offers (and the lib knows) is pinned above."""
    from app.vendor.pybambu.const import Printers
    offered = set(re.findall(r'value="([A-Z0-9]+)"', re.search(r'<select id="prnModelBambu".*?</select>',
                                                              _src("app/web/index.html"), re.S).group(0)))
    assert offered and offered <= set(BAMBU_BEFORE)
    assert {p.value for p in Printers} <= set(BAMBU_BEFORE)


def test_no_model_prefix_check_is_left_outside_the_table():
    from app.services import bambu_models
    prefixes = {p for ps in bambu_models.CAPABILITIES.values() for p in ps}
    check = re.compile(r"""startswith\(\(?\s*["'](%s)["']""" % "|".join(sorted(prefixes)))
    for p in APP_PY:
        if p.name == "bambu_models.py":
            continue
        assert not check.search(p.read_text(encoding="utf-8")), p
    for rel, cap in (("app/services/normalizer.py", "chamber_sensor"), ("app/services/printer_commands.py", "chamber_heater"),
                     ("app/services/printer_commands.py", "second_light"), ("app/services/printer_commands.py", "hotend_350"),
                     ("app/services/printer_files.py", "files_over_ftps"), ("app/services/printer_files.py", "usb_over_ftps"),
                     ("app/services/printer_files.py", "dual_nozzle"),
                     ("app/services/print_material.py", "current_job_over_file_channel"),
                     ("app/services/file_pool.py", "reads_every_file")):
        assert 'bambu_models.has(' in _src(rel) and '"%s"' % cap in _src(rel), (rel, cap)


# --- 4. One table of Creality model names, on the server -----------------------------------------

# app.js CARD_MODEL_NAMES as it was (main 816ecf2): the server's table keeps every name.
OLD_CARD_NAMES = {
    "k1": "K1", "k1max": "K1 Max", "k1c": "K1C", "k1se": "K1 SE", "k2": "K2", "k2plus": "K2 Plus",
    "k2pro": "K2 Pro", "k2se": "K2 SE", "ender3v3": "Ender-3 V3", "ender3v3ke": "Ender-3 V3 KE",
    "ender3v3plus": "Ender-3 V3 Plus", "ender5max": "Ender-5 Max", "cr10se": "CR-10 SE", "hi": "Hi",
    "sparkxi7": "SPARKX i7",
}


def test_the_server_table_has_every_name_the_card_had_in_the_same_order():
    from app.services import creality_models
    assert list(creality_models.NAMES.items()) == list(OLD_CARD_NAMES.items())


# ported from qa/code-2026-11-17 (tests/test_qa_code_2026_11_17.py, Н14)
@pytest.mark.parametrize("model,title", [("k2pro", "Creality K2 Pro"), ("k1se", "Creality K1 SE"),
                                         ("ender3v3", "Creality Ender-3 V3")])
def test_file_pool_names_a_model_like_the_printer_card(model, title):
    """file_pool.group_title knew 5 Creality names, app.js CARD_MODEL_NAMES 16: the same printer was
    «K2 Pro» on its card and «Creality k2pro» in Админ → Парк → общий пул."""
    from app.services import file_pool
    assert file_pool.group_title("creality-" + model) == title


def test_the_pool_titles_every_group_as_before_or_by_the_table():
    from app.services import file_pool
    for code, name in OLD_CARD_NAMES.items():
        assert file_pool.group_title("creality-" + code) == "Creality " + name
    assert file_pool.group_title("creality-myk9") == "Creality myk9"      # an unknown model: as stored
    assert file_pool.group_title("bambu-X1C") == "Bambu X1C"
    assert file_pool.group_title("bambu-H2D") == "Bambu H2D"


def test_display_name_and_code_for():
    from app.services import creality_models as cm
    assert [cm.display_name(m) for m in ("k1c", "K1C", " ender5max ", "k2plus", "X1C", "", None, 5)] == [
        "K1C", "K1C", "Ender-5 Max", "K2 Plus", None, None, None, None]
    # a model stored as its name (typed past the form) reads as that name too
    assert [cm.display_name(m) for m in ("K1 Max", "ender-5 max", "K2 PLUS", "Ender 3")] == [
        "K1 Max", "Ender-5 Max", "K2 Plus", None]
    # the printer form sends the name; a known one is stored as its code (the form did that before)
    assert [cm.code_for(n) for n in ("K2 Plus", "ender-5 max", "K1C", "k1max", "Ender-3 V3 KE")] == [
        "k2plus", "ender5max", "k1c", "k1max", "ender3v3ke"]
    assert cm.code_for("My K9") == "My K9" and cm.code_for(None) is None


def _api_client(monkeypatch, statuses, registry):
    import config as cfg
    from fastapi.testclient import TestClient
    from app.api import auth as api_auth, main as api_main
    from app.domain.user_models import UserRole
    from app.services import auth_service, settings_service

    class Store:
        def get_all(self):
            return list(statuses)

        def get_one(self, pid):
            return next((s for s in statuses if s.id == pid), None)

    monkeypatch.setattr(cfg, "JWT_SECRET", "test-secret", raising=False)
    monkeypatch.setattr(auth_service, "get_user_from_token", lambda *a, **k: ("viewer", UserRole.VIEWER))
    monkeypatch.setattr(auth_service, "decode_token", lambda *a, **k: {"iat": 0})
    monkeypatch.setattr(api_auth, "token_subject_active", lambda *a, **k: True)
    monkeypatch.setattr("app.services.printer_registry.list_printers", lambda *a, **k: [dict(r) for r in registry])
    monkeypatch.setattr(settings_service, "get", lambda key, **k: {})
    client = TestClient(api_main.create_app(Store()))
    client.cookies.set("access_token", "stub")
    return client


def test_api_printers_gives_the_model_name(monkeypatch):
    from app.domain.models import PrinterKind, PrinterState, PrinterStatus
    statuses = [PrinterStatus(id="c1", label="E5", kind=PrinterKind.CREALITY, online=True, state=PrinterState.IDLE,
                              device_type="ender5max"),
                PrinterStatus(id="c2", label="K", kind=PrinterKind.CREALITY, online=True, state=PrinterState.IDLE,
                              device_type="k2plus"),
                PrinterStatus(id="b1", label="P", kind=PrinterKind.BAMBU, online=True, state=PrinterState.IDLE,
                              device_type="X1C")]
    registry = [{"id": "c1", "label": "E5", "kind": "creality", "model": "ender5max"},
                {"id": "c2", "label": "K", "kind": "creality", "model": "k2plus"},
                {"id": "b1", "label": "P", "kind": "bambu", "model": "X1C"}]
    r = _api_client(monkeypatch, statuses, registry).get("/api/printers")
    assert r.status_code == 200
    got = {p["id"]: p["model_name"] for p in r.json()}
    assert got == {"c1": "Ender-5 Max", "c2": "K2 Plus", "b1": None}


def test_the_park_rows_and_the_form_list_come_from_the_table(monkeypatch):
    from app.api import admin
    from app.services import creality_models, printer_registry
    monkeypatch.setattr(printer_registry, "pending_state", lambda row: None)
    rows = [admin._printer_with_pending({"id": "k", "kind": "creality", "model": m}) for m in ("k1c", "ender5max", "Custom")]
    assert [r["model_name"] for r in rows] == ["K1C", "Ender-5 Max", None]
    assert [r["model"] for r in rows] == ["k1c", "ender5max", "Custom"]          # the code stays the data
    assert admin.printer_models() == {"creality": creality_models.NAMES}


def test_the_form_s_name_is_stored_as_its_code():
    from app.api import admin
    for kind, sent, stored in (("creality", "K2 Plus", "k2plus"), ("creality", "ender-5 max", "ender5max"),
                               ("creality", "My K9", "My K9"), ("bambu", "K2 Plus", "K2 Plus"),
                               ("klipper", "K1C", "K1C")):
        data = {"model": sent}
        admin._creality_model_code(kind, data)
        assert data["model"] == stored, (kind, sent)
    src = _src("app/api/admin.py")
    assert "_creality_model_code(body.kind, data)\n    row = printer_registry.create_printer(data)" in src.replace("\r\n", "\n")
    assert "_creality_model_code(existing[\"kind\"], data)\n    row = printer_registry.update_printer(" in src.replace("\r\n", "\n")


def test_job_rows_and_stats_give_the_model_name(tmp_path):
    import sqlite3
    from app.services import job_journal, job_queries
    db = str(tmp_path / "ph.db")
    conn = sqlite3.connect(db)
    job_journal.init_db(conn)
    for i, (pid, kind, model) in enumerate((("c1", "creality", "k1c"), ("b1", "bambu", "P2S"), ("c2", "creality", "zz9"))):
        job_journal.insert_job(conn, {"printer_id": pid, "kind": kind, "model": model, "job_name": "J%d" % i,
                                      "started_at": 1000.0 + i, "ended_at": 1100.0 + i, "result": "finished",
                                      "start_exact": 1, "end_exact": 1, "last_seen": 1100.0 + i,
                                      "actual_sec": 100, "source": "live"}, 1100.0 + i)
    conn.commit()
    conn.close()
    rows = job_queries.list_jobs(db, 0, 10 ** 10)["rows"]
    assert {r["printer_id"]: (r["model"], r["model_name"]) for r in rows} == {
        "c1": ("k1c", "K1C"), "b1": ("P2S", None), "c2": ("zz9", None)}
    assert tuple(rows[0]) == job_queries._ROW_FIELDS
    stats = job_queries.job_stats(db, 0, 10 ** 10, include_maint=True)["printers"]
    assert {s["printer_id"]: s["model_name"] for s in stats} == {"c1": "K1C", "b1": None, "c2": None}


def test_app_js_keeps_no_copy_of_the_names_and_reads_the_server_s():
    js = _src("app/web/app.js")
    assert "CARD_MODEL_NAMES" not in js
    for name in ("K1 Max", "K2 Plus", "Ender-5 Max", "Ender-3 V3 KE", "SPARKX i7"):
        assert '"%s"' % name not in js and "'%s'" % name not in js, name
    assert "var text = (p && p.model_name) ||" in js                                   # the card's head
    assert "var model = p.model_name || p.model || p.device_type;" in js              # the job card
    assert "escHtml(p.model_name || p.model || '—')" in js                            # «Парк»
    assert "apiFetch('/api/admin/printer-models')" in js                              # the form's list
    assert "payload.model = name;" in js
    # the bump that shipped this (175); a later change bumps it again
    m = re.search(r'src="/static/app\.js\?v=(\d+)"', _src("app/web/index.html"))
    assert m and int(m.group(1)) >= 175


def test_the_job_card_and_the_history_row_show_the_name():
    import test_web_jobs_ui as jobs_ui
    live = {"id": "c1", "label": "E5", "kind": "creality", "device_type": "ender5max", "model_name": "Ender-5 Max",
            "preview_key": "a" * 64, "job_name": "Part.gcode"}
    assert jobs_ui.call("previewData", live, False)["printer"] == "E5 · Ender-5 Max"
    old = dict(live, model_name=None)                         # an unknown model: as reported, as before
    assert jobs_ui.call("previewData", old, False)["printer"] == "E5 · ender5max"
    job = {"id": 1, "printer_id": "c1", "label": "K", "kind": "creality", "model": "k1c", "model_name": "K1C",
           "job_name": "Part.gcode", "started_at": 1000, "result": "finished", "ended_at": 2000}
    assert jobs_ui.call("previewData", job, True)["printer"] == "K · K1C"
    html = jobs_ui.run("jobRowHtml(%s, null, {now: 3000})" % json.dumps(job))
    assert '<span class="job-model">K1C</span>' in html and "k1c" not in html


# --- 4b. Every other place people read a Creality model: notifications, group dialogs, AMS, CSV --------

def _creality_row(model, **kw):
    """A /api/printers row of a Creality as the API sends it (model_name from the server's table)."""
    import test_web_nf_ui as nf_ui
    from app.services import creality_models
    row = nf_ui.prn(nf_ui.PE, pid="c1", label="Цех 2", kind="creality", model=model, **kw)
    row["model_name"] = creality_models.display_name(model)
    return row


def test_the_notifications_name_a_creality_model_like_the_card():
    import test_web_nf_ui as nf_ui
    k2, e5 = _creality_row("k2plus"), _creality_row("ender5max")
    bambu = nf_ui.prn(nf_ui.PE, label="P3")
    klipper = dict(nf_ui.prn(nf_ui.PE, kind="klipper", model=None), model_name=None)
    assert nf_ui.run("[%s].map(nfModel)" % ", ".join(nf_ui.js(p) for p in (k2, e5, bambu, klipper))) == [
        "K2 Plus", "Ender-5 Max", "P2S", "KLIPPER"]
    # the printer's popover head and the farm list's group head
    html = nf_ui.pop(e5)
    assert '<span class="bd-model">Ender-5 Max</span>' in html and "ENDER5MAX" not in html
    farm = nf_ui.run("nfPopFarmHtml(%s, {}, {}, Object.assign(%s, {status: %s, lines: {}, now: 0}))"
                     % (nf_ui.js([k2]), nf_ui.js(nf_ui.ADMIN), nf_ui.STATUS))
    assert '<span class="bd-model">K2 Plus</span>' in farm and "K2PLUS" not in farm
    # the «Сбросить ошибки» dialog's rows
    cancel = dict(_creality_row("k2plus"), notifications=[nf_ui.CANCEL], state="idle")
    dlg = nf_ui.run("nfResetDialog(%s)" % nf_ui.js([cancel]))
    assert [r["model"] for r in dlg["go"] + dlg["skip"]] == ["K2 Plus"]


def test_the_group_dialog_names_a_creality_model_like_the_card():
    js = _src("app/web/app.js")
    body = js[js.index("function openBulkDialog(action) {"):]
    body = body[:body.index("\n      }\n")]
    assert "function model(p) { return nfModel(p); }" in body
    assert "toUpperCase" not in body                                     # no second way to spell a model
    # the dialogs' and the notifications' model line no longer capitalises «K2 Plus» into «K2 PLUS»
    css = _src("app/web/styles.css")
    rule = re.sub(r"/\*.*?\*/", "", re.search(r"\n    \.bd-model \{(.*?)\}", css, re.S).group(1), flags=re.S)
    assert not re.search(r"text-transform\s*:", rule) and "white-space: nowrap" in rule
    m = re.search(r'href="/static/styles\.css\?v=(\d+)"', _src("app/web/index.html"))
    assert m and int(m.group(1)) >= 133                    # 133 shipped this; a later change bumps it again


def test_the_ams_card_names_a_creality_printer_like_its_card(monkeypatch):
    from app.domain.models import PrinterKind, PrinterState, PrinterStatus
    cfs = [{"id": 0, "humidity": 30, "temp": 25.0, "external": False, "slots": [
        {"index": 0, "type": "PLA", "color": "#FFFFFF", "selected": True}]}]
    s = PrinterStatus(id="c1", label="K2", kind=PrinterKind.CREALITY, online=True, state=PrinterState.IDLE,
                      device_type="k2plus")
    s.cfs = cfs
    registry = [{"id": "c1", "label": "K2", "kind": "creality", "model": "k2plus"}]
    units = _api_client(monkeypatch, [s], registry).get("/api/ams/current").json()
    assert units and {(u["device_type"], u["model_name"]) for u in units} == {("k2plus", "K2 Plus")}
    js = _src("app/web/app.js")
    assert "(u.model_name || u.device_type ? escHtml(u.model_name || u.device_type) : 'AMS')" in js
    assert "escHtml(u.device_type) : 'AMS'" not in js


def test_the_csv_model_column_is_the_name_people_read(tmp_path):
    import csv
    import io
    import sqlite3
    from app.services import job_journal, job_queries
    db = str(tmp_path / "ph.db")
    conn = sqlite3.connect(db)
    job_journal.init_db(conn)
    for i, (pid, kind, model) in enumerate((("c1", "creality", "k2plus"), ("c2", "creality", "ender5max"),
                                            ("b1", "bambu", "P2S"), ("c3", "creality", "Custom 9"))):
        job_journal.insert_job(conn, {"printer_id": pid, "kind": kind, "model": model, "job_name": "J%d" % i,
                                      "started_at": 1000.0 + i, "ended_at": 1100.0 + i, "result": "finished",
                                      "start_exact": 1, "end_exact": 1, "last_seen": 1100.0 + i,
                                      "actual_sec": 100, "source": "live"}, 1100.0 + i)
    conn.commit()
    conn.close()
    text = job_queries.jobs_csv(db, 0, 10 ** 10, labels={"c1": "K", "c2": "E", "b1": "P", "c3": "X"})
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿")), delimiter=";"))
    assert rows[0][:2] == ["Принтер", "Модель"] and len(rows[0]) == 15     # the same 15 columns, for people
    assert sorted((r[0], r[1]) for r in rows[1:]) == [("E", "Ender-5 Max"), ("K", "K2 Plus"), ("P", "P2S"),
                                                     ("X", "Custom 9")]


# --- 5. The short job name: project_summary.short_name == app.js jobShortName -------------------
# Both kept (the browser shortens names the server never sees, e.g. a live card's job), pinned
# together here on every combination of folder, part name, Creality suffix and extension.

_DIRS = ("", "/data/", "/mnt/UDISK/printer_data/gcodes/", "C:\\Users\\me\\", "cache/sub\\")
_STEMS = ("Шестерня 20 мм", "Part", "Колесо.STL", "Корпус.step", "a.b.c", "Box_v2", "plate_1", "Деталь (копия) #3",
          "X.OBJ", "model.3mf", "   space   ", "UPPER.Stl")
_SUFFIXES = ("", "_PETG_8h55m7s", "_PLA+_1d2h3m4s", "_PETG-CF_0h5m0s", "_PLA_8h55m", "_ABS_12h0m1s_extra")
_EXTS = ("", ".gcode", ".GCODE", ".gco", ".3mf", ".gcode.3mf", ".GCODE.3MF", ".gcode.gcode")
_SPECIAL = ["", None, "Metadata/plate_1.gcode", "/sdcard/", ".gcode", "_PETG_8h55m7s.gcode", ".stl.gcode",
            "a/b/c/", "Шестерня.gcode.3mf", "Колесо.STL_PETG_8h55m7s.gcode"]


def _short_name_examples():
    out = [d + s + x + e for d in _DIRS for s in _STEMS for x in _SUFFIXES for e in _EXTS]
    return out + _SPECIAL


def test_short_job_name_is_the_same_on_the_server_and_in_the_browser():
    import test_web_jobs_ui as jobs_ui
    from app.services import project_summary
    names = _short_name_examples()
    assert len(names) > 2000
    js = jobs_ui._node(jobs_ui._function("jobShortName") +
                       "\nconsole.log(JSON.stringify(%s.map(jobShortName)));" % json.dumps(names, ensure_ascii=False))
    differ = []
    for name, got in zip(names, js):
        py = project_summary.short_name(name)
        if py:
            ok = got == py
        else:   # nothing left: the server says "" (its callers fall back), the browser keeps the base name
            base = str(name or "").replace("\\", "/").rsplit("/", 1)[-1]
            base = re.sub(r"\.(gcode|gco|3mf)$", "", re.sub(r"\.gcode\.3mf$", "", base, flags=re.I), flags=re.I)
            ok = got == base
        if not ok:
            differ.append((name, py, got))
    assert differ == []
    # the two the docstrings promise
    assert project_summary.short_name("Шестерня.gcode.3mf") == "Шестерня"
    assert project_summary.short_name("Колесо.STL_PETG_8h55m7s.gcode") == "Колесо"


# --- 6. The K2 «Сервис» titles: only klipper_service.ACTIONS ------------------------------------

def test_app_js_has_no_copy_of_the_service_titles():
    from app.services import klipper_service
    js = _src("app/web/app.js")
    assert not re.search(r"\bsvc_[a-z_]+\s*:", js)                    # no table keyed by the actions
    cmd = re.search(r"var ACT_CMD = \{.*?\n      \};", js, re.S).group(0)     # the journal's command words
    for key, (_script, title, _about) in klipper_service.ACTIONS.items():
        assert title not in cmd and key not in js, (key, title)
    for rel in ("app/web/printer-service.js", "app/web/app.js"):
        src = _src(rel)
        assert all(about not in src for _s, _t, about in klipper_service.ACTIONS.values()), rel


def test_the_journal_gets_the_titles_with_its_rows(tmp_path, monkeypatch):
    import config as cfg
    from fastapi.testclient import TestClient
    from app.api import auth as api_auth, main as api_main
    from app.domain.user_models import UserRole
    from app.services import audit_service, auth_service, klipper_service
    monkeypatch.setattr(audit_service, "USERS_DB", str(tmp_path / "users.db"))
    monkeypatch.setattr(audit_service, "_audit", None)
    audit_service.get_audit_service().log("admin", "printer_command:svc_shaper:k2:st=sent")
    monkeypatch.setattr(cfg, "JWT_SECRET", "test-secret", raising=False)
    monkeypatch.setattr(auth_service, "get_user_from_token", lambda *a, **k: ("admin", UserRole.ADMIN))
    monkeypatch.setattr(auth_service, "decode_token", lambda *a, **k: {"iat": 0})
    monkeypatch.setattr(api_auth, "token_subject_active", lambda *a, **k: True)

    class Store:
        def get_all(self):
            return []

    client = TestClient(api_main.create_app(Store()))
    client.cookies.set("access_token", "stub")
    page = client.get("/api/admin/audit?limit=10").json()
    assert [r["action"] for r in page["rows"]] == ["printer_command:svc_shaper:k2:st=sent"]
    assert page["service_titles"] == {k: a[1] for k, a in klipper_service.ACTIONS.items()}
    assert list(page["service_titles"]) == list(klipper_service.ACTIONS)


def test_the_journal_names_a_service_command_by_the_server_s_title():
    import test_web_acts_ui as acts_ui
    from app.services import klipper_service
    titles = {k: a[1] for k, a in klipper_service.ACTIONS.items()}
    row = "printer_command:svc_shaper:k2:st=sent"
    before, after, bulk = acts_ui.run(
        "[actText(%s).what, (actSetServiceTitles(%s), actText(%s).what), actText('printer_bulk:pause:b1:confirmed=2').what]"
        % (json.dumps(row), json.dumps(titles, ensure_ascii=False), json.dumps(row)))
    assert before.startswith("svc_shaper")                      # before the first page: the key, as for any unknown
    assert after.startswith("Калибровка резонансов") and "отправлено" in after
    assert bulk.startswith("Пауза — всем")                       # the app's own commands: unchanged
    # an HTML-looking title from the server is text, not markup
    assert acts_ui.run("(actSetServiceTitles({svc_x: '<b>x</b>'}), actText('printer_command:svc_x:k2').what)") == \
        "&lt;b&gt;x&lt;/b&gt;"
    js = _src("app/web/app.js")
    assert js.count("actSetServiceTitles(page.service_titles);") == 2       # the first page and «Показать ещё»


# --- 7. The nightly backup copies data/job_previews/ too -----------------------------------------

def _backup_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("forge_db_backup_phase2", ROOT / "deploy" / "forge_db_backup.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _farm(tmp_path, previews):
    import sqlite3
    src = tmp_path / "app"
    (src / "data" / "job_previews").mkdir(parents=True)
    con = sqlite3.connect(str(src / "users.db"))
    con.execute("create table t (x)")
    con.commit()
    con.close()
    for name, body in previews.items():
        (src / "data" / "job_previews" / name).write_bytes(body)
    return src


K1, K2, K3 = "a" * 64, "b" * 64, "c" * 64


def test_the_backup_copies_the_job_cards_and_previews(tmp_path):
    import datetime
    backup = _backup_module()
    src = _farm(tmp_path, {K1 + ".png": b"png-1", K1 + ".json": b'{"pick": "x"}', ".preview-tmp123": b"half",
                           "notes.txt": b"n"})
    (src / "data" / "job_previews" / "sub").mkdir()                     # not a file: left out
    (src / "data" / "job_previews" / "sub" / "x.png").write_bytes(b"x")
    dest = tmp_path / "backups"
    assert backup.main(["--src", str(src), "--dest", str(dest)]) == 0
    day = dest / datetime.date.today().isoformat()
    got = sorted(p.name for p in (day / "data" / "job_previews").iterdir())
    assert got == sorted([K1 + ".json", K1 + ".png", "notes.txt"])      # no write in progress, no subfolder
    assert (day / "data" / "job_previews" / (K1 + ".png")).read_bytes() == b"png-1"
    manifest = json.loads((day / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["files"]["users.db"]["quick_check"] == "ok"           # the databases as before
    info = manifest["folders"]["data/job_previews"]
    assert (info["files"], info["copied"], info["linked"], info["errors"]) == (3, 3, 0, 0)
    assert info["bytes"] == len(b"png-1") + len(b'{"pick": "x"}') + 1


def test_a_farm_without_previews_backs_up_as_before(tmp_path):
    import datetime
    backup = _backup_module()
    src = _farm(tmp_path, {})
    (src / "data" / "job_previews").rmdir()
    dest = tmp_path / "backups"
    assert backup.main(["--src", str(src), "--dest", str(dest)]) == 0
    day = dest / datetime.date.today().isoformat()
    assert sorted(p.name for p in day.iterdir()) == ["manifest.json", "users.db"]
    assert "folders" not in json.loads((day / "manifest.json").read_text(encoding="utf-8"))


def _next_day(dest):
    """Make today's backup look like yesterday's, so the next run is «the next night»."""
    import datetime
    today = datetime.date.today()
    (dest / today.isoformat()).rename(dest / (today - datetime.timedelta(days=1)).isoformat())
    return dest / (today - datetime.timedelta(days=1)).isoformat(), dest / today.isoformat()


def test_an_unchanged_preview_is_a_hard_link_to_the_last_backup(tmp_path):
    backup = _backup_module()
    src = _farm(tmp_path, {K1 + ".png": b"png-1" * 1000, K2 + ".json": b"{}"})
    dest = tmp_path / "backups"
    assert backup.main(["--src", str(src), "--dest", str(dest)]) == 0
    old, new = _next_day(dest)
    (src / "data" / "job_previews" / (K3 + ".png")).write_bytes(b"png-3")          # a new job
    (src / "data" / "job_previews" / (K2 + ".json")).write_bytes(b'{"x": 1}')      # rewritten: copied anew
    os.utime(src / "data" / "job_previews" / (K2 + ".json"), (2_000_000_000, 2_000_000_000))
    assert backup.main(["--src", str(src), "--dest", str(dest)]) == 0
    a, b = old / "data" / "job_previews", new / "data" / "job_previews"
    assert os.stat(a / (K1 + ".png")).st_ino == os.stat(b / (K1 + ".png")).st_ino      # one file on the disk
    assert os.stat(b / (K1 + ".png")).st_nlink == 2
    assert (b / (K2 + ".json")).read_bytes() == b'{"x": 1}' and (a / (K2 + ".json")).read_bytes() == b"{}"
    assert (b / (K3 + ".png")).read_bytes() == b"png-3"
    info = json.loads((new / "manifest.json").read_text(encoding="utf-8"))["folders"]["data/job_previews"]
    assert (info["files"], info["linked"], info["copied"]) == (3, 1, 2)
    # pruning the old night keeps the new one whole
    assert backup.main(["--src", str(src), "--dest", str(dest), "--keep", "1"]) == 0
    assert not old.exists() and (b / (K1 + ".png")).read_bytes() == b"png-1" * 1000


def test_the_free_space_check_counts_only_what_the_night_adds(tmp_path, monkeypatch):
    from types import SimpleNamespace
    backup = _backup_module()
    big = b"p" * 300_000
    src = _farm(tmp_path, {K1 + ".png": big})
    dest = tmp_path / "backups"
    db = os.path.getsize(src / "users.db")
    monkeypatch.setattr(backup.shutil, "disk_usage", lambda p: SimpleNamespace(free=2 * db + 10))
    assert backup.main(["--src", str(src), "--dest", str(dest)]) == 3                 # the preview needs room
    monkeypatch.setattr(backup.shutil, "disk_usage", lambda p: SimpleNamespace(free=10 ** 12))
    assert backup.main(["--src", str(src), "--dest", str(dest)]) == 0
    _next_day(dest)
    monkeypatch.setattr(backup.shutil, "disk_usage", lambda p: SimpleNamespace(free=2 * db + 10))
    assert backup.main(["--src", str(src), "--dest", str(dest)]) == 0                 # linked: no new bytes
    monkeypatch.setattr(backup, "links_work", lambda d: False)                       # FAT / exFAT: all copied
    assert backup.main(["--src", str(src), "--dest", str(dest)]) == 3


def test_without_hard_links_every_preview_is_copied(tmp_path, monkeypatch):
    backup = _backup_module()
    src = _farm(tmp_path, {K1 + ".png": b"png-1"})
    dest = tmp_path / "backups"
    assert backup.main(["--src", str(src), "--dest", str(dest)]) == 0
    old, new = _next_day(dest)
    monkeypatch.setattr(backup.os, "link", lambda a, b: (_ for _ in ()).throw(OSError("no links here")))
    assert backup.main(["--src", str(src), "--dest", str(dest)]) == 0
    assert os.stat(old / "data" / "job_previews" / (K1 + ".png")).st_ino != os.stat(
        new / "data" / "job_previews" / (K1 + ".png")).st_ino
    assert (new / "data" / "job_previews" / (K1 + ".png")).read_bytes() == b"png-1"


def test_a_preview_that_cannot_be_copied_fails_the_run_but_not_the_rest(tmp_path, monkeypatch):
    import datetime
    backup = _backup_module()
    src = _farm(tmp_path, {K1 + ".png": b"1", K2 + ".png": b"2"})
    real = backup.shutil.copy2

    def copy2(a, b, *k, **kw):
        if str(a).endswith(K1 + ".png"):
            raise PermissionError("denied")
        return real(a, b, *k, **kw)
    monkeypatch.setattr(backup.shutil, "copy2", copy2)
    dest = tmp_path / "backups"
    assert backup.main(["--src", str(src), "--dest", str(dest)]) == 1
    day = dest / datetime.date.today().isoformat()
    assert (day / "users.db").exists() and (day / "data" / "job_previews" / (K2 + ".png")).read_bytes() == b"2"
    info = json.loads((day / "manifest.json").read_text(encoding="utf-8"))["folders"]["data/job_previews"]
    assert (info["files"], info["errors"]) == (1, 1)


def test_the_docs_say_the_backup_keeps_the_job_cards():
    for lang in ("ru", "en"):
        index = _src("docs-site/docs/%s/install/index.md" % lang)
        assert "не копирует" not in index and "does not copy" not in index
        assert "data/job_previews" in _src("docs-site/docs/%s/install/linux.md" % lang)
        assert "data/job_previews" in _src("docs-site/docs/%s/faq.md" % lang)
    readme = _src("README.md")
    assert "forge_db_backup.py" in readme and "data/job_previews/" in readme
    assert "data/job_previews" in _src("deploy/forge-db-backup.service")
