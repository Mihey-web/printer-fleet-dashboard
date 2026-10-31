"""Live print-job tracker: start / observe / restart (spec_job_journal §5, §11.2). Job names are invented."""
import logging
import time
from types import SimpleNamespace

import pytest

import main
from app.domain.models import PrinterKind, PrinterState, PrinterStatus
from app.services import job_journal as jj
from app.services import state_store as ss
from test_job_journal import NAME, NAME2, T0

PID = "b-live"


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = str(tmp_path / "ph.db")
    monkeypatch.setattr(ss, "DB_PATH", path)
    return path


@pytest.fixture
def store(db_path):
    st = ss.StateStore()
    yield st
    jj.stop()
    st._db.close()


@pytest.fixture(autouse=True)
def _stopped():
    jj.stop()
    yield
    jj.stop()


def st(state, pid=PID, kind=PrinterKind.BAMBU, **kw):
    """A polled status as the normalizers build it."""
    online = state != PrinterState.OFFLINE
    base = dict(id=pid, label="L", kind=kind, online=online, state=state, device_type="P2S")
    if state in (PrinterState.PRINTING, PrinterState.PAUSED, PrinterState.FINISHED):
        base["job_name"] = NAME
    base.update(kw)
    return PrinterStatus(**base)


def rows(store):
    return jj._dicts(store._db.execute("SELECT * FROM print_jobs ORDER BY id"))


def feed(statuses, t0=T0, step=30):
    """observe() each status `step` seconds apart; returns the time after the last one."""
    t = t0
    for s in statuses:
        jj.observe(s, t)
        t += step
    return t


def printing(n, p=(0, 50), eta=(3600, 1800), **kw):
    out = []
    for i in range(n):
        f = i / (n - 1) if n > 1 else 0
        out.append(st(PrinterState.PRINTING, progress_pct=p[0] + (p[1] - p[0]) * f,
                      eta_seconds=int(eta[0] + (eta[1] - eta[0]) * f), **kw))
    return out


def history(store, ts, state, pid=PID, grace=0):
    store._db.execute(
        "INSERT INTO printer_history (printer_id, label, kind, online, state, recorded_at, grace_period_active)"
        " VALUES (?, 'L', 'bambu', ?, ?, ?, ?)", (pid, int(state != "offline"), state, ts, grace))
    store._db.commit()


# --- observe ---
def test_observe_does_nothing_before_start(store):
    feed([st(PrinterState.IDLE)] + printing(3))
    assert rows(store) == [] and jj._machines == {}


def test_a_live_print_open_pause_finish(store, caplog):
    caplog.set_level(logging.INFO, logger=jj.__name__)
    jj.start(store, now=T0)
    t = feed([st(PrinterState.IDLE)] + printing(5, p=(0, 1), eta=(3600, 3560))
             + [st(PrinterState.PAUSED, progress_pct=1.0, eta_seconds=3560, stage="paused_user")] * 3
             + printing(5, p=(2, 99), eta=(3500, 60))
             + [st(PrinterState.FINISHED, progress_pct=100.0, eta_seconds=0), st(PrinterState.IDLE)])
    (row,) = rows(store)
    assert (row["result"], row["start_exact"], row["end_exact"], row["source"]) == ("finished", 1, 1, "live")
    assert (row["started_at"], row["ended_at"]) == (T0 + 30, t - 60)
    assert (row["pauses"], row["paused_sec"], row["planned_src"]) == (1, 90, "slicer")
    assert '"stage:paused_user"' in row["pauses_json"] and row["ctx"] is None
    assert (row["kind"], row["model"], row["job_name"]) == ("bambu", "P2S", NAME)
    assert "[jobs] %s: печать #%d начата" % (PID, row["id"]) in caplog.text
    assert "[jobs] %s: печать #%d — finished (" % (PID, row["id"]) in caplog.text


