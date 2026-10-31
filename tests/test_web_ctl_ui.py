"""Card controls, ⋯ menu, command results and adaptive chip rows of app/web/app.js, run in node.

The block between /* ctl-ui:begin */ and /* ctl-ui:end */ holds only pure code;
it may use safeText, icon, amsUnitName, amsFmtMin, isOffline (with hasTelemetry)
(function declarations elsewhere in app.js) and the ams-ui block, which are
extracted alongside it.
Skipped without node.
"""
import itertools
import json
import random
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app.services import printer_commands

ROOT = Path(__file__).resolve().parents[1]
JS = (ROOT / "app" / "web" / "app.js").read_text(encoding="utf-8")
CSS = (ROOT / "app" / "web" / "styles.css").read_text(encoding="utf-8")


def _function(name: str) -> str:
    m = re.search(r"function %s\([^)]*\) \{.*?\n\s*\}" % name, JS, re.S)
    assert m, "%s() not found in app.js" % name
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
    out = subprocess.run([node, "-"], input=src, capture_output=True, text=True, timeout=60, encoding="utf-8")
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _lib() -> str:
    src = "\n".join(_function(n) for n in ("safeText", "icon", "amsUnitName", "amsFmtMin", "hasTelemetry", "isOffline"))
    return src + "\n" + _block("ams-ui") + "\n" + _block("ctl-ui")


def run(expr: str):
    """Evaluate `expr` (JSON-serializable) against the helper blocks in node."""
    return _node(_lib() + "\nconsole.log(JSON.stringify(" + expr + "));")


def test_block_is_pure():
    block = _block("ctl-ui")
    for banned in ("document.", "window.", "fetch(", "apiFetch", "cachedAllPrinters", "localStorage"):
        assert banned not in block, banned


# --- the card's control row --------------------------------------------------------

CAP_KEYS = ("light", "pause", "resume", "stop", "cooldown", "eject", "tune", "ams")
ALL_CAPS = dict.fromkeys(CAP_KEYS, True)
NO_CAPS = dict.fromkeys(CAP_KEYS, False)
ADMIN = {"admin": True, "offline": False, "now": 1000}
MSG = printer_commands.STATE_MSG
GRACE = "Нет связи с принтером — данные устарели"
EJECT_WAIT = "Стол уже опускается — подождите"
COOL_PAUSED = "Стол остынет — деталь может отклеиться. Подогреет ли принтер при продолжении — не проверено."


def _p(**kw):
    p = {"id": "b1", "label": "P1", "kind": "bambu", "state": "printing", "online": True, "light_on": True,
         "grace_period_active": False, "chamber_ctl": None, "caps": dict(ALL_CAPS), "caps_note": None,
         "filament_swap_ok": True, "filament_loaded": True, "dual_nozzle": False, "filament_change": None,
         "feedrate_pct": 100,
         "ams": {"tray_now": 0, "units": [{"index": 0, "slots": [{"type": "PLA", "color": "FF0000", "empty": False}]}]}}
    p.update(kw)
    return p


def row(p, ui=None, opts=None):
    return run("ctlRowHtml(%s, %s, %s)" % (json.dumps(p), json.dumps(ui), json.dumps(opts or ADMIN)))


_BTN = re.compile(r'<button type="button" class="pc-ctl([^"]*)" data-cmd="([^"]*)" data-slot="([^"]*)" '
                  r'data-tip="([^"]*)" aria-label="([^"]*)"( aria-disabled="true")?>')


def buttons(html):
    """[(slot, cmd, classes, tip, disabled)] of the row's buttons, in order."""
    out = []
    for cls, cmd, slot, tip, label, dis in _BTN.findall(html):
        assert tip == label
        out.append((slot, cmd, cls.split(), tip, bool(dis)))
    return out


def test_no_row_for_viewers_or_offline_cards():
    assert row(_p(), opts={"admin": False, "offline": False, "now": 0}) == ""
    assert row(_p(), opts={"admin": True, "offline": True, "now": 0}) == ""


def test_bambu_printing_has_five_buttons():
    # IA §5 (plan step 5, deliberately changed from seven): 💡 ⏸/▶ ■ ❄ ⤓ — no 🧵 (the AMS chip opens
    # the AMS popover) and no ⋯ (the card's one ⋯ is in its head, test_one_more_in_the_head_of_every_card)
    html = row(_p())
    b = buttons(html)
    assert [x[0] for x in b] == ["light", "pp", "stop", "cool", "eject"]
    assert [x[1] for x in b] == ["light", "pause", "stop", "cool", "eject"]
    assert [x[4] for x in b] == [False, False, False, True, True]
    assert b[3][3] == MSG["cooldown"] and b[4][3] == MSG["eject"]
    assert b[0][2] == ["lit"] and b[0][3] == "Свет горит — выключить"
    assert b[1][3] == "Пауза" and b[2][2] == ["danger"] and b[2][3] == "Остановить печать"
    assert 'data-cmd="ams"' not in html and 'data-cmd="more"' not in html
    assert html.startswith('<div class="pc-controls" role="toolbar" aria-label="Управление принтером">')
    assert not re.search(r"\sdisabled[\s>=]", html)          # aria-disabled only: tips and taps still work


def test_paused_finished_and_idle_states():
    b = buttons(row(_p(state="paused")))
    assert (b[1][1], b[1][3], b[1][4]) == ("resume", "Продолжить печать", False)
    assert b[3][4] is False and (b[4][4], b[4][3]) == (True, MSG["eject"])
    b = buttons(row(_p(state="finished")))
    assert (b[1][4], b[1][3]) == (True, MSG["pause_finished"]) and (b[2][4], b[2][3]) == (True, MSG["stop"])
    assert b[3][4] is False and b[4][4] is False
    b = buttons(row(_p(state="idle", chamber_ctl=True)))
    assert (b[1][3], b[3][3]) == (MSG["pause_other"], "Остудить всё: сопло, стол, камера")
    assert buttons(row(_p(state="idle")))[3][3] == "Остудить всё: сопло и стол"
    assert buttons(row(_p(state="error")))[1][3] == MSG["pause_error"]
    b = buttons(row(_p(state="idle", filament_change={"action": "load", "step": "heat"})))
    assert b[3][3] == b[4][3] == "Идёт смена филамента — дождитесь окончания"


@pytest.mark.parametrize("light,cls,tip", [(True, ["lit"], "Свет горит — выключить"),
                                           (False, [], "Свет выключен — включить"),
                                           (None, [], "Свет: состояние неизвестно — включить")])
def test_light_button(light, cls, tip):
    b = buttons(row(_p(light_on=light)))[0]
    assert (b[2], b[3], b[4]) == (cls, tip, False)


def test_eject_guard_disables_the_button_until_it_lapses():
    ui = {"ejectUntil": 1500}
    b = buttons(row(_p(state="idle"), ui, dict(ADMIN, now=1000)))[4]
    assert (b[4], b[3]) == (True, EJECT_WAIT)
    assert buttons(row(_p(state="idle"), ui, dict(ADMIN, now=1600)))[4][4] is False


EXT_CHIP = ('<span class="pc-chip pc-ext pc-editable" data-edit="ext" data-tip="Внешняя катушка: выгрузка">'
            '<svg class="pc-ic" aria-hidden="true"><use href="#i-spool"></use></svg>внешняя катушка</span>')


def test_external_spool_chip_without_an_ams():
    # The row's 🧵 (gone, IA §5) was also the only way to unload an external spool: on a printer without
    # an AMS its chip takes the AMS chip's place (an admin's live card) and opens the same popover, which
    # says why it cannot unload now (test_external_spool_popover)
    for st in ("idle", "printing"):
        p = _p(state=st, ams=None, filament_loaded=True)
        assert 'data-cmd="ams"' not in row(p)
        chip = run("extSpoolChip(%s, true)" % json.dumps(p))
        # the tip says why it cannot unload now, as the 🧵 did
        assert chip == EXT_CHIP if st == "idle" else chip == EXT_CHIP.replace(
            "Внешняя катушка: выгрузка", "Внешняя катушка: Только в простое — принтер печатает или на паузе")
    assert run("extSpoolChip(%s, false)" % json.dumps(_p(ams=None, filament_loaded=True))) == ""   # a viewer, offline, grace
    assert run("extSpoolChip(%s, true)" % json.dumps(_p(ams=None, filament_loaded=False))) == ""   # nothing to unload
    assert run("extSpoolChip(%s, true)" % json.dumps(_p(ams=None, filament_loaded=None))) == ""
    assert run("extSpoolChip(%s, true)" % json.dumps(_p())) == ""                                  # an AMS: its own chip
    card = _decl("renderPrinterCard")
    assert "var canAms = isAdmin() && !offlineFlag && caps.ams === true && !p.grace_period_active;" in card
    assert "var sub = amsChip(p, canAms, true) || extSpoolChip(p, canAms);" in card
    assert "${sub ? '<div class=\"pc-subrow\">' + sub + '</div>' : \"\"}" in card      # no empty row
    assert "printer-meta" not in card and "printer-meta" not in CSS        # «BAMBU · X1C»: the model is in the head
    ctl = JS[JS.index("(function initPrinterControls() {"):JS.index("// --- Printer notifications")]
    assert 'kind === "ext" ? extSpoolPopHtml(pa) : chip.dataset.edit ? amsPopHtml(pa) : amsViewPopHtml(pa)' in ctl


def test_print_control_locked_keeps_the_light():
    caps = dict(NO_CAPS, light=True)
    html = row(_p(caps=caps, caps_note="прошивка блокирует (нет Developer Mode)"))
    assert [x[0] for x in buttons(html)] == ["light"]
    assert ('<span class="pc-ctl-lock" data-tip="Печать: прошивка блокирует (нет Developer Mode)">'
            '<svg class="pc-ic" aria-hidden="true"><use href="#i-lock"></use></svg>'
            '<span>Печать: прошивка блокирует (нет Developer Mode)</span></span>') in html
    evil = row(_p(caps=caps, caps_note="<img src=x>"))
    assert "<img" not in evil and "&lt;img src=x&gt;" in evil
    assert "управление не подключено" in row(_p(caps=caps, caps_note=None))
    assert row(_p(caps=dict(NO_CAPS, light=None), caps_note="управление не подключено")) == ""


def test_creality_and_klipper_rows():
    creality = dict(NO_CAPS, pause=True, resume=True, stop=True, light=None)
    # the card's ⋯ is in its head now, on every card (IA §5) — not at the end of the row
    assert [x[0] for x in buttons(row(_p(kind="creality", caps=creality, ams=None)))] == ["pp", "stop"]
    listed = dict(creality, light=True)
    assert [x[0] for x in buttons(row(_p(kind="creality", caps=listed, ams=None)))] == ["light", "pp", "stop"]
    klipper = dict(NO_CAPS, pause=True, resume=True, stop=True, light=True)
    for state, pp in (("idle", "pause"), ("paused", "resume"), ("printing", "pause")):
        b = buttons(row(_p(kind="klipper", caps=klipper, state=state, ams=None)))
        assert [(x[0], x[1]) for x in b] == [("light", "light"), ("pp", pp), ("stop", "stop")]


def test_grace_disables_every_button():
    b = buttons(row(_p(grace_period_active=True)))
    assert len(b) == 5 and all(x[4] and x[3] == GRACE for x in b)


def test_busy_result_and_open_popover_classes():
    b = buttons(row(_p(), {"busy": "pp", "res": {"slot": "light", "k": "ok"}, "pop": "menu"}))
    assert "is-wait" in b[1][2] and "is-ok" in b[0][2] and not any("open" in x[2] for x in b)
    b = buttons(row(_p(), {"res": {"slot": "stop", "k": "bad"}, "pop": "ams"}))
    assert "is-bad" in b[2][2] and not any("open" in x[2] for x in b)
    # an open ⋯ menu marks the head's ⋯, the only one
    assert buttons(run("headMoreHtml(%s, {pop: 'menu'}, {admin: true})" % json.dumps(_p())))[0][2] == ["pc-ctl-head", "open"]
    assert buttons(run("headMoreHtml(%s, {pop: 'ams'}, {admin: true})" % json.dumps(_p())))[0][2] == ["pc-ctl-head"]


def test_inline_confirm_replaces_the_row():
    html = row(_p(), {"confirm": "stop"})
    assert buttons(html) == []
    assert html.startswith('<div class="pc-confirm" role="group" aria-label="Подтверждение">'
                           '<span class="q"><svg class="pc-ic" aria-hidden="true"><use href="#i-stop"></use></svg>'
                           'Остановить печать?</span>')
    assert '<button type="button" class="cf-stop" data-cmd="confirm-yes">Стоп</button>' in html
    assert '<button type="button" class="cf-cancel" data-cmd="confirm-no">Отмена</button>' in html
    html = row(_p(state="paused"), {"confirm": "cooldown", "line": {"k": "info", "t": "x"}})
    assert ('<span class="q" data-tip="%s"><svg class="pc-ic" aria-hidden="true"><use href="#i-snow"></use></svg>'
            'На паузе — остудить?</span>' % COOL_PAUSED) in html
    assert '<button type="button" class="cf-go" data-cmd="confirm-yes">Остудить</button>' in html
    assert '<div class="pc-cmd info" role="status">' in html            # the status line stays


# --- command results and the status line ------------------------------------------------

NET_LOST = "связь прервалась — итог покажет статус принтера"


@pytest.mark.parametrize("action,out,want", [
    ("pause", {"status": "confirmed", "detail": "подтверждено принтером"}, {"k": "ok", "t": "Пауза — подтверждено"}),
    ("light_on", {"status": "confirmed"}, {"k": "ok", "t": "Свет включён — подтверждено"}),
    ("light_off", {"status": "sent", "detail": "отправлено (без подтверждения)"},
     {"k": "unk", "t": "Свет — отправлено, ответа нет"}),
    ("eject", {"status": "sent", "detail": NET_LOST}, {"k": "unk", "t": "Стол вниз — " + NET_LOST}),
    ("cooldown", {"status": "refused", "detail": MSG["cooldown"]}, {"k": "bad", "t": "Остудить — " + MSG["cooldown"]}),
    ("stop", {"status": "rejected", "detail": ""}, {"k": "bad", "t": "Стоп — ошибка"}),
    ("dry_start", {"status": "confirmed"}, {"k": "ok", "t": "Сушка запущена — подтверждено"}),
    # H2 / X2: lamp 1 confirmed, lamp 2 did not — the server's note is on the card, so a
    # bulb still lit after the next poll is explained
    ("light_off", {"status": "confirmed", "detail": "подтверждено принтером (вторая лампа: без ответа)"},
     {"k": "ok", "t": "Свет выключен — подтверждено принтером (вторая лампа: без ответа)"}),
])
def test_cmd_result(action, out, want):
    assert run("cmdResult(%s, %s)" % (json.dumps(action), json.dumps(out))) == want
    assert run("cmdWaitLine('resume')") == {"k": "wait", "t": "Продолжить — ждём принтер…"}


