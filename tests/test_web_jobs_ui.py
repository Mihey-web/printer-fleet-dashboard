"""The print-journal helpers of app/web/app.js (the /* jobs-ui */ block), run in node.

The block holds only pure code; it may use safeText, timeFmt, jobShortName and
stageLabel (with its STAGE_LABELS table), which are extracted alongside it.
Node runs with TZ=Europe/Moscow so the clock texts are deterministic. Job names
are invented. Skipped without node.
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
API = (ROOT / "app" / "api" / "main.py").read_text(encoding="utf-8")
MSK = datetime.timezone(datetime.timedelta(hours=3))
NOW = datetime.datetime(2026, 10, 17, 12, 0, tzinfo=MSK).timestamp()
H = 3600
NAME = "Деталь A.gcode"
EVIL = '<img src=x onerror=alert(1)>"q".3mf'
# IA §7 (plan step 9, deliberately changed): «Длительность (план, Δ)» is one column, «Оценка» is gone
COLS = ["Принтер", "Начало", "Длительность (план, Δ)", "Результат", "Паузы", "Слои", "Задание"]
DUR = "Длительность (план, Δ)"


def _function(name: str) -> str:
    m = re.search(r"function %s\([^)]*\) \{.*?\n\s*\}" % name, JS, re.S)
    assert m, "%s() not found in app.js" % name
    return m.group(0)


def _js_function(name: str) -> str:
    """The whole `function name(...) {...}` declaration of app.js, also with
    nested blocks: braces matched, string literals and // comments skipped."""
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


def _const(name: str) -> str:
    m = re.search(r"const %s = \{.*?\n\s*\};" % name, JS, re.S)
    assert m, "%s not found in app.js" % name
    return m.group(0)


def _block(tag: str) -> str:
    m = re.search(r"/\* %s:begin \*/(.*?)/\* %s:end \*/" % (tag, tag), JS, re.S)
    assert m, "%s block not found in app.js" % tag
    return m.group(1)


def _node(src: str):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    # the script on stdin: a long -e argument hits the Windows command line limit
    out = subprocess.run([node, "-"], input=src, capture_output=True, text=True, timeout=60, encoding="utf-8",
                         env=dict(os.environ, TZ="Europe/Moscow"))
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _lib() -> str:
    src = "\n".join(_function(n) for n in ("safeText", "timeFmt", "jobShortName"))
    return src + "\n" + _const("STAGE_LABELS") + "\n" + _js_function("stageLabel") + "\n" + _block("jobs-ui")


def run(expr: str):
    """Evaluate `expr` (JSON-serializable) against the helper block in node."""
    return _node(_lib() + "\nconsole.log(JSON.stringify(" + expr + "));")


def call(fn: str, *args):
    return run("%s(%s)" % (fn, ", ".join(json.dumps(a, ensure_ascii=False) for a in args)))


class _Tags(HTMLParser):
    """Start tags with their (unescaped) attributes, in order."""

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


def cells(row_html):
    """{data-l: (td attributes, inner html)} of a table row, in order."""
    out = {}
    for attrs, inner in re.findall(r"<td( [^>]*)>(.*?)</td>", row_html, re.S):
        a = dict(tags("<td%s>" % attrs)[0][1])
        out[a["data-l"]] = (a, inner)
    return out


def job(**kw):
    """A /api/history/jobs row (job_queries._ROW_FIELDS): a finished print, 5ч 12м of a 5ч plan."""
    j = {"id": 7, "printer_id": "p-1", "label": "P2S-1", "kind": "bambu", "model": "P2S", "job_name": NAME,
         "started_at": NOW - 6 * H, "ended_at": NOW - 6 * H + 18720, "start_exact": 1, "end_exact": 1,
         "last_seen": NOW - 6 * H + 18720, "result": "finished", "error_code": None, "error_text": None,
         "planned_sec": 18000, "planned_src": "slicer", "actual_sec": 18720, "paused_sec": 0, "pauses": 0,
         "pause_codes": [], "offline_sec": 0, "offline_n": 0, "power_losses": 0, "end_progress": 100.0,
         "layer": 300, "total_layers": 300, "maint": 0, "verdict": None}
    j.update(kw)
    return j


def open_job(**kw):
    base = dict(result=None, ended_at=None, end_exact=None, actual_sec=None, started_at=NOW - 2 * H,
                last_seen=NOW - 60, end_progress=40.0, layer=120)
    base.update(kw)
    return job(**base)


PRINTING = {"id": "p-1", "online": True, "state": "printing", "progress_pct": 47.6, "eta_seconds": 3 * H + 600}


def row(j, printer=None, admin=True, now=NOW, open=False):
    # admin: kept for the callers — a row is the same for both roles now (no «Годно / Брак», the owner 29.10)
    return call("jobRowHtml", j, printer, {"now": now, "open": open})


def dur(c):
    """The duration of a «Длительность (план, Δ)» cell, without its plan."""
    return text(c[DUR][1]).split(" · план")[0]


# --- purity and the result table ------------------------------------------------------

def test_block_is_pure():
    block = _block("jobs-ui")
    for banned in ("document.", "window.", "fetch(", "apiFetch", "cachedAllPrinters", "localStorage"):
        assert banned not in block, banned


def test_results_match_the_api():
    res = run("JOB_RESULTS")
    assert set(res) == set(job_queries.RESULT_FILTERS) == set(jj.RESULTS) | {"open"}
    # the same words in the table and in the CSV export
    assert {k: v["label"] for k, v in res.items()} == job_queries.RESULT_LABELS
    # IA §10's dictionary «Результаты печати»: the rows say what the chips over them say
    chips = {c["id"]: c["label"] for c in run("JOB_CHIPS") if c["id"]}
    assert {k: v["label"] for k, v in res.items() if k != "open"} == {k: v for k, v in chips.items() if k != "open"}
    assert [res[k]["label"] for k in run("JOB_RESULT_ORDER")] == [
        "Успешно", "Отменено", "Ошибка", "Питание", "Остановлено", "Нет связи", "Идёт"]
    assert run("JOB_RESULT_ORDER") == list(job_queries.RESULT_FILTERS)
    assert res["failed"]["badge"] == "error" and res["lost"]["seg"] == "seg-nodata"


def test_result_badges_and_segments_have_css():
    for key, r in run("JOB_RESULTS").items():
        assert re.search(r"\.event-badge-%s\b[^{]*\{" % r["badge"], CSS), (key, r["badge"])
        assert re.search(r"\.%s\b[^{]*\{" % r["seg"], CSS), (key, r["seg"])


def test_page_size_fits_the_api_bound():
    page = int(re.search(r"var JOBS_PAGE = (\d+);", JS).group(1))
    sig = re.search(r'@app\.get\("/api/history/jobs"\)\s*def get_jobs\((.*?)\):', API, re.S)
    assert sig, "GET /api/history/jobs not found"
    le = int(re.search(r"limit: int = Query\(\d+, ge=1, le=(\d+)\)", sig.group(1)).group(1))
    assert 1 <= page <= le
    assert run("JOBS_PAGE") == page


# --- rows -------------------------------------------------------------------------------

