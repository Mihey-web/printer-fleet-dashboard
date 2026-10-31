"""Confirmations, tips and command answers (IA §10, plan step 14) of app/web: no confirm() /
alert() / prompt() anywhere — one object is asked in place (the AMS popover, the AMS block,
the ⋯ menu, a park row, the apply bar, a proxy row), a group in its dialog; no native title —
one data-tip binding for the page, a tap shows it on a phone; a command's answer where it was
pressed (replyIn), a toast only when that place is gone. Pure helpers and small DOM functions
run in node against stubs (skipped without node), the rest are source checks.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "app" / "web"
JS = (WEB / "app.js").read_text(encoding="utf-8")
HTML = (WEB / "index.html").read_text(encoding="utf-8")
LOGIN = (WEB / "login.html").read_text(encoding="utf-8")
THEME = (WEB / "theme.js").read_text(encoding="utf-8")
CSS = (WEB / "styles.css").read_text(encoding="utf-8")


def _block(tag):
    m = re.search(r"/\* %s:begin \*/(.*?)/\* %s:end \*/" % (tag, tag), JS, re.S)
    assert m, tag
    return m.group(1)


def _js_function(name):
    """The whole `[async] function name(...) {...}` of app.js: braces matched, strings and // comments skipped."""
    m = re.search(r"(?:async )?function %s\([^)]*\) \{" % name, JS)
    assert m, name
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
    raise AssertionError(name)


def _safe_text():
    return re.search(r"function safeText\([^)]*\) \{.*?\n\s*\}", JS, re.S).group(0)


def _node(src):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    out = subprocess.run([node, "-"], input=src, capture_output=True, text=True, timeout=60, encoding="utf-8")
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _ams(expr):
    lib = "\n".join([_safe_text(), _js_function("icon"), _js_function("amsUnitName"), _js_function("amsFmtMin"),
                     _block("ams-ui")])
    return _node(lib + "\nconsole.log(JSON.stringify(" + expr + "));")


# --- no native dialogs, no native titles ------------------------------------------------------

NATIVE = re.compile(r"(?<![\w.$])(?:window\.)?(confirm|alert|prompt)\s*\(")


SOURCES = {"app.js": JS, "index.html": HTML, "login.html": LOGIN, "theme.js": THEME}


@pytest.mark.parametrize("name", ["app.js", "index.html", "login.html", "theme.js"])
def test_no_native_dialog_anywhere(name):
    src = SOURCES[name]
    assert NATIVE.findall(src) == [], name
    assert "window.confirm" not in src and "window.alert" not in src and "window.prompt" not in src
    # the one prompt() left is the browser's install offer (BeforeInstallPromptEvent), not a dialog
    if name == "app.js":
        assert src.count(".prompt()") == 1 and "e.prompt();" in src


@pytest.mark.parametrize("name", ["app.js", "index.html", "login.html"])
def test_no_native_title_anywhere(name):
    src = SOURCES[name]
    # IA §10: tips only as data-tip (one floating tooltip), never the native title; <title> of the page stays
    assert re.findall(r"\stitle=[\"']", src) == [], name
    assert 'setAttribute("title"' not in src and "setAttribute('title'" not in src
    assert re.findall(r"(?<!document)\.title\s*=", src) == [], name


# --- one tooltip for the page -------------------------------------------------------------

