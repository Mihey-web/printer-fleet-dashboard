"""QA 17.11.2026, part B2: the print journal and its stats, notifications, Telegram, auto-light,
auto-ignore and auto-drying.

The testers' confirming tests (docs/dev/qa-2026-11-17: code.md Н1, hunt.md №2, 4, 5, 7, 10, 11, 17,
18, 26, 27, 28, 29, 37, 38, 44), moved here with their imports fixed; each failed on main 0f67c79.
Nothing here touches a printer or Telegram: fake stores, collectors and bots only.
"""
import copy
import csv
import datetime as dt
import io
import json
import os
import sqlite3
import time
from types import SimpleNamespace

import pytest

import main
from app.domain.models import PrinterKind, PrinterState, PrinterStatus
from app.services import ams_autodry as ad
from app.services import ams_drying, auto_ignore, auto_light, job_previews
from app.services import job_journal as jj
from app.services import job_queries as jq
from app.services import notification_mutes as nm
from app.services import notifications as nf
from app.services import state_store as ss
from app.services.job_journal import JobMachine, JournalDB, Sample


# --- code.md Н1: the plate map of a kept job survives the prune ------------------------------------

def _png(rgb=(1, 0, 0)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), rgb).save(buf, "PNG")
    return buf.getvalue()


def _age(path, seconds):
    then = time.time() - seconds
    os.utime(path, (then, then))


def test_history_prune_keeps_the_plate_map_of_a_kept_job(monkeypatch, tmp_path):
    """print_material.collect_project stores the plate map (Bambu's pick_N.png) as job_previews key
    "pick" INSIDE the project card; the prune kept only print_jobs.preview_key / project_key."""
    monkeypatch.setattr(job_previews, "DIRECTORY", tmp_path)
    pick = job_previews.save(_png())
    project = job_previews.save_project({"title": "Корпус", "parts": [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}],
                                         "pick": pick})
    three_days_ago = time.time() - 3 * 86400
    for f in tmp_path.iterdir():
        os.utime(f, (three_days_ago, three_days_ago))
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE print_jobs (result TEXT, started_at REAL, preview_key TEXT, project_key TEXT)")
    conn.execute("INSERT INTO print_jobs VALUES ('finished', ?, NULL, ?)", (three_days_ago, project))
    jj.prune(conn, time.time())
    assert json.loads((tmp_path / (project + ".json")).read_text(encoding="utf-8"))["pick"] == pick
    assert (tmp_path / (pick + ".png")).exists()


def test_prune_still_removes_an_orphan_map_and_old_temporaries(monkeypatch, tmp_path):
    monkeypatch.setattr(job_previews, "DIRECTORY", tmp_path)
    pick = job_previews.save(_png((2, 0, 0)))
    orphan = job_previews.save_project({"title": "x", "pick": pick})        # no print job refers to it
    stale = [tmp_path / ".preview-abc123", tmp_path / ".project-def456"]    # a crash left them behind
    fresh = tmp_path / ".preview-now"                                       # a write going on right now
    for p in stale + [fresh]:
        p.write_bytes(b"x")
    for p in [tmp_path / (pick + ".png"), tmp_path / (orphan + ".json")] + stale:
        _age(p, 3 * 86400)
    assert job_previews.prune(set(), time.time()) == 4
    assert sorted(p.name for p in tmp_path.iterdir()) == [".preview-now"]


# --- hunt №2: «Печатей в день» by the page's zone, inside the window --------------------------------

MSK = dt.timezone(dt.timedelta(hours=3))
BROWSER = dt.timezone(dt.timedelta(hours=7))


def _fake_zone(monkeypatch):
    """zoneinfo without a zone database (Windows without tzdata): a fixed table for the tests."""
    zones = {"Asia/Novosibirsk": BROWSER, "Europe/Moscow": MSK}

    def zone_info(name):
        if name not in zones:
            raise KeyError(name)          # zoneinfo.ZoneInfoNotFoundError is a KeyError
        return zones[name]

    monkeypatch.setattr(jq, "ZoneInfo", zone_info)


def test_one_browser_day_is_one_column(monkeypatch):
    """hunt-repros/p8a__test_per_day_browser_tz.py: the browser's day 10.11 in UTC+7, the Pi in Moscow."""
    monkeypatch.setattr(jq, "_TZ", MSK)                     # the Pi
    _fake_zone(monkeypatch)
    fr = dt.datetime(2026, 11, 10, 0, 0, tzinfo=BROWSER).timestamp()   # histDayStart("2026-11-10")
    to = dt.datetime(2026, 11, 11, 0, 0, tzinfo=BROWSER).timestamp()   # histDayStart("2026-11-10", 1)
    started = dt.datetime(2026, 11, 10, 2, 0, tzinfo=BROWSER).timestamp()
    row = {"result": "finished", "started_at": started, "ended_at": started + 3600}
    days = jq._per_day([row], fr, to, jq.zone("Asia/Novosibirsk"))
    assert [d["day"] for d in days] == ["2026-11-10"], days
    assert days[0]["jobs"] == 1


