"""The screen addresses of app/web/app.js (IA §3, plan step 2), run in node.

The block between /* route:begin */ and /* route:end */ is pure: the map of
the screens, of old names and of every tab forge-ops-active-view ever kept,
and parse / build of #/<screen>[/<sub>][?key=value]. The router's DOM side next
to it is run with stubs: the one-time migration of the saved tab, Back /
Forward, the role check, the sub-tabs of «Админ» (plan step 3) and of «История»
with the period / printer of its one head (plan step 8). Plus the sticky
header and its status chip. Skipped without node.
"""
import json
import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
JS = (ROOT / "app" / "web" / "app.js").read_text(encoding="utf-8")
HTML = (ROOT / "app" / "web" / "index.html").read_text(encoding="utf-8")
CSS = (ROOT / "app" / "web" / "styles.css").read_text(encoding="utf-8")

# Every value forge-ops-active-view ever held: the data-view of every tab since
# the key appeared (git log -S'data-view=' / -S'active-view': 3f3b061, bf18cd5, cd414c3).
SAVED_VIEWS = ["fleet", "overview", "cameras", "dashboard", "history", "ams", "forecast", "admin", "settings"]
DOM_FUNCS = ("routeActiveView", "routeNow", "routeSync", "routeRole", "routeApply", "routeOnHash",
             "routeStart", "routeCheckRole")


def _block(tag):
    m = re.search(r"/\* %s:begin \*/(.*?)/\* %s:end \*/" % (tag, tag), JS, re.S)
    assert m, "%s block not found in app.js" % tag
    return m.group(1)


def _js_function(name):
    """The whole `function name(...) {...}` of app.js: braces matched, strings and // comments skipped."""
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


def _node(src):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    out = subprocess.run([node, "-"], input=src, capture_output=True, text=True, timeout=60, encoding="utf-8")
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def run(expr, prelude=""):
    """Evaluate `expr` (JSON-serializable) against the route block."""
    return _node(_block("route") + prelude + "\nconsole.log(JSON.stringify(" + expr + "));")


def js(v):
    return json.dumps(v, ensure_ascii=False)


def R(screen, sub="", query=None):
    return {"screen": screen, "sub": sub, "query": query or {}}


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


PANELS = {a["data-panel"] for t, a in tags(HTML) if "data-panel" in a}
TABS = {a["data-view"] for t, a in tags(HTML) if "data-view" in a}
HISTORY_SUBS = {a["data-sub"] for t, a in tags(HTML[HTML.index('id="historySub"'):]) if "data-sub" in a}
ADMIN_SUBS = [a["data-asub"] for t, a in tags(HTML) if "data-asub" in a]
ADMIN_PANES = [a["data-asub-panel"] for t, a in tags(HTML) if "data-asub-panel" in a]


# --- the pure block --------------------------------------------------------------------------

def test_block_is_pure():
    block = _block("route")
    for banned in ("document.", "window.", "location", "history.", "localStorage", "fetch(", "setTimeout",
                   "currentUser", "historySub", "setActiveView"):
        assert banned not in block, banned


def test_every_screen_shows_an_existing_panel():
    screens, default = run("[ROUTE_SCREENS, ROUTE_DEFAULT]")
    assert default == "printers" and screens["printers"]["view"] == "fleet"
    for name, conf in screens.items():
        assert conf["view"] in PANELS and conf["view"] in TABS, name
    # no panel is left without an address
    assert {c["view"] for c in screens.values()} == PANELS
    # the subs: the history switch's own names, nothing more — Печати · Хронология · Статистика ·
    # Действия (plan step 8, deliberately changed: the panel's names are the addresses' now)
    assert set(screens["history"]["subs"].values()) == HISTORY_SUBS == {"jobs", "chrono", "stats", "actions"}
    assert screens["history"]["subs"] == {s: s for s in ("jobs", "chrono", "stats", "actions")}
    assert screens["history"]["adminSubs"] == ["actions"]
    # «Сводка» is gone with its panel (plan step 8)
    assert "dashboard" not in screens and "dashboard" not in PANELS and "dashboard" not in TABS
    # «Админ» (plan step 3): Парк · Доступ · Система, each a sub-tab and a pane of its own
    assert ADMIN_SUBS == ADMIN_PANES == ["park", "auto", "access", "system"]     # «Автоматика»: plan step 11
    assert screens["admin"]["subs"] == {s: s for s in ADMIN_SUBS}
    assert "settings" not in screens and "settings" not in PANELS and "settings" not in TABS
    assert [n for n, c in screens.items() if c.get("admin")] == ["admin"]
    # the query keys come with their screens: «Принтеры» keeps the «Финиши» window (plan step 7),
    # «История» its head's period, «Свой…» days and printer (plan step 8; both deliberately
    # changed); the «Окно» tab and its panel are gone. bell=open (plan step 15, deliberately
    # changed): the PWA shortcut «Нужно решение» opens the bell's list once, the address drops it
    assert {n: c["keys"] for n, c in screens.items() if c["keys"]} == {
        "printers": ["finish", "bell"], "history": ["period", "from", "to", "printer"]}
    assert "forecast" not in screens and "forecast" not in PANELS and "forecast" not in TABS