def test_cmd_line_html():
    assert run("cmdLineHtml(null)") == ""
    assert run("cmdLineHtml({k: 'wait', t: 'Пауза — ждём принтер…'})") == (
        '<div class="pc-cmd wait" role="status"><span class="lead"><span class="spin-sm"></span></span>'
        '<span class="t">Пауза — ждём принтер…</span></div>')
    bad = run("cmdLineHtml({k: 'bad', t: 'Стоп — <b>x</b>'})")
    assert "<b>" not in bad and "&lt;b&gt;x&lt;/b&gt;" in bad
    assert bad.endswith('<button type="button" class="x" data-cmd="dismiss" aria-label="Закрыть">×</button></div>')
    for k, lead in (("ok", "✓"), ("unk", "?"), ("info", "ⓘ")):
        html = run("cmdLineHtml({k: '%s', t: 't'})" % k)
        assert '<span class="lead">%s</span>' % lead in html and 'data-cmd="dismiss"' not in html


def test_cmd_line_html_takes_another_dismiss_attribute():
    # the notification lists and the card's notification line close their answers themselves
    bad = run("cmdLineHtml({k: 'bad', t: 'Отказ'}, 'data-nf-dismiss=\"b1|pe:0500_8062\"')")
    assert bad.endswith('<button type="button" class="x" data-nf-dismiss="b1|pe:0500_8062" aria-label="Закрыть">×</button></div>')
    assert "data-cmd" not in bad
    assert 'data-cmd="dismiss"' in run("cmdLineHtml({k: 'bad', t: 'x'}, '')")      # empty: the default
    assert "data-nf" not in run("cmdLineHtml({k: 'ok', t: 'x'}, 'data-nf-dismiss=\"a\"')")   # only a failure closes


# --- the ⋯ menu and the external spool popover ---------------------------------------

def menu(p, ui=None, now=1000):
    return run("ctlMenuHtml(%s, %s, %d)" % (json.dumps(p), json.dumps(ui), now))


_ITEM = re.compile(r'<button type="button" class="pm-item" data-menu="([^"]*)"( aria-disabled="true")?>')


def test_menu_repeats_nothing_from_the_card():
    # owner, 19.10: cooldown, eject and the speed are the row's ❄ / ⤓ buttons and the
    # speed chip — the ⋯ menu only repeated them. IA §5 (plan step 5): it holds an admin's
    # maintenance section and the links; a viewer gets the links
    for p in (_p(), _p(state="paused", chamber_ctl=True, feedrate_pct=124), _p(state="idle"),
              _p(caps=dict(NO_CAPS, tune=True, pause=True))):
        viewer = menu(p)
        assert [m for m, _d in _ITEM.findall(viewer)] == ["jobs", "chrono"] and "pm-sep" not in viewer
        html = run("ctlMenuHtml(%s, null, 1000, {admin: true})" % json.dumps(p))
        assert [m for m, _d in _ITEM.findall(html)] == ["maint-open", "jobs", "chrono", "params"]
        assert html.count("pm-sep") == 1
        for gone in ("Остудить", "Опустить стол", "Скорость", "data-speed"):
            assert gone not in html and gone not in viewer, gone


def test_menu_links():
    # IA §5: «Печати / Хронология принтера ›» (both roles), «Параметры принтера ›» (admins)
    def ic(name):
        return '<svg class="pc-ic" aria-hidden="true"><use href="#i-%s"></use></svg>' % name
    html = run("ctlMenuHtml(%s, null, 1000, {admin: true})" % json.dumps(_p()))
    assert html.endswith('<div class="pm-sep"></div>'
                         '<button type="button" class="pm-item" data-menu="jobs">%s<span class="lbl">Печати принтера ›</span></button>'
                         '<button type="button" class="pm-item" data-menu="chrono">%s<span class="lbl">Хронология принтера ›</span></button>'
                         '<button type="button" class="pm-item" data-menu="params">%s<span class="lbl">Параметры принтера ›'
                         '<span class="sub">Админ → Парк, откроется форма</span></span></button>' % (ic("file"), ic("clock"), ic("tool")))
    sprite = set(re.findall(r'<symbol id="i-([\w-]+)"', HTML))
    assert {"file", "clock", "tool", "dots"} <= sprite
    # the links: the printer's stable id (IA §10) in the one head of «История», the period kept (plan
    # step 8, deliberately changed): #/history/jobs?printer=<id> or #/history/chrono?printer=<id>
    click = _decl("onMenuClick")
    assert 'if (what === "jobs" || what === "chrono") historyOpenFor(what, p);' in click
    assert 'else if (what === "params") adminOpenPrinter(p);' in click
    hist = _decl("historyOpenFor")
    for line in ("printer: String(p.id) });", "period: histFilter.period, from: histFilter.from, to: histFilter.to,",
                 "histRenderHead();", 'setHistorySub(what === "jobs" ? "jobs" : "chrono");', 'setActiveView("history");'):
        assert line in hist, line
    assert hist.index("histFilter = histFilterFrom(") < hist.index("setHistorySub(") < hist.index('setActiveView("history");')
    for gone in ("jobsPrinterLabel", "historyPrinterLabel", "timelinePrinter", "jobsFilter.printer"):
        assert gone not in hist, gone
    adm = _decl("adminOpenPrinter")
    assert adm.index("if (!isAdmin()) return;") < adm.index("prnOpenAfterLoad = String(p.id);")
    assert 'setAdminSub("park");' in adm and 'setActiveView("admin");' in adm
    load = _decl("loadAdminPrinters")
    assert load.index("renderAdminPrinters();") < load.index("prnOpenAfterLoad = null;") < load.index("prnOpenForm(row.id);")
    assert "var prnOpenAfterLoad = null;" in JS


def test_external_spool_popover():
    html = run("extSpoolPopHtml(%s)" % json.dumps(_p(state="idle", ams=None)))
    assert html == ('<div class="pc-pop-h">Внешняя катушка</div><div class="pc-pop-row">'
                    '<button type="button" class="pc-pop-ok" data-ams-unload>Выгрузить филамент</button></div>')
    html = run("extSpoolPopHtml(%s)" % json.dumps(_p(state="printing", ams=None)))
    assert "data-ams-unload" not in html and "Только в простое" in html


def test_state_block():
    cases = run("[ctlStateBlock('speed', {state: 'idle'}), ctlStateBlock('speed', {state: 'paused'}),"
                " ctlStateBlock('resume', {state: 'printing'}), ctlStateBlock(null, {state: 'idle'})]")
    assert cases == ["Скорость меняется только во время печати", None, MSG["resume"], None]


# --- parity with the server ---------------------------------------------------------

def test_refusal_texts_match_the_server():
    block = _block("ctl-ui")
    for text in list(MSG.values()) + [printer_commands.MSG_CHANGE_RUNNING, printer_commands.MSG_EJECT_INFLIGHT]:
        assert '"%s"' % text in block, text
    ctl = run("CTL_MSG")
    assert {k: ctl.get(k) for k in MSG} == MSG


# --- layout guard ------------------------------------------------------------------------

def _rule(selector):
    m = re.search(r"(?m)^\s*%s\s*\{([^}]*)\}" % re.escape(selector), CSS)
    assert m, selector
    return m.group(1)


def test_layout_guard():
    assert re.search(r"\.printer-grid\s*\{[^}]*minmax\(min\(300px, 100%\), 1fr\)", CSS)
    ctl = _rule(".pc-controls .pc-ctl")
    assert "flex: 1 1 0" in ctl and "min-width: 0" in ctl
    assert re.search(r"\.printer-card\s*\{[^}]*container-type: inline-size", CSS)
    assert "--chip-k" in _rule(".pc-chips[data-balanced] > .pc-chip")
    assert not re.search(r"\.pc-ctl\[data-cmd=", CSS) and "pc-ctl-on" not in CSS and "pc-ctl-danger" not in CSS


def test_status_line_wraps_instead_of_cutting_the_reason():
    # On a phone a tap on a greyed-out button puts its reason (up to ~95 characters,
    # CTL_MSG.cool_paused) into the status line, the only place it shows: the text wraps
    # instead of an ellipsis after ~45 characters, and is never cut — not by a line clamp
    # either (at 150 % text scale the longest reason takes 4 lines on a 375 px phone; a
    # Klipper error up to ~225 characters, more).
    t = _rule(".pc-cmd .t")
    for cut in ("nowrap", "ellipsis", "line-clamp", "overflow: hidden", "max-height"):
        assert cut not in t, cut
    assert "overflow-wrap: anywhere" in t
    assert "align-items: flex-start" in _rule(".pc-cmd")                      # the mark on the first line
    assert "height: 1.3em" in _rule(".pc-cmd .lead")                         # ... centred in it


def test_bulk_dialog_footer_stays_in_view():
    # «Пауза выбранных (N)» is the urgent action: with a long list on a phone (12 printing
    # printers at 375×812: 1139 px of dialog in 787 px) it stays at the bottom of the dialog's
    # view instead of under the fold. Chrome sticks it at the content edge: the negative
    # offset and margin equal .bd's padding, so the footer sits on the dialog's border, its
    # padding keeps the buttons where they were and its background hides the rows under it.
    pad = re.search(r"padding: (\d+)px", _rule(".bd")).group(1)
    foot = _rule(".bd-foot")
    assert "position: sticky" in foot
    assert re.search(r"(?<![\w-])bottom: -%spx" % pad, foot) and "margin-bottom: -%spx" % pad in foot
    assert re.search(r"padding: \d+px 0 %spx" % pad, foot) and "background: var(--bg-elev)" in foot


def test_keyboard_focus_never_lands_under_the_dialog_footer():
    # A checkbox scrolled into view by focus (Tab) must not stop behind the stuck footer
    # (1366×768: a row 58 px under it): the dialog keeps the footer's height clear at its
    # bottom — two rows of buttons, as on a phone where they wrap.
    foot = _rule(".bd-foot")
    top, bottom = map(int, re.search(r"padding: (\d+)px 0 (\d+)px", foot).groups())
    gap = int(re.search(r"gap: (\d+)px", foot).group(1))
    btn = int(re.search(r"height: (\d+)px", _rule(".bd-btn")).group(1))
    m = re.search(r"scroll-padding-bottom: (\d+)px", _rule(".bd"))
    assert m and int(m.group(1)) >= top + 2 * btn + gap + bottom


def test_compact_chips_on_every_card_of_three_columns_or_more():
    # Amendment A1: one row whenever the chips fit — compact on a narrow card. A card of a grid
    # with c columns is under ((c + 1) * 300 + 12) / c px (300 px column minimum, 12 px gap; else
    # c + 1 columns fit), its content box 30 px less (padding 14, border 1): under 374 px for
    # c = 3, 348 for c = 4 (1600 px window: 341), 332 for c = 5 (1920: 331). A chip is text (rem)
    # plus padding, icon and gap (px): at any text scale (50-150 %) the threshold is at least that
    # bound, and it grows with the text.
    # The chips' query is the one container query on .pc-chips (the notification badge has its own,
    # 320 / 285 px, for the top row only).
    queries = re.findall(r"@container pcard ((?:\(max-width: [\d.]+(?:rem|px)\)(?: or )?)+) \{(.*?)\n\s*\}\n", CSS, re.S)
    m = [q for q, body in queries if ".pc-chips" in body]
    assert len(m) == 1, queries
    assert all(".pc-chips" not in body for q, body in queries if q != m[0])
    terms = re.findall(r"\(max-width: ([\d.]+)(rem|px)\)", m[0])

    def threshold(scale):
        return max(float(v) * (16 * scale if unit == "rem" else 1) for v, unit in terms)

    for scale in (0.5, 0.75, 1, 1.25, 1.5):
        assert threshold(scale) >= 374 * max(1, scale), (scale, terms)


# --- one fetch -> one outcome ------------------------------------------------------------------

@pytest.mark.parametrize("status,body,transport,want", [
    (200, {"success": True, "status": "confirmed", "detail": "подтверждено принтером"}, "ok",
     {"status": "confirmed", "detail": "подтверждено принтером"}),
    (200, {"success": True, "detail": "x"}, "ok", {"status": "sent", "detail": "x"}),
    (400, {"detail": "Уже на паузе", "status": "refused"}, "ok", {"status": "refused", "detail": "Уже на паузе"}),
    (400, {"detail": "Слот 3 пуст"}, "ok", {"status": "rejected", "detail": "Слот 3 пуст"}),
    (409, {"detail": "занято"}, "ok", {"status": "rejected", "detail": "занято"}),
    (422, {"detail": [{"msg": "a"}, {"msg": "b"}]}, "ok", {"status": "rejected", "detail": "a; b"}),
    (400, {}, "ok", {"status": "rejected", "detail": "Ошибка команды"}),
    (502, None, "ok", {"status": "sent", "detail": NET_LOST}),
    (500, {"detail": "Internal Server Error"}, "ok", {"status": "sent", "detail": NET_LOST}),
    (400, None, "ok", {"status": "sent", "detail": NET_LOST}),         # a proxy's HTML page
    (0, None, "lost", {"status": "sent", "detail": NET_LOST}),
    (0, None, "offline", {"status": "rejected", "detail": "Нет сети — команда не отправлена"}),
])
def test_http_outcome(status, body, transport, want):
    assert run("httpOutcome(%d, %s, %s)" % (status, json.dumps(body), json.dumps(transport))) == want


def test_detail_text():
    assert run("[detailText('a'), detailText([{msg: 'x'}, {loc: 1}, {msg: 'y'}]), detailText({msg: 'm'}),"
               " detailText({code: 1}), detailText(''), detailText(null), detailText(undefined)]") == \
        ["a", "x; y", "m", '{"code":1}', "", "", ""]
    assert "[object Object]" not in run("detailText([{msg: 'a'}, {msg: 'b'}])")