def test_zone_falls_back_to_the_server_zone(monkeypatch):
    monkeypatch.setattr(jq, "_TZ", MSK)
    _fake_zone(monkeypatch)
    for bad in (None, "", "x" * 65, "Mars/Olympus", "../etc/passwd", "Asia/Omsk\n", 7, "Europe//Moscow"):
        assert jq.zone(bad) is MSK, bad
    assert jq.zone("UTC") is dt.timezone.utc and jq.zone("Etc/UTC") is dt.timezone.utc
    monkeypatch.setattr(jq, "ZoneInfo", None)                 # no zoneinfo at all
    assert jq.zone("Asia/Novosibirsk") is MSK


def test_zone_reads_the_real_zone_database_when_there_is_one():
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo("Asia/Omsk")
    except Exception:
        # Windows without the tzdata package: zone() answers the server's zone (the test above)
        pytest.skip("no IANA zone database here")
    assert jq.zone("Asia/Omsk").utcoffset(dt.datetime(2026, 11, 17)) == dt.timedelta(hours=6)


def _ts(s, tz=dt.timezone.utc):
    return dt.datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=tz).timestamp()


@pytest.fixture
def jdb(tmp_path, monkeypatch):
    monkeypatch.setattr(jq, "_TZ", dt.timezone.utc)
    path = str(tmp_path / "ph.db")
    conn = sqlite3.connect(path)
    jj.init_db(conn)
    yield path, conn
    conn.close()


def _job(conn, start, end, result="finished", tz=dt.timezone.utc, **kw):
    st, en = _ts(start, tz), (_ts(end, tz) if end else None)
    fields = dict(printer_id="P1", kind="bambu", model="P2S", job_name="part", started_at=st, start_exact=1,
                  ended_at=en, end_exact=1 if en else None, last_seen=en or st, result=result,
                  actual_sec=int(en - st) if en else None, source="live")
    fields.update(kw)
    jj.insert_job(conn, fields, st)
    conn.commit()


def test_per_day_stays_inside_the_requested_days(jdb):
    """hunt-repros/p3__test_queries_days_and_csv.py::test_per_day_stays_inside_the_requested_days"""
    path, conn = jdb
    _job(conn, "2026-10-10 22:00", "2026-10-11 02:00")                   # started in the window, ended after it
    _job(conn, "2026-10-09 20:00", None, result=None, printer_id="P2")   # open, started before the window
    stats = jq.job_stats(path, _ts("2026-10-10 00:00"), _ts("2026-10-10 23:59"))
    assert stats["total"]["jobs"] == 2
    days = [{k: d[k] for k in ("day", "jobs", "finished", "bad")} for d in stats["per_day"]]
    assert days == [{"day": "2026-10-10", "jobs": 2, "finished": 1, "bad": 0}]


def test_job_stats_cuts_the_days_in_the_pages_zone(jdb, monkeypatch):
    path, conn = jdb
    monkeypatch.setattr(jq, "_TZ", MSK)
    _fake_zone(monkeypatch)
    # 01:00 and 23:00 of 16.11 at the owner's (UTC+7): 15.11 21:00 and 16.11 19:00 in Moscow
    _job(conn, "2026-11-16 00:10", "2026-11-16 01:00", tz=BROWSER)
    _job(conn, "2026-11-16 22:00", "2026-11-16 23:00", tz=BROWSER)
    fr, to = _ts("2026-11-16 00:00", BROWSER), _ts("2026-11-17 00:00", BROWSER)
    days = jq.job_stats(path, fr, to, tz="Asia/Novosibirsk")["per_day"]
    assert [{k: d[k] for k in ("day", "jobs", "finished", "bad")} for d in days] == [{"day": "2026-11-16", "jobs": 2, "finished": 2, "bad": 0}]
    # no zone, or one the server does not know: the server's days, as before
    for tz in (None, "Mars/Olympus"):
        assert [d["day"] for d in jq.job_stats(path, fr, to, tz=tz)["per_day"]] == ["2026-11-15", "2026-11-16"]


# --- hunt №26: CSV grams with a decimal comma ---------------------------------------------------------

def test_csv_grams_use_a_decimal_comma_for_russian_excel(jdb):
    path, conn = jdb
    _job(conn, "2026-10-10 10:00", "2026-10-10 12:00", planned_filament_g=23.4)
    _job(conn, "2026-10-10 13:00", "2026-10-10 14:00", planned_filament_g=None, printer_id="P2")
    text = jq.jobs_csv(path, _ts("2026-10-10 00:00"), _ts("2026-10-10 23:59"))
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿")), delimiter=";"))
    col = rows[0].index("Расчётный расход, г")
    assert sorted(r[col] for r in rows[1:]) == ["", "23,4"]


# --- hunt №7: a done Creality / MKS print back idle after an outage is finished ----------------------

T0 = 1_760_000_000.0


def _gap_run(kind, last_p, back_state="idle", back_name=None):
    m = JobMachine("p3-x", kind=kind)
    t, ops = T0, []
    ops += m.feed(Sample(ts=t, state="idle", kind=kind))
    for i in range(10):
        t += 80
        p = 10 + (last_p - 10) * i / 9.0
        ops += m.feed(Sample(ts=t, state="printing", kind=kind, progress=p, print_time=600 + 600 * i,
                             eta=max(0, 5400 - 600 * i), name="part.gcode"))
    for _ in range(12):                      # 12 x 80 s = 16 min without data
        t += 80
        ops += m.feed(Sample(ts=t, state="offline", online=False, kind=kind))
    t += 80
    ops += m.feed(Sample(ts=t, state=back_state, kind=kind, progress=0 if back_name else None,
                         name=back_name, print_time=0 if back_name else None, eta=7200 if back_name else None))
    return [o for o in ops if o.op == "close"]