PARSE = [
    ("", R("printers")),
    ("#", R("printers")),
    ("#/", R("printers")),
    (None, R("printers")),
    ("#/printers", R("printers")),
    ("#/ams", R("ams")),
    ("#/history", R("history")),
    ("#/history/jobs", R("history", "jobs")),
    ("#/history/chrono", R("history", "chrono")),
    ("#/history/timeline", R("history", "chrono")),       # the old sub name
    ("#/History/JOBS", R("history", "jobs")),              # case-insensitive
    ("#//history//jobs/", R("history", "jobs")),
    ("history/jobs", R("history", "jobs")),                # without the #
    ("#/history/jobs/extra", R("history", "jobs")),
    ("#/history/stats", R("history", "stats")),            # plan step 8: Статистика and Действия
    ("#/history/actions", R("history", "actions")),        # (admins: routeForRole)
    ("#/history/STATS/x", R("history", "stats")),
    ("#/admin", R("admin")),
    ("#/admin/park", R("admin", "park")),                  # the sub-tabs of «Админ» (plan step 3)
    ("#/admin/access", R("admin", "access")),
    ("#/admin/SYSTEM", R("admin", "system")),
    ("#/admin/auto", R("admin", "auto")),                  # «Автоматика» (plan step 11)
    ("#/admin/rules", R("admin")),
    ("#/admin/__proto__", R("admin")),
    ("#/dashboard", R("history", "stats")),                # «Сводка» → «История → Статистика» (step 8)
    ("#/dashboard/jobs", R("history", "stats")),
    ("#/Dashboard?period=7d", R("history", "stats", {"period": "7d"})),
    ("#/forecast", R("printers", "", {"finish": "open"})),  # «Окно» → the «Финиши» window (plan step 7)
    ("#/forecast?finish=x", R("printers", "", {"finish": "x"})),   # what the address says wins
    ("#/Forecast/sub", R("printers", "", {"finish": "open"})),
    ("#/settings", R("printers")),                         # gone in plan step 3: 👤 and «Админ → Система»
    ("#/settings/x", R("printers")),
    ("#/fleet", R("printers")),                            # old tab names
    ("#/overview", R("printers")),
    ("#/cameras", R("printers")),
    ("#/bogus", R("printers")),
    ("#/bogus/jobs", R("printers")),
    ("#/printers/jobs", R("printers")),                    # a sub of another screen
    ("#/__proto__", R("printers")),                        # no prototype keys
    ("#/constructor", R("printers")),
    ("#/history/__proto__", R("history")),
    ("#/history/hasOwnProperty", R("history")),
    ("#/%61ms", R("ams")),                                 # percent-encoded
    ("#/%E0%A4%A", R("printers")),                         # bad encoding
    ("#/history%2Fjobs", R("printers")),                   # one segment
    ("#/printers?finish=open", R("printers", "", {"finish": "open"})),   # the window unfolded
    ("#/printers?finish=open&period=24h", R("printers", "", {"finish": "open"})),
    ("#/ams?finish=open", R("ams")),                       # another screen's key
    ("#/history/jobs?period=24h&printer=p1", R("history", "jobs", {"period": "24h", "printer": "p1"})),
    ("#/history/chrono?period=custom&from=2026-10-21&to=2026-10-28",
     R("history", "chrono", {"period": "custom", "from": "2026-10-21", "to": "2026-10-28"})),
    ("#/history?finish=open&period=7d", R("history", "", {"period": "7d"})),   # another screen's key dropped
]


def test_parse_table():
    got = run("[" + ",".join("routeParse(%s)" % js(h) for h, _ in PARSE) + "]")
    assert got == [want for _, want in PARSE]


def test_build_and_round_trip():
    routes = [R(s) for s in ("printers", "ams", "history", "admin")]
    routes += [R("history", "jobs"), R("history", "chrono"), R("history", "stats"), R("history", "actions"),
               R("admin", "park"), R("admin", "auto"), R("admin", "access"), R("admin", "system"),
               R("printers", "", {"finish": "open"}),
               R("history", "stats", {"period": "7d", "printer": "p-1"}),
               R("history", "chrono", {"period": "custom", "from": "2026-10-21", "to": "2026-10-28"})]
    built = run("[" + ",".join("routeBuild(%s)" % js(r) for r in routes) + "]")
    assert built == ["#/printers", "#/ams", "#/history", "#/admin",
                     "#/history/jobs", "#/history/chrono", "#/history/stats", "#/history/actions",
                     "#/admin/park", "#/admin/auto", "#/admin/access", "#/admin/system", "#/printers?finish=open",
                     "#/history/stats?period=7d&printer=p-1",
                     "#/history/chrono?period=custom&from=2026-10-21&to=2026-10-28"]
    back = run("[" + ",".join("routeParse(%s)" % js(h) for h in built) + "]")
    assert back == routes
    # a route the app does not know is built as the one it shows
    odd = [R("bogus"), R("printers", "jobs"), R("history", "timeline"), {"screen": "ams"}, None,
           R("history", "jobs", {"period": "24h"}), R("settings"), R("admin", "jobs"), R("forecast"),
           R("ams", "", {"finish": "open"}), R("dashboard"), R("history", "stats", {"finish": "open", "printer": ""})]
    assert run("[" + ",".join("routeBuild(%s)" % js(r) for r in odd) + "]") == [
        "#/printers", "#/printers", "#/history", "#/ams", "#/printers", "#/history/jobs?period=24h", "#/printers",
        "#/admin", "#/printers", "#/ams", "#/printers", "#/history/stats"]


