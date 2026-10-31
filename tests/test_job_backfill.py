"""One-time replay of printer_history into print_jobs (spec_job_journal §6, §11.3). Job names are invented."""
import json
import logging
import sqlite3
import threading
from types import SimpleNamespace

import pytest

import test_job_journal as tjj
from app.domain.models import PrinterKind, PrinterState, PrinterStatus
from app.services import job_journal as jj
from app.services import state_store as ss
from test_job_journal import NAME, NAME2, T0

DAY = 86400


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    folder = tmp_path / "My history"   # a space: the read-only URI must survive it
    folder.mkdir()
    path = str(folder / "ph.db")
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


HISTORY_COLS = ("printer_id", "label", "kind", "online", "state", "progress", "job_name", "eta_sec", "print_time",
                "nozzle_temp", "bed_temp", "layer", "total_layer", "last_error", "device_type", "recorded_at",
                "grace_period_active", "last_successful_fetch")


def hrow(ts, state, pid="pa", kind="bambu", **kw):
    """A printer_history row as record_snapshot writes it (all columns of state_store.py)."""
    row = dict(printer_id=pid, label="L", kind=kind, online=int(state not in ("offline", "maintenance")),
               state=state, progress=None, job_name=None, eta_sec=None, print_time=None, nozzle_temp=None,
               bed_temp=None, layer=None, total_layer=None, last_error=None, device_type="P2S",
               recorded_at=ts, grace_period_active=0, last_successful_fetch=0)
    row.update(kw)
    return row


def _seed(store, rows, reason=False):
    """Insert history rows in the given order (rowid order = insertion order)."""
    cols = list(HISTORY_COLS)
    if reason:
        have = {r[1] for r in store._db.execute("PRAGMA table_info(printer_history)")}
        if "reason" not in have:
            store._db.execute("ALTER TABLE printer_history ADD COLUMN reason TEXT")
        cols.append("reason")
    sql = "INSERT INTO printer_history (%s) VALUES (%s)" % (", ".join(cols), ", ".join("?" * len(cols)))
    store._db.executemany(sql, [[r.get(c) for c in cols] for r in rows])
    store._db.commit()


def a_print(t, pid="pa", name=NAME, n=10, step=80, end="finished"):
    """idle, n printing rows 0 -> 99 %, the end state, idle."""
    rows = [hrow(t, "idle", pid)]
    for i in range(n):
        p = 99.0 * i / (n - 1)
        rows.append(hrow(t + step * (i + 1), "printing", pid, progress=p, job_name=name,
                         eta_sec=int(3600 * (1 - p / 100)), layer=i, total_layer=n))
    t_end = t + step * (n + 1)
    rows.append(hrow(t_end, end, pid, progress=100.0, job_name=name, eta_sec=0))
    rows.append(hrow(t_end + step, "idle", pid))
    return rows


def jobs(store):
    return jj._dicts(store._db.execute("SELECT * FROM print_jobs ORDER BY id"))


def meta(store):
    return dict(store._db.execute("SELECT key, value FROM job_journal_meta").fetchall())


def _clear(store):
    store._db.execute("DELETE FROM print_jobs")
    store._db.execute("DELETE FROM job_journal_meta")
    store._db.commit()


# --- the synthetic scenarios of §11.1 as history rows ---
def _last_error(s):
    """last_error text that parses back to the sample's code and text."""
    code, text = s.err_code, s.err_text
    if code is None:
        return None
    if code.startswith("HMS_"):
        return "%s: %s [fatal]" % (code, text or "unknown")
    return "Ошибка печати %s: %s" % (code, text) if text else "Ошибка печати %s" % code


def _history_rows(samples, pid, kind):
    return [hrow(s.ts, s.state, pid, kind, online=int(s.online), progress=s.progress, job_name=s.name,
                 eta_sec=s.eta, print_time=s.print_time, layer=s.layer, total_layer=s.total_layers,
                 last_error=_last_error(s), device_type=s.model, grace_period_active=int(s.grace),
                 reason=s.reason if s.reason and s.reason.startswith("stage:") else None)
            for s in samples]


