"""Printer registry: config migration, stable-id CRUD, pending diff, discard."""
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
    yield path
    reg.set_running(None)


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