def test_query_keys_once_a_screen_keeps_them():
    # the period / printer of «История» (plan step 8): kept as strings here, the page's head checks them
    keys = ""
    got = run("[routeParse(%s), routeParse(%s), routeParse(%s)]" % (
        js("#/history/jobs?printer=p%201&x=1&period=24h&printer=p2"),
        js("#/history/jobs?period=&&printer=%E0%A4%A&=v"),
        js("#/ams?period=24h"),
    ), keys)
    assert got == [R("history", "jobs", {"printer": "p 1", "period": "24h"}),   # first value wins, x dropped
                   R("history", "jobs"),                                        # empty / bad values dropped
                   R("ams")]                                                     # another screen's keys
    built = run("routeBuild(%s)" % js(R("history", "jobs", {"printer": "p 1&2", "period": "24h", "x": "1"})), keys)
    assert built == "#/history/jobs?period=24h&printer=p%201%262"                 # the declared order
    assert run("routeParse(routeBuild(%s))" % js(R("history", "", {"printer": "Р/1?"})), keys) == \
        R("history", "", {"printer": "Р/1?"})


def test_views_and_routes():
    got = run("[" + ",".join([
        'routeOfView("fleet", "")', 'routeOfView("history", "chrono")', 'routeOfView("history", "jobs")',
        'routeOfView("history", "nope")', 'routeOfView("admin", "")', 'routeOfView("nope", "")',
        'routeOfView(null, "")', 'routeOfView("settings", "")', 'routeOfView("admin", "system")',
        'routeOfView("admin", "nope")', 'routeOfView("history", "stats")', 'routeOfView("history", "timeline")',
        'routeOfView("dashboard", "")',
    ]) + "]")
    # the «Настройки» panel is gone (plan step 3): its old name shows «Принтеры»; the panel's own
    # «timeline» is «chrono» now and «Сводка»'s panel is gone (plan step 8, deliberately changed)
    assert got == [R("printers"), R("history", "chrono"), R("history", "jobs"), R("history"), R("admin"),
                   R("printers"), R("printers"), R("printers"), R("admin", "system"), R("admin"),
                   R("history", "stats"), R("history"), R("printers")]
    got = run("[" + ",".join("routeView(routeParse(%s))" % js(h) for h in (
        "#/printers", "#/history/chrono", "#/history/jobs", "#/history", "#/admin", "#/x", "#/admin/access",
        "#/history/stats", "#/dashboard")) + "]")
    assert got == [{"view": "fleet", "sub": ""}, {"view": "history", "sub": "chrono"},
                   {"view": "history", "sub": "jobs"}, {"view": "history", "sub": ""},
                   {"view": "admin", "sub": ""}, {"view": "fleet", "sub": ""}, {"view": "admin", "sub": "access"},
                   {"view": "history", "sub": "stats"}, {"view": "history", "sub": "stats"}]


def test_admin_screen_by_role():
    got = run("[" + ",".join("routeForRole(%s, %s)" % (js(r), js(a)) for r, a in (
        (R("admin"), False), (R("admin"), None), (R("admin"), True), (R("history", "jobs"), False),
        (R("admin", "system"), False), (R("admin", "access"), None),
        (R("history", "actions"), False), (R("history", "actions"), None), (R("history", "actions"), True),
        (R("history", "actions", {"period": "7d"}), False))) + "]")
    # unknown role (null): the screen stays — an admin's reload must not flash «Принтеры»;
    # «История → Действия» is the admins' (plan step 8): a viewer keeps «История» and its filter
    assert got == [R("printers"), R("admin"), R("admin"), R("history", "jobs"), R("printers"),
                   R("admin", "access"), R("history"), R("history", "actions"), R("history", "actions"),
                   R("history", "", {"period": "7d"})]
    only = run("[" + ",".join("routeAdminOnly(%s)" % js(r) for r in (
        R("admin"), R("admin", "park"), R("history", "actions"), R("history", "stats"), R("history"), R("printers"))) + "]")
    assert only == [True, True, True, False, False, False]


def test_every_saved_tab_ever_written_opens_an_existing_screen():
    odd = [None, "", "garbage", "__proto__", "constructor", "FLEET", "history/jobs"]
    got = run("[" + ",".join("routeFirst(%s, %s)" % (js(h), js(v)) for v in SAVED_VIEWS + odd
                             for h in ("", "#", "#/")) + "]")
    views = run("ROUTE_SCREENS")
    for r in got:
        assert r["screen"] in views and views[r["screen"]]["view"] in PANELS, r
    first = run("[" + ",".join("routeFirst('', %s).screen" % js(v) for v in SAVED_VIEWS + odd) + "]")
    # the plan's map (IA plan step 2): the tabs that go away start on «Принтеры»
    assert dict(zip(SAVED_VIEWS + odd, first)) == {
        "fleet": "printers", "overview": "printers", "cameras": "printers", "dashboard": "printers",
        "forecast": "printers", "settings": "printers", "history": "history", "ams": "ams", "admin": "admin",
        None: "printers", "": "printers", "garbage": "printers", "__proto__": "printers",
        "constructor": "printers", "FLEET": "printers", "history/jobs": "printers"}
    # «История» without a sub: the panel's last one (forge-history-sub)
    assert run("routeFirst('', 'history')") == R("history")
    # «Окно» (plan step 7): «Принтеры» with the «Финиши» window unfolded; the others without a query
    assert run("routeFirst('', 'forecast')") == R("printers", "", {"finish": "open"})
    assert [q for q in run("[" + ",".join("routeFirst('', %s).query" % js(v) for v in SAVED_VIEWS if v != "forecast") + "]")] \
        == [{}] * (len(SAVED_VIEWS) - 1)


def test_saved_views_hold_every_tab_of_today():
    assert TABS <= set(SAVED_VIEWS)
    assert set(run("Object.keys(ROUTE_FROM_SAVED_VIEW)")) == set(SAVED_VIEWS)


