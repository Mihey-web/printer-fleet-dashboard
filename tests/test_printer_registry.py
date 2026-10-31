"""Printer registry: config migration, stable-id CRUD, pending diff, discard, maintenance mode."""
from types import SimpleNamespace

import pytest

from app.services import printer_registry as reg


class FakeCfg:
    PRINTERS = [
        {"label": "(1) Bambu One", "device_type": "P2S", "host": "10.0.0.1",
         "access_code": "aaaa1111", "serial": "SER0000000000001"},
        {"label": "(2) Bambu Two", "device_type": "X1C", "host": "10.0.0.2",
         "access_code": "bbbb2222", "serial": "SER0000000000002"},
    ]
    CREALITY_PRINTERS = [
        {"label": "(3) K1 Max", "host": "10.0.0.3", "model": "k1max"},
    ]
    KLIPPER_PRINTERS = [
        {"label": "(4) Klipper", "host": "10.0.0.4", "port": 7125},
    ]
    MKS_PRINTERS = [
        {"label": "(5) MKS", "host": "10.0.0.5", "port": 8080, "model": "reborn2"},
    ]


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "registry.db")
    reg._invalidate_label()
    reg._invalidate_maintenance()
    yield path
    reg.set_running(None)
    reg._invalidate_label()
    reg._invalidate_maintenance()


def test_get_label_caches_within_ttl(db, monkeypatch):
    reg.init_db(db)
    p = reg.create_printer({"kind": "bambu", "label": "L1", "host": "h"}, db)
    pid = p["id"]
    reg._invalidate_label()
    calls = []
    real = reg.get_printer
    monkeypatch.setattr(reg, "get_printer", lambda i, d=None: (calls.append(i), real(i, db))[1])
    assert reg.get_label(pid, db) == "L1"
    assert reg.get_label(pid, db) == "L1"  # served from cache, no second DB read
    assert len(calls) == 1


def test_get_label_invalidated_on_rename(db):
    reg.init_db(db)
    p = reg.create_printer({"kind": "bambu", "label": "Old", "host": "h"}, db)
    pid = p["id"]
    assert reg.get_label(pid, db) == "Old"
    reg.update_printer(pid, {"label": "New"}, db)
    assert reg.get_label(pid, db) == "New"  # cache invalidated by the rename


@pytest.fixture
def loaded(db):
    rows = reg.load_for_startup(FakeCfg, db)
    return db, rows


def test_migration_keeps_legacy_ids(loaded):
    db, rows = loaded
    ids = {r["id"] for r in rows}
    assert ids == {"bambu-1", "bambu-2", "creality-1", "klipper-1", "mks-1"}
    b1 = reg.get_printer("bambu-1", db)
    assert b1["kind"] == "bambu"
    assert b1["model"] == "P2S"          # device_type maps to model
    assert b1["access_code"] == "aaaa1111"
    assert b1["serial"] == "SER0000000000001"
    c1 = reg.get_printer("creality-1", db)
    assert c1["kind"] == "creality"
    assert reg.get_printer("klipper-1", db)["port"] == 7125


def test_migration_runs_once(loaded):
    db, _ = loaded
    assert reg.migrate_from_config(FakeCfg, db) == 0
    assert len(reg.list_printers(db)) == 5


def test_create_generates_stable_prefixed_id(loaded):
    db, _ = loaded
    row = reg.create_printer({"kind": "bambu", "label": "(6) New", "host": "10.0.0.6",
                              "access_code": "cccc3333", "serial": "SER0000000000006",
                              "model": "P2S"}, db)
    assert row["id"].startswith("p-") and len(row["id"]) == 10
    assert reg.pending_state(row) == "new"
    # Existing printers are untouched by the insertion.
    assert reg.pending_state(reg.get_printer("bambu-1", db)) is None


def test_update_marks_modified_and_keeps_id(loaded):
    db, _ = loaded
    row = reg.update_printer("bambu-1", {"host": "10.0.9.9", "label": "(1) Renamed"}, db)
    assert row["id"] == "bambu-1"
    assert row["host"] == "10.0.9.9"
    assert reg.pending_state(row) == "modified"
    # kind is not updatable
    same = reg.update_printer("bambu-1", {"kind": "mks"}, db)
    assert same["kind"] == "bambu"


def test_delete_running_is_soft_and_restorable(loaded):
    db, _ = loaded
    assert reg.delete_printer("creality-1", db) == "soft"
    row = reg.get_printer("creality-1", db)
    assert row["deleted"] is True
    assert reg.pending_state(row) == "deleted"
    assert "creality-1" not in {r["id"] for r in reg.list_printers(db, include_deleted=False)}
    restored = reg.restore_printer("creality-1", db)
    assert restored["deleted"] is False
    assert reg.pending_state(restored) is None


