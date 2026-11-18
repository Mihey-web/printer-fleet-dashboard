"""«Пластик в день» (17.11.2026): the plastic the farm used per day, from the slicer's planned grams
of each print (planned_filament_g), spread over the time the print ran."""
import datetime as dt
import sqlite3

import pytest

from app.services import job_journal as jj
from app.services import job_queries as jq

UTC = dt.timezone.utc
OMSK = dt.timezone(dt.timedelta(hours=6))


def _ts(s, tz=UTC):
    return dt.datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=tz).timestamp()


@pytest.fixture
def jdb(tmp_path, monkeypatch):
    monkeypatch.setattr(jq, "_TZ", UTC)
    path = str(tmp_path / "ph.db")
    conn = sqlite3.connect(path)
    jj.init_db(conn)
    yield path, conn
    conn.close()


def _job(conn, start, end, grams=None, result="finished", progress=None, last_seen=None, printer_id="P1"):
    st, en = _ts(start), (_ts(end) if end else None)
    fields = dict(printer_id=printer_id, kind="bambu", model="P2S", job_name="part", started_at=st, start_exact=1,
                  ended_at=en, end_exact=1 if en else None, last_seen=_ts(last_seen) if last_seen else (en or st),
                  result=result, actual_sec=int(en - st) if en else None, source="live",
                  planned_filament_g=grams, end_progress=progress, updated_at=en or st)
    cols = ",".join(fields)
    conn.execute("INSERT INTO print_jobs (%s) VALUES (%s)" % (cols, ",".join("?" * len(fields))), list(fields.values()))
    conn.commit()


def _days(stats):
    return {d["day"]: (d["grams"], d["no_weight"]) for d in stats["per_day"]}


def test_a_print_over_midnight_splits_its_plastic_by_the_hours_it_ran(jdb):
    path, conn = jdb
    _job(conn, "2026-11-16 20:00", "2026-11-17 02:00", grams=300)       # 4 h on the 16th, 2 h on the 17th
    _job(conn, "2026-11-17 10:00", "2026-11-17 12:00", grams=50.5)
    stats = jq.job_stats(path, _ts("2026-11-16 00:00"), _ts("2026-11-18 00:00"))
    assert _days(stats) == {"2026-11-16": (200.0, 0), "2026-11-17": (150.5, 0)}
    assert stats["total"]["grams"] == 350.5 and stats["total"]["no_weight"] == 0


def test_an_unfinished_print_counts_the_share_it_printed(jdb):
    path, conn = jdb
    _job(conn, "2026-11-16 08:00", "2026-11-16 10:00", grams=200, result="cancelled", progress=25)
    _job(conn, "2026-11-16 11:00", "2026-11-16 12:00", grams=80, result="failed", progress=None)   # progress unknown: 0
    _job(conn, "2026-11-16 13:00", None, grams=400, result=None, progress=50, last_seen="2026-11-16 15:00")  # running
    stats = jq.job_stats(path, _ts("2026-11-16 00:00"), _ts("2026-11-17 00:00"))
    assert _days(stats) == {"2026-11-16": (250.0, 0)}                  # 50 + 0 + 200


def test_prints_without_a_weight_are_counted_not_guessed(jdb):
    path, conn = jdb
    _job(conn, "2026-11-13 08:00", "2026-11-13 10:00")                  # before the panel recorded grams
    _job(conn, "2026-11-13 12:00", "2026-11-13 13:00", grams=0)        # a zero weight is no weight
    _job(conn, "2026-11-14 08:00", "2026-11-14 10:00", grams=120)
    stats = jq.job_stats(path, _ts("2026-11-13 00:00"), _ts("2026-11-15 00:00"))
    assert _days(stats) == {"2026-11-13": (0.0, 2), "2026-11-14": (120.0, 0)}
    assert [d["weighed"] for d in stats["per_day"]] == [0, 1]
    assert stats["total"]["grams"] == 120.0 and stats["total"]["no_weight"] == 2


def test_only_the_part_inside_the_window_counts(jdb):
    path, conn = jdb
    _job(conn, "2026-11-15 18:00", "2026-11-16 06:00", grams=120)       # 12 h, 6 of them on the 16th
    stats = jq.job_stats(path, _ts("2026-11-16 00:00"), _ts("2026-11-17 00:00"))
    assert _days(stats) == {"2026-11-16": (60.0, 0)} and stats["total"]["grams"] == 60.0


def test_the_days_are_the_pages_zone(jdb, monkeypatch):
    path, conn = jdb
    monkeypatch.setattr(jq, "ZoneInfo", lambda name: {"Asia/Omsk": OMSK}[name])
    # 16.11 17:00–19:00 UTC = 16.11 23:00 – 17.11 01:00 in Omsk: half on each Omsk day
    _job(conn, "2026-11-16 17:00", "2026-11-16 19:00", grams=100)
    fr, to = _ts("2026-11-15 18:00"), _ts("2026-11-17 18:00")         # 16.11 and 17.11 in Omsk
    assert _days(jq.job_stats(path, fr, to, tz="Asia/Omsk")) == {"2026-11-16": (50.0, 0), "2026-11-17": (50.0, 0)}


def test_each_day_carries_the_grams(jdb):
    path, conn = jdb
    _job(conn, "2026-11-16 08:00", "2026-11-16 10:00", grams=42.25)
    stats = jq.job_stats(path, _ts("2026-11-16 00:00"), _ts("2026-11-17 00:00"))
    day = stats["per_day"][0]
    assert set(day) == {"day", "jobs", "finished", "bad", "grams", "no_weight", "weighed"} and day["grams"] == 42.2
    assert (day["weighed"], day["no_weight"]) == (1, 0)


def test_each_printer_carries_its_grams(jdb):
    path, conn = jdb
    _job(conn, "2026-11-16 08:00", "2026-11-16 10:00", grams=100, printer_id="P1")
    _job(conn, "2026-11-16 09:00", "2026-11-16 11:00", grams=40, printer_id="P2", result="cancelled", progress=50)
    _job(conn, "2026-11-16 12:00", "2026-11-16 13:00", printer_id="P2")              # no weight
    stats = jq.job_stats(path, _ts("2026-11-16 00:00"), _ts("2026-11-17 00:00"))
    by = {p["printer_id"]: (p["grams"], p["no_weight"]) for p in stats["printers"]}
    assert by == {"P1": (100.0, 0), "P2": (20.0, 1)}
    assert stats["total"]["grams"] == 120.0 and stats["total"]["no_weight"] == 1


def test_a_print_stopped_at_0_percent_used_nothing_known_weight_or_not(jdb):
    # an X1C cancelled after 42 s, before the panel read its file (16.11): 0 g, not «without weight»
    path, conn = jdb
    _job(conn, "2026-11-16 10:51", "2026-11-16 10:52", result="cancelled", progress=0)
    _job(conn, "2026-11-16 11:00", "2026-11-16 11:30", result="failed", progress=0)
    _job(conn, "2026-11-16 12:00", "2026-11-16 13:00", result="cancelled", progress=None)   # progress unknown
    _job(conn, "2026-11-16 14:00", "2026-11-16 15:00", result="cancelled", progress=30)     # printed, weight unknown
    _job(conn, "2026-11-16 16:00", None, result=None, progress=0, last_seen="2026-11-16 16:05")  # just started
    stats = jq.job_stats(path, _ts("2026-11-16 00:00"), _ts("2026-11-17 00:00"))
    day = stats["per_day"][0]
    assert (day["grams"], day["no_weight"], day["weighed"]) == (0.0, 3, 2)
    assert stats["total"]["no_weight"] == 3