def test_row_has_a_labelled_cell_per_column():
    for j in (job(), open_job()):
        html = row(j, PRINTING)
        assert re.match(r'<tr class="job-row( job-open)?" data-job-id="7" data-pid="p-1" tabindex="0" aria-expanded="false">', html)
        assert html.endswith("</tr>")
        labels = re.findall(r"<td data-l=\"([^\"]*)\"", html)
        assert labels == COLS
        assert html.count("<td") == len(labels)
        assert re.findall(r"<th>([^<]*)</th>", call("jobsHeadHtml")) == labels
    # unfolded: the chevron turns, the row says so
    html = row(job(), open=True)
    assert 'class="job-row job-x"' in html and 'aria-expanded="true"' in html and '<span class="job-chev" aria-hidden="true">▾</span>' in html
    assert '<span class="job-chev" aria-hidden="true">▸</span>' in row(job())


def test_finished_row_cells():
    # 5ч 26м with a 14-minute pause against a 5ч plan: Δ by the duration shown (+9%), 5ч 12м printed (+4%)
    c = cells(row(job(pause_codes=["stage:paused_user"], pauses=1, paused_sec=840, actual_sec=19560,
                      ended_at=NOW - 6 * H + 19560), admin=False))
    # the model next to the name, dim (as on the card, IA §5)
    assert text(c["Принтер"][1]) == "▸P2S-1 P2S" and '<span class="job-model">P2S</span>' in c["Принтер"][1]
    assert "data-tip" not in c["Принтер"][0]
    assert text(c["Начало"][1]) == "06:00 → 11:26"
    # «Длительность (план, Δ)»: the duration, the plan and Δ with their classes and tips
    assert text(c[DUR][1]) == "5ч 26м · план 5ч +9%" and dur(c) == "5ч 26м"
    spans = {a["class"]: a for t_, a in tags(c[DUR][1]) if t_ == "span"}
    assert spans["job-plan"]["data-tip"] == spans["job-delta jd-ok"]["data-tip"] ==         "оценка слайсера · факт 5ч 26м, план 5ч; без пауз 5ч 12м (+4%)"
    assert text(c["Результат"][1]) == "Успешно"
    assert 'class="event-badge event-badge-finished"' in c["Результат"][1]
    assert text(c["Паузы"][1]) == "1 · 14м" and c["Паузы"][0]["data-tip"] == "Пауза (пользователь)"
    assert text(c["Слои"][1]) == "300"
    assert text(c["Задание"][1]) == "Деталь A"
    assert tags(c["Задание"][1])[0][1]["data-tip"] == NAME


def test_inexact_times_get_a_tilde():
    c = cells(row(job(start_exact=0)))
    assert text(c["Начало"][1]) == "~06:00 → 11:12" and dur(c) == "~5ч 12м"
    c = cells(row(job(end_exact=0)))
    assert text(c["Начало"][1]) == "06:00 → ~11:12" and dur(c) == "~5ч 12м"
    c = cells(row(open_job(start_exact=0), PRINTING))
    assert text(c["Начало"][1]) == "~10:00" and dur(c) == "~2ч"
    c = cells(row(open_job(), PRINTING))          # a running print: no end, its end is not "inexact"
    assert text(c["Начало"][1]) == "10:00" and dur(c) == "2ч"
    assert text(c[DUR][1]) == "2ч · план 5ч"         # its plan, no Δ while it runs


def test_dates_show_when_not_today():
    c = cells(row(job(started_at=NOW - 30 * H, ended_at=NOW - 20 * H, actual_sec=10 * H)))
    assert text(c["Начало"][1]) == "16.10 06:00 → 16:00"
    c = cells(row(job(started_at=NOW - 14 * H, ended_at=NOW - 4 * H, actual_sec=10 * H)))
    assert text(c["Начало"][1]) == "16.10 22:00 → 17.10 08:00"


def test_names_and_strings_are_escaped():
    j = job(job_name=EVIL, label='P<b>1</b>', model='"M"', result="failed", error_code="0300_4000",
            error_text='<script>x</script>"', pause_codes=['<i>"'], pauses=1)
    html = row(j, admin=True)
    assert "<img" not in html and "<script" not in html and "<b>" not in html and "<i>" not in html
    t = tags(html)
    assert [x for x, _ in t if x in ("img", "script", "b", "i")] == []
    tips = [a.get("data-tip") for _, a in t if a.get("data-tip")]
    assert EVIL in tips                                   # the full name round-trips through data-tip
    assert "0300_4000: <script>x</script>\"" in tips      # the badge's tip
    assert '<i>"' in tips
    c = cells(html)
    assert text(c["Задание"][1]) == '<img src=x onerror=alert(1)>"q"'
    assert text(c["Принтер"][1]) == '▸P<b>1</b> "M"' and "&quot;M&quot;" in c["Принтер"][1]
    # the details too
    xp = call("jobDetailHtml", j, {"now": NOW})
    assert "<img" not in xp and "<script" not in xp and "<i>" not in xp and EVIL not in xp
    assert '<img src=x onerror=alert(1)>"q".3mf' in text(xp)


def test_missing_name_and_layers():
    c = cells(row(job(job_name=None, total_layers=None, layer=None)))
    assert text(c["Задание"][1]) == "—" and text(c["Слои"][1]) == "—"
    assert text(cells(row(job(result="stopped", layer=150)))["Слои"][1]) == "150/300"
    assert text(cells(row(open_job(layer=None), PRINTING))["Слои"][1]) == "—"
    assert text(cells(row(job(total_layers=None, layer=42)))["Слои"][1]) == "42"


def test_no_verdict_anywhere():
    # the owner, 29.10: «Годно / Брак» is gone completely — no buttons, no chip, no column, for any
    # role and any stored mark (the server keeps its PUT route, nothing on the page calls it)
    for v in (None, "good", "defect"):
        for j in (job(verdict=v), open_job(verdict=v)):
            for admin in (True, False):
                html = row(j, PRINTING, admin=admin) + call("jobDetailHtml", j, {"now": NOW})
                for gone in ("data-verdict", "Годно", "Брак", "Оценка", "job-good", "job-defect", "job-verdict"):
                    assert gone not in html, (v, gone)
    assert "Оценка" not in call("jobsHeadHtml") and "Брак" not in call("jobsChipsHtml", stats(total=dict(
        stats()["total"], good=3, defect=2)), [])
    for gone in ("JOB_VERDICTS", "jobVerdictButtons", "jobVerdictNext", "jobsStatsWithVerdict", "jobVerdictError",
                 "setJobVerdict", "jobsKpiHtml", "/verdict"):
        assert gone not in JS, gone


def test_flags_power_and_maintenance():
    res = cells(row(job(power_losses=2, maint=1)))["Результат"][1]
    assert "⚡2" in text(res) and "обслуживание" in text(res)
    one = cells(row(job(power_losses=1)))["Результат"][1]
    assert "⚡" in text(one) and "⚡1" not in text(one)
    assert "job-flag" not in row(job())


def test_closed_result_tips():
    c = cells(row(job(result="stopped")))["Результат"][1]
    assert text(c) == "Остановлено"
    assert tags(c)[0][1]["data-tip"] == "Остановлено — принтер не сообщил причину"
    c = cells(row(job(result="power_loss", error_code="0300_8007", error_text="Пропало питание")))["Результат"][1]
    assert tags(c)[0][1]["data-tip"] == "0300_8007: Пропало питание"
    assert "event-badge-paused" in c
    c = cells(row(job(result="cancelled", error_code="0300_400C")))["Результат"][1]
    assert tags(c)[0][1]["data-tip"] == "0300_400C" and "event-badge-cancelled" in c
    c = cells(row(job(result="finished")))["Результат"][1]
    assert "data-tip" not in c
    c = cells(row(job(result="something_new")))["Результат"][1]   # a result newer than this page
    assert text(c) == "something_new" and "event-badge-unknown" in c


