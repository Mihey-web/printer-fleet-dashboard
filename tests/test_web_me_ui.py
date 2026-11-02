"""👤 «Этот браузер» and «Админ» (IA §8, §9; plan step 3).

«Настройки» are gone: what acts in this browser only (the looks, the browser's
notifications, «Выйти», the version) moved into the 👤 panel of the header,
the server's settings into «Админ → Система»; «Администрирование» became
«Админ» with the sub-tabs Парк · Доступ · Система, each loading its own data.
The login page loads theme.js (a public exact route, like the PWA files) and
shows the theme this browser chose. The DOM-free bits run in node with stubs;
skipped without node.
"""
import json
import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import main as api_main

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "app" / "web"
JS = (WEB / "app.js").read_text(encoding="utf-8")
HTML = (WEB / "index.html").read_text(encoding="utf-8")
CSS = (WEB / "styles.css").read_text(encoding="utf-8")
LOGIN = (WEB / "login.html").read_text(encoding="utf-8")
THEME_JS = (WEB / "theme.js").read_text(encoding="utf-8")
THEMES = ["", "dark-classic", "light", "night", "cosmos", "amber", "glass"]


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


def _between(html, start, end):
    i = html.index(start)
    return html[i:html.index(end, i)]


PANEL = _between(HTML, '<aside class="me-panel" id="mePanel"', "</aside>")
ADMIN = _between(HTML, '<section class="view-panel" data-panel="admin">', "\n    </section>\n  </div>")


def _ids(html):
    return [a["id"] for t, a in tags(html) if "id" in a]


# --- the header ------------------------------------------------------------------------------

def test_header_has_the_me_button_and_no_settings_tab():
    head = _between(HTML, '<header class="topbar">', "</header>")
    btn = [(t, a) for t, a in tags(head) if a.get("id") == "meBtn"]
    assert len(btn) == 1
    t, a = btn[0]
    assert t == "button" and a["class"] == "me-btn"            # not a .view-tab: it opens no panel
    assert a["aria-haspopup"] == "dialog" and a["aria-controls"] == "mePanel" and a["aria-expanded"] == "false"
    assert a["data-tip"] and "title" not in a
    assert '<use href="#i-user"></use>' in head and '<symbol id="i-user"' in HTML
    assert head.index('id="liveChip"') < head.index('id="meBtn"')   # the last item on the right
    for gone in ("settingsTab", "settings-tab", 'data-view="settings"', "⚙ Настройки", "settings-tab-gear"):
        assert gone not in HTML, gone
    tabs = [a for t, a in tags(head) if "data-view" in a]
    # «Окно» is the «Финиши» window on «Принтеры» now (plan step 7), «Сводка» is gone (plan step 8);
    # IA §4's order on a desktop too, as the approved mockup: Принтеры · AMS · История · Админ (plan step 16)
    assert [a["data-view"] for a in tabs] == ["fleet", "ams", "history", "admin"]
    admin = [x for x in tags(head) if x[1].get("id") == "adminTab"][0][1]
    assert "title" not in admin and ">Админ</button>" in head and "Администрирование" not in HTML
    assert "bindTips(document);" in JS          # one binding for the page (plan step 14), the header in it


ME_HARNESS = r"""
function el() { return { textContent: "", className: "", children: [], cls: {},
  classList: { toggle: function (c, v) { this.o.cls[c] = !!v; }, o: null },
  appendChild: function (c) { this.children.push(c); return c; } }; }
var els = { meRole: el(), meBtn: el(), meWho: el() };
Object.keys(els).forEach(function (k) { els[k].classList.o = els[k]; });
var document = {
  getElementById: function (id) { return els[id] || null; },
  createElement: function (t) { var e = el(); e.tag = t; e.classList.o = e; return e; },
  createTextNode: function (s) { return { text: s }; }
};
var currentUser = %(user)s;
function isAdmin() { return currentUser && currentUser.role === 'admin'; }
%(fn)s
renderMeWho();
console.log(JSON.stringify({ role: els.meRole.textContent, viewer: !!els.meBtn.cls.viewer,
  who: els.meWho.children.map(function (c) { return c.text != null ? c.text : "<" + c.tag + (c.className ? "." + c.className : "") + ">" + c.textContent; }) }));
"""


