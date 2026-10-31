"""The farm's row over the printers' grid of app/web/app.js (IA §5, plan step 6), run in node.

The chips that are the filters (one dictionary of states, IA §10), the saved choice
taken over from the old «Фильтры ▾» (forge-ops-state-filters without «Онлайн»), the
order as a segment (forge-ops-sort kept, forge-ops-alerts-top removed), the farm's
commands, and «Не в работе · N» (maintenance, offline a day and more) with the
state-change times it needs. The pure helpers live in the ctl-ui block and are run
with its own helpers (tests/test_web_ctl_ui._lib); the DOM side with stubs.
Skipped without node.
"""
import json
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

from tests.test_web_ctl_ui import ALL_CAPS, _decl, _lib, _node, _p, run

ROOT = Path(__file__).resolve().parents[1]
JS = (ROOT / "app" / "web" / "app.js").read_text(encoding="utf-8")
HTML = (ROOT / "app" / "web" / "index.html").read_text(encoding="utf-8")
CSS = (ROOT / "app" / "web" / "styles.css").read_text(encoding="utf-8")
API = (ROOT / "app" / "api" / "main.py").read_text(encoding="utf-8")

NOW = 1_800_000_000
DAY = 86400
MAINT = {"since": 1_799_000_000.0, "note": "меняю <хотенд>", "by": "admin"}


def js(v):
    return json.dumps(v, ensure_ascii=False)


def P(pid, state="printing", **kw):
    d = {"id": pid, "label": kw.pop("label", pid), "state": state, "online": state != "offline", "progress_pct": 10}
    d.update(kw)
    return d


# --- the dictionary and the chips ---------------------------------------------------------------

def test_the_chips_are_the_one_dictionary():
    chips = run("FLEET_CHIPS")
    assert [(c["id"], c["label"]) for c in chips] == [
        ("printing", "Печатает"), ("paused", "Пауза"), ("error", "Ошибка"), ("finished", "Завершено"),
        ("cancelled", "Отменено"), ("idle", "Простой"), ("offline", "Офлайн"), ("maintenance", "Обслуживание")]
    assert [c.get("short") for c in chips] == [None] * 7 + ["Обсл."]
    # the card's badge says the same word, in capitals (IA §10)
    words = _node(_decl("statusLabel") + "\nconsole.log(JSON.stringify(['printing', 'paused', 'error', 'finished',"
                  " 'cancelled', 'idle', 'offline', 'unknown'].map(statusLabel)));")
    assert words == ["ПЕЧАТАЕТ", "ПАУЗА", "ОШИБКА", "ЗАВЕРШЕНО", "ОТМЕНЕНО", "ПРОСТОЙ", "ОФЛАЙН", "НЕТ ДАННЫХ"]
    assert [w.capitalize() for w in words[:7]] == [c["label"] for c in chips[:7]]


def test_a_printer_s_chip():
    cases = [
        (P("a", "printing"), "printing"), (P("b", "paused"), "paused"), (P("c", "error"), "error"),
        (P("d", "finished"), "finished"), (P("e", "cancelled"), "cancelled"), (P("f", "idle"), "idle"),
        (P("g", "offline"), "offline"),
        (P("h", "printing", online=False), "offline"),                         # not online: offline, as its card
        (P("i", "unknown", progress_pct=None), "offline"),                     # unknown without telemetry
        (P("j", "unknown"), "idle"),                                           # unknown with telemetry: no word
        (P("k", "offline", maintenance=MAINT), "maintenance"),                 # maintenance first, any state
        (P("l", "printing", maintenance=MAINT), "maintenance"),
        (P("m", "maintenance"), "idle"),                                       # never a live state
    ]
    got = run("[" + ",".join("fleetBucket(%s)" % js(p) for p, _ in cases) + "]")
    assert got == [want for _, want in cases]


def test_counts_and_chips():
    farm = [P("a"), P("b"), P("c", "paused"), P("d", "offline"), P("e", "offline", maintenance=MAINT), P("f", "unknown")]
    counts = run("fleetCounts(%s)" % js(farm))
    assert counts == {"printing": 2, "paused": 1, "error": 0, "finished": 0, "cancelled": 0, "idle": 1, "offline": 1,
                      "maintenance": 1}
    assert run("fleetCounts([])") == dict.fromkeys(counts, 0)
    html = run("fleetChipsHtml(%s, ['paused'])" % js(counts))
    chips = re.findall(r'<button type="button" class="farm-chip st-(\w+)([^"]*)" data-filter="(\w+)" aria-pressed="(\w+)" '
                       r'data-tip="([^"]*)">(.*?)</button>', html)
    assert [c[0] for c in chips] == [c[2] for c in chips] == ["printing", "paused", "error", "finished", "cancelled", "idle",
                                                              "offline", "maintenance"]
    cls = {c[0]: c[1] for c in chips}
    assert cls["paused"] == " on" and cls["error"] == " zero" and cls["printing"] == ""   # zero chips stay, dim
    assert {c[0]: c[3] for c in chips}["paused"] == "true" and {c[0]: c[3] for c in chips}["idle"] == "false"
    tips = {c[0]: c[4] for c in chips}
    assert tips["paused"] == "Повторный клик — все принтеры"
    assert tips["printing"] == "Показать только «Печатает» · Ctrl+клик — добавить к выбору"
    body = {c[0]: c[5] for c in chips}
    assert body["printing"] == '<i class="farm-dot" aria-hidden="true"></i><span class="fc-l">Печатает</span> <b>2</b>'
    assert body["paused"].endswith('<b>1</b><span class="farm-x" aria-hidden="true">×</span>')   # the chosen: ×
    assert '<span class="fc-l">Обслуживание</span><span class="fc-s">Обсл.</span> <b>1</b>' in body["maintenance"]
    two = run("fleetChipsHtml(%s, ['paused', 'idle'])" % js(counts))
    assert "Ctrl+клик — убрать из выбора · клик — только «Пауза»" in two