def test_the_address_wins_over_the_saved_tab():
    got = run("[" + ",".join("routeFirst(%s, %s)" % (js(h), js(v)) for h, v in (
        ("#/ams", "history"), ("#/bogus", "history"), ("#/history/timeline", "admin"), ("#//", "ams"))) + "]")
    assert got == [R("ams"), R("printers"), R("history", "chrono"), R("ams")]


# --- the DOM side, with stubs ------------------------------------------------------------------

HARNESS = r"""
var calls = [], store = %(store)s, storeThrows = %(throws)s;
var localStorage = {
  getItem: function (k) { if (storeThrows) throw new Error("denied"); return Object.prototype.hasOwnProperty.call(store, k) ? store[k] : null; },
  removeItem: function (k) { if (storeThrows) throw new Error("denied"); calls.push(["remove", k]); delete store[k]; },
  setItem: function (k, v) { calls.push(["set", k, v]); store[k] = String(v); }
};
var location = { hash: %(hash)s, replace: function (h) { calls.push(["location.replace", h]); location.hash = h; } };
var listeners = {};
var window = {
  history: {
    pushState: function (s, t, h) { calls.push(["push", h]); location.hash = h; },
    replaceState: function (s, t, h) { calls.push(["replace", h]); location.hash = h; }
  },
  addEventListener: function (t, f) { calls.push(["listen", t]); listeners[t] = f; }
};
function panel(name, on) {
  return { dataset: { panel: name }, classList: { on: on,
    contains: function (c) { return c === "active" && this.on; }, toggle: function (c, v) { this.on = !!v; } } };
}
var viewPanels = ["fleet", "history", "ams", "admin"].map(function (n) {
  return panel(n, n === "fleet");
});
var currentUser = %(user)s, historySub = %(sub)s, adminSub = "park", finOpen = %(fin)s;
// a phone sheet's «Назад» entry (plan step 10): dead — the sheet just closed, a move takes the entry over
var sheetNav = { open: null, entry: false, dead: %(dead)s, skip: 0 };
// a phone has the «Финиши» list sheet, not the window; the bell's list (plan step 15)
var phoneSheets = %(phone)s;
function finPhone() { return phoneSheets; }
function finSheetSet(open) { calls.push(["finSheet", open]); }
function nfOpen(type) { calls.push(["nf", type]); }
var histFilter = null;   // assigned after the blocks (their vars)
function histApplyQuery(q) {
  var f = histFilterFrom(q);
  if (histSameFilter(f, histFilter)) return false;
  histFilter = f;
  calls.push(["filter", histQueryOf(f)]);
  return true;
}
function histLoadSub() { calls.push(["load", historySub]); }
function finSetOpen(open, fromAddress) {
  if (!!open !== finOpen) calls.push(["finish", !!open, !!fromAddress]);
  finOpen = !!open;
}
function setActiveView(view, fromAddress) {
  calls.push(["view", view, !!fromAddress]);
  viewPanels.forEach(function (p) { p.classList.toggle("active", p.dataset.panel === view); });
  if (!fromAddress) routeSync(true);
}
function setHistorySub(sub) { calls.push(["sub", sub]); historySub = sub; }
function setAdminSub(sub) { calls.push(["asub", sub]); adminSub = sub; }
function adminLoadSub() { calls.push(["load", "admin:" + adminSub]); }
%(lib)s
histFilter = histFilterFrom(%(filter)s);
%(steps)s
// after the microtasks: a sheet the route opens waits for the address to be written (plan step 15)
setTimeout(function () {
  console.log(JSON.stringify({ calls: calls, hash: location.hash, store: store,
    active: viewPanels.filter(function (p) { return p.classList.on; }).map(function (p) { return p.dataset.panel; }) }));
}, 0);
"""


def dom(steps, hash="", store=None, user=None, sub="chrono", throws=False, fin=False, filt=None, dead=False,
        phone=False):
    # the hist-ui block: the head's filter as the address carries it (pure, tests/test_web_hist_ui.py)
    lib = _block("route") + "\n" + _block("hist-ui") + "\n" + "\n".join(_js_function(n) for n in DOM_FUNCS)
    return _node(HARNESS % {"store": js(store or {}), "throws": js(throws), "hash": js(hash), "user": js(user),
                            "sub": js(sub), "lib": lib, "steps": steps, "fin": js(fin), "filter": js(filt or {}),
                            "dead": js(dead), "phone": js(phone)})


ADMIN, VIEWER = {"role": "admin"}, {"role": "viewer"}


@pytest.mark.parametrize("saved,view,address", [
    ("fleet", "fleet", None), ("overview", "fleet", None), ("cameras", "fleet", None),
    ("dashboard", "fleet", None), ("forecast", "fleet", "#/printers?finish=open"), ("settings", "fleet", None),
    ("ams", "ams", "#/ams"), ("admin", "admin", "#/admin/park"), ("history", "history", "#/history/jobs"),
    (None, "fleet", None), ("garbage", "fleet", None),
])
def test_start_migrates_the_saved_tab_once(saved, view, address):
    store = {"forge-history-sub": "jobs"}
    if saved is not None:
        store["forge-ops-active-view"] = saved
    out = dom("routeStart();", store=store, user=ADMIN, sub="jobs")
    assert "forge-ops-active-view" not in out["store"]                      # removed after the one read
    assert out["store"] == {"forge-history-sub": "jobs"}                     # nothing else touched
    assert ["set", "forge-ops-active-view", view] not in out["calls"]
    assert out["active"] == [view] and ["view", view, True] in out["calls"]
    # the address is written in place (no extra history entry), once
    writes = [c for c in out["calls"] if c[0] in ("push", "replace", "location.replace")]
    assert writes == [["replace", address or "#/printers"]]
    assert out["hash"] == (address or "#/printers")
    assert out["calls"][-1] == ["listen", "hashchange"]


