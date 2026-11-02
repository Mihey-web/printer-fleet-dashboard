"""Print journal read API (spec_job_journal §7.1-7.3, §11.4): list, stats, CSV.

Auth is stubbed as in test_ams_api; print_jobs lives in a tmp DB. Job names
are invented. Local days and CSV times use a fixed +03:00 offset.
"""
import csv
import inspect
import io
import json
import logging
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.api import auth as api_auth
from app.api import main as api_main
from app.domain.user_models import UserRole
from app.services import auth_service, job_queries
from app.services import job_journal as jj

MSK = timezone(timedelta(hours=3))
T0 = datetime(2026, 10, 10, tzinfo=MSK).timestamp()   # local midnight 10.10
H, D = 3600.0, 86400.0
FR, TO = T0, T0 + 3 * D
NAME = "Деталь A"
SECRET = "fixture_job_secret"
INJECT = "=cmd|' /C calc'!A0"
LABELS = {"p1": "P2S-1", "p2": "H2S-1"}


class FakeStore:
    def get_all(self):
        return []


def _pauses(*items):
    return json.dumps([{"at": 0, "sec": 60, "code": c, "text": t} for c, t in items], ensure_ascii=False)


def _fin(start, end, **kw):
    return dict(dict(result="finished", started_at=start, ended_at=end, last_seen=end,
                     actual_sec=int(end - start)), **kw)


# A fixed journal: 16 prints, one of them before the window, one in maintenance.
JOBS = {
    "A": _fin(T0 + 1 * H, T0 + 3 * H, planned_sec=6900, planned_src="slicer"),
    "B": _fin(T0 + 4 * H, T0 + 6 * H, planned_sec=6000, planned_src="printer", paused_sec=600, pauses=1,
              pauses_json=_pauses(("stage:paused_user", None))),
    "C": _fin(T0 + 7 * H, T0 + 8 * H, planned_sec=3000, planned_src="linear"),
    "D": _fin(T0 + 9 * H, T0 + 10 * H, planned_sec=3500, planned_src="slicer", start_exact=0, job_name=INJECT),
    "E": dict(_fin(T0 + 11 * H, T0 + 12 * H), result="failed", error_code="0300_4000",
              error_text="Ошибка подогрева стола", paused_sec=300, pauses=2,
              pauses_json=_pauses(("0700_8011", "Филамент закончился"), ("stage:paused_user", None))),
    "F": dict(_fin(T0 + D + 1 * H, T0 + D + 2 * H), result="failed", error_code="0300_4000",
              error_text="Ошибка подогрева стола"),
    "G": dict(_fin(T0 + 2 * H, T0 + 5 * H), printer_id="p2", result="power_loss", error_code="0300_8007",
              error_text="Пропало питание", power_losses=1, paused_sec=120, pauses=1,
              pauses_json=_pauses(("0300_8007", "Пропало питание"))),
    "H": dict(_fin(T0 + 6 * H, T0 + 6.5 * H), printer_id="p2", result="stopped"),
    "I": dict(_fin(T0 + D + 3 * H, T0 + D + 4 * H), printer_id="p2", result="cancelled", error_code="0300_400C"),
    "J": dict(_fin(T0 + D + 5 * H, T0 + D + 6 * H), printer_id="p2", result="lost", end_exact=0),
    "K": dict(printer_id="p2", started_at=T0 + 2 * D + 0.5 * H, last_seen=T0 + 2 * D + 2 * H, result=None,
              job_name=SECRET, end_progress=47.0, ctx="{}"),
    "L": dict(_fin(T0 + 1 * H, T0 + 2 * H), printer_id="p3", maint=1),
    "M": _fin(T0 + 2 * D + 1 * H, T0 + 2 * D + 3 * H, planned_sec=7000, planned_src="slicer", verdict="good"),
    "N": _fin(T0 + 2 * D + 4 * H, T0 + 2 * D + 5 * H, planned_sec=3600, planned_src="slicer", verdict="defect"),
    "O": _fin(T0 - 5 * H, T0 - 3 * H),
    "P": _fin(T0 - 1 * H, T0 + 0.5 * H, planned_sec=5400, planned_src="slicer"),
}


def _seed(path, jobs):
    conn = sqlite3.connect(path)
    try:
        jj.init_db(conn)
        ids = {}
        for key, job in jobs.items():
            fields = dict(dict(printer_id="p1", kind="bambu", model="P2S", job_name=NAME, start_exact=1,
                               end_exact=None if job.get("result") is None else 1, source="live"), **job)
            ids[key] = jj.insert_job(conn, fields, fields["last_seen"])
        conn.commit()
    finally:
        conn.close()
    return ids


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = str(tmp_path / "ph.db")
    monkeypatch.setattr(api_main, "_history_db_path", lambda: path)
    monkeypatch.setattr(api_main, "_current_labels", lambda rows=None: dict(LABELS))
    monkeypatch.setattr(job_queries, "_TZ", MSK)
    return path


