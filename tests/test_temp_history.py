"""Temperature history for the chip popover chart: the printer_history columns
(chamber and targets), app/services/temp_history.py and GET /api/printers/{id}/temps."""
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient
from starlette.responses import Response

import config as cfg
from app.api import auth as api_auth
from app.api import main as api_main
from app.domain.models import PrinterKind, PrinterState, PrinterStatus
from app.domain.user_models import UserRole
from app.services import auth_service, state_store, temp_history

T0 = 1760000000.0
H = 3600
TEMP_COLS = ("chamber_temp", "target_nozzle_temp", "target_bed_temp", "target_chamber_temp")

# printer_history as on the Pi before the temperature chart: grace and reason
# columns, no chamber / targets.
_PRE_CHART_SCHEMA = """
    CREATE TABLE printer_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        printer_id TEXT NOT NULL, label TEXT NOT NULL, kind TEXT NOT NULL,
        online INTEGER NOT NULL, state TEXT NOT NULL, progress REAL, job_name TEXT,
        eta_sec INTEGER, print_time INTEGER, nozzle_temp REAL, bed_temp REAL,
        layer INTEGER, total_layer INTEGER, last_error TEXT, device_type TEXT,
        recorded_at REAL NOT NULL,
        grace_period_active INTEGER DEFAULT 0, last_successful_fetch REAL DEFAULT 0,
        reason TEXT
    )
"""


@pytest.fixture(autouse=True)
def _clear_temps_cache():
    temp_history._cache.clear()
    yield
    temp_history._cache.clear()


def _store(monkeypatch, db):
    monkeypatch.setattr(state_store, "DB_PATH", db)
    return state_store.StateStore()


# --- 1-2: the columns ----------------------------------------------------------------------

def test_init_db_adds_temp_columns_to_an_old_table(tmp_path, monkeypatch):
    db = str(tmp_path / "ph.db")
    conn = sqlite3.connect(db)
    conn.execute(_PRE_CHART_SCHEMA)
    conn.execute("INSERT INTO printer_history (printer_id, label, kind, online, state, nozzle_temp, bed_temp,"
                 " recorded_at, reason) VALUES ('p1', 'P1', 'bambu', 1, 'printing', 219.5, 65.0, 1000.0, 'r')")
    conn.commit()
    conn.close()
    assert state_store.HISTORY_TEMP_COLUMNS == TEMP_COLS
    for _ in range(2):                                   # idempotent on the next start
        store = _store(monkeypatch, db)
        info = {r[1]: r for r in store._db.execute("PRAGMA table_info(printer_history)").fetchall()}
        row = store._db.execute("SELECT id, state, nozzle_temp, bed_temp, reason, %s FROM printer_history"
                                % ", ".join(TEMP_COLS)).fetchall()
        store._db.close()
        for col in TEMP_COLS:
            # nullable REAL without a default: a schema-only ALTER, no table rewrite
            assert info[col][2] == "REAL" and info[col][3] == 0 and info[col][4] is None, col
        assert list(info)[-4:] == list(TEMP_COLS)        # appended to the old table
        assert row == [(1, "printing", 219.5, 65.0, "r", None, None, None, None)]


def test_record_snapshot_stores_chamber_and_targets(tmp_path, monkeypatch):
    store = _store(monkeypatch, str(tmp_path / "ph.db"))
    try:
        store.record_snapshot(PrinterStatus(
            id="h2s", label="H2S", kind=PrinterKind.BAMBU, online=True, state=PrinterState.PRINTING,
            nozzle_temp=219.6, bed_temp=64.8, chamber_temp=41.5,
            target_nozzle_temp=220.0, target_bed_temp=65.0, target_chamber_temp=45.0))
        store.record_snapshot(PrinterStatus(                 # P1S: no chamber sensor, heaters off
            id="p1s", label="P1S", kind=PrinterKind.BAMBU, online=True, state=PrinterState.IDLE,
            nozzle_temp=30.0, bed_temp=25.0, target_nozzle_temp=0.0, target_bed_temp=0.0))
        rows = store._db.execute("SELECT printer_id, nozzle_temp, bed_temp, %s FROM printer_history ORDER BY id"
                                 % ", ".join(TEMP_COLS)).fetchall()
    finally:
        store._db.close()
    assert rows == [("h2s", 219.6, 64.8, 41.5, 220.0, 65.0, 45.0),
                    ("p1s", 30.0, 25.0, None, 0.0, 0.0, None)]


# --- 3-11: temp_history -------------------------------------------------------------------