def _scenarios(monkeypatch):
    """(pid, history rows, expected print rows) of every §11.1 scenario a history row can carry.

    Out: job_seq (R1, live only), a restore from ctx, maint on a printing sample.
    """
    out = []
    for i, scenario in enumerate(tjj.SCENARIOS):
        fed = []
        real = tjj.run

        def recorder(samples, m=None, kind="bambu", db=None):
            fed.append((list(samples), kind, m))
            return real(samples, m=m, kind=kind, db=db)

        with monkeypatch.context() as mp:
            mp.setattr(tjj, "run", recorder)
            r = scenario()[0]
        if len(fed) != 1 or fed[0][2] is not None:
            continue
        samples, kind, _ = fed[0]
        if any(s.seq is not None or (s.maint and s.state != "maintenance") for s in samples):
            continue
        out.append(("sc%02d" % i, _history_rows(samples, "sc%02d" % i, kind), r.rows))
    return out


def _interleaved(per_printer):
    """Round-robin the printers' rows: each printer keeps its order, the rowids mix all of them."""
    out, i = [], 0
    while any(i < len(rows) for rows in per_printer):
        out.extend(rows[i] for rows in per_printer if i < len(rows))
        i += 1
    return out


def _machine_cols(row):
    """The columns the machine decides (the printer id is per scenario, the source differs by design)."""
    return {k: row[k] for k in jj.COLUMNS if k not in ("source", "printer_id")}


def test_history_rows_give_the_same_prints_as_the_live_machine(store, monkeypatch):
    cases = _scenarios(monkeypatch)
    assert len(cases) >= 30
    _seed(store, _interleaved([rows for _, rows, _ in cases]), reason=True)
    stats = jj.backfill(store, chunk=3, pause=0, now=T0)
    assert stats["rows"] == sum(len(rows) for _, rows, _ in cases)
    stored = jobs(store)
    for pid, _, expected in cases:
        got = [r for r in stored if r["printer_id"] == pid]
        assert [_machine_cols(r) for r in got] == [_machine_cols(e) for e in expected], pid
    assert {r["source"] for r in stored} == {"backfill"}
    assert stats["jobs"] == stats["inserted"] == len(stored) and stats["skipped"] == 0
    assert stats["results"].get("open", 0) == sum(1 for r in stored if r["result"] is None)


def test_chunk_size_does_not_change_the_result(store, monkeypatch):
    cases = _scenarios(monkeypatch)
    _seed(store, _interleaved([rows for _, rows, _ in cases]), reason=True)
    no_id = lambda rows: [{k: v for k, v in r.items() if k != "id"} for r in rows]   # noqa: E731
    jj.backfill(store, chunk=3, pause=0, now=T0)
    small = no_id(jobs(store))
    _clear(store)
    jj.backfill(store, chunk=5000, pause=0, now=T0)
    assert no_id(jobs(store)) == small and len(small) > 30


# --- once only, restart-safe ---
def test_second_run_does_nothing(store):
    _seed(store, a_print(T0) + a_print(T0 + 3600, pid="pb", name=NAME2))
    stats = jj.backfill(store, pause=0, now=T0)
    assert stats["inserted"] == 2 and meta(store)["backfill_version"] == str(jj.BACKFILL_VERSION)
    before = jobs(store)
    assert jj.backfill(store, pause=0) is None   # the version is there: nothing written
    jj.start(store, now=T0 + 7200)
    assert jj._ready and jj._bf_thread is None   # no thread on the next start
    assert jobs(store) == before


def test_meta_holds_counts_and_the_last_row(store):
    _seed(store, a_print(T0) + a_print(T0 + 3600, end="cancelled"))
    jj.backfill(store, pause=0, now=T0 + 99)
    m = meta(store)
    last_id = store._db.execute("SELECT MAX(id) FROM printer_history").fetchone()[0]
    assert (m["backfill_max_id"], float(m["backfill_done_at"])) == (str(last_id), T0 + 99)
    stats = json.loads(m["backfill_stats"])
    assert stats["results"] == {"cancelled": 1, "finished": 1} and stats["rows"] == 26
    assert NAME not in m["backfill_stats"]


