"""The panel's languages (app/web/i18n.js, app/web/i18n/en.js, app/api/i18n.py; I18N_REPORT.md).

* coverage: every Cyrillic piece of app.js, i18n.js, index.html, login.html, the PWA
  manifest and the server texts that reach the screen (scripts/i18n_extract.py) is in
  the English dictionary, matches one of its patterns, or is listed in `untranslated`
  with a reason;
* the patterns compile (in node, and as Python regexes: the coverage check uses them);
* the language from navigator.languages: the CIS languages read Russian, others English;
* /i18n.js and the translated manifest are public, the fleet's live texts are not;
* both pages load /i18n.js before anything else.
The JS parts run in node (skipped without node).
"""
import importlib.util
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import i18n as i18n_api
from app.api import main as api_main

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "app" / "web"
RUNTIME = (WEB / "i18n.js").read_text(encoding="utf-8")
EN = (WEB / "i18n" / "en.js").read_text(encoding="utf-8")
HTML = (WEB / "index.html").read_text(encoding="utf-8")
LOGIN = (WEB / "login.html").read_text(encoding="utf-8")
CSS = (WEB / "styles.css").read_text(encoding="utf-8")


def _extract():
    spec = importlib.util.spec_from_file_location("i18n_extract", ROOT / "scripts" / "i18n_extract.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _node(src):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    out = subprocess.run([node, "-"], input=src, capture_output=True, text=True, timeout=60, encoding="utf-8")
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


# the runtime and the dictionary in node: no DOM, the pure lookup only
LIB = "globalThis.window = undefined;\n" + RUNTIME + "\n" + EN + "\n"


def _tr(texts):
    return _node(LIB + "console.log(JSON.stringify(%s.map(function (s) { return I18N.tr('en', s); })));"
                 % json.dumps(texts, ensure_ascii=False))


# --- coverage -------------------------------------------------------------------------------

# every language file of app/web/i18n is checked the same way (a third language: one more file)
@pytest.fixture(scope="module", params=i18n_api.languages())
def extracted(request):
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    x = _extract()
    return x, x.collect(), x.load_dictionary(request.param)


def test_every_russian_string_is_covered(extracted):
    x, rows, dic = extracted
    assert len(rows) > 1500                     # the extractor did find the panel's texts
    files = {r["file"] for r in rows}
    assert {"app/web/app.js", "app/web/index.html", "app/web/login.html",
            "app/services/printer_commands.py", "app/services/normalizer.py"} <= files
    miss = x.uncovered(rows, dic)
    seen, report = set(), []
    for r in miss:
        if r["text"] not in seen:
            seen.add(r["text"])
            report.append("%s:%s %r" % (r["file"], r["line"], r["text"]))
    assert report == [], "not in the dictionary (python scripts/i18n_extract.py --missing <lang>):\n" + "\n".join(report[:50])


def test_untranslated_have_reasons_and_exist(extracted):
    x, rows, dic = extracted
    texts = {r["text"] for r in rows}
    for k, why in dic["exclude"].items():
        assert isinstance(why, str) and len(why) > 10, k
        assert k in texts, "stale exclusion: %r" % k
        assert k not in dic["dict"], "both translated and excluded: %r" % k


def test_dictionary_values_have_no_cyrillic(extracted):
    _x, _rows, dic = extracted
    bad = {k: v for k, v in dic["dict"].items() if not v.strip() or re.search(r"[А-Яа-яЁё]", v)}
    assert bad == {}


def test_strings_block_is_plain_json():
    # the server reads it for the manifest (app/api/i18n.py strings())
    m = re.search(r"/\*strings:begin\*/(.*?)/\*strings:end\*/", EN, re.S)
    assert m
    data = json.loads(m.group(1))
    assert data["Кузница"] == "Kuznitsa" and len(data) > 1400
    assert i18n_api.strings("en") == data


def test_extractor_reads_fragments_and_attributes():
    x = _extract()
    assert x.pieces('<span class="a" data-tip="Пауза печати">Стоп</span>') == ["Пауза печати", "Стоп"]
    assert x.pieces('" data-tip="Загрузить: ') == ["Загрузить:"]
    assert x.pieces('— открыть список" aria-label="') == ["— открыть список"]
    assert x.pieces(">Готово") == ["Готово"]
    assert x.pieces("&nbsp;мин&nbsp;") == ["мин"]
    js = 'var a = "Пауза"; // комментарий\nvar r = /ошибка/i; var t = `x ${n} осталось ${m}`;'
    assert [s for _l, s in x.js_strings(js)] == ["Пауза", "x ", " осталось ", ""]


def test_css_texts_have_overrides():
    # ::before { content: "…" } with Russian in styles.css: the language's css restates it
    found = re.findall(r'([^{}\n]+)\{[^}]*content:\s*"([^"]*[А-Яа-яЁё][^"]*)"', CSS)
    assert found
    css = _node(LIB + "console.log(JSON.stringify(''));")   # node present
    assert css == ""
    for sel, _text in found:
        assert sel.strip() in EN, sel


# --- patterns -------------------------------------------------------------------------------

def test_patterns_compile_in_node_and_python():
    pats = _node(LIB + """
var d = null;
I18N.register = function (lang, data) { d = data; };
""" + EN + """
console.log(JSON.stringify(d.patterns.map(function (p) {
  new RegExp(p[0].source, p[0].flags);
  return [p[0].source, p[0].flags, typeof p[1], p[2] || null];
})));""")
    assert len(pats) >= 8
    x = _extract()
    for src, flags, rep, kind in pats:
        re.compile(x._py_regex(src), x._py_flags(flags))
        assert rep in ("string", "function") and kind in (None, "sub"), src


def test_patterns_and_pieces_translate():
    src = ["Пауза", "  Принтеры ", "Сушка · осталось 2 ч 05 мин", "5 с назад", "Показать ещё 50",
           "Ошибка печати 0300_4000: Нагрев сопла", "Пауза — ждём принтер…", "Загрузка — подтверждено",
           "5 принтеров требуют внимания", "21 принтер требует внимания", "1 принтер требует внимания",
           "24 ч · все принтеры", "и ещё 3", "КУЗНИЦА", "X1C-01"]
    assert _tr(src) == ["Paused", "  Printers ", "Drying · 2 h 05 min left", "5 s ago", "Show 50 more",
                        "Print error 0300_4000: Heating nozzle", "Pause — waiting for printer…",
                        "Load — confirmed", "5 printers need attention", "21 printers need attention",
                        "1 printer needs attention", "24 h · all printers", "and 3 more", "KUZNITSA", "X1C-01"]


def test_percent_s_keys_become_patterns():
    out = _tr(["ошибка 0300_4000 — команды сброса нет, решите на экране принтера"])
    assert out == ["error 0300_4000 — no clear command, resolve it on the printer screen"]


# --- the language from the browser ----------------------------------------------------------

@pytest.mark.parametrize("langs, want", [
    (["ru-RU"], "ru"), (["uk-UA", "en-US"], "ru"), (["be"], "ru"), (["kk-KZ"], "ru"), (["ky"], "ru"),
    (["uz-Latn-UZ"], "ru"), (["tg"], "ru"), (["hy-AM"], "ru"), (["az-AZ"], "ru"),
    (["en-US", "ru"], "en"), (["de-DE"], "en"), (["fr", "uk"], "ru"), (["pl-PL", "de"], "en"),
    ([], "en"), ([""], "en"),
])
def test_language_from_navigator(langs, want):
    assert _node(LIB + "console.log(JSON.stringify(I18N.detect(%s)));" % json.dumps(langs)) == want


def test_detect_order_and_storage_key():
    # the first language the panel knows decides; the choice is kept like the theme's
    assert "var STORAGE_KEY = \"forge-ops-lang\";" in RUNTIME
    assert 'var RU_FAMILY = ["ru", "uk", "be", "kk", "ky", "uz", "tg", "hy", "az"];' in RUNTIME
    assert _node(LIB + "console.log(JSON.stringify([I18N.languages(), I18N.lang()]));") == [["ru", "en"], "ru"]


# --- serving --------------------------------------------------------------------------------

@pytest.fixture
def client(monkeypatch):
    import config as cfg
    monkeypatch.setattr(cfg, "JWT_SECRET", getattr(cfg, "JWT_SECRET", ""), raising=False)
    return TestClient(api_main.create_app(type("S", (), {"get_all": lambda self: []})()))


def test_i18n_js_is_public_to_an_anonymous_visitor(client):
    assert "/i18n.js" in api_main.PUBLIC_EXACT and "/manifest.en.webmanifest" in api_main.PUBLIC_EXACT
    assert not [p for p in api_main.PUBLIC_EXACT if p.startswith("/static") or p.startswith("/api/i18n")]
    r = client.get("/i18n.js?v=1")
    assert r.status_code == 200 and r.headers["content-type"].split(";")[0] == "text/javascript"
    assert r.headers["cache-control"] == "no-cache"
    body = r.text
    assert body.startswith(RUNTIME) and EN in body and body.rstrip().endswith("I18N.start();")
    again = client.get("/i18n.js", headers={"If-None-Match": r.headers["etag"]})
    assert again.status_code == 304
    m = client.get("/manifest.en.webmanifest")
    assert m.status_code == 200 and m.headers["content-type"].startswith("application/manifest+json")
    doc = m.json()
    assert (doc["name"], doc["short_name"], doc["description"], doc["lang"]) == \
        ("Kuznitsa", "Kuznitsa", "Dashboard", "en")
    assert [s["name"] for s in doc["shortcuts"]] == ["Needs action", "Finishes", "AMS"]
    assert not re.search(r"[А-Яа-яЁё]", m.text)


def test_the_rest_stays_closed(client):
    for path in ("/api/i18n/texts?lang=en", "/static/i18n.js", "/static/i18n/en.js", "/i18n/en.js",
                 "/i18n.js/../static/app.js", "/manifest.de.webmanifest"):
        assert client.get(path).status_code in (401, 404), path


def test_bundle_reveals_nothing_about_the_farm():
    for bad in ("printer_id", "currentUser", "XMLHttpRequest", "/api/printers", "/api/admin"):
        assert bad not in RUNTIME and bad not in EN, bad
    # the only request: the session-gated live texts
    assert re.findall(r'fetch\("([^"?]+)', RUNTIME) == ["/api/i18n/texts"]


def test_pages_load_i18n_first():
    for page in (HTML, LOGIN):
        scripts = re.findall(r'<script src="([^"]+)"', page)
        assert scripts[0] == "/i18n.js?v=1", scripts
    assert HTML.index('<script src="/i18n.js') < HTML.index('<link rel="stylesheet"')


def test_switch_retitles_the_tab():
    # the tab's title follows a switch at once, not on app.js's next title change
    stub = """
globalThis.Document = function () {};
Object.defineProperty(Document.prototype, "title", {
  configurable: true,
  get: function () { return this._t || ""; },
  set: function (v) { this._t = String(v); }
});
globalThis.document = new Document();
document.nodeType = 9;
document.documentElement = { setAttribute: function () {} };
document.querySelector = function () { return null; };
document.querySelectorAll = function () { return []; };
document.createTreeWalker = function () { return { nextNode: function () { return null; } }; };
"""
    real = "Object.getOwnPropertyDescriptor(Document.prototype, 'title').get.call(document)"
    out = _node(stub + LIB + """
var seen = [];
I18N.set("en"); document.title = "Кузница"; seen.push(%s);
I18N.set("ru"); seen.push(%s);
I18N.set("en"); seen.push(%s, document.title);
console.log(JSON.stringify(seen));
""" % (real, real, real))
    assert out == ["Kuznitsa", "Кузница", "Kuznitsa", "Кузница"]


def test_docs_link_follows_the_language():
    # 👤 → «Документация» opens the docs site in the panel's language
    docs = re.search(r'<a href="([^"]+)" data-href-ru="([^"]+)" data-href-en="([^"]+)"[^>]*>Документация</a>', HTML)
    assert docs and docs.group(1) == docs.group(2) and docs.group(2).endswith("/ru/"), "the docs link in 👤"
    assert 'href="https://github.com/Mihey-web/printer-fleet-dashboard"' in HTML
    out = _node("""
globalThis.Document = function () {};
globalThis.document = new Document();
var a = { attrs: {"data-href-ru": "/ru/", "data-href-en": "/", href: "/ru/"},
          getAttribute: function (n) { return n in this.attrs ? this.attrs[n] : null; },
          setAttribute: function (n, v) { this.attrs[n] = v; } };
document.documentElement = { setAttribute: function () {} };
document.querySelector = function () { return null; };
document.querySelectorAll = function (s) { return s === "a[data-href-ru]" ? [a] : []; };
document.createTreeWalker = function () { return { nextNode: function () { return null; } }; };
""" + LIB + """
var seen = [];
I18N.set("en"); seen.push(a.attrs.href);
I18N.set("ru"); seen.push(a.attrs.href);
console.log(JSON.stringify(seen));
""")
    assert out == ["/", "/ru/"]


def test_live_texts_are_bambus_english():
    from app.vendor.pybambu.utils import get_HMS_error_text, get_print_error_text
    hms_ru = get_HMS_error_text("0500_0100_0003_0004", "X1C", "ru")
    hms_en = get_HMS_error_text("0500_0100_0003_0004", "X1C", "en")
    pe_ru = get_print_error_text("03004000", "P2S", "ru")
    pe_en = get_print_error_text("03004000", "P2S", "en")
    assert re.search(r"[А-Яа-яЁё]", hms_ru) and not re.search(r"[А-Яа-яЁё]", hms_en)
    rows = [{"device_type": "X1C", "notifications": [
        {"src": "hms", "code": "0500_0100_0003_0004", "text": hms_ru},
        {"src": "print_error", "code": "0300_4000", "text": "без кода"},
        {"src": "hms", "code": "", "text": "пусто"}]},
        {"model": "P2S", "notifications": [{"src": "print_error", "code": "0300_4000", "text": pe_ru}]}]
    out = i18n_api.live_texts(rows, "en")
    assert out[hms_ru] == hms_en and out[pe_ru] == pe_en
    assert "пусто" not in out
    assert i18n_api.live_texts(rows, "de") == {}


def test_live_texts_route(monkeypatch):
    import config as cfg
    from app.vendor.pybambu.utils import get_HMS_error_text
    ru = get_HMS_error_text("0500_0100_0003_0004", "X1C", "ru")
    row = type("St", (), {"to_dict": lambda self: {"device_type": "X1C", "notifications": [
        {"src": "hms", "code": "0500_0100_0003_0004", "text": ru}]}})()
    monkeypatch.setattr(cfg, "JWT_SECRET", getattr(cfg, "JWT_SECRET", ""), raising=False)
    app = api_main.create_app(type("S", (), {"get_all": lambda self: [row]})())
    route = [r for r in app.routes if getattr(r, "path", "") == "/api/i18n/texts"][0]
    assert route.endpoint(lang="en") == {ru: get_HMS_error_text("0500_0100_0003_0004", "X1C", "en")}
    assert route.endpoint(lang="xx") == {}


def test_asked_texts_find_bambus_english_by_the_russian_text():
    from app.vendor.pybambu.utils import get_print_error_text
    ru = get_print_error_text("0300801A", "", "ru")
    en = get_print_error_text("0300801A", "", "en")
    out = i18n_api.asked_texts(["0300_801A: " + ru, "Принтер у окна", 5, "x" * 3000], "en")
    assert out == {ru: en}
    assert i18n_api.asked_texts([ru], "de") == {} and i18n_api.asked_texts("нет", "en") == {}
    # the client sends at most MAX_ASK strings; more are ignored
    assert len(i18n_api.asked_texts([ru] * (i18n_api.MAX_ASK + 50), "en")) == 1