def test_start_keeps_an_address_and_drops_the_key():
    out = dom("routeStart();", hash="#/ams", store={"forge-ops-active-view": "history"}, user=ADMIN)
    assert out["active"] == ["ams"] and out["store"] == {}
    assert not [c for c in out["calls"] if c[0] in ("push", "replace")]       # already the address it shows
    assert ["remove", "forge-ops-active-view"] in out["calls"]


def test_start_corrects_an_old_address_in_place():
    out = dom("routeStart();", hash="#/history/timeline", user=ADMIN, sub="jobs")
    assert out["active"] == ["history"] and out["hash"] == "#/history/chrono"
    calls = [c for c in out["calls"] if c[0] != "remove"]
    # the sub first (setActiveView's load reads it), then the panel, then the address
    assert calls == [["sub", "chrono"], ["view", "history", True], ["replace", "#/history/chrono"],
                     ["listen", "hashchange"]]
    # «Сводка»'s address (plan step 8): «Статистика», in place
    out = dom("routeStart();", hash="#/dashboard", user=ADMIN, sub="jobs")
    assert out["active"] == ["history"] and out["hash"] == "#/history/stats"
    assert [c for c in out["calls"] if c[0] in ("sub", "push", "replace")] == [["sub", "stats"], ["replace", "#/history/stats"]]


def test_start_survives_a_storage_that_throws():
    out = dom("routeStart();", hash="", throws=True)
    assert out["active"] == ["fleet"] and out["hash"] == "#/printers"


def test_start_on_printers_reopens_the_panel():
    # the HTML opens «Принтеры» already: the first apply still runs setActiveView once
    out = dom("routeStart();", hash="#/printers")
    assert [c for c in out["calls"] if c[0] == "view"] == [["view", "fleet", True]]


def test_back_and_forward_follow_the_address_without_new_entries():
    out = dom("""
      routeStart();
      location.hash = "#/history/chrono"; listeners.hashchange();
      location.hash = "#/history/jobs"; listeners.hashchange();
      location.hash = "#/ams"; listeners.hashchange();
      location.hash = "#/ams"; listeners.hashchange();
      location.hash = "#/fleet"; listeners.hashchange();
    """, hash="#/printers", user=ADMIN, sub="jobs")
    calls = [c for c in out["calls"] if c[0] not in ("remove", "listen")]
    assert calls == [
        ["view", "fleet", True],
        ["sub", "chrono"], ["view", "history", True],
        ["sub", "jobs"], ["load", "jobs"],                       # same panel: only the sub-view reloads
        ["view", "ams", True],
        # the same address again: nothing
        ["view", "fleet", True], ["replace", "#/printers"],      # an old name is rewritten in place
    ]
    assert not [c for c in out["calls"] if c[0] == "push"]


def test_a_move_in_the_app_pushes_one_entry():
    out = dom('routeStart(); setActiveView("ams"); setActiveView("ams"); historySub = "jobs"; setActiveView("history");',
              hash="#/printers", user=ADMIN)
    assert [c for c in out["calls"] if c[0] == "push"] == [["push", "#/ams"], ["push", "#/history/jobs"]]


def test_a_move_takes_over_a_closed_sheets_entry():
    # plan step 10: a phone sheet closed by a move to another screen in the same click («к карточке ›»
    # from the bell's sheet on AMS) — the move replaces the sheet's «Назад» entry instead of
    # pushing one more, so one «Назад» returns to the screen it came from
    out = dom('routeStart(); setActiveView("ams"); setActiveView("history");', hash="#/printers", user=ADMIN, dead=True)
    moves = [c for c in out["calls"] if c[0] in ("push", "replace")]
    assert moves[-2:] == [["replace", "#/ams"], ["push", "#/history/chrono"]]   # taken over once, then as usual
    # no move (the same screen): nothing is written and the entry stays to be given back
    out = dom('routeStart(); setActiveView("fleet"); calls.push(["dead", sheetNav.dead]);', hash="#/printers",
              user=ADMIN, dead=True)
    assert not [c for c in out["calls"] if c[0] == "push"] and out["calls"][-1] == ["dead", True]
    # an in-place correction does not take the entry over either
    out = dom('routeStart(); finOpen = true; routeSync(false); calls.push(["dead", sheetNav.dead]);',
              hash="#/printers", user=ADMIN, dead=True)
    assert out["calls"][-2:] == [["replace", "#/printers?finish=open"], ["dead", True]]


def test_a_viewer_gets_no_admin_screen():
    out = dom("routeStart();", hash="#/admin", user=VIEWER)
    assert out["active"] == ["fleet"] and out["hash"] == "#/printers"
    # the role arrives later: the check moves a viewer off it, in place
    out = dom("routeStart(); currentUser = {role: 'viewer'}; routeCheckRole();", hash="#/admin", user=None)
    assert out["active"] == ["fleet"] and out["hash"] == "#/printers"
    assert [c for c in out["calls"] if c[0] == "view"] == [["view", "admin", True], ["view", "fleet", True]]
    assert not [c for c in out["calls"] if c[0] == "push"]
    # an admin stays, and nothing happens on other screens
    out = dom("routeStart(); currentUser = {role: 'admin'}; routeCheckRole();", hash="#/admin", user=None)
    assert out["active"] == ["admin"] and out["hash"] == "#/admin/park"      # the sub-tab on screen
    out = dom("routeStart(); routeCheckRole();", hash="#/ams", user=VIEWER)
    assert out["active"] == ["ams"] and [c for c in out["calls"] if c[0] == "view"] == [["view", "ams", True]]
    # a typed address too
    out = dom('routeStart(); location.hash = "#/admin"; listeners.hashchange();', hash="#/ams", user=VIEWER)
    assert out["active"] == ["fleet"] and out["hash"] == "#/printers"


