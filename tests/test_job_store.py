"""print_jobs storage (spec_job_journal §3, §5.3, §5.4, §11.2). Job names are invented."""
import logging
import sqlite3

import pytest

from app.domain.models import PrinterKind, PrinterState, PrinterStatus
from app.services import job_journal as jj
from app.services import state_store as ss
from app.services.job_journal import JobMachine, JournalDB, Sample
from test_job_journal import NAME, NAME2, PID, T0, err, gap, printing, seg, seq


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = str(tmp_path / "ph.db")
    monkeypatch.setattr(ss, "DB_PATH", path)
    return path


@pytest.fixture
def store(db_path):
    st = ss.StateStore()
    yield st
    st._db.close()


def _rows(store):
    return jj._dicts(store._db.execute("SELECT * FROM print_jobs ORDER BY id"))


def _cols(row):
    return {k: v for k, v in row.items() if k in jj.COLUMNS}


def drive(jdb, samples, m=None, kind="bambu", jobs=None):
    """Feed samples to a machine and write every batch of ops through JournalDB."""
    m = m or JobMachine(PID, kind=kind)
    jobs = [] if jobs is None else jobs
    for s in samples:
        ops = m.feed(s)
        jobs.extend(op.job for op in ops if op.op == "open")
        jdb.apply(m, ops, now=s.ts)
    return m, jobs


def check(store, jobs):
    """The DB holds exactly what the machine says: closed rows in full, open ones as last saved."""
    rows = {r["id"]: r for r in _rows(store)}
    assert sorted(rows) == sorted(j.id for j in jobs)
    for job in jobs:
        want = job.row() if job.result is not None else job._emitted
        assert _cols(rows[job.id]) == want
    assert sum(1 for r in rows.values() if r["result"] is None) <= 1
    return [rows[j.id] for j in jobs]


def _history(store, pid, ts, state, grace=0):
    store._db.execute(
        "INSERT INTO printer_history (printer_id, label, kind, online, state, recorded_at, grace_period_active)"
        " VALUES (?, 'L', 'bambu', ?, ?, ?, ?)", (pid, int(state != "offline"), state, ts, grace))
    store._db.commit()


# --- schema / migration ---
OLD_HISTORY = """
    CREATE TABLE printer_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT, printer_id TEXT NOT NULL, label TEXT NOT NULL,
        kind TEXT NOT NULL, online INTEGER NOT NULL, state TEXT NOT NULL, progress REAL,
        job_name TEXT, eta_sec INTEGER, print_time INTEGER, nozzle_temp REAL, bed_temp REAL,
        layer INTEGER, total_layer INTEGER, last_error TEXT, device_type TEXT, recorded_at REAL NOT NULL)
"""


def _schema(path):
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY type, name").fetchall()
    finally:
        conn.close()