# --- running prints ------------------------------------------------------------------------

@pytest.mark.parametrize("printer,state", [
    (None, "open"),
    (PRINTING, "open"),
    (dict(PRINTING, state="paused"), "open_paused"),
    (dict(PRINTING, online=False), "open_offline"),
    (dict(PRINTING, state="offline"), "open_offline"),
])
def test_live_state_of_running_prints(printer, state):
    assert call("jobLiveState", open_job(), printer) == state
    assert call("jobLiveState", job(result="failed"), printer) == "failed"


def test_running_print_state_cell():
    html = call("jobStateHtml", open_job(), PRINTING, NOW)
    assert text(html) == "Идёт 47% · осталось 3ч 10м" and "event-badge-printing" in html
    html = call("jobStateHtml", open_job(), dict(PRINTING, state="paused"), NOW)
    assert text(html) == "Пауза 47%" and "event-badge-paused" in html
    html = call("jobStateHtml", open_job(last_seen=NOW - 2 * H + 1800), dict(PRINTING, online=False), NOW)
    assert text(html) == "Нет связи с 10:30" and "event-badge-offline" in html
    assert text(call("jobStateHtml", open_job(last_seen=None), {"online": False}, NOW)) == "Нет связи"
    # no live status: the journal's last progress
    assert text(call("jobStateHtml", open_job(), None, NOW)) == "Идёт 40%"
    assert text(call("jobStateHtml", open_job(end_progress=None), None, NOW)) == "Идёт"


def test_duration_text():
    assert call("jobDurText", open_job(), NOW) == "2ч"
    assert call("jobDurText", open_job(), NOW + 1800) == "2ч 30м"
    assert call("jobDurText", job(actual_sec=None), NOW) == "—"
    assert call("jobDurText", job(actual_sec=30, start_exact=0), NOW) == "—"


# --- plan and fact ----------------------------------------------------------------------------

@pytest.mark.parametrize("actual,delta,cls", [
    (18720, "+4%", "jd-ok"),
    (18000, "0%", "jd-ok"),
    (22500, "+25%", "jd-slow"),
    (14400, "−20%", "jd-fast"),
    (19800, "+10%", "jd-ok"),
    (19980, "+11%", "jd-slow"),
])
def test_plan_delta(actual, delta, cls):
    p = call("jobPlanCells", job(actual_sec=actual))
    assert (p["plan"], p["delta"], p["cls"], p["planCls"]) == ("5ч", delta, cls, "")


def test_plan_delta_is_by_the_duration_shown_the_tip_adds_it_without_pauses():
    # 5ч 42м with a 30-minute pause: «5ч 42м из плана 5ч +14%», the pauses cell says why
    p = call("jobPlanCells", job(actual_sec=18720 + 1800, paused_sec=1800, planned_src="printer"))
    assert (p["delta"], p["cls"]) == ("+14%", "jd-slow")
    assert p["tip"] == "оценка принтера · факт 5ч 42м, план 5ч; без пауз 5ч 12м (+4%)"
    # under a minute of pauses: nothing to explain
    p = call("jobPlanCells", job(actual_sec=17000, paused_sec=30))
    assert (p["delta"], p["cls"], p["tip"]) == ("−6%", "jd-ok", "оценка слайсера · факт 4ч 43м, план 5ч")


def test_linear_plan_is_gray_without_delta():
    p = call("jobPlanCells", job(planned_src="linear", actual_sec=30000))
    assert (p["plan"], p["delta"], p["cls"], p["planCls"], p["tip"]) == ("5ч", "", "", "jd-ok", "оценка по прогрессу")
    c = cells(row(job(planned_src="linear")))
    spans = [a for t_, a in tags(c[DUR][1]) if t_ == "span"]
    assert [(a["class"], a.get("data-tip")) for a in spans] == [("job-plan jd-ok", "оценка по прогрессу")]   # no Δ
    assert text(c[DUR][1]) == "5ч 12м · план 5ч"
    assert text(cells(row(job(planned_sec=None)))[DUR][1]) == "5ч 12м"                              # no plan


@pytest.mark.parametrize("kw", [dict(result="cancelled"), dict(start_exact=0), dict(end_exact=0),
                                dict(result=None, ended_at=None, actual_sec=None)])
def test_no_delta_without_a_measurable_fact(kw):
    p = call("jobPlanCells", job(**kw))
    assert p["plan"] == "5ч" and p["delta"] == "" and p["cls"] == "" and p["tip"] == "оценка слайсера"


@pytest.mark.parametrize("planned", [None, 0])
def test_no_plan(planned):
    assert call("jobPlanCells", job(planned_sec=planned)) == {"plan": "—", "planCls": "", "delta": "", "cls": "",
                                                               "tip": ""}


# --- reasons ---------------------------------------------------------------------------------

def test_reason_text():
    assert call("jobReasonText", "stage:paused_user", None) == "Пауза (пользователь)"
    assert call("jobReasonText", "stage:paused_filament_runout", "ignored") == "Пауза: закончился филамент"
    assert call("jobReasonText", "stage:some_new_stage", None) == "Some new stage"
    assert call("jobReasonText", "0300_8007", "Пропало питание") == "0300_8007: Пропало питание"
    assert call("jobReasonText", "HMS_0300_4000", None) == "HMS_0300_4000"
    assert call("jobReasonText", None, None) == ""
    assert call("jobReasonText", "<b>", '"x"') == "&lt;b&gt;: &quot;x&quot;"


def test_pauses_cell():
    p = call("jobPausesCell", job(pauses=2, paused_sec=840, pause_codes=["0300_8007", "stage:paused_user"],
                                  offline_n=2, offline_sec=1500))
    assert p == {"text": "2 · 14м", "tip": "0300_8007, Пауза (пользователь), без связи ×2, 25м"}
    assert call("jobPausesCell", job()) == {"text": "—", "tip": ""}
    assert call("jobPausesCell", job(pauses=1, paused_sec=20)) == {"text": "1", "tip": ""}


# --- stats sections ----------------------------------------------------------------------------

def stats(**kw):
    s = {"fr": NOW - 7 * 86400, "to": NOW,
         "total": {"jobs": 34, "finished": 30, "cancelled": 1, "failed": 1, "power_loss": 0, "stopped": 1,
                   "lost": 0, "open": 1, "success_pct": 90.9, "print_hours": 123.44, "acc_median": 1.04,
                   "acc_p90": 1.1, "acc_n": 20, "good": 0, "defect": 0},
         "printers": [{"printer_id": "p-1", "label": "P2S-1", "model": "P2S", "jobs": 20, "finished": 18,
                       "cancelled": 1, "failed": 0, "power_loss": 0, "stopped": 0, "lost": 0, "open": 1,
                       "success_pct": 94.7, "acc_median": 1.04, "acc_n": 12, "print_hours": 80.0,
                       "avg_sec": 15000, "top_code": "0300_4000", "top_code_text": "Ошибка подогрева стола"},
                      {"printer_id": "p-2", "label": "<K1>", "model": None, "jobs": 14, "finished": 12,
                       "cancelled": 0, "failed": 1, "power_loss": 0, "stopped": 1, "lost": 0, "open": 0,
                       "success_pct": None, "acc_median": None, "acc_n": 0, "print_hours": 43.4,
                       "avg_sec": None, "top_code": None, "top_code_text": None}],
         "failure_codes": [{"code": "0300_4000", "text": "Ошибка подогрева стола", "n": 3, "result": "failed"}],
         "cancel_n": 1,
         "pause_reasons": [{"code": "0300_8007", "text": "Пропало питание", "n": 5},
                           {"code": "stage:paused_user", "text": None, "n": 2}],
         "per_day": [{"day": "2026-10-16", "jobs": 10, "finished": 8, "bad": 2},
                     {"day": "2026-10-17", "jobs": 20, "finished": 20, "bad": 0}]}
    s.update(kw)
    return s