@pytest.mark.parametrize("kind,last_p", [("mks", 100.0), ("creality", 99.5)])
def test_done_print_back_idle_after_outage_is_finished(kind, last_p):
    closes = _gap_run(kind, last_p)
    assert len(closes) == 1
    job = closes[0].job
    assert (job.result, job.end_exact) == ("finished", 0)
    assert job.ended_at <= T0 + 10 * 80 + 13 * 80


def test_an_unfinished_print_back_idle_after_outage_is_still_lost():
    assert _gap_run("mks", 60.0)[0].job.result == "lost"
    assert _gap_run("mks", 100.0, back_state="printing", back_name="next.gcode")[0].job.result == "finished"


# --- hunt №17, №18: a restart of the panel ------------------------------------------------------------

PID = "p3-bambu"
NAME = "part_A"


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(ss, "DB_PATH", str(tmp_path / "ph.db"))
    st = ss.StateStore()
    yield st
    st._db.close()


def _drive(jdb_, m, samples):
    for s in samples:
        jdb_.apply(m, m.feed(s), now=s.ts)


def _before(t):
    out = [Sample(ts=t, state="idle", kind="bambu")]
    for i in range(10):
        t += 80
        out.append(Sample(ts=t, state="printing", kind="bambu", progress=20 + i, eta=6000 - 60 * i, name=NAME))
    t += 80
    out.append(Sample(ts=t, state="idle", kind="bambu"))          # blank idle of a reconnect
    for _ in range(20):                                             # then offline ~30 min
        t += 90
        out.append(Sample(ts=t, state="offline", online=False, kind="bambu"))
    return out, t


def _back(t):
    return [Sample(ts=t + 80 * (i + 1), state="printing", kind="bambu", progress=29 + i, eta=5400 - 60 * i,
                   name=NAME) for i in range(3)]


def _rows(st):
    return jj._dicts(st._db.execute("SELECT id, result, blips FROM print_jobs ORDER BY id"))


def test_control_without_restart_one_print(store):
    jdb_, m = JournalDB(store), JobMachine(PID, kind="bambu")
    before, t = _before(T0)
    _drive(jdb_, m, before + _back(t))
    rows = _rows(store)
    assert len(rows) == 1 and rows[0]["result"] is None and rows[0]["blips"] == 1


def test_restart_in_the_reopen_window_keeps_one_print(store):
    jdb_, m = JournalDB(store), JobMachine(PID, kind="bambu")
    before, t = _before(T0)
    _drive(jdb_, m, before)
    machines = JournalDB(store).load_open(now=t + 30)              # the panel restarts
    m2 = machines.get(PID) or JobMachine(PID, kind="bambu")
    _drive(JournalDB(store), m2, _back(t))
    rows = _rows(store)
    assert len(rows) == 1, rows                                     # was: [stopped, open]
    assert rows[0]["result"] is None and rows[0]["blips"] == 1


def test_restart_keeps_no_reopen_chance_for_a_print_that_cannot_come_back(store):
    jdb_, m = JournalDB(store), JobMachine(PID, kind="bambu")
    before, t = _before(T0)
    _drive(jdb_, m, before)
    # another print started and finished since: the stopped one is not the printer's newest any more
    _drive(jdb_, m, [Sample(ts=t + 10, state="printing", kind="bambu", progress=1, eta=900, name="other"),
                     Sample(ts=t + 100, state="finished", kind="bambu", progress=100, eta=0, name="other")])
    assert JournalDB(store).load_open(now=t + 200) == {}
    assert not store._db.execute("SELECT 1 FROM job_journal_meta WHERE key LIKE 'reopen:%'").fetchall()
    # a print closed as finished never leaves a note
    m3 = JobMachine("p9", kind="bambu")
    _drive(jdb_, m3, [Sample(ts=t, state="idle", kind="bambu"),
                      Sample(ts=t + 80, state="printing", kind="bambu", progress=50, eta=600, name="x"),
                      Sample(ts=t + 160, state="finished", kind="bambu", progress=100, eta=0, name="x")])
    assert JournalDB(store).load_open(now=t + 300) == {}


def _hist(st, s):
    st._db.execute("INSERT INTO printer_history (printer_id, label, kind, online, state, progress, eta_sec,"
                   " print_time, job_name, recorded_at) VALUES (?, 'L', 'creality', 1, ?, ?, ?, ?, ?, ?)",
                   ("p3-cr", s.state, s.progress, s.eta, s.print_time, s.name, s.ts))
    st._db.commit()


def _creality(t):
    out = [Sample(ts=t, state="idle", kind="creality")]
    for i in range(21):                                       # 70 -> 100 %, checkpoints at i = 0, 8, 16
        t += 80
        out.append(Sample(ts=t, state="printing", kind="creality", progress=70 + 30 * i / 20.0,
                          print_time=3000 + 80 * i, eta=max(0, 1600 - 80 * i), name="k1_part"))
    return out, t