@pytest.mark.parametrize("user,want", [
    ({"username": "alice", "role": "admin"}, {"role": "Админ", "viewer": False,
                                               "who": ["<span.me-login>alice", " · ", "<b>Админ"]}),
    ({"username": "guest", "role": "viewer"}, {"role": "Зритель", "viewer": True,
                                                "who": ["<span.me-login>guest", " · ", "<b>Зритель"]}),
    (None, {"role": "—", "viewer": False, "who": ["<b>—"]}),
    ({}, {"role": "—", "viewer": False, "who": ["<b>—"]}),           # a refresh without the user in the answer
])
def test_the_role_in_words(user, want):
    out = _node(ME_HARNESS % {"user": json.dumps(user), "fn": _js_function("renderMeWho")})
    assert out == want
    assert "renderMeWho();" in _js_function("updateAuthUI")
    # the login is text, never markup
    assert "innerHTML" not in _js_function("renderMeWho")


# --- the 👤 panel ------------------------------------------------------------------------------

def test_the_panel_holds_what_acts_in_this_browser():
    ids = _ids(PANEL)
    # moved from «Настройки» with their ids, so their handlers stay as they were
    for i in ("logoutBtn", "themeSegment", "textScaleRange", "textScaleValue", "settingsList",
              "notificationsEnabledToggle", "notificationsFinishToggle", "notificationsErrorToggle",
              "notificationsPausedToggle", "appVersion"):
        assert ids.count(i) == 1 and _ids(HTML).count(i) == 1, i
    # «Выйти» at the top, the version at the bottom (IA §9)
    assert PANEL.index('id="logoutBtn"') < PANEL.index('id="themeSegment"') < PANEL.index('id="settingsList"') \
        < PANEL.index('id="meInstallSec"') < PANEL.index('id="appVersion"')
    assert [a.get("data-theme") for t, a in tags(PANEL) if "theme-seg-btn" in a.get("class", "")] == THEMES
    # nothing server-side, no way into the auto-dry
    for bad in ("tg", "proxy", "adm", "autodry", "prn", "Telegram", "Автосушка"):
        assert not [i for i in ids if i.startswith(bad)] and bad not in PANEL.replace("me-", ""), bad
    head = [a for t, a in tags(PANEL) if t == "aside"][0]
    assert head["role"] == "dialog" and head["aria-labelledby"] == "meTitle" and "hidden" in head
    assert '<div class="me-back" id="meBack" hidden></div>' in HTML
    # outside the shell: the panel covers the page, not a part of it
    assert HTML.index('id="mePanel"') > HTML.index("</header>") and HTML.index('id="mePanel"') < HTML.index(
        '<script src="/static/app.js')
    assert HTML.index('id="mePanel"') > HTML.rindex('data-panel="admin"')


def test_the_panel_opens_and_closes():
    fn = JS[JS.index("(function initMePanel() {"):]
    fn = fn[:fn.index("})();") + 5]
    for need in ('btn.addEventListener("click", function () { setOpen(panel.hidden); });',
                 'back.addEventListener("click", function () { setOpen(false); });',
                 'if (close) close.addEventListener("click", function () { setOpen(false); });',
                 'e.key === "Escape" && !panel.hidden',
                 'btn.setAttribute("aria-expanded", open ? "true" : "false");',
                 "panel.hidden = !open;", "back.hidden = !open;"):
        assert need in fn, need
    # the focus returns to the button: read before hiding moves it to <body>
    assert fn.index("var inside = panel.contains(document.activeElement);") < fn.index("panel.hidden = !open;")
    assert "else if (inside) btn.focus({ preventScroll: true });" in fn


def test_install_only_while_the_browser_offers_it():
    fn = JS[JS.index("(function initInstall() {"):]
    fn = fn[:fn.index("})();") + 5]
    assert 'window.addEventListener("beforeinstallprompt"' in fn and "sec.hidden = false;" in fn
    assert 'window.addEventListener("appinstalled"' in fn
    assert fn.index("offer = null;") < fn.index("e.prompt()")                 # one prompt per offer
    assert '<div class="me-sec" id="meInstallSec" hidden>' in PANEL           # hidden until then