def chips(html):
    """[(data-result, label, count or None, on, zero)] of jobsChipsHtml."""
    out = []
    for m in re.finditer(r'<button type="button" class="farm-chip([^"]*)" data-result="([^"]*)" aria-pressed="(true|false)"'
                         r' data-tip="[^"]*">([^<]*?)(?: <b>(\d+)</b>)?(?:<span|</button>)', html):
        cls = m.group(1).split()
        out.append((m.group(2), m.group(4).strip(), None if m.group(5) is None else int(m.group(5)), m.group(3) == "true",
                    "zero" in cls))
        assert ("on" in cls) == (m.group(3) == "true")
    return out


def test_result_chips_count_and_filter():
    # IA §7 (plan step 9): the chips are the totals and the filter at once; no separate KPI row
    got = chips(call("jobsChipsHtml", stats(), []))
    assert got == [("", "Все", 34, True, False), ("finished", "Успешно", 30, False, False),
                   ("cancelled", "Отменено", 1, False, False), ("failed", "Ошибка", 1, False, False),
                   ("power_loss", "Питание", 0, False, True), ("lost", "Нет связи", 0, False, True),
                   ("stopped", "Остановлено", 1, False, False), ("open", "Идут", 1, False, False)]
    assert sorted(c[0] for c in got[1:]) == sorted(job_queries.RESULT_FILTERS)
    html = call("jobsChipsHtml", stats(), ["failed", "open"])
    on = [c[0] for c in chips(html) if c[3]]
    assert on == ["failed", "open"] and html.count('class="farm-x"') == 2          # the chosen ones with ×
    # before the counts land: the chips without numbers
    assert [c[2] for c in chips(call("jobsChipsHtml", None, []))] == [None] * 8
    assert '<div class="dash-kpi' not in HTML and 'id="jobsKpi"' not in HTML


def test_result_chip_click():
    # as the farm's chips (IA §5): only this, the same single one again — all, Ctrl — add or take away
    def click(results, rid, add=False):
        return call("jobsResultsClick", results, rid, add)
    assert click([], "failed") == ["failed"]
    assert click(["failed"], "failed") == []
    assert click(["failed", "open"], "failed") == ["failed"]
    assert click(["failed"], "open", True) == ["failed", "open"]
    assert click(["failed", "open"], "open", True) == ["failed"]
    assert click(["failed"], "") == [] and click(["failed"], "bogus") == ["failed"]
    assert click(["bogus", "failed"], "open", True) == ["failed", "open"]


def test_shown_counter():
    st = stats()
    assert call("jobsShownText", 34, st, [], False) == "показано 34 из 34"
    assert call("jobsShownText", 50, stats(total=dict(st["total"], jobs=216)), [], True) == "показано 50 из 216"
    assert call("jobsShownText", 1, st, ["failed", "open"], False) == "показано 1 из 2"
    assert call("jobsShownText", 50, None, [], True) == "показано 50+"
    assert call("jobsShownText", 50, st, [], True) == "показано 50+"            # the counts are behind the list


def test_reasons():
    html = call("jobsReasonsHtml", stats())
    cols = re.findall(r'<div class="jobs-reasons-col"><h4>([^<]*)</h4>(.*?)</div>(?=<div class="jobs-reasons-col"|$)',
                      html, re.S)
    assert [c for c, _ in cols] == ["Ошибки и прерывания", "Паузы"]
    items = re.findall(r'<code>([^<]*)</code><span class="jobs-reason-text"(?: data-tip="[^"]*")?>([^<]*)</span>'
                       r'<b>(\d+)</b>', html)
    assert items == [("0300_4000", "Ошибка подогрева стола", "3"), ("", "Отменены", "1"),
                     ("0300_8007", "Пропало питание", "5"), ("", "Пауза (пользователь)", "2")]
    empty = call("jobsReasonsHtml", stats(failure_codes=[], pause_reasons=[], cancel_n=0))
    assert "Ошибок нет" in empty and "Пауз с причиной нет" in empty and "Отменены" not in empty
    evil = call("jobsReasonsHtml", stats(failure_codes=[{"code": "<x>", "text": "<y>", "n": 1}]))
    assert "<x>" not in evil and "<y>" not in evil and "&lt;y&gt;" in evil


def test_day_bars():
    html = call("jobsDayBarsHtml", stats())
    days = re.findall(r'<div class="jobs-day" data-tip="([^"]*)"><span class="jobs-day-bar">'
                      r'<i class="jobs-day-other" style="height:([\d.]+)%"></i>'
                      r'<i class="jobs-day-ok" style="height:([\d.]+)%"></i></span>'
                      r'<span class="jobs-day-lbl">([^<]*)</span></div>', html)
    assert days == [("16.10: 10, успешно 8", "10.0", "40.0", "16.10"), ("17.10: 20, успешно 20", "0.0", "100.0", "17.10")]
    many = [{"day": "2026-09-%02d" % (i + 1), "jobs": 1, "finished": 1, "bad": 0} for i in range(30)]
    labels = re.findall(r'<span class="jobs-day-lbl">([^<]*)</span>', call("jobsDayBarsHtml", stats(per_day=many)))
    assert len(labels) == 30 and len([x for x in labels if x]) == 10 and labels[0] == "01.09"
    # 90 days: a label every 10th column; the one on the last column would stick out of the chart
    d0 = datetime.date(2026, 7, 20)
    quarter = [{"day": (d0 + datetime.timedelta(days=i)).isoformat(), "jobs": 1, "finished": 1, "bad": 0}
               for i in range(91)]
    labels = re.findall(r'<span class="jobs-day-lbl">([^<]*)</span>', call("jobsDayBarsHtml", stats(per_day=quarter)))
    assert [i for i, x in enumerate(labels) if x] == list(range(0, 81, 10)) and labels[80] == "08.10"
    quiet = [{"day": "2026-10-17", "jobs": 0, "finished": 0, "bad": 0}]
    assert call("jobsDayBarsHtml", stats(per_day=quiet)) == '<div class="dash-stats-empty">Печатей за период нет</div>'


# --- requests ------------------------------------------------------------------------------------

def test_queries_encode_the_filters():
    f = {"days": 7, "fr": None, "to": None, "printer": "p 1&x=#", "results": ["finished", "failed"]}
    fr, to = int(NOW - 7 * 86400), int(NOW)
    # 50 a page (IA §10, plan step 9); «Печати»'s stats count the prints in maintenance too — the chips'
    # numbers are the list's (deliberately changed)
    assert call("jobsQuery", f, NOW, 400) == (
        "fr=%d&to=%d&printer=p%%201%%26x%%3D%%23&result=finished%%2Cfailed&limit=50&offset=400" % (fr, to))
    assert call("jobsStatsQuery", f, NOW) == "fr=%d&to=%d&printer=p%%201%%26x%%3D%%23&include_maint=true" % (fr, to)
    assert call("jobsCsvUrl", f, NOW) == (
        "/api/history/jobs.csv?fr=%d&to=%d&printer=p%%201%%26x%%3D%%23&result=finished%%2Cfailed" % (fr, to))
    assert call("jobsQuery", {"days": 1}, NOW + 0.5, None) == "fr=%d&to=%d&limit=50&offset=0" % (
        int(NOW - 86400), int(NOW) + 1)