def test_a_chip_click():
    cases = [
        ([], "paused", False, ["paused"]),
        (["paused"], "paused", False, []),                      # the same chip again: everyone
        (["paused"], "error", False, ["error"]),                # another: only that one
        (["paused", "error"], "paused", False, ["paused"]),     # one of several: only it
        (["paused"], "error", True, ["paused", "error"]),       # Ctrl: added
        (["paused", "error"], "paused", True, ["error"]),       # Ctrl: taken away
        (["paused"], "paused", True, []),
        (["paused"], "online", False, ["paused"]),              # not a chip: nothing changes
        (None, "idle", False, ["idle"]),
    ]
    got = run("[" + ",".join("fleetFilterClick(%s, %s, %s)" % (js(f), js(i), js(a)) for f, i, a, _ in cases) + "]")
    assert got == [want for *_, want in cases]


# --- the saved choice (IA plan step 6: «выбросить online, остальное взять») -----------------------

def test_the_saved_filters_are_taken_over():
    every_old = ["online", "offline", "printing", "idle", "error", "paused", "maintenance"]   # the old «Фильтры ▾» ids
    assert run("fleetFiltersFrom(%s)" % js(every_old)) == ["offline", "printing", "idle", "error", "paused", "maintenance"]
    cases = [(["online"], []), (["idle", "idle", "bogus", 3, None, "finished"], ["idle", "finished"]), (None, []),
             ("printing", []), ({"printing": 1}, []), ([], []), (["__proto__", "constructor"], [])]
    got = run("[" + ",".join("fleetFiltersFrom(%s)" % js(v) for v, _ in cases) + "]")
    assert got == [want for _, want in cases]
    # idempotent: a choice saved by today's page reads back as it is
    assert run("fleetFiltersFrom(fleetFiltersFrom(%s))" % js(every_old)) == run("fleetFiltersFrom(%s)" % js(every_old))
    # one of the old «Фильтры ▾» (old): its «Простаивают» held the finished and the cancelled too — today's
    # «Простой», «Завершено» and «Отменено»; today's «Простой» (not old) is only itself
    assert run("fleetFiltersFrom(%s, true)" % js(every_old)) == [
        "offline", "printing", "idle", "finished", "cancelled", "error", "paused", "maintenance"]
    olds = [(["idle"], ["idle", "finished", "cancelled"]), (["online", "paused"], ["paused"]),
            (["finished", "idle"], ["finished", "idle", "cancelled"]), (None, []), ("idle", [])]
    assert run("[" + ",".join("fleetFiltersFrom(%s, true)" % js(v) for v, _ in olds) + "]") == [w for _, w in olds]
    assert run("fleetFiltersFrom(['idle'])") == ["idle"]


STORE_HARNESS = r"""
var calls = [], store = %(store)s;
var localStorage = {
  getItem: function (k) { return Object.prototype.hasOwnProperty.call(store, k) ? store[k] : null; },
  removeItem: function (k) { calls.push(["remove", k]); delete store[k]; },
  setItem: function (k, v) { calls.push(["set", k, String(v)]); store[k] = String(v); }
};
var console = { warn: function () {}, log: function (s) { process.stdout.write(s + "\n"); } };
var FILTERS_STORAGE_KEY = "forge-ops-state-filters", SORT_STORAGE_KEY = "forge-ops-sort";
var FILTERS_FORMAT_KEY = "forge-ops-state-filters-v", FILTERS_FORMAT = "2";
var fleetFilter = [], currentSort = "name", sortSeg = null;
%(lib)s
loadPersistedFilters();
loadSortPrefs();
var farm = %(farm)s;
console.log(JSON.stringify({ calls: calls, store: store, filter: fleetFilter, sort: currentSort,
                             shown: applyFilters(farm).map(function (p) { return p.id; }) }));
"""
STORE_FARM = [P("pr", "printing"), P("fin", "finished"), P("cnc", "cancelled"), P("idl", "idle"), P("unk", "unknown"),
        P("off", "offline")]


def _start(store):
    lib = "\n".join([_lib()] + [_decl(n) for n in ("loadPersistedFilters", "persistFilters", "loadSortPrefs",
                                                    "renderSortSeg", "applyFilters")])
    return _node(STORE_HARNESS % {"store": js(store), "lib": lib, "farm": js(STORE_FARM)})


def test_the_old_keys_at_start():
    # the owner's Chrome and Edge reload after the deploy with whatever the old page kept
    out = _start({"forge-ops-state-filters": '["online","paused","maintenance"]', "forge-ops-sort": "eta",
                  "forge-ops-alerts-top": "false"})
    assert out["filter"] == ["paused", "maintenance"] and out["sort"] == "eta"      # «по времени» stays
    assert out["store"] == {"forge-ops-state-filters": '["paused","maintenance"]', "forge-ops-state-filters-v": "2",
                            "forge-ops-sort": "eta"}
    assert ["remove", "forge-ops-alerts-top"] in out["calls"]
    # the old «Простаивают» (idle, finished, cancelled, unknown): the cards waiting to be unloaded stay
    # in the grid after the reload — mapped once and marked, the next start reads it as today's
    out = _start({"forge-ops-state-filters": '["idle"]', "forge-ops-sort": "name"})
    assert out["filter"] == ["idle", "finished", "cancelled"] and out["shown"] == ["fin", "cnc", "idl", "unk"]
    assert out["store"] == {"forge-ops-state-filters": '["idle","finished","cancelled"]',
                            "forge-ops-state-filters-v": "2", "forge-ops-sort": "name"}
    again = _start(out["store"])
    assert again["filter"] == ["idle", "finished", "cancelled"] and not [c for c in again["calls"] if c[0] == "set"]
    # a choice of today's page (marked): read as it is, nothing written — its «Простой» is only itself
    out = _start({"forge-ops-state-filters": '["finished"]', "forge-ops-state-filters-v": "2", "forge-ops-sort": "name"})
    assert out["filter"] == ["finished"] and out["sort"] == "name" and out["shown"] == ["fin"]
    assert not [c for c in out["calls"] if c[0] == "set"]
    out = _start({"forge-ops-state-filters": '["idle"]', "forge-ops-state-filters-v": "2"})
    assert out["filter"] == ["idle"] and out["shown"] == ["idl", "unk"] and not [c for c in out["calls"] if c[0] == "set"]
    # garbage and nothing at all
    out = _start({"forge-ops-state-filters": "{oops", "forge-ops-sort": "random"})
    assert out["filter"] == [] and out["sort"] == "name" and out["store"]["forge-ops-state-filters"] == "[]"
    out = _start({})
    assert out["filter"] == [] and out["sort"] == "name" and out["store"] == {}
    assert not [c for c in out["calls"] if c[0] == "set"]