def test_notifications_master_dims_its_three_kinds():
    css = CSS[CSS.index("/* === 👤 «Этот браузер» and «Админ»"):]
    assert ".me-panel .settings-list.master-off .me-subs { opacity: 0.48; }" in css
    subs = _between(PANEL, '<div class="me-subs">', "</div>")
    assert [a["id"] for t, a in tags(subs) if t == "input"] == [
        "notificationsFinishToggle", "notificationsErrorToggle", "notificationsPausedToggle"]
    assert all(a["type"] == "checkbox" for t, a in tags(subs) if t == "input")
    assert 'settingsList.classList.toggle("master-off", !settings.enabled);' in JS


def test_panel_css_is_opaque_and_above_the_popovers():
    css = CSS[CSS.index("/* === 👤 «Этот браузер» and «Админ»"):]
    panel = re.search(r"\n    \.me-panel \{(.*?)\}", css, re.S).group(1)
    assert "background: var(--bg-elev);" in panel and "width: min(360px, 100vw);" in panel
    assert "right: 0;" in panel and "overflow-y: auto;" in panel
    z = {k: int(v) for k, v in re.findall(r"\n    \.(me-back|me-panel) \{[^}]*?z-index: (\d+);", css)}
    pop = int(re.search(r"\n    \.nf-pop \{[^}]*?z-index: (\d+);", CSS).group(1))
    assert pop < z["me-back"] < z["me-panel"]
    # every theme's --bg-elev is opaque (the panel and the admin head sit on it)
    for value in re.findall(r"^\s*--bg-elev:\s*([^;]+);", CSS, re.M):
        assert re.fullmatch(r"#[0-9a-fA-F]{6}", value.strip()), value
    # a bottom sheet on a phone — ≤ 899 px since plan step 15 (deliberately changed, was ≤ 640: every
    # popover is a sheet there); the header button is the icon in the phone's bar (plan step 10)
    phone = css[css.index("@media (max-width: 899px) {"):]
    assert "top: auto; left: 0; width: 100%;" in phone and "env(safe-area-inset-bottom)" in phone
    assert ".me-btn span { display: none; }" in phone
    # the old tab's rules went with it
    assert ".settings-tab" not in CSS and 'data-panel="settings"' not in CSS


# --- «Админ» ------------------------------------------------------------------------------------

def test_admin_sub_tabs_and_their_panes():
    subs = [(a["data-asub"], a.get("class", ""), a["aria-pressed"]) for t, a in tags(ADMIN) if "data-asub" in a]
    # «Автоматика» between «Парк» and «Доступ» (plan step 11)
    assert subs == [("park", "active", "true"), ("auto", "", "false"), ("access", "", "false"), ("system", "", "false")]
    assert re.findall(r'data-asub="\w+"[^>]*>([^<]+)<', ADMIN) == ["Парк", "Автоматика", "Доступ", "Система"]
    panes = [(a["data-asub-panel"], "hidden" in a) for t, a in tags(ADMIN) if "data-asub-panel" in a]
    assert panes == [("park", False), ("auto", True), ("access", True), ("system", True)]

    def pane(name):
        return _between(ADMIN, 'data-asub-panel="%s"' % name, "\n      </div>")

    # the same ids as before, so the handlers keep working
    for i in ("adminPrintersTable", "addPrinterBtn", "prnForm", "prnSaveBtn"):
        assert 'id="%s"' % i in pane("park"), i
    for i in ("adminUsersTable", "addUserBtn", "adminError"):
        assert 'id="%s"' % i in pane("access"), i
    # the audit moved to «История → Действия» (plan step 12)
    assert "adminAudit" not in HTML and "loadAdminAudit" not in JS and "Входы и действия — «История → Действия»" in pane("access")
    for i in ("tgSection", "tgEnabledToggle", "tgToken", "tgStatusLine", "proxySection", "proxyTable"):
        assert 'id="%s"' % i in pane("system"), i
    # the «неприменённые изменения» bar: in the sticky head, above every pane
    head = _between(ADMIN, '<div class="adm-head">', 'data-asub-panel="park"')
    assert 'id="printersApplyBar"' in head and 'id="adminSub"' in head
    assert head.index('id="adminSub"') < head.index('id="printersApplyBar"')
    assert "settings-admin" not in HTML and "settings-admin" not in JS and "admin-chip\">админ" not in HTML


