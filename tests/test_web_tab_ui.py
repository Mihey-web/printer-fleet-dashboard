"""The browser tab of app/web/app.js (spec_pwa_tab §3): title and favicon key, run in node.

The block between /* tab-ui:begin */ and /* tab-ui:end */ holds only pure
function declarations. statusOf / attentionOf are stubs here, except in the
parity test, which runs the real nfAttention and nfBellHtml of the nf-ui
block: the count in the title must be the header bell's. Skipped without node.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
JS = (ROOT / "app" / "web" / "app.js").read_text(encoding="utf-8")


def _block(tag):
    m = re.search(r"/\* %s:begin \*/(.*?)/\* %s:end \*/" % (tag, tag), JS, re.S)
    assert m, "%s block not found in app.js" % tag
    return m.group(1)


def _function(name):
    m = re.search(r"function %s\([^)]*\) \{.*?\n\s*\}" % name, JS, re.S)
    assert m, "%s() not found in app.js" % name
    return m.group(0)


def _node(src):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    out = subprocess.run([node, "-"], input=src, capture_output=True, text=True, timeout=60, encoding="utf-8")
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


STUBS = """
function st(p) { return p.offline ? "offline" : p.state; }
function att(p) { return !!p.att; }
function title(printers, stale) { return tabTitle(tabSummary(printers, st, att), "Кузница", !!stale); }
function key(printers, stale) { return tabIconKey(tabSummary(printers, st, att), !!stale); }
"""


def run(expr):
    """Evaluate `expr` (JSON-serializable) against the tab-ui block with the stubs."""
    return _node(_block("tab-ui") + STUBS + "\nconsole.log(JSON.stringify(" + expr + "));")


def js(v):
    return json.dumps(v, ensure_ascii=False)


def prn(state="idle", pct=None, eta=None, att=False, offline=False):
    return {"state": state, "progress_pct": pct, "eta_seconds": eta, "att": att, "offline": offline}


PAUSED_ERR = prn("paused", pct=40, att=True)
A = prn("printing", pct=47.6, eta=600)
B = prn("printing", pct=80, eta=3600)

# The table of §3.1: fleet -> title.
TABLE = [
    ([], False, "Кузница"),
    ([prn(), prn("finished"), prn("idle")], False, "Кузница"),
    ([A, B], False, "47% · Кузница"),                                  # nearest to the finish, not the max
    ([A, B, PAUSED_ERR, PAUSED_ERR, prn("error", att=True)], False, "(3⚠) 47% · Кузница"),
    ([prn(), PAUSED_ERR, PAUSED_ERR], False, "(2⚠) Кузница"),
    ([prn("printing", pct=30), prn("printing", pct=65)], False, "65% · Кузница"),   # no ETA: the max
    ([prn("printing", pct=100, eta=30)], False, "99% · Кузница"),     # 100 % only after the finish
    ([A, PAUSED_ERR], True, "нет связи · Кузница"),
]


def test_title_table():
    got = run("[" + ",".join("title(%s, %s)" % (js(f), js(s)) for f, s, _ in TABLE) + "]")
    assert got == [want for *_, want in TABLE]


def test_warning_sign_has_no_variation_selector():
    title = run("title(%s)" % js([PAUSED_ERR]))
    assert title == "(1⚠) Кузница" and "️" not in title


def test_eta_beats_progress():
    fleet = [prn("printing", pct=90, eta=5000), prn("printing", pct=10, eta=200),
             prn("printing", pct=95, eta=0), prn("printing", pct=99, eta=None)]
    # eta 0 / none do not compete with a real ETA (renderSummary's rule)
    assert run("tabSummary(%s, st, att)" % js(fleet)) == {"attention": 0, "pct": 10}


def test_pct_rules():
    got = run("[" + ",".join("tabSummary(%s, st, att).pct" % js(f) for f in (
        [prn("printing", pct=47.9, eta=10)],                     # floor
        [prn("printing", pct=-3, eta=10)],                       # clamped to 0
        [prn("printing", pct=None, eta=10)],                     # no progress: not a candidate
        [prn("paused", pct=50, eta=10), prn("finished", pct=100)],   # only the raw state "printing"
        # offline (statusOf) is skipped even with state "printing"
        [prn("printing", pct=50, eta=100, offline=True), prn("printing", pct=20, eta=1000)],
        [prn("printing", pct=50, eta=100, offline=True)],
        [prn("printing", pct=70, offline=True), prn("printing", pct=30)],
    )) + "]")
    assert got == [47, 0, None, None, 20, None, 30]


def test_attention_is_exactly_the_predicate():
    # tabSummary drops nothing itself: an offline printer the predicate counts is counted
    fleet = [prn(att=True, offline=True), prn("printing", pct=5, eta=9, att=True), prn(att=False), prn("idle", att=True)]
    assert run("tabSummary(%s, st, att).attention" % js(fleet)) == 3
    assert run("tabSummary(null, st, att)") == {"attention": 0, "pct": None}


def test_icon_key():
    p47, p48 = [prn("printing", pct=47, eta=9)], [prn("printing", pct=48, eta=9)]
    got = run("[" + ",".join([
        "key(%s)" % js(p47), "key(%s)" % js(p47),
        "key(%s)" % js(p48),
        "key(%s)" % js(p47 + [PAUSED_ERR]),
        "key(%s, true)" % js(p47 + [PAUSED_ERR]),
        "key([])", "key(%s)" % js([PAUSED_ERR]),
    ]) + "]")
    assert got == ["r47", "r47", "r48", "r47+dot", "base", "base", "base+dot"]


def test_title_count_matches_the_bell():
    # The real predicate of the header bell: fatal / pause count, serious and
    # common do not; an offline printer's status carries no notifications.
    lib = "\n".join(_function(n) for n in ("safeText", "icon", "plural", "cmdLineHtml"))
    lib += "\n" + _block("nf-ui") + "\n" + _block("tab-ui")
    fleet = [
        {"id": "a", "state": "paused", "online": True, "notifications": [{"key": "hms:1", "severity": "pause", "text": "t"}]},
        {"id": "b", "state": "error", "online": True, "notifications": [{"key": "pe:2", "severity": "fatal", "text": "t"}]},
        {"id": "c", "state": "printing", "online": True, "progress_pct": 12, "eta_seconds": 60,
         "notifications": [{"key": "hms:3", "severity": "serious", "text": "t"}]},
        {"id": "d", "state": "idle", "online": True, "notifications": [{"key": "hms:4", "severity": "common", "text": "t"}]},
        {"id": "e", "state": "offline", "online": False, "notifications": []},
        {"id": "f", "state": "paused", "online": True, "notifications": [
            {"key": "hms:5", "severity": "common", "text": "t"}, {"key": "pe:6", "severity": "pause", "text": "t"}]},
    ]
    got = _node(lib + """