def test_the_keys_are_written_by_the_new_controls_only():
    assert len(re.findall(r'localStorage\.removeItem\("forge-ops-alerts-top"\)', JS)) == 1
    assert "ALERTS_TOP_KEY" not in JS and not re.search(r"setItem\([^)]*alerts-top", JS)
    assert JS.count("localStorage.setItem(FILTERS_STORAGE_KEY") == 1 and 'FILTERS_STORAGE_KEY = "forge-ops-state-filters"' in JS
    # the format mark (the harness above uses the same values) is written with the choice, nowhere else
    assert 'const FILTERS_FORMAT_KEY = "forge-ops-state-filters-v";' in JS and 'const FILTERS_FORMAT = "2";' in JS
    assert JS.count("localStorage.setItem(FILTERS_FORMAT_KEY") == 1
    persist = _decl("persistFilters")
    assert persist.index("setItem(FILTERS_STORAGE_KEY") < persist.index("setItem(FILTERS_FORMAT_KEY, FILTERS_FORMAT)")
    assert JS.count("localStorage.setItem(SORT_STORAGE_KEY, currentSort)") == 1
    seg = JS[JS.index("if (sortSeg) {"):JS.index("// A chip (IA §5)")]
    assert 'currentSort = b.dataset.sort === "eta" ? "eta" : "name";' in seg and "applyAndRender();" in seg


# --- how long a printer has stood in its state ------------------------------------------------------

def ev(pid, new_state, t):
    return {"printer_id": pid, "new_state": new_state, "old_state": "x", "time": t}


def test_since_from_the_last_day_s_events():
    farm = [P("fresh", "offline"), P("long", "offline"), P("fin", "finished"), P("moved", "offline"),
            P("seen", "offline"), P("pr", "printing"), P(7, "idle")]
    rows = [ev("fresh", "offline", NOW - 3600), ev("fresh", "printing", NOW - 7200),   # newest first: the newest wins
            ev("fin", "finished", NOW - 600), ev("moved", "idle", NOW - 900), ev("seen", "idle", NOW - 900),
            ev("pr", "printing", NOW - 5000), ev(7, "idle", NOW - 60), ev("fresh", "offline", NOW - 80000)]
    had = {"seen": {"s": "offline", "t": NOW - 40}}
    since = run("fleetSinceEvents(%s, %s, %s, %d)" % (js(had), js(farm), js(rows), NOW))
    assert since == {
        "fresh": {"s": "offline", "t": NOW - 3600},
        "long": {"s": "offline", "t": None},               # no change all day: a day and more
        "fin": {"s": "finished", "t": NOW - 600},
        "moved": {"s": "offline", "t": NOW},               # changed after the history's last row
        "seen": {"s": "offline", "t": NOW - 40},           # this page saw it change, the history not yet
        "pr": {"s": "printing", "t": NOW - 5000},
        "7": {"s": "idle", "t": NOW - 60},                 # ids as strings
    }
    # an unknown printer without telemetry is offline for its card, and so for its time
    assert run("fleetStateKey(%s)" % js(P("u", "unknown", progress_pct=None))) == "offline"


def test_since_from_the_polls():
    out = run("""(function () {
      var s = {}, a = %(a)s, b = %(b)s;
      fleetSinceTrack(s, a, %(now)d);
      var first = JSON.parse(JSON.stringify(s));
      fleetSinceTrack(s, b, %(now)d + 30);
      fleetSinceTrack(s, b, %(now)d + 60);
      return {first: first, after: s, ages: b.map(function (p) { return fleetSinceAge(s, p, %(now)d + 90); })};
    })()""" % {"a": js([P("x", "printing"), P("y", "offline")]), "b": js([P("x", "finished"), P("y", "offline")]),
               "now": NOW})
    assert out["first"] == {"x": {"s": "printing"}, "y": {"s": "offline"}}         # not known yet (t undefined)
    assert out["after"] == {"x": {"s": "finished", "t": NOW + 30}, "y": {"s": "offline"}}
    assert out["ages"] == [60, None]
    ages = run("[fleetSinceAge({a: {s: 'offline', t: null}}, %s, %d), fleetSinceAge({a: {s: 'idle', t: 1}}, %s, %d),"
               " fleetSinceAge({a: {s: 'offline', t: %d}}, %s, %d), fleetSinceAge(null, %s, 1)]"
               % (js(P("a", "offline")), NOW, js(P("a", "offline")), NOW, NOW + 10, js(P("a", "offline")), NOW,
                  js(P("a", "offline"))))
    # Infinity is no JSON: null; a state that changed since: unknown; a clock behind: 0
    assert ages == [None, None, 0, None]
    assert run("fleetSinceAge({a: {s: 'offline', t: null}}, %s, 1) === Infinity" % js(P("a", "offline"))) is True