def test_creality_done_during_a_3_min_restart_is_finished(store):
    jdb_, m = JournalDB(store), JobMachine("p3-cr", kind="creality")
    before, t = _creality(T0)
    _drive(jdb_, m, before)
    for s in before:
        _hist(store, s)                                      # the history saw 100 %
    stored = jj._dicts(store._db.execute("SELECT end_progress FROM print_jobs"))[0]
    machines = JournalDB(store).load_open(now=t + 150)      # restart; the print ended meanwhile
    m2 = machines["p3-cr"]
    assert (m2.job.lp, m2.job.le, m2.job.last_seen) == (100.0, 0, t)
    JournalDB(store).apply(m2, m2.feed(Sample(ts=t + 180, state="idle", kind="creality")), now=t + 180)
    row = jj._dicts(store._db.execute("SELECT result, end_progress FROM print_jobs"))[0]
    assert stored["end_progress"] < 99                       # the stale checkpoint
    assert row == {"result": "finished", "end_progress": 100.0}


def test_restart_never_takes_another_prints_progress(store):
    jdb_, m = JournalDB(store), JobMachine("p3-cr", kind="creality")
    before, t = _creality(T0)
    _drive(jdb_, m, before[:12])
    stored = jj._dicts(store._db.execute("SELECT end_progress FROM print_jobs"))[0]["end_progress"]
    _hist(store, Sample(ts=t, state="printing", kind="creality", progress=99.0, name="another_part"))
    m2 = JournalDB(store).load_open(now=t + 60)["p3-cr"]
    assert m2.job.lp == stored and m2.job.last_seen == t


# --- the poll loop: Telegram (hunt №4, №5) and the grace clone (hunt №44) --------------------------

class _PollStore:
    def __init__(self):
        self.items = {}

    def upsert(self, status):
        self.items[status.id] = status

    def get_one(self, pid):
        return self.items.get(pid)

    def record_snapshot(self, status):
        pass


class _Stop(Exception):
    pass


def _run_cycles(monkeypatch, collectors, tg, settings, cycles, prev_states=None, clock=None):
    """main.poll_loop for `cycles` cycles with a fake store / collectors / bot (hunt-repros/p7__pollharness.py)."""
    calls = {"n": 0}

    def _sleep(_s):
        calls["n"] += 1
        if clock is not None:
            clock["now"] += 60
        if calls["n"] >= cycles:
            raise _Stop

    now = (lambda: clock["now"]) if clock is not None else time.time
    monkeypatch.setattr(main, "time", SimpleNamespace(time=now, monotonic=time.monotonic, sleep=_sleep))
    monkeypatch.setattr(main, "_display_label", lambda pid, fallback: fallback)
    monkeypatch.setattr(main, "_in_maintenance", lambda pid: False)
    monkeypatch.setattr(main, "_note_signature", lambda pid, status: None)
    monkeypatch.setattr(main.job_journal, "observe", lambda status, now: None)
    monkeypatch.setattr(main.settings_service, "get_all", lambda: dict(settings))
    store = _PollStore()
    with pytest.raises(_Stop):
        main.poll_loop(store, collectors, tg, prev_states=prev_states or {})
    return store


class _Finished:
    def fetch(self):
        return {"state": "finish"}


def test_finish_goes_out_once_the_bot_recovers(monkeypatch):
    """hunt-repros/p7__test_finish_lost_when_bot_down.py"""
    answers = iter([False, True, True, True])   # bot down on the finishing poll, up afterwards
    sent = []

    class _Tg:
        def notify(self, text):
            ok = next(answers)
            if ok:
                sent.append(text)
            return ok

    settings = {"telegram_notify_on_finish": True, "telegram_finish_template": "OK {label}"}
    _run_cycles(monkeypatch, [("p1", PrinterKind.MKS, "P1", _Finished(), "mks")], _Tg(), settings,
                cycles=3, prev_states={"p1": PrinterState.PRINTING})
    assert sent == ["OK P1"], "finish never delivered after the bot came back: %r" % sent


def test_a_finish_already_seen_on_start_stays_quiet(monkeypatch):
    sent = []

    class _Tg:
        def notify(self, text):
            sent.append(text)
            return True

    settings = {"telegram_notify_on_finish": True, "telegram_finish_template": "OK {label}"}
    _run_cycles(monkeypatch, [("p1", PrinterKind.MKS, "P1", _Finished(), "mks")], _Tg(), settings, cycles=3)
    assert sent == []


ERR = 0x05008062
_PE = {"key": "pe:0500_8062", "src": "print_error", "code": "0500_8062", "severity": "pause"}


def _paused_bambu(monkeypatch, states):
    """normalize_bambu answers the next state of `states` on every poll (paused on 0500_8062)."""
    seq = iter(states)

    def normalize(pid, label, result, device_type=None):
        state = next(seq)
        return PrinterStatus(id=pid, label=label, kind=PrinterKind.BAMBU, online=True, state=state,
                             last_error="0500_8062" if state == PrinterState.PAUSED else None,
                             notifications=[dict(_PE)] if state == PrinterState.PAUSED else [])

    monkeypatch.setattr(main, "normalize_bambu", normalize)