def test_one_tooltip_binding_for_the_page():
    init = JS[JS.index("(function initChipTooltips() {"):]
    init = init[:init.index("})();") + 5]
    assert re.findall(r"bindTips\(([^)]*)\);", JS) == ["document"]        # the cards, AMS, «Админ», popovers…
    assert 'root.addEventListener("pointerdown"' in init and '}, true);' in init
    assert "Date.now() - touchAt < 1000" in init                          # a tap's emulated hover is no hover
    assert "setTimeout(function () { if (curEl === el && el.isConnected) place(el); }, 250);" in init
    assert "timer = setTimeout(hide, TIP_TAP_MS);" in init and "var TIP_TAP_MS = 4000;" in JS
    assert 'window.addEventListener("scroll", hide, true);' in init
    # above every popover, sheet, panel and dialog (me-panel / fin-sheet 620, nf-pop 600, dialogs 500)
    z = [int(v) for v in re.findall(r"\n    \.pc-tip \{[^}]*?z-index: (\d+);", CSS)]
    layers = {sel: int(v) for sel, v in re.findall(
        r"\n    (\.me-panel|\.fin-sheet|\.nf-pop|\.ad-dlg-back|\.bd-modal) \{[^}]*?z-index: (\d+);", CSS)}
    assert z and z[-1] == 700 and len(layers) == 5 and max(layers.values()) < 700, layers


TAP_DOM = r"""
// a tiny element: tag, classes, attributes, a parent; closest() over simple selectors
function el(tag, cls, attrs, parent) {
  return { tag: tag, cls: cls || [], attrs: attrs || {}, parentNode: parent || null,
           disabled: !!(attrs && attrs.disabled),
           getAttribute: function (k) { return k in this.attrs ? String(this.attrs[k]) : null; },
           closest: function (sel) {
             var parts = sel.split(",").map(function (s) { return s.trim(); });
             for (var n = this; n; n = n.parentNode) {
               if (parts.some(function (s) { return match(n, s); })) return n;
             }
             return null;
           } };
}
function match(n, s) {
  var m = /^([a-z]*)((?:\.[\w-]+)*)((?:\[[^\]]+\])*)$/.exec(s);
  if (!m) throw new Error("selector " + s);
  if (m[1] && m[1] !== n.tag) return false;
  var cls = m[2] ? m[2].slice(1).split(".") : [];
  if (cls.some(function (c) { return n.cls.indexOf(c) < 0; })) return false;
  var attrs = m[3] ? m[3].slice(1, -1).split("][") : [];
  return attrs.every(function (a) {
    var kv = a.split("=");
    if (!(kv[0] in n.attrs)) return false;
    return kv.length < 2 || String(n.attrs[kv[0]]) === kv[1];
  });
}
"""


def test_a_tap_shows_the_tip_of_what_does_nothing_else():
    out = _node(TAP_DOM + _js_function("tipOnTap") + r"""
    var card = el("div", ["printer-card"]);
    var r = {
      chip: tipOnTap(el("span", ["pc-chip"], {"data-tip": "t"}, card)),                       // a chip: yes
      badge: tipOnTap(el("span", ["badge"], {"data-tip": "t"}, el("td", [], {}))),             // a badge in a row
      inChipBtn: tipOnTap(el("span", ["pc-ams-temp"], {"data-tip": "t"}, el("span", ["pc-chip", "pc-editable"], {}, card))),
      button: tipOnTap(el("button", ["farm-chip"], {"data-tip": "t"})),                         // works: it acts
      blocked: tipOnTap(el("button", ["fb-btn"], {"data-tip": "t", "aria-disabled": "true"})),  // says why
      disabled: tipOnTap(el("select", ["adm-role"], {"data-tip": "t", disabled: true})),
      link: tipOnTap(el("a", [], {"data-tip": "t", href: "#"})),
      label: tipOnTap(el("span", [], {"data-tip": "t"}, el("label", ["toggle-switch"]))),
      cardBtn: tipOnTap(el("button", ["pc-ctl"], {"data-tip": "t", "aria-disabled": "true"}, card)),  // its status line
      confirmQ: tipOnTap(el("span", ["q"], {"data-tip": "t"}, el("div", ["pc-confirm"], {}, card))),
      amsRow: tipOnTap(el("span", ["ams-badge"], {"data-tip": "t"}, el("button", ["ams-row"], {"data-ams-row": "k"})))
    };
    console.log(JSON.stringify(r));
    """)
    assert out == {"chip": True, "badge": True, "inChipBtn": False, "button": False, "blocked": True,
                   "disabled": True, "link": False, "label": False, "cardBtn": False, "confirmQ": False,
                   "amsRow": False}


