"""Tests for AmsStore — the separate AMS telemetry history DB.

Mirrors tests/test_history.py: monkeypatch DB_PATH onto a temp file so each
test gets a fresh SQLite database.
"""
import pytest

from app.services import ams_store
from app.domain.models import PrinterStatus, PrinterKind, PrinterState


def _status(pid="bambu-3", label="P2S-3", ams=None):
    return PrinterStatus(
        id=pid, label=label, kind=PrinterKind.BAMBU,
        online=True, state=PrinterState.PRINTING, ams=ams,
    )


def _ams(units):
    return {"tray_now": 0, "units": units}


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(ams_store, "DB_PATH", str(tmp_path / "ams.db"))
    s = ams_store.AmsStore()
    yield s
    s._db.close()


def test_init_creates_table_and_index(store):
    names = {r[0] for r in store._db.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','index')"
    ).fetchall()}
    assert "ams_history" in names
    assert "idx_ams_printer_unit_time" in names


def test_record_writes_one_row_per_unit(store):
    st = _status(ams=_ams([
        {"humidity": 1, "humidity_pct": 24, "temp": 28.5, "dry_time": 0, "slots": []},
        {"humidity": 3, "humidity_pct": 47, "temp": 30.0, "dry_time": 0, "slots": []},
    ]))
    store.record_ams(st)
    rows = store.query("bambu-3", 0, 0, 9_999_999_999) + \
        store.query("bambu-3", 1, 0, 9_999_999_999)
    assert len(rows) == 2
    assert rows[0]["humidity_pct"] == 24
    assert rows[0]["temp"] == 28.5
    assert rows[1]["humidity_pct"] == 47


def test_record_skips_status_without_ams(store):
    store.record_ams(_status(ams=None))
    store.record_ams(_status(ams=_ams([])))  # units empty
    count = store._db.execute("SELECT COUNT(*) FROM ams_history").fetchone()[0]
    assert count == 0


def test_classic_ams_writes_null_pct(store):
    # Классический AMS отдаёт только уровень 0–5, humidity_pct отсутствует.
    st = _status(ams=_ams([
        {"humidity": 2, "humidity_pct": None, "temp": 26.0, "dry_time": None, "slots": []},
    ]))
    store.record_ams(st)
    row = store.query("bambu-3", 0, 0, 9_999_999_999)[0]
    assert row["humidity_idx"] == 2
    assert row["humidity_pct"] is None


def test_query_returns_series_in_time_order(store):
    st = _status(ams=_ams([{"humidity": 1, "humidity_pct": 20, "temp": 25.0, "dry_time": 0, "slots": []}]))
    store.record_ams(st)
    store.record_ams(st)
    store.record_ams(st)
    rows = store.query("bambu-3", 0, 0, 9_999_999_999)
    assert len(rows) == 3
    times = [r["recorded_at"] for r in rows]
    assert times == sorted(times)


def test_query_respects_window_and_unit(store):
    st = _status(ams=_ams([{"humidity": 1, "humidity_pct": 20, "temp": 25.0, "dry_time": 0, "slots": []}]))
    store.record_ams(st)
    # Другой юнит-индекс и окно в прошлом не должны попасть.
    assert store.query("bambu-3", 5, 0, 9_999_999_999) == []
    assert store.query("bambu-3", 0, 0, 1) == []
    assert store.query("other", 0, 0, 9_999_999_999) == []