def test_empty_history_is_done_at_once(store):
    jj.start(store, now=T0)
    assert jj._ready and jj._bf_thread is None
    assert meta(store)["backfill_version"] == str(jj.BACKFILL_VERSION)


def test_an_error_midway_writes_nothing_and_the_next_start_tries_again(store, monkeypatch, caplog):
    _seed(store, a_print(T0) + a_print(T0 + 3600))
    real, calls = jj._history_sample, []   # one call per history row (the backfill builds no row dicts)

    def flaky(*row):
        calls.append(1)
        if len(calls) == 15:
            raise ValueError("bad row %r" % (row,))   # a message quoting the job name
        return real(*row)
    monkeypatch.setattr(jj, "_history_sample", flaky)
    jj.start(store, now=T0 + 7200)
    assert jj.wait_backfill(10)
    assert jj._ready and jobs(store) == [] and "backfill_version" not in meta(store)
    assert "ValueError" in caplog.text and NAME not in caplog.text
    monkeypatch.setattr(jj, "_history_sample", real)
    jj.stop()
    jj.start(store, now=T0 + 7200)
    assert jj.wait_backfill(10) and [r["result"] for r in jobs(store)] == ["finished", "finished"]


def test_the_write_is_one_transaction(store, monkeypatch):
    _seed(store, a_print(T0) + a_print(T0 + 3600))
    real, calls = jj.insert_job, []

    def second_fails(conn, fields, now):
        calls.append(1)
        if len(calls) == 2:
            raise sqlite3.OperationalError("disk I/O error")
        return real(conn, fields, now)
    monkeypatch.setattr(jj, "insert_job", second_fails)
    assert jj.backfill(store, pause=0) is None
    assert jobs(store) == [] and "backfill_version" not in meta(store)


def test_stop_cancels_a_running_backfill(store, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=jj.__name__)
    _seed(store, a_print(T0) * 3)
    monkeypatch.setattr(jj, "time", SimpleNamespace(time=jj.time.time, monotonic=jj.time.monotonic,
                                                    sleep=lambda _s: jj.stop()))
    monkeypatch.setattr(jj, "BACKFILL_CHUNK", 5)
    jj.start(store, now=T0)
    assert jj.wait_backfill(10)
    assert not jj._ready and jobs(store) == [] and "backfill_version" not in meta(store)
    assert "бэкфилл остановлен" in caplog.text


# --- catch-up and handover (§6.1 p. 5, §6.4) ---
def test_rows_written_during_the_pass_are_replayed_too(store, monkeypatch):
    _seed(store, a_print(T0))
    cutoff = store._db.execute("SELECT MAX(id) FROM printer_history").fetchone()[0]
    added = []

    def sleep(_s):
        if not added:   # the poll loop writes on while the backfill reads
            added.append(1)
            _seed(store, a_print(T0 + 3600, pid="pb", name=NAME2))
    monkeypatch.setattr(jj, "time", SimpleNamespace(time=jj.time.time, monotonic=jj.time.monotonic, sleep=sleep))
    stats = jj.backfill(store, chunk=4, pause=0.01, now=T0)
    assert added and stats["late_rows"] == 13 and stats["inserted"] == 2
    assert meta(store)["backfill_max_id"] == str(cutoff + 13)
    assert {r["printer_id"] for r in jobs(store)} == {"pa", "pb"}


def _status(state, **kw):
    base = dict(id="pa", label="L", kind=PrinterKind.BAMBU, online=state != PrinterState.OFFLINE, state=state,
                device_type="P2S", job_name=NAME)
    base.update(kw)
    return PrinterStatus(**base)