def test_offline_and_grace_statuses_do_not_end_a_print(store):
    jj.start(store, now=T0)
    t = feed([st(PrinterState.IDLE)] + printing(3, p=(10, 12)))
    grace = st(PrinterState.PRINTING, progress_pct=12.0, eta_seconds=1800, grace_period_active=True)
    t = feed([grace] * 3 + [st(PrinterState.OFFLINE)] * 3, t0=t)
    (row,) = rows(store)
    assert row["result"] is None and jj._machines[PID].job.last_seen == T0 + 90
    feed(printing(2, p=(13, 14)), t0=t)
    (row,) = rows(store)
    assert row["result"] is None and row["offline_n"] == 0   # 6 polls = 3 min: not a gap


def test_job_seq_splits_prints_the_polls_did_not_see_end(store):
    jj.start(store, now=T0)
    t = feed([st(PrinterState.IDLE, job_seq=4)] + printing(4, p=(40, 50), job_seq=4))
    # Between two polls the print ended and the next one started (same name, ~same progress).
    feed(printing(3, p=(1, 2), eta=(9000, 8900), job_seq=5, job_started_at=t - 12), t0=t)
    first, second = rows(store)
    assert first["result"] == "stopped" and second["result"] is None
    assert (second["started_at"], second["start_exact"]) == (t - 12, 1)


def test_maintenance_from_the_registry_marks_the_print(store, monkeypatch):
    from app.services import printer_registry
    monkeypatch.setattr(printer_registry, "in_maintenance", lambda pid: pid == PID, raising=False)
    jj.start(store, now=T0)
    feed([st(PrinterState.IDLE)] + printing(3) + [st(PrinterState.IDLE)])
    assert rows(store)[0]["maint"] == 1


def test_kinds_and_printers_get_their_own_machines(store):
    jj.start(store, now=T0)
    for i in range(4):
        jj.observe(st(PrinterState.PRINTING, progress_pct=10.0 + i, eta_seconds=900), T0 + 30 * i)
        jj.observe(st(PrinterState.PRINTING, pid="k1", kind=PrinterKind.CREALITY, device_type="k1c",
                      progress_pct=20.0 + i, eta_seconds=600, print_time_seconds=300 + 30 * i,
                      job_name=NAME2), T0 + 30 * i + 1)
    by_pid = {r["printer_id"]: r for r in rows(store)}
    assert set(by_pid) == {PID, "k1"}
    assert (by_pid["k1"]["kind"], by_pid["k1"]["model"], by_pid["k1"]["planned_src"]) == ("creality", "k1c", "printer")
    assert by_pid[PID]["start_exact"] == 0   # printing when first seen


# --- restart (spec §5.4) ---
def test_restart_continues_the_open_print(db_path):
    st1 = ss.StateStore()
    jj.start(st1, now=T0)
    statuses = [st(PrinterState.IDLE)] + printing(30, p=(0, 30), eta=(9000, 7000))   # 15 min: one checkpoint
    t = feed(statuses)
    for i in range(len(statuses)):
        history(st1, T0 + 30 * i, statuses[i].state.value)
    stored = rows(st1)[0]
    assert stored["result"] is None and stored["last_seen"] < t - 30   # the checkpoint lags
    jj.stop()
    st1._db.close()

    st2 = ss.StateStore()
    try:
        jj.start(st2, now=t + 90)   # a Pi restart: 2 min without polls
        m = jj._machines[PID]
        assert m.job.id == stored["id"] and m.job.last_seen == t - 30 and m.seq is None
        # The collectors reconnect: offline first, then the same print, job_seq from 0 again.
        t = feed([st(PrinterState.OFFLINE)] + printing(5, p=(31, 99), eta=(6900, 60), job_seq=0)
                 + [st(PrinterState.FINISHED, progress_pct=100.0, eta_seconds=0, job_seq=0)], t0=t + 90)
        (row,) = rows(st2)
        assert (row["id"], row["result"], row["offline_n"], row["start_exact"]) == (stored["id"], "finished", 0, 1)
    finally:
        jj.stop()
        st2._db.close()


def test_restart_after_a_day_without_data_is_lost(store, caplog):
    caplog.set_level(logging.INFO, logger=jj.__name__)
    jj.start(store, now=T0)
    t = feed([st(PrinterState.IDLE)] + printing(4))
    history(store, T0 + 90, "printing")   # the stored last_seen (T0 + 30, the open) lags; the history knows better
    jj.stop()
    jj.start(store, now=t + jj.JOBS_MAX_GAP_S + 60)
    (row,) = rows(store)
    assert (row["result"], row["ended_at"], row["end_exact"]) == ("lost", T0 + 90, 0)
    assert jj._machines[PID].job is None
    assert "открытых печатей после старта — 0" in caplog.text


