"""Notification tracker (notifications spec §2.5, step 5): `since` = the first
time a key was seen, kept in printer_history.db across restarts; a key clears
only after FLAP_GRACE_S; rows are written on appear / clear only; offline and
grace statuses say nothing; the cancel flag outlives a restart."""
import sqlite3
from types import SimpleNamespace

import pytest

from app.domain.models import PrinterKind, PrinterState, PrinterStatus
from app.services import notification_tracker as nt
from app.services.normalizer import normalize_bambu

GRACE = nt.FLAP_GRACE_S
T0 = 1_760_000_000.0


def _n(key, seen_at=None, severity="serious", src="hms"):
    return {"key": key, "src": src, "code": key.split(":", 1)[1], "severity": severity,
            "since": seen_at, "seen_at": seen_at}


def _status(*items, pid="b1", state=PrinterState.PAUSED, online=True, grace=False):
    return PrinterStatus(id=pid, label="B1", kind=PrinterKind.BAMBU, online=online, state=state,
                         grace_period_active=grace, notifications=list(items))


def _rows(db, where="1=1"):
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT printer_id, nkey, src, code, severity, first_seen, cleared_at "
                            "FROM printer_notifications WHERE %s ORDER BY id" % where).fetchall()
    finally:
        conn.close()


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "ph.db")


@pytest.fixture
def make(db):
    made = []

    def factory():
        t = nt.NotificationTracker(db)
        made.append(t)
        return t

    yield factory
    for t in made:
        try:
            t.close()
        except Exception:
            pass


def test_since_is_the_first_seen_time_and_one_row_is_written(make, db):
    t = make()
    s = _status(_n("pe:0500_8062", seen_at=T0 - 30, severity="pause", src="print_error"))
    t.annotate(s, now=T0)
    assert s.notifications[0]["since"] == T0 - 30
    assert _rows(db) == [("b1", "pe:0500_8062", "print_error", "0500_8062", "pause", T0 - 30, None)]
    # the next polls: the same since, no new row
    for i in range(1, 4):
        s = _status(_n("pe:0500_8062", seen_at=T0 + 999, severity="pause", src="print_error"))
        t.annotate(s, now=T0 + 30 * i)
        assert s.notifications[0]["since"] == T0 - 30
    assert len(_rows(db)) == 1


def test_since_without_seen_at_or_from_the_future_is_now(make):
    t = make()
    s = _status(_n("hms:0700_2500_0002_0001"), _n("hms:0500_0600_0002_0070", seen_at=T0 + 500))
    t.annotate(s, now=T0)
    assert [n["since"] for n in s.notifications] == [T0, T0]


def test_first_seen_survives_a_restart(make, db):
    make().annotate(_status(_n("hms:0700_2500_0002_0001", seen_at=T0)), now=T0)
    # a new process: RawReport saw the code only now
    t2 = make()
    s = _status(_n("hms:0700_2500_0002_0001", seen_at=T0 + 3600))
    t2.annotate(s, now=T0 + 3600)
    assert s.notifications[0]["since"] == T0
    assert len(_rows(db)) == 1


def test_a_key_clears_only_after_the_grace(make, db):
    t = make()
    key = "pe:0700_8011"
    t.annotate(_status(_n(key, seen_at=T0, severity="pause", src="print_error")), now=T0)
    t.annotate(_status(), now=T0 + 30)                       # gone: first poll without it
    t.annotate(_status(), now=T0 + 30 + GRACE - 1)
    assert _rows(db)[0][-1] is None                          # still active
    t.annotate(_status(), now=T0 + 30 + GRACE)
    assert _rows(db)[0][-1] == T0 + 30                       # cleared at the first poll without it
    assert t.view("b1")["active"] == []
    # back later: a new episode with its own time
    s = _status(_n(key, seen_at=T0 + 1000, severity="pause", src="print_error"))
    t.annotate(s, now=T0 + 1000)
    assert s.notifications[0]["since"] == T0 + 1000
    assert [r[-1] for r in _rows(db)] == [T0 + 30, None]


