"""The phone of app/web (IA §4, §5; plan step 10): the navigation.

≤ 900 px: a sticky 44 px bar on top (the screen's name, the bell, ● time, 👤) and
the bottom navigation — the header's own tabs, fixed at the bottom, an icon over
the word, in the order of IA §4 (Принтеры · AMS · История · Админ; a viewer's
three). A second tap on the screen's own item goes back to its top and folds
what is unfolded. A sheet on screen owns one history entry, so «Назад» closes it
(popstate). «Ферма ▾» opens a sheet with «Порядок» and the farm's commands — the
farm row's own controls, moved in while it is open. A desktop (≥ 901 px) is not
touched (plan step 10).

Plan step 15: every popover is a bottom sheet on a phone (≤ 899 px: the card's
chips, AMS, ⋯, the notifications, 👤), the AMS blocks are rows that unfold, the
«Админ» tables are cards, «Хронология» takes a pinch and a tap, the PWA has
shortcuts (tests/test_pwa.py, tests/test_web_route_ui.py) and a browser push opens
its printer. The DOM-free bits run in node with stubs; skipped without node.
"""
import json
import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "app" / "web"
JS = (WEB / "app.js").read_text(encoding="utf-8")
HTML = (WEB / "index.html").read_text(encoding="utf-8")
CSS = (WEB / "styles.css").read_text(encoding="utf-8")


def _js_function(name, src=JS):
    """The whole `function name(...) {...}`: braces matched, strings and // comments skipped."""
    m = re.search(r"function %s\([^)]*\) \{" % name, src)
    assert m, "%s() not found" % name
    i, depth, quote = m.end() - 1, 0, None
    while i < len(src):
        ch = src[i]
        if quote:
            if ch == "\\":
                i += 1
            elif ch == quote:
                quote = None
        elif ch in "'\"`":
            quote = ch
        elif src.startswith("//", i):
            i = src.index("\n", i)
            continue
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[m.start():i + 1]
        i += 1
    raise AssertionError("%s(): unbalanced braces" % name)


def _node(src):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    out = subprocess.run([node, "-"], input=src, capture_output=True, text=True, timeout=60, encoding="utf-8")
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


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


def _rule(block, sel):
    m = re.search(r"\n\s*" + re.escape(sel) + r" \{(.*?)\}", block, re.S)
    assert m, sel
    return m.group(1)


HEAD = HTML[HTML.index('<header class="topbar">'):HTML.index("</header>")]
PHONE = [b for b in _media("max-width: 900px") if ".topbar {" in b]


# --- markup ------------------------------------------------------------------------------------

def test_the_bar_has_the_screen_name_and_the_tabs_their_icons():
    assert HEAD.index('id="brandBtn"') < HEAD.index('id="topbarScr"') < HEAD.index('class="view-tabs"')
    scr = [a for t, a in tags(HEAD) if a.get("id") == "topbarScr"][0]
    assert scr["class"] == "topbar-scr"
    tabs = re.findall(r'<button type="button" class="view-tab[^"]*" data-view="(\w+)"[^>]*>'
                      r'<svg class="pc-ic vt-ic" aria-hidden="true"><use href="#(i-[\w-]+)"></use></svg>([^<]+)</button>', HEAD)
    # IA §4's order in the markup since plan step 16 (the desktop header as the approved mockup); the
    # phone's CSS order below says the same
    assert tabs == [("fleet", "i-grid", "Принтеры"), ("ams", "i-drop", "AMS"), ("history", "i-history", "История"),
                    ("admin", "i-tool", "Админ")]
    for sym in ("i-grid", "i-history", "i-drop", "i-tool"):
        assert '<symbol id="%s" viewBox="0 0 24 24">' % sym in HTML, sym
    assert 'id="adminTab" style="display: none;"' in HEAD          # a viewer's three items


def test_the_farm_row_has_its_sheet():
    farm = HTML[HTML.index('<div class="farm" id="fleetFarm">'):HTML.index('<div id="finishRow">')]
    assert farm.index('id="farmOrder"') < farm.index('id="farmSheetBtn"') < farm.index('<div id="fleetBulk">')
    btn = [a for t, a in tags(farm) if a.get("id") == "farmSheetBtn"][0]
    assert btn["class"] == "farm-more" and btn["aria-haspopup"] == "dialog" and btn["aria-controls"] == "farmSheet"
    assert btn["aria-expanded"] == "false" and "Ферма ▾" in farm
    sheet = HTML[HTML.index('<div class="fin-back" id="farmBack"'):HTML.index('<div class="me-back"')]
    t = dict((a.get("id"), a) for _, a in tags(sheet) if a.get("id"))
    assert t["farmSheet"]["role"] == "dialog" and t["farmSheet"]["aria-modal"] == "true" and "hidden" in t["farmSheet"]
    assert t["farmSheet"]["class"] == "fin-sheet farm-sheet" and t["farmSheet"]["aria-labelledby"] == "farmSheetTitle"
    assert "hidden" in t["farmBack"] and "hidden" in t["farmSheetCmds"] and "farmSheetOrder" in t
    assert "data-farm-close" in sheet and "Команды фермы" in sheet


# --- CSS ---------------------------------------------------------------------------------------