def test_query_window_from_the_filter():
    assert call("jobsWindow", {"days": 30}, NOW) == {"fr": NOW - 30 * 86400, "to": NOW}
    assert call("jobsWindow", {"days": 7, "fr": NOW - 5 * H, "to": NOW - H}, NOW) == {"fr": NOW - 5 * H, "to": NOW - H}
    assert call("jobsWindow", {"days": 1, "fr": NOW - 50 * H}, NOW) == {"fr": NOW - 50 * H, "to": NOW}
    assert call("jobsWindow", {"days": 1, "to": NOW - H}, NOW) == {"fr": NOW - 25 * H, "to": NOW - H}
    assert call("jobsWindow", {}, NOW) == {"fr": NOW - 7 * 86400, "to": NOW}


def test_a_result_chip_reloads_the_list_only():
    dom = _dom_part()
    load = _js_function("loadJobsView")
    assert "var listOnly = !!(opts && opts.listOnly) && !!jobsStats && !jobsStatsStale;" in load
    # a chip clicked while a new period's load is in flight: that load's stats never landed, reload them too
    assert load.index("if (!listOnly) jobsStatsStale = true;") < load.index("await Promise.all")
    assert load.index("jobsStatsStale = false;") > load.index("jobsStats = stats;")
    assert re.search(r"listOnly \? \[\] : \[apiFetch\(\"/api/history/jobs/stats\?\"", load)
    assert "if (listOnly) renderJobsTable(); else renderJobsView();" in load
    chips = dom[dom.index('jobsEls.results.addEventListener("click"'):]
    chips = chips[:chips.index('getElementById("jobsCsvBtn")')]   # up to the CSV button that follows
    assert "jobsReload({ listOnly: true });" in chips and "jobsSetResults(" in chips
    # the period and the printer are the head's (plan step 8, deliberately changed): a new filter shows
    # the loader and asks the stats too — the list's filter key, not a reload of its own
    assert dom.count("jobsReload({ listOnly: true });") == 1 and "jobsReload();" not in dom
    assert "var first = !jobsLoadedAt || key !== jobsLoadedKey;" in load
    assert load.index("var key = JSON.stringify(histQueryOf(histFilter));") < load.index("var first =")


# --- the real API shapes -------------------------------------------------------------------------

def _seed(path):
    conn = sqlite3.connect(path)
    try:
        jj.init_db(conn)
        base = dict(printer_id="p-1", kind="bambu", model="P2S", job_name=NAME, start_exact=1, end_exact=1,
                    source="live")
        pause = json.dumps([{"at": 0, "sec": 60, "code": "stage:paused_user", "text": None},
                            {"at": 0, "sec": 60, "code": "0300_8007", "text": "Пропало питание"}], ensure_ascii=False)
        jobs = [
            dict(result="finished", started_at=NOW - 10 * H, ended_at=NOW - 5 * H, last_seen=NOW - 5 * H,
                 actual_sec=5 * H, planned_sec=17000, planned_src="slicer", verdict="good"),
            dict(result="failed", started_at=NOW - 30 * H, ended_at=NOW - 29 * H, last_seen=NOW - 29 * H,
                 actual_sec=H, error_code="0300_4000", error_text="Ошибка подогрева стола", pauses=2,
                 paused_sec=120, pauses_json=pause, power_losses=1, job_name=None),
            dict(printer_id="p-2", kind="klipper", model=None, result="stopped", started_at=NOW - 8 * H,
                 ended_at=NOW - 7 * H, last_seen=NOW - 7 * H, actual_sec=H, planned_sec=4000,
                 planned_src="linear", start_exact=0, maint=1),
            dict(printer_id="p-2", kind="klipper", model=None, result=None, end_exact=None, started_at=NOW - 2 * H,
                 last_seen=NOW - 300, end_progress=12.5, ctx="{}", job_name=EVIL),
        ]
        for j in jobs:
            fields = dict(base, **j)
            jj.insert_job(conn, fields, fields["last_seen"])
        conn.commit()
    finally:
        conn.close()


def test_renders_what_the_api_serves(tmp_path, monkeypatch):
    monkeypatch.setattr(job_queries, "_TZ", MSK)
    db = str(tmp_path / "printer_history.db")
    _seed(db)
    labels = {"p-1": "P2S-1", "p-2": "Varan"}
    page = job_queries.list_jobs(db, NOW - 7 * 86400, NOW, labels=labels)
    st = job_queries.job_stats(db, NOW - 7 * 86400, NOW, include_maint=True, labels=labels)
    assert len(page["rows"]) == 4
    out = run("""(function () {
      var page = %s, st = %s, printers = {"p-2": {"online": true, "state": "printing", "progress_pct": 13,
                                                   "eta_seconds": 7200}};
      return {
        rows: page.rows.map(function (j) { return jobRowHtml(j, printers[j.printer_id] || null, {now: %r}); }),
        open: page.rows.map(function (j) { return jobRowHtml(j, null, {now: %r, open: true}); }),
        details: page.rows.map(function (j) { return jobDetailHtml(j, {now: %r}); }),
        head: [jobsHeadHtml()], chips: jobsChipsHtml(st, []), reasons: jobsReasonsHtml(st), days: jobsDayBarsHtml(st)
      };
    })()""" % (json.dumps(page, ensure_ascii=False), json.dumps(st, ensure_ascii=False), NOW, NOW, NOW))
    for key, chunk in out.items():
        for html in chunk if isinstance(chunk, list) else [chunk]:
            for bad in ("undefined", "NaN", "null", "[object", "<img"):
                assert bad not in html, (key, bad)
    th = len(re.findall(r"<th>", out["head"][0]))
    assert all(len(re.findall(r"<td ", h)) == th for h in out["rows"] + out["open"])
    by_id = {int(re.search(r'data-job-id="(\d+)"', h).group(1)): h for h in out["rows"]}
    running = next(h for h in by_id.values() if 'class="job-row job-open"' in h)
    assert "Идёт" in text(running) and "13% · осталось 2ч" in text(running)
    # one print has a mark in the journal: nothing shows it (the owner, 29.10)
    assert not any("data-verdict" in h or "Годно" in h for h in out["rows"] + out["open"] + out["details"])
    # the chips count the four prints, the one in maintenance too (the list shows it)
    assert chips(out["chips"])[0] == ("", "Все", 4, True, False)
    failed = next(h for h in out["details"] if "0300_4000" in h)
    assert "Ошибка подогрева стола" in failed and "Пропало питание" in failed and "⚡ пропадало питание ×1" in failed
    assert "Пауза (пользователь)" in out["reasons"] and "0300_4000" in out["reasons"]


# --- the unfolded row (IA §7, plan step 9) ---------------------------------------------------------------

