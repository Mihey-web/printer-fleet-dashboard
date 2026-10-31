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
    """Create a minimal printer_history table and insert (pid,label,state,job,err,ts[,reason]) rows."""
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE printer_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            printer_id TEXT, label TEXT, state TEXT,
            job_name TEXT, last_error TEXT, recorded_at REAL,
            progress REAL, eta_sec REAL, print_time REAL,
            nozzle_temp REAL, bed_temp REAL, reason TEXT
        )
        """
    )
    conn.executemany(
        "INSERT INTO printer_history (printer_id, label, state, job_name, last_error, recorded_at, reason)"
        " VALUES (?,?,?,?,?,?,?)",
        [tuple(r) + (None,) * (7 - len(r)) for r in rows],
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


def test_query_timeline_decimates_but_keeps_state_changes(tmp_path, monkeypatch):
    """Плотные 5-секундные снапшоты прореживаются до бакетов, но каждая смена
    состояния и края окна каждого принтера сохраняются точно."""
    db = str(tmp_path / "h.db")
    base = 1000.0
    rows = []
    # 600с idle каждые 5с, одна смена на printing в середине, дальше printing.
    for i in range(120):
        state = "idle" if i < 60 else "printing"
        rows.append(("p1", "P1", state, None, None, base + i * 5.0))
    _seed(db, rows)
    monkeypatch.setattr(api_main, "_current_labels", lambda: {})

    res = api_main._query_timeline(db, 0, 100000, limit=5000, offset=0)
    out = res["rows"]
    # Сильно меньше сырых 120 строк (окно огромное => step большой, остаются
    # только края и смена состояния).
    assert len(out) < 20
    assert res["has_more"] is False
    ts = [r["recorded_at"] for r in out]
    assert ts == sorted(ts)
    # Края окна принтера на месте.
    assert out[0]["recorded_at"] == base
    assert out[-1]["recorded_at"] == base + 119 * 5.0
    # Первая printing-строка (точная граница сегмента) сохранена.
    assert any(r["state"] == "printing" and r["recorded_at"] == base + 60 * 5.0
               for r in out)
    # Узкое окно => step упирается в минимум 30с, опорных точек больше.
    res_narrow = api_main._query_timeline(db, base, base + 600, limit=5000, offset=0)
    assert len(res_narrow["rows"]) >= 600 / 30
    assert len(res_narrow["rows"]) < 120


def test_query_timeline_pagination_and_desc(tmp_path, monkeypatch):
    db = str(tmp_path / "h.db")
    base = 1000.0
    # Каждая строка меняет state => прореживание ничего не выкидывает.
    states = ["idle", "printing"] * 10
    _seed(db, [("p1", "P1", s, None, None, base + i) for i, s in enumerate(states)])
    monkeypatch.setattr(api_main, "_current_labels", lambda: {"p1": "P9"})

    page1 = api_main._query_timeline(db, 0, 100000, limit=8, offset=0, desc=True)
    assert len(page1["rows"]) == 8
    assert page1["has_more"] is True
    # desc: новейшие первыми.
    assert page1["rows"][0]["recorded_at"] > page1["rows"][-1]["recorded_at"]
    # Имя — из реестра, не из замороженного label.
    assert all(r["label"] == "P9" for r in page1["rows"])

    page3 = api_main._query_timeline(db, 0, 100000, limit=8, offset=16, desc=True)
    assert len(page3["rows"]) == 4
    assert page3["has_more"] is False

    # Страницы не пересекаются и вместе дают все 20 строк.
    page2 = api_main._query_timeline(db, 0, 100000, limit=8, offset=8, desc=True)
    all_ts = {r["recorded_at"] for p in (page1, page2, page3) for r in p["rows"]}
    assert len(all_ts) == 20


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


def test_state_durations_do_not_stretch_a_state_over_a_history_gap(tmp_path):
    """07–11.10.2026 the history was not written for ~5 days (dying SSD); the last
    state before the gap used to fill all of it — "printing" or "offline" for days.
    A state now lasts at most HISTORY_GAP_CAP_S past its row; the rest is "nodata"."""
    db = str(tmp_path / "h.db")
    cap = api_main.HISTORY_GAP_CAP_S
    _seed(db, [
        ("p1", "P1", "printing", None, None, 1000.0),
        ("p1", "P1", "printing", None, None, 1080.0),      # normal cadence: 80 s
        ("p1", "P1", "idle", None, None, 1160.0 + 5 * 86400),   # 5 days without rows
        ("p1", "P1", "idle", None, None, 1240.0 + 5 * 86400),
    ])
    st = api_main._query_state_durations(db, 0, 1240.0 + 5 * 86400)["p1"]["states"]
    gap = 1160.0 + 5 * 86400 - 1080.0
    assert st["printing"] == 80 + cap
    assert st["nodata"] == int(gap - cap)
    assert st["idle"] == 80
    # a printer whose rows stopped long ago: its last state does not run up to `to`
    st = api_main._query_state_durations(db, 0, 1240.0 + 6 * 86400)["p1"]["states"]
    assert st["idle"] == 80 + cap


def test_states_summary_passes_nodata_through():
    import inspect
    assert '"nodata"' in inspect.getsource(api_main.create_app)


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


def test_init_db_creates_the_print_jobs_journal(tmp_path, monkeypatch):
    db = str(tmp_path / "ph.db")
    monkeypatch.setattr(state_store, "DB_PATH", db)
    store = state_store.StateStore()
    try:
        names = {r[0] for r in store._db.execute("SELECT name FROM sqlite_master").fetchall()}
    finally:
        store._db.close()
    assert {"print_jobs", "job_journal_meta", "idx_jobs_one_open", "idx_jobs_started",
            "idx_jobs_printer_started"} <= names


def test_cancelled_state_in_events_filter_and_states_summary(tmp_path, monkeypatch):
    """A cancelled job is its own state in the history: the events filter accepts
    it (it answered 400 for an unknown state) and the states-summary reports its
    duration instead of dropping it silently."""
    from fastapi.testclient import TestClient

    import config as cfg
    from app.api import auth as api_auth
    from app.domain.user_models import UserRole
    from app.services import auth_service

    db = str(tmp_path / "h.db")
    _seed(db, [
        ("p1", "P1", "printing", None, None, 1000.0),
        ("p1", "P1", "cancelled", None, None, 1060.0),
        ("p1", "P1", "idle", None, None, 1180.0),
    ])
    monkeypatch.setattr(api_main, "_history_db_path", lambda: db)
    monkeypatch.setattr(api_main, "_current_labels", lambda rows=None: {})
    monkeypatch.setattr(cfg, "JWT_SECRET", "test-secret", raising=False)
    monkeypatch.setattr(auth_service, "get_user_from_token", lambda *a, **k: ("viewer", UserRole.VIEWER))
    monkeypatch.setattr(auth_service, "decode_token", lambda *a, **k: {"iat": 0})
    monkeypatch.setattr(api_auth, "token_subject_active", lambda *a, **k: True)
    api_main._states_summary_cache.clear()

    class _Store:
        def get_all(self):
            return []

        def get_one(self, pid):
            return None

    client = TestClient(api_main.create_app(_Store()))
    client.cookies.set("access_token", "stub")
    try:
        r = client.get("/api/history/events", params={"fr": 0, "to": 2000, "state": "cancelled"})
        assert r.status_code == 200
        assert [(e["old_state"], e["new_state"]) for e in r.json()["rows"]] == [("printing", "cancelled")]
        r = client.get("/api/history/states-summary", params={"fr": 0, "to": 1200})
        assert r.status_code == 200
        [p1] = r.json()
        assert p1["states"]["cancelled"] == 120 and p1["states"]["printing"] == 60
    finally:
        api_main._states_summary_cache.clear()


# --- reason: why a printer paused / errored / was cancelled (step 5) -------------

_OLD_SCHEMA = """
    CREATE TABLE printer_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        printer_id TEXT NOT NULL, label TEXT NOT NULL, kind TEXT NOT NULL,
        online INTEGER NOT NULL, state TEXT NOT NULL, progress REAL, job_name TEXT,
        eta_sec INTEGER, print_time INTEGER, nozzle_temp REAL, bed_temp REAL,
        layer INTEGER, total_layer INTEGER, last_error TEXT, device_type TEXT,
        recorded_at REAL NOT NULL,
        grace_period_active INTEGER DEFAULT 0, last_successful_fetch REAL DEFAULT 0
    )