def test_not_at_work():
    since = {"fresh": {"s": "offline", "t": NOW - 3600}, "day": {"s": "offline", "t": NOW - DAY},
             "long": {"s": "offline", "t": None}, "idle": {"s": "idle", "t": None}}
    cases = [(P("fresh", "offline"), False), (P("day", "offline"), True), (P("long", "offline"), True),
             (P("new", "offline"), False),                                   # not known yet: stays in the grid
             (P("idle", "idle"), False), (P("m", "printing", maintenance=MAINT), True),
             (P("long", "printing"), False)]                                 # online again
    got = run("[" + ",".join("fleetNotWorking(%s, %s, %d)" % (js(p), js(since), NOW) for p, _ in cases) + "]")
    assert got == [want for _, want in cases]


def test_durations():
    got = run("[" + ",".join("fleetDur(%s)" % v for v in ("0", "29", "89", "2400", "3600", "3660", "7500", "172740",
                                                          "172800", "300000", "Infinity", "null", "NaN")) + "]")
    assert got == ["1м", "1м", "1м", "40м", "1ч", "1ч 1м", "2ч 5м", "47ч 59м", "2 дн", "3 дн", "> 1 дн", "", ""]


# --- the grid and «Не в работе» ---------------------------------------------------------------

def _split(farm, filt, since, open_=False):
    return run("(function () { var s = fleetSplit(%s, %s, %s, %d, %s); return {work: s.work.map(function (p) { return p.id; }),"
               " rest: s.rest.map(function (p) { return p.id; }), open: s.open, shown: s.shown.map(function (p) { return p.id; })}; })()"
               % (js(farm), js(filt), js(since), NOW, js(open_)))


FARM = [P("P10", "printing"), P("P2", "paused"), P("M1", "idle", maintenance=MAINT), P("K1", "offline"),
        P("E5", "offline"), P("Reborn 2", "offline", maintenance=MAINT)]
SINCE = {"K1": {"s": "offline", "t": None}, "E5": {"s": "offline", "t": NOW - 600}}


def test_the_grid_and_the_rest():
    s = _split(FARM, [], SINCE)
    # the grid keeps the given order (sortPrinters); the rest by name, natural; folded by default
    assert s == {"work": ["P10", "P2", "E5"], "rest": ["K1", "M1", "Reborn 2"], "open": False, "shown": []}
    assert _split(FARM, [], SINCE, True)["shown"] == ["K1", "M1", "Reborn 2"]
    # a chip filters the grid; «Не в работе» stays one line with every printer it has
    s = _split(FARM, ["printing"], SINCE)
    assert s["work"] == ["P10"] and s["rest"] == ["K1", "M1", "Reborn 2"] and not s["open"]
    # «Обслуживание» unfolds it with its cards; so does a filter only its cards answer
    s = _split(FARM, ["maintenance"], SINCE)
    assert s["work"] == [] and s["open"] and s["shown"] == ["M1", "Reborn 2"]
    s = _split(FARM, ["offline"], SINCE)
    assert s["work"] == ["E5"] and not s["open"]                     # a fresh drop answers in the grid
    s = _split(FARM, ["offline"], SINCE, True)
    assert s["shown"] == ["K1"]
    s = _split([P("K1", "offline")], ["offline"], SINCE)
    assert s["work"] == [] and s["open"] and s["shown"] == ["K1"]
    # unfolded by hand with a filter none of its cards answers: all of them (the mockup)
    assert _split(FARM, ["printing"], SINCE, True)["shown"] == ["K1", "M1", "Reborn 2"]


def test_the_rest_line():
    rest = [P("K1", "offline"), P("E5", "offline"), P("<M>", "idle", maintenance=MAINT)]
    since = {"K1": {"s": "offline", "t": None}, "E5": {"s": "offline", "t": NOW - 3 * DAY}}
    html = run("fleetRestHtml(%s, false, %s, %d)" % (js(rest), js(since), NOW))
    assert html.startswith('<div class="fleet-rest" role="group" aria-label="Не в работе"><b class="fr-title">Не в работе · 3</b>')
    assert '<span class="fr-mini" data-tip="Офлайн больше суток">K1 · офлайн &gt; 1 дн</span>' in html
    assert '<span class="fr-mini" data-tip="Офлайн больше суток">E5 · офлайн 3 дн</span>' in html
    # maintenance: the name and 🔧, the whole story (escaped) only in the tooltip
    m = re.search(r'<span class="fr-mini" data-tip="([^"]*)">&lt;M&gt;<svg class="pc-ic" aria-hidden="true"><use href="#i-tool"></use></svg></span>', html)
    assert m and m.group(1).startswith("На обслуживании с ") and m.group(1).endswith("· поставил admin · меняю &lt;хотенд&gt;")
    assert "меняю" not in html.replace(m.group(1), "")
    assert html.endswith('<span class="fr-sep"></span><button type="button" class="bd-link fr-toggle" data-rest-toggle '
                         'aria-expanded="false">Показать</button></div>')
    assert 'aria-expanded="true">Скрыть</button>' in run("fleetRestHtml(%s, true, %s, %d)" % (js(rest), js(since), NOW))
    assert run("fleetRestHtml([], true, {}, 1)") == "" and run("fleetRestHtml(null, true, {}, 1)") == ""


def test_the_empty_line():
    assert run("fleetEmptyHtml(true, true)") == (
        '<div class="empty-state fleet-empty">Нет принтеров с таким состоянием · '
        '<button type="button" class="bd-link" data-filter-clear>Показать все</button></div>')
    assert run("fleetEmptyHtml(false, true)") == '<div class="empty-state fleet-empty">Сейчас в работе нет ни одного принтера</div>'
    assert run("fleetEmptyHtml(false, false)") == '<div class="empty-state fleet-empty">Принтеров пока нет</div>'


