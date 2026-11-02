"""PWA of the dashboard (spec_pwa_tab §2, §5): manifest, service worker, icons.

The PWA files are served anonymously on exact routes (app.api.main.PWA_FILES)
while /static/ stays closed — no token stubs here, every request is a
visitor without a session. The worker's routing function (the block between
/* sw-route:begin */ and /* sw-route:end */ in app/web/pwa/sw.js) runs in
node; skipped without node, like tests/test_web_ctl_ui.py.
"""
import asyncio
import json
import re
import shutil
import struct
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import main as api_main

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "app" / "web"
SW = (WEB / "pwa" / "sw.js").read_text(encoding="utf-8")
CSS = (WEB / "styles.css").read_text(encoding="utf-8")
INDEX = (WEB / "index.html").read_text(encoding="utf-8")
APP_JS = (WEB / "app.js").read_bytes()
ICON_CACHE = "public, max-age=604800"


class FakeStore:
    def get_all(self):
        return []


@pytest.fixture
def app(monkeypatch):
    import config as cfg
    # create_app publishes its secret on config; restore it after the test
    monkeypatch.setattr(cfg, "JWT_SECRET", getattr(cfg, "JWT_SECRET", ""), raising=False)
    return api_main.create_app(FakeStore())


@pytest.fixture
def client(app):
    return TestClient(app)


def _raw_get(app, path, host="testserver", root_path=""):
    """GET with the path and Host exactly as given (no client-side normalization)."""
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
             "scheme": "http", "path": path, "raw_path": path.encode(), "root_path": root_path,
             "query_string": b"", "headers": [(b"host", host.encode("latin-1"))],
             "client": ("127.0.0.1", 50000), "server": ("testserver", 80)}
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, body


# --- routes ------------------------------------------------------------------

EXPECTED = {
    "/manifest.webmanifest": ("application/manifest+json", "no-cache"),
    "/sw.js": ("text/javascript", "no-cache"),
    "/pwa/icon-192.png": ("image/png", ICON_CACHE),
    "/pwa/icon-512.png": ("image/png", ICON_CACHE),
    "/pwa/icon-maskable-512.png": ("image/png", ICON_CACHE),
}


def test_pwa_files_table_is_the_spec_table():
    assert {path: (mt, cc) for path, (_f, mt, cc) in api_main.PWA_FILES.items()} == EXPECTED


@pytest.mark.parametrize("path", sorted(EXPECTED))
def test_pwa_file_served_without_a_session(client, path):
    media_type, cache_control = EXPECTED[path]
    r = client.get(path)
    assert r.status_code == 200
    assert r.headers["content-type"].split(";")[0] == media_type
    assert r.headers["cache-control"] == cache_control
    assert r.content == (ROOT / api_main.PWA_FILES[path][0]).read_bytes()


def test_icon_query_is_ignored_by_the_route(client):
    # the manifest asks for /pwa/icon-192.png?v=1
    r = client.get("/pwa/icon-192.png?v=1")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"


def test_pwa_paths_are_public_and_files_exist():
    for path, (file, _mt, _cc) in api_main.PWA_FILES.items():
        assert path in api_main.PUBLIC_EXACT
        assert (ROOT / file).is_file(), file
        assert file.startswith("app/web/pwa/")


def test_no_public_prefix_under_static():
    # An open prefix under /static would let /static/pwa/../app.js through (§2.2).
    assert api_main.PUBLIC_PREFIXES == ("/api/auth/",)
    assert not [p for p in api_main.PUBLIC_EXACT if p.startswith("/static")]
    assert api_main.PUBLIC_EXACT[:2] == ("/api/health", "/favicon.ico")


@pytest.mark.parametrize("path", ["/static/app.js", "/static/pwa/sw.js", "/static/pwa/manifest.webmanifest",
                                  "/static/pwa/icon-192.png", "/static/styles.css"])
def test_static_stays_closed_to_a_visitor(client, path):
    assert client.get(path).status_code == 401


def test_raw_helper_reaches_the_app(app):
    # sanity: the raw request path works for a public file and a closed one
    assert _raw_get(app, "/sw.js")[0] == 200
    assert _raw_get(app, "/static/app.js")[0] == 401


@pytest.mark.parametrize("path", ["/pwa/../static/app.js", "/static/pwa/../app.js", "/sw.js/../static/app.js",
                                  "/pwa/icon-192.png/../../static/app.js", "/manifest.webmanifest/../static/app.js",
                                  "/static/pwa/%2e%2e/app.js", "/pwa/%2e%2e/static/app.js"])