def test_the_phone_bar_and_the_bottom_navigation():
    assert len(PHONE) == 1, "one @media (max-width: 900px) block with the phone's bar"
    b = PHONE[0]
    # after every base rule, so its overrides win (the sticky heads, the toast, the save bar…)
    assert CSS.index(b) > CSS.index("\n    .hist-head {") and CSS.index(b) > CSS.index("\n    .adm-head {")
    assert CSS.index(b) > CSS.index("\n    .scroll-top-btn {") and CSS.index(b) > CSS.index("\n    .me-btn {")
    top = _rule(b, ".topbar")
    for need in ("position: sticky;", "top: 0;", "z-index: 90;", "height: 44px;", "background-color: var(--bg-elev);",
                 "background-image: linear-gradient(var(--bg-soft), var(--bg-soft));", "flex-wrap: nowrap;"):
        assert need in top, need
    nav = _rule(b, ".view-tabs")
    for need in ("position: fixed;", "bottom: 0;", "left: 0;", "right: 0;", "height: calc(56px + env(safe-area-inset-bottom));",
                 "padding: 0 0 env(safe-area-inset-bottom);", "background-color: var(--bg-elev);",
                 "grid-auto-columns: minmax(0, 1fr);", "align-items: stretch;"):
        assert need in nav, need
    assert int(re.search(r"z-index: (\d+);", nav).group(1)) < 90              # under the popovers, the sheets
    # every theme's --bg-elev is opaque: the page never shows through the bar or the navigation
    for value in re.findall(r"^\s*--bg-elev:\s*([^;]+);", CSS, re.M):
        assert re.fullmatch(r"#[0-9a-fA-F]{6}", value.strip()), value
    tab = _rule(b, ".view-tabs .view-tab")
    assert "flex-direction: column;" in tab and "text-transform: none;" in tab
    assert ".view-tabs .view-tab .vt-ic { display: block; width: 22px; height: 22px; }" in b
    assert ".view-tabs .view-tab.active { color: var(--info); background: none; box-shadow: none; }" in b
    # IA §4's order on a phone: Принтеры · AMS · История · Админ
    order = dict(re.findall(r'\.view-tab\[data-view="(\w+)"\] \{ order: (\d); \}', b))
    assert order == {"ams": "1", "history": "2", "admin": "3"}
    # the page clears both bars; «Наверх» is the second tap; the toast clears the navigation
    assert ".shell { width: 100%; padding: 0 12px calc(76px + env(safe-area-inset-bottom)); }" in b
    assert "scroll-padding-top: 54px;" in b and ".scroll-top-btn { display: none; }" in b
    assert ".app-toast { bottom: calc(68px + env(safe-area-inset-bottom)); }" in b
    # the screens' sticky heads stick under the 44 px bar, edge to edge; the save bar over the navigation
    assert ".hist-head, .adm-head, .ams-strip { top: 44px; margin: -10px -12px 10px; padding: 6px 12px; }" in b
    assert ".autodry .ad-save { bottom: calc(56px + env(safe-area-inset-bottom)); }" in b
    # 44 px touch targets on the card; the bar's 👤 is the icon
    assert ".pc-controls .pc-ctl, .printer-card .pc-confirm .q, .printer-card .pc-confirm button { height: 44px; }" in b
    assert ".me-btn span { display: none; }" in b and ".me-btn { width: 40px; min-height: 40px;" in b
    # «Принтеры»: the cards on the page itself (no frame around the grid)
    assert ".fleet-section { padding: 0; border: 0;" in b
    # a desktop keeps them hidden; the old two-row phone header is gone
    assert ".topbar-scr, .view-tab .vt-ic { display: none; }" in CSS
    assert "Компактная шапка" not in CSS and ".topbar-side::-webkit-scrollbar" not in CSS
    small = [x for x in _media("max-width: 420px") if "#lastUpdate" in x]
    assert small == ["\n      .status-chip:not(.down) #lastUpdate { display: none; }\n    "]   # a failure keeps its words


def test_the_farm_row_on_a_phone():
    assert ".farm-more { display: none; }" in CSS
    phone = [b for b in _media("max-width: 899px") if ".farm-more {" in b]
    assert len(phone) == 1
    b = phone[0]
    assert ".farm-row .farm-sep, .farm .farm-order, .farm #fleetBulk { display: none; }" in b
    assert ".farm-row { flex-wrap: nowrap; gap: 6px; }" in b
    assert "min-height: 36px;" in _rule(b, ".farm-more")
    assert ".farm-more.warn::after" in b
    assert ".farm-sheet .farm-seg button, .farm-sheet .fb-btn { min-height: 40px; }" in CSS


# --- «Назад» closes a sheet --------------------------------------------------------------------

SHEET_SRC = JS[JS.index("      var navMq = "):JS.index("      // The bar's screen name")]

SHEET_HARNESS = r"""
var phone = %(phone)s, log = [], listeners = {}, ticks = [];
var location = { href: "http://x/#/ams" };
var window = {
  matchMedia: function (q) { return { matches: phone, q: q }; },
  addEventListener: function (t, f) { listeners[t] = f; },
  history: {
    state: null,
    pushState: function (s, t, h) { log.push(["push", s, h]); this.state = s; },
    // the browser answers history.back() later, with a popstate
    back: function () { log.push(["back"]); this.state = null; ticks.push(function () { listeners.popstate({}); }); }
  }
};
%(src)s
var closes = [];
function closerA() { closes.push("A"); sheetClosed(closerA); }
function closerB() { closes.push("B"); sheetClosed(closerB); }
function flush() { return Promise.resolve().then(function () { while (ticks.length) ticks.shift()(); }); }
(async function () {
  %(steps)s
  console.log(JSON.stringify({ log: log, closes: closes, nav: sheetNav, mq: [navMq.q, sheetMq.q] }));
})();
"""