def test_details_of_a_row():
    j = job(result="failed", error_code="0300_4000", error_text="Ошибка подогрева стола", end_progress=47.4,
            pauses=2, paused_sec=900, pause_codes=["0500_8062", "stage:paused_user"],
            pause_list=[{"at": NOW - 5 * H, "sec": 600, "code": "0500_8062", "text": "Маркер стола не найден"},
                        {"at": NOW - 4 * H, "sec": 300, "code": "stage:paused_user", "text": None}],
            offline_n=2, offline_sec=1500, power_losses=1)
    hits = [{"at": NOW - 5 * H + 30, "code": "0500_8062", "ok": True, "text": "автопропуск нажал ✓"},
            {"at": NOW - 3 * H, "code": "0C00_8009", "ok": False, "text": "автопропуск не нажал: попытки кончились"}]
    xp = call("jobDetailHtml", j, {"now": NOW, "hits": hits})
    secs = re.findall(r"<div><h5>([^<]*)</h5>(.*?)</div>(?=<div><h5>|</div>$)", xp, re.S)
    assert [h for h, _ in secs] == ["Паузы · 2 · 15м", "Конец", "Задание"]
    pz = [text(r) for r in re.findall(r'<div class="r[^"]*">(.*?)</div>', secs[0][1])]
    # each pause: its time, reason (the code apart), length; the press next to its pause, the rest after
    assert pz == ["07:00 0500_8062 Маркер стола не найден · 10м автопропуск нажал ✓ 07:00",
                  "08:00 Пауза (пользователь) · 5м",
                  "0C00_8009 автопропуск не нажал: попытки кончились 09:00",
                  "без связи ×2, 25м", "⚡ пропадало питание ×1"]
    assert '<span class="job-tag ok">автопропуск нажал ✓ 07:00</span>' in xp
    end = [text(r) for r in re.findall(r'<div class="r[^"]*">(.*?)</div>', secs[1][1])]
    assert end == ["0300_4000 Ошибка подогрева стола на 47% · 11:12", "план 5ч — оценка слайсера"]
    what = [text(r) for r in re.findall(r'<div class="r[^"]*">(.*?)</div>', secs[2][1])]
    assert what == ["Деталь A.gcode", "слои 300/300"]
    # a server without pause_list (the Pi before this step): the codes, without times
    old = call("jobDetailHtml", dict(j, pause_list=None), {"now": NOW})
    assert "0500_8062" in old and "Пауза (пользователь)" in old and "07:00" not in old
    # no pauses; a running print; a lost one; a print in maintenance mode
    quiet = call("jobDetailHtml", job(), {"now": NOW})
    assert "пауз не было" in quiet and "Успешно" in quiet and "Паузы · 0</h5>" in quiet
    running = text(call("jobDetailHtml", open_job(), {"now": NOW}))
    assert "идёт · 40%" in running and "Конец" in running
    lost = text(call("jobDetailHtml", job(result="lost", end_progress=12.0), {"now": NOW}))
    assert "Нет связи — связь пропала, и продолжения печати не было видно на 12%" in lost
    assert "шла в режиме обслуживания" in call("jobDetailHtml", job(maint=1), {"now": NOW})
    more = call("jobDetailHtml", job(pauses=5, pause_list=[{"at": NOW - H, "sec": 60, "code": None, "text": None}]), {"now": NOW})
    assert "11:00 пауза · 1м" in text(more) and "и ещё 4" in text(more)


def test_details_with_a_press_but_no_pause_in_the_journal():
    # the auto-ignore pressed «Игнорировать и продолжить», the journal caught no pause (a short one):
    # no «пауз не было» next to the press — the press says the printer stopped
    hits = [{"at": NOW - 2 * H, "code": "0500_8062", "ok": True, "text": "автопропуск нажал ✓"}]
    xp = call("jobDetailHtml", job(pauses=0, pause_list=[], pause_codes=[]), {"now": NOW, "hits": hits})
    secs = re.findall(r"<div><h5>([^<]*)</h5>(.*?)</div>(?=<div><h5>|</div>$)", xp, re.S)
    pz = [text(r) for r in re.findall(r'<div class="r[^"]*">(.*?)</div>', secs[0][1])]
    assert pz == ["пауза не записана в журнал", "0500_8062 автопропуск нажал ✓ 10:00"]
    assert "пауз не было" not in xp
    # without the press (a viewer has none): «пауз не было», as before
    assert "пауз не было" in call("jobDetailHtml", job(pauses=0, pause_list=[], pause_codes=[]), {"now": NOW, "hits": []})


def test_auto_ignore_presses_from_the_audit():
    j = job(printer_id="p-1", started_at=NOW - 6 * H, ended_at=NOW - H)
    rows = [{"action": "auto_ignore:p-1:0500_8062:confirmed", "created_at": NOW - 2 * H},
            {"action": "auto_ignore:p-1:0500_8062:sent", "created_at": NOW - 5 * H},
            {"action": "auto_ignore:p-1:0C00_8009:skip:tries_used", "created_at": NOW - 4 * H},
            {"action": "auto_ignore:p-1:0C00_8009:skip:state_RUNNING", "created_at": NOW - 3 * H},
            {"action": "auto_ignore:p-1:0C00_8009:skip:maintenance", "created_at": NOW - 3 * H + 1},
            {"action": "auto_ignore:p-1:0C00_8009:refused", "created_at": NOW - 3 * H + 2},
            {"action": "auto_ignore:p-1:0500_8062:confirmed", "created_at": NOW - 30},          # after its end
            {"action": "auto_ignore:p-10:0500_8062:confirmed", "created_at": NOW - 2 * H},     # another printer
            {"action": "login_ok", "created_at": NOW - 2 * H}, {"action": None}, "junk", None]
    got = call("jobAutoHits", j, rows, NOW)
    assert [(h["at"], h["code"], h["ok"], h["text"]) for h in got] == [
        (NOW - 5 * H, "0500_8062", False, "автопропуск нажал, принтер не подтвердил"),
        (NOW - 4 * H, "0C00_8009", False, "автопропуск не нажал: попытки кончились"),
        (NOW - 3 * H, "0C00_8009", False, "автопропуск не нажал: принтер не на паузе"),
        (NOW - 3 * H + 1, "0C00_8009", False, "автопропуск не нажал: принтер на обслуживании"),
        (NOW - 3 * H + 2, "0C00_8009", False, "автопропуск не нажал: refused"),
        (NOW - 2 * H, "0500_8062", True, "автопропуск нажал ✓")]
    # a running print: up to now
    assert len(call("jobAutoHits", open_job(printer_id="p-1"), rows, NOW)) == 2
    assert call("jobAutoHits", j, None, NOW) == [] and call("jobAutoHits", None, rows, NOW) == []


# --- the page: История → Печати (markup, wiring, styles) ------------------------------------------

HTML = (ROOT / "app" / "web" / "index.html").read_text(encoding="utf-8")


def _dom_part() -> str:
    """The journal's DOM code: from its banner to the timeline canvas setup after it."""
    m = re.search(r"/\* ── История → Печати: the print journal ─+ \*/(.*?)\n\s*var timelineCanvas = ", JS, re.S)
    assert m, "the print journal's DOM part not found in app.js"
    return m.group(1)


def _element(html: str, tag: str, **attrs) -> str:
    """The first <tag ...attrs>...</tag> of html (no nesting of the same tag inside)."""
    for m in re.finditer(r"<%s\b([^>]*)>" % tag, html):
        a = dict(tags("<%s%s>" % (tag, m.group(1)))[0][1])
        if all(a.get(k) == v for k, v in attrs.items()):
            end = html.index("</%s>" % tag, m.end())
            return html[m.start():end + len(tag) + 3]
    raise AssertionError("<%s %s> not found" % (tag, attrs))