# --- adaptive chip rows (amendment A1) -----------------------------------------------------

def chip_rows(widths, row_width, gap=5):
    return run("chipRows(%s, %s, %s)" % (json.dumps(widths), json.dumps(row_width), json.dumps(gap)))


def test_chip_rows_examples():
    assert chip_rows([60, 60, 60, 60], 300) is None                     # fits: one flexible row
    assert chip_rows([200], 100) is None and chip_rows([], 100) is None    # n < 2
    assert chip_rows([60, 60], 0) is None
    assert chip_rows([60] * 5, 280) == [3, 2]       # 320 > 280; 4 per row fit -> 2 rows
    assert chip_rows([60] * 6, 280) == [3, 3]
    assert chip_rows([60] * 7, 280) == [4, 3]
    assert chip_rows([60] * 8, 280) == [4, 4]
    assert chip_rows([84] * 7, 270) == [3, 2, 2]     # 3 per row: never 3+3+1
    assert chip_rows([60] * 6, 300) == [3, 3]
    assert chip_rows([60] * 5 + [100], 300) == [2, 2, 2]                  # a wide chip forces more rows
    assert chip_rows([96, 96, 96], 260) == [3]       # the squeeze fallback, never [2, 1]
    assert chip_rows([200, 100], 250) == [1, 1]      # two chips: one per row


def test_chip_rows_with_a_chip_wider_than_half_the_row():
    # Not even two cells of a row hold the widest chip: still no lone chip — rows of two (one of
    # three when n is odd), the widest chip clipped in its cell, never 2+1+1+…
    assert chip_rows([140, 60, 60], 260) == [3]
    assert chip_rows([140, 60, 60, 60], 260) == [2, 2]
    assert chip_rows([200, 50, 50, 50, 50], 260) == [2, 3]          # the wide chip in the wider cells
    # the live card at 150 % text (1024 px): 8 compact chips in a 276 px row, gap 4
    assert chip_rows([107, 94, 94, 94, 107, 115, 143, 180], 276, 4) == [2, 2, 2, 2]


def test_chip_rows_narrow_cells_go_where_the_chips_are_narrow():
    # A row of three cells narrower than the widest chip: placed after the wide (heating) chips
    # instead of first, when that keeps them whole; otherwise larger rows first.
    assert chip_rows([95, 60, 60, 60, 60], 200) == [2, 3]
    assert chip_rows([95, 95, 60, 60, 60, 60, 60], 205) == [2, 3, 2]
    assert chip_rows([60, 60, 60, 60, 95], 200) == [3, 2]
    assert chip_rows([60, 60, 60, 60, 140], 260) == [3, 2]
    assert chip_rows([60] * 5, 150) == [3, 2]                          # every order clips alike