def test_the_print_open_at_the_end_of_the_history_goes_on_live(store, monkeypatch):
    rows = a_print(T0 - 3600, pid="pb", name=NAME2)
    rows += [hrow(T0, "idle")] + [hrow(T0 + 80 * (i + 1), "printing", progress=float(i), job_name=NAME,
                                       eta_sec=3600 - 60 * i) for i in range(10)]
    _seed(store, rows)
    gate, entered = threading.Event(), threading.Event()
    real = jj.backfill

    def gated(*a, **kw):
        entered.set()
        gate.wait(10)
        return real(*a, **kw)
    monkeypatch.setattr(jj, "backfill", gated)
    t_end = T0 + 800
    jj.start(store, now=t_end + 30)
    assert entered.wait(10)
    assert jj._bf_thread.name == "jobs-backfill" and jj._bf_thread.daemon
    jj.observe(_status(PrinterState.PRINTING, progress_pct=10.0, eta_seconds=3000), t_end + 30)
    assert not jj._ready and jj._machines == {}   # observe is a no-op until the handover
    gate.set()
    assert jj.wait_backfill(10) and jj._ready
    finished, opened = jobs(store)
    assert (finished["printer_id"], finished["result"]) == ("pb", "finished")
    assert (opened["result"], opened["source"], opened["last_seen"]) == (None, "backfill", t_end)
    assert json.loads(opened["ctx"])["lp"] == 9.0
    assert jj._machines["pa"].job.id == opened["id"]
    t = t_end + 30
    for i in range(3):
        jj.observe(_status(PrinterState.PRINTING, progress_pct=10.0 + i, eta_seconds=3000 - 60 * i), t)
        t += 30
    jj.observe(_status(PrinterState.FINISHED, progress_pct=100.0, eta_seconds=0), t)
    finished2, row = jobs(store)
    assert (row["id"], row["result"], row["start_exact"], row["end_exact"]) == (opened["id"], "finished", 1, 1)
    assert row["source"] == "backfill" and row["ended_at"] == t and row["offline_n"] == 0


def test_a_stale_open_print_is_lost_at_the_handover(store):
    _seed(store, [hrow(T0, "idle")] + [hrow(T0 + 80 * (i + 1), "printing", progress=float(i), job_name=NAME)
                                       for i in range(5)])   # the history ends here: the printer is gone
    jj.start(store, now=T0 + 2 * DAY)
    assert jj.wait_backfill(10) and jj._ready
    (row,) = jobs(store)
    assert (row["result"], row["ended_at"], row["end_exact"]) == ("lost", T0 + 400, 0)
    assert jj._machines["pa"].job is None


# --- what the rows can say ---
@pytest.mark.parametrize("reason_column", [True, False])
def test_maintenance_and_cancelled_rows_with_and_without_the_reason_column(store, reason_column):
    t = T0
    rows = [hrow(t, "idle")]
    for i in range(4):
        t += 80
        rows.append(hrow(t, "printing", progress=10.0 + i, job_name=NAME, eta_sec=3000))
    for _ in range(2):
        t += 80
        rows.append(hrow(t, "maintenance", progress=None))
    for _ in range(3):   # a filament runout pause
        t += 80
        if reason_column:
            rows.append(hrow(t, "paused", progress=14.0, job_name=NAME, reason="0700_8011: Пруток закончился"))
        else:
            rows.append(hrow(t, "paused", progress=14.0, job_name=NAME,
                             last_error="Ошибка печати 0700_8011: Пруток закончился"))
    t += 80
    rows.append(hrow(t, "printing", progress=15.0, job_name=NAME, eta_sec=2900))
    t += 80
    rows.append(hrow(t, "cancelled", progress=15.0, job_name=NAME))
    _seed(store, rows, reason=reason_column)
    jj.backfill(store, pause=0)
    (row,) = jobs(store)
    assert (row["result"], row["maint"], row["offline_n"], row["ended_at"]) == ("cancelled", 1, 0, t)
    (pause,) = json.loads(row["pauses_json"])
    assert (pause["code"], pause["text"], row["paused_sec"]) == ("0700_8011", "Пруток закончился", 240)


def test_old_error_format_and_grace_rows(store):
    rows = [hrow(T0, "idle")] + [hrow(T0 + 80 * (i + 1), "printing", progress=20.0 + i, job_name=NAME)
                                 for i in range(4)]
    rows += [hrow(T0 + 400 + 80 * i, "printing", progress=23.0, job_name=NAME, grace_period_active=1)
             for i in range(3)]   # clones of the last good status: skipped
    rows.append(hrow(T0 + 700, "error", progress=23.0, job_name=NAME, last_error="Код ошибки печати: 0x0300400C"))
    _seed(store, rows)
    jj.backfill(store, pause=0)
    (row,) = jobs(store)
    assert (row["result"], row["error_code"], row["last_seen"], row["ended_at"]) == (
        "cancelled", "0300_400C", T0 + 320, T0 + 700)