def test_delete_pending_new_is_hard(loaded):
    db, _ = loaded
    row = reg.create_printer({"kind": "klipper", "label": "(7) Tmp", "host": "10.0.0.7"}, db)
    assert reg.delete_printer(row["id"], db) == "hard"
    assert reg.get_printer(row["id"], db) is None


def test_discard_restores_running_snapshot(loaded):
    db, rows = loaded
    reg.update_printer("bambu-2", {"host": "10.9.9.9"}, db)
    reg.delete_printer("mks-1", db)
    reg.create_printer({"kind": "mks", "label": "(8) Extra", "host": "10.0.0.8"}, db)
    assert reg.discard_changes(db) == 5
    after = reg.list_printers(db)
    assert {r["id"] for r in after} == {r["id"] for r in rows}
    assert reg.get_printer("bambu-2", db)["host"] == "10.0.0.2"
    assert reg.get_printer("mks-1", db)["deleted"] is False
    assert all(reg.pending_state(r) is None for r in after)


def test_unknown_ids_return_none(loaded):
    db, _ = loaded
    assert reg.update_printer("nope-1", {"label": "x"}, db) is None
    assert reg.delete_printer("nope-1", db) is None
    assert reg.restore_printer("nope-1", db) is None


def test_soft_deleted_purged_on_restart(loaded):
    """Перезапуск = применение: помеченный на удаление принтер вычищается
    при следующем load_for_startup и не всплывает как «новый»."""
    db, _ = loaded
    assert reg.delete_printer("creality-1", db) == "soft"
    # до рестарта строка видна с pending='deleted'
    row = reg.get_printer("creality-1", db)
    assert row["deleted"] is True
    assert reg.pending_state(row) == "deleted"
    # «рестарт»
    rows = reg.load_for_startup(FakeCfg, db)
    assert "creality-1" not in {r["id"] for r in rows}
    assert reg.get_printer("creality-1", db) is None
    for r in reg.list_printers(db):
        assert reg.pending_state(r) is None


# --- maintenance mode (spec_maintenance §1) --------------------------------------

_OLD_PRINTERS = """
    CREATE TABLE printers (
        id TEXT PRIMARY KEY, kind TEXT NOT NULL, label TEXT NOT NULL, host TEXT NOT NULL,
        port INTEGER, model TEXT, access_code TEXT, serial TEXT,
        deleted INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, updated_at REAL NOT NULL
    )
"""


def test_init_db_adds_the_maintenance_columns_to_an_old_table_in_place(db):
    import sqlite3
    conn = sqlite3.connect(db)
    conn.execute(_OLD_PRINTERS)
    conn.execute("INSERT INTO printers VALUES ('bambu-1', 'bambu', 'B1', 'h', NULL, 'P2S', 'code', 'ser', 0, 1.0, 2.0)")
    conn.commit()
    conn.close()
    for _ in range(2):                                   # idempotent on the next start
        reg.init_db(db)
    conn = sqlite3.connect(db)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(printers)")]
    row = conn.execute("SELECT * FROM printers").fetchone()
    conn.close()
    assert cols[-3:] == ["maintenance_since", "maintenance_note", "maintenance_by"]
    assert len(cols) == 14                               # appended once, nothing rebuilt
    assert row == ("bambu-1", "bambu", "B1", "h", None, "P2S", "code", "ser", 0, 1.0, 2.0, None, None, None)
    assert reg.maintenance_map(db) == {}


def test_set_maintenance_on_note_off_and_none(loaded):
    db, _ = loaded
    assert reg.set_maintenance("klipper-1", True, note="меняю хотенд", by="admin", now=1000.0, db_path=db) == (
        "on", {"since": 1000.0, "note": "меняю хотенд", "by": "admin"})
    # already on: only the note changes, since and by stay
    assert reg.set_maintenance("klipper-1", True, note="жду детали", by="other", now=2000.0, db_path=db) == (
        "note", {"since": 1000.0, "note": "жду детали", "by": "admin"})
    assert reg.set_maintenance("klipper-1", True, note="жду детали", by="other", db_path=db) == (
        "none", {"since": 1000.0, "note": "жду детали", "by": "admin"})
    assert reg.set_maintenance("klipper-1", True, note="", db_path=db) == (       # empty note -> NULL
        "note", {"since": 1000.0, "note": None, "by": "admin"})
    assert reg.get_printer("klipper-1", db)["maintenance_note"] is None
    assert reg.set_maintenance("klipper-1", False, db_path=db) == ("off", None)
    row = reg.get_printer("klipper-1", db)
    assert (row["maintenance_since"], row["maintenance_note"], row["maintenance_by"]) == (None, None, None)
    assert reg.set_maintenance("klipper-1", False, db_path=db) == ("none", None)
    assert reg.set_maintenance("nope-1", True, db_path=db) is None
    assert reg.set_maintenance("nope-1", False, db_path=db) is None