"""


def _store(monkeypatch, db):
    monkeypatch.setattr(state_store, "DB_PATH", db)
    return state_store.StateStore()


def test_init_db_adds_the_reason_column_to_an_old_table_in_place(tmp_path, monkeypatch):
    db = str(tmp_path / "ph.db")
    conn = sqlite3.connect(db)
    conn.execute(_OLD_SCHEMA)
    conn.executemany("INSERT INTO printer_history (printer_id, label, kind, online, state, last_error, recorded_at)"
                     " VALUES (?,?,?,?,?,?,?)",
                     [("p1", "P1", "bambu", 1, "paused", "Ошибка печати 0500_8062: x", 1000.0 + i) for i in range(3)])
    conn.commit()
    conn.close()
    for _ in range(2):                                   # idempotent on the next start
        store = _store(monkeypatch, db)
        cols = [r[1] for r in store._db.execute("PRAGMA table_info(printer_history)").fetchall()]
        rows = store._db.execute("SELECT id, state, last_error, reason FROM printer_history ORDER BY id").fetchall()
        store._db.close()
        assert cols.count("reason") == 1 and cols[-1] == "reason"     # appended: ALTER, not a rebuilt table
        assert rows == [(i + 1, "paused", "Ошибка печати 0500_8062: x", None) for i in range(3)]


def test_record_snapshot_writes_the_reason(tmp_path, monkeypatch):
    from app.domain.models import PrinterKind, PrinterState, PrinterStatus
    from app.services import notifications

    store = _store(monkeypatch, str(tmp_path / "ph.db"))
    pause = {"key": "pe:0500_8062", "src": "print_error", "code": "0500_8062", "severity": "pause",
             "text": "Маркер стола не найден"}
    lesser_hms = {"key": "hms:0700_2500_0002_0001", "src": "hms", "code": "0700_2500_0002_0001",
                  "severity": "serious", "text": "Сушка от питания"}

    def status(state, *notifs, stage=None):
        return PrinterStatus(id="p1", label="P1", kind=PrinterKind.BAMBU, online=True, state=state,
                             stage=stage, notifications=list(notifs))

    try:
        store.record_snapshot(status(PrinterState.PAUSED, lesser_hms, pause))
        store.record_snapshot(status(PrinterState.PRINTING, lesser_hms))
        store.record_snapshot(status(PrinterState.PAUSED, lesser_hms, stage="filament_runout"))
        monkeypatch.setattr(notifications, "top_reason", lambda s: 1 / 0)
        store.record_snapshot(status(PrinterState.PAUSED, pause))     # a broken reason: the row still goes in
        got = [r[0] for r in store._db.execute("SELECT reason FROM printer_history ORDER BY id").fetchall()]
    finally:
        store._db.close()
    assert got == ["0500_8062: Маркер стола не найден", None, "stage:filament_runout", None]


def test_query_events_carries_the_reason(tmp_path):
    db = str(tmp_path / "h.db")
    _seed(db, [
        ("p1", "P1", "printing", None, None, 1000.0),
        ("p1", "P1", "paused", None, "Ошибка печати 0500_8062: x", 1060.0, "0500_8062: Маркер стола не найден"),
        ("p1", "P1", "printing", None, None, 1120.0),
    ])
    rows = api_main._query_events(db, 0, 100000, limit=50, offset=0)["rows"]
    assert [(r["new_state"], r["reason"]) for r in rows] == [
        ("printing", None), ("paused", "0500_8062: Маркер стола не найден")]


# --- maintenance mode: 'maintenance' history rows (spec_maintenance §3) ----------

def test_history_state_hook_writes_maintenance_rows_and_falls_back_to_the_live_state(tmp_path, monkeypatch):
    from app.domain.models import MAINTENANCE_STATE, PrinterKind, PrinterState, PrinterStatus

    hook = {"fn": lambda pid: MAINTENANCE_STATE if pid == "p1" else None}
    monkeypatch.setattr(state_store, "DB_PATH", str(tmp_path / "ph.db"))
    store = state_store.StateStore(history_state=lambda pid: hook["fn"](pid))
    pause = {"key": "pe:0500_8062", "src": "print_error", "code": "0500_8062", "severity": "pause",
             "text": "Маркер стола не найден"}

    def status(pid, state, online=True):
        return PrinterStatus(id=pid, label=pid.upper(), kind=PrinterKind.BAMBU, online=online, state=state,
                             nozzle_temp=215.0, progress_pct=40.0, notifications=[pause])

    try:
        store.record_snapshot(status("p1", PrinterState.PAUSED))
        store.record_snapshot(status("p1", PrinterState.OFFLINE, online=False))
        store.record_snapshot(status("p2", PrinterState.PAUSED))           # not in maintenance
        hook["fn"] = lambda pid: 1 / 0
        store.record_snapshot(status("p1", PrinterState.PAUSED))           # a broken hook: the live state
        hook["fn"] = lambda pid: None
        store.record_snapshot(status("p1", PrinterState.PRINTING))
        got = store._db.execute("SELECT printer_id, state, online, reason, nozzle_temp, progress"
                                " FROM printer_history ORDER BY id").fetchall()
    finally:
        store._db.close()
    reason = "0500_8062: Маркер стола не найден"
    assert got == [
        ("p1", "maintenance", 1, None, 215.0, 40.0),     # telemetry stays, the reason does not
        ("p1", "maintenance", 0, None, 215.0, 40.0),     # the link is still in `online`
        ("p2", "paused", 1, reason, 215.0, 40.0),
        ("p1", "paused", 1, reason, 215.0, 40.0),
        ("p1", "printing", 1, None, 215.0, 40.0),
    ]


def test_state_store_without_a_hook_writes_the_live_state(tmp_path, monkeypatch):
    from app.domain.models import PrinterKind, PrinterState, PrinterStatus

    store = _store(monkeypatch, str(tmp_path / "ph.db"))
    try:
        store.record_snapshot(PrinterStatus(id="p1", label="P1", kind=PrinterKind.MKS, online=True,
                                            state=PrinterState.IDLE))
        got = store._db.execute("SELECT state FROM printer_history").fetchall()
    finally:
        store._db.close()
    assert got == [("idle",)]


def test_main_wires_the_maintenance_hook(monkeypatch):
    import inspect

    import main
    from app.services import printer_registry

    assert "StateStore(history_state=_history_state)" in inspect.getsource(main.main)
    monkeypatch.setattr(printer_registry, "in_maintenance", lambda pid: pid == "k1")
    assert (main._history_state("k1"), main._history_state("b1")) == ("maintenance", None)
    assert main._in_maintenance("k1") is True

    def broken(pid):
        raise RuntimeError("registry down")

    monkeypatch.setattr(printer_registry, "in_maintenance", broken)
    assert main._in_maintenance("k1") is False and main._history_state("k1") is None


def test_state_durations_give_maintenance_its_own_bucket(tmp_path):
    db = str(tmp_path / "h.db")
    _seed(db, [("p1", "P1", s, None, None, 1000.0 + 60 * i)
               for i, s in enumerate(["offline", "maintenance", "maintenance", "maintenance", "idle"])])
    st = api_main._query_state_durations(db, 0, 1300.0)["p1"]["states"]
    assert st == {"offline": 60, "maintenance": 180, "idle": 60}


def test_query_events_skip_maintenance_rows(tmp_path):
    """offline, maintenance×N, printing: one event offline → printing at the first
    printing row; entering and leaving the mode are no events (spec §3.3)."""
    db = str(tmp_path / "h.db")
    _seed(db, [("p1", "P1", "offline", None, None, 1000.0)]
          + [("p1", "P1", "maintenance", None, None, 1060.0 + 60 * i) for i in range(5)]
          + [("p1", "P1", "printing", None, None, 1360.0), ("p1", "P1", "printing", None, None, 1420.0),
             # printing before the mode, idle after: the event says it came back idle
             ("p2", "P2", "printing", None, None, 1000.0), ("p2", "P2", "maintenance", None, None, 1060.0),
             ("p2", "P2", "idle", None, None, 1120.0)])
    rows = api_main._query_events(db, 0, 100000, limit=50, offset=0)["rows"]
    assert sorted((r["printer_id"], r["old_state"], r["new_state"], r["time"]) for r in rows) == [
        ("p1", "offline", "printing", 1360.0), ("p2", "printing", "idle", 1120.0)]
    assert api_main._query_events(db, 0, 100000, limit=50, offset=0, printer_id="p1")["rows"][0]["time"] == 1360.0
    # a window that starts inside the mode has nothing to compare with
    assert api_main._query_events(db, 1100.0, 100000, limit=50, offset=0, printer_id="p1")["rows"] == []


def test_query_timeline_keeps_the_maintenance_segment_bounds(tmp_path, monkeypatch):
    db = str(tmp_path / "h.db")
    base = 1000.0
    states = ["idle"] * 40 + ["maintenance"] * 40 + ["offline"] * 40
    _seed(db, [("p1", "P1", s, None, None, base + i * 5.0) for i, s in enumerate(states)])
    monkeypatch.setattr(api_main, "_current_labels", lambda: {})
    out = api_main._query_timeline(db, 0, 100000, limit=5000, offset=0)["rows"]
    assert len(out) < 20
    firsts = [(r["state"], r["recorded_at"]) for i, r in enumerate(out) if i == 0 or out[i - 1]["state"] != r["state"]]
    assert firsts == [("idle", base), ("maintenance", base + 200.0), ("offline", base + 400.0)]


def test_states_summary_reports_maintenance_and_the_current_flag(tmp_path, monkeypatch):
    import inspect

    from fastapi.testclient import TestClient

    import config as cfg
    from app.api import auth as api_auth
    from app.domain.models import PrinterKind, PrinterState, PrinterStatus
    from app.domain.user_models import UserRole
    from app.services import auth_service

    assert '"maintenance"' in inspect.getsource(api_main.create_app)
    db = str(tmp_path / "h.db")
    _seed(db, [
        ("k1", "K1", "offline", None, None, 1000.0), ("k1", "K1", "maintenance", None, None, 1060.0),
        ("b1", "B1", "printing", None, None, 1000.0),
    ])
    monkeypatch.setattr(api_main, "_history_db_path", lambda: db)
    monkeypatch.setattr(api_main, "_registry_rows", lambda: [
        {"id": "k1", "label": "Varan", "maintenance_since": 1060.0, "maintenance_note": None, "maintenance_by": "admin"},
        {"id": "b1", "label": "P1", "maintenance_since": None}])
    monkeypatch.setattr(cfg, "JWT_SECRET", "test-secret", raising=False)
    monkeypatch.setattr(auth_service, "get_user_from_token", lambda *a, **k: ("viewer", UserRole.VIEWER))
    monkeypatch.setattr(auth_service, "decode_token", lambda *a, **k: {"iat": 0})
    monkeypatch.setattr(api_auth, "token_subject_active", lambda *a, **k: True)
    api_main._states_summary_cache.clear()

    class _Store:
        def get_all(self):
            return [PrinterStatus(id="k1", label="x", kind=PrinterKind.KLIPPER, online=False,
                                  state=PrinterState.OFFLINE)]

        def get_one(self, pid):
            return None

    client = TestClient(api_main.create_app(_Store()))
    client.cookies.set("access_token", "stub")
    try:
        r = client.get("/api/history/states-summary", params={"fr": 0, "to": 1200})
        assert r.status_code == 200
        got = {p["printer_id"]: p for p in r.json()}
        assert got["k1"]["states"] == {"offline": 60, "maintenance": 140}
        assert (got["k1"]["label"], got["k1"]["current_state"], got["k1"]["maintenance"]) == ("Varan", "offline", True)
        assert (got["b1"]["maintenance"], got["b1"]["current_state"]) == (False, "unknown")
    finally:
        api_main._states_summary_cache.clear()