def test_admin_sub_tabs_follow_the_address():
    # a reload on a sub-tab: the sub first, then the panel (its load reads the sub), the address kept
    out = dom("routeStart();", hash="#/admin/system", user=ADMIN)
    calls = [c for c in out["calls"] if c[0] != "remove"]
    assert calls == [["asub", "system"], ["view", "admin", True], ["listen", "hashchange"]]
    assert out["active"] == ["admin"] and out["hash"] == "#/admin/system"
    # «Админ» without a sub: the sub-tab on screen («Парк» in a fresh tab), written in place
    out = dom("routeStart();", hash="#/admin", user=ADMIN)
    assert out["hash"] == "#/admin/park" and ["replace", "#/admin/park"] in out["calls"]
    assert not [c for c in out["calls"] if c[0] == "asub"]
    # Back / Forward between the sub-tabs: only the sub-tab and its data, no new entries
    out = dom("""
      routeStart();
      location.hash = "#/admin/access"; listeners.hashchange();
      location.hash = "#/admin/access"; listeners.hashchange();
      location.hash = "#/admin/park"; listeners.hashchange();
      location.hash = "#/ams"; listeners.hashchange();
      location.hash = "#/admin"; listeners.hashchange();
    """, hash="#/admin/park", user=ADMIN)
    calls = [c for c in out["calls"] if c[0] not in ("remove", "listen")]
    assert calls == [
        ["view", "admin", True],
        ["asub", "access"], ["load", "admin:access"],
        ["asub", "park"], ["load", "admin:park"],
        ["view", "ams", True],
        ["view", "admin", True], ["replace", "#/admin/park"],
    ]
    # a sub-tab click in the panel: one new entry
    out = dom('routeStart(); setAdminSub("access"); routeSync(true);', hash="#/admin/park", user=ADMIN)
    assert [c for c in out["calls"] if c[0] == "push"] == [["push", "#/admin/access"]]
    # a viewer never gets a sub-tab of it
    out = dom("routeStart();", hash="#/admin/system", user=VIEWER)
    assert out["active"] == ["fleet"] and out["hash"] == "#/printers"
    assert not [c for c in out["calls"] if c[0] == "asub"]


def test_the_finish_window_lives_in_the_address():
    # plan step 7: the old «Окно» address and tab open «Принтеры» with the «Финиши» window unfolded
    out = dom("routeStart();", hash="#/forecast", user=ADMIN)
    assert out["active"] == ["fleet"] and out["hash"] == "#/printers?finish=open"
    assert ["finish", True, True] in out["calls"]
    assert [c for c in out["calls"] if c[0] in ("push", "replace")] == [["replace", "#/printers?finish=open"]]
    # a reload keeps it; another screen and back (Back / Forward) keep and restore it; the tab click keeps it
    out = dom("""
      routeStart();
      setActiveView("ams");
      location.hash = "#/printers?finish=open"; listeners.hashchange();
      location.hash = "#/printers"; listeners.hashchange();
      setActiveView("fleet");
    """, hash="#/printers?finish=open", user=ADMIN)
    calls = [c for c in out["calls"] if c[0] in ("finish", "push", "replace")]
    assert calls == [["finish", True, True], ["push", "#/ams"], ["finish", False, True]]
    assert out["hash"] == "#/printers"
    out = dom('routeStart(); finOpen = true; setActiveView("ams"); setActiveView("fleet");', hash="#/printers", user=ADMIN)
    assert [c for c in out["calls"] if c[0] == "push"] == [["push", "#/ams"], ["push", "#/printers?finish=open"]]
    # a value the page does not know: folded, and the address says so
    out = dom("routeStart();", hash="#/printers?finish=1", user=ADMIN)
    assert out["hash"] == "#/printers" and not [c for c in out["calls"] if c[0] == "finish"]


def test_the_pwa_shortcuts_open_once():
    # plan step 15: «Финиши» on a phone — the list sheet (no window there), the address drops the key;
    # the sheet opens after the address is written (its «Назад» entry is the next one)
    out = dom("routeStart(); calls.push(['sync']);", hash="#/printers?finish=open", user=ADMIN, phone=True)
    assert out["hash"] == "#/printers" and not [c for c in out["calls"] if c[0] == "finish"]
    calls = [c for c in out["calls"] if c[0] in ("replace", "sync", "finSheet")]
    assert calls == [["replace", "#/printers"], ["sync"], ["finSheet", True]]
    # a desktop keeps the window, as before
    out = dom("routeStart();", hash="#/printers?finish=open", user=ADMIN, phone=False)
    assert out["hash"] == "#/printers?finish=open" and not [c for c in out["calls"] if c[0] == "finSheet"]
    # «Нужно решение»: the bell's list once, on both, the address keeps nothing
    for phone in (True, False):
        out = dom("routeStart();", hash="#/printers?bell=open", user=ADMIN, phone=phone)
        assert out["hash"] == "#/printers" and [c for c in out["calls"] if c[0] == "nf"] == [["nf", "farm"]]
    out = dom('routeStart(); location.hash = "#/printers?bell=open"; listeners.hashchange();', hash="#/ams",
              user=ADMIN)
    assert out["hash"] == "#/printers" and [c for c in out["calls"] if c[0] == "nf"] == [["nf", "farm"]]
    # another value, another screen: nothing opens
    out = dom("routeStart();", hash="#/printers?bell=1", user=ADMIN)
    assert not [c for c in out["calls"] if c[0] in ("nf", "finSheet")]
    out = dom("routeStart();", hash="#/ams?bell=open", user=ADMIN)
    assert out["hash"] == "#/ams" and not [c for c in out["calls"] if c[0] == "nf"]