def test_the_block_stays_pure():
    for name in ("fleetBucket", "fleetCounts", "fleetFiltersFrom", "fleetFilterClick", "fleetChipsHtml", "fleetSinceEvents",
                 "fleetSinceTrack", "fleetSinceAge", "fleetDur", "fleetNotWorking", "fleetSplit", "fleetRestHtml",
                 "fleetEmptyHtml", "fleetBulkHtml"):
        body = _decl(name)
        start = JS.index("/* ctl-ui:begin */")
        assert start < JS.index(body) < JS.index("/* ctl-ui:end */"), name
        for banned in ("document.", "window.", "localStorage", "fetch(", "Date.now"):
            assert banned not in body, (name, banned)


# --- the DOM side -------------------------------------------------------------------------------

def test_render_wires_the_split():
    render = _decl("applyAndRender")
    assert "fleetSinceApply(printers, now);" in render
    assert "var split = fleetSplit(sortPrinters(printers), fleetFilter, fleetSince, now, fleetRestOpen);" in render
    assert render.index("fleetSinceApply(") < render.index("fleetSplit(") < render.index("renderFleet(")
    assert "renderFilters();" in render and "renderFleetBulk();" in render
    items = _decl("fleetItems")
    # in #printerGrid, in this order: the cards at work, the empty line, «Не в работе», its cards
    assert items.index("split.work.map(") < items.index('key: "empty"') < items.index('key: "rest"') < items.index("split.shown.forEach(")
    assert "fleetRestOpen = false" in JS and "sessionStorage" not in _decl("fleetItems")
    fleet = _decl("renderFleet")
    assert "if (el && el.parentNode !== grid) el = null;" in fleet and "balanceChips(freshCards);" in fleet
    assert "Array.from(grid.children).forEach(function (el) { if (!keep.has(el)) el.remove(); });" in fleet


def test_the_events_feed_the_times_within_the_api_bounds():
    # verify-every-tab-not-just-curl: the page size must be inside the endpoint's Query(le=…)
    m = re.search(r'def get_events\(fr: float, to: float = None,\s*limit: int = Query\(\d+, ge=1, le=(\d+)\)', API)
    assert m, "get_events signature changed"
    page = int(re.search(r"var FLEET_EV_PAGE = (\d+)", JS).group(1))
    assert 1 <= page <= int(m.group(1))
    load = _decl("fleetLoadSince")
    assert '"/api/history/events?fr=" + fr + "&to=" + to + "&limit=" + FLEET_EV_PAGE' in load
    assert "fr = to - FLEET_DAY_S" in load and "if (!data || !data.has_more) break;" in load
    assert "fleetEv = { rows: rows, at: to, used: false };" in load
    # a failure keeps what was known
    assert "catch (e)" in load and "fleetSince =" not in load
    apply_ = _decl("fleetSinceApply")
    assert apply_.index("fleetSinceEvents(") < apply_.index("fleetSinceTrack(")
    # the first paint waits for them a moment; then every 5 min and back in view
    first = _decl("load")
    assert first.index("if (!fleetPainted) {") < first.index("cachedAllPrinters = data.map(")
    assert "setTimeout(r, 1500)" in first
    assert "setInterval(fleetLoadSince, FLEET_EV_EVERY_MS);" in JS and "FLEET_EV_EVERY_MS = 5 * 60 * 1000" in JS


def test_the_go_to_card_unfolds_the_rest():
    go = _decl("nfGoto")
    assert "if (gp && fleetNotWorking(gp, fleetSince, Date.now() / 1000)) fleetRestOpen = true;" in go
    assert go.index("fleetRestOpen = true") < go.index("applyAndRender();")
    # a card the chips hide still says so (the chips are on screen, their × undoes it)
    assert 'showToast("Карточка скрыта фильтром — снимите фильтры", false)' in go


def test_the_grid_s_own_buttons():
    click = JS[JS.index('var t = e.target.closest("[data-rest-toggle],[data-filter-clear]");'):JS.index("(function initPrinterControls() {")]
    assert 'if (t.getAttribute("aria-expanded") === "true") {' in click
    assert 'fleetFilter = fleetFilter.filter(function (k) { return k !== "offline" && k !== "maintenance"; });' in click
    assert "persistFilters();" in click and "applyAndRender();" in click and "focus({ preventScroll: true })" in click
    chips = JS[JS.index("if (filtersEl) {"):JS.index("function initScrollTop() {")]
    assert "fleetFilterClick(fleetFilter, id, e.ctrlKey || e.metaKey)" in chips
    assert '(id === "offline" || id === "maintenance")) fleetRestOpen = true;' in chips


# --- the markup ---------------------------------------------------------------------------------

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


def test_the_farm_row_markup():
    fleet = HTML[HTML.index('data-panel="fleet"'):HTML.index('data-panel="history"')]
    assert fleet.index('id="fleetFarm"') < fleet.index('id="filters"') < fleet.index('id="sortSeg"') \
        < fleet.index('id="fleetBulk"') < fleet.index('id="printerGrid"')
    seg = [a for t, a in tags(fleet[fleet.index('id="sortSeg"'):]) if "data-sort" in a]
    assert [(a["data-sort"], a["aria-pressed"]) for a in seg] == [("name", "true"), ("eta", "false")]
    assert all(a.get("data-tip") and "title" not in a for a in seg)
    assert ">Имя</button>" in fleet and ">Финиш</button>" in fleet and ">Порядок</span>" in fleet
    for gone in ("sortBtn", "sortDropdown", "filtersBtn", "filtersDropdown", "alertsTopToggle", "Сортировка:",
                 "Фильтры по состоянию", "Проблемные сверху", "sort-bar"):
        assert gone not in HTML, gone
    # «Сводка» lost its KPI tiles to the chips (plan step 6) and is gone (plan step 8)
    assert 'data-panel="dashboard"' not in HTML and "data-kpi-filter" not in HTML and "initKpiClicks" not in JS
    for gone in ("kpiTotal", "kpiOnline", "kpiPrinting", "kpiPaused", "kpiError", "kpiOffline", "kpiMaint"):
        assert gone not in HTML and gone not in JS, gone
    assert "bindTips(document);" in JS          # one binding for the page (plan step 14), the farm row in it