@pytest.fixture
def ids(db):
    return _seed(db, JOBS)


def _client(monkeypatch, role=UserRole.VIEWER, cookie=True):
    import config as cfg
    monkeypatch.setattr(cfg, "JWT_SECRET", "test-secret", raising=False)
    monkeypatch.setattr(auth_service, "get_user_from_token", lambda *a, **k: ("u", role))
    monkeypatch.setattr(auth_service, "decode_token", lambda *a, **k: {"iat": 0})
    monkeypatch.setattr(api_auth, "token_subject_active", lambda *a, **k: True)
    c = TestClient(api_main.create_app(FakeStore()))
    if cookie:
        c.cookies.set("access_token", "stub")
    return c


@pytest.fixture
def client(monkeypatch, db):
    return _client(monkeypatch)


@pytest.fixture
def boss(monkeypatch, db):
    """An admin's client: the CSV export is theirs only (IA step 1)."""
    return _client(monkeypatch, role=UserRole.ADMIN)


def _reader(monkeypatch, path):
    """A client that may read `path`: the CSV takes an admin, the rest any logged-in user."""
    return _client(monkeypatch, role=UserRole.ADMIN if path.endswith(".csv") else UserRole.VIEWER)


def _list(client, **params):
    params = dict(dict(fr=FR, to=TO), **params)
    r = client.get("/api/history/jobs", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def _keys(ids, rows):
    by_id = {v: k for k, v in ids.items()}
    return [by_id[r["id"]] for r in rows]


# --- list ---
def test_list_returns_the_window_newest_first(client, ids):
    data = _list(client)
    assert _keys(ids, data["rows"]) == list("NMKJIFEDCHBGLAP")   # O ended before the window
    assert data["has_more"] is False
    row = data["rows"][0]
    assert tuple(row) == job_queries._ROW_FIELDS
    assert "ctx" not in row and "pauses_json" not in row
    assert (row["label"], row["job_name"], row["verdict"]) == ("P2S-1", NAME, "defect")


def test_list_labels_come_from_the_registry_or_fall_back_to_the_id(client, ids):
    page = _list(client)["rows"]
    rows = dict(zip(_keys(ids, page), page))
    assert (rows["A"]["label"], rows["G"]["label"], rows["L"]["label"]) == ("P2S-1", "H2S-1", "p3")


def test_list_filters(client, ids):
    assert _keys(ids, _list(client, printer="p2")["rows"]) == list("KJIHG")
    assert _keys(ids, _list(client, result="failed,power_loss")["rows"]) == list("FEG")
    assert _keys(ids, _list(client, result="open")["rows"]) == ["K"]
    assert _keys(ids, _list(client, result="open,lost, ")["rows"]) == ["K", "J"]
    assert _keys(ids, _list(client, include_maint="false", printer="p3")["rows"]) == []


def test_list_window_overlap_keeps_an_open_print_with_a_stale_last_seen(client, ids):
    # K's last_seen (checkpoint lag) is before fr, yet it is still running.
    rows = _list(client, fr=T0 + 2 * D + 3 * H)["rows"]
    assert _keys(ids, rows) == ["N", "M", "K"]   # M ended exactly at fr
    assert _keys(ids, _list(client, fr=T0 - 4 * H, to=T0 - 0.5 * H)["rows"]) == ["P", "O"]


def test_list_pages_with_limit_plus_one(client, ids):
    first = _list(client, limit=2)
    assert (_keys(ids, first["rows"]), first["has_more"]) == (["N", "M"], True)
    last = _list(client, limit=2, offset=14)
    assert (_keys(ids, last["rows"]), last["has_more"]) == (["P"], False)
    assert _list(client, limit=15)["has_more"] is False


def test_list_pause_codes_are_the_first_three_distinct(client, ids):
    page = _list(client)["rows"]
    rows = dict(zip(_keys(ids, page), page))
    assert rows["E"]["pause_codes"] == ["0700_8011", "stage:paused_user"]
    assert rows["A"]["pause_codes"] == []
    many = _pauses(("A1", None), (None, None), ("A1", None), ("B2", None), ("C3", None), ("D4", None))
    assert job_queries._pause_codes(many) == ["A1", "B2", "C3"]
    assert job_queries._pause_codes("not json") == job_queries._pause_codes('{"a":1}') == []


def test_list_pause_list_is_every_pause_with_its_time(client, ids):
    # IA §7 (plan step 9): a row's details show every pause the journal kept — when, how long, why
    page = _list(client)["rows"]
    rows = dict(zip(_keys(ids, page), page))
    assert rows["E"]["pause_list"] == [{"at": 0, "sec": 60, "code": "0700_8011", "text": "Филамент закончился"},
                                       {"at": 0, "sec": 60, "code": "stage:paused_user", "text": None}]
    assert rows["A"]["pause_list"] == []
    odd = json.dumps([{"at": 1793000000.6, "sec": None, "code": "", "text": 5}, {"at": "x", "sec": True}, "junk",
                      {"at": float("nan")}])
    assert job_queries._pause_list(odd) == [{"at": 1793000000, "sec": None, "code": None, "text": None},
                                            {"at": None, "sec": None, "code": None, "text": None},
                                            {"at": None, "sec": None, "code": None, "text": None}]
    assert job_queries._pause_list("not json") == job_queries._pause_list(None) == []


def test_list_defaults_to_the_last_seven_days(client, db):
    now = time.time()
    ids = _seed(db, {"new": dict(started_at=now - H, last_seen=now - 60, result=None),
                     "old": _fin(now - 10 * D, now - 9 * D)})
    r = client.get("/api/history/jobs")
    assert r.status_code == 200
    assert _keys(ids, r.json()["rows"]) == ["new"]


@pytest.mark.parametrize("path", ["/api/history/jobs", "/api/history/jobs/stats", "/api/history/jobs.csv"])
def test_bad_windows_are_400(monkeypatch, ids, path):
    client = _reader(monkeypatch, path)
    assert client.get(path, params={"fr": TO, "to": FR}).status_code == 400
    assert client.get(path, params={"fr": "inf"}).status_code == 400
    assert client.get(path, params={"fr": "nan", "to": TO}).status_code == 400


@pytest.mark.parametrize("path", ["/api/history/jobs", "/api/history/jobs.csv"])
def test_unknown_result_is_400(monkeypatch, ids, path):
    r = _reader(monkeypatch, path).get(path, params={"fr": FR, "to": TO, "result": "finished,exploded"})
    assert r.status_code == 400
    assert r.json() == {"detail": "unknown result"}


def test_limit_bounds_are_422(monkeypatch, client, ids):
    assert client.get("/api/history/jobs", params={"limit": 2001}).status_code == 422
    assert client.get("/api/history/jobs", params={"limit": 0}).status_code == 422
    assert client.get("/api/history/jobs", params={"offset": -1}).status_code == 422
    assert client.get("/api/history/jobs", params={"limit": 2000}).status_code == 200
    assert _reader(monkeypatch, ".csv").get("/api/history/jobs.csv", params={"limit": 20001}).status_code == 422


def test_parse_results():
    assert job_queries.parse_results(None) is None
    assert job_queries.parse_results(" , ") is None
    assert job_queries.parse_results("open,failed,open") == ["open", "failed"]
    assert set(job_queries.RESULT_FILTERS) == set(jj.RESULTS) | {"open"}
    with pytest.raises(ValueError):
        job_queries.parse_results("FINISHED")


# --- stats ---
def _stats(client, **params):
    r = client.get("/api/history/jobs/stats", params=dict(dict(fr=FR, to=TO), **params))
    assert r.status_code == 200, r.text
    return r


def test_stats_on_a_fixed_journal(client, ids):
    data = _stats(client).json()
    assert (data["fr"], data["to"]) == (FR, TO)
    assert data["total"] == {
        "jobs": 14, "finished": 7, "cancelled": 1, "failed": 2, "power_loss": 1, "stopped": 1, "lost": 1,
        "open": 1,
        "success_pct": 58.3,          # 7 / 12: lost and open are not in the base
        "print_hours": 17.7,          # closed prints, pauses excluded
        # A 1.043, B 1.1 (pause excluded), M 1.029, N 1.0, P 1.0; C (linear) and D (inexact start) left out
        "acc_median": 1.029, "acc_p90": 1.077, "acc_n": 5,
        "good": 1, "defect": 1,
        "maint": 1,                   # the maintenance print left out (the list has it)
    }
    assert data["failure_codes"] == [
        {"code": "0300_4000", "text": "Ошибка подогрева стола", "n": 2, "result": "failed"},
        {"code": "0300_8007", "text": "Пропало питание", "n": 1, "result": "power_loss"},
    ]
    assert data["cancel_n"] == 1
    assert data["pause_reasons"] == [
        {"code": "stage:paused_user", "text": None, "n": 2},
        {"code": "0300_8007", "text": "Пропало питание", "n": 1},
        {"code": "0700_8011", "text": "Филамент закончился", "n": 1},
    ]


def test_stats_per_printer(client, ids):
    p1, p2 = _stats(client).json()["printers"]
    assert {k: p1[k] for k in ("printer_id", "label", "model", "jobs", "finished", "failed", "success_pct",
                               "acc_median", "acc_n", "avg_sec", "top_code", "top_code_text")} == {
        "printer_id": "p1", "label": "P2S-1", "model": "P2S", "jobs": 9, "finished": 7, "failed": 2,
        "success_pct": 77.8, "acc_median": 1.029, "acc_n": 5, "avg_sec": 5400,
        "top_code": "0300_4000", "top_code_text": "Ошибка подогрева стола"}
    assert {k: p2[k] for k in ("printer_id", "label", "jobs", "open", "success_pct", "acc_median", "avg_sec",
                               "top_code", "print_hours")} == {
        "printer_id": "p2", "label": "H2S-1", "jobs": 5, "open": 1, "success_pct": 0.0, "acc_median": None,
        "avg_sec": None, "top_code": "0300_8007", "print_hours": 5.5}


def test_stats_per_day_uses_local_days(client, ids):
    days = _stats(client).json()["per_day"]
    assert days == [
        {"day": "2026-10-10", "jobs": 8, "finished": 5, "bad": 3},   # P ended at 00:30 local
        {"day": "2026-10-11", "jobs": 3, "finished": 0, "bad": 3},
        {"day": "2026-10-12", "jobs": 3, "finished": 2, "bad": 0},   # K is open: its start day, not bad
        {"day": "2026-10-13", "jobs": 0, "finished": 0, "bad": 0},   # an empty day of the window is listed
    ]


def test_stats_per_day_follows_the_zone(client, ids, monkeypatch):
    monkeypatch.setattr(job_queries, "_TZ", timezone.utc)
    days = {d["day"]: d["jobs"] for d in _stats(client).json()["per_day"]}
    # T0 is 21:00 UTC on 09.10: only P ends before the UTC midnight; A ends right on it, F at 23:00 UTC.
    assert list(days) == ["2026-10-09", "2026-10-10", "2026-10-11", "2026-10-12"]
    assert (days["2026-10-09"], days["2026-10-10"], sum(days.values())) == (1, 8, 14)


def test_stats_leave_maintenance_out_unless_asked(client, ids):
    assert _stats(client).json()["total"]["jobs"] == 14
    data = _stats(client, include_maint="true").json()
    assert data["total"]["jobs"] == 15 and data["total"]["maint"] == 0
    assert _stats(client, printer="p3").json()["total"]["maint"] == 1
    assert [p["printer_id"] for p in data["printers"]] == ["p1", "p2", "p3"]
    assert _list(client, include_maint="false")["rows"].__len__() == 14


def test_stats_filter_by_printer(client, ids):
    data = _stats(client, printer="p2").json()
    assert data["total"]["jobs"] == 5
    assert [p["printer_id"] for p in data["printers"]] == ["p2"]
    assert data["failure_codes"] == [{"code": "0300_8007", "text": "Пропало питание", "n": 1,
                                      "result": "power_loss"}]


def test_stats_never_carry_job_names(client, ids):
    r = _stats(client, include_maint="true")
    for name in (NAME, SECRET, INJECT, "job_name"):
        assert name not in r.text
    assert "job_name" not in job_queries._STATS_COLS


def test_quantile_interpolates():
    q = job_queries._quantile
    assert q([], 0.5) is None
    assert q([2.0], 0.9) == 2.0
    assert q([1.0, 2.0, 3.0, 4.0], 0.5) == 2.5
    assert q([1.0, 2.0, 3.0, 4.0, 5.0], 0.9) == pytest.approx(4.6)


# --- CSV ---
def _csv(client, **params):
    r = client.get("/api/history/jobs.csv", params=dict(dict(fr=FR, to=TO), **params))
    assert r.status_code == 200, r.text
    return r


def test_csv_format(boss, ids):
    client = boss
    r = _csv(client)
    assert r.headers["content-type"] == "text/csv; charset=utf-8"
    assert r.headers["content-disposition"] == 'attachment; filename="forge-jobs-20261010-20261013.csv"'
    assert r.headers["cache-control"] == "no-store"
    assert r.content.startswith(b"\xef\xbb\xbf")
    text = r.content.decode("utf-8-sig")
    assert NAME in text and "\r\n" in text
    rows = list(csv.reader(io.StringIO(text), delimiter=";"))
    assert tuple(rows[0]) == job_queries.CSV_HEADER
    assert len(rows) == 16
    head = rows[0]
    by_key = dict(zip(_keys(ids, _list(client)["rows"]), (dict(zip(head, r)) for r in rows[1:])))
    a = by_key["A"]
    assert (a["Принтер"], a["Модель"], a["Задание"], a["Начало"], a["Конец"]) == (
        "P2S-1", "P2S", NAME, "10.10.2026 01:00", "10.10.2026 03:00")
    assert (a["План, мин"], a["Факт, мин"], a["Пауз"], a["Результат"]) == ("115", "120", "0", "Успешно")
    assert by_key["D"]["Начало"] == "~10.10.2026 09:00"
    assert by_key["J"]["Конец"] == "~11.10.2026 06:00"
    assert (by_key["K"]["Результат"], by_key["K"]["Конец"], by_key["K"]["Факт, мин"]) == ("Идёт", "", "")
    assert (by_key["E"]["Код"], by_key["E"]["Описание"], by_key["E"]["Пауза, мин"]) == (
        "0300_4000", "Ошибка подогрева стола", "5")
    # no «Годно / Брак» column (the owner, 29.10): M and N have a verdict in the journal, the export leaves it out
    assert "Оценка" not in head and len(head) == 14 and all(len(r) == 14 for r in rows[1:])
    assert not {"Годно", "Брак"} & {c for r in rows[1:] for c in r}
    assert by_key["G"]["Результат"] == "Питание"
    # the results in IA §10's words, the web page's list says the same (tests/test_web_jobs_ui.py)
    assert set(job_queries.RESULT_LABELS.values()) == {"Успешно", "Отменено", "Ошибка", "Питание", "Остановлено",
                                                      "Нет связи", "Идёт"}


def test_csv_in_the_panels_language(boss, ids):
    # «⇩ Выгрузить» of the English panel adds lang=en: the column names and the result words
    # from the panel's dictionary (app/web/i18n/en.js), the data as it is
    ru = list(csv.reader(io.StringIO(_csv(boss).content.decode("utf-8-sig")), delimiter=";"))
    en = list(csv.reader(io.StringIO(_csv(boss, lang="en").content.decode("utf-8-sig")), delimiter=";"))
    assert en[0] == ["Printer", "Model", "Job", "Start", "End", "Plan, min", "Actual, min", "Pauses",
                     "Paused, min", "Offline, min", "Result", "Code", "Description", "Layers"]
    assert len(en) == len(ru)
    words = {"Успешно": "Successful", "Отменено": "Cancelled", "Ошибка": "Error", "Питание": "Power loss",
             "Остановлено": "Stopped", "Нет связи": "No connection", "Идёт": "Running"}
    assert set(words) == set(job_queries.RESULT_LABELS.values())
    for r, e in zip(ru[1:], en[1:]):
        assert e[10] == words[r[10]]
        assert e[:10] + e[11:] == r[:10] + r[11:]                     # names, times, codes: unchanged
    # Russian by default, and for a language without a dictionary
    for lang in ("ru", "xx", "../en"):
        assert _csv(boss, lang=lang).content == _csv(boss).content
    assert boss.get("/api/history/jobs.csv", params=dict(fr=FR, to=TO, lang="x" * 9)).status_code == 422


def test_csv_neutralises_formulas(boss, ids):
    rows = list(csv.reader(io.StringIO(_csv(boss).content.decode("utf-8-sig")), delimiter=";"))
    names = [r[2] for r in rows[1:]]
    assert "'" + INJECT in names and INJECT not in names
    for ch in ("=", "+", "-", "@", "\t", "\r"):
        assert job_queries.csv_cell(ch + "1") == "'" + ch + "1"
    assert job_queries.csv_cell("Деталь =1") == "Деталь =1"
    assert job_queries.csv_cell(None) == ""
    assert job_queries.csv_cell(42) == "42"


def test_csv_uses_the_list_filters(boss, ids):
    rows = list(csv.reader(io.StringIO(_csv(boss, printer="p2", result="lost,open").content
                                       .decode("utf-8-sig")), delimiter=";"))
    assert [r[10] for r in rows[1:]] == ["Идёт", "Нет связи"]
    assert len(list(csv.reader(io.StringIO(_csv(boss, limit=3).content.decode("utf-8-sig"))))) == 4


def test_csv_is_gzipped_in_the_handler_once(boss, ids, monkeypatch):
    client = boss
    calls = []
    real = api_main.gzip.compress
    monkeypatch.setattr(api_main.gzip, "compress", lambda data, level: calls.append(len(data)) or real(data, level))
    plain = client.get("/api/history/jobs.csv", params={"fr": FR, "to": TO}, headers={"Accept-Encoding": "identity"})
    assert "content-encoding" not in plain.headers and not calls and len(plain.content) >= 1024
    assert plain.headers["vary"] == "Accept-Encoding"   # GZipMiddleware's, not twice
    packed = client.get("/api/history/jobs.csv", params={"fr": FR, "to": TO}, headers={"Accept-Encoding": "gzip"})
    assert packed.headers["content-encoding"] == "gzip" and "Accept-Encoding" in packed.headers["vary"]
    assert calls == [len(plain.content)]
    assert packed.content == plain.content   # decoded once: GZipMiddleware left it alone
    small = client.get("/api/history/jobs.csv", params={"fr": FR, "to": TO, "printer": "nope"},
                       headers={"Accept-Encoding": "gzip"})
    assert "content-encoding" not in small.headers and len(calls) == 1   # header only: not worth it


def _traced(monkeypatch):
    """The SQL job_queries runs (its read-only connections, traced)."""
    sql, real = [], job_queries._connect

    def connect(db):
        conn = real(db)
        if conn is not None:
            conn.set_trace_callback(sql.append)
        return conn
    monkeypatch.setattr(job_queries, "_connect", connect)
    return sql


def test_window_reads_are_bounded_by_the_end_index(client, ids, db, monkeypatch):
    sql = _traced(monkeypatch)
    _stats(client)
    _list(client, result="failed")
    _list(client)
    _list(client, result="failed", printer="p1")
    selects = [s for s in sql if s.startswith("SELECT") and "FROM print_jobs" in s and "sqlite_master" not in s]
    assert ["INDEXED BY idx_jobs_ended" in s for s in selects] == [True, True, False, False]
    conn = sqlite3.connect(db)
    plan = " ".join(r[3] for r in conn.execute(
        "EXPLAIN QUERY PLAN SELECT id FROM print_jobs INDEXED BY idx_jobs_ended WHERE started_at <= ?"
        " AND (ended_at >= ? OR ended_at IS NULL)", (TO, FR)))
    assert "idx_jobs_ended (ended_at>?)" in plan and "idx_jobs_ended (ended_at=?)" in plan
    # a journal written by older code has no such index: the reads still work
    conn.execute("DROP INDEX idx_jobs_ended")
    conn.commit()
    conn.close()
    assert _stats(client).json()["total"]["jobs"] == 14 and len(_list(client, result="failed")["rows"]) == 2


# --- serialization, access, names ---
def test_json_is_built_by_json_dumps_with_cyrillic_as_is(client, ids):
    for path in ("/api/history/jobs", "/api/history/jobs/stats"):
        r = client.get(path, params={"fr": FR, "to": TO})
        assert r.headers["content-type"] == "application/json"
        assert "Ошибка подогрева стола".encode("utf-8") in r.content
        assert b"\\u04" not in r.content
        assert b", " not in r.content and b'": ' not in r.content   # compact separators


def test_job_routes_are_sync_so_they_run_in_the_threadpool(client):
    paths = {"/api/history/jobs", "/api/history/jobs/stats", "/api/history/jobs.csv"}
    routes = [r for r in client.app.routes if getattr(r, "path", None) in paths]
    assert len(routes) == 3
    assert not any(inspect.iscoroutinefunction(r.endpoint) for r in routes)


@pytest.mark.parametrize("path", ["/api/history/jobs", "/api/history/jobs/stats", "/api/history/jobs.csv"])
def test_anonymous_gets_401_and_no_names(monkeypatch, db, ids, path):
    r = _client(monkeypatch, cookie=False).get(path, params={"fr": FR, "to": TO})
    assert r.status_code == 401
    assert NAME not in r.text


def test_a_viewer_sees_names_like_in_the_timeline(monkeypatch, db, ids):
    c = _client(monkeypatch, role=UserRole.VIEWER)
    assert NAME in {r["job_name"] for r in _list(c)["rows"]}


@pytest.mark.parametrize("params", [
    {"fr": FR, "to": TO},
    {"fr": TO, "to": FR},                      # a bad window: still 403, not 400
    {"fr": FR, "to": TO, "result": "exploded"},
    {"limit": 20001},                          # out of bounds: still 403, not 422
    {},
])
def test_the_csv_is_the_admins_only(monkeypatch, db, ids, params):
    # IA step 1 (§13): the export is closed to viewers on the server, whatever they ask for
    r = _client(monkeypatch, role=UserRole.VIEWER).get("/api/history/jobs.csv", params=params)
    assert r.status_code == 403 and r.json() == {"detail": "Admin role required"}
    assert NAME not in r.text and "content-disposition" not in r.headers
    assert _client(monkeypatch, role=UserRole.ADMIN).get(
        "/api/history/jobs.csv", params={"fr": FR, "to": TO}).status_code == 200
    # the list and the stats stay open to viewers
    viewer = _client(monkeypatch, role=UserRole.VIEWER)
    assert viewer.get("/api/history/jobs", params={"fr": FR, "to": TO}).status_code == 200
    assert viewer.get("/api/history/jobs/stats", params={"fr": FR, "to": TO}).status_code == 200


def test_a_session_without_a_role_gets_no_csv(monkeypatch, db, ids):
    r = _client(monkeypatch, role=None).get("/api/history/jobs.csv", params={"fr": FR, "to": TO})
    assert r.status_code == 403 and NAME not in r.text


def test_no_job_name_reaches_the_log(boss, ids, caplog):
    client = boss   # an admin: the CSV is theirs only
    caplog.set_level(logging.DEBUG)
    for path in ("/api/history/jobs", "/api/history/jobs/stats", "/api/history/jobs.csv"):
        assert client.get(path, params={"fr": FR, "to": TO, "include_maint": "true"}).status_code == 200
    assert client.get("/api/history/jobs", params={"result": "bogus"}).status_code == 400
    for name in (NAME, SECRET, INJECT):
        assert name not in caplog.text


# --- no journal yet ---
def test_missing_db_reads_as_an_empty_journal(boss, db, tmp_path):
    client = boss   # an admin: the CSV is theirs only
    assert _list(client) == {"rows": [], "has_more": False}
    stats = _stats(client).json()
    assert stats["total"]["jobs"] == 0 and stats["total"]["success_pct"] is None
    assert stats["printers"] == stats["failure_codes"] == stats["pause_reasons"] == []
    assert len(stats["per_day"]) == 4
    assert _csv(client).content.decode("utf-8-sig").count("\r\n") == 1
    assert not (tmp_path / "ph.db").exists()   # reading never creates the file


def test_db_without_the_journal_table_reads_as_empty(client, db):
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE printer_history (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()
    assert _list(client) == {"rows": [], "has_more": False}
    assert _stats(client).json()["total"]["jobs"] == 0


def _count(db):
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT COUNT(*), MAX(updated_at) FROM print_jobs").fetchone()
    finally:
        conn.close()


def test_reads_never_write(boss, ids, db):
    client = boss   # an admin: the CSV is theirs only
    before = _count(db)
    _list(client), _stats(client), _csv(client)
    assert _count(db) == before
    conn = job_queries._connect(db)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM print_jobs")
    finally:
        conn.close()


# --- «Годно / Брак» (§7.4, §9) ---
@pytest.fixture
def admin(monkeypatch, db):
    from types import SimpleNamespace
    from app.api import admin as api_admin
    audit = []
    monkeypatch.setattr(api_admin, "get_audit_service",
                        lambda: SimpleNamespace(log=lambda user, action, *a, **k: audit.append((user, action))))
    return SimpleNamespace(client=_client(monkeypatch, role=UserRole.ADMIN), audit=audit)


def _put(client, job_id, verdict="good", body=None):
    return client.put("/api/admin/history/jobs/%s/verdict" % job_id,
                      json={"verdict": verdict} if body is None else body)


def _marks(db):
    conn = sqlite3.connect(db)
    try:
        return {r[0]: r[1:] for r in conn.execute(
            "SELECT id, verdict, verdict_by, verdict_at, updated_at FROM print_jobs").fetchall()}
    finally:
        conn.close()


def test_admin_marks_a_print_and_gets_its_row_back(admin, ids, db):
    before = time.time()
    r = _put(admin.client, ids["A"], "defect")
    assert r.status_code == 200, r.text
    row = r.json()
    assert tuple(row) == job_queries._ROW_FIELDS
    assert (row["id"], row["verdict"], row["label"], row["job_name"], row["result"]) == (
        ids["A"], "defect", "P2S-1", NAME, "finished")
    verdict, by, at, updated = _marks(db)[ids["A"]]
    assert (verdict, by) == ("defect", "u") and before <= at == updated
    assert admin.audit == [("u", "job_verdict:%d:defect" % ids["A"])]
    # the list and the stats see it: M good, N defect, now A defect
    assert {j["id"]: j["verdict"] for j in _list(admin.client)["rows"]}[ids["A"]] == "defect"
    total = _stats(admin.client).json()["total"]
    assert (total["good"], total["defect"]) == (1, 2)


def test_a_mark_is_changed_and_cleared(admin, ids, db):
    assert _put(admin.client, ids["M"], "defect").json()["verdict"] == "defect"
    r = _put(admin.client, ids["M"], None)
    assert r.status_code == 200 and r.json()["verdict"] is None
    assert _marks(db)[ids["M"]][:3] == (None, None, None)   # no mark: no author, no time either
    assert [a for _, a in admin.audit] == ["job_verdict:%d:defect" % ids["M"], "job_verdict:%d:clear" % ids["M"]]
    total = _stats(admin.client).json()["total"]
    assert (total["good"], total["defect"]) == (0, 1)


def test_a_running_print_takes_no_mark(admin, ids, db):
    before = _marks(db)
    r = _put(admin.client, ids["K"], "good")
    assert r.status_code == 409
    assert _marks(db) == before and admin.audit == []
    assert SECRET not in r.text


def test_an_unknown_print_is_404(admin, ids, db):
    before = _marks(db)
    assert _put(admin.client, max(ids.values()) + 1, "good").status_code == 404
    assert _marks(db) == before and admin.audit == []


def test_no_journal_is_404_and_creates_nothing(admin, db, tmp_path):
    assert _put(admin.client, 1, "good").status_code == 404
    assert not (tmp_path / "ph.db").exists()
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE printer_history (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()
    assert _put(admin.client, 1, "good").status_code == 404
    assert admin.audit == []


@pytest.mark.parametrize("body", [{"verdict": "meh"}, {"verdict": 1}, {"verdict": ["good"]}, {}, [], "good"])
def test_bad_bodies_are_400(admin, ids, db, body):
    before = _marks(db)
    assert _put(admin.client, ids["A"], body=body).status_code == 400
    assert _marks(db) == before and admin.audit == []


def test_a_bad_id_is_422(admin, ids):
    assert admin.client.put("/api/admin/history/jobs/x/verdict", json={"verdict": "good"}).status_code == 422
    assert admin.audit == []


def test_viewers_and_anonymous_cannot_mark(monkeypatch, db, ids):
    before = _marks(db)
    r = _put(_client(monkeypatch, role=UserRole.VIEWER), ids["A"], "good")
    assert r.status_code == 403 and r.json()["detail"] == "Admin access required"   # the middleware's answer
    assert _put(_client(monkeypatch, role=UserRole.ADMIN, cookie=False), ids["A"], "good").status_code == 401
    assert _marks(db) == before


def test_marks_never_put_names_in_the_audit_or_the_log(admin, ids, caplog):
    caplog.set_level(logging.DEBUG)
    for key in ("A", "D", "G"):
        assert _put(admin.client, ids[key], "defect").status_code == 200
    assert _put(admin.client, ids["K"], "good").status_code == 409
    lines = [a for _, a in admin.audit]
    assert lines == ["job_verdict:%d:defect" % ids[k] for k in ("A", "D", "G")]
    for name in (NAME, SECRET, INJECT, "P2S-1", "H2S-1", "p1", "p2"):
        assert not any(name in a for a in lines)
    for name in (NAME, SECRET, INJECT):
        assert name not in caplog.text


def test_the_mark_answer_is_json_dumps_with_cyrillic_as_is(admin, ids):
    r = _put(admin.client, ids["E"], "good")
    assert r.headers["content-type"] == "application/json"
    assert "Ошибка подогрева стола".encode("utf-8") in r.content and b"\\u04" not in r.content


def test_the_mark_route_is_sync(admin):
    route = next(r for r in admin.client.app.routes
                 if getattr(r, "path", None) == "/api/admin/history/jobs/{job_id}/verdict")
    assert route.methods == {"PUT"}
    assert not inspect.iscoroutinefunction(route.endpoint)


def test_set_verdict_statuses(db, ids):
    with pytest.raises(ValueError):
        jj.set_verdict(db, ids["A"], "meh", "u")
    assert jj.set_verdict(db, ids["A"], "good", "u", now=T0 + 99) == "ok"
    assert _marks(db)[ids["A"]] == ("good", "u", T0 + 99, T0 + 99)
    assert jj.set_verdict(db, ids["K"], "good", "u") == "open"
    assert jj.set_verdict(db, max(ids.values()) + 1, None, "u") == "missing"
    assert jj.set_verdict(db + ".none", 1, "good", "u") == "missing"
