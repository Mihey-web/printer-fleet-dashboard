"""«История» of app/web/app.js (IA §7, plan step 8), run in node.

The /* hist-ui */ block is pure: the one head's filter (the period, «Свой…»,
the printer) as the address carries it, its window, the texts of the loaders
and of a phone's folded head, the one legend, and the merged «Статистика»
(the time shares of /api/history/states-summary and the prints of
/api/history/jobs/stats in one row per printer). It may use safeText, timeFmt
and the jobs-ui block. Plus the head's markup, wiring and styles, and the
one-time migration of forge-history-sub. Node runs with TZ=Europe/Moscow.
Printer names are invented. Skipped without node.
"""
import datetime
import html as htmllib
import json
import os
import re
import shutil
import sqlite3
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

from app.services import job_journal as jj
from app.services import job_queries

ROOT = Path(__file__).resolve().parents[1]
JS = (ROOT / "app" / "web" / "app.js").read_text(encoding="utf-8")
CSS = (ROOT / "app" / "web" / "styles.css").read_text(encoding="utf-8")
HTML = (ROOT / "app" / "web" / "index.html").read_text(encoding="utf-8")
API = (ROOT / "app" / "api" / "main.py").read_text(encoding="utf-8")
MSK = datetime.timezone(datetime.timedelta(hours=3))
NOW = datetime.datetime(2026, 10, 30, 12, 0, tzinfo=MSK).timestamp()
DAY = 86400
H = 3600


def _function(name):
    m = re.search(r"function %s\([^)]*\) \{.*?\n\s*\}" % name, JS, re.S)
    assert m, name
    return m.group(0)


def _js_function(name):
    """The whole `function name(...) {...}`: braces matched, strings and // comments skipped."""
    m = re.search(r"function %s\([^)]*\) \{" % name, JS)
    assert m, "%s() not found in app.js" % name
    i, depth, quote = m.end() - 1, 0, None
    while i < len(JS):
        ch = JS[i]
        if quote:
            if ch == "\\":
                i += 1
            elif ch == quote:
                quote = None
        elif ch in "'\"`":
            quote = ch
        elif JS.startswith("//", i):
            i = JS.index("\n", i)
            continue
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return JS[m.start():i + 1]
        i += 1
    raise AssertionError("%s(): unbalanced braces" % name)


def _block(tag):
    m = re.search(r"/\* %s:begin \*/(.*?)/\* %s:end \*/" % (tag, tag), JS, re.S)
    assert m, tag
    return m.group(1)


def _node(src):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    out = subprocess.run([node, "-"], input=src, capture_output=True, text=True, timeout=60, encoding="utf-8",
                         env=dict(os.environ, TZ="Europe/Moscow"))
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _lib():
    src = "\n".join(_function(n) for n in ("safeText", "timeFmt", "jobShortName"))
    m = re.search(r"const STAGE_LABELS = \{.*?\n\s*\};", JS, re.S)
    return src + "\n" + m.group(0) + "\n" + _js_function("stageLabel") + "\n" + _block("jobs-ui") + "\n" + _block("hist-ui")


def run(expr):
    return _node(_lib() + "\nconsole.log(JSON.stringify(" + expr + "));")


def js(v):
    return json.dumps(v, ensure_ascii=False)