def test_a_grace_row_with_a_state_the_history_never_had_is_fed(store):
    # The poll saw FINISH, the 60 s snapshot throttle skipped it, the printer
    # powered off: only the grace clone of that status reached the history.
    rows = [hrow(T0, "idle")] + [hrow(T0 + 80 * (i + 1), "printing", progress=90.0 + i, job_name=NAME,
                                      eta_sec=540 - 60 * i, layer=900 + i, total_layer=909) for i in range(10)]
    rows += [hrow(T0 + 880 + 60 * i, "finished", progress=100.0, job_name=NAME, eta_sec=0, grace_period_active=1)
             for i in range(3)]
    rows += [hrow(T0 + 1060 + 80 * i, "offline") for i in range(23)]   # 30 min
    rows.append(hrow(T0 + 1060 + 80 * 23, "idle"))
    _seed(store, rows)
    jj.backfill(store, pause=0)
    (row,) = jobs(store)
    assert (row["result"], row["ended_at"], row["end_exact"], row["end_progress"]) == ("finished", T0 + 880, 1, 100)


def test_backfill_grace_feeds_only_a_state_not_seen_yet():
    def s(state, grace):
        return jj.Sample(ts=T0, state=state, grace=grace)
    assert jj._backfill_grace(None, s("printing", False)) == ("printing", False)
    assert jj._backfill_grace("printing", s("printing", True)) == ("printing", True)    # a clone: skipped
    assert jj._backfill_grace("printing", s("finished", True)) == ("finished", False)   # never written: fed
    assert jj._backfill_grace("finished", s("finished", True)) == ("finished", True)
    assert jj._backfill_grace("printing", s("offline", True)) == ("printing", True)     # no data either way
    assert jj._backfill_grace("printing", s("maintenance", True)) == ("printing", True)


def test_the_backfill_sampler_matches_sample_from_row():
    rows = [
        hrow(T0, "printing", progress=47.0, job_name=NAME, eta_sec=600, print_time=120, layer=12, total_layer=300,
             last_error="Код ошибки печати: 0x0300400C", device_type="X1C"),
        hrow(T0 + 80, "paused", progress=48.0, job_name=NAME, grace_period_active=1, reason="stage:paused_user"),
        hrow(T0 + 160, "paused", reason="0700_8011: Пруток закончился",
             last_error="HMS_0C00_0100_0001_0004: Сопло забито [fatal]"),
        hrow(T0 + 240, "maintenance", job_name="  "),
        hrow(T0 + 320, "offline", online=None, kind="creality"),
    ]
    for i, r in enumerate(rows):
        r["id"] = i + 1
    for cols in (list(jj._HISTORY_COLS) + list(jj._HISTORY_OPTIONAL), list(jj._HISTORY_COLS)):
        make = jj._tuple_sampler(cols)
        for r in rows:
            r = {k: v for k, v in r.items() if k in cols}
            assert make(tuple(r.get(c) for c in cols)) == jj.sample_from_row(r)