def test_telegram_is_folded_with_its_switch_in_sight():
    tg = _between(ADMIN, 'id="tgSection"', "</details>")
    det = tg.index('<details class="adm-det" id="tgDetails">')
    assert '<details class="adm-det" id="tgDetails">' in tg and " open" not in tg[det:tg.index(">", det)]
    # the switch and its status are outside the folded part (owner 29.10: on / off from here)
    assert tg.index('id="tgEnabledToggle"') < det and tg.index('id="tgStatusLine"') < det
    for i in ("tgToken", "tgChatId", "tgUpdateInterval", "tgTplFinish", "proxySection", "proxyOffHint"):
        assert tg.index('id="%s"' % i) > det, i
    assert 'aria-label="Телеграм-бот включён"' in tg


FILL_HARNESS = r"""
var els = {};
function srvEl(id) { return els[id] || (els[id] = { id: id, hidden: null, checked: false, value: "",
  classList: { toggle: function () {} } }); }
var document = { activeElement: null };
var srvValues = null;
function renderTgStatus() {} function renderProxies() {}
%(fn)s
fillServerSettings({ telegram_enabled: %(on)s, telegram_chat_id: null, telegram_token: "x" }, {});
console.log(JSON.stringify({ proxies: els.proxySection.hidden, hint: els.proxyOffHint.hidden,
                             toggle: els.tgEnabledToggle.checked }));
"""


@pytest.mark.parametrize("on", [True, False])
def test_the_proxies_show_only_with_the_bot_on(on):
    out = _node(FILL_HARNESS % {"fn": _js_function("fillServerSettings"), "on": json.dumps(on)})
    assert out == {"proxies": not on, "hint": on, "toggle": on}


SUB_HARNESS = r"""
function btn(s) { return { dataset: { asub: s }, cls: {}, attrs: {},
  classList: { toggle: function (c, v) { btn.last = this; } }, setAttribute: function (k, v) { this.attrs[k] = v; } }; }
var buttons = ["park", "access", "system"].map(function (s) {
  var b = btn(s); b.classList.toggle = function (c, v) { b.cls[c] = !!v; }; return b; });
var panes = ["park", "access", "system"].map(function (s) { return { dataset: { asubPanel: s }, hidden: false }; });
var document = { querySelectorAll: function (q) { return q.indexOf("data-asub-panel") >= 0 ? panes : buttons; } };
%(route)s
var adminSub = "park";
var scrNames = 0;
function renderScrName() { scrNames++; }   // the phone bar's screen name (plan step 10, tests/test_web_phone_ui.py)
%(fn)s
var out = [];
%(steps)s
console.log(JSON.stringify(out));
"""


def test_set_admin_sub_shows_one_pane():
    route = re.search(r"/\* route:begin \*/(.*?)/\* route:end \*/", JS, re.S).group(1)
    steps = """
    ["access", "system", "bogus", "__proto__", "park"].forEach(function (s) {
      setAdminSub(s);
      out.push([adminSub, buttons.filter(function (b) { return b.cls.active; }).map(function (b) { return b.dataset.asub; }),
                buttons.map(function (b) { return b.attrs["aria-pressed"]; }).join(","),
                panes.filter(function (p) { return !p.hidden; }).map(function (p) { return p.dataset.asubPanel; })]);
    });
    """
    out = _node(SUB_HARNESS % {"route": route, "fn": _js_function("setAdminSub"), "steps": steps})
    assert out == [
        ["access", ["access"], "false,true,false", ["access"]],
        ["system", ["system"], "false,false,true", ["system"]],
        ["park", ["park"], "true,false,false", ["park"]],            # unknown: «Парк»
        ["park", ["park"], "true,false,false", ["park"]],
        ["park", ["park"], "true,false,false", ["park"]],
    ]