def test_blinking_within_the_grace_keeps_since_and_writes_nothing(make, db):
    # The P2S fixture: the code vanishes and returns every 10-30 s.
    t = make()
    key = "hms:0500_0100_0003_0007"
    t.annotate(_status(_n(key, seen_at=T0)), now=T0)
    writes = t._db.total_changes
    now = T0
    for _ in range(20):
        now += 25
        t.annotate(_status(), now=now)
        now += 25
        s = _status(_n(key, seen_at=now))                    # RawReport may have restarted its own time
        t.annotate(s, now=now)
        assert s.notifications[0]["since"] == T0
    assert t._db.total_changes == writes
    assert _rows(db) == [("b1", key, "hms", "0500_0100_0003_0007", "serious", T0, None)]


@pytest.mark.parametrize("away", [dict(online=False, state=PrinterState.OFFLINE),
                                  dict(grace=True)])
def test_offline_and_grace_statuses_are_ignored(make, db, away):
    t = make()
    key = "hms:0700_2500_0002_0001"
    t.annotate(_status(_n(key, seen_at=T0)), now=T0)
    for i in range(1, 10):
        t.annotate(_status(**away), now=T0 + 60 * i)        # no notifications: nothing known
    assert _rows(db)[0][-1] is None
    s = _status(_n(key, seen_at=T0 + 600))
    t.annotate(s, now=T0 + 600)
    assert s.notifications[0]["since"] == T0
    # a grace status (the last good one cloned) is not touched
    stale = _n(key, seen_at=T0 + 700)
    t.annotate(_status(stale, grace=True), now=T0 + 700)
    assert stale["since"] == T0 + 700


def test_back_online_without_the_key_clears_it_after_the_grace(make, db):
    t = make()
    t.annotate(_status(_n("pe:0300_8007", seen_at=T0, severity="pause", src="print_error")), now=T0)
    t.annotate(_status(online=False, state=PrinterState.OFFLINE), now=T0 + 30)
    t.annotate(_status(state=PrinterState.IDLE), now=T0 + 7200)
    assert _rows(db)[0][-1] is None                          # the first report after a reconnect may be partial
    t.annotate(_status(state=PrinterState.IDLE), now=T0 + 7200 + GRACE)
    assert _rows(db)[0][-1] == T0 + 7200


def test_printers_are_tracked_apart(make):
    t = make()
    key = "hms:0700_2500_0002_0001"
    t.annotate(_status(_n(key, seen_at=T0), pid="h2"), now=T0)
    s = _status(_n(key, seen_at=T0 + 50), pid="h3")
    t.annotate(s, now=T0 + 50)
    assert s.notifications[0]["since"] == T0 + 50
    t.annotate(_status(pid="h3"), now=T0 + 60)
    t.annotate(_status(pid="h3"), now=T0 + 60 + GRACE)
    assert [a["key"] for a in t.view("h2")["active"]] == [key]
    assert t.view("h3")["active"] == []


def test_cancel_flag_survives_a_restart_and_clears_on_leaving(make, db):
    t = make()
    t.annotate(_status(state=PrinterState.CANCELLED), now=T0)
    assert t.flag("b1", nt.CANCELLED_FLAG) is True
    t.annotate(_status(state=PrinterState.CANCELLED), now=T0 + 30)
    assert len(_rows(db, "src = 'flag'")) == 1               # written once
    # restart; the printer is away for a while: the flag stays
    t2 = make()
    assert t2.flag("b1", nt.CANCELLED_FLAG) is True
    t2.annotate(_status(online=False, state=PrinterState.OFFLINE), now=T0 + 600)
    assert t2.flag("b1", nt.CANCELLED_FLAG) is True
    # a new job: the flag goes
    t2.annotate(_status(state=PrinterState.PRINTING), now=T0 + 900)
    assert t2.flag("b1", nt.CANCELLED_FLAG) is False
    assert _rows(db, "src = 'flag'")[0][-1] == T0 + 900
    assert make().flag("b1", nt.CANCELLED_FLAG) is False
    assert t2.view("b1")["flags"] == []