class _Tags(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def tags(html):
    p = _Tags()
    p.feed(html)
    p.close()
    return p.tags


def text(html):
    return htmllib.unescape(re.sub(r"<[^>]+>", "", html))


def F(period="24h", printer=None, fr=None, to=None):
    return {"period": period, "from": fr, "to": to, "printer": printer}


# --- purity -------------------------------------------------------------------------------------

def test_block_is_pure():
    block = _block("hist-ui")
    for banned in ("document.", "window.", "fetch(", "apiFetch", "cachedAllPrinters", "localStorage", "location",
                   "histFilter =", "historySub ="):
        assert banned not in block, banned


# --- the filter and the address -------------------------------------------------------------------

def test_filter_from_the_address():
    got = run("[" + ",".join("histFilterFrom(%s)" % js(q) for q in (
        {}, None, {"period": "7d"}, {"period": "90d", "printer": "p-1"}, {"period": "1d"}, {"period": "__proto__"},
        {"period": "custom", "from": "2026-10-21", "to": "2026-10-28"},
        {"period": "custom", "from": "2026-10-28", "to": "2026-10-21"},     # the wrong way round
        {"period": "custom", "from": "2026-10-21"},                          # one day only
        {"period": "custom", "from": "2026-02-30", "to": "2026-03-01"},     # no such day
        {"period": "custom", "from": "21.10.2026", "to": "2026-10-28"},
        {"period": "custom", "from": "2026-10-21", "to": "2026-10-21"},     # a single day
        {"printer": ""}, {"printer": 5}, {"from": "2026-10-21", "to": "2026-10-28"},
    )) + "]")
    assert got == [
        F(), F(), F("7d"), F("90d", "p-1"), F(), F(),
        F("custom", fr="2026-10-21", to="2026-10-28"),
        F(), F(), F(), F(),
        F("custom", fr="2026-10-21", to="2026-10-21"),
        F(), F(), F(),
    ]


def test_query_leaves_the_defaults_out_and_round_trips():
    filters = [F(), F("7d"), F(printer="p-1"), F("30d", "p 2"), F("custom", "p-1", "2026-10-01", "2026-10-07")]
    got = run("[" + ",".join("histQueryOf(%s)" % js(f) for f in filters) + "]")
    assert got == [{}, {"period": "7d"}, {"printer": "p-1"}, {"period": "30d", "printer": "p 2"},
                   {"period": "custom", "from": "2026-10-01", "to": "2026-10-07", "printer": "p-1"}]
    back = run("[" + ",".join("histFilterFrom(%s)" % js(q) for q in got) + "]")
    assert back == filters
    assert run("[histSameFilter(%s, %s), histSameFilter(%s, %s), histSameFilter({}, %s)]" % (
        js(F("7d")), js({"period": "7d", "from": "x"}), js(F()), js(F("7d")), js(F()))) == [True, False, True]


def test_the_periods():
    periods = run("HIST_PERIODS")
    assert [(p["id"], p["label"], p["days"]) for p in periods] == [
        ("24h", "24 ч", 1), ("7d", "7 дн", 7), ("30d", "30 дн", 30), ("90d", "90 дн", 90)]
    assert run("[HIST_DEFAULT_PERIOD, HIST_CHRONO_DAYS, HIST_SUBS]") == ["24h", 7, ["jobs", "chrono", "stats", "actions"]]
    # the head's buttons are the periods and «Свой…», in this order
    head = HTML[HTML.index('id="histHead"'):HTML.index('data-hsub="jobs"')]
    btns = [(a["data-period"], re.sub(r"<[^>]+>", "", b)) for (t, a), b in zip(
        [x for x in tags(head) if "data-period" in x[1]], re.findall(r'data-period="[^"]*"[^>]*>([^<]*)</button>', head))]
    assert btns == [(p["id"], p["label"]) for p in periods] + [("custom", "Свой…")]


def test_the_window():
    def w(f, now=NOW):
        return run("histWindow(%s, %r)" % (js(f), now))
    assert w(F()) == {"fr": NOW - DAY, "to": NOW}
    assert w(F("90d")) == {"fr": NOW - 90 * DAY, "to": NOW}
    assert w(F("7d"), NOW + 0.5) == {"fr": NOW - 7 * DAY, "to": NOW + 1}      # whole seconds, the window covers now
    oct21 = datetime.datetime(2026, 10, 21, tzinfo=MSK).timestamp()
    # «Свой…»: from the start of its first day to the end of its last one (browser time)
    assert w(F("custom", fr="2026-10-21", to="2026-10-28")) == {"fr": oct21, "to": oct21 + 8 * DAY}
    assert w(F("custom", fr="2026-10-21", to="2026-10-21")) == {"fr": oct21, "to": oct21 + DAY}
    # never past now: today's last day ends now; a day still to come is an empty window
    assert w(F("custom", fr="2026-10-29", to="2026-10-30")) == {"fr": oct21 + 8 * DAY, "to": NOW}
    assert w(F("custom", fr="2026-11-02", to="2026-11-03")) == {"fr": NOW, "to": NOW}


def test_the_chronology_shows_a_week_at_most():
    got = run("[" + ",".join("histChronoWindow(%s, %r)" % (js(f), NOW) for f in (
        F(), F("7d"), F("30d"), F("90d"), F("custom", fr="2026-10-01", to="2026-10-10"),
        F("custom", fr="2026-10-24", to="2026-10-30"))) + "]")
    assert [(g["fr"], g["to"], g["clipped"], g["days"]) for g in got] == [
        (NOW - DAY, NOW, False, 1), (NOW - 7 * DAY, NOW, False, 7), (NOW - 7 * DAY, NOW, True, 30),
        (NOW - 7 * DAY, NOW, True, 90),
        (datetime.datetime(2026, 10, 4, tzinfo=MSK).timestamp(), datetime.datetime(2026, 10, 11, tzinfo=MSK).timestamp(),
         True, 10),
        (datetime.datetime(2026, 10, 24, tzinfo=MSK).timestamp(), NOW, False, 7)]
    notes = run("[" + ",".join("histChronoNote(histChronoWindow(%s, %r))" % (js(f), NOW) for f in (
        F("7d"), F("30d"), F("90d"))) + "]")
    assert notes == ["", "Хронология — до 7 дн: показаны последние 7 из 30.", "Хронология — до 7 дн: показаны последние 7 из 90."]
    # the timeline asks that window and the head's printer
    load = _js_function("loadTimeline")
    assert load.index("histChronoWindow(histFilter, Date.now() / 1000)") < load.index("var url = '/api/history/timeline?fr='")
    assert "timelineRange = { from: cw.fr, to: cw.to };" in load and "timelinePrinter = histFilter.printer;" in load
    assert "note.textContent = histChronoNote(cw); note.hidden = !cw.clipped;" in load


def test_texts():
    got = run("[" + ",".join(e for e in (
        "histPeriodText(%s)" % js(F()), "histPeriodText(%s)" % js(F("30d")),
        "histPeriodText(%s)" % js(F("custom", fr="2026-10-21", to="2026-10-28")),
        "histPeriodText(%s)" % js(F("custom", fr="2026-10-21", to="2026-10-21")),
        "histLoadingText(%s)" % js(F("7d")),
        "histSummaryText(%s, '')" % js(F()), "histSummaryText(%s, 'P3')" % js(F("7d", "p-3")),
        "histSummaryText(%s, '')" % js(F("90d", "p-9")),
    )) + "]")
    assert got == ["24 ч", "30 дн", "21.10–28.10", "21.10", "Считаем за 7 дн…", "24 ч · все принтеры", "7 дн · P3",
                   "90 дн · p-9"]


# --- the legend, the shares, «Загрузка» ---------------------------------------------------------------

def test_one_legend_from_the_dictionary():
    html = run("histLegendHtml()")
    assert re.findall(r'<span><i class="seg-([a-z]+)"></i>([^<]+)</span>', html) == [
        ("printing", "Печатает"), ("idle", "Простой"), ("paused", "Пауза"), ("error", "Ошибка"), ("cancelled", "Отменено"),
        ("finished", "Завершено"), ("offline", "Офлайн"), ("maintenance", "Обслуживание"), ("nodata", "Нет данных")]
    for k in ("printing", "idle", "paused", "error", "cancelled", "finished", "offline", "maintenance", "nodata"):
        assert re.search(r"\.seg-%s\b[^{]*\{" % k, CSS), k
    assert ".hist-legend { display: contents; }" in CSS


def test_shares():
    sh = run("histShares(%s)" % js({"printing": 6 * H, "idle": H, "offline": 2 * H, "maintenance": H, "nodata": 1800,
                                    "unknown": 1800, "bogus": 99}))
    assert sh["total"] == 11 * H and sh["printing"] == 6 * H and sh["maint"] == H     # «bogus» is no state
    assert sh["known"] == 11 * H - H - H                      # nodata + unknown and maintenance are no work time
    assert [(g["id"], round(g["pct"], 2)) for g in sh["segs"]] == [
        ("printing", 54.55), ("idle", 9.09), ("offline", 18.18), ("maintenance", 9.09), ("nodata", 9.09)]
    assert run("histShares(null)") == {"segs": [], "total": 0, "printing": 0, "known": 0, "maint": 0}
    bar = run("histBarHtml(histShares(%s))" % js({"printing": 30, "idle": 3 * H}))
    tips = re.findall(r'data-tip="([^"]*)"', bar)
    assert tips == ["Печатает · < 1м (<1%)", "Простой · 3ч (100%)"]
    assert bar.startswith('<span class="dash-stats-bar"><i class="dash-seg seg-printing" style="width:0.28%"')


def test_utilization():
    ss = [{"printer_id": "a", "states": {"printing": 3 * H, "idle": H}},
          {"printer_id": "b", "states": {"printing": H, "offline": 3 * H, "nodata": 10 * H}},
          {"printer_id": "m", "states": {"maintenance": 24 * H}}]
    assert run("[histUtil(%s), histUtil(%s, 'a'), histUtil(%s, 'm'), histUtil([]), histUtil(null)]" % (
        js(ss), js(ss), js(ss))) == [50, 75, None, None, None]


# --- «Статистика»: one row per printer ------------------------------------------------------------------

SS = [{"printer_id": "p-10", "label": "P10", "current_state": "printing", "maintenance": False,
       "states": {"printing": 6 * H, "idle": 2 * H}},
      {"printer_id": "p-2", "label": "P2", "current_state": "idle", "maintenance": False,
       "states": {"printing": H, "offline": 3 * H}},
      {"printer_id": "g", "label": "Ghost 6", "current_state": "idle", "maintenance": True,
       "states": {"maintenance": 8 * H}}]
ST = {"total": {"jobs": 5, "finished": 4, "cancelled": 0, "failed": 1, "power_loss": 0, "stopped": 0, "lost": 0, "open": 0,
                "success_pct": 80.0, "print_hours": 12.34, "acc_median": 1.04, "acc_p90": 1.1, "acc_n": 3,
                "good": 2, "defect": 1, "maint": 1},
      "printers": [{"printer_id": "p-2", "label": "P2", "model": "P2S", "jobs": 4, "finished": 3, "failed": 1,
                    "success_pct": 75.0, "acc_median": 1.04, "acc_n": 3, "avg_sec": 15000, "top_code": "0300_400F",
                    "top_code_text": "Напряжение не совпадает"},
                   {"printer_id": "old", "label": "<Старый>", "model": None, "jobs": 1, "finished": 1,
                    "success_pct": 100.0, "acc_n": 0, "top_code": None}]}
LIVE = [{"id": "p-10", "label": "P10", "model": "X1C"}, {"id": "p-2", "label": "P2"},
        {"id": "e5", "label": "E5", "maintenance": None}, {"id": "g", "label": "Ghost 6", "maintenance": {"since": 1}}]


def test_rows_merge_by_printer_in_name_order():
    rows = run("histStatsRows(%s, %s, %s)" % (js(SS), js(ST), js(LIVE)))
    assert [(r["id"], r["label"]) for r in rows] == [("old", "<Старый>"), ("e5", "E5"), ("g", "Ghost 6"),
                                                     ("p-2", "P2"), ("p-10", "P10")]          # P2 before P10
    by = {r["id"]: r for r in rows}
    assert by["p-2"]["jobs"]["jobs"] == 4 and by["p-2"]["states"] == SS[1]["states"] and by["p-2"]["model"] == "P2S"
    assert by["e5"]["jobs"] is None and by["e5"]["states"] is None              # no data in the window: a row all the same
    assert by["g"]["maint"] is True and by["p-10"]["maint"] is False and by["p-10"]["model"] == "X1C"
    assert by["old"]["states"] is None                                          # gone from the farm, its prints stay
    # one printer; states-summary still loading (null)
    assert [r["id"] for r in run("histStatsRows(%s, %s, %s, 'p-2')" % (js(SS), js(ST), js(LIVE)))] == ["p-2"]
    rows = run("histStatsRows(null, null, %s)" % js(LIVE))
    assert [r["id"] for r in rows] == ["e5", "g", "p-2", "p-10"] and all(r["states"] is None for r in rows)
    assert run("histStatsRows(null, null, null)") == []
    # a label from the data when the farm has none; the id when nobody has one
    rows = run("histStatsRows(%s, null, [])" % js([{"printer_id": "x", "states": {}}]))
    assert rows[0]["label"] == "x"


def _cells(tr):
    return [(dict(tags("<td%s>" % a)[0][1]), inner) for a, inner in re.findall(r"<td( [^>]*)?>(.*?)</td>", tr, re.S)]


def test_a_row():
    rows = run("histStatsRows(%s, %s, %s).map(function (r) { return histStatsRowHtml(r, true); })" % (js(SS), js(ST), js(LIVE)))
    assert all(r.startswith('<tr data-pid="') and r.endswith("</tr>") for r in rows)
    p2 = next(r for r in rows if 'data-pid="p-2"' in r)
    cells = _cells(p2)
    assert [c[0].get("class") for c in cells] == ["hs-pr", "hs-bar", "hs-num hs-print", "hs-num hs-jobs", "hs-num hs-ok",
                                                  "hs-num hs-acc", "hs-top"]
    assert [text(c[1]) for c in cells] == ["P2", "", "25%", "4 печ.", "75%", "×1,04", "0300_400F"]
    assert cells[0][0]["data-tip"] == "P2S · печатей: 4 · в среднем 4ч 10м"
    assert cells[6][0]["data-tip"] == "0300_400F: Напряжение не совпадает"
    assert cells[5][0]["data-tip"] == "медиана по завершённым с прогнозом, печатей: 3"
    assert [c[0].get("data-l") for c in cells] == [None, None, "печать", None, None, "факт/план", "причина"]
    ghost = _cells(next(r for r in rows if 'data-pid="g"' in r))
    assert 'class="hs-maint">обсл.' in ghost[0][1] and text(ghost[2][1]) == "—"
    assert ghost[2][0]["data-tip"] == "всё окно — на обслуживании"
    e5 = _cells(next(r for r in rows if 'data-pid="e5"' in r))
    assert [text(c[1]) for c in e5] == ["E5", "", "—", "0 печ.", "—", "—", "—"]
    old = next(r for r in rows if 'data-pid="old"' in r)
    assert "&lt;Старый&gt;" in old and "<Старый>" not in old
    # while states-summary is on its way: the bar waits, «…»
    wait = run("histStatsRowHtml(histStatsRows(null, %s, %s, 'p-2')[0], false)" % (js(ST), js(LIVE)))
    assert '<span class="dash-stats-bar hs-wait"></span>' in wait and text(_cells(wait)[2][1]) == "…"
    assert "data-tip" not in _cells(wait)[2][0]


def kpis(html):
    return [(a.get("data-kpi"), text(lbl), text(val)) for a, lbl, val in (
        (dict(tags(m.group(1) + ">")[0][1]), m.group(2), m.group(3)) for m in
        re.finditer(r'(<(?:button|div) [^>]*class="hist-kpi[^"]*"[^>]*)><span>(.*?)</span><b>(.*?)</b>', html))]


def test_the_totals():
    html = run("histKpiHtml(%s, 58)" % js(ST))
    assert kpis(html) == [("", "Печатей", "5"), ("finished", "Успешно", "80%"), (None, "Часы печати", "12,3"),
                          (None, "Загрузка", "58%"), (None, "Факт / план", "×1,04")]
    assert html.count("<button") == 2 and html.count('class="hist-kpi static"') == 3
    assert "без 1 в режиме обслуживания" in html
    # the owner (29.10): no «Годно / Брак» — no «Брак» even with marks in the window
    assert "Брак" not in html and "Годно" not in html
    assert [k[2] for k in kpis(run("histKpiHtml(%s)" % js(ST)))][3] == "…"          # states-summary loading
    assert [k[2] for k in kpis(run("histKpiHtml(%s, null)" % js(ST)))][3] == "—"
    empty = kpis(run("histKpiHtml({total: {}}, null)"))
    assert [k[2] for k in empty] == ["0", "—", "0,0", "—", "—"]


def _seed(path):
    conn = sqlite3.connect(path)
    try:
        jj.init_db(conn)
        base = dict(printer_id="p-1", kind="bambu", model="P2S", job_name="Деталь.gcode", start_exact=1, end_exact=1,
                    source="live")
        for j in (dict(result="finished", started_at=NOW - 10 * H, ended_at=NOW - 5 * H, last_seen=NOW - 5 * H,
                       actual_sec=5 * H, planned_sec=17000, planned_src="slicer"),
                  dict(result="failed", started_at=NOW - 30 * H, ended_at=NOW - 29 * H, last_seen=NOW - 29 * H,
                       actual_sec=H, error_code="0300_4000", error_text="Ошибка подогрева стола"),
                  dict(printer_id="p-2", kind="klipper", model=None, result=None, end_exact=None,
                       started_at=NOW - 2 * H, last_seen=NOW - 300, ctx="{}")):
            fields = dict(base, **j)
            jj.insert_job(conn, fields, fields["last_seen"])
        conn.commit()
    finally:
        conn.close()


def test_renders_what_the_api_serves(tmp_path, monkeypatch):
    monkeypatch.setattr(job_queries, "_TZ", MSK)
    db = str(tmp_path / "printer_history.db")
    _seed(db)
    st = job_queries.job_stats(db, NOW - 7 * DAY, NOW, labels={"p-1": "P2S-1", "p-2": "Varan"})
    ss = [{"printer_id": "p-1", "label": "P2S-1", "current_state": "idle", "maintenance": False,
           "states": {"printing": 6 * H, "idle": 18 * H, "unknown": 60}}]
    out = run("""(function () {
      var ss = %s, st = %s, live = [{id: "p-1", label: "P2S-1"}, {id: "p-2", label: "Varan"}];
      var rows = histStatsRows(ss, st, live);
      return {rows: rows.map(function (r) { return histStatsRowHtml(r, true); }),
              wait: histStatsRows(null, st, live).map(function (r) { return histStatsRowHtml(r, false); }),
              kpi: histKpiHtml(st, histUtil(ss)), legend: histLegendHtml()};
    })()""" % (js(ss), js(st)))
    for key, chunk in out.items():
        for html in chunk if isinstance(chunk, list) else [chunk]:
            for bad in ("undefined", "NaN", "null", "[object"):
                assert bad not in html, (key, bad)
    assert len(out["rows"]) == 2 and "Varan" in out["rows"][1] and "25%" in out["rows"][0]


# --- the head: markup, wiring, styles ----------------------------------------------------------------------

def test_the_head_markup():
    panel = HTML[HTML.index('data-panel="history"'):HTML.index('data-panel="ams"')]
    head = panel[panel.index('id="histHead"'):panel.index('data-hsub="jobs"')]
    assert head.index('id="historySub"') < head.index('id="histSum"') < head.index('id="histFilters"') \
        < head.index('id="histPeriod"') < head.index('id="histCustom"') < head.index('id="histPrinter"') \
        < head.index('id="jobsCsvBtn"')
    custom = [a for t, a in tags(head) if a.get("id") == "histCustom"][0]
    assert "hidden" in custom                                      # «Свой…» opens С / По
    assert '<select id="histPrinter" class="hist-in"><option value="">Все</option></select>' in head
    for a in (a for t, a in tags(head) if t == "button"):
        assert "title" not in a, a                                 # tips are data-tip (IA §10)
    sum_btn = [a for t, a in tags(head) if a.get("id") == "histSum"][0]
    assert sum_btn["aria-controls"] == "histFilters" and sum_btn["aria-expanded"] == "false"
    # «Хронология»: the instruction under «?», the note, one legend; «Статистика»: its blocks; «Действия»
    chrono = panel[panel.index('data-hsub="chrono"'):panel.index('data-hsub="stats"')]
    assert chrono.index('id="histHelpBtn"') < chrono.index('id="histHelp" hidden') < chrono.index('id="chronoNote" hidden') \
        < chrono.index("data-hist-legend") < chrono.index('id="timeline-canvas"')
    stats = panel[panel.index('data-hsub="stats"'):panel.index('data-hsub="actions"')]
    for need in ('id="histStatsKpi"', 'id="histStatsBody"', 'id="statesSummaryLoader"', "data-hist-legend",
                 'id="jobsDays"', 'id="jobsReasons"', 'id="histReasonsSeg"'):
        assert need in stats, need
    cols = re.findall(r"<th>([^<]*)</th>", stats)
    assert cols == ["Принтер", "Доли времени", "Печать", "Печатей", "Успешно", "Факт / план", "Частая причина"]
    # «Действия» (plan step 12): the audit journal itself, no more a stub pointing at «Админ → Доступ»
    actions = panel[panel.index('data-hsub="actions"'):]
    for need in ('id="actsChips"', 'id="actsWho"', 'id="actsSvc"', 'id="actsBody"', 'id="actsMore"'):
        assert need in actions, need
    assert "histAuditBtn" not in HTML and "«Админ → Доступ»" not in actions
    # the loaders stay and say what they count
    for lid in ("jobsLoader", "timelineLoader", "statesSummaryLoader"):
        assert re.search(r'id="%s"[^>]*>Считаем за 24 ч…<' % lid, panel), lid
    assert '<symbol id="i-download"' in HTML and '<symbol id="i-filter"' in HTML


def test_the_head_wiring():
    init = JS[JS.index("(function initHistHead() {"):JS.index("/* ── История → Статистика")]
    # a sub-tab: an entry in the history; a period, «Применить» or a printer: the address in place
    sub = init[init.index("histEls.sub.addEventListener"):init.index("histEls.period.addEventListener")]
    assert sub.index("setHistorySub(b.dataset.sub);") < sub.index("routeSync(true);") < sub.index("histLoadSub();")
    setf = _js_function("histSetFilter")
    assert setf.index("if (histSameFilter(f, histFilter)) return;") < setf.index("routeSync(false);") < setf.index("histLoadSub();")
    period = init[init.index("histEls.period.addEventListener"):init.index("histEls.apply.addEventListener")]
    assert 'if (b.dataset.period === "custom") { histOpenCustom(histEls.custom.hidden); return; }' in period
    assert "histSetFilter({ period: b.dataset.period, printer: histFilter.printer });" in period
    apply = init[init.index("histEls.apply.addEventListener"):init.index("histEls.printer.addEventListener")]
    assert "var a = histEls.from.value || histEls.to.value, z = histEls.to.value || histEls.from.value;" in apply
    assert 'showToast("Начало периода позже конца", false)' in apply
    assert 'histSetFilter({ period: "custom", from: a, to: z, printer: histFilter.printer });' in apply
    assert "printer: histEls.printer.value || null" in init
    assert 'histEls.head.classList.toggle("hist-open", open);' in init     # a phone's folded head
    # the router reads and writes the filter: routeNow takes it, routeApply hands the address' query to it
    assert "if (view === \"history\") r.query = histQueryOf(histFilter);" in _js_function("routeNow")
    apply_route = _js_function("routeApply")
    assert apply_route.index("setHistorySub(sub)") < apply_route.index("histApplyQuery(shown.query)") \
        < apply_route.index('setActiveView("history", true)')
    # the printer choice follows the farm, never under the user's hand
    opts = _js_function("histPrinterOptions")
    assert "if (sig === histPrinterSig || document.activeElement === sel) return;" in opts
    assert 'localeCompare(b.label, "ru", { numeric: true })' in opts
    assert "histPrinterOptions();" in _js_function("load")
    # the role: «Действия» and «⇩ Выгрузить» (tests/test_web_jobs_ui.py) for admins only; the stub's
    # «Открыть журнал ›» is gone with the stub (plan step 12, tests/test_web_acts_ui.py)
    assert "histAuditBtn" not in init and 'else if (historySub === "actions") loadActs();' in _js_function("histLoadSub")



def test_the_summary_window_has_no_printer():
    # the shares do not depend on the printer: one key for every printer, whole minutes
    now = NOW + 37.4
    got = run("[%s]" % ",".join("histSummaryWindow(%s, %r)" % (js(f), t) for f, t in (
        (F("30d"), now), (F("30d", "p-2"), now), (F("30d", "p-2"), now + 15), (F("7d"), now),
        (F("custom", "p-1", "2026-10-21", "2026-10-28"), now), (F("custom", None, "2026-10-21", "2026-10-28"), now))))
    assert got[0] == got[1] == got[2]                                    # a printer picked, a few seconds later
    assert got[0]["fr"] % 60 == 0 and got[0]["to"] % 60 == 0
    assert got[0]["to"] == NOW and got[0]["fr"] == NOW - 30 * DAY
    assert got[3]["key"] != got[0]["key"] and got[4] == got[5]
    assert json.loads(got[0]["key"]) == {"period": "30d"} and "printer" not in got[4]["key"]
    # the minutes cut at most a minute off the shares' window, never past the head's window
    w = run("histWindow(%s, %r)" % (js(F("30d")), now))
    assert w["fr"] - 60 < got[0]["fr"] <= w["fr"] and w["to"] - 60 < got[0]["to"] <= w["to"]


STATS_HARNESS = r"""
%(lib)s
var cachedAllPrinters = [], renders = 0, urls = [], pending = [];
var console = { error: function () {}, log: function (s) { process.stdout.write(s + "\n"); } };
function renderHistStats() { renders++; }
var histStatsEls = { body: {}, loader: { style: { display: "none" }, textContent: "" } };
var _now = %(now)s * 1000;
Date.now = function () { return _now; };
function histFetchJson(url) {
  urls.push(url);
  return new Promise(function (ok, bad) { pending.push({ url: url, ok: ok, bad: bad }); });
}
%(state)s
%(load)s
var histFilter = { period: "24h", from: null, to: null, printer: null };
function settle(fail) {
  var p = pending; pending = [];
  p.forEach(function (x) {
    if (fail && x.url.indexOf("states-summary") >= 0) x.bad(new Error("HTTP 504"));
    else x.ok(x.url.indexOf("states-summary") >= 0 ? [{ printer_id: "p-1", states: {} }] : { total: { jobs: 1 } });
  });
  return new Promise(function (r) { setTimeout(r, 0); });
}
function kinds() {
  var u = urls; urls = [];
  return u.map(function (x) { return x.indexOf("states-summary") >= 0 ? "ss" : "st"; });
}
var out = [];
(async function () {
%(steps)s
console.log(JSON.stringify(out));
})();
"""


def test_a_printer_picked_keeps_the_shares():
    # the review of 31.10: a row's click (a printer) started another full window scan on the Pi
    state = re.search(r"\n\s*(var HIST_STATS_FRESH_MS = \d+;)", JS).group(1) + "\n" + \
        re.search(r"\n\s*(var histStats = \{.*?\};)", JS, re.S).group(1)
    steps = r"""
    loadHistStats(); out.push(["open", kinds(), histStatsEls.loader.style.display]);
    await settle(); out.push(["landed", kinds(), histStatsEls.loader.style.display, !!histStats.ss]);
    histFilter = { period: "24h", from: null, to: null, printer: "p-1" }; loadHistStats();
    out.push(["printer", kinds(), !!histStats.ss]);
    await settle();
    histFilter = { period: "24h", from: null, to: null, printer: null }; loadHistStats();
    out.push(["printer again", kinds()]);
    await settle();
    // a new period: both; quick clicks on printers while the scan is on its way share it
    histFilter = { period: "30d", from: null, to: null, printer: null }; loadHistStats();
    out.push(["period", kinds(), histStats.ss]);
    histFilter = { period: "30d", from: null, to: null, printer: "p-2" }; loadHistStats();
    histFilter = { period: "30d", from: null, to: null, printer: null }; loadHistStats();
    histFilter = { period: "30d", from: null, to: null, printer: "p-3" }; loadHistStats();
    out.push(["clicks", kinds(), histStatsEls.loader.style.display]);
    await settle(); out.push(["after", !!histStats.ss, histStatsEls.loader.style.display, histStats.ssReq]);
    // the same filter within 2 min: nothing asked; later: both again
    loadHistStats(); out.push(["re-open", kinds()]);
    _now += 121000; loadHistStats(); out.push(["stale", kinds()]);
    await settle();
    // a failed scan: the next open asks again
    histFilter = { period: "7d", from: null, to: null, printer: null }; loadHistStats(); kinds();
    await settle(true); out.push(["failed", histStats.ssErr, histStatsEls.loader.style.display]);
    histFilter = { period: "7d", from: null, to: null, printer: "p-1" }; loadHistStats();
    out.push(["retry", kinds()]);
    await settle();
    """
    out = _node(STATS_HARNESS % {"lib": _lib(), "now": NOW + 37.4, "state": state, "load": _js_function("loadHistStats"),
                                 "steps": steps})
    assert out == [
        ["open", ["st", "ss"], ""],
        ["landed", [], "none", True],
        ["printer", ["st"], True],                       # the shares stay on screen, /jobs/stats only
        ["printer again", ["st"]],
        ["period", ["st", "ss"], None],
        ["clicks", ["st", "st", "st"], ""],              # one scan for the four clicks
        ["after", True, "none", None],
        ["re-open", []],
        ["stale", ["st", "ss"]],
        ["failed", True, "none"],
        ["retry", ["st", "ss"]],
    ]


def test_the_stats_load():
    load = _js_function("loadHistStats")
    # the journal's numbers and the time shares in parallel; the window and the printer are the head's
    assert 'histFetchJson("/api/history/jobs/stats?" + q)' in load
    # the shares' window: whole minutes, without the printer (histSummaryWindow; the run below)
    assert 'histFetchJson("/api/history/states-summary?fr=" + sw.fr + "&to=" + sw.to)' in load
    assert "var sw = histSummaryWindow(histFilter, now);" in load
    assert "var q = jobsFilterQuery({ fr: w.fr, to: w.to, printer: histFilter.printer }, now, false);" in load
    # the loader says what it counts, a stale answer goes to waste, a re-open within 2 min asks nothing
    assert "els.loader.textContent = histLoadingText(histFilter);" in load
    assert load.count("gen !== histStats.gen") >= 2 and load.count("histStats.ssReq !== req") >= 3
    assert "var HIST_STATS_FRESH_MS = 120000;" in JS
    assert "Date.now() - histStats.at < HIST_STATS_FRESH_MS" in load
    render = _js_function("renderHistStats")
    assert "histStatsRows(ss, s.st, cachedAllPrinters, pid)" in render and "histKpiHtml(s.st, ss ? histUtil(ss, pid) : undefined)" in render
    assert "jobsDayBarsHtml(s.st)" in render and "jobsReasonsHtml(s.st)" in render
    # a row picks the head's printer; «Печатей» / «Успешно» open «Печати» with that filter
    init = JS[JS.index("(function initHistStats() {"):]
    init = init[:init.index("})();")]
    assert "printer: histFilter.printer === tr.dataset.pid ? null : tr.dataset.pid" in init
    kpi = init[init.index("els.kpi.addEventListener"):init.index("els.seg.addEventListener")]
    assert kpi.index('jobsSetResults(b.dataset.kpi ? [b.dataset.kpi] : []);') < kpi.index('setHistorySub("jobs");') \
        < kpi.index("routeSync(true);") < kpi.index("histLoadSub();")
    assert "els.reasons.dataset.show = b.dataset.reasons;" in init


def test_query_bounds_fit_the_api():
    # verify-every-tab-not-just-curl: every limit the page sends is within the endpoint's Query(le=…)
    def le(path, name="limit"):
        sig = re.search(r'@app\.get\("%s"[^)]*\)\s*def \w+\((.*?)\):' % re.escape(path), API, re.S)
        assert sig, path
        return int(re.search(r"%s: int = Query\(\d+, ge=1, le=(\d+)\)" % name, sig.group(1)).group(1))
    assert int(re.search(r"let timelineLimit = (\d+);", JS).group(1)) <= le("/api/history/timeline")
    assert int(re.search(r"var JOBS_PAGE = (\d+);", JS).group(1)) <= le("/api/history/jobs")
    assert int(re.search(r"var FLEET_EV_PAGE = (\d+)", JS).group(1)) <= le("/api/history/events")
    # states-summary and jobs/stats take no limit; the event log's requests are gone with it
    sig = re.search(r'@app\.get\("/api/history/states-summary"\)\s*def \w+\((.*?)\):', API, re.S).group(1)
    assert "limit" not in sig
    assert "/api/history/events?fr=\" + from" not in JS and "EVENT_LOG_LIMIT" not in JS


def test_the_saved_sub_is_read_once_and_removed():
    line = next(l for l in JS.splitlines() if "localStorage.getItem(HIST_SUB_KEY)" in l)
    assert line.strip().startswith("try {") and line.strip().endswith("} catch (e) {}")
    assert 'var HIST_SUB_KEY = "forge-history-sub";' in JS and JS.count("HIST_SUB_KEY") == 3
    assert not re.search(r"setItem\(\s*(HIST_SUB_KEY|[\"']forge-history-sub)", JS)       # never written again
    harness = r"""
    var removed = [], store = %s, throws = %s;
    var localStorage = {
      getItem: function (k) { if (throws) throw new Error("denied"); return Object.prototype.hasOwnProperty.call(store, k) ? store[k] : null; },
      removeItem: function (k) { removed.push(k); delete store[k]; }
    };
    var HIST_SUB_KEY = "forge-history-sub", historySub = HIST_SUBS[0];
    %s
    console.log(JSON.stringify({ sub: historySub, removed: removed, store: store }));
    """

    def mig(saved, throws=False):
        store = {} if saved is None else {"forge-history-sub": saved}
        store["other"] = "x"
        return _node(_lib() + harness % (js(store), js(throws), line))
    # every value the old switch ever wrote (timeline / jobs), and junk: «Печати» by default (IA §7)
    for saved, want in (("timeline", "chrono"), ("jobs", "jobs"), ("garbage", "jobs"), ("", "jobs"),
                        ("__proto__", "jobs"), (None, "jobs")):
        out = mig(saved)
        assert out["sub"] == want, saved
        assert out["removed"] == ["forge-history-sub"] and out["store"] == {"other": "x"}, saved
    out = mig("timeline", throws=True)
    assert out["sub"] == "jobs" and out["removed"] == []
    # the sub lives in the address now: setHistorySub writes no storage
    assert "localStorage" not in _js_function("setHistorySub")


def test_the_head_styles():
    def rule(sel, within=CSS):
        m = re.search(r"\n\s*%s \{([^}]*)\}" % re.escape(sel), within)
        assert m, sel
        return m.group(1)
    head = rule(".hist-head")
    for need in ("position: sticky;", "top: 0;", "background-color: var(--bg-elev);", "border-bottom: 1px solid var(--line);"):
        assert need in head, need
    desk = CSS[CSS.index("@media (min-width: 901px) {\n      .hist-head"):]
    assert ".hist-head { top: 48px; margin: -16px -20px 14px; padding: 8px 20px; }" in desk[:120]
    # a phone: the sub-tabs share the width, the filters fold into one line, the stats rows are cards
    phone = CSS[CSS.index("@media (max-width: 900px) {\n      .hist-head .hist-seg"):]
    phone = phone[:phone.index("\n    }\n")]
    assert "grid-auto-columns: minmax(0, 1fr)" in phone and ".hist-filters { display: none; }" in phone
    assert ".hist-head.hist-open .hist-filters { display: flex; }" in phone
    cards = CSS[CSS.index("@media (max-width: 900px) {\n      .hist-kpi"):]
    cards = cards[:cards.index("\n    }\n")]
    assert ".hist-stab thead { display: none; }" in cards and ".hist-stab td.hs-bar { grid-column: 1 / -1;" in cards
    assert '.hist-stab td[data-l]::before { content: attr(data-l) " ";' in cards
    # only one reasons column at a time, by the segment
    assert '.hist-reasons[data-show="err"] .jobs-reasons-col + .jobs-reasons-col' in CSS
    # the old controls are gone with their styles
    for gone in (".history-controls", ".history-range-btns", "#history-apply", ".stats-period-btn", ".dash-meta-row",
                 ".event-row", ".jobs-prow", ".jobs-grid", ".history-sub {"):
        assert gone not in CSS, gone


def test_every_rendered_class_has_a_style():
    out = run("""(function () {
      var rows = histStatsRows(%s, %s, %s);
      return rows.map(function (r) { return histStatsRowHtml(r, true) + histStatsRowHtml(r, false); }).join("") +
        histKpiHtml(%s, 50) + histLegendHtml();
    })()""" % (js(SS), js(ST), js(LIVE), js(ST)))
    classes = set()
    for cls in re.findall(r'class="([^"]+)"', out):
        classes.update(cls.split())
    assert {"hs-pr", "hs-bar", "hs-num", "hs-maint", "hs-wait", "hist-kpi", "static", "dash-stats-bar", "dash-seg"} <= classes
    for cls in sorted(classes - {"static"}):
        assert re.search(r"\.%s(?![\w-])[^{}]*\{" % re.escape(cls), CSS), cls


def test_cache_bust_bumped():
    assert int(re.search(r"styles\.css\?v=(\d+)", HTML).group(1)) >= 94
    assert int(re.search(r"app\.js\?v=(\d+)", HTML).group(1)) >= 111