def sheets(steps, phone=True):
    return _node(SHEET_HARNESS % {"phone": json.dumps(phone), "src": SHEET_SRC, "steps": steps})


def test_back_closes_the_sheet_and_leaves_the_screen_alone():
    out = sheets("sheetOpened(closerA); window.history.state = null; listeners.popstate({}); await flush();")
    assert out["log"] == [["push", {"forgeSheet": 1}, "http://x/#/ams"]]   # the same address
    assert out["closes"] == ["A"]                                          # closed once, no history.back
    assert out["nav"] == {"open": None, "entry": False, "dead": False, "skip": 0}
    assert out["mq"] == ["(max-width: 900px)", "(max-width: 899px)"]


def test_a_sheet_closed_otherwise_gives_its_entry_back():
    out = sheets("sheetOpened(closerA); closerA(); await flush(); await flush();")
    assert out["log"] == [["push", {"forgeSheet": 1}, "http://x/#/ams"], ["back"]]
    assert out["closes"] == ["A"]                       # the popstate of our own back closes nothing
    assert out["nav"] == {"open": None, "entry": False, "dead": False, "skip": 0}


def test_one_sheet_replacing_another_keeps_one_entry():
    # the bell's sheet closes as a card's popover opens (openPop → nfClose → the next sheet)
    out = sheets("sheetOpened(closerA); closerA(); sheetOpened(closerB); await flush();"
                 "window.history.state = null; listeners.popstate({}); await flush();")
    assert out["log"] == [["push", {"forgeSheet": 1}, "http://x/#/ams"]]
    assert out["closes"] == ["A", "B"]                  # «Назад» closed the second one
    # opening the same sheet again (its content changed) pushes nothing more
    out = sheets("sheetOpened(closerA); sheetOpened(closerA); await flush();")
    assert len(out["log"]) == 1 and out["nav"]["entry"] is True


def test_a_desktop_gets_no_entries():
    out = sheets("sheetOpened(closerA); closerA(); await flush(); listeners.popstate({});", phone=False)
    assert out["log"] == [] and out["closes"] == ["A"]
    # a closer that is not the open sheet's changes nothing; sheetCloseAll closes the open one
    out = sheets("sheetOpened(closerA); sheetClosed(closerB); sheetCloseAll(); await flush();")
    assert out["closes"] == ["A"] and out["log"][-1] == ["back"]


def test_the_router_takes_a_closed_sheets_entry_over():
    sync = _js_function("routeSync")
    assert "if (push && sheetNav && sheetNav.dead) { sheetNav.dead = false; push = false; }" in sync
    assert sync.index("if (location.hash === want) return;") < sync.index("sheetNav.dead")   # only a real move takes it


def test_every_sheet_owns_its_entry():
    fin = _js_function("finSheetSet")
    assert "if (open) sheetOpened(finSheetClose); else sheetClosed(finSheetClose);" in fin
    assert "function finSheetClose() { finSheetSet(false); }" in JS
    assert "sheetOpened(nfSheetClose);" in _js_function("nfOpen")
    assert "sheetClosed(nfSheetClose);" in _js_function("nfClose")
    goto = _js_function("nfGoto")
    assert goto.index("nfPop = null;") < goto.index("sheetClosed(nfSheetClose);") < goto.index("setActiveView(")
    assert "{ nfPop = null; sheetClosed(nfSheetClose); }" in _js_function("renderNf")
    me = JS[JS.index("(function initMePanel() {"):JS.index("function renderMeWho()")]
    assert "if (open) sheetOpened(meSheetClose); else sheetClosed(meSheetClose);" in me
    farm = _js_function("farmSheetSet")
    assert "sheetOpened(farmSheetClose);" in farm and "sheetClosed(farmSheetClose);" in farm


# --- the second tap, the screen name -----------------------------------------------------------

RETAP_HARNESS = r"""
var log = [], fleetRestOpen = %(rest)s, fleetFilter = ["printing", "offline", "maintenance"];
var classes = { "hist-open": true }, attrs = {};
var document = { getElementById: function (id) {
  if (id === "histHead") return { classList: { remove: function (c) { log.push(["fold", c]); delete classes[c]; } } };
  if (id === "histSum") return { setAttribute: function (k, v) { attrs[k] = v; } };
  return null; } };
var window = { scrollTo: function (o) { log.push(["scroll", o]); } };
function sheetCloseAll() { log.push(["sheets"]); }
function persistFilters() { log.push(["persist", fleetFilter.slice()]); }
function applyAndRender() { log.push(["render"]); }
%(fn)s
phoneRetap(%(view)s);
console.log(JSON.stringify({ log: log, rest: fleetRestOpen, attrs: attrs }));
"""


def retap(view, rest=True):
    return _node(RETAP_HARNESS % {"fn": _js_function("phoneRetap"), "view": json.dumps(view), "rest": json.dumps(rest)})


def test_a_second_tap_folds_and_goes_to_the_top():
    out = retap("fleet")
    assert out["rest"] is False                        # «Не в работе» folds, as its «Скрыть»
    assert out["log"] == [["sheets"], ["persist", ["printing"]], ["render"], ["scroll", {"top": 0, "behavior": "smooth"}]]
    assert retap("fleet", rest=False)["log"] == [["sheets"], ["scroll", {"top": 0, "behavior": "smooth"}]]
    out = retap("history")
    assert out["log"][:2] == [["sheets"], ["fold", "hist-open"]] and out["attrs"] == {"aria-expanded": "false"}
    assert retap("admin")["log"] == [["sheets"], ["scroll", {"top": 0, "behavior": "smooth"}]]   # a form is never lost