var P = %s;
function st(p) { return p.online ? p.state : "offline"; }
var sum = tabSummary(P, st, nfAttention);
console.log(JSON.stringify({sum: sum, title: tabTitle(sum, "Кузница", false), bell: nfBellHtml(P, false)}));
""" % js(fleet))
    bell = re.search(r"⚠ (\d+)</span>", got["bell"])
    assert bell and int(bell.group(1)) == got["sum"]["attention"] == 3
    assert got["title"] == "(3⚠) 12% · Кузница"


def test_block_is_pure():
    block = _block("tab-ui")
    for banned in ("document.", "window.", "fetch(", "localStorage", "cachedAllPrinters",
                   "requestAnimationFrame", "setInterval", "setTimeout", "navigator"):
        assert banned not in block, banned
    # function declarations only at its top level (the IIFE's init runs load()
    # before the vars further down are assigned)
    top = [line for line in block.splitlines() if re.match(r"      \S", line) and not line.strip().startswith("//")]
    assert top and all(line.startswith("      function ") or line == "      }" for line in top), top


def test_block_sits_above_load():
    assert JS.index("/* tab-ui:begin */") < JS.index("async function load()")


def _load_body():
    start = JS.index("async function load()")
    return JS[start:JS.index("viewTabs.forEach", start)]


def test_load_updates_the_tab_on_success_and_on_failure():
    body = _load_body()
    ok, fail = body.split("} catch (error) {", 1)
    assert "updateTabStatus(cachedAllPrinters, false)" in ok
    assert ok.index("applyAndRender();") < ok.index("updateTabStatus(")
    assert "tabState.lastOkAt = Date.now()" in ok
    assert re.search(r"updateTabStatus\(cachedAllPrinters, Date\.now\(\) - tabState\.lastOkAt > 30000\)", fail)


def test_wrapper_uses_the_bell_predicate_and_draws_only_on_change():
    wrapper = JS[JS.index("/* tab-ui:end */"):JS.index("async function load()")]
    # the same predicate as the header bell, without a wrapper
    assert "tabSummary(printers, computeUiStatus, nfAttention)" in wrapper
    assert "if (document.title !== t) document.title = t;" in wrapper
    # the canvas is drawn only when the key changes, the ring at most once per 10 s
    assert "if (key === st.key) return;" in wrapper
    assert "10000" in wrapper
    assert wrapper.count("toDataURL(") == 1
    assert "(display-mode: standalone)" in wrapper and "setAppBadge" in wrapper
    for banned in ("requestAnimationFrame", "setInterval", "setTimeout", "animation:", ".animate(", "@keyframes"):
        assert banned not in wrapper, banned



# --- the header's status chip (IA §4, plan step 2) ---------------------------------------------

def test_live_chip_words_only_on_a_failure():
    got = run("[" + ",".join([
        'liveChip("ok", "23:48:05", 0)', 'liveChip("ok", "", 0)',
        'liveChip("late", "23:48:05", 4)', 'liveChip("late", "", 4)',
        'liveChip("down", "23:48:05", 31.7)', 'liveChip("down", "23:48:05", 125)',
        'liveChip("down", "23:48:05", 7300)', 'liveChip("down", "", 40)', 'liveChip("down", "23:48:05", -5)',
    ]) + "]")
    assert got == [
        {"cls": "", "text": "23:48:05", "tip": "Данные с принтеров получены в 23:48:05"},
        {"cls": "", "text": "—", "tip": "Данные с принтеров ещё не получены"},
        {"cls": "late", "text": "23:48:05", "tip": "Сервер не ответил, повторяем · данные от 23:48:05"},
        {"cls": "late", "text": "—", "tip": "Сервер не ответил, повторяем"},
        {"cls": "down", "text": "нет связи · 31 с", "tip": "Нет связи с сервером · данные от 23:48:05"},
        {"cls": "down", "text": "нет связи · 2 мин", "tip": "Нет связи с сервером · данные от 23:48:05"},
        {"cls": "down", "text": "нет связи · 2 ч", "tip": "Нет связи с сервером · данные от 23:48:05"},
        {"cls": "down", "text": "нет связи · 40 с", "tip": "Нет связи с сервером"},
        {"cls": "down", "text": "нет связи · 0 с", "tip": "Нет связи с сервером · данные от 23:48:05"},
    ]


def test_live_chip_says_down_exactly_when_the_title_does():
    wrapper = JS[JS.index("/* tab-ui:end */"):JS.index("async function load()")]
    assert "tabState.stale = !!stale;" in wrapper[wrapper.index("function updateTabStatus("):]
    chip = wrapper[wrapper.index("function renderLiveChip(ok) {"):]
    assert 'liveChip(ok ? "ok" : tabState.stale ? "down" : "late", tabState.okClock || "", ' \
           '(Date.now() - tabState.lastOkAt) / 1000)' in chip
    assert "lastUpdate.textContent = v.text;" in chip and 'el.setAttribute("data-tip", v.tip);' in chip
    body = _load_body()
    ok, fail = body.split("} catch (error) {", 1)
    assert "renderLiveChip(true);" in ok and ok.index("tabState.lastOkAt = Date.now()") < ok.index("renderLiveChip(true)")
    # the failure: the title's verdict first, the chip shows it
    assert fail.index("updateTabStatus(cachedAllPrinters,") < fail.index("renderLiveChip(false);")
    assert "lastUpdate.classList" not in body