class _Any:
    def fetch(self):
        return object()


class _SentTg:
    def __init__(self):
        self.sent = []

    def notify(self, text):
        self.sent.append(text)
        return True


PAUSE_SETTINGS = {"telegram_notify_on_paused": True, "telegram_paused_template": "PAUSE {label}",
                  "auto_ignore_print_errors": ["0500_8062"]}


@pytest.fixture
def ai_clean(monkeypatch):
    monkeypatch.setattr(auto_ignore, "_tries", {})
    monkeypatch.setattr(auto_ignore, "_skips", {})
    monkeypatch.setattr(auto_ignore, "_out", {}, raising=False)
    monkeypatch.setattr(auto_light, "_last", {})
    monkeypatch.setattr(auto_light, "_held", set(), raising=False)


def test_no_pause_message_for_a_pause_auto_ignore_presses(monkeypatch, ai_clean):
    """hunt-repros/p7__test_paused_tg_on_auto_ignore.py: P3's plate check at every start, pressed within 15 s."""
    _paused_bambu(monkeypatch, [PrinterState.PAUSED, PrinterState.PRINTING])
    tg = _SentTg()
    _run_cycles(monkeypatch, [("p3", PrinterKind.BAMBU, "P3", _Any(), "p2s")], tg, PAUSE_SETTINGS,
                cycles=2, prev_states={"p3": PrinterState.PRINTING})
    assert tg.sent == [], "pause message sent for an auto-ignored pause: %r" % tg.sent


def test_the_pause_message_goes_out_once_auto_ignore_gives_up(monkeypatch, ai_clean):
    _paused_bambu(monkeypatch, [PrinterState.PAUSED] * 4)
    clock = {"now": 1_000_000.0}
    tg = _SentTg()

    class _Collector:
        calls = 0

        def fetch(self):
            _Collector.calls += 1
            if _Collector.calls == 3:     # the auto-ignore thread used its tries meanwhile
                auto_ignore._tries[("p3", "0", ERR)] = (auto_ignore.MAX_TRIES, clock["now"])
            return object()

    _run_cycles(monkeypatch, [("p3", PrinterKind.BAMBU, "P3", _Collector(), "p2s")], tg, PAUSE_SETTINGS,
                cycles=4, prev_states={"p3": PrinterState.PRINTING}, clock=clock)
    assert tg.sent == ["PAUSE P3"]


def test_a_pause_on_another_code_is_said_at_once(monkeypatch, ai_clean):
    _paused_bambu(monkeypatch, [PrinterState.PAUSED])
    tg = _SentTg()
    _run_cycles(monkeypatch, [("p3", PrinterKind.BAMBU, "P3", _Any(), "p2s")], tg,
                dict(PAUSE_SETTINGS, auto_ignore_print_errors=["0C00_8009"]), cycles=1,
                prev_states={"p3": PrinterState.PRINTING})
    assert tg.sent == ["PAUSE P3"]


def test_a_held_pause_is_said_when_it_outlasts_every_try(monkeypatch, ai_clean):
    _paused_bambu(monkeypatch, [PrinterState.PAUSED] * 9)
    clock = {"now": 1_000_000.0}
    tg = _SentTg()
    # nothing tells the thread's state (it is not running): the hold ends after HOLD_MAX_S
    _run_cycles(monkeypatch, [("p3", PrinterKind.BAMBU, "P3", _Any(), "p2s")], tg, PAUSE_SETTINGS,
                cycles=9, prev_states={"p3": PrinterState.PRINTING}, clock=clock)
    assert auto_ignore.HOLD_MAX_S <= 8 * 60 and tg.sent == ["PAUSE P3"]


def _replace(status, **kw):
    import dataclasses
    return dataclasses.replace(status, **kw)


def test_the_grace_clone_counts_the_time_left_down():
    """hunt №44 (the server's part): the clone repeated eta_seconds, so «Дальше» (now + eta) moved on."""
    prev = PrinterStatus(id="p", label="P1", kind=PrinterKind.BAMBU, online=True, state=PrinterState.PRINTING,
                         eta_seconds=3600, progress_pct=40, last_update_ts=1000.0)
    g1 = main._build_grace_status(prev, reason="timeout", now=1060.0, device_type="P2S")
    g2 = main._build_grace_status(g1, reason="timeout", now=1180.0, device_type="P2S")
    assert (g1.eta_seconds, g2.eta_seconds) == (3540, 3420)
    assert 1060.0 + g1.eta_seconds == 1180.0 + g2.eta_seconds == 1000.0 + 3600      # one finish time
    assert (g2.progress_pct, g2.last_update_ts, g2.grace_period_active) == (40, 1180.0, True)
    paused = _replace(prev, state=PrinterState.PAUSED)
    assert main._build_grace_status(paused, "timeout", 1600.0, "P2S").eta_seconds == 3600   # a pause keeps it
    nearly = _replace(prev, eta_seconds=30)
    assert main._build_grace_status(nearly, "timeout", 1600.0, "P2S").eta_seconds == 0


# --- hunt №4 (the second half): a send that fails on the way is tried again ------------------------