def test_dot_segments_do_not_open_static(app, path):
    status, body = _raw_get(app, path)
    assert status in (401, 404)
    assert APP_JS[:200] not in body


# request.url is built as scheme://<Host header><path>, so a Host like
# "x/sw.js?" made request.url.path read "/sw.js" while the router served the
# real path: the auth check must look at the path the router matches.
@pytest.mark.parametrize("host", ["x/sw.js?", "x/sw.js#", "x/api/health?", "x/manifest.webmanifest?",
                                  "x/favicon.ico?", "x/api/auth/login?"])
@pytest.mark.parametrize("path", ["/static/app.js", "/api/printers"])
def test_host_header_does_not_open_closed_paths(app, host, path):
    status, body = _raw_get(app, path, host=host)
    assert status == 401
    assert APP_JS[:200] not in body


def test_root_path_is_stripped_like_the_router_does(app):
    # an ASGI path carries root_path; the router matches it without
    assert _raw_get(app, "/forge/sw.js", root_path="/forge")[0] == 200
    assert _raw_get(app, "/forge/static/app.js", root_path="/forge")[0] == 401
    assert _raw_get(app, "/forge/api/printers", root_path="/forge")[0] == 401


def test_encoded_dot_segments_via_client(client):
    r = client.get("/static/pwa/%2e%2e/app.js")
    assert r.status_code in (401, 404) and APP_JS[:200] not in r.content


def test_login_page_carries_the_pwa_head(client):
    r = client.get("/")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    html = r.text
    assert 'name="robots"' in html            # it is the login page, not the app shell
    assert '<link rel="manifest" href="/manifest.webmanifest">' in html
    assert '<meta name="theme-color" content="#061723">' in html
    assert "/api/auth/refresh" in html


def test_index_carries_the_pwa_head():
    head = INDEX.split("</head>", 1)[0]
    assert '<link rel="manifest" href="/manifest.webmanifest">' in head
    assert '<meta name="theme-color" content="#061723">' in head
    assert 'rel="icon" href="data:image/svg+xml' in head      # the base href of the drawn favicon


def test_index_static_refs_are_versioned():
    # A deploy reaches the clients through new ?v= URLs (the worker caches by URL).
    refs = re.findall(r'(?:href|src)="(/static/[^"]*)"', INDEX)
    assert refs, "no /static/ references in index.html"
    bad = [r for r in refs if not re.search(r"\?v=\d+", r)]
    assert not bad, bad


# --- the login page's silent refresh --------------------------------------------

def test_login_refresh_is_one_shot_with_a_loop_guard():
    html = (WEB / "login.html").read_text(encoding="utf-8")
    script = html.rsplit("<script>", 1)[1].split("</script>", 1)[0]
    assert "forge-login-refresh-at" in script
    assert "sessionStorage" in script and "try" in script
    assert "15000" in script
    assert re.search(r"fetch\('/api/auth/refresh', \{ method: 'POST', credentials: 'same-origin' \}\)", script)
    # the screen asked for survives the sign-in (plan step 15: a PWA shortcut /#/ams, deliberately changed)
    assert "location.replace('/' + location.hash)" in script and "location.replace('/')" not in script
    # once per load: no timers, no reload
    assert "setInterval" not in script and "setTimeout" not in script and "reload" not in script


# --- manifest ----------------------------------------------------------------------