# --- a new BACKFILL_VERSION (§6.3) ---
def test_a_new_version_rebuilds_what_the_history_still_shows(store, monkeypatch):
    _seed(store, a_print(T0) + a_print(T0 + DAY, pid="pb") + a_print(T0 + 2 * DAY)
          + a_print(T0 + 2 * DAY + 3 * 3600, pid="pc", name=NAME2))
    jj.backfill(store, pause=0, now=T0)
    pa1, pb1, pa2, pc1 = sorted(jobs(store), key=lambda r: r["started_at"])
    db = store._db
    db.execute("UPDATE print_jobs SET verdict = 'bad', verdict_by = 'admin', verdict_at = ? WHERE id = ?",
               (T0 + 5, pa2["id"]))
    db.execute("UPDATE print_jobs SET verdict = 'good', verdict_by = 'admin', verdict_at = ? WHERE id = ?",
               (T0 + 6, pa1["id"]))
    live = dict(pc1, source="live", started_at=pc1["started_at"] - 30, updated_at=T0)   # the tracker saw it too
    del live["id"]
    live_id = jj.insert_job(db, live, T0)
    db.execute("DELETE FROM printer_history WHERE recorded_at < ?", (T0 + DAY / 2,))   # retention ate pa1's rows
    db.commit()
    before = {r["id"]: r for r in jobs(store)}

    monkeypatch.setattr(jj, "BACKFILL_VERSION", 2)
    stats = jj.backfill(store, pause=0, now=T0 + 10)
    after = {r["id"]: r for r in jobs(store)}
    assert meta(store)["backfill_version"] == "2"
    # the oldest history day + 1 is the line: pa1 (gone from the history) and pb1 stay as they were
    assert after[pa1["id"]] == before[pa1["id"]] and after[pa1["id"]]["verdict"] == "good"
    assert after[pb1["id"]] == before[pb1["id"]]
    assert after[live_id] == before[live_id]   # live rows are never touched
    # pa2 is rebuilt (a new row) and gets its verdict back; pc1 is not rebuilt over the live row
    assert pa2["id"] not in after and pc1["id"] not in after
    (new_pa2,) = [r for r in after.values() if r["id"] not in before]
    assert (new_pa2["printer_id"], new_pa2["started_at"], new_pa2["result"]) == ("pa", pa2["started_at"], "finished")
    assert (new_pa2["verdict"], new_pa2["verdict_by"], new_pa2["verdict_at"]) == ("bad", "admin", T0 + 5)
    assert (stats["replaced"], stats["inserted"], stats["skipped"], stats["verdicts"]) == (2, 1, 2, 1)


def test_rebuilt_prints_never_duplicate_live_ones(store):
    """A backfill that failed before: the tracker has written prints meanwhile, one is still open."""
    _seed(store, a_print(T0) + a_print(T0 + 3600) + [hrow(T0 + 7200, "idle")]
          + [hrow(T0 + 7200 + 80 * (i + 1), "printing", progress=float(i), job_name=NAME2) for i in range(5)])
    m = jj.JobMachine("pa", kind="bambu")
    jdb = jj.JournalDB(store)
    for s in tjj.seq(tjj.seg(1, "idle"), tjj.printing(5, p=(0, 99), e=(3600, 0)), tjj.seg(1, "finished", progress=100),
                     t0=T0 + 3600):   # the second print, seen live
        jdb.apply(m, m.feed(s), now=s.ts)
    for s in tjj.seq(tjj.seg(1, "idle"), tjj.printing(3, p=(0, 2), name=NAME2), t0=T0 + 7200):   # the open one
        jdb.apply(m, m.feed(s), now=s.ts)
    live = jobs(store)
    assert [(r["result"], r["source"]) for r in live] == [("finished", "live"), (None, "live")]
    stats = jj.backfill(store, pause=0)
    rows = jobs(store)
    assert rows[:2] == live   # untouched
    (rebuilt,) = rows[2:]
    assert (rebuilt["source"], rebuilt["started_at"], rebuilt["result"]) == ("backfill", T0 + 80, "finished")
    assert (stats["inserted"], stats["skipped"]) == (1, 2)
    assert sum(1 for r in rows if r["result"] is None) == 1


# --- names, read-only ---
def test_no_job_name_in_backfill_logs(store, caplog):
    caplog.set_level(logging.DEBUG)
    _seed(store, a_print(T0) + a_print(T0 + 3600, name=NAME2, end="error")
          + [hrow(T0 + 7200, "printing", progress=5.0, job_name=NAME)])
    jj.start(store, now=T0 + 7300)
    assert jj.wait_backfill(10) and jj._ready
    assert "бэкфилл: строк истории 27, печатей 3" in caplog.text
    for rec in caplog.records:
        text = rec.getMessage() + (rec.exc_text or "")
        assert NAME not in text and NAME2 not in text


def test_the_history_is_read_on_a_read_only_connection(store, db_path):
    _seed(store, a_print(T0))
    conn = jj._connect_ro(jj._db_file(store))
    try:
        assert conn.execute("SELECT COUNT(*) FROM printer_history").fetchone()[0] == 13
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM printer_history")
    finally:
        conn.close()