def test_a_failed_send_is_retried_and_a_bad_message_is_not(monkeypatch):
    from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
    from app.services import telegram_bot_async as tba

    r = tba.TelegramReporter(token="x")
    r._chat_id = 123
    waits = []
    monkeypatch.setattr(r._stop_event, "wait", lambda s: waits.append(s) or False)
    attempts = []

    def flaky(self, coro):
        coro.close()
        attempts.append(1)
        if len(attempts) < 3:
            raise TelegramNetworkError(method=None, message="proxy down")

    monkeypatch.setattr(tba.asyncio.BaseEventLoop, "run_until_complete", flaky)
    r._send_now("готово")
    assert len(attempts) == 3 and waits == list(tba.NOTIFY_RETRY_S)

    attempts.clear()
    waits.clear()

    def bad(self, coro):
        coro.close()
        attempts.append(1)
        raise TelegramBadRequest(method=None, message="can't parse entities")

    monkeypatch.setattr(tba.asyncio.BaseEventLoop, "run_until_complete", bad)
    r._send_now("<b>")
    assert len(attempts) == 1 and waits == []


# --- hunt №10: the lamp goes on when auto-ignore will not press the pause ---------------------------

class _St:
    def __init__(self, d):
        self.d = d

    def to_dict(self):
        return dict(self.d)


def _light_args(sent, ctx):
    return dict(states=["paused", "error", "cancelled"], ignore_codes=["0500_8062"],
                send=lambda pid, a: sent.append((pid, a)) or {"status": "confirmed"},
                audit=lambda a: None, caps=lambda dd: {"light": True}, live=lambda pid: dict(ctx))


def _one(d):
    class _Store:
        def get_all(self):
            return [_St(d)]
    return _Store()


def test_light_on_when_auto_ignore_will_not_press(ai_clean):
    """hunt-repros/p7__test_auto_light_dark_when_auto_ignore_gave_up.py"""
    now = time.time()
    ctx = {"gcode_state": "PAUSE", "err": ERR, "job_id": "0"}
    auto_ignore._tries[("p3", "0", ERR)] = (auto_ignore.MAX_TRIES, now)   # this pause: tries used
    assert auto_ignore.should_ignore(ctx, ["0500_8062"], "p3", now) is False
    d = {"id": "p3", "label": "P3", "kind": "bambu", "online": True, "state": "printing", "light_on": False}
    sent = []
    auto_light.check_once(_one(d), **_light_args(sent, ctx))          # baseline: printing
    d["state"] = "paused"
    auto_light.check_once(_one(d), **_light_args(sent, ctx))          # steps into paused, auto-ignore gave up
    auto_light.check_once(_one(d), **_light_args(sent, ctx))          # still paused
    assert sent == [("p3", "light_on")], "lamp never turned on: %r" % sent


def test_light_waits_for_auto_ignore_and_comes_on_when_it_gives_up(ai_clean):
    ctx = {"gcode_state": "PAUSE", "err": ERR, "job_id": "0"}
    d = {"id": "p3", "label": "P3", "kind": "bambu", "online": True, "state": "printing", "light_on": False}
    sent = []
    auto_light.check_once(_one(d), **_light_args(sent, ctx))
    d["state"] = "paused"
    auto_light.check_once(_one(d), **_light_args(sent, ctx))           # auto-ignore will press: left alone
    auto_light.check_once(_one(d), **_light_args(sent, ctx))
    assert sent == []
    auto_ignore._tries[("p3", "0", ERR)] = (auto_ignore.MAX_TRIES, time.time())
    auto_light.check_once(_one(d), **_light_args(sent, ctx))           # it gave up: the lamp after all
    auto_light.check_once(_one(d), **_light_args(sent, ctx))
    assert sent == [("p3", "light_on")]
    # a pause it presses through: nothing, and nothing left waiting
    sent.clear()
    auto_ignore._tries.clear()
    for state in ("printing", "paused", "printing", "printing"):
        d["state"] = state
        auto_light.check_once(_one(d), **_light_args(sent, ctx))
    assert sent == [] and auto_light._held == set()


# --- hunt №37: each print gets its own three tries --------------------------------------------------

def test_each_new_print_gets_its_own_tries(ai_clean):
    """hunt-repros/p7__test_auto_ignore_tries_pooled_across_jobs.py"""
    d = {"id": "p3", "label": "P3", "kind": "bambu", "online": True, "state": "printing"}
    pressed = []
    t = {"now": 0.0}

    def ignore(pid, err):
        pressed.append(t["now"])
        return {"status": "confirmed"}

    live = lambda pid: {"gcode_state": "PAUSE" if d["state"] == "paused" else "RUNNING", "err": ERR, "job_id": "0"}

    def at(now, state):
        t["now"] = now
        d["state"] = state
        auto_ignore.check_once(_one(d), ["0500_8062"], live, ignore, lambda a: None, now=now,
                               maint=lambda pid: False)

    # four 7-minute prints back to back; each pauses once at plate detection, each ignore works
    for start in (0, 480, 960, 1440):
        at(start, "paused")
        at(start + 60, "printing")
    assert len(pressed) == 4, "pressed at %r; 4th print left paused (%r)" % (
        pressed, auto_ignore.view("p3", ERR, now=1440))
    # a plate that re-pauses right after the ignore still counts toward the three tries
    for i in range(5):
        at(5000 + i * auto_ignore.RETRY_S, "paused")
        at(5000 + i * auto_ignore.RETRY_S + 15, "printing")
    assert len(pressed) == 4 + auto_ignore.MAX_TRIES
    assert auto_ignore.gave_up("p3", ERR, now=5000 + 5 * auto_ignore.RETRY_S)