def test_cancel_flag_outlives_a_short_spell_without_the_state(make, db):
    # a reconnect's first report (gcode_state "unknown" -> IDLE) must not drop it
    t = make()
    t.annotate(_status(state=PrinterState.CANCELLED), now=T0)
    t2 = make()                                                # restart
    t2.annotate(_status(state=PrinterState.IDLE), now=T0 + 600)
    assert t2.flag("b1", nt.CANCELLED_FLAG) is True
    t2.annotate(_status(state=PrinterState.CANCELLED), now=T0 + 630)   # FAILED again: the spell is over
    t2.annotate(_status(state=PrinterState.IDLE), now=T0 + 700)
    t2.annotate(_status(state=PrinterState.IDLE), now=T0 + 700 + GRACE - 1)
    assert t2.flag("b1", nt.CANCELLED_FLAG) is True
    t2.annotate(_status(state=PrinterState.IDLE), now=T0 + 700 + GRACE)
    assert t2.flag("b1", nt.CANCELLED_FLAG) is False
    assert _rows(db, "src = 'flag'")[0][-1] == T0 + 700      # closed at the first status without it


def test_flags_do_not_count_as_notifications(make):
    t = make()
    t.annotate(_status(state=PrinterState.CANCELLED), now=T0)
    t2 = make()
    assert t2.view("b1") == {"active": [], "flags": [nt.CANCELLED_FLAG]}
    t2.annotate(_status(state=PrinterState.CANCELLED), now=T0 + 10 * GRACE)
    assert t2.flag("b1", nt.CANCELLED_FLAG) is True


def test_cleared_rows_past_retention_are_pruned(make, db, monkeypatch):
    monkeypatch.setattr(nt, "RETENTION_SECONDS", 1000)
    t = make()
    t.annotate(_status(_n("hms:a", seen_at=T0), _n("hms:b", seen_at=T0)), now=T0)
    t.annotate(_status(_n("hms:b")), now=T0 + 10)
    t.annotate(_status(_n("hms:b")), now=T0 + 10 + GRACE)   # hms:a cleared at T0 + 10
    t._last_prune = 0.0
    t.annotate(_status(_n("hms:b")), now=T0 + 10 + 1000 + 1)
    assert [r[1] for r in _rows(db)] == ["hms:b"]            # the active one stays, however old


def test_a_key_active_twice_in_the_db_keeps_its_earliest_row(make, db):
    make().close()                                           # creates the table
    conn = sqlite3.connect(db)
    conn.executemany("INSERT INTO printer_notifications (printer_id, nkey, src, code, severity, first_seen) "
                     "VALUES (?, ?, ?, ?, ?, ?)",
                     [("b1", "hms:x", "hms", "x", "serious", T0 + 5), ("b1", "hms:x", "hms", "x", "serious", T0)])
    conn.commit()
    conn.close()
    t = make()
    s = _status(_n("hms:x", seen_at=T0 + 100))
    t.annotate(s, now=T0 + 100)
    assert s.notifications[0]["since"] == T0
    assert len(_rows(db, "cleared_at IS NULL")) == 1


def test_a_storage_failure_costs_the_row_not_the_status(make, caplog):
    t = make()

    class _Broken:
        total_changes = 0

        def execute(self, *a, **k):
            raise sqlite3.OperationalError("disk I/O error")

        def commit(self):
            raise sqlite3.OperationalError("disk I/O error")

        def close(self):
            pass

    t._db = _Broken()
    s = _status(_n("hms:x", seen_at=T0), state=PrinterState.CANCELLED)
    t.annotate(s, now=T0)                                    # no raise
    assert s.notifications[0]["since"] == T0
    assert t.flag("b1", nt.CANCELLED_FLAG) is True           # in memory until the restart
    t.annotate(_status(), now=T0 + 10)
    t.annotate(_status(), now=T0 + 10 + GRACE)
    assert t.view("b1")["active"] == []
    assert "printer_notifications insert failed" in caplog.text