def _css_root_var(name):
    block = re.search(r":root\s*\{(.*?)\n\s*\}", CSS, re.S).group(1)
    values = dict(re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", block))
    value = values[name].strip()
    while value.startswith("var("):
        value = values[re.match(r"var\((--[\w-]+)\)", value).group(1)].strip()
    return value


def test_manifest_contents():
    m = json.loads((WEB / "pwa" / "manifest.webmanifest").read_text(encoding="utf-8"))
    assert m["name"] == "Кузница" and m["short_name"] == "Кузница"
    assert m["start_url"] == "/" and m["scope"] == "/" and m["id"] == "/"
    assert m["display"] == "standalone"
    assert m.get("prefer_related_applications") is not True
    # nothing about the farm in a public file (print names are sensitive): no screenshots; the
    # shortcuts (plan step 15, deliberately changed) name screens only — «Нужно решение» opens the
    # bell's list once, «Финиши» the finishes (a phone's list sheet), AMS its tab
    assert "screenshots" not in m
    assert [(s["name"], s["short_name"], s["url"]) for s in m["shortcuts"]] == [
        ("Нужно решение", "Решение", "/#/printers?bell=open"), ("Финиши", "Финиши", "/#/printers?finish=open"),
        ("AMS", "AMS", "/#/ams")]
    for s in m["shortcuts"]:
        assert set(s) == {"name", "short_name", "url", "icons"}
        assert [(i["src"].split("?")[0], i["sizes"], i["type"]) for i in s["icons"]] == [("/pwa/icon-192.png", "192x192", "image/png")]
        assert s["icons"][0]["src"].split("?")[0] in api_main.PWA_FILES
    bg = _css_root_var("--bg")
    assert bg == "#061723"
    assert m["theme_color"] == bg and m["background_color"] == bg
    icons = {(i["sizes"], i["purpose"]): i for i in m["icons"]}
    assert set(icons) == {("192x192", "any"), ("512x512", "any"), ("512x512", "maskable")}
    for (sizes, _purpose), icon in icons.items():
        assert icon["type"] == "image/png"
        path = icon["src"].split("?", 1)[0]
        assert path in api_main.PWA_FILES
        assert re.search(r"\?v=\d+$", icon["src"])


# --- icons (no Pillow: PNG signature + IHDR) ---------------------------------------

def _png_header(path):
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    length, kind = struct.unpack(">I4s", data[8:16])
    assert kind == b"IHDR" and length == 13
    width, height, depth, color_type = struct.unpack(">IIBB", data[16:26])
    return width, height, depth, color_type


def test_icon_files_match_the_manifest():
    m = json.loads((WEB / "pwa" / "manifest.webmanifest").read_text(encoding="utf-8"))
    for icon in m["icons"]:
        file = ROOT / api_main.PWA_FILES[icon["src"].split("?", 1)[0]][0]
        width, height, depth, color_type = _png_header(file)
        assert "%dx%d" % (width, height) == icon["sizes"]
        assert depth == 8
        # any: RGBA with transparent corners; maskable: RGB, no alpha at all
        assert color_type == (2 if icon["purpose"] == "maskable" else 6), icon["src"]


# --- service worker ----------------------------------------------------------------

def _block(src, tag):
    m = re.search(r"/\* %s:begin \*/(.*?)/\* %s:end \*/" % (tag, tag), src, re.S)
    assert m, "%s block not found" % tag
    return m.group(1)


def _node(src):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    out = subprocess.run([node, "-"], input=src, capture_output=True, text=True, timeout=60, encoding="utf-8")
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


ORIGIN = "https://forge.example:8443"
ROUTES = [
    # (url, method, mode, expected)
    (ORIGIN + "/api/printers", "GET", "cors", None),
    (ORIGIN + "/api/auth/refresh", "GET", "cors", None),
    (ORIGIN + "/api/auth/refresh", "POST", "cors", None),
    (ORIGIN + "/api", "GET", "cors", None),
    (ORIGIN + "/", "GET", "navigate", None),
    (ORIGIN + "/", "GET", "no-cors", None),
    (ORIGIN + "/static/app.js?v=91", "GET", "no-cors", "swr"),
    (ORIGIN + "/static/styles.css?v=82", "GET", "no-cors", "swr"),
    (ORIGIN + "/static/img/bambu-p2s.png", "GET", "no-cors", "swr"),
    (ORIGIN + "/pwa/icon-192.png?v=1", "GET", "no-cors", "swr"),
    (ORIGIN + "/pwa/icon-maskable-512.png?v=1", "GET", "no-cors", "swr"),
    (ORIGIN + "/pwa/other.png", "GET", "no-cors", None),
    (ORIGIN + "/static/x", "POST", "cors", None),
    (ORIGIN + "/static/app.js", "GET", "navigate", None),      # typed in the address bar
    (ORIGIN + "/static/../api/printers", "GET", "cors", None),  # URL resolves the dot segments
    ("https://fonts.gstatic.com/s/ibmplexsans/v19/a.woff2", "GET", "cors", None),
    ("https://fonts.googleapis.com/css2?family=IBM+Plex+Sans", "GET", "no-cors", None),
    ("https://other.example:8443/static/app.js?v=91", "GET", "no-cors", None),
    (ORIGIN + "/sw.js", "GET", "same-origin", None),
    (ORIGIN + "/manifest.webmanifest", "GET", "cors", None),
    ("not a url", "GET", "no-cors", None),
]


def test_sw_route():
    calls = ",".join("forgeSwRoute(%s, %s, %s, %s)" % tuple(json.dumps(v) for v in (u, m, mode, ORIGIN))
                     for u, m, mode, _ in ROUTES)
    got = _node(_block(SW, "sw-route") + "\nconsole.log(JSON.stringify([" + calls + "]));")
    assert got == [want for *_, want in ROUTES]


def test_sw_route_block_is_pure():
    block = _block(SW, "sw-route")
    for banned in ("self.", "caches", "fetch(", "document", "window"):
        assert banned not in block, banned


def test_sw_lifecycle_and_cache_rules():
    assert 'var CACHE = "forge-static-v1";' in SW
    assert "self.skipWaiting()" in SW
    assert "self.clients.claim()" in SW
    # activate drops the other forge-* caches
    assert re.search(r'k\.indexOf\("forge-"\) === 0 && k !== CACHE', SW)
    # only a 200 same-origin answer is stored: the fetch handler checks before it stores
    handler = SW[SW.index('self.addEventListener("fetch"'):]
    assert handler.index('if (!(res.ok && res.type === "basic")) return undefined;') < handler.index("forgePut(cache, req, copy)")
    assert SW.count("cache.put(") == 1 and SW.count("forgePut(") == 2   # its definition and the one call
    # the same path with another query is dropped on store
    assert "u.pathname === url.pathname && u.search !== url.search" in SW
    assert '"forge-clear"' in SW and "caches.delete(CACHE)" in SW
    assert "event.waitUntil(refresh)" in SW
    # the background refresh skips the heuristic HTTP cache (always revalidates)
    assert 'var net = fetch(req, { cache: "no-cache" });' in handler and handler.count("fetch(") == 1
    # the kill switch is documented in the header
    head = SW.split('"use strict";', 1)[0]
    assert "Kill switch" in head and "unregister()" in head and "?nosw" in head


def test_sw_has_no_extra_powers():
    assert "importScripts" not in SW
    assert '"push"' not in SW and '"sync"' not in SW
    assert "/api" not in SW.split("/* sw-route:end */", 1)[1]


# --- app.js: registration, kill switch, logout, theme-color ------------------------

APP = APP_JS.decode("utf-8")


def test_app_registers_the_worker_at_the_root():
    assert 'navigator.serviceWorker.register("/sw.js", { scope: "/", updateViaCache: "none" })' in APP
    # a self-signed address fails here: caught and logged at debug level only
    reg = APP[APP.index("function initServiceWorker()"):APP.index("function forgeLogoutClear()")]
    assert ".catch(function (e) {\n" in reg.replace("\r\n", "\n") and "console.debug(" in reg
    assert 'window.addEventListener("load", register)' in reg
    # no hostname check: the same code runs in any copy of the app
    assert "location.hostname" not in reg and ":8443" not in reg


def test_nosw_unregisters_and_clears_instead_of_registering():
    reg = APP[APP.index("function initServiceWorker()"):APP.index("function forgeLogoutClear()")]
    nosw = reg[reg.index('.has("nosw")'):reg.index("function register()")]
    assert "getRegistrations()" in nosw and "r.unregister()" in nosw
    assert ".then(forgeClearCaches)" in nosw and "return;" in nosw
    # the old worker keeps controlling this page and would refill the cache:
    # one reload without it, only from a controlled page (no loop)
    assert "var controlled = !!navigator.serviceWorker.controller;" in nosw
    assert "if (controlled) location.reload();" in nosw
    clear = APP[APP.index("function forgeClearCaches()"):APP.index("function initServiceWorker()")]
    assert 'k.indexOf("forge-") === 0' in clear and "caches.delete(k)" in clear


def test_logout_clears_the_cache_before_leaving():
    fn = APP[APP.index("function forgeLogoutClear()"):APP.index("// Logout button")]
    assert 'postMessage({ type: "forge-clear" })' in fn
    assert "setTimeout(r, 300)" in fn and "Promise.race(" in fn
    handler = APP[APP.index("logoutBtn.addEventListener('click'"):APP.index("function loadAppVersion()")]
    assert "var cleared = forgeLogoutClear();" in handler
    assert "cleared.then(function () { window.location.replace('/'); });" in handler
    assert "fetch('/api/auth/logout', { method: 'POST' }).then(leave, leave);" in handler


def test_apply_theme_sets_theme_color_from_the_css_token():
    fn = APP[APP.index("function applyTheme("):APP.index("function initTheme(")]
    assert 'meta[name="theme-color"]' in fn
    assert 'getPropertyValue("--bg")' in fn


def test_app_js_cache_bust_bumped():
    assert int(re.search(r"app\.js\?v=(\d+)", INDEX).group(1)) >= 96


# --- app.js: reload a long-lived window after a deploy (§2.9.4) ----------------------

def _version_section():
    return APP[APP.index("var appVer = "):APP.index("// Версия деплоя")]


VERSION_HARNESS = r"""
var handlers = {}, reloads = 0, now = 1000000, version = "aaa", fetches = 0;
var dlg = { on: false, classList: { contains: function (c) { return c === "on" && dlg.on; } } };
var document = { visibilityState: "hidden", activeElement: null,
                 addEventListener: function (t, f) { handlers[t] = f; },
                 getElementById: function (id) { return id === "adDlgBack" ? dlg : null; } };
var location = { reload: function () { reloads++; } };
var pcPopPid = null, nfPop = null, bulkDlg = null, adForm = null, aiDirty = false;
function aiIsDirty() { return aiDirty; }
Date.now = function () { return now; };
function apiFetch(url) {
  if (url !== "/api/version") throw new Error(url);
  fetches++;
  return Promise.resolve({ json: function () { return { commit: version, date: "2026-10-18" }; } });
}
%s
async function show() {
  document.visibilityState = "visible";
  handlers.visibilitychange();
  await new Promise(function (r) { setTimeout(r, 0); });
  return [fetches, reloads];
}
(async function () {
  var log = [];
  appVer.commit = "aaa";                                   // loadAppVersion's answer at startup
  document.visibilityState = "hidden"; handlers.visibilitychange(); log.push([fetches, reloads]);
  log.push(await show());                                  // within 5 min of the start: no request
  now += 5 * 60 * 1000; log.push(await show());            // same commit: no reload
  version = "bbb"; now += 60 * 1000; log.push(await show());   // 1 min later: throttled
  now += 5 * 60 * 1000;
  document.activeElement = { tagName: "INPUT", type: "text" };
  log.push(await show());                                  // newer commit, typing: deferred
  document.activeElement = { tagName: "INPUT", type: "checkbox" }; dlg.on = true;
  log.push(await show());                                  // a dialog is open: deferred, no new request
  dlg.on = false; pcPopPid = "p1"; log.push(await show());  // a card popover
  pcPopPid = null; nfPop = { type: "farm" }; log.push(await show());
  nfPop = null; bulkDlg = { step: 1 }; log.push(await show());
  bulkDlg = null; adForm = { target_rh: 6 }; log.push(await show());   // unsaved auto-dry rules (plan step 11)
  adForm = null; aiDirty = true; log.push(await show());                 // unsaved auto-ignore codes (plan step 13)
  aiDirty = false; document.activeElement = { tagName: "SELECT" }; log.push(await show());
  document.activeElement = { tagName: "BUTTON" }; log.push(await show());   // idle now: reload
  console.log(JSON.stringify(log));
})();
"""


def test_version_check_reloads_only_when_idle():
    got = _node(VERSION_HARNESS % _version_section())
    assert got == [[0, 0], [0, 0], [1, 0], [1, 0], [2, 0], [2, 0], [2, 0], [2, 0], [2, 0], [2, 0], [2, 0], [2, 0], [2, 1]]


def test_version_check_unknown_start_commit_is_learned_not_reloaded():
    src = (VERSION_HARNESS % _version_section()).replace('appVer.commit = "aaa";', "")
    src = src.replace('log.push(await show());                                  // within 5 min',
                      'now += 5 * 60 * 1000; log.push(await show());  // within 5 min')
    got = _node(src)
    assert got[1] == [1, 0]           # first answer only remembered
    assert got[-1][1] == 1            # a later different commit still reloads


def test_version_check_reuses_the_startup_request():
    # one request in loadAppVersion (it also seeds appVer.commit), one in the handler
    assert APP.count("'/api/version'") + APP.count('"/api/version"') == 2
    load_version = APP[APP.index("(function loadAppVersion()"):]
    load_version = load_version[:load_version.index("})();")]
    assert "appVer.commit = v.commit" in load_version
    assert APP.index("var appVer = ") < APP.index("(function loadAppVersion()")
    section = _version_section()
    assert 'document.visibilityState !== "visible"' in section
    assert section.count("location.reload()") == 1