# --- hunt №38: hiding one Klipper shutdown does not hide another reason -----------------------------

def test_mute_of_one_shutdown_reason_does_not_hide_another():
    """hunt-repros/p7__test_klipper_mute_hides_other_reason.py"""
    old = nf.build_klipper({"host_state": "shutdown", "host_message": "Lost communication with MCU 'mcu'"})
    new = nf.build_klipper({"host_state": "shutdown",
                            "host_message": "Heater extruder not heating at expected rate"})
    mutes = {"k1": [old[0]["key"]]}           # admin pressed «Скрыть» on the MCU shutdown (a bare key)
    nm.mark("k1", new, mutes)
    assert new[0]["text"] != old[0]["text"]
    assert new[0]["muted"] is False, "new shutdown %r hidden by the old mute %r" % (new[0]["text"], old[0]["key"])
    # muted by the reason it was hidden for; the record's own key is unchanged (its buttons use it)
    assert old[0]["key"] == new[0]["key"] == "klipper:shutdown"
    nm.mark("k1", old, {"k1": [old[0]["mute_key"]]})
    nm.mark("k1", new, {"k1": [old[0]["mute_key"]]})
    assert (old[0]["muted"], new[0]["muted"]) == (True, False)
    assert nm.valid_key(old[0]["mute_key"])
    other = [{"key": "pe:0500_8062", "src": "print_error"}]
    nm.mark("k1", other, {"k1": ["pe:0500_8062"]})
    assert other[0]["muted"] is True                                     # other records: by their key


def test_the_page_muting_a_bare_klipper_key_mutes_that_reason(monkeypatch):
    from app.services import printer_commands, settings_service
    stored = {}
    monkeypatch.setattr(settings_service, "get", lambda key: copy.deepcopy(stored.get(key, {})))
    monkeypatch.setattr(settings_service, "set_many", lambda items: stored.update(copy.deepcopy(items)))
    shown = nf.build_klipper({"host_state": "shutdown", "host_message": "Lost communication with MCU 'mcu'"})
    status = SimpleNamespace(notifications=shown)
    monkeypatch.setattr(printer_commands, "_store", SimpleNamespace(get_one=lambda pid: status))
    changed, keys = nm.set_muted("k1", "klipper:shutdown", True)
    assert changed and keys == [shown[0]["mute_key"]]
    later = nf.build_klipper({"host_state": "shutdown", "host_message": "Heater extruder not heating"})
    nm.mark("k1", later, nm.current())
    nm.mark("k1", shown, nm.current())
    assert (shown[0]["muted"], later[0]["muted"]) == (True, False)
    stored["notification_mutes"] = {"k1": ["klipper:shutdown"]}           # left by an older version
    assert nm.set_muted("k1", "klipper:shutdown", False) == (True, [])
    assert nm.set_muted("k1", "klipper:shutdown", False) == (False, [])


# --- hunt №11, 27, 28, 29: auto-drying ----------------------------------------------------------------

AD_NOW = dt.datetime(2026, 10, 12, 9, 0, tzinfo=dt.timezone.utc).timestamp()   # 12:00 MSK
H = 3600
KEY = "bambu-1__0"


def _slot(t):
    return {"type": t, "color": "FFF144", "name": None, "remain_pct": 80, "empty": False, "nozzle_temp": [220, 260]}


EMPTY = {"type": None, "color": None, "name": None, "remain_pct": None, "empty": True, "nozzle_temp": None}
PETG2 = [_slot("PETG"), _slot("PETG"), EMPTY, EMPTY]


def _dry(active=False, remaining=None):
    d = {"type": "n3f", "status": "drying" if active else "off", "phase": None, "active": active,
         "remaining_min": remaining, "temp": 65 if active else None, "hours": 12 if active else None,
         "filament": "PETG" if active else None, "blockers": [], "feeding": False}
    d.update(ams_drying._policy("n3f", PETG2, False))
    return d