# --- the answer where it was pressed ------------------------------------------------------

REPLY_DOM = r"""
var toasts = [], timers = [], CMD_RESULT_MS = 3000, made = 0;
function showToast(t, ok) { toasts.push([t, ok]); }
function setTimeout(fn, ms) { timers.push([fn, ms]); }
function slotEl() {
  var s = { className: "", innerHTML: "", scrolled: 0, dataset: {},
            classList: { contains: function (c) { return s.className.split(" ").indexOf(c) >= 0; } },
            scrollIntoView: function () { s.scrolled++; } };
  return s;
}
var document = { createElement: function () { made++; return slotEl(); } };
function box(onScreen, long) {
  return { isConnected: onScreen, children: [], scrollHeight: long ? 900 : 100, clientHeight: 100,
           getClientRects: function () { return onScreen ? [1] : []; },
           appendChild: function (c) { this.children.push(c); } };
}
"""


def test_reply_in_says_it_in_place_or_in_a_toast():
    out = _node(REPLY_DOM + _safe_text() + _js_function("cmdLineHtml") + _js_function("replyIn") + r"""
    var r = {}, b = box(true, false);
    r.waitIn = replyIn(b, {k: "wait", t: "Стоп сушки — ждём принтер…"});
    var slot = b.children[0];
    r.slot = [b.children.length, slot.className, /spin-sm/.test(slot.innerHTML), timers.length];
    r.okIn = replyIn(b, {k: "ok", t: "Сушка остановлена — подтверждено"});
    r.reused = [b.children.length, made, /✓/.test(slot.innerHTML), timers.length, timers[0][1]];
    timers[0][0]();                                     // CMD_RESULT_MS later: an answer fades
    r.faded = slot.innerHTML;
    r.badIn = replyIn(b, {k: "bad", t: "Принтер <занят>"});
    r.bad = [timers.length, /data-reply-x/.test(slot.innerHTML), /&lt;занят&gt;/.test(slot.innerHTML)];  // stays, ×
    timers[0][0]();                                     // an older fade does not wipe a newer line
    r.badKept = /занят/.test(slot.innerHTML);
    r.offOk = replyIn(box(false), {k: "ok", t: "Правки отменены"});
    r.offBad = replyIn(null, {k: "bad", t: "Ошибка сети"});
    r.offInfo = replyIn(box(false), {k: "info", t: "Карточка занята"});
    r.offWait = replyIn(box(false), {k: "wait", t: "ждём"});    // a wait line is never a toast
    r.none = replyIn(b, null);
    var lb = box(true, true);
    replyIn(lb, {k: "bad", t: "x"});
    r.scrolled = lb.children[0].scrolled;               // a long sheet shows its answer
    r.toasts = toasts;
    console.log(JSON.stringify(r));
    """)
    assert out["waitIn"] is True and out["slot"] == [1, "reply-slot", True, 0]
    assert out["okIn"] is True and out["reused"] == [1, 1, True, 1, 3000] and out["faded"] == ""
    assert out["badIn"] is True and out["bad"] == [1, True, True] and out["badKept"] is True
    assert (out["offOk"], out["offBad"], out["offInfo"], out["offWait"], out["none"]) == (False, False, False, False, False)
    assert out["toasts"] == [["Правки отменены", True], ["Ошибка сети", False], ["Карточка занята", True]]
    assert out["scrolled"] == 1