def test_the_tabs_wire_the_second_tap_on_a_phone_only():
    click = JS[JS.index("viewTabs.forEach((tab) => {\n        tab.addEventListener"):]
    click = click[:click.index("});\n      });") + 12]
    assert "if (navPhone() && routeActiveView() === tab.dataset.view) { phoneRetap(tab.dataset.view); return; }" in click
    assert click.index("phoneRetap") < click.index("setActiveView(tab.dataset.view);") < click.index("window.scrollTo(0, 0);")


SCR_HARNESS = r"""
var active = %(view)s, pressed = %(sub)s;
function el() { return { textContent: "", kids: [], appendChild: function (c) { this.kids.push(c); return c; } }; }
var bar = el();
var document = {
  getElementById: function (id) { return id === "topbarScr" ? bar : null; },
  querySelector: function (q) { return pressed && q.indexOf(active === "history" ? "#historySub" : "#adminSub") === 0
    ? { textContent: " " + pressed + "\n" } : null; },
  createElement: function () { return el(); }
};
function routeActiveView() { return active; }
%(fn)s
renderScrName();
console.log(JSON.stringify([bar.textContent].concat(bar.kids.map(function (k) { return [k.className, k.textContent]; }))));
"""


@pytest.mark.parametrize("view,sub,want", [
    ("fleet", None, ["Принтеры"]),
    ("ams", None, ["AMS"]),
    ("history", "Печати", ["История", ["ts-sub", " · Печати"]]),
    ("admin", "Парк", ["Админ", ["ts-sub", " · Парк"]]),
])
def test_the_screen_name(view, sub, want):
    out = _node(SCR_HARNESS % {"fn": _js_function("renderScrName"), "view": json.dumps(view), "sub": json.dumps(sub)})
    assert out == want


def test_the_screen_name_follows_every_move():
    assert _js_function("setActiveView").rstrip("}").rstrip().endswith("renderScrName();")
    assert "renderScrName();" in _js_function("setAdminSub") and "renderScrName();" in _js_function("setHistorySub")


# --- «Ферма ▾» ---------------------------------------------------------------------------------

FARM_HARNESS = r"""
function node(id, cls) {
  return { id: id, className: cls || "", parentNode: null, kids: [], hidden: false, attrs: {}, focused: 0,
    appendChild: function (c) { detach(c); c.parentNode = this; this.kids.push(c); return c; },
    insertBefore: function (c, ref) { detach(c); c.parentNode = this; this.kids.splice(this.kids.indexOf(ref), 0, c); return c; },
    contains: function (x) { while (x) { if (x === this) return true; x = x.parentNode; } return false; },
    setAttribute: function (k, v) { this.attrs[k] = v; }, focus: function () { this.focused++; } };
}
function detach(c) { if (c.parentNode) { var k = c.parentNode.kids; k.splice(k.indexOf(c), 1); } }
var row = node("farmRow", "farm-row"), top = node("farmTop", "farm-top");
var order = node("farmOrder"), btn = node("farmSheetBtn"), fleetBulkEl = node("fleetBulk");
row.appendChild(order); row.appendChild(btn); top.appendChild(row); top.appendChild(fleetBulkEl);
var sheet = node("farmSheet"), back = node("farmBack"), sOrder = node("farmSheetOrder"), cmds = node("farmSheetCmds");
sheet.appendChild(sOrder); sheet.appendChild(cmds); sheet.hidden = back.hidden = cmds.hidden = true;
var byId = { farmSheet: sheet, farmBack: back, farmSheetBtn: btn, farmOrder: order, farmSheetOrder: sOrder, farmSheetCmds: cmds };
var document = { activeElement: null, getElementById: function (id) { return byId[id] || null; },
  querySelector: function (q) { return q === "#fleetFarm .farm-top" ? top : null; } };
var admin = %(admin)s, log = [];
function isAdmin() { return admin; }
function sheetOpened(c) { log.push(["opened", c.name]); }
function sheetClosed(c) { log.push(["closed", c.name]); }
var farmSheetOn = false;
%(fn)s
function farmSheetClose() { farmSheetSet(false); }
function where() {
  return { order: order.parentNode.id, bulk: fleetBulkEl.parentNode.id, sheet: !sheet.hidden, back: !back.hidden,
           cmds: !cmds.hidden, expanded: btn.attrs["aria-expanded"], rowKids: row.kids.map(function (k) { return k.id; }),
           topKids: top.kids.map(function (k) { return k.id; }) };
}
var out = { start: where() };
farmSheetSet(true); out.open = where(); out.sheetFocus = sheet.focused;
farmSheetSet(true);
document.activeElement = order;
farmSheetSet(false); out.closed = where(); out.btnFocus = btn.focused;
out.log = log;
console.log(JSON.stringify(out));
"""