def test_each_sub_tab_loads_its_own_data():
    body = _js_function("adminLoadSub")
    assert body.index("loadAdminPrinters();") < body.index('if (adminSub === "access")')   # the bar: always
    assert 'if (adminSub === "access") loadAdminUsers();' in body          # the audit: «История → Действия»
    assert 'if (adminSub === "system") loadServerSettings();' in body
    # opening «Админ»: in a microtask (AUDIT_LIMIT is assigned further down the IIFE)
    view = _js_function("setActiveView")
    assert "if (view === 'admin') {\n          Promise.resolve().then(function () { adminLoadSub(); });" in view
    assert "loadServerSettings" not in view and "'settings'" not in view
    # a sub-tab click: the sub-tab, one history entry, its data
    click = JS[JS.index("(function initAdminSub() {"):]
    click = click[:click.index("})();")]
    assert click.index("setAdminSub(b.dataset.asub);") < click.index("routeSync(true);") < \
        click.index("adminLoadSub();")
    # the role arriving late (any sub-tab, «Система» too) loads its data then: adminLoadWaited (the run below)
    auth = _js_function("updateAuthUI")
    assert "adminLoadWaited();" in auth and "loadServerSettings" not in auth and "loadAutoIgnore" not in auth
    assert "view === 'settings'" not in JS


ADMIN_LOAD_HARNESS = r"""
var hits = [], errors = [], ticks = [];
var console = { error: function (a, e) { errors.push(String(a) + " " + String(e)); },
                log: function (s) { process.stdout.write(s + "\n"); } };
var currentUser = null;
function isAdmin() { return currentUser && currentUser.role === 'admin'; }
function apiFetch(url) {
  hits.push(url);
  return Promise.resolve({ ok: !!isAdmin(), status: isAdmin() ? 200 : 403,
                           json: function () { return Promise.resolve(url.indexOf('/printers') >= 0 ? [] : { values: {} }); } });
}
var panelActive = true;
var document = { querySelector: function (q) {
  if (q.indexOf('data-panel="admin"') >= 0) return { classList: { contains: function () { return panelActive; } } };
  return {};   // the tbody
} };
var prnLightOff = [], prnRows = [], prnNeedLive = false, prnOpenAfterLoad = null, cachedAllPrinters = [];
function renderAdminPrinters() {}
function fwCmp() { return 0; }
function loadAutodry() { hits.push("/api/ams/autodry"); }
function loadAutoIgnore() { if (isAdmin()) hits.push("/api/admin/settings#codes"); }
function loadAdminUsers() { if (isAdmin()) hits.push("/api/admin/users"); }
function loadServerSettings() { if (isAdmin()) hits.push("/api/admin/settings#system"); }
var adminSub = %(sub)s;
%(fns)s
var adminLoadWaits = false;
function tick() { return new Promise(function (r) { setTimeout(r, 0); }); }
(async function () {
  var out = {};
  adminLoadSub();                                   // #/admin/<sub> before /api/auth/me answered
  await tick();
  out.before = hits.slice(); hits.length = 0;
  currentUser = { role: %(role)s };                 // /api/auth/me
  adminLoadWaited();
  await tick();
  out.after = hits.slice(); hits.length = 0;
  adminLoadWaited();                                // a later token refresh: nothing again
  await tick();
  out.again = hits.slice(); hits.length = 0;
  adminLoadSub();                                   // a sub-tab click with the role known
  await tick();
  out.click = hits.slice();
  out.errors = errors;
  console.log(JSON.stringify(out));
})();
"""


@pytest.mark.parametrize("role,sub,after", [
    ("admin", "park", ["/api/admin/settings", "/api/admin/printers"]),
    ("admin", "system", ["/api/admin/settings", "/api/admin/settings#system", "/api/admin/printers"]),
    ("admin", "auto", ["/api/admin/settings", "/api/ams/autodry", "/api/admin/settings#codes", "/api/admin/printers"]),
    ("viewer", "park", []),
    ("viewer", "system", []),
])
def test_no_admin_request_before_the_role(role, sub, after):
    # the review of 31.10: a viewer on a shared #/admin/<sub> link fired /api/admin/* (403s and a
    # console error) before the router moved them; an admin's reload still fills the sub-tab, once
    assert "async function loadAdminPrinters() {" in JS
    fns = "\n".join([_js_function("adminLoadSub"), _js_function("adminLoadWaited"),
                     "async " + _js_function("loadAdminPrinters")])
    out = _node(ADMIN_LOAD_HARNESS % {"sub": json.dumps(sub), "role": json.dumps(role), "fns": fns})
    assert out["before"] == [] and out["errors"] == []
    assert sorted(out["after"]) == sorted(after)
    assert out["again"] == []
    assert sorted(out["click"]) == sorted(after)