def test_a_reply_line_has_its_close_and_the_toasts_are_gone_from_commands():
    # × of a refusal clears its line (an AMS block's state too)
    x = JS[JS.index("// × of a reply line"):]
    x = x[:x.index("});") + 3]
    assert 'e.target.closest("[data-reply-x]")' in x and "slot.innerHTML = \"\";" in x
    assert "delete amsReply[slot.dataset.amsReply];" in x
    send = _js_function("sendPrinterCmd")
    assert "showToast" not in send and "return { ok: ok, line: cmdResult(action, out) };" in send
    card = _js_function("cardPopCmd")
    assert "replyIn(pcPop, cmdResult(action, o));" in card and "showToast" not in card
    pop = _js_function("onPopClick")
    assert 'replyIn(pcPop, { k: "info", t: CTL_MSG.busy }); return;' in pop and "showToast" not in pop
    maint = _js_function("setMaintenance")
    assert maint.count("replyIn(pcPop, { k: \"bad\"") == 2               # the form / the question says why
    assert '": снят с обслуживания"' in maint                              # done: the popover is closed — a toast
    trial = _js_function("adTrialOn")
    assert "replyIn(adTrialBox(), line);" in trial and "showToast" not in trial
    assert 'replyIn(adTrialBox(), { k: ok ? "ok" : "bad"' in _js_function("adDryRunOffDlg")
    rel = _js_function("adOnRelease")
    assert rel.count('replyIn(b.closest("td")') == 2 and "showToast" not in rel
    users = _js_function("admUserSend")
    assert "await loadAdminUsers();" in users and "replyIn(row ? row.querySelector('td.adm-actions') : null" in users
    assert "showToast" not in users
    perm = _js_function("updateNotificationSetting")
    assert "showToast" not in perm and perm.count("nfPermReply(") == 4
    assert 'document.getElementById("settingsList")' in _js_function("nfPermReply")
    restart = _js_function("prnWaitForRestart")
    assert "showToast" not in restart and "replyIn(txt, { k: 'wait', t: 'Сервис перезапускается…' });" in restart
    assert "loadAdminPrinters().then(function () {" in restart        # the bar goes when nothing is pending → a toast


# --- the AMS popover and the AMS block ask in place ----------------------------------------

def test_the_ams_popover_question():
    html = _ams('amsAskHtml("Загрузить слот 2 (<b>) на «P3»?", "Загрузить", \'data-ams-load-go="0:1"\')')
    assert html == ('<div class="pc-pop-ask" role="group" aria-label="Подтверждение"><div class="pc-pop-note ask">'
                    'Загрузить слот 2 (&lt;b&gt;) на «P3»?</div><div class="pc-pop-row">'
                    '<button type="button" class="pc-pop-preset" data-ams-back>Отмена</button>'
                    '<button type="button" class="pc-pop-ok" data-ams-load-go="0:1">Загрузить</button></div></div>')
    assert 'class="pc-pop-ok danger" data-dry-stop-go="0">Остановить<' in _ams(
        'amsAskHtml("x", "Остановить", \'data-dry-stop-go="0"\', true)')


DRYING = {"type": "n3f", "status": "drying", "phase": "dehumidify", "active": True, "remaining_min": 712,
          "temp": 55, "hours": 12, "filament": "PETG", "blockers": [], "feeding": False, "max_temp": 65}


def test_the_ams_block_asks_in_its_drying_line():
    u = {"printer_id": "b1", "unit_index": 0, "dry": DRYING, "dry_time": 712}
    p = {"id": "b1", "label": "P3", "ams_dry_ok": True}
    plain = _ams("amsTabDryHtml(%s, %s, true, null)" % (json.dumps(u), json.dumps(p)))
    asked = _ams("amsTabDryHtml(%s, %s, true, null, true)" % (json.dumps(u), json.dumps(p)))
    assert plain == _ams("amsTabDryHtml(%s, %s, true, null, false)" % (json.dumps(u), json.dumps(p)))
    assert 'data-ams-dry-stop data-pid="b1" data-ams="0">Стоп<' in plain and "asking" not in plain
    assert asked.startswith('<div class="ams-dry asking">') and "осталось 11:52" in asked
    assert ('<span class="ams-ask">Остановить сушку?</span><button type="button" class="pc-ctl cf-stop" '
            'data-ams-dry-stop-go data-pid="b1" data-ams="0">Стоп</button><button type="button" class="pc-ctl" '
            'data-ams-dry-stop-no data-pid="b1" data-ams="0">Отмена</button>') in asked
    assert "data-ams-dry-stop " not in asked                                     # no plain Stop beside it
    # a viewer is never asked; an idle block keeps «Сушить…»
    viewer = _ams("amsTabDryHtml(%s, %s, false, null, true)" % (json.dumps(u), json.dumps(p)))
    assert "button" not in viewer and "asking" not in viewer
    idle = dict(u, dry=dict(DRYING, status="off", active=False, remaining_min=None), dry_time=None)
    assert "data-ams-dry-open" in _ams("amsTabDryHtml(%s, %s, true, null, true)" % (json.dumps(idle), json.dumps(p)))