def test_set_maintenance_without_a_clock_stamps_now(loaded, monkeypatch):
    db, _ = loaded
    monkeypatch.setattr(reg, "time", SimpleNamespace(time=lambda: 1234.5))
    assert reg.set_maintenance("mks-1", True, db_path=db) == ("on", {"since": 1234.5, "note": None, "by": None})


def test_maintenance_map_lists_soft_deleted_printers_and_follows_every_write(loaded):
    db, _ = loaded
    reg.set_maintenance("creality-1", True, note="n", by="admin", now=10.0, db_path=db)
    reg.set_maintenance("mks-1", True, now=20.0, db_path=db)
    assert reg.maintenance_map(db) == {"creality-1": {"since": 10.0, "note": "n", "by": "admin"},
                                       "mks-1": {"since": 20.0, "note": None, "by": None}}
    assert reg.delete_printer("creality-1", db) == "soft"     # still polled until the restart
    assert reg.in_maintenance("creality-1", db) and reg.in_maintenance("mks-1", db)
    assert not reg.in_maintenance("bambu-1", db)
    assert reg.maintenance_of("mks-1", db) == {"since": 20.0, "note": None, "by": None}
    assert reg.maintenance_of("bambu-1", db) is None
    reg.set_maintenance("mks-1", False, db_path=db)
    assert reg.maintenance_map(db) == {"creality-1": {"since": 10.0, "note": "n", "by": "admin"}}
    reg.load_for_startup(FakeCfg, db)                         # the restart purges the deleted row
    assert reg.maintenance_map(db) == {}


def test_maintenance_map_is_cached_within_its_ttl(loaded, monkeypatch):
    db, _ = loaded
    reg.set_maintenance("bambu-1", True, now=5.0, db_path=db)
    reads = []
    real = reg._connect
    monkeypatch.setattr(reg, "_connect", lambda d=None: (reads.append(d), real(d))[1])
    clock = {"t": 1000.0}
    monkeypatch.setattr(reg, "time", SimpleNamespace(time=lambda: clock["t"]))
    for _ in range(3):
        assert reg.in_maintenance("bambu-1", db)
    assert len(reads) == 1
    clock["t"] += reg._MAINT_TTL + 0.1
    assert reg.in_maintenance("bambu-1", db) and len(reads) == 2
    m = reg.maintenance_map(db)
    m["bambu-1"]["note"] = "mutated"                          # callers get a copy
    assert reg.maintenance_of("bambu-1", db)["note"] is None


def test_maintenance_map_answers_the_last_good_read_when_sqlite_fails(loaded, monkeypatch):
    db, _ = loaded
    reg.set_maintenance("klipper-1", True, now=7.0, db_path=db)
    assert reg.in_maintenance("klipper-1", db)

    def broken(d=None):
        raise reg.sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(reg, "_connect", broken)
    reg._invalidate_maintenance()
    assert reg.maintenance_map(db) == {"klipper-1": {"since": 7.0, "note": None, "by": None}}
    assert reg.in_maintenance("klipper-1", db)
    # without any good read of that DB: nobody is in maintenance, and nothing raises
    assert reg.maintenance_map(db + ".other") == {}
    assert reg.in_maintenance("klipper-1", db + ".other") is False


def test_discard_keeps_the_maintenance_mode(loaded):
    db, _ = loaded
    reg.set_maintenance("klipper-1", True, note="меняю хотенд", by="admin", now=100.0, db_path=db)
    reg.set_maintenance("mks-1", True, now=200.0, db_path=db)
    reg.update_printer("klipper-1", {"host": "10.9.9.9"}, db)
    reg.create_printer({"kind": "mks", "label": "(8) Extra", "host": "10.0.0.8"}, db)
    created = [r["id"] for r in reg.list_printers(db) if r["id"].startswith("p-")]
    reg.set_maintenance(created[0], True, now=300.0, db_path=db)   # a dropped row takes its mode along
    assert reg.discard_changes(db) == 5
    assert reg.get_printer("klipper-1", db)["host"] == "10.0.0.4"
    assert reg.maintenance_map(db) == {"klipper-1": {"since": 100.0, "note": "меняю хотенд", "by": "admin"},
                                       "mks-1": {"since": 200.0, "note": None, "by": None}}


def test_maintenance_is_no_pending_change(loaded):
    db, _ = loaded
    reg.set_maintenance("bambu-1", True, note="x", by="admin", db_path=db)
    row = reg.get_printer("bambu-1", db)
    assert row["maintenance_since"] is not None
    assert reg.pending_state(row) is None
    # the regular PUT does not touch it
    reg.update_printer("bambu-1", {"maintenance_since": None, "maintenance_note": "y", "label": "(1) B"}, db)
    assert reg.maintenance_of("bambu-1", db)["note"] == "x"