def _row(ts, pid="p1", online=1, grace=0, n=None, tn=None, b=None, tb=None, c=None, tc=None):
    return (pid, pid.upper(), "bambu", online, "printing" if online else "offline", ts, grace,
            n, tn, b, tb, c, tc)


def _seed(tmp_path, monkeypatch, rows, name="ph.db"):
    """A printer_history with the real schema (StateStore) holding `rows` (_row tuples)."""
    db = str(tmp_path / name)
    _store(monkeypatch, db)._db.close()
    conn = sqlite3.connect(db)
    conn.executemany(
        "INSERT INTO printer_history (printer_id, label, kind, online, state, recorded_at, grace_period_active,"
        " nozzle_temp, target_nozzle_temp, bed_temp, target_bed_temp, chamber_temp, target_chamber_temp)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()
    return db


def test_raw_window_keeps_each_sample(tmp_path, monkeypatch):
    rows = [_row(T0 + 45 + 90 * i, n=200.04 + i, tn=220.0, b=60.06, tb=60.0, c=30.0) for i in range(240)]
    db = _seed(tmp_path, monkeypatch, rows)
    out = temp_history.query_temps(db, "p1", T0, T0 + 6 * H)
    assert out["step"] == 90 and out["fr"] == int(T0) and out["to"] == int(T0 + 6 * H)
    assert out["t"] == [int(T0) + 45 + 90 * i for i in range(240)]
    assert all(isinstance(t, int) for t in out["t"])
    noz = out["series"]["nozzle"]
    assert noz["v"][:3] == [200.0, 201.0, 202.0] and noz["v"][-1] == 439.0     # rounded to 0.1
    assert out["series"]["bed"]["v"][0] == 60.1
    assert noz["target"] == [220] * 240
    for s in out["series"].values():
        assert "lo" not in s and "hi" not in s                                  # raw step: no envelope
        assert len(s["v"]) == len(s["target"]) == 240
    # the cached path computes the same window back from `now`
    cached = json.loads(temp_history.cached_temps_json(db, "p1", 6, now=T0 + 6 * H))
    assert cached["hours"] == 6 and cached["step"] == 90 and cached["t"] == out["t"]


def test_offline_and_grace_rows_are_skipped(tmp_path, monkeypatch):
    db = _seed(tmp_path, monkeypatch, [
        _row(T0 + 100, n=210.0, b=60.0),
        _row(T0 + 190, online=0),                         # offline: temperatures NULL
        _row(T0 + 280, grace=1, n=210.0, b=60.0),         # grace: a stale copy of the last status
        _row(T0 + 370, grace=None, n=180.0, b=55.0),      # a NULL grace flag counts as live
    ])
    out = temp_history.query_temps(db, "p1", T0, T0 + H)
    assert out["t"] == [int(T0) + 100, int(T0) + 370]
    assert out["series"]["nozzle"]["v"] == [210.0, 180.0]
    assert out["series"]["nozzle"]["stats"]["n"] == 2 and out["series"]["bed"]["stats"]["n"] == 2


def test_heater_off_target_is_null(tmp_path, monkeypatch):
    raw = _seed(tmp_path, monkeypatch, [
        _row(T0 + 10, n=30.0, tn=0.0), _row(T0 + 100, n=31.0, tn=None), _row(T0 + 190, n=150.0, tn=219.6)])
    assert temp_history.query_temps(raw, "p1", T0, T0 + H)["series"]["nozzle"]["target"] == [None, None, 220]

    db = _seed(tmp_path, monkeypatch, [
        _row(T0 + 10, n=30.0, tn=0.0), _row(T0 + 100, n=150.0, tn=220.0), _row(T0 + 200, n=210.0, tn=None),
        _row(T0 + 400, n=210.0, tn=0.0), _row(T0 + 500, n=200.0, tn=None)], name="day.db")
    out = temp_history.query_temps(db, "p1", T0, T0 + 24 * H)
    assert out["step"] == 360
    assert out["series"]["nozzle"]["target"] == [220, None]   # 0 / 220 / NULL -> 220; 0 / NULL -> null


def test_series_without_data_is_omitted(tmp_path, monkeypatch):
    db = _seed(tmp_path, monkeypatch, [_row(T0 + 90 * i, n=25.0, tn=0.0, b=24.0, tb=0.0) for i in range(5)])
    out = temp_history.query_temps(db, "p1", T0, T0 + H)
    assert set(out["series"]) == {"nozzle", "bed"}            # P1S: no chamber sensor
    empty = temp_history.query_temps(db, "nope", T0, T0 + H)
    assert empty["t"] == [] and empty["series"] == {}
    missing = temp_history.query_temps(str(tmp_path / "missing.db"), "p1", T0, T0 + H)
    assert missing["series"] == {} and not (tmp_path / "missing.db").exists()   # read-only: no file created


def test_long_window_is_bucketed_with_envelope(tmp_path, monkeypatch):
    n = 7 * 24 * H // 90                                       # 6720 rows
    spike = 3000
    rows = [_row(T0 + 45 + 90 * i, n=300.0 if i == spike else 200.0 + (i % 7) * 0.5, tn=220.0,
                 b=60.0 + (i % 3), tb=60.0, c=35.0 + (i % 5) * 0.2) for i in range(n)]
    db = _seed(tmp_path, monkeypatch, rows)
    out = temp_history.query_temps(db, "p1", T0, T0 + 7 * 24 * H)
    assert out["step"] == 2520 and len(out["t"]) <= 240
    assert out["t"] == sorted(out["t"])
    for s in out["series"].values():
        assert len(s["v"]) == len(s["lo"]) == len(s["hi"]) == len(s["target"]) == len(out["t"])
        for v, lo, hi in zip(s["v"], s["lo"], s["hi"]):
            assert lo <= v <= hi
    noz = out["series"]["nozzle"]
    assert max(noz["hi"]) == 300.0 and max(noz["v"]) < 210      # a 90 s spike stays in the envelope
    assert noz["stats"]["max"] == 300.0
    assert out["series"]["chamber"]["target"] == [None] * len(out["t"])


def test_stats_are_over_raw_samples(tmp_path, monkeypatch):
    # bucket 1: 90, 110, 100, 100 (mean 100); bucket 2: 200 alone
    db = _seed(tmp_path, monkeypatch,
               [_row(T0 + 10 + 60 * i, n=v) for i, v in enumerate((90.0, 110.0, 100.0, 100.0))] +
               [_row(T0 + 5000, n=200.0)])
    noz = temp_history.query_temps(db, "p1", T0, T0 + 24 * H)["series"]["nozzle"]
    assert noz["v"] == [100.0, 200.0]
    assert noz["stats"] == {"min": 90.0, "avg": 120.0, "max": 200.0, "n": 5}   # not 100 / 150 / 200 / 2


def test_other_printers_and_out_of_window_rows_excluded(tmp_path, monkeypatch):
    db = _seed(tmp_path, monkeypatch, [
        _row(T0 - 1, n=1.0), _row(T0, n=10.0), _row(T0 + 100, n=20.0, pid="p2"),
        _row(T0 + 200, n=30.0), _row(T0 + H, n=40.0), _row(T0 + H + 1, n=50.0)])
    out = temp_history.query_temps(db, "p1", T0, T0 + H)
    assert out["printer_id"] == "p1"
    assert out["series"]["nozzle"]["v"] == [10.0, 30.0, 40.0]
    assert out["series"]["nozzle"]["stats"]["n"] == 3
    # never more than 7 days back
    capped = temp_history.query_temps(db, "p1", T0 - 30 * 24 * H, T0 + H)
    assert capped["fr"] == int(T0 + H - 7 * 24 * H) and capped["step"] == 2520
    assert json.loads(temp_history.cached_temps_json(db, "p1", 10_000, now=T0 + H))["hours"] == 168


def test_cache_serves_within_ttl_and_expires(tmp_path, monkeypatch):
    db = _seed(tmp_path, monkeypatch, [_row(T0 + 100, n=200.0)])
    calls = []
    real = temp_history.query_temps

    def counting(*a):
        calls.append(a)
        return real(*a)

    clock = {"v": 1000.0}
    monkeypatch.setattr(temp_history, "query_temps", counting)
    monkeypatch.setattr(temp_history.time, "monotonic", lambda: clock["v"])

    first = temp_history.cached_temps_json(db, "p1", 6, now=T0 + H)
    assert temp_history.cached_temps_json(db, "p1", 6, now=T0 + H) == first and len(calls) == 1
    clock["v"] += temp_history.CACHE_TTL_S - 1
    temp_history.cached_temps_json(db, "p1", 6, now=T0 + H)
    assert len(calls) == 1                                      # within the TTL: no DB read
    temp_history.cached_temps_json(db, "p1", 24, now=T0 + H)
    assert len(calls) == 2 and calls[1][2:] == (T0 + H - 24 * H, T0 + H)   # another window: its own entry
    clock["v"] += 2
    temp_history.cached_temps_json(db, "p1", 6, now=T0 + H)
    assert len(calls) == 3                                      # expired: read again

    monkeypatch.setattr(temp_history, "CACHE_MAX", 3)
    for pid in ("a", "b", "c", "d", "e"):
        temp_history.cached_temps_json(db, pid, 6, now=T0 + H)
        assert len(temp_history._cache) <= 3
    assert (db, "e", 6) in temp_history._cache                  # the newest entry is kept


def test_week_payload_is_small(tmp_path, monkeypatch):
    n = 7 * 24 * H // 90
    rows = [_row(T0 + 45 + 90 * i, n=215.37 + (i % 11) * 1.13, tn=220.0 if i % 2 else 0.0,
                 b=64.83 + (i % 5) * 0.31, tb=65.0, c=41.27 + (i % 7) * 0.17, tc=45.0) for i in range(n)]
    db = _seed(tmp_path, monkeypatch, rows)
    text = temp_history.cached_temps_json(db, "p1", 168, now=T0 + 7 * 24 * H)
    out = json.loads(text)
    assert set(out["series"]) == {"nozzle", "bed", "chamber"}
    assert all("lo" in s and "hi" in s for s in out["series"].values())
    assert len(text) < 24_000, len(text)


# --- 12-16: GET /api/printers/{id}/temps --------------------------------------------------

class FakeStore:
    def __init__(self, ids):
        self._ids = set(ids)

    def get_all(self):
        return []

    def get_one(self, printer_id):
        if printer_id not in self._ids:
            return None
        return PrinterStatus(id=printer_id, label="P", kind=PrinterKind.BAMBU, online=True,
                             state=PrinterState.PRINTING)


def _client(monkeypatch, db, role):
    monkeypatch.setattr(cfg, "JWT_SECRET", "test-secret", raising=False)
    # The middleware imports these locally from their modules: patch at the source.
    monkeypatch.setattr(auth_service, "get_user_from_token", lambda *a, **k: (role.value, role))
    monkeypatch.setattr(auth_service, "decode_token", lambda *a, **k: {"iat": 0})
    monkeypatch.setattr(api_auth, "token_subject_active", lambda *a, **k: True)
    monkeypatch.setattr(api_main, "_history_db_path", lambda: db)
    app = api_main.create_app(FakeStore(["p1"]))
    c = TestClient(app)
    c.cookies.set("access_token", "stub")
    return app, c


@pytest.fixture
def api(tmp_path, monkeypatch):
    import time
    now = time.time()
    db = _seed(tmp_path, monkeypatch, [_row(now - 600 + 90 * i, n=210.0, tn=220.0, b=60.0, tb=60.0)
                                       for i in range(5)])
    return _client(monkeypatch, db, UserRole.ADMIN)


def test_temps_endpoint_json(api):
    _, c = api
    r = c.get("/api/printers/p1/temps", params={"hours": 24})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    data = r.json()
    assert {"t", "step", "series", "hours", "fr", "to"} <= set(data)
    assert data["hours"] == 24 and data["step"] == 360 and len(data["t"]) >= 1
    assert data["series"]["nozzle"]["target"][-1] == 220


def test_temps_hours_bounds(api):
    _, c = api
    assert c.get("/api/printers/p1/temps", params={"hours": 0}).status_code == 422
    assert c.get("/api/printers/p1/temps", params={"hours": 169}).status_code == 422
    assert c.get("/api/printers/p1/temps", params={"hours": "x"}).status_code == 422
    r = c.get("/api/printers/p1/temps")
    assert r.status_code == 200 and r.json()["hours"] == 6 and r.json()["step"] == 90
    assert c.get("/api/printers/p1/temps", params={"hours": 168}).json()["step"] == 2520


def test_temps_unknown_printer_404(api):
    _, c = api
    assert c.get("/api/printers/nope/temps").status_code == 404


def test_temps_open_to_viewers(tmp_path, monkeypatch):
    db = _seed(tmp_path, monkeypatch, [])
    _, c = _client(monkeypatch, db, UserRole.VIEWER)
    r = c.get("/api/printers/p1/temps", params={"hours": 6})
    assert r.status_code == 200 and r.json()["series"] == {}
    c.cookies.clear()
    assert c.get("/api/printers/p1/temps").status_code == 401   # still behind the session


def test_temps_handler_returns_prebuilt_response(api):
    app, _ = api
    [route] = [r for r in app.routes if getattr(r, "path", None) == "/api/printers/{printer_id}/temps"]
    out = route.endpoint(printer_id="p1", hours=6)
    assert isinstance(out, Response) and not isinstance(out, dict)   # serialized off the event loop
    assert out.media_type == "application/json"
    assert json.loads(out.body)["hours"] == 6