def test_the_farm_sheet_moves_the_rows_controls_in_and_back():
    out = _node(FARM_HARNESS % {"fn": _js_function("farmSheetSet"), "admin": "true"})
    assert out["start"]["rowKids"] == ["farmOrder", "farmSheetBtn"] and out["start"]["topKids"] == ["farmRow", "fleetBulk"]
    o = out["open"]
    assert (o["order"], o["bulk"], o["sheet"], o["back"], o["cmds"], o["expanded"]) == \
        ("farmSheetOrder", "farmSheetCmds", True, True, True, "true")
    assert out["sheetFocus"] == 1
    c = out["closed"]                                   # back in their places, in their order
    assert c["rowKids"] == ["farmOrder", "farmSheetBtn"] and c["topKids"] == ["farmRow", "fleetBulk"]
    assert (c["sheet"], c["back"], c["expanded"]) == (False, False, "false") and out["btnFocus"] == 1
    assert out["log"] == [["opened", "farmSheetClose"], ["closed", "farmSheetClose"]]   # opening twice: one entry
    viewer = _node(FARM_HARNESS % {"fn": _js_function("farmSheetSet"), "admin": "false"})
    assert viewer["open"]["cmds"] is False and viewer["open"]["order"] == "farmSheetOrder"   # «Порядок» only


def test_the_farm_sheet_is_wired():
    init = JS[JS.index("(function initFarmSheet() {"):]
    init = init[:init.index("})();") + 5]
    assert 'btn.addEventListener("click", function () { farmSheetSet(!farmSheetOn); });' in init
    assert 'back.addEventListener("click", farmSheetClose);' in init
    # «Пауза всех…» / «Продолжить» open their dialog over the page: the sheet goes first (capture)
    assert ("""e.target.closest('[data-bulk="pause"]:not([aria-disabled="true"]),[data-bulk="resume"]')) farmSheetSet(false);"""
            "\n        }, true);") in init
    assert 'if (e.key === "Escape" && farmSheetOn && !bulkDlg) farmSheetSet(false);' in init
    assert "if (!sheetPhone()) farmSheetSet(false);" in init
    # the answer of a farm command: in the sheet when it is open, else a toast; a failure marks «Ферма ▾»
    flash = _js_function("flashBulkMsg")
    assert 'if (msg && sheetPhone() && !farmSheetOn) showToast(msg.t, msg.k !== "bad");' in flash
    assert 'fb.classList.toggle("warn", !!(isAdmin() && bulkUi.failed));' in _js_function("renderFleetBulk")


# --- plan step 15: every popover is a sheet on a phone -----------------------------------------

def _phone899(needle):
    blocks = [b for b in _media("max-width: 899px") if needle in b]
    assert len(blocks) == 1, needle
    return blocks[0]


def test_every_popover_is_a_bottom_sheet_on_a_phone():
    b = _phone899(".pc-pop, .pc-pop[data-kind] {")
    pop = _rule(b, ".pc-pop, .pc-pop[data-kind]")      # [data-kind]: as specific as the kinds' widths
    for need in ("left: 0 !important;", "right: 0;", "top: auto !important;", "bottom: 0;", "width: auto;",
                 "max-width: none;", "max-height: 86dvh;", "padding: 22px 16px calc(16px + env(safe-area-inset-bottom));",
                 "border-radius: 20px 20px 0 0;", "overscroll-behavior: contain;"):
        assert need in pop, need
    assert CSS.index(b) > CSS.index('.pc-pop[data-kind="nozzle"], .pc-pop[data-kind="bed"]')   # later: it wins
    assert "content: \"\"; position: absolute; top: 8px; left: 50%; width: 40px;" in _rule(b, ".pc-pop::before")
    assert '.pc-pop[data-kind="menu"] button { min-height: 44px; }' in b
    # the backdrop: under the sheet, over the page and the bars; a desktop never shows it
    back = re.search(r"\n    \.pc-pop-back \{([^}]*)\}", CSS).group(1)
    z_pop = int(re.search(r"\n    \.pc-pop \{[^}]*?z-index: (\d+);", CSS, re.S).group(1))
    assert "position: fixed;" in back and "inset: 0;" in back and 90 < int(re.search(r"z-index: (\d+);", back).group(1)) < z_pop
    assert ".pc-pop-back[hidden] { display: none; }" in CSS
    # the notifications and 👤: sheets from 899 px down too (were ≤ 640)
    nf = _phone899(".nf-pop, .nf-pop.farm {")
    assert "top: auto !important;" in nf and ".nf-pop-h::before" in nf
    assert 'window.matchMedia("(max-width: 899px)") : null;\n      function nfIsSheet()' in JS
    me = _phone899(".me-back { background: rgba(8, 14, 22, 0.45); }")
    assert "top: auto; left: 0; width: 100%;" in _rule(me, ".me-panel")


POP_HARNESS = r"""
var phone = %(phone)s, log = [], cardUi = { b1: { pop: "ams" } }, pcPopPid = null, pcPopAnchorRect = null;
var pcPop = null, pcPopBack = null;
var window = { matchMedia: function () { return { matches: false }; } };
var TEMP_KINDS = {};
var document = { body: { appendChild: function (el) { log.push(["append", el.className]); } },
  createElement: function () { return { style: {}, dataset: {}, addEventListener: function () {},
    querySelector: function () { return null; } }; } };
function sheetPhone() { return phone; }
function sheetOpened(c) { log.push(["opened", c.name]); }
function sheetClosed(c) { log.push(["closed", c.name]); }
function nfClose() { log.push(["nfClose"]); }
function placePop() {}
function applyAndRender() { log.push(["render"]); }
function onPopClick() {}
%(fns)s
var chip = { getBoundingClientRect: function () { return { left: 0, top: 0, right: 60, bottom: 24 }; } };
openPop(chip, "b1", "<p>x</p>", "ams");
var shown = { display: pcPop.style.display, back: !pcPopBack.hidden, scroll: pcPop.scrollTop };
closePop();
console.log(JSON.stringify({ log: log, shown: shown, closed: { display: pcPop.style.display, back: !pcPopBack.hidden } }));
"""