def test_the_old_settings_address_opens_printers():
    # the owner's windows may sit on #/settings (plan step 2) when step 3 reloads them
    out = dom("routeStart();", hash="#/settings", user=ADMIN)
    assert out["active"] == ["fleet"] and out["hash"] == "#/printers"
    assert [c for c in out["calls"] if c[0] in ("push", "replace")] == [["replace", "#/printers"]]


def test_the_head_filter_lives_in_the_address():
    # plan step 8: a reload keeps the period and the printer; Back / Forward between filters reload
    # the sub-tab only; what the head does not know is written back as the filter it shows
    out = dom("routeStart();", hash="#/history/stats?period=7d&printer=p-1", user=ADMIN, sub="jobs")
    assert out["active"] == ["history"] and out["hash"] == "#/history/stats?period=7d&printer=p-1"
    calls = [c for c in out["calls"] if c[0] not in ("remove", "listen")]
    assert calls == [["sub", "stats"], ["filter", {"period": "7d", "printer": "p-1"}], ["view", "history", True]]
    out = dom("""
      routeStart();
      location.hash = "#/history/stats"; listeners.hashchange();
      location.hash = "#/history/stats"; listeners.hashchange();
      location.hash = "#/history/jobs?period=30d"; listeners.hashchange();
      location.hash = "#/history/jobs?period=nope&printer="; listeners.hashchange();
    """, hash="#/history/stats?period=7d", user=ADMIN, sub="jobs")
    calls = [c for c in out["calls"] if c[0] not in ("remove", "listen")]
    assert calls == [
        ["sub", "stats"], ["filter", {"period": "7d"}], ["view", "history", True],
        ["filter", {}], ["load", "stats"],                               # the default period, the sub reloads
        ["sub", "jobs"], ["filter", {"period": "30d"}], ["load", "jobs"],  # one load for both
        ["filter", {}], ["load", "jobs"], ["replace", "#/history/jobs"],   # an unknown period: 24 ч, in place
    ]
    assert not [c for c in out["calls"] if c[0] == "push"]
    # the screen left and come back to with the tab: the filter of the session, in the address
    out = dom('routeStart(); setActiveView("ams"); setActiveView("history");',
              hash="#/history/chrono?period=custom&from=2026-10-21&to=2026-10-28", user=ADMIN, sub="jobs")
    assert [c for c in out["calls"] if c[0] == "push"] == [
        ["push", "#/ams"], ["push", "#/history/chrono?period=custom&from=2026-10-21&to=2026-10-28"]]
    # «Свой…» with the days the wrong way round: no custom period
    out = dom("routeStart();", hash="#/history/jobs?period=custom&from=2026-10-28&to=2026-10-21", user=ADMIN, sub="jobs")
    assert out["hash"] == "#/history/jobs"


def test_a_viewer_gets_no_actions_sub():
    # «Действия» are the admins' (plan step 8): a viewer's address goes to the sub on screen (a fresh
    # tab's «Печати»), in place; a role that arrives later moves them the same way
    out = dom("routeStart();", hash="#/history/actions?period=7d", user=VIEWER, sub="actions")
    assert out["active"] == ["history"] and out["hash"] == "#/history/jobs?period=7d"
    assert ["sub", "jobs"] in out["calls"] and ["sub", "actions"] not in out["calls"]
    out = dom("routeStart(); currentUser = {role: 'viewer'}; routeCheckRole();", hash="#/history/actions",
              user=None, sub="jobs")
    assert out["active"] == ["history"] and out["hash"] == "#/history/jobs"
    assert [c for c in out["calls"] if c[0] == "sub"] == [["sub", "actions"], ["sub", "jobs"]]
    assert not [c for c in out["calls"] if c[0] == "push"]
    out = dom("routeStart(); currentUser = {role: 'admin'}; routeCheckRole();", hash="#/history/actions",
              user=None, sub="jobs")
    assert out["hash"] == "#/history/actions"


# --- the wiring in app.js ----------------------------------------------------------------------

def test_the_saved_tab_is_never_written_again():
    assert not re.search(r"setItem\(\s*[\"']forge-ops-active-view", JS)
    assert JS.count('localStorage.getItem("forge-ops-active-view")') == 1
    assert JS.count('localStorage.removeItem("forge-ops-active-view")') == 1
    start = _js_function("routeStart")
    assert start.index("getItem") < start.index("removeItem") < start.index("routeApply(")
    for line in start.splitlines():
        if "localStorage" in line:
            assert line.strip().startswith(("saved = ", "localStorage.removeItem")), line
    assert "try {" in start and "} catch (e) {}" in start


def test_set_active_view_writes_the_address():
    body = _js_function("setActiveView")
    assert body.startswith("function setActiveView(view, fromAddress) {")
    assert "if (!fromAddress) routeSync(true);" in body
    assert body.index("panel.classList.toggle") < body.index("routeSync(true)")   # reads the panel on screen
    assert "localStorage" not in body