def test_nothing_calls_the_removed_controls():
    # a leftover call throws at run time (node --check does not see it): «Фильтры ▾», «Сортировка ▾»,
    # the switch and the KPI tiles are gone with every name they had
    for gone in ("closeFiltersPopup", "openFiltersPopup", "toggleFiltersPopup", "closeSortPopup", "sortDropdown",
                 "sortBtnLabel", "filtersBtnBadge", "uiStatusFilters", "stateFilter", "lastFilteredPrinters", "kpiMaintBtn"):
        assert not re.search(r"\b%s\b" % gone, JS), gone


def test_the_farm_row_styles():
    def rule(sel):
        m = re.search(r"\n    %s \{([^}]*)\}" % re.escape(sel), CSS)
        assert m, sel
        return m.group(1)
    on = rule(".farm-chip.on")
    assert "background-color: var(--bg-elev);" in on and "linear-gradient(var(--info-soft), var(--info-soft))" in on
    assert "opacity: 0.45" in rule(".farm-chip.zero")
    assert "grid-column: 1 / -1" in rule(".fleet-rest") and "dashed" in rule(".fleet-rest")
    assert "grid-column: 1 / -1" in rule(".fleet-empty")
    seg = rule(".farm-seg button.active")
    assert "background-color: var(--bg-elev);" in seg
    for st, tok in (("printing", "--info"), ("paused", "--warn"), ("error", "--danger"), ("finished", "--ok"),
                    ("cancelled", "--gray-9"), ("idle", "--steel"), ("offline", "--gray-7"), ("maintenance", "--warn-7")):
        assert ".farm-chip.st-%s { --st: var(%s); }" % (st, tok) in CSS, st
    # ≥ 1600 px the chips' row and the commands are one line; on a phone only the non-zero chips, short
    wide = CSS[CSS.index("@media (min-width: 1600px) {\n      .farm-top"):]
    assert ".farm-top { display: flex;" in wide[:200]
    phone = CSS[CSS.index("@media (max-width: 899px) {\n      /* a phone: the chips"):]
    phone = phone[:phone.index("\n    }\n")]
    assert ".farm-chip.zero { display: none; }" in phone and ".farm-chip.st-maintenance .fc-s { display: inline; }" in phone
    for gone in (".filters-btn", ".alerts-toggle", ".filter-btn", ".sort-bar", "#fleetBulk { display: contents; }"):
        assert gone not in CSS, gone
    # the printer pickers of «История» are one <select> in its head now (plan step 8, deliberately changed)
    assert ".sort-option" not in CSS and ".sort-dropdown" not in CSS and "sort-dropdown" not in HTML


def test_the_farm_row_styles_stay_on_the_row():
    # plan step 16 (the matrix): the bell's popover is «nf-pop farm» too — a bare «.farm» rule of the
    # row made it a grid and lifted the phone's sheet 10 px off the bottom (802 of 812)
    assert 'var cls = "nf-pop" + (nfPop.type === "farm" ? " farm" : "");' in JS
    bare = []
    for sels in re.findall(r"([^{}]+)\{", re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)):
        for sel in sels.split(","):
            sel = sel.strip()
            last = re.split(r"\s*[>+~]\s*|\s+", sel)[-1]
            if re.search(r"\.farm(?![-\w])", last) and ".nf-pop" not in last and not sel.startswith(".fleet-section > .farm"):
                bare.append(sel)
    assert bare == []
    assert "margin-bottom: 12px" in CSS[CSS.index(".fleet-section > .farm {"):][:120]


def test_cache_bust_bumped():
    assert int(re.search(r"styles\.css\?v=(\d+)", HTML).group(1)) >= 91
    assert int(re.search(r"app\.js\?v=(\d+)", HTML).group(1)) >= 108


def test_the_farm_s_commands_only_for_admins():
    assert _decl("renderFleetBulk").count('isAdmin() ? fleetBulkHtml(cachedAllPrinters, bulkUi) : ""') == 1
    html = run("fleetBulkHtml(%s, {})" % js([_p(caps=dict(ALL_CAPS), state="paused", stage="paused_user")]))
    assert "Продолжить" in html



# --- «Финиши» (IA §5, plan step 7) -----------------------------------------------------------------

def _clock(t):
    import time
    return time.strftime("%H:%M", time.localtime(t))


def _fin(farm, since=None):
    return run("(function () { var f = fleetFinishes(%s, %s, %d); return {ready: f.ready.map(function (x) { return [x.p.id, x.age]; }),"
               " next: f.next.map(function (p) { return p.id; }), noEta: f.noEta.map(function (p) { return p.id; })}; })()"
               % (js(farm), js(since or {}), NOW))


FIN_FARM = [P("P2", "finished"), P("H3", "finished"), P("K1", "finished"), P("Q", "finished", online=False),
            P("M", "finished", maintenance=MAINT), P("P10", "printing", eta_seconds=600), P("P3", "printing", eta_seconds=60),
            P("P4", "printing", eta_seconds=60), P("H4", "printing", eta_seconds=None), P("A", "printing", eta_seconds=0),
            P("Z", "paused", eta_seconds=30), P("MP", "printing", eta_seconds=10, maintenance=MAINT)]
FIN_SINCE = {"P2": {"s": "finished", "t": NOW - 2400}, "H3": {"s": "finished", "t": None}}