def test_the_ams_wiring_asks_then_sends_and_answers_on_the_block():
    click = JS[JS.index("if (grid) grid.addEventListener('click', function (e) {"):]
    click = click[:click.index("\n      // The AMS tab answers")]
    assert "[data-ams-dry-open],[data-ams-dry-stop],[data-ams-dry-stop-go],[data-ams-dry-stop-no]" in click
    assert "amsAskSet(key);" in click and "amsAskSet(null);" in click
    assert "if (amsReply[key] && amsReply[key].k === 'wait')" in click             # no second stop meanwhile
    go = click[click.index("amsReplySet(key, cmdWaitLine('dry_stop'));"):]
    assert "sendPrinterCmd(pid, 'dry_stop', { ams: idx }).then(function (r) { amsReplySet(key, r.line); });" in go
    ask = _js_function("amsAskSet")
    assert "}, CONFIRM_MS);" in ask and "amsDryRedraw(key);" in ask              # cancels itself, as a card's
    redraw = _js_function("amsDryRedraw")
    assert "tmp.innerHTML = amsCardShell(u, false);" in redraw and "old.replaceWith(fresh);" in redraw
    rs = _js_function("amsReplySet")
    assert "if (!replyIn(card, line)) { delete amsReply[key]; return; }" in rs
    assert "amsUnitsShown = units;" in _js_function("loadAms")
    # the popover: a question first, its «…-go» sends; «Отмена» back to the slots or the external spool
    ams = _js_function("onAmsPopClick")
    for need in ('swapPop(amsAskHtml(amsLoadConfirmText(p, ams, slot), "Загрузить", \'data-ams-load-go="\' + ams + ":" + slot + \'"\'), "ams");',
                 'swapPop(amsAskHtml(amsUnloadConfirmText(p), "Выгрузить", "data-ams-unload-go"), "ams");',
                 'await popCmd(pid, "ams_unload", null);', 'await popCmd(pid, "dry_stop", { ams: parseInt(t.dataset.dryStopGo, 10) });',
                 'swapPop(amsUnits(p).length ? amsPopHtml(p) : extSpoolPopHtml(p), "ams");'):
        assert need in ams, need
    sends = re.search(r"var sends = t\.matches\(\"([^\"]*)\"\)", _js_function("onPopClick")).group(1).split(",")
    assert sends == ["[data-speed]", "[data-ok]", "[data-ams-load-go]", "[data-ams-unload-go]", "[data-dry-start]",
                     "[data-dry-stop-go]"]