def test_init_on_an_old_history_db_creates_the_journal_once(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute(OLD_HISTORY)
    conn.execute("INSERT INTO printer_history (printer_id, label, kind, online, state, recorded_at)"
                 " VALUES ('p1', 'P1', 'bambu', 1, 'printing', ?)", (T0,))
    conn.commit()
    conn.close()

    st = ss.StateStore()
    try:
        cols = [r[1] for r in st._db.execute("PRAGMA table_info(print_jobs)")]
        assert cols == [name for name, _ in jj._JOBS_TABLE]
        idx = {r[1]: r for r in st._db.execute("PRAGMA index_list(print_jobs)")}
        assert {"idx_jobs_started", "idx_jobs_printer_started", "idx_jobs_one_open", "idx_jobs_ended"} <= set(idx)
        assert idx["idx_jobs_one_open"][2] == 1 and idx["idx_jobs_one_open"][4] == 1   # unique, partial
        assert st._db.execute("SELECT COUNT(*) FROM job_journal_meta").fetchone()[0] == 0
        assert st._db.execute("SELECT COUNT(*) FROM printer_history").fetchone()[0] == 1
    finally:
        st._db.close()
    before = _schema(db_path)
    ss.StateStore()._db.close()   # a second start changes nothing
    assert _schema(db_path) == before


def test_init_adds_columns_a_newer_version_knows(store):
    conn = store._db
    conn.execute("DROP TABLE print_jobs")
    conn.execute("CREATE TABLE print_jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, printer_id TEXT NOT NULL,"
                 " kind TEXT NOT NULL, started_at REAL NOT NULL, last_seen REAL NOT NULL, result TEXT,"
                 " source TEXT NOT NULL, updated_at REAL NOT NULL)")
    conn.execute("INSERT INTO print_jobs (printer_id, kind, started_at, last_seen, result, source, updated_at)"
                 " VALUES ('p1', 'bambu', ?, ?, 'finished', 'live', ?)", (T0, T0 + 60, T0 + 60))
    conn.commit()
    jj.init_db(conn)
    assert {r[1] for r in conn.execute("PRAGMA table_info(print_jobs)")} == {n for n, _ in jj._JOBS_TABLE}
    row = _rows(store)[0]
    assert (row["result"], row["blips"], row["maint"], row["pauses"], row["verdict"]) == ("finished", 0, 0, 0, None)
    assert "idx_jobs_one_open" in {r[1] for r in conn.execute("PRAGMA index_list(print_jobs)")}


def test_journal_init_failure_does_not_break_the_history(db_path, monkeypatch, caplog):
    def boom(conn):
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(jj, "init_db", boom)
    st = ss.StateStore()
    try:
        st.record_snapshot(PrinterStatus(id="p1", label="P1", kind=PrinterKind.BAMBU, online=True,
                                         state=PrinterState.IDLE))
        assert st._db.execute("SELECT COUNT(*) FROM printer_history").fetchone()[0] == 1
    finally:
        st._db.close()
    assert "print_jobs init failed" in caplog.text


def test_one_open_row_per_printer_closed_ones_are_unlimited(store):
    row = dict(JobMachine(PID, kind="bambu").feed(Sample(ts=T0, state="printing", kind="bambu"))[0].fields)
    for i in range(3):
        jj.insert_job(store._db, dict(row, result="finished", ctx=None), T0)
    jj.insert_job(store._db, row, T0)
    jj.insert_job(store._db, dict(row, printer_id="other"), T0)
    with pytest.raises(sqlite3.IntegrityError):
        jj.insert_job(store._db, row, T0)
    store._db.commit()
    assert len(_rows(store)) == 5


def test_meta_round_trip(store):
    assert store.db_write(lambda c: jj.write_meta(c, {"backfill_version": 1, "backfill_done_at": T0}))
    assert jj.read_meta(store._db, "backfill_version") == "1"
    store.db_write(lambda c: jj.write_meta(c, {"backfill_version": 2}))
    assert jj.read_meta(store._db, "backfill_version") == "2"
    assert jj.read_meta(store._db, "missing") is None


# --- db_write ---
def test_db_write_commits_and_never_raises(store, caplog):
    assert store.db_write(lambda c: _history_rows(c, 1)) is True
    assert store._db.execute("SELECT COUNT(*) FROM printer_history").fetchone()[0] == 1

    def half_then_fail(conn):
        _history_rows(conn, 2)
        raise sqlite3.OperationalError("database is locked")
    assert store.db_write(half_then_fail) is False   # logged, rolled back
    assert store._db.execute("SELECT COUNT(*) FROM printer_history").fetchone()[0] == 1
    assert "db_write failed" in caplog.text
    assert store.db_write(lambda c: 1 / 0) is False
    assert store.db_write(lambda c: _history_rows(c, 1)) is True   # the connection still works
    assert store._db.execute("SELECT COUNT(*) FROM printer_history").fetchone()[0] == 2


def _history_rows(conn, n):
    conn.executemany("INSERT INTO printer_history (printer_id, label, kind, online, state, recorded_at)"
                     " VALUES ('p1', 'P1', 'bambu', 1, 'idle', ?)", [(T0 + i,) for i in range(n)])


# --- JournalDB: ops through real SQLite ---
SCENARIOS = {
    "finished": lambda: seq(seg(2, "idle"), printing(20, p=(0, 99), e=(3600, 60)),
                            seg(1, "finished", progress=100, eta=0, name=NAME), seg(2, "idle")),
    "pause": lambda: seq(seg(1, "idle"), printing(5, p=(0, 20)),
                         seg(3, "paused", progress=20, eta=3000, name=NAME,
                             **err("Ошибка печати 0700_8011: Пруток закончился")),
                         printing(5, p=(21, 99)), seg(1, "finished", progress=100, eta=0, name=NAME)),
    "late_cancel_amend": lambda: seq(seg(1, "idle"), printing(5, p=(0, 30)),
                                     seg(1, "error", progress=30, name=NAME),
                                     seg(2, "error", progress=30, name=NAME,
                                         **err("Ошибка печати 0300_400C: Печать отменена"))),
    "idle_blip_reopen": lambda: seq(seg(1, "idle"), printing(6, p=(10, 40), e=(5000, 4000)),
                                    seg(1, "idle", name=NAME), printing(6, p=(41, 99), e=(3900, 60)),
                                    seg(1, "finished", progress=100, eta=0, name=NAME)),
    "back_to_back": lambda: seq(seg(1, "idle"), printing(5, p=(90, 99), e=(600, 0)),
                                printing(5, p=(0, 10), e=(25000, 23000), name=NAME2)),
    "offline_lost": lambda: seq(seg(1, "idle"), printing(5, p=(0, 20)), seg(19, "offline", online=False),
                                seg(1, "idle")),
    "day_gap_lost": lambda: seq(seg(1, "idle"), printing(3), gap(90000), seg(1, "offline", online=False)),
}


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_ops_land_in_sqlite_exactly(store, name):
    m, jobs = drive(JournalDB(store), SCENARIOS[name]())
    rows = check(store, jobs)
    assert jobs and all(j.id is not None for j in jobs)
    want = {"finished": ["finished"], "pause": ["finished"], "late_cancel_amend": ["cancelled"],
            "idle_blip_reopen": ["finished"], "back_to_back": ["finished", None], "offline_lost": ["lost"],
            "day_gap_lost": ["lost"]}[name]
    assert [r["result"] for r in rows] == want
    assert all((r["ctx"] is None) == (r["result"] is not None) for r in rows)
    if name == "idle_blip_reopen":
        assert rows[0]["blips"] == 1
    if name == "pause":
        assert rows[0]["pauses"] == 1 and '"0700_8011"' in rows[0]["pauses_json"]


def test_updated_at_is_the_write_time(store):
    ss_ = SCENARIOS["finished"]()
    drive(JournalDB(store), ss_)
    assert _rows(store)[0]["updated_at"] == ss_[-3].ts   # the finished sample closed it


def test_checkpoint_not_more_often_than_checkpoint_s(store):
    statements = []
    store._db.set_trace_callback(statements.append)
    try:
        drive(JournalDB(store), seq(seg(1, "idle"), printing(63, p=(0, 30), e=(9000, 6000)), step=30))   # 31 min
    finally:
        store._db.set_trace_callback(None)
    assert sum(1 for q in statements if q.startswith("INSERT INTO print_jobs")) == 1
    assert sum(1 for q in statements if q.startswith("UPDATE print_jobs")) == 3   # at +10, +20, +30 min


def test_a_second_open_row_is_refused_and_the_machine_continues_the_stored_one(store, caplog):
    jdb = JournalDB(store)
    first = JobMachine(PID, kind="bambu", source="backfill")
    drive(jdb, seq(seg(1, "idle"), printing(4, p=(0, 20))), m=first)   # e.g. the backfill's open print
    stored = first.job
    fresh = JobMachine(PID, kind="bambu")   # a tracker that doesn't know about it
    t = stored.last_seen + 80
    ops = fresh.feed(Sample(ts=t, state="printing", progress=21, eta=5000, name=NAME, kind="bambu"))
    assert [op.op for op in ops] == ["open"]
    assert jdb.apply(fresh, ops, now=t) is True
    adopted = fresh.job
    assert adopted is not ops[0].job and adopted.id == stored.id and adopted.source == "backfill"
    assert "already open" in caplog.text
    drive(jdb, seq(printing(5, p=(22, 99), e=(4900, 60)), seg(1, "finished", progress=100, eta=0, name=NAME),
                   t0=t + 80), m=fresh)
    rows = _rows(store)
    assert len(rows) == 1 and rows[0]["id"] == stored.id and rows[0]["result"] == "finished"
    assert _cols(rows[0]) == adopted.row()


class FlakyStore:
    """A StateStore whose next `fail` writes run, then fail (and roll back) like a locked DB."""

    def __init__(self, store):
        self.store, self.fail, self.calls = store, 0, 0

    def db_write(self, fn):
        self.calls += 1
        if self.fail <= 0:
            return self.store.db_write(fn)
        self.fail -= 1

        def run_then_fail(conn):
            fn(conn)
            raise sqlite3.OperationalError("database is locked")
        return self.store.db_write(run_then_fail)


def test_a_failed_write_is_written_in_full_next_time(store):
    flaky = FlakyStore(store)
    jdb = JournalDB(flaky)
    m, jobs = JobMachine(PID, kind="bambu"), []
    samples = seq(seg(1, "idle"), printing(3, p=(0, 3)), printing(4, p=(90, 99), e=(600, 0)),
                  printing(4, p=(0, 5), e=(25000, 24000), name=NAME2), printing(30, p=(5, 20), e=(24000, 20000),
                                                                             name=NAME2))
    flaky.fail = 1
    drive(jdb, samples[:2], m=m, jobs=jobs)            # the open is rolled back
    assert jobs[0].id is None and _rows(store) == []
    drive(jdb, samples[2:8], m=m, jobs=jobs)           # retried in full, then diffs again
    assert jobs[0].id == 1
    flaky.fail = 1
    drive(jdb, samples[8:9], m=m, jobs=jobs)           # [close first, open second] rolled back
    assert jobs[1].id is None and _rows(store)[0]["result"] is None
    drive(jdb, samples[9:], m=m, jobs=jobs)
    rows = check(store, jobs)
    assert [r["result"] for r in rows] == ["finished", None] and [j.id for j in jobs] == [1, 2]


def test_a_vanished_row_is_written_anew(store):
    jdb = JournalDB(store)
    m, jobs = drive(jdb, seq(seg(1, "idle"), printing(5, p=(0, 20))))
    store._db.execute("DELETE FROM print_jobs")
    store._db.commit()
    drive(jdb, seq(printing(10, p=(21, 99), e=(3000, 60)), seg(1, "finished", progress=100, eta=0, name=NAME),
                   t0=m.job.last_seen + 80), m=m, jobs=jobs)
    rows = check(store, jobs)
    assert [r["result"] for r in rows] == ["finished"] and jobs[0].id == 2


# --- restart (spec §5.4) ---
def test_restart_continues_the_open_print_from_ctx_and_the_history(db_path):
    st = ss.StateStore()
    samples = seq(seg(1, "idle"), printing(12, p=(0, 30), e=(9000, 7000)))   # 16 min: one checkpoint
    m, jobs = drive(JournalDB(st), samples)
    for s in samples:
        _history(st, PID, s.ts, s.state)
    stored = _rows(st)[0]
    assert stored["result"] is None and stored["last_seen"] < samples[-1].ts   # the checkpoint lags
    _history(st, PID, samples[-1].ts + 30, "printing", grace=1)               # a clone, not data
    _history(st, PID, samples[-1].ts + 60, "offline")
    st._db.close()

    st2 = ss.StateStore()
    try:
        jdb = JournalDB(st2)
        now = samples[-1].ts + 120
        machines = jdb.load_open(now=now)
        assert list(machines) == [PID]
        m2 = machines[PID]
        job = m2.job
        assert job.id == jobs[0].id and m2.seq is None
        assert job.last_seen == samples[-1].ts and job.lp == stored["end_progress"]
        rest = seq(printing(30, p=(31, 99), e=(6900, 60), seq=0), seg(1, "finished", progress=100, eta=0,
                                                                       name=NAME, seq=0), t0=now + 30, step=30)
        drive(jdb, rest, m=m2, jobs=[])
        rows = _rows(st2)
        assert len(rows) == 1 and rows[0]["id"] == jobs[0].id
        assert (rows[0]["result"], rows[0]["offline_n"], rows[0]["start_exact"]) == ("finished", 0, 1)
        assert _cols(rows[0]) == job.row()
    finally:
        st2._db.close()


def test_restart_after_a_day_without_data_closes_the_print_as_lost(store):
    jdb = JournalDB(store)
    m, jobs = drive(jdb, seq(seg(1, "idle"), printing(5, p=(0, 20))))
    last = _rows(store)[0]["last_seen"]
    machines = JournalDB(store).load_open(now=last + jj.JOBS_MAX_GAP_S + 100)
    assert machines[PID].job is None
    row = _rows(store)[0]
    assert (row["result"], row["ended_at"], row["end_exact"], row["ctx"]) == ("lost", last, 0, None)


def test_restart_reads_the_open_prints_through_the_partial_index(store):
    drive(JournalDB(store), SCENARIOS["finished"]())
    drive(JournalDB(store), seq(seg(1, "idle"), printing(5, p=(0, 20))))
    sql = []
    store._db.set_trace_callback(sql.append)
    try:
        machines = JournalDB(store).load_open(now=T0 + 10 ** 4)
    finally:
        store._db.set_trace_callback(None)
    assert machines[PID].job is not None
    (query,) = [s for s in sql if "FROM print_jobs" in s]
    assert "INDEXED BY idx_jobs_one_open" in query
    plan = " ".join(r[3] for r in store._db.execute("EXPLAIN QUERY PLAN " + query))
    assert "SCAN print_jobs USING INDEX idx_jobs_one_open" in plan   # not a full table scan


def test_restart_with_nothing_open(store):
    drive(JournalDB(store), SCENARIOS["finished"]())
    assert JournalDB(store).load_open(now=T0 + 10 ** 5) == {}


# --- «Годно / Брак» (spec §7.4) ---
def test_a_mark_set_after_the_close_survives_the_trackers_later_writes(store, db_path):
    flaky = FlakyStore(store)
    jdb = JournalDB(flaky)
    samples = SCENARIOS["late_cancel_amend"]()
    m, jobs = drive(jdb, samples[:7])                  # the first error sample closes it as «stopped»
    assert jobs[0].result == "stopped"
    assert jj.set_verdict(db_path, jobs[0].id, "defect", "u", now=samples[6].ts + 5) == "ok"
    flaky.fail = 1
    drive(jdb, samples[7:8], m=m, jobs=jobs)           # the cancel code amends it, the write is rolled back...
    assert flaky.fail == 0 and _rows(store)[0]["result"] == "stopped" and jdb._retry
    drive(jdb, samples[8:], m=m, jobs=jobs)            # ...and the print is written again in full
    (row,) = _rows(store)
    assert (row["result"], row["error_code"]) == ("cancelled", "0300_400C")
    assert (row["verdict"], row["verdict_by"], row["verdict_at"]) == ("defect", "u", samples[6].ts + 5)


# --- names stay out of the logs ---
def test_no_job_name_in_storage_logs(store, caplog):
    caplog.set_level(logging.DEBUG)
    flaky = FlakyStore(store)
    jdb = JournalDB(flaky)
    for name in sorted(SCENARIOS):
        flaky.fail = 1
        drive(jdb, SCENARIOS[name](), m=JobMachine("%s-%s" % (PID, name), kind="bambu"))
    drive(jdb, seq(seg(1, "idle"), printing(3)), m=JobMachine("dup", kind="bambu"))
    fresh = JobMachine("dup", kind="bambu")   # conflicts with the open print of the same printer
    jdb.apply(fresh, fresh.feed(Sample(ts=T0 + 400, state="printing", progress=1, name=NAME2, kind="bambu")))
    assert "already open" in caplog.text
    JournalDB(store).load_open(now=T0 + 10 ** 6)
    assert caplog.records
    for rec in caplog.records:
        text = rec.getMessage() + (rec.exc_text or "")
        assert NAME not in text and NAME2 not in text


def test_retention_days_from_env(monkeypatch):
    assert jj.JOBS_RETENTION_S == jj.JOBS_RETENTION_DAYS * 86400
    monkeypatch.setenv("JOBS_TEST_DAYS", "30")
    assert jj._env_days("JOBS_TEST_DAYS", 730) == 30
    monkeypatch.setenv("JOBS_TEST_DAYS", "много")
    assert jj._env_days("JOBS_TEST_DAYS", 730) == 730
    monkeypatch.setenv("JOBS_TEST_DAYS", "0")
    assert jj._env_days("JOBS_TEST_DAYS", 730) == 1
    monkeypatch.delenv("JOBS_TEST_DAYS")
    assert jj._env_days("JOBS_TEST_DAYS", 730) == 730