def test_finishes():
    f = _fin(FIN_FARM, FIN_SINCE)
    # ready: finished, online, not in maintenance; the longest standing first, an unknown time last
    assert f["ready"] == [["H3", None], ["P2", 2400], ["K1", None]]          # Infinity is no JSON: null
    assert run("fleetFinishes(%s, %s, %d).ready[0].age === Infinity" % (js(FIN_FARM), js(FIN_SINCE), NOW)) is True
    # next: the prints with an ETA, the nearest first (then by name); no pause, no maintenance
    assert f["next"] == ["P3", "P4", "P10"]
    assert f["noEta"] == ["A", "H4"]
    assert _fin([]) == {"ready": [], "next": [], "noEta": []}


def test_the_finish_clock():
    for t in (NOW, NOW + 3600 * 5 + 61, NOW + 40000):
        assert run("finClock(%d)" % t) == _clock(t)


def _row(farm, since=None, **o):
    opts = dict({"open": False, "hours": 12, "phone": False, "now": NOW}, **o)
    return run("finishRowHtml(fleetFinishes(%s, %s, %d), %s)" % (js(farm), js(since or {}), NOW, js(opts)))


def test_the_finish_row():
    html = _row(FIN_FARM, FIN_SINCE)
    assert html.startswith('<div class="fin-row"><span class="fin-it"><span class="fin-lbl">Готовы:</span> ')
    assert ('<b class="fin-ready" data-tip="Завершено — снять пластину · готово больше суток">H3</b> · &gt; 1 дн, '
            '<b class="fin-ready" data-tip="Завершено — снять пластину · готово 40м назад">P2</b> · 40м, '
            '<b class="fin-ready" data-tip="Завершено — снять пластину">K1</b></span>') in html
    assert '<span class="fin-it"><span class="fin-lbl">Дальше:</span></span>' in html
    nxt = re.findall(r'<span class="fin-it" data-tip="([^"]*)"><span class="fin-t">([^<]*)</span><b>([^<]*)</b></span>', html)
    assert nxt == [("P3 · через 1м · 10%", _clock(NOW + 60), "P3"), ("P4 · через 1м · 10%", _clock(NOW + 60), "P4"),
                   ("P10 · через 10м · 10%", _clock(NOW + 600), "P10")]
    assert html.endswith('<span class="farm-sep"></span><button type="button" class="bd-link fin-win-btn" data-fin-win '
                         'aria-controls="finishWin" aria-expanded="false">Окно 12 ч ▾</button></div>')
    assert 'aria-expanded="true">Окно 24 ч ▴</button>' in _row(FIN_FARM, FIN_SINCE, open=True, hours=24)
    # five shown, «ещё N» for the rest; nothing to say says so
    many = [P("p%d" % i, "printing", eta_seconds=100 * (i + 1)) for i in range(8)]
    html = _row(many)
    assert html.count('class="fin-t"') == 5 and '<span class="fin-dim">· ещё 3</span>' in html
    assert '<span class="fin-dim">нет — снимать нечего</span>' in html
    assert '<span class="fin-dim">ничего не печатается</span>' in _row([P("x", "idle")])
    assert '<span class="fin-dim">без оценки времени</span>' in _row([P("x", "printing", eta_seconds=None)])
    # names are escaped
    odd = _row([P("x", "finished", label="<b>")])
    assert "&lt;b&gt;" in odd and "<b><b>" not in odd


def test_the_finish_row_on_a_phone():
    html = _row(FIN_FARM, FIN_SINCE, phone=True)
    assert html == ('<button type="button" class="fin-row fin-phone" data-fin-sheet aria-haspopup="dialog" aria-controls="finSheet">'
                    '<span class="fin-it"><span class="fin-lbl">Готовы:</span> <b class="fin-ready">H3, P2, K1</b></span>'
                    '<span class="fin-it"><span class="fin-lbl">Дальше:</span> <b>P3</b> <span class="fin-t">%s</span></span>'
                    '<span class="fin-go" aria-hidden="true">›</span></button>' % _clock(NOW + 60))
    idle = _row([P("x", "idle")], phone=True)
    assert '<span class="fin-dim">нет</span>' in idle and '<span class="fin-dim">—</span>' in idle


def test_the_phone_s_list():
    farm = FIN_FARM + [P("L", "printing", eta_seconds=13 * 3600, progress_pct=None)]
    html = run("finishListHtml(fleetFinishes(%s, %s, %d), 12, %d)" % (js(farm), js(FIN_SINCE), NOW, NOW))
    assert re.findall(r'<div class="fin-grp">([^<]*)</div>', html) == ["Готовы · снять пластину", "Ближайшие 12 ч", "Позже",
                                                                      "Без оценки"]
    lines = re.findall(r'<button type="button" class="fin-li" data-fin-goto="([^"]*)"><span class="fin-t">([^<]*)</span>'
                       r'<b>([^<]*)</b><span class="fin-sub">([^<]*)</span></button>', html)
    assert lines == [
        ("H3", "✓", "H3", "готово больше суток"), ("P2", "✓", "P2", "готово 40м назад"), ("K1", "✓", "K1", "завершено"),
        ("P3", _clock(NOW + 60), "P3", "через 1м · 10%"), ("P4", _clock(NOW + 60), "P4", "через 1м · 10%"),
        ("P10", _clock(NOW + 600), "P10", "через 10м · 10%"),
        ("L", _clock(NOW + 13 * 3600), "L", "через 13ч"),
        ("A", "—", "A", "печатает, время неизвестно · 10%"), ("H4", "—", "H4", "печатает, время неизвестно · 10%")]
    empty = run("finishListHtml(fleetFinishes([], {}, 1), 24, 1)")
    assert empty.count('<div class="fin-li fin-none">—</div>') == 4 and "Ближайшие 24 ч" in empty
    assert 'data-fin-goto="&lt;x&gt;"' in run("finishListHtml(fleetFinishes(%s, {}, 1), 12, 1)" % js([P("<x>", "finished")]))