@pytest.mark.parametrize("phone", [True, False])
def test_the_popover_sheet_has_its_backdrop_and_its_back_entry(phone):
    fns = "\n".join(_js_function(n) for n in ("ensurePop", "closePop", "pcPopClose", "openPop"))
    out = _node(POP_HARNESS % {"phone": json.dumps(phone), "fns": fns})
    appended = [x for x in out["log"] if x[0] == "append"]
    assert appended == [["append", "pc-pop-back"], ["append", "pc-pop"]]         # the backdrop first, under it
    assert ["opened", "pcPopClose"] in out["log"] and ["closed", "pcPopClose"] in out["log"]
    assert out["shown"] == {"display": "block", "back": phone, "scroll": 0}       # a desktop: no backdrop
    assert out["closed"] == {"display": "none", "back": False}


# --- AMS blocks as rows ------------------------------------------------------------------------

def _simple_function(name):
    m = re.search(r"function %s\([^)]*\) \{.*?\n\s*\}" % name, JS, re.S)
    assert m, name
    return m.group(0)


AMS_BLOCK = re.search(r"/\* ams-ui:begin \*/(.*?)/\* ams-ui:end \*/", JS, re.S).group(1)


def ams_row(unit, auto=None):
    lib = "\n".join(_simple_function(n) for n in ("safeText", "icon", "amsUnitName", "amsFmtMin")) + AMS_BLOCK
    return _node(lib + "\nconsole.log(JSON.stringify(amsRowText(%s, %s)));" % (json.dumps(unit), json.dumps(auto)))


DRY_ON = {"type": "n3f", "status": "drying", "active": True, "remaining_min": 571, "temp": 55, "filament": "PETG"}


@pytest.mark.parametrize("unit,auto,want", [
    ({"dry": DRY_ON, "online": True}, {"card": "x", "status": {"kind": "dry", "origin": "auto", "series": 2}}, "🔥 авто · ещё 9:31"),
    ({"dry": DRY_ON, "online": True}, {"card": "x", "status": {"kind": "dry", "origin": "manual"}}, "🔥 вручную · ещё 9:31"),
    ({"dry": DRY_ON, "online": True}, {"card": "x", "status": {"kind": "foreign"}}, "🔥 чужая · ещё 9:31"),
    ({"dry": DRY_ON, "online": True}, None, "🔥 сушка · ещё 9:31"),
    ({"dry_time": 45, "online": True}, None, "🔥 сушка · ещё 45 мин"),               # an AMS without the dry block
    ({"dry": dict(DRY_ON, remaining_min=0), "online": True}, None, "🔥 сушка"),
    ({"dry": {"status": "off", "active": False}, "online": True, "maintenance": True}, None, "на обслуживании"),
    ({"dry": {"status": "off", "active": False}, "online": False}, None, "нет связи"),
    ({"dry": {"status": "off", "active": False}, "online": True}, {"card": "ждёт · 7% (старт от 10%)", "status": {}},
     "ждёт · 7% (старт от 10%)"),
    ({"online": True}, None, "покой"),
    (None, None, ""),
])
def test_an_ams_row_says_its_drying_short(unit, auto, want):
    assert ams_row(unit, auto) == want


def test_an_ams_row_text_is_escaped_where_it_comes_from_the_printer():
    out = ams_row({"dry": {"status": "<b>x</b>", "active": False}, "online": True})
    assert "<b>" not in out and out == "сушка выключена"            # an unknown status: our word, never the raw one
    assert "safeText(" in _js_function("amsRowText", AMS_BLOCK)


def test_the_ams_card_has_its_row_on_a_phone():
    shell = _js_function("amsCardShell")
    assert "var open = !!amsRowOpen[key];" in shell
    assert ("'<button type=\"button\" class=\"ams-row' + (dryActive(u) ? ' drying' : '') + '\" data-ams-row=\"' + key +"
            in shell)
    assert "'\" aria-expanded=\"' + (open ? 'true' : 'false') + '\"><span class=\"ams-row-n\">' + escHtml(u.label) +" in shell
    assert "(multi ? ' · ' + amsUnitName(u.unit_index) : '')" in shell and "amsRowText(u, auto)" in shell
    assert "return row + '<div class=\"ams-card' + cls.slice(8) + (open ? '' : ' ph-closed') + '\" data-key=\"' + key" in shell
    load = _js_function("loadAms")
    assert "return amsCardShell(u, perPrinter[u.printer_id] > 1);" in load        # «H1 · AMS 2» when it has two
    click = JS[JS.index("if (grid) grid.addEventListener('click', function (e) {"):]
    assert click.index("var row = e.target.closest('[data-ams-row]');") < click.index("[data-ams-dry-open],[data-ams-dry-stop]")
    assert "amsRowSet(row.dataset.amsRow, row.getAttribute('aria-expanded') !== 'true'); return;" in click
    assert 'if (view === "ams") amsRowSet(null);' in _js_function("phoneRetap")
    b = _phone899(".ams-row {")
    assert ".ams-card.ph-closed { display: none; }" in b and "min-height: 60px;" in _rule(b, ".ams-row")
    assert ".ams-grid { grid-template-columns: minmax(0, 1fr); gap: 8px; }" in b
    assert ".ams-row { display: none; }" in CSS                                   # a desktop: the blocks as they are