def test_start_survives_a_broken_db(store, monkeypatch, caplog):
    def boom(self, now=None):
        raise RuntimeError(NAME)
    monkeypatch.setattr(jj.JournalDB, "load_open", boom)
    jj.start(store, now=T0)
    assert jj._ready and jj._machines == {}
    assert "RuntimeError" in caplog.text and NAME not in caplog.text


# --- failures never reach the poll loop, names never reach the log ---
def test_an_unexpected_error_is_logged_without_the_name_and_the_print_goes_on(store, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    jj.start(store, now=T0)
    t = feed([st(PrinterState.IDLE)] + printing(3, p=(10, 12)))
    opened = rows(store)[0]["id"]
    real = jj.JobMachine.feed
    calls = []

    def flaky(self, s):
        calls.append(1)
        if len(calls) <= 2:
            raise ValueError("could not use %r" % s.name)   # a message quoting the job name
        return real(self, s)
    monkeypatch.setattr(jj.JobMachine, "feed", flaky)
    t = feed(printing(2, p=(13, 14)), t0=t)                 # never raises
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1 and "ValueError" in errors[0].getMessage()   # once per place, then DEBUG
    assert jj._machines[PID].job.id == opened               # reloaded from the stored open print
    feed(printing(3, p=(15, 99), eta=(1500, 60)) + [st(PrinterState.FINISHED, progress_pct=100.0)], t0=t)
    assert [(r["id"], r["result"]) for r in rows(store)] == [(opened, "finished")]
    for rec in caplog.records:
        assert NAME not in rec.getMessage() + (rec.exc_text or "")


def test_no_job_name_in_tracker_logs(store, caplog):
    caplog.set_level(logging.DEBUG)
    jj.start(store, now=T0)
    t = feed([st(PrinterState.IDLE)] + printing(4, p=(90, 99), eta=(600, 0))
             + printing(3, p=(0, 5), eta=(25000, 24000), job_name=NAME2)                       # a boundary
             + [st(PrinterState.IDLE, job_name=NAME2)] + printing(2, p=(6, 7), job_name=NAME2)  # a blip
             + [st(PrinterState.ERROR, last_error="Ошибка печати 0300_400C: Печать отменена", job_name=NAME2)])
    jj.stop()
    jj.start(store, now=t + 10)
    assert [r["result"] for r in rows(store)] == ["finished", "cancelled"]
    assert "печать #2 продолжается" in caplog.text
    for rec in caplog.records:
        text = rec.getMessage() + (rec.exc_text or "")
        assert NAME not in text and NAME2 not in text


# --- through the real poll loop ---
def test_main_starts_the_journal_before_the_poll_loop():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
    body = src[src.index("def main():"):]
    assert body.index("auto_ignore.start(store)") < body.index("job_journal.start(store)") < body.index("poll_loop(")


def test_poll_loop_writes_the_journal(store, monkeypatch):
    states = iter(["printing", "printing", "paused", "printing", "finish", "idle"])
    cycles = []

    class _Collector:
        def fetch(self):
            state = next(states)
            return {"state": state, "progress": 100 if state == "finish" else 40 + len(cycles),
                    "remaining_time": 600, "print_time": 60 * (len(cycles) + 1), "filename": NAME}

    class _Done(Exception):
        pass

    def _sleep(_seconds):
        cycles.append(1)
        if len(cycles) == 6:
            raise _Done

    monkeypatch.setattr(main, "time", SimpleNamespace(time=time.time, monotonic=time.monotonic, sleep=_sleep))
    jj.start(store)
    with pytest.raises(_Done):
        main.poll_loop(store, [("mks-1", PrinterKind.MKS, "MKS", _Collector(), "mks")])
    (row,) = rows(store)
    assert (row["printer_id"], row["kind"], row["result"], row["pauses"]) == ("mks-1", "mks", "finished", 1)
    assert row["job_name"] == NAME and row["planned_src"] == "linear"