def test_admin_head_is_sticky_and_opaque():
    css = CSS[CSS.index("/* === 👤 «Этот браузер» and «Админ»"):]
    head = re.search(r"\n    \.adm-head \{(.*?)\}", css, re.S).group(1)
    assert "position: sticky;" in head and "background-color: var(--bg-elev);" in head
    desk = css[css.index("@media (min-width: 901px) {"):]
    assert ".adm-head { top: 48px;" in desk                       # under the 48 px sticky header
    assert ".adm-pane[hidden] { display: none; }" in css
    active = re.search(r"\.adm-seg button\.active \{([^}]*)\}", css).group(1)
    assert "background-color: var(--bg-elev);" in active and "linear-gradient(var(--info-soft), var(--info-soft))" in active


# --- the login page: the theme of this browser ---------------------------------------------------

@pytest.fixture
def client(monkeypatch):
    import config as cfg
    monkeypatch.setattr(cfg, "JWT_SECRET", getattr(cfg, "JWT_SECRET", ""), raising=False)
    return TestClient(api_main.create_app(type("S", (), {"get_all": lambda self: []})()))


def test_theme_js_is_public_on_its_own_route(client):
    assert api_main.LOGIN_FILES == {"/theme.js": ("app/web/theme.js", "text/javascript", "no-cache"),
                                    "/night-sky.jpg": ("app/web/night-sky.jpg", "image/jpeg", "public, max-age=604800")}
    assert "/theme.js" in api_main.PUBLIC_EXACT and "/night-sky.jpg" in api_main.PUBLIC_EXACT
    assert api_main.PUBLIC_EXACT[:2] == ("/api/health", "/favicon.ico")
    assert not [p for p in api_main.PUBLIC_EXACT if p.startswith("/static")]
    r = client.get("/theme.js?v=5")
    assert r.status_code == 200 and r.headers["content-type"].split(";")[0] == "text/javascript"
    assert r.headers["cache-control"] == "no-cache" and r.content == (WEB / "theme.js").read_bytes()
    # the night sky of «Космос» and «Стекло»: the login page shows it before any session
    r = client.get("/night-sky.jpg")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg" and r.content[:2] == b"\xff\xd8"
    assert len(r.content) < 400_000
    # the rest stays closed to a visitor
    for path in ("/static/theme.js", "/static/app.js", "/theme.js/../static/app.js", "/static/styles.css"):
        assert client.get(path).status_code in (401, 404), path


def test_theme_js_reveals_nothing():
    # public now: only the theme boot and the colour getters, no farm, no API
    for bad in ("fetch(", "/api/", "XMLHttpRequest", "printer", "currentUser"):
        assert bad not in THEME_JS, bad
    assert set(re.findall(r"^function (\w+)", THEME_JS, re.M)) == {"applyGlassPhoto", "getStateColors", "getTimelineColors"}


def test_login_page_loads_the_theme():
    head = LOGIN.split("</head>", 1)[0]
    m = re.search(r'<script src="/theme\.js\?v=(\d+)"></script>', head)
    assert m, "login.html does not load /theme.js"
    # the same file version as the app's
    assert m.group(1) == re.search(r'/static/theme\.js\?v=(\d+)', HTML).group(1)
    # after its tokens (theme.js reads --bg for the theme-color), the head still the PWA one
    assert head.index("</style>") < head.index('<script src="/theme.js')
    assert '<meta name="theme-color" content="#061723">' in head
    css = head[head.index("<style>"):head.index("</style>")]
    # no colours of its own outside the token blocks
    rules = re.sub(r'(:root|html\[data-theme="[\w-]+"\]) \{[^}]*\}', "", css)
    assert not re.findall(r"#[0-9a-fA-F]{3,6}\b", rules)


def _block(src, sel):
    i = src.index(sel + " {")
    j, depth = src.index("{", i), 0
    for k in range(j, len(src)):
        depth += {"{": 1, "}": -1}.get(src[k], 0)
        if depth == 0:
            return src[j + 1:k]