ROWSET_HARNESS = r"""
function el(cls) { return { cls: cls.split(" "), attrs: {}, dataset: {},
  classList: { contains: function (c) { return this.o.cls.indexOf(c) >= 0; },
    toggle: function (c, v) { var i = this.o.cls.indexOf(c); if (v && i < 0) this.o.cls.push(c); if (!v && i >= 0) this.o.cls.splice(i, 1); } },
  setAttribute: function (k, v) { this.attrs[k] = v; }, getAttribute: function (k) { return this.attrs[k]; } }; }
var rows = ["a__0", "b__0"].map(function (k) {
  var r = el("ams-row"), c = el("ams-card ph-closed");
  r.classList.o = r; c.classList.o = c; r.dataset.amsRow = k; r.nextElementSibling = c; return r; });
var document = { querySelectorAll: function () { return rows; } };
var amsRowOpen = {};
%(fn)s
function state() { return rows.map(function (r) { return [r.attrs["aria-expanded"], r.nextElementSibling.cls.indexOf("ph-closed") >= 0]; }); }
var out = [];
amsRowSet("a__0", true); out.push(state());
amsRowSet("b__0", true); out.push(state());
amsRowSet("a__0", false); out.push(state());
amsRowSet(null); out.push(state(), Object.keys(amsRowOpen));
console.log(JSON.stringify(out));
"""


def test_a_row_unfolds_its_block_in_place():
    out = _node(ROWSET_HARNESS % {"fn": _js_function("amsRowSet")})
    assert out == [[["true", False], ["false", True]], [["true", False], ["true", False]],
                   [["false", True], ["true", False]], [["false", True], ["false", True]], []]


# --- «Админ» tables as cards -------------------------------------------------------------------

def test_the_admin_tables_are_cards_on_a_phone():
    rend = _js_function("renderAdminPrinters")
    for need in ("'<td class=\"prk-name\">'", "'<td class=\"dim prk-model\" data-l=\"модель\">'", "'<td class=\"prk-kind\">'",
                 "'<td class=\"prk-ip\">'", "'<td class=\"prk-fw\" data-l=\"прошивка\">'",
                 "'<td class=\"prk-maint\" data-l=\"обслуживание\">'", "'<td class=\"prk-ctl\" data-l=\"управление\">'",
                 "'<td class=\"adm-actions\">'"):
        assert need in rend, need
    b = _phone899("#adminPrintersTable thead, #adminUsersTable thead { display: none; }")
    # no sideways scroll (IA §10): only the wraps whose tables are cards drop their own scroll — another
    # table (none in «Админ» now) keeps it instead of being cut off by its panel
    assert ".adm-table-wrap.adm-cards { overflow: visible; }" in b
    assert ".adm-table-wrap { overflow: visible; }" not in CSS.replace(".autodry .ad-table-wrap", "")
    wraps = re.findall(r'<div class="adm-table-wrap([^"]*)"[^>]*>\s*<table class="([^"]*)"(?: id="(\w+)")?', HTML)
    assert sorted(t for c, _, t in wraps if "adm-cards" in c) == ["adminPrintersTable", "adminUsersTable", "proxyTable"]
    assert all(c.strip() in ("jobs-table-wrap", "acts-table-wrap", "ad-table-wrap") for c, _, t in wraps if "adm-cards" not in c)
    # «Система → Прокси для Telegram» (the bot on): the address on its line, «Удалить» across the card
    assert "#proxyTable thead { display: none; }" in b
    assert "display: flex; flex-wrap: wrap;" in _rule(b, "#proxyTable tr")
    assert "#proxyTable td.proxy-url, #proxyTable td.adm-actions, #proxyTable td[colspan] { flex: 1 1 100%; }" in b
    assert "#proxyTable td.adm-actions > .ghost-btn { width: 100%; min-height: 40px; }" in b
    prox = _js_function("renderProxies")
    assert '<td class="mono dim proxy-url">' in prox and "'<td class=\"adm-actions\">' + acts + '</td></tr>'" in prox
    assert "display: grid;" in _rule(b, "#adminPrintersTable tr, #adminUsersTable tr")
    assert '#adminPrintersTable td[data-l]::before, #adminUsersTable td.ll-cell::before {' in b
    assert '#adminUsersTable td.ll-cell::before { content: "вход: "; }' in b
    assert "#adminPrintersTable td.prk-kind, #adminPrintersTable td.prk-ip { display: none; }" in b
    assert "#adminPrintersTable td.prk-ctl, #adminPrintersTable td.adm-actions, #adminUsersTable td.adm-actions," in b
    # «Автоматика → Блоки» are cards up to 899 px too (were ≤ 760)
    assert "@media (max-width: 899px) {\n      .autodry .ad-table-wrap { overflow: visible; }" in CSS
    assert "@media (max-width: 760px) {\n      .autodry" not in CSS



def test_the_park_fits_a_narrow_pc():
    # IA §8: ~700 px «на ПК без прокрутки вбок» — from 900 px up the maintenance cell wraps
    # («к карточке ›» under the badge), at ≤ 1100 px «Тип» goes, at ≤ 1000 px «Изменить / Удалить» stack
    # (measured live: the wrap's scrollWidth = its clientWidth at 900, 1024, 1101 and 1280 px)
    assert ".prk-maint { white-space: nowrap; }" not in CSS and ".prk-maint .badge { white-space: nowrap; }" in CSS
    wide = [b for b in _media("min-width: 900px") if "td.prk-maint .adm-link" in b]
    assert len(wide) == 1 and "display: block;" in _rule(wide[0], "#adminPrintersTable td.prk-maint .adm-link")
    narrow = [b for b in _media("max-width: 1100px") if "#adminPrintersTable" in b]
    assert len(narrow) == 1
    assert '#adminPrintersTable th[data-sort="kind"], #adminPrintersTable td.prk-kind { display: none; }' in narrow[0]
    stack = [b for b in _media("max-width: 1000px") if "#adminPrintersTable" in b]
    assert len(stack) == 1 and "#adminPrintersTable td.adm-actions { white-space: normal; }" in stack[0]
    assert '<th class="sortable" data-sort="kind">Тип</th>' in HTML