# ---- module level: init / annotate / flag ---------------------------------------

def test_module_without_init_does_nothing(monkeypatch):
    monkeypatch.setattr(nt, "_tracker", None)
    s = _status(_n("hms:x", seen_at=T0), state=PrinterState.CANCELLED)
    nt.annotate(s)
    assert s.notifications[0]["since"] == T0
    assert nt.flag("b1", nt.CANCELLED_FLAG) is False and nt.view("b1") is None


def test_module_annotate_never_raises(monkeypatch, caplog):
    class _Bad:
        def annotate(self, status, now=None):
            raise RuntimeError("boom")

    monkeypatch.setattr(nt, "_tracker", _Bad())
    nt.annotate(_status())
    assert "notification tracker failed for b1" in caplog.text


def test_init_failure_leaves_the_tracker_off(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(nt, "_tracker", None)
    assert nt.init(str(tmp_path / "no" / "such" / "dir" / "ph.db")) is None
    assert nt._tracker is None and "notification tracker not started" in caplog.text


def test_state_store_upsert_annotates_through_the_tracker(monkeypatch, tmp_path):
    from app.services import state_store

    db = str(tmp_path / "ph.db")
    monkeypatch.setattr(state_store, "DB_PATH", db)
    store = state_store.StateStore()
    monkeypatch.setattr(nt, "_tracker", None)
    tracker = nt.init(db)
    try:
        s = _status(_n("pe:0500_8062", seen_at=T0, severity="pause", src="print_error"))
        store.upsert(s)
        assert store.get_one("b1").notifications[0]["since"] == T0
        later = _status(_n("pe:0500_8062", seen_at=T0 + 300, severity="pause", src="print_error"))
        store.upsert(later)
        assert store.get_one("b1").notifications[0]["since"] == T0
        assert len(_rows(db)) == 1
    finally:
        tracker.close()
        store._db.close()


# ---- normalizer: the cancel flag after a restart ---------------------------------

def _failed(print_error):
    return SimpleNamespace(print_job=SimpleNamespace(gcode_state="FAILED", print_error=print_error))


def test_normalizer_keeps_a_cancel_across_a_restart_through_the_flag(make, monkeypatch):
    t = make()
    monkeypatch.setattr(nt, "_tracker", t)
    assert normalize_bambu("b1", "B1", _failed(0)).state == PrinterState.ERROR
    t.annotate(_status(state=PrinterState.CANCELLED), now=T0)    # it stood cancelled before the restart
    assert normalize_bambu("b1", "B1", _failed(0)).state == PrinterState.CANCELLED
    assert normalize_bambu("b2", "B2", _failed(0)).state == PrinterState.ERROR      # per printer
    # another print error is never hidden behind the flag
    assert normalize_bambu("b1", "B1", _failed(0x03004000)).state == PrinterState.ERROR


def test_normalizer_keeps_the_cancel_through_a_reconnects_unknown_report(make, monkeypatch):
    t = make()
    t.annotate(_status(state=PrinterState.CANCELLED), now=T0)
    t2 = make()                                                # Forge restarted: no latch in the new process
    monkeypatch.setattr(nt, "_tracker", t2)
    unknown = SimpleNamespace(print_job=SimpleNamespace(gcode_state="unknown", print_error=0))
    s = normalize_bambu("b1", "B1", unknown)                   # the lib's first message after the reconnect
    assert (s.online, s.state) == (True, PrinterState.IDLE)
    t2.annotate(s, now=T0 + 600)
    assert normalize_bambu("b1", "B1", _failed(0)).state == PrinterState.CANCELLED