def test_the_finish_helpers_stay_pure():
    for name in ("fleetFinishes", "finClock", "finishRowHtml", "finishListHtml"):
        body = _decl(name)
        assert JS.index("/* ctl-ui:begin */") < JS.index(body) < JS.index("/* ctl-ui:end */"), name
        for banned in ("document.", "window.", "localStorage", "fetch(", "Date.now"):
            assert banned not in body, (name, banned)


def test_the_window_is_the_old_tab_s_scale():
    fc = _decl("renderForecast")
    # the same prints as before, the details in data-tip (IA §10), not in a native title
    assert 'p.state === "printing" && !inMaint(p)' in fc
    assert "title=" not in fc and "data-tip=\"' + escHtml(tip) + '\"" in fc
    assert '"Без оценки: "' in fc and "Печатают без оценки времени" not in JS
    # a stagger as wide as the labels: never closer than 14% of the axis, wider on a narrow one
    assert "var MIN_GAP_PCT = Math.max(14, (widest * 7 + 16 + 8) / Math.max(area.clientWidth || 0, 300) * 100);" in fc
    # forge-ops-forecast-hours is the horizon, kept as it was
    assert JS.count("localStorage.getItem(FORECAST_HOURS_KEY)") == 1 and JS.count("localStorage.setItem(FORECAST_HOURS_KEY") == 1
    assert 'var FORECAST_HOURS_KEY = "forge-ops-forecast-hours";' in JS


def test_the_finish_wiring():
    render = _decl("applyAndRender")
    assert "renderFinish(now);" in render and 'data-panel="forecast"' not in render
    assert "view === 'forecast'" not in JS and 'data-view="forecast"' not in HTML and 'data-panel="forecast"' not in HTML
    fin = _decl("renderFinish")
    assert "var show = finOpen && !finPhone();" in fin and 'if (show && routeActiveView() === "fleet") renderForecast();' in fin
    assert "if (view === 'fleet') { Promise.resolve().then(function () { if (cachedAllPrinters.length) renderFinish(" in JS
    assert "finishRowHtml(f, { open: finOpen, hours: forecastHours, phone: finPhone(), now: now })" in fin
    setter = _decl("finSetOpen")
    assert "if (!fromAddress) routeSync(false);" in setter               # unfolding is no move: no history entry
    # a phone opens the list sheet instead (plan step 15, deliberately changed: the PWA shortcut «Финиши»;
    # tests/test_web_route_ui.py::test_the_pwa_shortcuts_open_once)
    apply = _decl("routeApply")
    assert 'var finWant = shown.query.finish === "open";' in apply and "finSetOpen(finWant && !finPhone(), true);" in apply
    assert 'if (view === "fleet" && finOpen) r.query.finish = "open";' in _decl("routeNow")
    go = _decl("finGoto")
    assert go.index("finSheetSet(false);") < go.index("fleetRestOpen = true") < go.index("applyAndRender();")
    assert 'window.matchMedia("(max-width: 899px)")' in JS


def test_the_finish_markup():
    farm = HTML[HTML.index('id="fleetFarm"'):HTML.index('id="printerGrid"')]
    assert farm.index('id="fleetBulk"') < farm.index('id="finishRow"') < farm.index('id="finishWin"')
    win = [a for t, a in tags(farm) if a.get("id") == "finishWin"][0]
    assert "hidden" in win and win["class"] == "fin-win"
    hours = [(a["data-hours"], a.get("aria-pressed")) for t, a in tags(farm) if "data-hours" in a]
    assert hours == [("12", "true"), ("24", "false")] and ">Горизонт</span>" in farm
    for need in ('id="forecastArea"', 'id="forecastMeta"', 'id="forecastNoEta"'):
        assert need in farm, need
    sheet = [a for t, a in tags(HTML) if a.get("id") == "finSheet"][0]
    assert sheet["role"] == "dialog" and sheet["aria-modal"] == "true" and "hidden" in sheet and sheet["tabindex"] == "-1"
    assert 'id="finBack" hidden' in HTML and 'data-fin-close aria-label="Закрыть"' in HTML and 'id="finSheetBody"' in HTML
    # «Сводка» is gone (plan step 8): «Загрузка» is a tile of «История → Статистика»
    assert 'data-panel="dashboard"' not in HTML and 'id="metaUtil"' not in HTML and 'id="histStatsKpi"' in HTML


def test_the_finish_styles():
    def rule(sel):
        m = re.search(r"\n    %s \{([^}]*)\}" % re.escape(sel), CSS)
        assert m, sel
        return m.group(1)
    sheet = rule(".fin-sheet")
    assert "background: var(--bg-elev);" in sheet and "position: fixed;" in sheet and "bottom: 0;" in sheet
    assert "z-index: 620" in sheet and "safe-area-inset-bottom" in sheet
    assert "background: var(--bg-elev);" in rule(".fin-sheet-h")
    assert "min-height: 44px" in rule(".fin-li")                         # a finger's target
    assert ".fin-win[hidden] { display: none; }" in CSS and ".fin-back[hidden], .fin-sheet[hidden] { display: none; }" in CSS
    assert ".forecast-controls .time-btn" not in CSS                     # the horizon is a .farm-seg now
    assert "flex-wrap: nowrap" in rule(".fin-row.fin-phone") and "text-overflow: ellipsis" in rule(".fin-phone .fin-it")


def test_cache_bust_bumped_for_the_finishes():
    assert int(re.search(r"styles\.css\?v=(\d+)", HTML).group(1)) >= 92
    assert int(re.search(r"app\.js\?v=(\d+)", HTML).group(1)) >= 109