def test_the_router_starts_in_a_microtask():
    # appjs-iife-var-hoisting-trap: setActiveView / setHistorySub read vars assigned further down the IIFE
    assert JS.count("Promise.resolve().then(routeStart);") == 1
    assert len(re.findall(r"\brouteStart\(\)", JS)) == 1                          # its declaration only
    assert JS.index("Promise.resolve().then(routeStart);") > JS.index("async function load()")
    assert "routeStart" not in JS[:JS.index("/* route:begin */")]


def test_moves_and_the_role_are_wired():
    click = JS[JS.index("(function initHistHead() {"):]
    click = click[:click.index("histEls.period.addEventListener")]
    assert click.index("setHistorySub(b.dataset.sub);") < click.index("routeSync(true);") < \
        click.index("histLoadSub();")
    assert "routeCheckRole();" in _js_function("updateAuthUI")
    # a tab opens its screen from the top; on a phone a second tap on its own item goes to phoneRetap (plan step 10)
    assert ('if (navPhone() && routeActiveView() === tab.dataset.view) { phoneRetap(tab.dataset.view); return; }\n'
            '          setActiveView(tab.dataset.view);\n          window.scrollTo(0, 0);') in JS
    assert 'document.getElementById("brandBtn").addEventListener("click", () => { setActiveView("fleet"); ' \
           'window.scrollTo(0, 0); });' in JS
    # the router's own writes: pushState / replaceState only (they fire no hashchange)
    sync = _js_function("routeSync")
    assert 'window.history[push ? "pushState" : "replaceState"](null, "", want);' in sync
    assert _js_function("routeOnHash").count("routeSync(false)") == 1


# --- the header (IA §4) ------------------------------------------------------------------------

def _media(cond):
    """The bodies of every `@media (<cond>) {` block of styles.css (braces matched)."""
    out, start = [], 0
    while True:
        i = CSS.find("@media (%s) {" % cond, start)
        if i < 0:
            return out
        j, depth = CSS.index("{", i), 0
        for k in range(j, len(CSS)):
            depth += {"{": 1, "}": -1}.get(CSS[k], 0)
            if depth == 0:
                out.append(CSS[j + 1:k])
                break
        start = k


def test_header_markup():
    head = HTML[HTML.index('<header class="topbar">'):HTML.index("</header>")]
    assert head.index('id="brandBtn"') < head.index('class="view-tabs"') < head.index('class="topbar-side"')
    assert '<button type="button" class="topbar-brand" id="brandBtn">' in head and "Кузница" in head
    chip = [a for t, a in tags(head) if a.get("id") == "liveChip"]
    assert len(chip) == 1 and chip[0]["class"] == "status-chip" and "title" not in chip[0] and chip[0]["data-tip"]
    assert "на связи" not in HTML and 'id="lastUpdate"' in head
    assert "bindTips(document);" in JS          # one binding for the page (plan step 14), the chip in it


def test_header_is_sticky_and_opaque_on_a_desktop():
    desk = "\n".join(_media("min-width: 901px"))
    top = re.search(r"\n      \.topbar \{(.*?)\}", desk, re.S)
    assert top, "no .topbar rule in @media (min-width: 901px)"
    rule = top.group(1)
    for need in ("position: sticky;", "top: 0;", "height: 48px;", "z-index: 90;",
                 "background-color: var(--bg-elev);", "background-image: linear-gradient(var(--bg-soft), var(--bg-soft));"):
        assert need in rule, need
    # every theme's --bg-elev is opaque: the page never shows through
    elev = re.findall(r"^\s*--bg-elev:\s*([^;]+);", CSS, re.M)
    assert len(elev) == 8                                                     # the seven themes, «Стекло» twice (dark, light)
    for value in elev:
        assert re.fullmatch(r"#[0-9a-fA-F]{6}", value.strip()), value
    assert "html { scroll-padding-top: 64px; }" in desk                      # scrollIntoView clears it
    # the AMS tab's auto-drying line sticks right under it (plan step 11; the form's sticky side is gone)
    assert ".ams-strip { top: 48px; margin: -16px -20px 12px; padding: 6px 20px; }" in desk
    assert ".autodry .ad-side" not in CSS
    # the popovers and tips stay above the header, the page's own layers below it
    z = {sel: int(v) for sel, v in re.findall(r"\n    (\.pc-pop|\.nf-pop|\.nf-back|\.pc-tip) \{[^}]*?z-index: (\d+);", CSS)}
    assert z and all(v > 90 for v in z.values()), z
    # a phone has its own 44 px bar and the bottom navigation since plan step 10 (tests/test_web_phone_ui.py):
    # the brand stays a desktop thing
    assert ".topbar-brand { display: none; }" in CSS
    phone = [b for b in _media("max-width: 900px") if ".topbar {" in b]
    assert len(phone) == 1 and "height: 44px;" in phone[0] and "height: 48px;" not in phone[0]


def test_status_chip_has_no_leftovers():
    assert "#lastUpdate.stale" not in CSS and ".status-chip strong" not in CSS
    assert 'classList.add("stale")' not in JS and "(устар.)" not in CSS
    for cls in ("late", "down"):
        assert ".status-chip.%s " % cls in CSS, cls


def test_cache_bust_bumped():
    assert int(re.search(r"styles\.css\?v=(\d+)", HTML).group(1)) >= 87
    assert int(re.search(r"app\.js\?v=(\d+)", HTML).group(1)) >= 104