def _decls(body):
    body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
    return {m.group(1): m.group(2).strip() for m in re.finditer(r"(--[\w-]+)\s*:\s*([^;]+);", body)}


def _resolve(d, name, seen=()):
    def sub(m):
        ref, fb = m.group(1), m.group(2)
        if ref in d and ref not in seen:
            return _resolve(d, ref, seen + (name,))
        return fb.strip() if fb else m.group(0)
    return re.sub(r"var\((--[\w-]+)(?:,\s*([^)]+))?\)", sub, d[name])


@pytest.mark.parametrize("theme", THEMES)
def test_login_tokens_are_the_apps(theme):
    # the login page has no session, so no styles.css: its tokens must be the app's, theme by theme
    sel = ":root" if not theme else 'html[data-theme="%s"]' % theme
    login = _decls(_block(LOGIN, "    " + sel))
    assert len(login) >= 12
    app = _decls(_block(CSS, "    :root"))
    if theme:
        app.update(_decls(_block(CSS, '    html[data-theme="%s"]' % theme)))
    for name, value in login.items():
        assert value == _resolve(app, name), (theme, name)
    # every token the page uses is declared for this theme (--glass-photo is theme.js's, set
    # on <html> when this browser has a photo of its own)
    used = set(re.findall(r"var\((--[\w-]+)\)", LOGIN)) - {"--glass-photo"}
    assert used <= set(login), used - set(login)


BOOT_HARNESS = r"""
var attrs = {}, meta = { content: "#0f172a", setAttribute: function (k, v) { this[k] = v; } };
var store = %(store)s, throws = %(throws)s;
var localStorage = { getItem: function (k) { if (throws) throw new Error("denied");
  return Object.prototype.hasOwnProperty.call(store, k) ? store[k] : null; },
  setItem: function (k, v) { if (throws) throw new Error("denied"); store[k] = String(v); } };
var window = { matchMedia: function (q) { return { matches: %(dark)s }; } };
var props = {};
var document = {
  documentElement: { setAttribute: function (k, v) { attrs[k] = v; }, removeAttribute: function (k) { delete attrs[k]; },
    style: { setProperty: function (k, v) { props[k] = v; }, removeProperty: function (k) { delete props[k]; } } },
  querySelector: function () { return meta; }
};
function getComputedStyle() { return { getPropertyValue: function (n) { return n === "--bg" ? " #f6f8fa " : ""; } }; }
%(boot)s
console.log(JSON.stringify({ theme: attrs["data-theme"] === undefined ? null : attrs["data-theme"], meta: meta.content, store: store,
  photo: "data-glass-photo" in attrs, photoUrl: props["--glass-photo"] || null }));
"""


@pytest.mark.parametrize("store,throws,dark,want", [
    ({"forge-ops-theme": "light"}, False, True, "light"),
    ({"forge-ops-theme": "amber"}, False, False, "amber"),
    ({"forge-ops-theme": "neon"}, False, False, "amber"),     # «Неон» is gone: moved to «Янтарь»
    ({"forge-ops-theme": ""}, False, False, None),           # «Океан» chosen: no attribute
    ({}, False, True, None),                                  # never chosen: the system's dark → «Океан»
    ({}, False, False, "light"),                              # … or its light
    ({"forge-ops-theme": "amber"}, True, False, "light"),     # no storage: the system's choice
])
def test_theme_boot(store, throws, dark, want):
    boot = THEME_JS[THEME_JS.index("(function forgeThemeBoot() {"):THEME_JS.index("function getStateColors")]
    out = _node(BOOT_HARNESS % {"store": json.dumps(store), "throws": json.dumps(throws),
                                "dark": json.dumps(dark), "boot": boot})
    assert {k: out[k] for k in ("theme", "meta")} == {"theme": want, "meta": "#f6f8fa"}
    if not throws and "forge-ops-theme" in store:
        # what is kept is what is shown: a «Неон» left behind is rewritten once, as «Янтарь»
        assert out["store"]["forge-ops-theme"] == (want or "")
    # the same rule and key as the app's own initTheme
    init = _js_function("initTheme")
    assert 'window.matchMedia("(prefers-color-scheme: dark)").matches' in init and 'applyTheme("light")' in init
    assert 'const THEME_STORAGE_KEY = "forge-ops-theme";' in JS