def test_the_popover_question_sends_only_from_its_go_button():
    # onAmsPopClick in node: load / unload / stop ask (no command), their «…-go» send, «Отмена» goes back
    p = {"id": "b1", "label": "P3", "filament_loaded": True,
         "ams": {"tray_now": 0, "units": [{"index": 0, "slots": [{"type": "PLA"}, {"type": "PETG"}], "dry": {}}]}}
    src = "\n".join([_safe_text(), _js_function("icon"), _js_function("amsUnitName"), _js_function("amsFmtMin"),
                     _block("ams-ui")]) + r"""
    var log = [], P = %s, pcPop = {querySelector: function () { return null; }, querySelectorAll: function () { return []; }};
    function findPrinter() { return P; }
    function closePop() { log.push(["close"]); }
    function swapPop(html, kind) { log.push(["swap", kind, (html.match(/data-[\w-]+(="[^"]*")?(?=>)/g) || []).join(" ")]); }
    function popCmd(pid, action, extra) { log.push(["send", action, extra]); return Promise.resolve(true); }
    function amsPopHtml() { return '<b data-ams-pop>'; }
    function extSpoolPopHtml() { return '<b data-ext-pop>'; }
    function t(data, attrs) {
      return {dataset: data || {}, hasAttribute: function (k) { return (attrs || []).indexOf(k) >= 0; }};
    }
    """ % json.dumps(p) + _js_function("onAmsPopClick") + r"""
    (async function () {
      await onAmsPopClick("b1", t({amsLoad: "0:1"}));
      await onAmsPopClick("b1", t({amsLoadGo: "0:1"}));
      await onAmsPopClick("b1", t({}, ["data-ams-unload"]));
      await onAmsPopClick("b1", t({}, ["data-ams-unload-go"]));
      await onAmsPopClick("b1", t({dryStop: "0"}));
      await onAmsPopClick("b1", t({dryStopGo: "0"}));
      await onAmsPopClick("b1", t({}, ["data-ams-back"]));
      P.ams = null;
      await onAmsPopClick("b1", t({}, ["data-ams-back"]));
      console.log(JSON.stringify(log));
    })();
    """
    log = _node(src)
    assert log == [
        ["swap", "ams", 'data-ams-back data-ams-load-go="0:1"'],
        ["send", "ams_load", {"ams": 0, "slot": 1}],
        ["swap", "ams", "data-ams-back data-ams-unload-go"],
        ["send", "ams_unload", None],
        ["swap", "ams", 'data-ams-back data-dry-stop-go="0"'],
        ["send", "dry_stop", {"ams": 0}],
        ["swap", "ams", "data-ams-pop"],
        ["swap", "ams", "data-ext-pop"],
    ]


# --- the ⋯ menu, the park, the apply bar, the proxies --------------------------------------

def test_the_menu_asks_before_maintenance_ends():
    lib = "\n".join([_safe_text(), _js_function("maintOffAskHtml")])
    html = _node(lib + '\nconsole.log(JSON.stringify(maintOffAskHtml({label: "P<3>"})));')
    assert html == ('<div class="pc-pop-ask" role="group" aria-label="Подтверждение"><div class="pc-pop-note ask">Снять «P&lt;3&gt;» '
                    'с обслуживания? Принтер вернётся в сетку, уведомления и групповые команды.</div><div class="pc-pop-row">'
                    '<button type="button" class="pc-pop-preset" data-menu="back">Отмена</button>'
                    '<button type="button" class="pc-pop-ok" data-menu="maint-off-yes">Снять</button></div></div>')
    click = _js_function("onMenuClick")
    assert click.index('if (what === "maint-off") {') < click.index("closePop(true);")   # asked, the popover stays
    assert 'swapPop(ctlMenuHtml(p, cardUiOf(pid), Date.now(), menuOpts(p)), "menu");' in click
    assert 'if (isAdmin()) setMaintenance(pid, false);' in click


def test_a_park_row_asks_before_a_delete():
    lib = "\n".join([_safe_text(), _block("adm-ui").split("var USER_ROLE_RU")[0]])
    out = _node(lib + r"""
    var p = {id: "bambu-3", label: "P<3>"};
    console.log(JSON.stringify([parkActsHtml(p, null), parkActsHtml(p, "bambu-3"), parkActsHtml(p, "bambu-30"),
                                parkActsHtml({id: "bambu-3", label: "P3", pending: "deleted"}, "bambu-3")]));
    """)
    plain, asked, other, deleted = out
    assert plain == ('<button type="button" class="ghost-btn" data-act="edit" data-id="bambu-3">Изменить</button> '
                     '<button type="button" class="ghost-btn danger" data-act="delete" data-id="bambu-3">Удалить</button>')
    assert asked == ('<div class="adm-inrow"><span class="adm-ask">Удалить «P&lt;3&gt;»? История печати останется.</span>'
                     '<button type="button" class="ghost-btn danger" data-act="del-yes" data-id="bambu-3">Удалить</button>'
                     '<button type="button" class="adm-link" data-act="del-no" data-id="bambu-3">Отмена</button></div>')
    assert other == plain                                                       # bambu-30 is another printer
    assert deleted == '<button type="button" class="ghost-btn" data-act="restore" data-id="bambu-3">Вернуть</button>'
    init = _js_function("initAdminPrinters")
    assert "var actions = parkActsHtml(p, prnAsk);" in _js_function("renderAdminPrinters")
    for need in ("prnAsk = id;", "btn.dataset.act === 'del-no'", "btn.dataset.act === 'del-yes'",
                 "prnRowReply(id, 'Ошибка сети');", "prnBarAsk('discard');", "prnBarAsk('apply');", "prnBarAsk(null);"):
        assert need in init, need