# --- «Хронология»: a pinch and a tap -----------------------------------------------------------

def test_the_chronology_takes_a_finger():
    assert "function tlLabelW() { return sheetPhone() ? 64 : 200; }" in JS
    draw = _js_function("drawTimeline")
    assert "var labelW = tlLabelW();" in draw and "var labelW = 200;" not in draw
    assert "ctx.measureText(dn).width > labelW - 26" in draw                       # a phone's names fit the column
    assert "var labelW = 200;" not in JS and "- 200 - 10" not in JS and "var lw3 = 200;" not in JS
    tl = JS[JS.index("// Zoom-to-cursor"):JS.index("/* ── Toast")]
    assert "tlTipAt(e.clientX, e.clientY);" in tl and "function tlTipAt(clientX, clientY) {" in tl
    down = tl[tl.index("canvas.addEventListener('pointerdown'"):tl.index("canvas.addEventListener('pointermove'")]
    assert "if (e.pointerType !== 'touch') return;" in down and "e.preventDefault();" in down   # the mouse keeps its handlers
    assert "pinch = { d: Math.abs(a.x - b.x) || 1, s: viewStart, e: viewEnd, f: tFrac((a.x + b.x) / 2) };" in down
    move = tl[tl.index("canvas.addEventListener('pointermove'"):tl.index("function tEnd(e)")]
    assert "var ns = Math.max(TIMELINE_MIN_SPAN, Math.min(span0 * pinch.d / d, b.hi - b.lo));" in move
    assert "clampView();\n          drawTimeline();" in move
    end = tl[tl.index("function tEnd(e)"):]
    assert "if (now - lastTap < 350 && timelineData.length) {" in end                 # a double tap resets
    assert "if (!tlTipAt(t.x, t.y)) tip.style.display = 'none';" in end              # a tap shows the tooltip
    assert "canvas.addEventListener('pointerup', tEnd);" in end and "canvas.addEventListener('pointercancel', tEnd);" in end
    assert "#timeline-canvas { touch-action: pan-y; }" in _phone899("#timeline-canvas { touch-action: pan-y; }")
    assert "На телефоне: щипок — масштаб, палец вбок — сдвиг, тап — подробности, двойной тап — сброс." in HTML


# --- a browser push opens its printer ----------------------------------------------------------

PUSH_HARNESS = r"""
var log = [], made = [];
function Notification(title, o) { this.title = title; this.o = o; made.push(this); this.close = function () { log.push(["close"]); }; }
Notification.permission = "granted";
var window = { Notification: Notification, focus: function () { log.push(["focus"]); } };
function pushOpen(pid, notes) { log.push(["open", pid, notes]); }
%(fn)s
showBrowserNotification("t", "b", "bambu-3", true);
showBrowserNotification("t", "b");
made[0].onclick();
console.log(JSON.stringify({ log: log, n: made.length, second: typeof made[1].onclick }));
"""

OPEN_HARNESS = r"""
var log = [], known = %(known)s;
function sheetCloseAll() { log.push(["sheets"]); }
function closePop(n) { log.push(["pop", n]); }
function findPrinter(pid) { return known ? { id: pid } : null; }
function setActiveView(v) { log.push(["view", v]); }
function nfGoto(pid, cardOnly) { log.push(["goto", pid, cardOnly]); }
%(fn)s
pushOpen("bambu-3", %(notes)s);
console.log(JSON.stringify(log));
"""


def test_a_push_opens_its_printer():
    fn = _js_function("showBrowserNotification").replace('("Notification" in window)', "true")
    out = _node(PUSH_HARNESS % {"fn": fn})
    assert out == {"log": [["focus"], ["close"], ["open", "bambu-3", True]], "n": 2, "second": "undefined"}
    op = _js_function("pushOpen")
    run = lambda known, notes: _node(OPEN_HARNESS % {"fn": op, "known": json.dumps(known), "notes": json.dumps(notes)})
    assert run(True, True) == [["sheets"], ["pop", True], ["goto", "bambu-3", False]]     # with its notifications
    assert run(True, False) == [["sheets"], ["pop", True], ["goto", "bambu-3", True]]     # a finish: the card only
    assert run(False, True) == [["sheets"], ["pop", True], ["view", "fleet"]]             # gone from the farm
    load = _js_function("load")
    assert 'showBrowserNotification("✅ Печать завершена", p.label, p.id, false);' in load
    assert 'showBrowserNotification("🔴 Ошибка принтера", p.label + (why ? ": " + why : ""), p.id, true);' in load
    assert 'showBrowserNotification("⚠️ Пауза печати", p.label + (why ? ": " + why : ""), p.id, !!why);' in load
    goto = _js_function("nfGoto")
    assert goto.index('card.classList.add("nf-flash");') < goto.index("if (cardOnly) return;") < goto.index("setTimeout(")


def test_cache_bust_bumped():
    assert int(re.search(r"styles\.css\?v=(\d+)", HTML).group(1)) >= 99
    assert int(re.search(r"app\.js\?v=(\d+)", HTML).group(1)) >= 116
