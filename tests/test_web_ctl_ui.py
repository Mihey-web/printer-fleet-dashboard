"""Card controls, ⋯ menu, command results and adaptive chip rows of app/web/app.js, run in node.

The block between /* ctl-ui:begin */ and /* ctl-ui:end */ holds only pure code;
it may use safeText, icon, amsUnitName, amsFmtMin (function declarations
elsewhere in app.js) and the ams-ui block, which are extracted alongside it.
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
    src = "\n".join(_function(n) for n in ("safeText", "icon", "amsUnitName", "amsFmtMin"))
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


def test_bambu_printing_has_seven_buttons():
    html = row(_p())
    b = buttons(html)
    assert [x[0] for x in b] == ["light", "pp", "stop", "cool", "eject", "ams", "more"]
    assert [x[1] for x in b] == ["light", "pause", "stop", "cool", "eject", "ams", "more"]
    assert [x[4] for x in b] == [False, False, False, True, True, False, False]
    assert b[3][3] == MSG["cooldown"] and b[4][3] == MSG["eject"]
    assert b[0][2] == ["lit"] and b[0][3] == "Свет горит — выключить"
    assert b[1][3] == "Пауза" and b[2][2] == ["danger"] and b[2][3] == "Остановить печать"
    assert b[5][3] == "AMS: слоты, загрузка, выгрузка, сушка" and b[6][3] == "Ещё: остудить, стол, скорость"
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


def test_ams_button_without_an_ams():
    b = buttons(row(_p(state="idle", ams=None, filament_loaded=False)))[5]
    assert (b[4], b[3]) == (True, "AMS не подключена")
    b = buttons(row(_p(state="idle", ams=None, filament_loaded=True)))[5]           # external spool loaded
    assert (b[4], b[3]) == (False, "Внешняя катушка: выгрузка")
    b = buttons(row(_p(state="printing", ams=None, filament_loaded=True)))[5]       # the real reason
    assert (b[4], b[3]) == (True, "Только в простое — принтер печатает или на паузе")


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
    assert [x[0] for x in buttons(row(_p(kind="creality", caps=creality, ams=None)))] == ["pp", "stop"]
    listed = dict(creality, light=True)
    assert [x[0] for x in buttons(row(_p(kind="creality", caps=listed, ams=None)))] == ["light", "pp", "stop"]
    klipper = dict(NO_CAPS, pause=True, resume=True, stop=True, light=True)
    for state, pp in (("idle", "pause"), ("paused", "resume"), ("printing", "pause")):
        b = buttons(row(_p(kind="klipper", caps=klipper, state=state, ams=None)))
        assert [(x[0], x[1]) for x in b] == [("light", "light"), ("pp", pp), ("stop", "stop")]


def test_grace_disables_every_button():
    b = buttons(row(_p(grace_period_active=True)))
    assert len(b) == 7 and all(x[4] and x[3] == GRACE for x in b)


def test_busy_result_and_open_popover_classes():
    b = buttons(row(_p(), {"busy": "pp", "res": {"slot": "light", "k": "ok"}, "pop": "menu"}))
    assert "is-wait" in b[1][2] and "is-ok" in b[0][2] and "open" in b[6][2]
    assert "open" not in b[5][2]
    b = buttons(row(_p(), {"res": {"slot": "stop", "k": "bad"}, "pop": "ams"}))
    assert "is-bad" in b[2][2] and "open" in b[5][2]


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


# --- the ⋯ menu and the external spool popover ---------------------------------------

def menu(p, ui=None, now=1000):
    return run("ctlMenuHtml(%s, %s, %d)" % (json.dumps(p), json.dumps(ui), now))


_ITEM = re.compile(r'<button type="button" class="pm-item" data-menu="([^"]*)"( aria-disabled="true")?>')


def test_menu_while_printing():
    html = menu(_p())
    assert _ITEM.findall(html) == [("cooldown", " aria-disabled=\"true\""), ("eject", " aria-disabled=\"true\""),
                                   ("speed-toggle", "")]
    assert "Остудить и опустить стол можно, когда печать не идёт." in html
    assert "(сопло и стол)" in html and "(снять деталь)" in html
    assert '<span class="val">Стандарт · 100%</span>' in html
    assert "pm-speed" not in html and '<div class="pm-sep"></div>' in html


def test_menu_reasons_and_speed():
    html = menu(_p(state="paused", chamber_ctl=True, feedrate_pct=124), {"speedOpen": True})
    assert _ITEM.findall(html) == [("cooldown", ""), ("eject", ' aria-disabled="true"'), ("speed-toggle", "")]
    assert "Стол опускается только после остановки печати." in html and "(сопло, стол, камера)" in html
    presets = re.findall(r'<button type="button" class="pc-pop-preset( on)?" data-speed="(\d)">([^<]*)</button>', html)
    assert presets == [("", "1", "Тихий · 50%"), ("", "2", "Стандарт · 100%"), (" on", "3", "Спорт · 124%"),
                       ("", "4", "Безумный · 166%")]
    html = menu(_p(state="idle", feedrate_pct=90), {"speedOpen": True})
    assert _ITEM.findall(html) == [("cooldown", ""), ("eject", ""), ("speed-toggle", ' aria-disabled="true"')]
    assert "pop-why" not in html and "pm-speed" not in html and '<span class="val">90%</span>' in html
    assert '<span class="val">—</span>' in menu(_p(state="idle", feedrate_pct=None))
    html = menu(_p(state="idle"), {"ejectUntil": 5000}, now=1000)
    assert ("eject", ' aria-disabled="true"') in _ITEM.findall(html) and EJECT_WAIT in html
    html = menu(_p(state="idle", filament_change={"action": "unload", "step": "cut"}))
    assert "Идёт смена филамента — дождитесь окончания" in html


def test_menu_items_follow_the_caps():
    html = menu(_p(caps=dict(NO_CAPS, tune=True, pause=True)))
    assert [m for m, _d in _ITEM.findall(html)] == ["speed-toggle"]
    html = menu(_p(caps=dict(NO_CAPS, cooldown=True, eject=True, pause=True), state="idle"))
    assert [m for m, _d in _ITEM.findall(html)] == ["cooldown", "eject"] and "pm-sep" not in html


def test_external_spool_popover():
    html = run("extSpoolPopHtml(%s)" % json.dumps(_p(state="idle", ams=None)))
    assert html == ('<div class="pc-pop-h">Внешняя катушка</div><div class="pc-pop-row">'
                    '<button type="button" class="pc-pop-ok" data-ams-unload>Выгрузить филамент</button></div>')
    html = run("extSpoolPopHtml(%s)" % json.dumps(_p(state="printing", ams=None)))
    assert "data-ams-unload" not in html and "Только в простое" in html


def test_state_block_and_speed_level():
    cases = run("[ctlStateBlock('speed', {state: 'idle'}), ctlStateBlock('speed', {state: 'paused'}),"
                " ctlStateBlock('resume', {state: 'printing'}), ctlStateBlock(null, {state: 'idle'}),"
                " speedLevelOf(166), speedLevelOf(101), speedLevelOf(null)]")
    assert cases == ["Скорость меняется только во время печати", None, MSG["resume"], None, 4, None, None]


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
    m = re.findall(r"@container pcard ((?:\(max-width: [\d.]+(?:rem|px)\)(?: or )?)+) \{", CSS)
    assert len(m) == 1, m
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
CREALITY = dict(NO_CAPS, pause=True, resume=True, stop=True, light=None)
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
        _p(id="c-1", label="K1", kind="creality", state="idle", caps=dict(CREALITY)),
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


_BULK_BTN = re.compile(r'<button type="button" class="fb-btn([^"]*)" data-bulk="([^"]*)"( aria-disabled="true")?'
                       r'(?: data-tip="([^"]*)")?>')


def test_fleet_bulk_bar():
    html = run("fleetBulkHtml(%s, {})" % json.dumps(_farm()))
    # one line with the sort bar (owner, 13.10): no title box, no group labels
    assert html.startswith('<div class="fleet-bulk" role="toolbar" aria-label="Вся ферма"><span class="fb-meta')
    assert "fb-title" not in html and "fb-lbl" not in html
    assert [(b[1], b[2]) for b in _BULK_BTN.findall(html)] == [("light_on", ""), ("light_off", ""), ("pause", ""),
                                                               ("resume", "")]
    assert html.count("горит <b>6</b> из 9 · 1 офлайн") == 2               # desktop + phone meta
    assert re.search(r'data-bulk="pause"[^>]*>.*?Пауза всех <span class="fb-n">2</span>', html)
    assert re.search(r'data-bulk="resume"[^>]*>.*?Продолжить <span class="fb-n">2</span>', html)
    nobody = [p for p in _farm() if p["state"] not in ("printing", "paused")]
    html = run("fleetBulkHtml(%s, {})" % json.dumps(nobody))
    btns = {b[1]: (b[2], b[3]) for b in _BULK_BTN.findall(html)}
    assert btns["pause"] == (' aria-disabled="true"', "Никто не печатает")
    assert btns["resume"] == (' aria-disabled="true"', "На паузе никого нет")
    # no printer with a light or print control: no bar at all
    assert run("fleetBulkHtml(%s, {})" % json.dumps([p for p in _farm() if p["kind"] == "mks"])) == ""
    # light only (print control blocked everywhere): no print group
    html = run("fleetBulkHtml(%s, {})" % json.dumps([_p(caps=dict(BAMBU_BLOCKED))]))
    assert "data-bulk=\"light_on\"" in html and "data-bulk=\"pause\"" not in html and "fb-sep" not in html


def test_fleet_bulk_bar_busy_message_and_failures():
    bulk = {"busy": "light_on", "msg": {"k": "bad", "t": "Свет включён: ✓ 3 · ✕ 1"},
            "failed": {"action": "light_on", "rows": [
                {"id": "a", "label": "P<1>", "status": "rejected", "detail": "🔒 <b>блок</b>"},
                {"id": "b", "label": "P2", "status": "sent", "detail": "отправлено (без подтверждения)"}]}}
    html = run("fleetBulkHtml(%s, %s)" % (json.dumps(_farm()), json.dumps(bulk)))
    assert re.search(r'class="fb-btn light is-wait" data-bulk="light_on"[^>]*><span class="spin-sm"></span>Свет вкл', html)
    assert html.count('<span class="fb-meta bad') == 2 and "Свет включён: ✓ 3 · ✕ 1" in html
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

    monkeypatch.setattr(settings_service, "get", lambda key, **k: ["c-lit"] if key == "printer_light_enabled" else {})
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
    actions = list(fleet_commands.BULK_ACTIONS)
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


@pytest.mark.parametrize("state", ["printing", "paused", "finished", "error", "idle", "unknown", "offline", None])
def test_print_control_blocks_match_the_server(state):
    # unknown / no state: the unknown-state text, never «Принтер не печатает» for a printer that may print
    got = run("['pause', 'resume', 'stop'].map(function (a) { return ctlStateBlock(a, %s); })"
              % json.dumps({"state": state}))
    assert got == [printer_commands.state_refusal(a, state) for a in ("pause", "resume", "stop")]


@pytest.mark.parametrize("state", ["printing", "paused", "finished", "error", "idle", "unknown", "offline", None])
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


def test_unknown_state_greys_out_cool_and_eject_on_the_card_and_in_the_menu():
    b = buttons(row(_p(state="unknown")))
    assert [(x[0], x[3], x[4]) for x in b if x[0] in ("cool", "eject")] == [
        ("cool", MSG["unknown"], True), ("eject", MSG["unknown"], True)]
    html = menu(_p(state="unknown"))
    assert _ITEM.findall(html)[:2] == [("cooldown", ' aria-disabled="true"'), ("eject", ' aria-disabled="true"')]
    assert MSG["unknown"] in html


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
