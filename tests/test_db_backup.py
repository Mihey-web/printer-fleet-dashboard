"""Nightly SQLite backup (deploy/forge_db_backup.py) — consistency, retention, safety."""
import datetime
import importlib.util
import os
import sqlite3
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "forge_db_backup", Path(__file__).resolve().parent.parent / "deploy" / "forge_db_backup.py")
backup = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(backup)


def _make_db(path: Path, rows: int, keep_open: bool = False):
    """WAL-mode DB; with keep_open the last rows stay in the WAL (not checkpointed)."""
    con = sqlite3.connect(path)
    con.execute("pragma journal_mode=wal")
    con.execute("create table t (id integer primary key, v text)")
    con.executemany("insert into t (v) values (?)", [("row%d" % i,) for i in range(rows)])
    con.commit()
    if keep_open:
        return con
    con.close()
    return None


def _count(path: Path) -> int:
    con = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    try:
        return con.execute("select count(*) from t").fetchone()[0]
    finally:
        con.close()


@pytest.fixture
def src(tmp_path):
    d = tmp_path / "app"; d.mkdir()
    return d


def test_backup_is_consistent_including_uncheckpointed_wal(src, tmp_path):
    live = _make_db(src / "printer_history.db", 500, keep_open=True)  # data still in -wal
    _make_db(src / "users.db", 5)
    _make_db(src / "ams_history.db", 50)
    (src / "config.py").write_text("X = 1\n", encoding="utf-8")
    dest = tmp_path / "backups"
    try:
        rc = backup.main(["--src", str(src), "--dest", str(dest), "--keep", "14"])
    finally:
        live.close()
    assert rc == 0
    day = dest / datetime.date.today().isoformat()
    assert _count(day / "printer_history.db") == 500
    assert _count(day / "users.db") == 5
    assert _count(day / "ams_history.db") == 50
    assert (day / "config.py").read_text(encoding="utf-8") == "X = 1\n"
    assert not list(day.glob("*-wal")), "backup copies must be self-contained single files"
    manifest = (day / "manifest.json").read_text(encoding="utf-8")
    assert '"quick_check": "ok"' in manifest


def test_retention_keeps_newest_dated_dirs_only(src, tmp_path):
    _make_db(src / "users.db", 1)
    dest = tmp_path / "backups"; dest.mkdir()
    today = datetime.date.today()
    for i in range(1, 6):
        (dest / (today - datetime.timedelta(days=i)).isoformat()).mkdir()
    (dest / "keep-me-not-a-date").mkdir()
    (dest / "notes.txt").write_text("x", encoding="utf-8")
    assert backup.main(["--src", str(src), "--dest", str(dest), "--keep", "3"]) == 0
    dated = sorted(p.name for p in dest.iterdir() if p.is_dir() and p.name[:4].isdigit())
    assert dated == [(today - datetime.timedelta(days=i)).isoformat() for i in (2, 1, 0)]
    assert (dest / "keep-me-not-a-date").is_dir() and (dest / "notes.txt").is_file()


def test_refuses_when_mountpoint_is_not_mounted(src, tmp_path):
    _make_db(src / "users.db", 1)
    mnt = tmp_path / "ssd"; mnt.mkdir()            # a plain dir, not a mount
    dest = mnt / "forge-backups"
    rc = backup.main(["--src", str(src), "--dest", str(dest), "--mountpoint", str(mnt)])
    assert rc == 2
    assert not dest.exists(), "must not write onto the SD card when the SSD is missing"


def test_missing_database_does_not_block_the_others(src, tmp_path):
    _make_db(src / "users.db", 3)                   # printer_history/ams_history absent
    dest = tmp_path / "backups"
    assert backup.main(["--src", str(src), "--dest", str(dest)]) == 0
    day = dest / datetime.date.today().isoformat()
    assert _count(day / "users.db") == 3
    assert not (day / "printer_history.db").exists()


def test_second_run_same_day_replaces_the_copy(src, tmp_path):
    _make_db(src / "users.db", 2)
    dest = tmp_path / "backups"
    assert backup.main(["--src", str(src), "--dest", str(dest)]) == 0
    con = sqlite3.connect(src / "users.db"); con.execute("insert into t (v) values ('new')"); con.commit(); con.close()
    assert backup.main(["--src", str(src), "--dest", str(dest)]) == 0
    day = dest / datetime.date.today().isoformat()
    assert _count(day / "users.db") == 3
    assert not [p for p in dest.iterdir() if p.name.startswith(".tmp-")], "no leftover temp dirs"


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_backup_files_are_owner_only(src, tmp_path):
    _make_db(src / "users.db", 1)
    dest = tmp_path / "backups"
    assert backup.main(["--src", str(src), "--dest", str(dest)]) == 0
    day = dest / datetime.date.today().isoformat()
    for f in day.iterdir():
        assert f.stat().st_mode & 0o077 == 0, f.name