def test_history_has_its_four_sub_tabs():
    # IA §7, plan step 8 (deliberately changed): Печати (the default) · Хронология · Статистика ·
    # Действия (admins: hidden until the role says so), in the head, a section each
    sub = _element(HTML, "div", id="historySub")
    btns = [a for t, a in tags(sub) if t == "button"]
    assert [b["data-sub"] for b in btns] == ["jobs", "chrono", "stats", "actions"]
    assert [b.get("class") for b in btns] == ["active", None, None, None]
    assert [b["aria-pressed"] for b in btns] == ["true", "false", "false", "false"]
    assert "hidden" in btns[3] and all("hidden" not in b for b in btns[:3])
    assert [re.sub(r"<[^>]+>", "", x) for x in re.findall(r"<button[^>]*>(.*?)</button>", sub)] == [
        "Печати", "Хронология", "Статистика", "Действия"]
    assert "Таймлайн" not in HTML
    panel = HTML[HTML.index('data-panel="history"'):HTML.index('data-panel="ams"')]   # «Окно» is gone (plan step 7)
    hsub = {a["data-hsub"]: a for t, a in tags(panel) if "data-hsub" in a}
    assert set(hsub) == {"jobs", "chrono", "stats", "actions"}
    assert "hidden" not in hsub["jobs"] and all("hidden" in hsub[k] for k in ("chrono", "stats", "actions"))
    assert panel.index('id="histHead"') < panel.index('id="historySub"') < panel.index('data-hsub="jobs"') \
        < panel.index('data-hsub="chrono"') < panel.index('data-hsub="stats"') < panel.index('data-hsub="actions"')


def test_jobs_filters_cover_the_api():
    # the period and the printer are the head's (plan step 8): 24 ч … 90 дн (the history keeps 90 days),
    # «Свой…», one printer choice; the result chips are the list's own
    head = HTML[HTML.index('id="histHead"'):HTML.index('data-hsub="jobs"')]
    periods = [(a["data-period"], a.get("class")) for t, a in tags(head) if "data-period" in a]
    assert periods == [("24h", "active"), ("7d", None), ("30d", None), ("90d", None), ("custom", None)]
    for need in ('id="histPrinter"', 'id="hist-from"', 'id="hist-to"', 'id="hist-apply"', 'id="jobsCsvBtn"',
                 'type="date" id="hist-from"', 'type="date" id="hist-to"', 'id="histSum"'):
        assert need in head, need
    jobs = HTML[HTML.index('data-hsub="jobs"'):HTML.index('data-hsub="chrono"')]
    # the result chips are drawn with their counts (plan step 9): every result the API filters by, once
    ids = [c["id"] for c in run("JOB_CHIPS")]
    assert ids[0] == "" and sorted(ids[1:]) == sorted(job_queries.RESULT_FILTERS) and len(set(ids)) == len(ids)
    assert '<nav class="farm-chips jobs-chips" id="jobsResultBtns"' in jobs
    for need in ('class="adm-table jobs-table"', 'id="jobsMore"', 'id="jobsEmpty"', 'class="jobs-foot"', 'id="jobsCount"'):
        assert need in jobs, need
    assert jobs.index('id="jobsResultBtns"') < jobs.index('id="jobsTableWrap"') < jobs.index('id="jobsMore"')
    # three period sets, two С / По pairs and three printer pickers are one head now (IA §11)
    for gone in ("data-jobs-days", "data-range-hours", "jobsPrinterBtn", "historyPrinterBtn", "eventPrinterBtn",
                 'id="jobs-from"', 'id="history-from"', 'id="jobs-apply"', 'id="history-apply"', "datetime-local",
                 "history-range-btns", "stats-period-btn"):
        assert gone not in HTML, gone


def test_dom_part_uses_existing_elements():
    dom = _dom_part()
    ids = set(re.findall(r'getElementById\("([^"]+)"\)', dom))
    assert {"jobsPanel", "jobsBody", "jobsResultBtns", "jobsCsvBtn"} <= ids
    for i in ids:
        assert len(re.findall(r'\bid="%s"' % re.escape(i), HTML)) == 1, i
    # the list's page size is the pure block's (checked against the API bound above)
    assert "JOBS_PAGE" in dom and not re.search(r"limit=\d", dom)
    # localStorage can throw (private mode, blocked storage): every access sits in a try
    for line in dom.splitlines():
        if "localStorage" in line:
            assert "try {" in line, line
    # job names are never logged
    for args in re.findall(r"console\.\w+\(([^;]*)\);", dom):
        assert "job_name" not in args and "jobsRows" not in args


def test_rows_unfold_and_nothing_calls_the_verdict_route():
    dom = _dom_part()
    # a click (Enter / Space on the focused row) unfolds the details under the row; the open ones stay over reloads
    click = dom[dom.index('jobsEls.body.addEventListener("click"'):]
    assert 'var tr = e.target.closest("tr.job-row");' in click and "jobsToggleRow(Number(tr.dataset.jobId))" in click
    assert 'if (e.key !== "Enter" && e.key !== " ") return;' in dom
    render = _js_function("renderJobsTable")
    assert "jobRowHtml(j, j.result == null ? printers[j.printer_id] || null : null, { now: now, open: open })" in render
    assert "(open ? jobsDetailRow(j, now) : \"\")" in render and "jobsOpenRows = kept;" in render
    assert "back.focus({ preventScroll: true });" in render                    # the focus stays on its row
    detail = _js_function("jobsDetailRow")
    assert '<td colspan="\' + JOB_COLS.length + \'">' in detail
    assert "var hits = isAdmin() && jobsAuto.rows ? jobAutoHits(j, jobsAuto.rows, now) : [];" in detail
    # the admins' auto-ignore presses: the audit, once a minute at most, only with a row unfolded
    need = _js_function("jobsAutoNeed")
    assert need.index("if (!isAdmin() || jobsAuto.busy || Date.now() - jobsAuto.at < JOBS_AUTO_MS) return;") \
        < need.index('apiFetch("/api/admin/audit?user=auto-ignore&limit=" + JOBS_AUTO_LIMIT)')
    assert "if (!Object.keys(jobsOpenRows).length) return;" in need
    admin = (ROOT / "app" / "api" / "admin.py").read_text(encoding="utf-8")
    le = int(re.search(r'@router\.get\("/audit"\)\s*def \w+\((.*?)\):', admin, re.S).group(1)
             .split("limit: int = Query(100, ge=1, le=")[1].split(")")[0])
    assert int(re.search(r"var JOBS_AUTO_LIMIT = (\d+);", JS).group(1)) <= le
    # «Годно / Брак» is gone (the owner, 29.10): nothing PUTs a mark; the route stays on the server
    assert "/verdict" not in JS and "data-verdict" not in JS and "setJobVerdict" not in JS
    assert '@router.put("/history/jobs/{job_id}/verdict")' in admin
    assert ".job-verdict" not in CSS and ".job-good" not in CSS and ".job-defect" not in CSS
    # the role arrives after the first render: the table re-renders with it (the admins' presses)
    assert "if (jobsEls && jobsStats) renderJobsTable();" in _js_function("updateAuthUI")