def test_the_apply_bar_asks_in_itself():
    bar = HTML[HTML.index('<div class="apply-bar" id="printersApplyBar"'):]
    bar = bar[:bar.index('<div class="adm-pane"')]
    for need in ('id="printersApplyTxt"', 'id="printersDiscardBtn"', 'id="printersApplyBtn"',
                 '<div class="apply-ask" id="printersAsk" role="group" aria-label="Подтверждение" hidden>',
                 'id="printersAskText"', 'id="printersAskNo">Отмена</button>', 'id="printersAskYes"'):
        assert need in bar, need
    ask = _js_function("prnBarAsk")
    assert "'Применить правки (' + n + ') и перезапустить сервис? Панель будет недоступна ~10 секунд.'" in ask
    assert "'Отменить все непримененные правки принтеров (' + n + ')?'" in ask
    assert "ask.hidden = !what;" in ask and "apply.hidden = discard.hidden = !!what;" in ask
    assert "b.disabled = prnRestarting;" in ask
    assert "if (!pending) prnBarAsk(null);" in _js_function("renderAdminPrinters")
    assert ".apply-ask[hidden] { display: none; }" in CSS


def test_a_proxy_row_asks_before_a_delete():
    render = _js_function("renderProxies")
    assert "var acts = proxyAsk === p.url" in render and 'data-act="del-yes">Удалить</button>' in render
    assert 'data-act="del-no">Отмена</button>' in render and "proxyTg = tg;" in render
    handler = JS[JS.index("document.querySelector('#proxyTable tbody').addEventListener('click'"):]
    handler = handler[:handler.index("srvEl('proxyCheckNowBtn')")]
    assert "proxyAsk = btn.dataset.act === 'del' ? url : null;" in handler
    assert handler.index("btn.dataset.act === 'del-yes'") < handler.index("saveServerSettings({ proxy_list: list }")


def test_the_styles_of_the_questions_and_the_answers():
    for rule in (".reply-slot:empty { display: none; }", ".pc-pop-note.ask {", ".pc-pop-ok.danger {",
                 ".ams-dry .ams-ask {", ".ams-dry.asking .pc-ctl { margin-left: 0; }", ".apply-ask {",
                 ".discard-btn.danger {", '.pc-pop-preset[aria-disabled="true"] { cursor: default; }',
                 '.pc-pop-preset:hover:not(:disabled):not([aria-disabled="true"]) {'):
        assert rule in CSS, rule


def test_cache_bust_bumped():
    # plan step 14 changed app.js and styles.css, step 16 both again: the versions only grow
    assert int(re.search(r"styles\.css\?v=(\d+)", HTML).group(1)) >= 101
    assert int(re.search(r"app\.js\?v=(\d+)", HTML).group(1)) >= 118


def test_the_park_table_survives_a_refusal():
    # plan step 16 (the matrix): a 403 / 500 body is no list — the table keeps its rows and the console
    # says «HTTP 403», not «prnRows.forEach is not a function»
    load = _js_function("loadAdminPrinters")
    assert load.index("if (!res.ok) throw new Error('HTTP ' + res.status);") < load.index("prnRows = await res.json();")
