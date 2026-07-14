"""Regression tests for the history SQL optimizations (round 3 deferred M9).

Covers:
- get_events pagination via LIMIT+1 / has_more (no second COUNT(*) scan).
- states-summary TTL memoization (the expensive LEAD() window scan).
- the idx_history_printer_time_state covering index created by StateStore.
"""
import sqlite3

from app.api import main as api_main
from app.services import state_store


def _seed(db, rows):
    """Create a minimal printer_history table and insert (pid,label,state,job,err,ts) rows."""
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE printer_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            printer_id TEXT, label TEXT, state TEXT,
            job_name TEXT, last_error TEXT, recorded_at REAL
        )
        """
    )
    conn.executemany(
        "INSERT INTO printer_history (printer_id, label, state, job_name, last_error, recorded_at)"
        " VALUES (?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()


def test_query_events_pagination_has_more(tmp_path):
    db = str(tmp_path / "h.db")
    # idle->printing->idle->printing->idle => 4 state-change events for p1.
    seq = ["idle", "printing", "idle", "printing", "idle"]
    base = 1000.0
    _seed(db, [("p1", "P1", s, None, None, base + i) for i, s in enumerate(seq)])

    # Page of 2 with a 5th probe row present => has_more True.
    page1 = api_main._query_events(db, 0, 100000, limit=2, offset=0)
    assert len(page1["rows"]) == 2
    assert page1["has_more"] is True

    # Final page drains the remaining 2; no probe row beyond => has_more False.
    page2 = api_main._query_events(db, 0, 100000, limit=2, offset=2)
    assert len(page2["rows"]) == 2
    assert page2["has_more"] is False

    # limit covering everything => has_more False and exactly 4 rows.
    allp = api_main._query_events(db, 0, 100000, limit=50, offset=0)
    assert len(allp["rows"]) == 4
    assert allp["has_more"] is False
    # Newest first.
    assert allp["rows"][0]["time"] >= allp["rows"][-1]["time"]


def test_events_use_current_registry_name_not_stored_label(tmp_path, monkeypatch):
    """A rename must show the current name in history, not the label frozen in
    the row at snapshot time. Old rows carry "P1", registry now says "P7"."""
    db = str(tmp_path / "h.db")
    seq = ["idle", "printing", "idle"]
    _seed(db, [("bambu-1", "P1", s, None, None, 1000.0 + i) for i, s in enumerate(seq)])

    monkeypatch.setattr(api_main, "_current_labels", lambda: {"bambu-1": "P7"})
    events = api_main._query_events(db, 0, 100000, limit=50, offset=0)
    assert events["rows"], "expected state-change events"
    assert all(r["label"] == "P7" for r in events["rows"])


def test_state_durations_merge_across_rename(tmp_path):
    """Grouping is by printer_id, not (printer_id, label): rows recorded under the
    old and new name for the same printer collapse into one entry, durations
    summed rather than one label's total clobbering the other's."""
    db = str(tmp_path / "h.db")
    _seed(db, [
        ("bambu-1", "P1", "printing", None, None, 1000.0),  # old name, 60s printing
        ("bambu-1", "P1", "idle", None, None, 1060.0),
        ("bambu-1", "P7", "printing", None, None, 1120.0),  # renamed, +60s printing
        ("bambu-1", "P7", "idle", None, None, 1180.0),
    ])
    printers = api_main._query_state_durations(db, 0, 1200.0)
    assert set(printers.keys()) == {"bambu-1"}, "one printer, not split by name"
    assert printers["bambu-1"]["states"]["printing"] == 120


def test_cached_state_durations_memoizes_within_ttl(tmp_path, monkeypatch):
    db = str(tmp_path / "h.db")
    _seed(db, [
        ("p1", "P1", "printing", None, None, 1000.0),
        ("p1", "P1", "idle", None, None, 1060.0),
    ])
    api_main._states_summary_cache.clear()

    calls = {"n": 0}
    real = api_main._query_state_durations

    def counting(d, fr, to):
        calls["n"] += 1
        return real(d, fr, to)

    monkeypatch.setattr(api_main, "_query_state_durations", counting)

    clock = {"v": 100.0}
    monkeypatch.setattr(api_main.time_module, "monotonic", lambda: clock["v"])

    first = api_main._cached_state_durations(db, 0, 100000)
    second = api_main._cached_state_durations(db, 0, 100000)
    assert calls["n"] == 1          # second call served from cache
    assert first == second

    # Past the TTL the window is recomputed.
    clock["v"] += api_main._STATES_SUMMARY_TTL + 1
    api_main._cached_state_durations(db, 0, 100000)
    assert calls["n"] == 2

    api_main._states_summary_cache.clear()


def test_init_db_creates_state_covering_index(tmp_path, monkeypatch):
    db = str(tmp_path / "ph.db")
    monkeypatch.setattr(state_store, "DB_PATH", db)
    store = state_store.StateStore()
    try:
        names = {
            r[0]
            for r in store._db.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
    finally:
        store._db.close()
    assert "idx_history_printer_time_state" in names