def test_csv_button_is_the_admins_only():
    # IA step 1: the server answers a viewer 403, so a viewer has no button at all; plan step 8
    # (deliberately changed): «⇩ Выгрузить» sits in the head; plan step 12 (deliberately changed):
    # on «Печати» and «Действия» (IA §7; the audit CSV: tests/test_audit_api.py, test_web_acts_ui.py)
    btn = [a for t, a in tags(HTML) if a.get("id") == "jobsCsvBtn"]
    assert len(btn) == 1 and "hidden" in btn[0]            # hidden until the role is known
    assert btn[0]["data-tip"] and "title" not in btn[0] and HTML.count("Выгрузить</button>") == 1
    assert ".hist-head [hidden] { display: none !important; }" in CSS
    assert "histRoleUI();" in _js_function("updateAuthUI")
    role = _js_function("histRoleUI")
    assert 'histEls.csv.hidden = !(admin && (historySub === "jobs" || historySub === "actions"));' in role
    assert "act.hidden = !admin;" in role                   # «Действия» too
    assert "histRoleUI();" in _js_function("setHistorySub")
    click = _dom_part()
    click = click[click.index('document.getElementById("jobsCsvBtn").addEventListener("click"'):]
    assert click.index("if (!isAdmin()) return;") < click.index("jobsCsvUrl(jobsQueryFilter(now), now)")
    api = API[API.index('@app.get("/api/history/jobs.csv"'):]
    assert api.startswith('@app.get("/api/history/jobs.csv", dependencies=[Depends(_admin_only)])')


def test_history_load_waits_for_the_iife():
    body = _js_function("setActiveView")
    assert "if (view === 'history') { Promise.resolve().then(function () { histLoadSub(); }); }" in body
    load = _js_function("histLoadSub")
    assert 'if (historySub === "jobs") loadJobsView();' in load and 'else if (historySub === "chrono") loadTimeline();' in load
    assert 'else if (historySub === "stats") loadHistStats();' in load
    # the running rows follow the status poll
    load = _js_function("load")
    assert load.index("applyAndRender();") < load.index("jobsTick();")


def test_jobs_tooltips_use_the_clamped_js_tip():
    # a CSS ::after tip would stick out of the table / the card on a phone
    # one binding for the whole page since plan step 14 (deliberately changed): «История» is in it
    assert "bindTips(document);" in JS and 'bindTips(document.getElementById(' not in JS
    assert "dash-stats-list" not in HTML and "dash-event-log" not in HTML


def test_phone_layout_turns_rows_into_two_lines():
    # IA §7 (plan step 9, deliberately changed): up to tablets a print is two lines (≈ 60 px, was a 150 px
    # card): printer · start · name and the result, then the duration with its plan and the pauses; a
    # running print adds its live line; the layers and the rest are in the unfolded details
    blocks = re.findall(r"@media \(max-width: 1100px\) \{(.*?)\n    \}", CSS, re.S)
    jobs = next((b for b in blocks if ".jobs-table" in b), None)
    assert jobs, "no @media (max-width: 1100px) block for .jobs-table"
    assert '"printer when name state" "dur dur dur pauses"' in jobs
    assert '.jobs-table tr.job-row.job-open { grid-template-areas: "printer when name name" "dur dur dur pauses" "state state state state"; }' in jobs
    for col in ("printer", "state", "when", "dur", "pauses", "name"):
        assert re.search(r"td\.job-%s \{ grid-area: %s;" % (col, col), jobs), col
    assert ".jobs-table td.job-layers { display: none; }" in jobs and ".jobs-table .job-model { display: none; }" in jobs
    assert ".jobs-table td.job-state .job-live { display: inline; white-space: nowrap;" in jobs
    assert ".jobs-table td.job-pauses.job-nil { visibility: hidden; }" in jobs
    assert ".jobs-table tr.job-xrow { display: block;" in jobs
    laptop = re.findall(r"@media \(max-width: 1440px\) \{(.*?)\n    \}", CSS, re.S)
    assert any("td.job-when, .jobs-table td.job-state { white-space: normal; }" in b for b in laptop)
    # the phone rules come later and win
    assert CSS.index(".jobs-table td.job-name { max-width: 180px; }") < CSS.index(".jobs-table thead { display: none; }")
    assert ".jobs-table thead { display: none; }" in jobs
    assert "[data-hsub][hidden]" in CSS


def test_every_rendered_class_has_a_style():
    out = run("""(function () {
      var now = %r, H = 3600, base = {id: 1, printer_id: "p-1", label: "P2S-1", model: "P2S", job_name: "Деталь A",
        started_at: now - 6 * H, ended_at: now - H, start_exact: 1, end_exact: 1, last_seen: now - H, result: "finished",
        planned_sec: 18000, planned_src: "slicer", actual_sec: 5 * H, paused_sec: 600, pauses: 1,
        pause_codes: ["stage:paused_user"], offline_sec: 900, offline_n: 1, power_losses: 2, end_progress: 100,
        layer: 300, total_layers: 300, maint: 1, verdict: "good"};
      function j(kw) { return Object.assign({}, base, kw); }
      var open = {result: null, ended_at: null, end_exact: null, actual_sec: null};
      var rows = [j({}), j({verdict: "defect", planned_src: "linear"}), j({result: "failed", error_code: "0300_4000"}),
        j({actual_sec: 7 * H}), j({actual_sec: 3 * H}), j({pauses: 0, pause_codes: []})];
      ["cancelled", "power_loss", "stopped", "lost"].forEach(function (r) { rows.push(j({result: r})); });
      var hits = [{at: now - 5 * H, code: "stage:paused_user", ok: true, text: "автопропуск нажал ✓"}];
      var html = rows.map(function (r) {
        return jobRowHtml(r, null, {now: now}) + jobRowHtml(r, null, {now: now, open: true}) +
          jobDetailHtml(Object.assign({}, r, {pause_list: [{at: now - 5 * H, sec: 600, code: "stage:paused_user", text: null}]}),
                        {now: now, hits: hits});
      });
      [{id: "p-1", online: true, state: "printing", progress_pct: 40, eta_seconds: H}, {id: "p-1", online: true, state: "paused"},
       {id: "p-1", online: false, state: "offline"}].forEach(function (p) {
        html.push(jobRowHtml(j(open), p, {now: now}) + jobDetailHtml(j(open), {now: now}));
      });
      var st = {total: {jobs: 3, finished: 2, open: 1, success_pct: 66.7, print_hours: 5, acc_median: 1.04, acc_p90: 1.1,
                        acc_n: 2, good: 1, defect: 1},
                printers: [{printer_id: "p-1", label: "P2S-1", model: "P2S", jobs: 7, finished: 1, cancelled: 1, failed: 1,
                            power_loss: 1, stopped: 1, lost: 1, open: 1, success_pct: 20, acc_median: 1, acc_n: 1,
                            top_code: "0300_4000", top_code_text: "x"}],
                failure_codes: [{code: "0300_4000", text: "x", n: 1}], cancel_n: 1,
                pause_reasons: [{code: "stage:paused_user", text: null, n: 1}],
                per_day: [{day: "2026-10-17", jobs: 3, finished: 2, bad: 1}]};
      html.push(jobsHeadHtml(), jobsChipsHtml(st, ["finished"]), jobsChipsHtml(null, []), jobsReasonsHtml(st),
                jobsDayBarsHtml(st), jobsReasonsHtml({}));
      return html.join("");
    })()""" % NOW)
    classes = set()
    for cls in re.findall(r'class="([^"]+)"', out):
        classes.update(cls.split())
    classes.discard("active")
    assert {"jobs-day-ok", "job-open", "job-flag", "jd-slow", "jd-fast", "job-live", "job-end", "jobs-cancel", "job-row",
            "job-x", "job-chev", "job-model", "job-plan", "job-delta", "job-nil", "job-xp", "r", "job-t", "job-dim",
            "job-tag", "ok", "job-full", "farm-chip", "zero", "on", "farm-x"} <= classes
    assert not classes & {"job-good", "job-defect", "job-verdict", "dash-kpi"}
    for cls in sorted(classes):
        assert re.search(r"\.%s(?![\w-])[^{}]*\{" % re.escape(cls), CSS), cls