def _cmax(widths, row_width, gap=5):
    return max(1, (row_width + gap) // (max(widths) + gap))


def _cells(sizes, row_width, gap=5):
    """The cell width of every chip, in chip order."""
    return [(row_width - (k - 1) * gap) / k for k in sizes for _ in range(k)]


def _worst_clip(widths, sizes, row_width, gap=5):
    return max(max(0.0, w - c) for w, c in zip(widths, _cells(sizes, row_width, gap)))


def _widest_held(widths, sizes, row_width, gap=5):
    m = max(widths)
    return all(c >= m - 1e-9 for w, c in zip(widths, _cells(sizes, row_width, gap)) if w == m)


def test_chip_rows_never_leave_a_lone_chip():
    rnd = random.Random(20261012)
    cases = []
    for n in range(3, 10):
        for _ in range(300):
            row_width = rnd.randint(180, 420)
            # up to 0.6 of the row: covers rows where not even two cells hold the widest chip
            cases.append(([rnd.randint(40, int(row_width * 0.6)) for _ in range(n)], row_width))
    results = run("%s.map(function (c) { return chipRows(c[0], c[1], 5); })" % json.dumps(cases))
    assert len(results) == len(cases) >= 2000
    wide = 0
    for (widths, row_width), sizes in zip(cases, results):
        n, total = len(widths), sum(widths) + 5 * (len(widths) - 1)
        if sizes is None:
            assert total <= row_width, (widths, row_width)
            continue
        assert total > row_width
        assert sum(sizes) == n and 1 not in sizes, (widths, row_width, sizes)
        assert max(sizes) - min(sizes) <= 1, (widths, row_width, sizes)          # balanced
        cmax = _cmax(widths, row_width)
        wide += cmax == 1
        if -(-n // cmax) <= n // 2:     # equal cells holding the widest chip, no lone chip: possible
            assert max(sizes) <= cmax, (widths, row_width, sizes)
        if max(sizes) <= cmax:          # nothing clipped: larger rows first
            assert sizes == sorted(sizes, reverse=True), (widths, row_width, sizes)
            continue
        orders = set(itertools.permutations(sizes))
        # the order clips the least: no other order of the same rows has a smaller worst clip ...
        best = min(_worst_clip(widths, o, row_width) for o in orders)
        assert _worst_clip(widths, sizes, row_width) <= best + 0.01, (widths, row_width, sizes)
        # ... so the widest chip gets cells that hold it whenever some order allows that
        if any(_widest_held(widths, o, row_width) for o in orders):
            assert _widest_held(widths, sizes, row_width), (widths, row_width, sizes)
    assert wide >= 300                  # the cmax = 1 rows are really exercised


# --- the fleet bar «Вся ферма» and the bulk dialog ------------------------------------

BAMBU_OK = dict(ALL_CAPS)
BAMBU_BLOCKED = dict(NO_CAPS, light=True)
# = printer_commands.caps_for of a Creality (27.10): light, print control, tune, cooldown
CREALITY = dict(NO_CAPS, pause=True, resume=True, stop=True, light=True, tune=True, cooldown=True)
KLIPPER = dict(NO_CAPS, pause=True, resume=True, stop=True, light=True)


def _farm():
    """The mockup's farm: who prints, who is paused, who is offline, MKS, a blocked P2S."""
    def b(label, state, **kw):
        return dict(_p(id="id-" + label, label=label, state=state, caps=dict(BAMBU_OK)), **kw)
    return [
        b("P3", "printing", light_on=True),
        b("H1", "printing", light_on=True),
        b("P1", "paused", light_on=False, stage="paused_user"),
        b("P2", "finished", light_on=True),
        b("P0", "idle", light_on=None),
        b("C1", "offline", online=False, light_on=None),
        b("P4", "printing", caps=dict(BAMBU_BLOCKED), caps_note="прошивка блокирует (нет Developer Mode)"),
        b("H2", "printing", grace_period_active=True),
        _p(id="mks-1", label="Reborn 2", kind="mks", state="offline", online=False, caps=dict(NO_CAPS),
           caps_note="управление не подключено"),
        _p(id="c-1", label="K1", kind="creality", state="idle", caps=dict(CREALITY, light=False)),  # printer_light_disabled
        _p(id="k-1", label="Ghost 6", kind="klipper", state="paused", caps=dict(KLIPPER), light_on=True,
           stage="paused_filament_runout"),
    ]


def test_bulk_skip_reasons_mirror_the_server():
    farm = {p["label"]: p for p in _farm()}
    reasons = run("%s.map(function (p) { return [p.label, bulkSkipReason('pause', p), bulkSkipReason('light_on', p),"
                  " bulkSkipReason('resume', p)]; })" % json.dumps(list(farm.values())))
    got = {label: (pause, light, resume) for label, pause, light, resume in reasons}
    assert got["P3"] == (None, None, "принтер не на паузе")
    assert got["P1"] == ("уже на паузе", None, None)
    assert got["P2"][0] == "печать завершена" and got["P0"][0] == "принтер не печатает"
    assert got["C1"] == ("офлайн", "офлайн", "офлайн")
    assert got["Reborn 2"] == ("офлайн · управление не подключено",) * 3
    assert got["P4"] == ("прошивка блокирует (нет Developer Mode)", None, "прошивка блокирует (нет Developer Mode)")
    assert got["H2"] == ("нет связи — данные устарели",) * 3
    assert got["K1"] == ("принтер не печатает", "нет управляемой подсветки", "принтер не на паузе")
    assert got["Ghost 6"] == ("уже на паузе", None, None)


def test_bulk_split_sorted_with_the_resume_pre_check():
    out = run("(function () { var s = bulkSplit('pause', %s); return {go: s.go.map(function (x) { return [x.p.label,"
              " x.pre, x.hold]; }), skip: s.skip.map(function (x) { return [x.p.label, x.why]; })}; })()"
              % json.dumps(_farm()))
    assert out["go"] == [["H1", True, None], ["P3", True, None]]               # sorted by label; pause: all pre
    assert [x[0] for x in out["skip"]] == ["C1", "Ghost 6", "H2", "K1", "P0", "P1", "P2", "P4", "Reborn 2"]
    out = run("bulkSplit('resume', %s).go.map(function (x) { return [x.p.label, x.pre, x.hold]; })"
              % json.dumps(_farm()))
    assert out == [["Ghost 6", False, "printer"], ["P1", True, None]]


@pytest.mark.parametrize("stage,hold", [
    (None, None), ("", None), ("paused", None), ("paused_user", None), ("PAUSED_USER", None),
    ("paused_filament_runout", "printer"), ("paused_nozzle_clog", "printer"), ("paused_first_layer_error", "printer"),
    ("paused_ams_lost", "printer"), ("paused_front_cover_falling", "printer"),
    ("paused_user_gcode", "gcode"), ("m400_pause", "gcode"),
])
def test_resume_hold(stage, hold):
    assert run("resumeHold(%s)" % json.dumps({"state": "paused", "stage": stage})) == hold


@pytest.mark.parametrize("stage", [None, "", "paused", "paused_user", "paused_filament_runout"])
def test_resume_hold_of_a_pause_on_an_error(stage):
    # a plain resume after a pause-error dialog pauses again (spec §8.7): never pre-checked, whatever the stage
    p = {"state": "paused", "stage": stage, "notifications": [
        {"key": "hms:0700_2500_0002_0001", "code": "0700_2500_0002_0001", "severity": "serious"},
        {"key": "pe:0500_8062", "code": "0500_8062", "severity": "pause"}]}
    assert run("[resumeHold(%s), resumeErrCode(%s)]" % (json.dumps(p), json.dumps(p))) == ["printer", "0500_8062"]
    # a serious HMS alone is not a pause on an error
    p["notifications"] = p["notifications"][:1]
    assert run("resumeErrCode(%s)" % json.dumps(p)) is None
    want = None if stage in (None, "", "paused", "paused_user") else "printer"
    assert run("resumeHold(%s)" % json.dumps(p)) == want


def test_resume_hold_text_of_a_pause_on_an_error():
    from app.services import notification_actions
    # the same words as the fleet reset's skip reason on the server
    assert run("resumeHoldText('printer', 'Пауза', '0500_8062')") == notification_actions.RESET_PAUSE % "0500_8062"
    farm = [p for p in _farm() if p["label"] == "P1"]
    farm[0]["notifications"] = [{"key": "pe:0500_8062", "code": "0500_8062", "severity": "pause"}]
    assert run("bulkSplit('resume', %s).go.map(function (x) { return [x.p.label, x.pre, x.hold]; })"
               % json.dumps(farm)) == [["P1", False, "printer"]]
    # the opener passes the code along (openBulkDialog)
    assert "resumeHoldText(x.hold, stageLabel(p.stage), resumeErrCode(p))" in _decl("openBulkDialog")


def test_resume_hold_text():
    assert run("resumeHoldText('printer', 'Пауза: закончился филамент')") == "пауза принтером: закончился филамент"
    assert run("resumeHoldText('printer', 'Пауза (пользователь)')") == "пауза принтером: (пользователь)"
    assert run("resumeHoldText('printer', null)") == "пауза принтером"
    assert run("resumeHoldText('gcode', 'Пауза (G-code)')") == \
        "пауза из G-code — сначала сделайте то, ради чего она в файле"


def test_plural():
    assert run("[1, 2, 5, 11, 12, 21, 22, 25, 101, 111].map(function (n) { return plural(n, 'принтер',"
               " 'принтера', 'принтеров'); })") == ["принтер", "принтера", "принтеров", "принтеров", "принтеров",
                                                    "принтер", "принтера", "принтеров", "принтер", "принтеров"]


def test_fleet_light_stats():
    # 8 Bambu + Ghost 6 have a light; lit: P3, H1, P2, P4, H2 (grace: still online) and Ghost 6;
    # C1 is offline; MKS and K1 have no light cap; light_on is read directly
    assert run("fleetLightStats(%s)" % json.dumps(_farm())) == {"total": 9, "lit": 6, "offline": 1}


def test_fleet_light_stats_and_bulk_split_leave_out_the_printers_in_maintenance():
    # spec_maintenance F2 / F3: P3 (lit) and C1 (offline) in maintenance — out of «горит X из Y» and
    # of every bulk action, with the server's reason, said before «офлайн»
    farm = _farm()
    for p in farm:
        if p["label"] in ("P3", "C1"):
            p["maintenance"] = {"since": 1000.0, "note": None, "by": "admin"}
    assert run("fleetLightStats(%s)" % json.dumps(farm)) == {"total": 7, "lit": 5, "offline": 0}
    for action in ("pause", "light_on", "resume"):
        out = run("bulkSplit(%s, %s)" % (json.dumps(action), json.dumps(farm)))
        assert "P3" not in [x["p"]["label"] for x in out["go"]]
        why = {x["p"]["label"]: x["why"] for x in out["skip"]}
        assert why["P3"] == why["C1"] == "на обслуживании"
    assert run("[inMaint(%s), inMaint(%s), inMaint(null)]" % (json.dumps(farm[0]), json.dumps(farm[1]))) == [
        True, False, False]


# --- maintenance mode: the plaque, the ⋯ section and form, the «Сводка» numbers --------------

NOW_JS = "new Date(2026, 9, 18, 20, 5).getTime()"     # local time: the tests run in any time zone


def _ts(args):
    """Unix seconds of a local-time JS Date(args) — a JS expression."""
    return "(new Date(%s).getTime() / 1000)" % args


def test_the_model_is_always_in_the_head():
    # IA §5 (plan step 5): «P3 · P2S» — the model next to the name on every card (it showed only on a
    # card without an AMS, in the row under the name); not when it only repeats the name
    cases = [({"label": "P3", "device_type": "P2S"}, "P2S"), ({"label": "C1", "device_type": "x1c"}, "X1C"),
             ({"label": "K1", "device_type": "k1c"}, "K1C"), ({"label": "E5", "device_type": "ender5max"}, "Ender-5 Max"),
             ({"label": "K2", "device_type": "k1max"}, "K1 Max"), ({"label": "Ghost 6", "device_type": "ghost6"}, ""),
             ({"label": "Varan", "device_type": "Varan"}, ""), ({"label": "Reborn 2", "device_type": "reborn2"}, ""),
             ({"label": "X", "device_type": None}, ""), ({"label": "X", "device_type": "  "}, "")]
    assert run("%s.map(cardModelText)" % json.dumps([c for c, _w in cases])) == [w for _c, w in cases]
    card = _decl("renderPrinterCard")
    assert "var model = cardModelText(p);" in card
    assert ("<span class=\"pc-nm\">${safeText(p.label, \"Без имени\")}</span>${model ? '<span class=\"pc-model\">' + safeText(model, \"\") + '</span>' : \"\"}"
            in card)
    css = _rule(".printer-name .pc-model")
    assert "color: var(--text-muted)" in css and "font-size: 0.66rem" in css and "margin-left: 6px" in css
    # whole or not at all: the name box is one line tall and wraps, so a model short of room drops to
    # the clipped second line instead of showing a «P…» stub; it never shrinks
    name = _rule(".printer-name")
    for decl in ("display: flex", "flex-wrap: wrap", "height: 1.2em", "overflow: hidden", "min-width: 0"):
        assert decl in name, decl
    assert "flex: none" in css and "order: 1" in css
    assert "text-overflow: ellipsis" in _rule(".printer-name .pc-nm")
    assert 'html[data-theme="light"] .printer-name .pc-model { color: var(--text-dim); }' in CSS


def test_the_head_never_runs_past_the_card():
    # The ⋯ is on every card's head now (IA §5). The head is one line at every width (01.11: a wrapped
    # head — P1S2 at 5 columns, 15 px short in another browser's font, and every card at ≤ 285 px — put
    # that card's progress and chips below its neighbours'); measured live at 320-1873 px: head 28 px and
    # the progress at the same offset on every card. Short of room the name box gives way (the model
    # first, then «…»), never the pills; narrow cards still compact the pills (Wi-Fi as its icon, hidden
    # next to ⚠ / ⓘ, a narrower ⋯)
    assert re.search(r"(?m)^    \.pc-toprow \{ flex-wrap: nowrap; \}$", CSS)
    assert not re.search(r"\.pc-toprow[^{,]*\{[^}]*flex-wrap: wrap", CSS)
    assert "flex: 0 1 auto" in _rule(".pc-toprow > .printer-name")
    assert "margin-left: auto" in _rule(".pc-toprow > .pc-toprow-right")
    q = re.search(r"@container pcard \(max-width: 320px\) \{(.*?)\n    \}", CSS, re.S).group(1)
    for decl in (".pc-toprow-right .pc-wifi { font-size: 0; gap: 0; }", ".pc-toprow-right:has(.nf-badge) .pc-wifi { display: none; }",
                 ".pc-ctl.pc-ctl-head { width: 24px; }"):
        assert decl in q, decl
    assert "max-width" not in q                                       # the name box shrinks, no cap
    assert ".pc-chips" not in q                                       # the chips keep their own query
    # 4–5 columns and phones: Wi-Fi is its icon on every card, not «-54» on some
    q4 = re.search(r"@container pcard \(max-width: 400px\) \{(.*?)\n    \}", CSS, re.S).group(1)
    assert ".pc-toprow-right .pc-wifi { font-size: 0; gap: 0; }" in q4


def test_maint_since_text():
    got = run("[%s]" % ", ".join("maintSinceText(%s, %s)" % (_ts(d), NOW_JS) for d in (
        "2026, 9, 18, 14, 32", "2026, 9, 18, 0, 0", "2026, 9, 17, 23, 59", "2026, 0, 5, 9, 0", "2025, 9, 18, 14, 32")))
    assert got == ["с 14:32", "с 00:00", "с 17.10", "с 05.01", "с 18.10.2025"]
    assert run("[maintSinceText(null, 0), maintSinceText('x', 0)]") == ["", ""]


def test_maint_plaque():
    # IA §5 (plan step 5, deliberately changed): a dim line «обслуживание · с …» without «снять» — the
    # card's ⋯ takes it off (one way to do it); an admin's tooltip says so
    p = "{id: 'k-1', label: 'Varan', maintenance: {since: %s, note: '<b>меняю</b> хотенд', by: 'admin'}}" % _ts(
        "2026, 9, 18, 14, 32")
    html = run("maintPlaqueHtml(%s, true, %s)" % (p, NOW_JS))
    assert html == ('<div class="pc-maint"><svg class="pc-ic" aria-hidden="true"><use href="#i-tool"></use></svg>'
                    '<span class="t" data-tip="На обслуживании с 18.10.2026 14:32 · поставил admin · '
                    '&lt;b&gt;меняю&lt;/b&gt; хотенд · снять — в меню ⋯">обслуживание · с 14:32 · &lt;b&gt;меняю&lt;/b&gt; хотенд'
                    '</span></div>')
    viewer = run("maintPlaqueHtml(%s, false, %s)" % (p, NOW_JS))
    assert "снять" not in viewer and "button" not in viewer and "&lt;b&gt;меняю" in viewer
    # no note, nobody known; an earlier year in full
    bare = run("maintPlaqueHtml({id: 'x\"', maintenance: {since: %s, note: null, by: null}}, false, %s)"
               % (_ts("2025, 2, 1, 8, 0"), NOW_JS))
    assert 'data-tip="На обслуживании с 01.03.2025 08:00">обслуживание · с 01.03.2025</span></div>' in bare
    assert "data-maint-off" not in JS                                          # no second way on the card
    assert run("[maintPlaqueHtml({id: 'a', maintenance: null}, true, 0), maintPlaqueHtml(null, true, 0)]") == ["", ""]


def test_maint_pop_form():
    from app.api import admin
    html = run("maintPopHtml(%s)" % json.dumps({"id": "k-1", "maintenance": None}))
    assert html == ('<div class="pc-pop-h">На обслуживание</div><div class="pc-pop-row"><input type="text" class="pc-pop-in"'
                    ' maxlength="%d" placeholder="что делаем (необязательно)" aria-label="Заметка обслуживания" value="">'
                    '<button type="button" class="pc-pop-ok" data-maint-save>Поставить</button></div>' % admin.MAINT_NOTE_MAX)
    html = run("maintPopHtml(%s)" % json.dumps({"id": "k-1", "maintenance": {"since": 1.0, "note": '"x" <b>', "by": "a"}}))
    assert html.startswith('<div class="pc-pop-h">Заметка обслуживания</div>')
    assert 'value="&quot;x&quot; &lt;b&gt;"' in html and ">Сохранить</button>" in html


def menu_opts(p, opts, ui=None, now=1000):
    return run("ctlMenuHtml(%s, %s, %d, %s)" % (json.dumps(p), json.dumps(ui), now, json.dumps(opts)))


def test_menu_maintenance_section_for_admins_only():
    idle = _p(state="idle")
    html = menu_opts(idle, {"admin": True, "offline": False})
    assert [m for m, _d in _ITEM.findall(html)] == ["maint-open", "jobs", "chrono", "params"]
    assert html.index('data-menu="maint-open"') < html.index("pm-sep") < html.index('data-menu="jobs"')
    assert ('<span class="lbl">На обслуживание…<span class="sub">убрать из сводки, уведомлений и групповых команд</span>'
            '</span></button>') in html
    viewer = menu_opts(idle, {"admin": False, "offline": False})               # a viewer: the links only (IA §5)
    assert [m for m, _d in _ITEM.findall(viewer)] == ["jobs", "chrono"] and "maint" not in viewer and "Параметры" not in viewer
    html = menu_opts(_p(state="idle", maintenance={"since": 1.0, "note": None, "by": "a"}), {"admin": True})
    assert [m for m, _d in _ITEM.findall(html)][:2] == ["maint-open", "maint-off"]
    assert "Заметка обслуживания…" in html and "Снять с обслуживания" in html and "На обслуживание…" not in html


def test_menu_of_an_offline_card_has_no_printer_items():
    off = _p(state="offline", online=False, filament_change={"action": "load", "step": "heat"})
    html = menu_opts(off, {"admin": True, "offline": True})
    assert [m for m, _d in _ITEM.findall(html)] == ["maint-open", "jobs", "chrono", "params"]
    assert "pop-why" not in html and "Скорость" not in html and 'aria-disabled' not in html
    assert [m for m, _d in _ITEM.findall(menu_opts(off, {"admin": False, "offline": True}))] == ["jobs", "chrono"]


def test_one_more_in_the_head_of_every_card():
    # IA §5 (plan step 5, deliberately changed): the card's one ⋯ is in its head, on every card and
    # for both roles (a viewer's holds the links); the row never has one. It sends no printer command,
    # so nothing greys it out — not the grace period either.
    off = _p(state="offline", online=False)
    cases = [off, _p(), _p(grace_period_active=True), _p(caps=dict(BAMBU_BLOCKED), caps_note="прошивка блокирует"),
             _p(caps=dict(NO_CAPS, pause=True, tune=True)), _p(kind="creality", caps=dict(CREALITY), ams=None),
             _p(kind="klipper", caps=dict(KLIPPER), ams=None), _p(kind="mks", caps=dict(NO_CAPS), ams=None)]
    tips = {True: "Обслуживание и ссылки: печати, хронология, параметры", False: "Печати и хронология этого принтера"}
    for p in cases:
        for admin in (True, False):
            opts = {"admin": admin, "offline": p["state"] == "offline"}
            head = buttons(run("headMoreHtml(%s, null, %s)" % (json.dumps(p), json.dumps(opts))))
            assert head == [("more", "more", ["pc-ctl-head"], tips[admin], False)], (p, admin)
            assert 'data-cmd="more"' not in row(p, None, dict(opts, now=0)), p
    b = buttons(run("headMoreHtml(%s, {pop: 'menu'}, {admin: false, offline: true})" % json.dumps(off)))
    assert b == [("more", "more", ["pc-ctl-head", "open"], tips[False], False)]
    # the DOM layer: the head's ⋯ opens the menu before the busy / greyed-out checks of the row
    ctl = JS[JS.index("(function initPrinterControls() {"):JS.index("// --- Printer notifications")]
    more = ctl.index('if (cmd === "more") { e.stopPropagation(); togglePop(btn, pid2, "menu"); return; }')
    assert more < ctl.index('btn.getAttribute("aria-disabled") === "true"') and more < ctl.index("if (ui.busy) return;")
    assert 'case "ams"' not in ctl and 'case "more"' not in ctl and "[data-cmd=ams]" not in JS


def test_the_summary_tab_is_gone():
    # plan step 8 (deliberately changed): «Сводка» is gone — its KPI tiles are the farm's chips (step 6),
    # its finishes the «Финиши» row (step 7), its «Загрузка» and «Статистика принтеров» are «История →
    # Статистика» (histUtil / histStatsRows, tests/test_web_hist_ui.py), its event log is not moved
    # (the prints, their pauses and reasons are in «Печати», the states in «Хронология»). The live
    # «printing now» share fleetSummary counted went with its only place.
    for gone in ("fleetSummary", "renderSummary", "metaUtil", "loadStatsSummary", "statsList", "loadEventLog",
                 "eventLogOffset", "initEventLogFilters", "EVENT_STATE_LABELS", "STATE_LABELS_RU", "eventReasonText",
                 "lastEventLogLoad", "setupInfiniteScroll", "metaNextFinish", "metaFarmIdle", "metaNozzle", "metaBed"):
        assert gone not in JS, gone
    for gone in ('data-panel="dashboard"', 'data-view="dashboard"', 'id="eventLog"', 'id="statsList"', "Лог событий",
                 "Статистика принтеров", "Ближайший финиш", "Освободятся", 'class="dash-meta-row"'):
        assert gone not in HTML, gone
    # «Наверх» stays: it was wired next to the event log's infinite scroll
    assert "initScrollTop();" in JS and 'id="scrollTopBtn"' in HTML


_BULK_BTN = re.compile(r'<button type="button" class="fb-btn([^"]*)" data-bulk="([^"]*)"( aria-disabled="true")?'
                       r'(?: data-tip="([^"]*)")?>')
_LIGHT_BTN = re.compile(r'<button type="button"( class="is-wait")? data-bulk="(light_o(?:n|ff))" data-tip="([^"]*)">')


def test_fleet_bulk_bar():
    # IA §5 (plan step 6, deliberately changed): the farm's commands are their own row under the
    # chips — «Свет: вкл · выкл» (how many are lit in its tooltip, no meta text), «Пауза всех…»
    # with its dialog, «Продолжить N» only while N > 0
    html = run("fleetBulkHtml(%s, {})" % json.dumps(_farm()))
    assert html.startswith('<div class="fleet-bulk" role="toolbar" aria-label="Вся ферма"><span class="fb-light-lbl" '
                           'data-tip="горит 6 из 9 · 1 офлайн">')
    assert "fb-title" not in html and "fb-lbl" not in html and "fb-meta" not in html and "fb-group" not in html
    assert '<span class="farm-seg fb-light">' in html
    assert [(b[1], b[2]) for b in _LIGHT_BTN.findall(html)] == [("light_on", "Включить на всех онлайн-принтерах со светом · горит 6 из 9 · 1 офлайн"),
                                                                ("light_off", "Выключить на всех онлайн-принтерах со светом · горит 6 из 9 · 1 офлайн")]
    assert re.search(r'data-bulk="light_on"[^>]*>вкл</button>', html) and re.search(r'data-bulk="light_off"[^>]*>выкл</button>', html)
    btns = {b[1]: (b[0], b[2], b[3]) for b in _BULK_BTN.findall(html)}
    assert list(btns) == ["pause", "resume"]
    assert btns["pause"] == (" pause", "", "Пауза 2 печатающим — сначала выбрать, каким")
    assert re.search(r'data-bulk="pause"[^>]*>.*?Пауза всех…</button>', html)
    assert re.search(r'data-bulk="resume"[^>]*>.*?Продолжить <span class="fb-n">2</span>', html)
    nobody = [p for p in _farm() if p["state"] not in ("printing", "paused")]
    html = run("fleetBulkHtml(%s, {})" % json.dumps(nobody))
    btns = {b[1]: (b[2], b[3]) for b in _BULK_BTN.findall(html)}
    assert btns == {"pause": (' aria-disabled="true"', "Никто не печатает")}     # no «Продолжить 0» at all
    one = [p for p in _farm() if p["label"] in ("P3", "P1")]
    assert "Пауза 1 печатающему" in run("fleetBulkHtml(%s, {})" % json.dumps(one))
    # no printer with a light or print control: no bar at all
    assert run("fleetBulkHtml(%s, {})" % json.dumps([p for p in _farm() if p["kind"] == "mks"])) == ""
    # light only (print control blocked everywhere): no print buttons
    html = run("fleetBulkHtml(%s, {})" % json.dumps([_p(caps=dict(BAMBU_BLOCKED))]))
    assert 'data-bulk="light_on"' in html and 'data-bulk="pause"' not in html and 'data-bulk="resume"' not in html
    # print only: no light group
    html = run("fleetBulkHtml(%s, {})" % json.dumps([_p(caps=dict(ALL_CAPS, light=False))]))
    assert "fb-light" not in html and 'data-bulk="pause"' in html


def test_fleet_bulk_bar_has_no_reset_group_any_more():
    # IA §5 (plan step 4, deliberately changed): «Сбросить ошибки» moved from the bar to the foot of the
    # bell's list (test_web_nf_ui.test_reset_errors_is_at_the_foot_of_the_bell_list); the bar keeps its
    # light, pause and resume buttons whatever it is given
    for bulk in ({}, {"reset": 2}, {"reset": 0}):
        html = run("fleetBulkHtml(%s, %s)" % (json.dumps(_farm()), json.dumps(bulk)))
        assert "reset" not in html and "Сбросить ошибки" not in html and html.endswith("</div>")
        assert [b[1] for b in _LIGHT_BTN.findall(html)] + [b[1] for b in _BULK_BTN.findall(html)] == \
            ["light_on", "light_off", "pause", "resume"]
    click = _decl_fleet_click()
    assert "reset_errors" not in click and "openResetDialog" not in click
    assert "nfResetSplit" not in JS[JS.index("function renderFleetBulk"):JS.index("function flashBulkMsg")]
    assert "fb-reset" not in CSS and ".fb-btn.reset" in CSS          # the button's look moved with it


def _decl_fleet_click() -> str:
    start = JS.index('fleetBulkEl.addEventListener("click"')
    return JS[start:JS.index("});", start)]


def test_fleet_bulk_bar_busy_message_and_failures():
    bulk = {"busy": "light_on", "msg": {"k": "bad", "t": "Свет включён: ✓ 3 · ✕ 1"},
            "failed": {"action": "light_on", "rows": [
                {"id": "a", "label": "P<1>", "status": "rejected", "detail": "🔒 <b>блок</b>"},
                {"id": "b", "label": "P2", "status": "sent", "detail": "отправлено (без подтверждения)"}]}}
    html = run("fleetBulkHtml(%s, %s)" % (json.dumps(_farm()), json.dumps(bulk)))
    # the busy light button spins inside its segment; the outcome is the row's own line (one, not two any more)
    assert re.search(r'<button type="button" class="is-wait" data-bulk="light_on"[^>]*><span class="spin-sm"></span>вкл', html)
    assert html.count('<span class="fb-meta bad" role="status">Свет включён: ✓ 3 · ✕ 1</span>') == 1
    assert html.count("fb-meta") == 1
    ok = run("fleetBulkHtml(%s, %s)" % (json.dumps(_farm()), json.dumps({"msg": {"k": "ok", "t": "<x>"}})))
    assert '<span class="fb-meta ok" role="status">&lt;x&gt;</span>' in ok
    assert '<div class="fb-result" role="status">' in html and "Свет: не подтвердили 2" in html
    assert "<b>P&lt;1&gt;</b> ✕ 🔒 &lt;b&gt;блок&lt;/b&gt;" in html and "<b>P2</b> ? без ответа" in html
    assert 'data-bulk-retry' in html and 'data-bulk-dismiss aria-label="Закрыть"' in html


def _dlg(action="pause", **kw):
    d = {"action": action, "step": 1, "busy": False, "rows": [],
         "go": [{"id": "h1", "label": "H<1>", "model": "H2S", "pct": "78%", "job": "Деталь_<01>", "eta": "ещё 1ч 47м",
                 "state": "printing", "stage": None, "pre": True, "holdText": None},
                {"id": "p3", "label": "P3", "model": "P2S", "pct": "56%", "job": "Деталь_06", "eta": "ещё 3ч 29м",
                 "state": "printing", "stage": None, "pre": True, "holdText": None}],
         "skip": [{"id": "m1", "label": "Reborn 2", "model": "MKS", "why": "офлайн · управление не подключено"}],
         "sel": {"h1": True, "p3": True}}
    d.update(kw)
    return d


def dialog(d):
    return run("bulkDialogHtml(%s)" % json.dumps(d))


def test_bulk_dialog_step1_pause():
    html = dialog(_dlg())
    assert html.startswith('<div class="bd" role="dialog" aria-modal="true" aria-labelledby="bdTitle">')
    assert '<h3 id="bdTitle">Поставить на паузу 2 принтера?</h3>' in html
    assert "Печать продолжится с того же места — кнопкой ▶ на карточке или «Продолжить» сверху." in html
    assert "<span>Печатают · 2</span>" in html and 'data-bd="all">Снять все</button>' in html
    assert len(re.findall(r'<input type="checkbox" data-bd="toggle" data-id="(\w+)" checked>', html)) == 2
    assert "H&lt;1&gt;" in html and "Деталь_&lt;01&gt;" in html and "<1>" not in html
    assert "<span>Не попадут · 1</span>" in html and "<b>Reborn 2</b><span>MKS — офлайн · управление не подключено</span>" in html
    assert re.search(r'<button type="button" class="bd-btn warn" data-bd="go">.*?Пауза выбранных \(2\)</button>', html)
    one = dialog(_dlg(sel={"p3": True}))
    assert '<h3 id="bdTitle">Поставить на паузу 1 принтер?</h3>' in one and 'data-bd="all">Выбрать все</button>' in one
    assert '<label class="bd-row unchecked"><input type="checkbox" data-bd="toggle" data-id="h1">' in one
    none = dialog(_dlg(sel={}))
    assert re.search(r'class="bd-btn warn" data-bd="go" aria-disabled="true">.*?Пауза выбранных \(0\)', none)


def test_bulk_dialog_step1_resume_with_held_rows():
    go = [{"id": "p1", "label": "P1", "model": "P2S", "pct": "40%", "job": "Деталь_02", "eta": "ещё 2ч",
           "state": "paused", "stage": "paused_user", "pre": True, "holdText": None},
          {"id": "g6", "label": "Ghost 6", "model": "KLIPPER", "pct": "12%", "job": "Деталь_03", "eta": "",
           "state": "paused", "stage": "paused_filament_runout", "pre": False,
           "holdText": "пауза принтером: закончился <филамент>"}]
    html = dialog(_dlg("resume", go=go, skip=[], sel={"p1": True}))
    assert '<h3 id="bdTitle">Снять с паузы 1 принтер?</h3>' in html
    assert ("Каждый принтер продолжит печать с места остановки. Принтеры, поставленные на паузу самим принтером,"
            " не отмечены — сначала проверьте их.") in html
    assert "<span>На паузе · 2</span>" in html and "Не попадут" not in html
    assert '<label class="bd-row unchecked hold"><input type="checkbox" data-bd="toggle" data-id="g6">' in html
    assert ('<span class="bd-hold"><svg class="pc-ic" aria-hidden="true"><use href="#i-lock"></use></svg>'
            'пауза принтером: закончился &lt;филамент&gt;</span>') in html
    assert re.search(r'<button type="button" class="bd-btn go" data-bd="go">.*?Продолжить выбранные \(1\)</button>', html)
    plain = dialog(_dlg("resume", go=go[:1], skip=[], sel={"p1": True}))
    assert "не отмечены" not in plain


def test_bulk_dialog_step2():
    rows = [{"id": "h1", "label": "H1", "model": "H2S", "job": "Деталь_01", "status": "confirmed", "detail": "подтверждено принтером"},
            {"id": "p3", "label": "P3", "model": "P2S", "job": "Деталь_06", "status": "sent", "detail": "отправлено (без подтверждения)"},
            {"id": "p4", "label": "P4", "model": "P2S", "job": "Деталь_10", "status": "rejected", "detail": "🔒 прошивка <b>"},
            {"id": "p5", "label": "P5", "model": "P2S", "job": "Деталь_12", "status": "skipped", "detail": "уже на паузе"},
            {"id": "p6", "label": "P6", "model": "P2S", "job": "Деталь_13", "status": "sent", "detail": NET_LOST}]
    html = dialog(_dlg(step=2, rows=rows))
    assert '<h3 id="bdTitle">Пауза: подтверждено 1 из 5</h3>' in html
    assert "Каждый принтер отвечает сам, до 8 с. Карточки обновятся через несколько секунд." in html
    assert re.findall(r'<span class="bd-st( \w+)?">([^<]*)</span>', html.split('<div class="bd-sum">')[1].split("</div>")[0]) == [
        (" ok", "✓ 1"), (" unk", "? 2"), (" bad", "✕ 1"), ("", "— 1")]
    for chip in ('<span class="bd-st ok">✓ подтверждено</span>', '<span class="bd-st unk">? отправлено, ответа нет</span>',
                 '<span class="bd-st bad">✕ 🔒 прошивка &lt;b&gt;</span>', '<span class="bd-st">— уже на паузе</span>',
                 '<span class="bd-st unk">? связь прервалась — проверьте карточку</span>'):
        assert chip in html, chip
    assert re.findall(r'data-bd="retry1" data-id="(\w+)"', html) == ["p3", "p4", "p6"]
    assert 'data-bd="retry">Повторить для 3</button>' in html and 'class="bd-btn warn" data-bd="close">Готово</button>' in html
    busy = dialog(_dlg(action="resume", step=2, busy=True, rows=[dict(rows[0], status="wait", detail="")]))
    assert '<h3 id="bdTitle">Продолжение: ждём ответа принтеров…</h3>' in busy
    assert '<span class="bd-st"><span class="spin-sm"></span>ждём принтер…</span>' in busy
    assert 'data-bd="retry"' not in busy and 'data-bd="close" aria-disabled="true">Готово' in busy


# --- «go» in the bulk dialog: each chosen row against the latest data -------------------------

CHANGED = "состояние изменилось, не отправлено"


def _snap(p):
    """The dialog's snapshot row of printer p (as openBulkDialog builds it)."""
    return {"id": p["id"], "label": p["label"], "model": "P2S", "job": "Деталь_01", "state": p["state"],
            "stage": p.get("stage") or None, "pre": True, "holdText": None}


def revalidate(action, chosen, printers):
    return run("bulkRevalidate(%s, %s, %s)" % (json.dumps(action), json.dumps(chosen), json.dumps(printers)))


def test_bulk_pause_still_sends_a_printer_whose_stage_moved_on():
    # A multi-colour print goes printing -> changing_filament while the dialog is open: the state
    # stays "printing", and an urgent «Пауза всех» must still reach that printer.
    then = _p(id="b1", label="P1", state="printing", stage="printing")
    out = revalidate("pause", [_snap(then)], [dict(then, stage="changing_filament")])
    assert out == {"ids": ["b1"], "rows": [{"id": "b1", "label": "P1", "model": "P2S", "job": "Деталь_01",
                                            "status": "wait", "detail": ""}]}


def test_bulk_revalidate_leaves_out_what_changed():
    printing = _p(id="a", label="A", state="printing")
    chosen = [_snap(printing), _snap(dict(printing, id="gone", label="G")), _snap(dict(printing, id="c", label="C"))]
    latest = [dict(printing, stage="auto_bed_leveling"), dict(printing, id="c", label="C", state="paused")]
    out = revalidate("pause", chosen, latest)
    assert out["ids"] == ["a"]
    assert [(r["id"], r["status"], r["detail"]) for r in out["rows"]] == [
        ("a", "wait", ""), ("gone", "skipped", CHANGED), ("c", "skipped", CHANGED)]
    # resume: the stage decides whether a pause is held, so a changed stage is not sent
    paused = _p(id="b", label="B", state="paused", stage="paused_user")
    chosen = [_snap(paused), _snap(dict(paused, id="d", label="D"))]
    out = revalidate("resume", chosen, [dict(paused, stage="paused_filament_runout"), dict(paused, id="d", label="D")])
    assert out["ids"] == ["d"] and [r["status"] for r in out["rows"]] == ["skipped", "wait"]
    # left out by now (stale data) although the state is the same
    out = revalidate("pause", [_snap(printing)], [dict(printing, grace_period_active=True)])
    assert out["ids"] == [] and out["rows"][0]["status"] == "skipped"
    # a numeric id matches its string form (findPrinter compares them as strings)
    assert revalidate("pause", [dict(_snap(printing), id=7)], [dict(printing, id="7")])["ids"] == [7]


# --- what an outcome changes: the card, the bar, the strip -------------------------------------

def after(action, slot, out, now=1000):
    return run("cardAfterResult(%s, %s, %s, %d)" % (json.dumps(action), json.dumps(slot), json.dumps(out), now))


def test_card_after_result_patches_a_confirmed_light_only():
    # the card shows the new light at once, until the next poll brings the printer's own report
    assert after("light_on", "light", {"status": "confirmed", "detail": "подтверждено принтером"}) == {
        "res": {"slot": "light", "k": "ok"}, "line": {"k": "ok", "t": "Свет включён — подтверждено"},
        "patch": {"light_on": True}, "ejectUntil": None}
    assert after("light_off", "light", {"status": "confirmed"})["patch"] == {"light_on": False}
    for st in ("sent", "rejected", "refused"):           # unconfirmed: the light may not have switched
        assert after("light_on", "light", {"status": st, "detail": "x"})["patch"] is None, st
    assert after("pause", "pp", {"status": "confirmed"})["patch"] is None


def test_card_after_result_eject_guard_and_marks():
    for st, until in (("confirmed", 21000), ("sent", 21000), ("rejected", None), ("refused", None)):
        assert after("eject", "eject", {"status": st, "detail": "x"})["ejectUntil"] == until, st
    assert after("cooldown", "cool", {"status": "confirmed"})["ejectUntil"] is None
    out = after("speed", None, {"status": "rejected", "detail": "Ошибка"})      # a menu item: no button mark
    assert out["res"] is None and out["line"] == {"k": "bad", "t": "Скорость — Ошибка"}
    assert after("stop", "stop", {"status": "sent", "detail": NET_LOST})["res"] == {"slot": "stop", "k": "unk"}
    assert run("EJECT_GUARD_MS") == printer_commands.EJECT_GUARD_SECONDS * 1000      # the server's guard


BULK_ROWS = [{"id": "a", "label": "A", "status": "confirmed", "detail": "подтверждено принтером"},
             {"id": "b", "label": "B", "status": "sent", "detail": "отправлено (без подтверждения)"},
             {"id": "c", "label": "C", "status": "rejected", "detail": "publish failed"},
             {"id": "d", "label": "D", "status": "skipped", "detail": "офлайн"}]


def bar_after(action, rows, prev):
    return run("bulkBarAfter(%s, %s, %s)" % (json.dumps(action), json.dumps(rows), json.dumps(prev)))


def test_bulk_bar_after_a_light_run():
    out = bar_after("light_off", BULK_ROWS, None)
    assert out["patches"] == [{"id": "a", "fields": {"light_on": False}}]          # confirmed rows only
    assert out["failed"] == {"action": "light_off", "rows": BULK_ROWS[1:3]}         # sent + rejected
    assert out["msg"] == {"k": "bad", "t": "Свет выключен: ✓ 1 · ? 1 · ✕ 1"}
    ok, prev = [dict(BULK_ROWS[0], id="x")], {"action": "light_off", "rows": BULK_ROWS[2:3]}
    # a clean run of the same action clears the strip, a run of another action keeps it
    assert bar_after("light_off", ok, prev) == {"patches": [{"id": "x", "fields": {"light_on": False}}],
                                                 "failed": None, "msg": {"k": "ok", "t": "Свет выключен: ✓ 1"}}
    assert bar_after("light_on", ok, prev)["failed"] == prev
    assert bar_after("light_on", ok, prev)["patches"] == [{"id": "x", "fields": {"light_on": True}}]


def test_bulk_bar_after_a_pause_or_resume_retry():
    out = bar_after("pause", BULK_ROWS, None)
    assert out["patches"] == [] and out["msg"]["t"] == "Пауза: ✓ 1 · ? 1 · ✕ 1"
    assert out["failed"] == {"action": "pause", "rows": BULK_ROWS[1:3]}
    assert bar_after("resume", [], None) == {"patches": [], "failed": None, "msg": {"k": "ok", "t": "Продолжение: ✓ 0"}}


def test_closed_bulk_dialog_leaves_its_failures_in_the_bar():
    prev = {"action": "light_on", "rows": [{"id": "z", "label": "Z", "status": "rejected", "detail": "x"}]}
    dlg = {"action": "pause", "rows": BULK_ROWS}
    assert run("bulkDialogFailed(%s, %s)" % (json.dumps(dlg), json.dumps(prev))) == \
        {"action": "pause", "rows": BULK_ROWS[1:3]}
    clean = {"action": "pause", "rows": [BULK_ROWS[0], BULK_ROWS[3]]}
    assert run("bulkDialogFailed(%s, %s)" % (json.dumps(clean), json.dumps(prev))) == prev
    assert run("bulkDialogFailed(%s, null)" % json.dumps(clean)) is None


def test_merge_bulk_results_by_id():
    rows = [{"id": "a", "status": "wait", "detail": ""}, {"id": 7, "status": "wait", "detail": ""},
            {"id": "c", "status": "skipped", "detail": CHANGED}]
    out = run("(function (d) { mergeBulkResults(d, [{id: 'a', status: 'confirmed', detail: 'ok'},"
              " {id: '7', status: 'rejected', detail: 'no'}]); return d.rows; })(%s)" % json.dumps({"rows": rows}))
    assert out == [{"id": "a", "status": "confirmed", "detail": "ok"}, {"id": 7, "status": "rejected", "detail": "no"},
                   {"id": "c", "status": "skipped", "detail": CHANGED}]


# --- bulk skip reasons: the same rule as the server, on the same /api/printers rows ---------------

def test_bulk_skip_reasons_match_the_server_rule(monkeypatch):
    from app.domain.models import PrinterKind, PrinterState, PrinterStatus
    from app.services import fleet_commands, settings_service
    from app.services.normalizer import normalize_ws_dict

    monkeypatch.setattr(settings_service, "get", lambda key, **k: ["c-1"] if key == "printer_light_disabled" else {})
    monkeypatch.setattr(printer_commands, "_capability", {
        pid: {"print_cmds": pid != "b-blocked", "via": "fun", "ts": 0}
        for pid in ("b-ok", "b-paused", "b-held", "b-fin", "b-err", "b-unknown", "b-grace", "b-off", "b-blocked")})

    def bambu(pid, state=PrinterState.PRINTING, online=True, grace=False, stage=None):
        return PrinterStatus(id=pid, label=pid, kind=PrinterKind.BAMBU, online=online, stage=stage,
                             state=state if online else PrinterState.OFFLINE, grace_period_active=grace)

    def klipper(pid, webhooks="ready", pin="caselight", state="printing"):
        return normalize_ws_dict(pid, pid, PrinterKind.KLIPPER,
                                 {"state": state, "debug": {"webhooks_state": webhooks}, "light_pin": pin})

    statuses = [
        bambu("b-ok"), bambu("b-paused", PrinterState.PAUSED, stage="paused_user"),
        bambu("b-held", PrinterState.PAUSED, stage="paused_filament_runout"), bambu("b-fin", PrinterState.FINISHED),
        bambu("b-err", PrinterState.ERROR), bambu("b-unknown", PrinterState.UNKNOWN), bambu("b-new", PrinterState.IDLE),
        bambu("b-grace", grace=True), bambu("b-off", online=False), bambu("b-blocked"),
        PrinterStatus(id="c-1", label="c", kind=PrinterKind.CREALITY, online=True, state=PrinterState.PRINTING),
        PrinterStatus(id="c-lit", label="c", kind=PrinterKind.CREALITY, online=True, state=PrinterState.PAUSED),
        klipper("k-ok"), klipper("k-nopin", pin=""), klipper("k-down", webhooks="shutdown"),
        klipper("k-idle", state="standby"),
        PrinterStatus(id="m-1", label="m", kind=PrinterKind.MKS, online=True, state=PrinterState.PRINTING),
        PrinterStatus(id="m-2", label="m", kind=PrinterKind.MKS, online=False, state=PrinterState.OFFLINE),
    ]
    # reset_errors is mirrored by nfResetSkipWhy (notification_actions.reset_skip_reason), not bulkSkipReason
    actions = [a for a in fleet_commands.BULK_ACTIONS if a != "reset_errors"]
    rows, want = [], {}
    for st in statuses:
        d = st.to_dict()
        caps, note = printer_commands.caps_for(d)
        rows.append(dict(d, caps=caps, caps_note=note))                  # as /api/printers sends it
        want[st.id] = [fleet_commands.skip_reason(a, d, caps, note) for a in actions]
    got = run("%s.map(function (p) { return [p.id, %s.map(function (a) { return bulkSkipReason(a, p); })]; })"
              % (json.dumps(rows, default=str), json.dumps(actions)))
    assert dict(got) == want
    # a Klipper that is not ready says so for its light too, as for pause and a single /command
    assert want["k-down"] == ["Klipper не готов"] * 4
    assert want["k-nopin"][:2] == ["нет управляемой подсветки"] * 2


def test_bulk_skip_reasons_of_printers_in_maintenance_match_the_server_rule(monkeypatch):
    # spec_maintenance F2: «на обслуживании» first, for every state — on the same /api/printers rows
    from app.domain.models import PrinterKind, PrinterState, PrinterStatus
    from app.services import fleet_commands, settings_service
    from app.services.normalizer import normalize_ws_dict

    monkeypatch.setattr(settings_service, "get", lambda key, **k: [] if key == "printer_light_disabled" else {})
    monkeypatch.setattr(printer_commands, "_capability", {"b-ok": {"print_cmds": True, "via": "fun", "ts": 0}})
    maint = {"since": 1000.0, "note": "<b>меняю хотенд</b>", "by": "admin"}
    statuses = [
        PrinterStatus(id="b-ok", label="b", kind=PrinterKind.BAMBU, online=True, state=PrinterState.PRINTING),
        PrinterStatus(id="b-off", label="b", kind=PrinterKind.BAMBU, online=False, state=PrinterState.OFFLINE),
        PrinterStatus(id="b-grace", label="b", kind=PrinterKind.BAMBU, online=True, state=PrinterState.PAUSED,
                      grace_period_active=True),
        normalize_ws_dict("k-down", "k", PrinterKind.KLIPPER, {"state": "printing", "debug": {"webhooks_state": "shutdown"}}),
        PrinterStatus(id="m-2", label="m", kind=PrinterKind.MKS, online=False, state=PrinterState.OFFLINE),
    ]
    actions = [a for a in fleet_commands.BULK_ACTIONS if a != "reset_errors"]
    rows, want = [], {}
    for st in statuses:
        d = dict(st.to_dict(), maintenance=maint)
        caps, note = printer_commands.caps_for(d)
        rows.append(dict(d, caps=caps, caps_note=note))
        want[st.id] = [fleet_commands.skip_reason(a, d, caps, note) for a in actions]
    got = run("%s.map(function (p) { return [p.id, %s.map(function (a) { return bulkSkipReason(a, p); })]; })"
              % (json.dumps(rows, default=str), json.dumps(actions)))
    assert dict(got) == want
    assert set(sum(want.values(), [])) == {fleet_commands.MSG_MAINT}


@pytest.mark.parametrize("state", ["printing", "paused", "finished", "error", "cancelled", "idle", "unknown", "offline",
                                   None])
def test_print_control_blocks_match_the_server(state):
    # unknown / no state: the unknown-state text, never «Принтер не печатает» for a printer that may print
    got = run("['pause', 'resume', 'stop'].map(function (a) { return ctlStateBlock(a, %s); })"
              % json.dumps({"state": state}))
    assert got == [printer_commands.state_refusal(a, state) for a in ("pause", "resume", "stop")]


@pytest.mark.parametrize("state", ["printing", "paused", "finished", "error", "cancelled", "idle", "unknown", "offline",
                                   None])
def test_cooldown_and_eject_blocks_match_the_server(state):
    # The server fails closed on an unknown state: the card's ❄ / ⬇ are off with the same text
    # instead of active buttons that end in a red ✕. A filament change is checked first, as in
    # printer_commands._state_check.
    got = run("['cooldown', 'eject'].map(function (a) { return ctlStateBlock(a, %s); })"
              % json.dumps({"state": state}))
    assert got == [printer_commands.state_refusal(a, state) for a in ("cooldown", "eject")]
    change = run("['cooldown', 'eject'].map(function (a) { return ctlStateBlock(a, %s); })"
                 % json.dumps({"state": state, "filament_change": {"action": "load", "step": "heat"}}))
    assert change == [printer_commands.MSG_CHANGE_RUNNING] * 2


def test_unknown_state_greys_out_cool_and_eject_on_the_card():
    b = buttons(row(_p(state="unknown")))
    assert [(x[0], x[3], x[4]) for x in b if x[0] in ("cool", "eject")] == [
        ("cool", MSG["unknown"], True), ("eject", MSG["unknown"], True)]


# --- chips stay alike across cards (owner, 12.10: P4 "256° →255°" wrapped to 3+2) -------

def _temp_chip(cur, target):
    src = "\n".join(_function(n) for n in ("safeText", "icon", "tempChip"))
    return _node(src + "\nconsole.log(JSON.stringify(tempChip('nozzle', 'Сопло', %s, %s, false)));"
                 % (json.dumps(cur), json.dumps(target)))


@pytest.mark.parametrize("cur,target", [(256, 255), (253, 255), (257.4, 255), (255, 255)])
def test_temp_chip_hides_the_target_within_3_degrees(cur, target):
    html = _temp_chip(cur, target)
    assert "pc-target" not in html and "pc-heating" not in html
    assert "цель 255°" in html  # still in the tooltip


@pytest.mark.parametrize("cur,target", [(150, 255), (251, 255), (259, 255)])
def test_temp_chip_shows_the_target_while_really_heating_or_cooling(cur, target):
    html = _temp_chip(cur, target)
    assert ">→255</span>" in html and "pc-heating" in html


def test_temp_chip_without_target():
    html = _temp_chip(30, 0)
    assert "pc-target" not in html and "цель" not in html


def chip_layout(normal, dense, row_width, gap=4, dense_gap=2):
    return run("chipLayout(%s, %s, %s, %s, %s)" % tuple(json.dumps(x) for x in (normal, dense, row_width, gap, dense_gap)))


def test_chip_layout_prefers_one_row_then_dense_then_balanced():
    five = [98, 56, 56, 60, 66]                               # 336 + 4*4 = 352
    assert chip_layout(five, None, 360) == {"dense": False, "sizes": None}
    dense = [88, 48, 48, 52, 58]                              # 294 + 4*2 = 302
    assert chip_layout(five, dense, 330) == {"dense": True, "sizes": None}
    # dense overflows too -> balanced rows of the regular widths, exactly as chipRows plans them
    assert chip_layout(five, dense, 290) == {"dense": False, "sizes": chip_rows(five, 290, 4)}
    assert chip_layout(five, None, 330) == {"dense": False, "sizes": [3, 2]}   # dense not measured


def test_dense_variant_css_exists_and_is_more_specific():
    assert re.search(r"\.pc-chips\[data-dense\] > \.pc-chip \{[^}]*padding", CSS)
    assert re.search(r"\.pc-chips\[data-dense\] \{[^}]*gap: 2px", CSS)


# --- Creality on the card (27.10): the row, chips and popovers from the server's caps ------

def _chips(expr: str):
    """`expr` against the ctl-ui block plus the card's chip and popover builders."""
    src = _lib() + "\n" + "\n".join(_function(n) for n in ("tempChip", "fanKeys", "fanChip", "speedChip")) \
        + "\n" + "\n".join(_decl(n) for n in ("speedPopHtml", "fanPopHtml", "tempPopHtml"))
    return _node(src + "\nconsole.log(JSON.stringify(" + expr + "));")


def _creality_row(monkeypatch, state="idle", disabled=(), **data):
    """A K1C's /api/printers row: real normalizer output and caps_for, as the server sends it."""
    from app.domain.models import PrinterKind
    from app.services import settings_service
    from app.services.normalizer import normalize_ws_dict

    monkeypatch.setattr(settings_service, "get",
                        lambda key, **k: list(disabled) if key == "printer_light_disabled" else {})
    snap = {"state": state, "nozzle_temp": 31, "bed_temp": 29, "target_nozzle_temp": 0, "target_bed_temp": 0,
            "chamber_temp": 27, "fan_speed_pct": 0, "case_fan_pct": 0, "aux_fan_pct": 0, "feedrate_pct": 100,
            "light_on": False, "material_status": 0}
    if state in ("printing", "paused"):
        snap.update(progress=40, remaining_time=600, print_time=400)
    snap.update(data)
    d = normalize_ws_dict("c-1", "K1", PrinterKind.CREALITY, snap, device_type="k1c").to_dict()
    d.pop("debug", None)
    caps, note = printer_commands.caps_for(d)
    return dict(d, caps=caps, caps_note=note)


def test_creality_row_from_the_server_caps(monkeypatch):
    p = _creality_row(monkeypatch)
    assert p["caps"] == CREALITY
    b = buttons(row(p))
    assert [(x[0], x[1]) for x in b] == [("light", "light"), ("pp", "pause"), ("stop", "stop"), ("cool", "cool")]
    assert b[0][3] == "Свет выключен — включить" and b[3][3] == "Остудить всё: сопло и стол" and b[3][4] is False
    b = buttons(row(_creality_row(monkeypatch, "printing", light_on=True)))
    assert (b[0][2], b[0][3]) == (["lit"], "Свет горит — выключить")
    assert (b[3][3], b[3][4]) == (MSG["cooldown"], True)                  # no cooling mid-print, as Bambu
    b = buttons(row(_creality_row(monkeypatch, "paused")))
    assert (b[1][1], b[3][4]) == ("resume", False)                        # cool on a pause: the card asks first
    # a model without a lamp (printer_light_disabled): no light button, the rest stays
    b = buttons(row(_creality_row(monkeypatch, disabled=["c-1"])))
    assert [x[0] for x in b] == ["pp", "stop", "cool"]
    assert "eject" not in [x[0] for x in b] and "ams" not in [x[0] for x in b]


def test_creality_chips_are_editable(monkeypatch):
    # renderCard: canEdit = admin && online && caps.tune === true -> the temperature, fan and speed chips
    assert "var canEdit = isAdmin() && !offlineFlag && caps.tune === true;" in JS
    assert "fanChip(p, canEdit)," in JS and "speedChip(p, canEdit)," in JS
    p = _creality_row(monkeypatch, "printing", target_nozzle_temp=220, nozzle_temp=150)
    assert p["caps"]["tune"] is True
    nozzle, bed, chamber = _chips("[tempChip('nozzle', 'Сопло', %s, %s, true), tempChip('bed', 'Стол', 29, 0, true),"
                                  " tempChip('chamber', 'Камера', 27, null, %s, 'temp')]"
                                  % (p["nozzle_temp"], p["target_nozzle_temp"], json.dumps(p.get("chamber_ctl") is True)))
    assert 'pc-editable" data-edit="nozzle" data-cur="220"' in nozzle and '→220' in nozzle
    assert 'pc-editable" data-edit="bed" data-cur="29"' in bed
    assert "pc-editable" not in chamber and "27°" in chamber              # K1C: a chamber sensor, no heater


def test_creality_fan_chip_sets_only_the_reported_fans(monkeypatch):
    k1c = _creality_row(monkeypatch, fan_speed_pct=40, case_fan_pct=30)
    e5 = _creality_row(monkeypatch, case_fan_pct=None, aux_fan_pct=None)       # only the part fan reported
    bare = _creality_row(monkeypatch, fan_speed_pct=None, case_fan_pct=None, aux_fan_pct=None)
    bambu = _p(fan_speed_pct=50, fans=None)
    got = _chips("[%s].map(function (p) { return [fanKeys(p), fanChip(p, true), fanChip(p, false)]; })"
                 % ",".join(json.dumps(x) for x in (k1c, e5, bare, bambu)))
    (k_keys, k_chip, k_ro), (e_keys, e_chip, _), (b_keys, b_chip, _), (bb_keys, bb_chip, _) = got
    assert k_keys == ["part", "aux", "chamber"] and k1c["fans"] == {"aux": 0, "chamber": 30}
    assert 'data-edit="fan" data-part="40" data-aux="0" data-chamber="30" data-fans="part,aux,chamber"' in k_chip
    assert "40%" in k_chip and "📦 30%" in k_chip and "pc-editable" not in k_ro
    assert e_keys == ["part"] and 'data-fans="part"' in e_chip
    assert b_keys == [] and b_chip == ""                                   # nothing to set, nothing to show
    assert bb_keys == ["part", "aux", "chamber"] and 'data-fans="part,aux,chamber"' in bb_chip   # Bambu: M106 P1-P3


def test_fan_popover_rows_follow_the_keys():
    rows_of = re.compile(r'data-fan="([a-z]+)"')
    full, e5, two = _chips("[fanPopHtml(10, 20, 30), fanPopHtml(10, 0, 0, ['part']), fanPopHtml(10, 20, 30, ['part', 'chamber'])]")
    assert rows_of.findall(full) == ["part", "aux", "chamber"]             # Bambu, as before
    assert rows_of.findall(e5) == ["part"] and 'value="10"' in e5
    assert rows_of.findall(two) == ["part", "chamber"] and "Вспом." not in two
    for html in (full, e5, two):
        assert html.endswith('<button type="button" class="pc-pop-ok" data-ok="fans">Применить</button></div>')


@pytest.mark.parametrize("state,editable", [("printing", True), ("paused", True), ("idle", False),
                                            ("finished", False), ("unknown", False), ("offline", False)])
def test_speed_chip_only_while_a_print_runs(state, editable):
    # a Creality's speed only while a print runs (= the server's _state_check)
    row = {"kind": "creality", "state": state, "feedrate_pct": 124}
    html = _chips("speedChip(%s, true)" % json.dumps(row))
    assert html.startswith('<span class="pc-chip pc-speed-fast') and "124%" in html
    assert ('pc-editable" data-edit="speed" data-cur="124"' in html) is editable
    assert "pc-editable" not in _chips("speedChip(%s, false)" % json.dumps(row))


@pytest.mark.parametrize("state", ["printing", "paused", "idle", "finished", "error", "cancelled", "unknown"])
def test_bambu_speed_chip_editable_in_any_state(state):
    # Bambu as before Creality control: editable whenever canEdit, no state gate (nor on the server)
    row = {"kind": "bambu", "state": state, "feedrate_pct": 50}
    assert _chips("speedChip(%s, true)" % json.dumps(row)) == (
        '<span class="pc-chip pc-speed-slow pc-editable" data-edit="speed" data-cur="50" data-tip="Скорость печати">'
        + '<svg class="pc-ic" aria-hidden="true"><use href="#i-gauge"></use></svg>50% ↓</span>')
    assert "pc-editable" not in _chips("speedChip(%s, false)" % json.dumps(row))


@pytest.mark.parametrize("pct,mode,mark", [(50, "slow", " ↓"), (80, "slow", " ↓"), (100, "norm", ""),
                                           (124, "fast", " ⚡"), (166, "turbo", " 🚀")])
def test_speed_chip_shows_the_reported_percent(pct, mode, mark):
    # Creality's curFeedratePct (any value set on the printer's screen) as is
    html = _chips("speedChip(%s, false)" % json.dumps({"state": "printing", "feedrate_pct": pct}))
    assert 'class="pc-chip pc-speed-%s"' % mode in html and html.endswith("%d%%%s</span>" % (pct, mark))
    assert _chips("[speedChip({feedrate_pct: null}, true), speedChip({feedrate_pct: 0, state: 'printing'}, true)]") == ["", ""]


def test_speed_popover_modes_are_the_creality_percentages():
    # the popover sends the level; the server maps it to setFeedratePct (printer_commands.CREALITY_SPEED_PCT)
    btn = re.compile(r'<button type="button" class="pc-pop-preset( on)?" data-speed="(\d)">([^<]*)</button>')
    for cur, on in (("124", "3"), (166, "4"), ("80", None), (None, None)):
        html = _chips("speedPopHtml(%s)" % json.dumps(cur))
        got = btn.findall(html)
        assert [(int(lv), text) for _on, lv, text in got] == [
            (lv, "%s · %d%%" % (name, printer_commands.CREALITY_SPEED_PCT[lv]))
            for lv, name in ((1, "Тихий"), (2, "Стандарт"), (3, "Спорт"), (4, "Безумный"))]
        assert [lv for o, lv, _t in got if o] == ([on] if on else [])


@pytest.mark.parametrize("state", ["printing", "paused", "finished", "error", "cancelled", "idle", "unknown", "offline",
                                   None])
def test_speed_block_matches_the_server(state):
    assert run("ctlStateBlock('speed', %s)" % json.dumps({"state": state})) == \
        printer_commands.state_refusal("speed", state)


# --- the cancelled state and the event log (notifications spec, step 8) -------------------

HTML = (ROOT / "app" / "web" / "index.html").read_text(encoding="utf-8")
THEME = (ROOT / "app" / "web" / "theme.js").read_text(encoding="utf-8")


def _decl(name: str) -> str:
    """The whole `function name(...) {...}` by brace count (the bodies below hold no
    unbalanced brace in a string or regex)."""
    m = re.search(r"function %s\([^)]*\) \{" % name, JS)
    assert m, "%s() not found in app.js" % name
    depth = 0
    for i in range(m.end() - 1, len(JS)):
        depth += {"{": 1, "}": -1}.get(JS[i], 0)
        if depth == 0:
            return JS[m.start():i + 1]
    raise AssertionError("unbalanced %s()" % name)


def _const(name: str) -> str:
    m = re.search(r"const %s = (\{.*?\n\s*\}|\[[^\]]*\]);" % name, JS, re.S)
    assert m, name
    return m.group(0)


def _states_js(expr: str, *, setup: str = ""):
    # sortPrinters ranks the notifications first (nfUrgency): the nf-ui block comes along; applyFilters
    # takes the chips' states (fleetBucket): the ctl-ui block with its helpers (_lib)
    src = "\n".join([_lib(), _const("STAGE_LABELS"), _decl("stageLabel"), _decl("computeUiStatus"), _decl("statusLabel"),
                     _decl("applyFilters"), _decl("sortPrinters"), _block("nf-ui"),
                     "var fleetFilter = [], currentSort = 'name';", setup])
    return _node(src + "\nconsole.log(JSON.stringify(" + expr + "));")


def test_cancelled_is_its_own_grey_status():
    got = _states_js("[computeUiStatus({state: 'cancelled', online: true, progress_pct: 47}),"
                     " statusLabel('cancelled'), computeUiStatus({state: 'error', online: true, progress_pct: 47}),"
                     " computeUiStatus({state: 'cancelled', online: false})]")
    assert got == ["cancelled", "ОТМЕНЕНО", "error", "offline"]
    # the card's ⏸ says what it says for any other non-printing state, as the server does
    assert run("ctlStateBlock('pause', {state: 'cancelled'})") == MSG["pause_other"]


def test_cancelled_and_finished_have_chips_of_their_own():
    # IA §10 (plan step 6, deliberately changed): one dictionary — «Завершено» and «Отменено» are chips
    # of their own, «Простой» is idle only (the old «Простаивают» took all three); a cancel is no error
    ps = [{"id": i, "label": i, "state": s, "online": True, "progress_pct": 10}
          for i, s in (("a", "printing"), ("b", "cancelled"), ("c", "paused"), ("d", "finished"), ("e", "error"),
                       ("f", "idle"), ("g", "unknown"))]

    def ids(*chips):
        return _states_js("applyFilters(%s).map(function (p) { return p.id; })" % json.dumps(ps),
                          setup="fleetFilter = %s;" % json.dumps(list(chips)))
    assert ids("idle") == ["f", "g"]                             # unknown (with telemetry) counts as «Простой»
    assert ids("cancelled") == ["b"] and ids("finished") == ["d"] and ids("error") == ["e"]
    assert ids("finished", "cancelled") == ["b", "d"] and ids() == list("abcdefg")
    # no group order any more (IA §10): by name, only a decision rises
    order = _states_js("sortPrinters(%s).map(function (p) { return p.id; })" % json.dumps(ps))
    assert order == list("abcdefg")


def test_a_decision_always_rises_the_rest_by_name():
    # IA §5, §10 (plan step 6, deliberately changed): no «Проблемные сверху» switch — a printer that wants
    # a decision (a fatal / a pause on an error that holds it) is always first, the rest by name, natural
    # (P2 before P10); no group order (error / paused / finished before the others) any more.
    # A lasting serious HMS (H2's «drying on printer power»), routine notes and a fatal HMS on a printer
    # that keeps printing (C1, owner 27.10) move nothing
    def n(sev):
        return [{"key": "hms:x", "code": "x", "severity": sev}]
    ps = [{"id": "err", "label": "A", "state": "error", "online": True},
          {"id": "fatal", "label": "B", "state": "printing", "online": True, "notifications": n("fatal")},
          {"id": "pause", "label": "P10", "state": "paused", "online": True, "notifications": n("pause")},
          {"id": "user", "label": "D", "state": "paused", "online": True},
          {"id": "serious", "label": "E", "state": "printing", "online": True, "notifications": n("serious")},
          {"id": "maint", "label": "F", "state": "idle", "online": True, "notifications": n("common")},
          {"id": "held", "label": "P2", "state": "error", "online": True, "notifications": n("fatal")}]
    order = _states_js("sortPrinters(%s).map(function (p) { return p.id; })" % json.dumps(ps))
    assert order == ["held", "pause", "err", "fatal", "user", "serious", "maint"]   # fatal (4) before pause (3)
    # «Финиш»: the decisions first all the same, then the prints with an ETA by the time left, the rest by name
    ps2 = [{"id": "p10", "label": "P10", "state": "printing", "online": True, "eta_seconds": 600},
           {"id": "p2", "label": "P2", "state": "printing", "online": True, "eta_seconds": 3600},
           {"id": "noeta", "label": "A1", "state": "printing", "online": True, "eta_seconds": None},
           {"id": "paused", "label": "B1", "state": "paused", "online": True, "eta_seconds": 300},
           {"id": "off", "label": "C1", "state": "offline", "online": False, "eta_seconds": 100},
           {"id": "pause", "label": "Z9", "state": "paused", "online": True, "notifications": n("pause")}]
    order = _states_js("sortPrinters(%s).map(function (p) { return p.id; })" % json.dumps(ps2),
                       setup="currentSort = 'eta';")
    assert order == ["pause", "p10", "p2", "noeta", "paused", "off"]
    order = _states_js("sortPrinters(%s).map(function (p) { return p.id; })" % json.dumps(ps2))
    assert order == ["pause", "noeta", "paused", "off", "p2", "p10"]
    sort = _decl("sortPrinters")
    assert "alertsTop" not in JS and "alertsTopToggle" not in JS and "Проблемные сверху" not in HTML


def _hist_block() -> str:
    m = re.search(r"/\* hist-ui:begin \*/(.*?)/\* hist-ui:end \*/", JS, re.S)
    assert m, "hist-ui block not found"
    return m.group(1)


def test_every_state_has_a_label_a_colour_and_a_legend():
    # plan step 8 (deliberately changed): the event log and the summary bars are gone with their own
    # words; the bars and the legend of «Хронология» / «Статистика» take the one dictionary (IA §10)
    from app.domain.models import PrinterState
    states = [s.value for s in PrinterState]
    labels = _node("\n".join([_decl("statusLabel"), _hist_block()])
                   + "\nconsole.log(JSON.stringify([%s.map(statusLabel), HIST_SEGS]));" % json.dumps(states))
    assert "—" not in labels[0]
    segs = [x["id"] for x in labels[1]]
    assert set(states) - {"unknown"} <= set(segs) and "nodata" in segs     # unknown counts as «Нет данных»
    words = {x["id"]: x["label"] for x in labels[1]}
    assert words == {"printing": "Печатает", "idle": "Простой", "paused": "Пауза", "error": "Ошибка",
                     "cancelled": "Отменено", "finished": "Завершено", "offline": "Офлайн",
                     "maintenance": "Обслуживание", "nodata": "Нет данных"}
    assert segs.index("cancelled") == segs.index("error") + 1
    names = re.search(r"var stateNames = (\{[^}]*\});", JS).group(1)
    for s in states:
        assert re.search(r"\b%s: '" % s, names), s
        assert re.search(r"\b%s: style\.getPropertyValue" % s, THEME), s
    assert re.search(r"cancelled: 3\.5\b", re.search(r"var stateOrder = \{[^}]*\}", JS).group(0))
    for sel in (".seg-cancelled", ".dot-cancelled", ".event-badge-cancelled"):
        assert re.search(r"(?m)^\s*%s \{[^}]*var\(--gray-" % re.escape(sel), CSS), sel
    assert re.search(r"\.status-badge\.cancelled \{[^}]*color: var\(--gray-11\)", CSS)
    assert re.search(r"\.printer-card\.cancelled \{[^}]*--bg-panel", CSS)
    assert "cancelled: style.getPropertyValue('--gray-9')" in THEME
    # one legend, drawn from the dictionary into «Хронология» and «Статистика»
    assert HTML.count("data-hist-legend") == 2 and "seg-cancelled" not in HTML
    assert 'document.querySelectorAll("[data-hist-legend]").forEach(function (el) { el.innerHTML = histLegendHtml(); });' in JS
    assert not re.findall(r'class="event-state-btn[^"]*" data-state=', HTML)        # the event log's filter is gone


# --- maintenance mode on the page (spec_maintenance F6–F17, F20) ----------------------------

_MAINT = {"since": 1000.0, "note": None, "by": "admin"}


def test_a_printer_in_maintenance_matches_its_own_filter_only():
    ps = [{"id": "off", "label": "A", "state": "offline", "online": False},
          {"id": "m-off", "label": "B", "state": "offline", "online": False, "maintenance": _MAINT},
          {"id": "m-pr", "label": "C", "state": "printing", "online": True, "maintenance": _MAINT},
          {"id": "pr", "label": "D", "state": "printing", "online": True}]

    def ids(*filters):
        return _states_js("applyFilters(%s).map(function (p) { return p.id; })" % json.dumps(ps),
                          setup="fleetFilter = %s;" % json.dumps(list(filters)))
    assert ids("offline") == ["off"]                  # = the «Офлайн» chip, which leaves them out
    assert ids("printing") == ["pr"]                  # «Онлайн» is gone with the old filters (IA §11, plan step 6)
    assert ids("maintenance") == ["m-off", "m-pr"]
    assert ids("maintenance", "offline") == ["off", "m-off", "m-pr"]
    assert ids() == ["off", "m-off", "m-pr", "pr"]
    assert '{ id: "maintenance", label: "Обслуживание", short: "Обсл." }' in re.search(r"var FLEET_CHIPS = \[.*?\];", JS, re.S).group(0)


def test_a_card_in_maintenance_never_rises():
    # F12: its error / pause (and a fatal notification) do not rise; its print is not ordered by its ETA.
    # Plan step 6 (deliberately changed): no group order any more — by name, a decision first
    fatal = [{"key": "pe:x", "code": "x", "severity": "fatal"}]
    ps = [{"id": "err", "label": "A", "state": "error", "online": True},
          {"id": "m-err", "label": "B", "state": "error", "online": True, "maintenance": _MAINT, "notifications": fatal},
          {"id": "m-pause", "label": "C", "state": "paused", "online": True, "maintenance": _MAINT},
          {"id": "idle", "label": "D", "state": "idle", "online": True},
          {"id": "m-off", "label": "E", "state": "offline", "online": False, "maintenance": _MAINT},
          {"id": "pr", "label": "F", "state": "printing", "online": True},
          {"id": "m-pr", "label": "G", "state": "printing", "online": True, "maintenance": _MAINT, "eta_seconds": 60},
          {"id": "held", "label": "H", "state": "error", "online": True, "notifications": fatal}]
    order = _states_js("sortPrinters(%s).map(function (p) { return p.id; })" % json.dumps(ps))
    assert order == ["held", "err", "m-err", "m-pause", "idle", "m-off", "pr", "m-pr"]
    order = _states_js("sortPrinters(%s).map(function (p) { return p.id; })" % json.dumps(ps), setup="currentSort = 'eta';")
    assert order == ["held", "err", "m-err", "m-pause", "idle", "m-off", "pr", "m-pr"]


def test_stats_and_timeline_know_maintenance():
    # plan step 8: «Статистика» (histShares: not in «Печать» / «Загрузка», tests/test_web_hist_ui.py)
    order = _node(_hist_block() + "\nconsole.log(JSON.stringify(HIST_SEGS.map(function (s) { return s.id; })));")
    assert order[-2:] == ["maintenance", "nodata"]
    shares = _hist_block()
    assert "known: total - sec.nodata - sec.maintenance" in shares
    assert re.search(r"\bmaintenance: 'Обслуживание'", re.search(r"var stateNames = (\{[^}]*\});", JS).group(1))
    assert "maintenance: style.getPropertyValue('--warn').trim()" in THEME
    draw = _decl("drawTimeline")
    assert "var maintPattern = makeMaintPattern(ctx);" in draw and "ctx.fillStyle = maintPattern;" in draw
    pattern = _decl("makeMaintPattern")
    assert "'--warn-5'" in pattern and "'--warn-7'" in pattern


def test_card_menus_and_tabs_are_wired():
    card = _decl("renderPrinterCard")
    assert '${inMaint(p) ? " maint" : ""}' in card
    assert "${maintPlaqueHtml(p, isAdmin(), Date.now())}" in card
    assert card.index("${maintPlaqueHtml(") < card.index("${showProgress ?")              # right under the header
    assert card.index('<div class="status-badge ${uiStatus}">') < card.index("${headMore}")
    assert "headMoreHtml(p, cardUi[p.id], { admin: isAdmin(), offline: offlineFlag })" in card
    # the head's ⋯ opens the menu with its options; the form is wired. No «снять» on the plaque (IA §5)
    assert "ctlMenuHtml(p, ui, Date.now(), menuOpts(p))" in _decl("togglePop")
    assert 'swapPop(maintPopHtml(p), "maint")' in _decl("onMenuClick")
    assert ".pc-head .pc-ctl[data-cmd=more]" in JS and ".pc-head [data-cmd=more]" in JS
    assert "data-maint-off" not in JS
    pop = _decl("onPopClick")
    sends = re.search(r"var sends = ([^;]*);", pop).group(1)
    assert "[data-maint-save]" in pop and "menu" not in sends                  # a busy card does not hold the menu
    put = _decl("setMaintenance")
    assert '"/api/admin/printers/" + encodeURIComponent(pid) + "/maintenance"' in put and 'method: "PUT"' in put
    for toast in ('"Заметка сохранена"', '": на обслуживании"', '": снят с обслуживания"'):
        assert toast in put, toast
    # plan step 14 (deliberately changed): «Снять с обслуживания» asks in the ⋯ popover itself, not with
    # confirm() — the question and its buttons are maintOffAskHtml (tests/test_web_confirm_ui.py)
    assert "maintOffAsk(" not in JS and 'swapPop(maintOffAskHtml(p), "menu");' in _decl("onMenuClick")
    # «Окно», the AMS tab, the admin table
    assert 'p.state === "printing" && !inMaint(p)' in _decl("renderForecast")
    # on a line of its own under the header: in the badges row it squeezed the title (375 px)
    ams = _decl("amsCardShell")
    assert ("(u.maintenance ? '<div class=\"ams-maint\"><span class=\"ams-badge warn\">на обслуживании</span></div>' : '')"
            in ams)
    assert ams.index("'<div class=\"ams-badges\">' + badges") < ams.index('<div class="ams-maint">')
    assert "на обслуживании" not in ams[:ams.index("var slots")]
    # plan step 13 (deliberately changed): «Парк» shows the status and «к карточке ›» (parkMaintHtml,
    # tests/test_web_adm_ui.py); maintenance is put on and taken off through the card's ⋯ only, no prompt()
    adm = _decl("renderAdminPrinters")
    assert 'data-act="maint-on"' not in adm and 'data-act="maint-off"' not in adm
    assert "(p.pending === 'deleted' ? '<span class=\"muted\">—</span>' : parkMaintHtml(p, nowMs))" in adm  # none for a deleted row
    init = _decl("initAdminPrinters")
    assert "prompt(" not in init and "maint-on" not in init and "if (btn.dataset.act === 'card') {" in init


def test_maintenance_markup_and_styles():
    # H1–H4, C1–C5: the «Обслуживание» chip over the grid (plan step 6: the KPI tiles of «Сводка» are the
    # chips now, the chip stays at 0 — dim), both legends, the yellow hatching / dot / chip colour, the
    # plaque in the .ams-dry shape, the header ⋯
    assert "dash-kpi maint" not in HTML and "kpiMaint" not in HTML and "kpiMaint" not in JS
    assert re.search(r"\.farm-chip\.st-maintenance \{ --st: var\(--warn-7\); \}", CSS)
    # one legend from the dictionary (plan step 8): «Офлайн», «Обслуживание», «Нет данных» in this order
    legend = _node(_hist_block() + "\nconsole.log(JSON.stringify(histLegendHtml()));")
    assert legend.index('<i class="seg-offline"></i>Офлайн') < legend.index('<i class="seg-maintenance"></i>Обслуживание') \
        < legend.index('<i class="seg-nodata"></i>Нет данных')
    assert HTML.index("data-hist-legend") < HTML.index('id="timeline-scale-label"')
    assert int(re.search(r"styles\.css\?v=(\d+)", HTML).group(1)) >= 91
    assert int(re.search(r"theme\.js\?v=(\d+)", HTML).group(1)) >= 4
    assert int(re.search(r"app\.js\?v=(\d+)", HTML).group(1)) >= 108
    assert re.search(r"(?m)^\.seg-maintenance \{[^}]*var\(--warn-5\)[^}]*var\(--warn-7\)", CSS)
    assert re.search(r"(?m)^\.dot-maintenance \{[^}]*var\(--warn\)", CSS)
    plaque = _rule(".pc-maint")
    # owner, 19.10: the note must not catch the eye — no fill, no border, a dim line
    for decl in ("background:none", "border:0", "color:var(--text-dim)", "opacity:0.55"):
        assert decl in plaque, decl
    assert "warn" not in plaque
    assert "animation" not in plaque and "-webkit-line-clamp:2" in _rule(".pc-maint .t")
    head = _rule(".pc-ctl.pc-ctl-head")
    assert "width: 30px" in head and "height: 28px" in head                   # the mockup's size: the card's only ⋯ (IA §5)
    assert "background: transparent" in _rule(".pc-ctl.pc-ctl-head:not(.open)")
    # a 40 px tap target without moving anything: the ⋯ plus 2×6
    hit = _rule(".pc-ctl.pc-ctl-head::before")
    assert 'content: ""' in hit and "position: absolute" in hit and "inset: -6px" in hit
    assert "position: relative" in _rule(".pc-ctl")
    assert ".pc-maint .bd-link" not in CSS                                    # no «снять» on the plaque
    assert "margin-top:8px" in _rule(".ams-maint")
    # every theme has the --warn steps the hatching and the plaque use
    for theme in ("dark-classic", "light", "night", "neon", "cosmos"):
        block = re.search(r'html\[data-theme="%s"\] \{(.*?)\n    \}' % theme, CSS, re.S).group(1)
        assert "--warn-5:" in block and "--warn-7:" in block and "--warn-11:" in block and "--warn-soft:" in block, theme