@pytest.mark.parametrize("photo,want", [
    ("data:image/jpeg;base64,/9j/4AAQ", True),     # a photo of this browser's own: shown through a blob: link
    ("", False), (None, False),
    ("javascript:alert(1)", False),               # anything but a base64 picture is not shown
    ("data:image/jpeg;base64,%%%", False),         # a broken one neither
])
def test_theme_boot_shows_this_browsers_photo(photo, want):
    store = {"forge-ops-theme": "glass"}
    if photo is not None:
        store["forge-ops-glass-photo"] = photo
    boot = THEME_JS[THEME_JS.index("(function forgeThemeBoot() {"):THEME_JS.index("function getStateColors")]
    out = _node(BOOT_HARNESS % {"store": json.dumps(store), "throws": "false", "dark": "true", "boot": boot})
    assert out["theme"] == "glass" and out["photo"] is want
    if want:
        assert re.fullmatch(r'url\("blob:[^"]+"\)', out["photoUrl"])
    else:
        assert out["photoUrl"] is None


def test_glass_photo_pick_in_the_panel():
    # 👤 → «Оформление»: the row is in «Стекло» only, the picture stays in this browser
    row = HTML[HTML.index('id="glassPhotoRow"'):]
    row = row[:row.index('<input type="file"')]
    assert 'id="glassPhotoPick"' in row and 'id="glassPhotoClear"' in row
    assert '<input type="file" id="glassPhotoInput" accept="image/*" hidden />' in HTML
    assert 'const GLASS_PHOTO_KEY = "forge-ops-glass-photo";' in JS
    sync = _js_function("glassPhotoSync")
    assert 'getAttribute("data-theme") !== "glass"' in sync
    assert "glassPhotoSync();" in _js_function("applyTheme")
    pick = _js_function("glassPhotoPick")
    assert "[[1920, 0.82], [1280, 0.72]]" in pick and "fetch(" not in pick and "/api/" not in pick
    # the photo is drawn under the glass with a veil, and only in «Стекло»
    assert 'html[data-theme="glass"][data-glass-photo] body::before' in CSS
    assert "var(--glass-veil), var(--glass-veil)), var(--glass-photo)" in CSS
    assert 'html[data-theme="glass"][data-glass-photo] body' in LOGIN


INIT_HARNESS = r"""
var store = %(store)s, applied = [];
var localStorage = { getItem: function (k) { return Object.prototype.hasOwnProperty.call(store, k) ? store[k] : null; },
  setItem: function (k, v) { store[k] = String(v); }, removeItem: function (k) { delete store[k]; } };
var window = { matchMedia: function () { return { matches: true }; } };
const THEME_STORAGE_KEY = "forge-ops-theme";
function applyTheme(v) { applied.push(v); }
%(fn)s
initTheme();
console.log(JSON.stringify({ applied: applied, store: store }));
"""


@pytest.mark.parametrize("stored,want", [("neon", "amber"), ("amber", "amber"), ("glass", "glass"), ("", "")])
def test_init_theme_moves_neon_to_amber(stored, want):
    # «Неон» is gone: a browser that kept it opens in «Янтарь», and keeps «Янтарь» from now on
    out = _node(INIT_HARNESS % {"store": json.dumps({"forge-ops-theme": stored}), "fn": _js_function("initTheme")})
    assert out["applied"] == [want]
    assert out["store"]["forge-ops-theme"] == want
    # nothing of the old theme is left to choose or to draw
    assert 'data-theme="neon"' not in HTML and 'data-theme="neon"' not in CSS and 'data-theme="neon"' not in LOGIN
    assert '"Неон"' not in (WEB / "i18n" / "en.js").read_text(encoding="utf-8")


def test_cache_bust_bumped():
    assert int(re.search(r"styles\.css\?v=(\d+)", HTML).group(1)) >= 88
    assert int(re.search(r"app\.js\?v=(\d+)", HTML).group(1)) >= 105
    assert int(re.search(r"theme\.js\?v=(\d+)", HTML).group(1)) >= 5