def _ad_rows(now, rh=30, temp=26.0, hours=2.5, step=90):
    n = int(hours * H // step)
    return [[now - (n - i) * step, rh, None, temp] for i in range(n + 1)]


def _ad_obs(now, active=False, remaining=None, **kw):
    o = {"key": KEY, "present": True, "online": True, "printing": False, "busy": False, "dry_ok": True,
         "cmd_ok": True, "dry": _dry(active, remaining), "rh": 30, "temp": 26, "slots": PETG2,
         "rows": [] if active else _ad_rows(now), "clock_jump": False, "tz_offset_min": 180}
    o.update(kw)
    return o


def _ad_cfg(**kw):
    c = copy.deepcopy(ad.DEFAULT_CONFIG)
    c.update(enabled=True, dry_run=False, **kw)            # max_runs 2, target 20 %
    return ad.validate_config(c)


def _running(**kw):
    st = ad.new_state()
    st.update(phase="running", origin="auto", series=1, run_started_at=AD_NOW - 2 * H,
              run_expected_end=AD_NOW + 10 * H, run_temp=65, run_hours=12, run_filament="PETG",
              confirm_by=AD_NOW - 2 * H + 300, seen_active=True, last_seen_at=AD_NOW - 60, last_remaining_min=600,
              remaining_changed_at=AD_NOW - 60, rh_before=27, t_before=26, runs=[AD_NOW - 2 * H])
    st.update(kw)
    return st


def test_switched_off_fragment_does_not_end_the_series():
    """hunt-repros/p4__test_interrupt_eats_series.py"""
    c = _ad_cfg()
    d = ad.decide(_running(), _ad_obs(AD_NOW), c, AD_NOW)          # the printer switched it off after 2 h of 12 h
    assert [e["kind"] for e in d.events] == ["interrupted"]
    d = ad.decide(d.state, _ad_obs(AD_NOW + 60), c, AD_NOW + 60)
    d = ad.decide(d.state, _ad_obs(AD_NOW + 300), c, AD_NOW + 300)
    a = d.actions[0]                                                # the redo of the switched-off run
    assert a["series"] == 2
    st, _, _ = ad.apply_start_result(d.state, a, {"success": True, "status": "confirmed"}, "auto", AD_NOW + 300)
    t = AD_NOW + 600
    d = ad.decide(st, _ad_obs(t, active=True, remaining=715), c, t)
    end = AD_NOW + 300 + 12 * H
    d = ad.decide(d.state, _ad_obs(end - 60, active=True, remaining=1), c, end - 60)
    d = ad.decide(d.state, _ad_obs(end + 60), c, end + 60)          # the redo ran its full 12 h
    assert [e["kind"] for e in d.events] == ["finish"]
    d = ad.decide(d.state, _ad_obs(end + 120), c, end + 120)        # measured: 30 % > target 20 %
    m = [e for e in d.events if e["kind"] == "measure"][0]
    # only ONE complete run so far: the series is not declared unreachable
    assert m["outcome"] != "unreachable" and d.state["backoff_until"] is None, m
    assert "unreachable" not in [e["kind"] for e in d.events]


def test_two_full_runs_above_the_target_still_end_the_series():
    c = _ad_cfg()
    st = ad.new_state()
    st.update(phase="settle", origin="auto", series=2, end_kind="finish", dry_end_at=AD_NOW - H - 60,
              settle_until=AD_NOW - 60, measure_entry=5, rh_before=27, t_before=26, run_temp=65,
              run_started_at=AD_NOW - 13 * H, runs=[AD_NOW - 23 * H, AD_NOW - 13 * H])
    d = ad.decide(st, _ad_obs(AD_NOW), c, AD_NOW)
    assert [e["outcome"] for e in d.events if e["kind"] == "measure"] == ["unreachable"]
    assert ad.load_state({"series_cut": -1})["series_cut"] == 0 and ad.load_state({})["series_cut"] == 0


def test_limit_until_is_when_the_limit_really_lifts():
    """hunt-repros/p4__test_limit_until_wrong.py"""
    c = _ad_cfg(max_runs=1)
    st = ad.new_state()
    st["runs"] = [AD_NOW - 3 * H, AD_NOW - 10 * H]          # e.g. two foreign dryings today
    d = ad.decide(st, _ad_obs(AD_NOW), c, AD_NOW)
    assert d.status["kind"] == "limit"
    until = d.status["until"]                                # shown to the user as «лимит до ...»
    assert until == AD_NOW - 3 * H + 24 * H
    assert ad.decide(d.state, _ad_obs(until + 60), c, until + 60).status["kind"] != "limit"
    assert ad.decide(d.state, _ad_obs(until - 60), c, until - 60).status["kind"] == "limit"


def test_run_seen_running_out_is_finish_after_long_outage():
    """hunt-repros/p4__test_clock_jump_hides_finish.py"""
    c = _ad_cfg()
    # our run, last seen 7 h ago with 1 min left (it ran out), then the panel was down
    st = _running(run_started_at=AD_NOW - 19 * H, run_expected_end=AD_NOW - 7 * H + 60,
                  confirm_by=AD_NOW - 19 * H + 300, last_seen_at=AD_NOW - 7 * H, last_remaining_min=1,
                  remaining_changed_at=AD_NOW - 7 * H, runs=[], interrupts=1)
    d = ad.decide(st, _ad_obs(AD_NOW, rows=[], clock_jump=True), c, AD_NOW)   # gap 7 h > 6 h
    assert [e["kind"] for e in d.events] == ["finish"]
    assert d.events[0]["dur"] == 12 * H and d.state["interrupts"] == 0
    # 5 min left at the last look: across the jump that proves nothing (still "lost")
    d = ad.decide(dict(st, last_remaining_min=5), _ad_obs(AD_NOW, rows=[], clock_jump=True), c, AD_NOW)
    assert [e["kind"] for e in d.events] == ["lost"]


def test_parse_key_returns_none_for_superscript_digit():
    """hunt-repros/p4__test_parse_key_unicode_digit.py"""
    assert ad.parse_key("bambu-1__²") is None and ad.parse_key("bambu-1__١") is None
    assert ad.parse_key("bambu-1__0") == ("bambu-1", 0)
