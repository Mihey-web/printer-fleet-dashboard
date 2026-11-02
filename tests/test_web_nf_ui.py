"""Printer notifications of app/web/app.js (badge, cause strip, lists, bell, fleet reset), run in node.

The block between /* nf-ui:begin */ and /* nf-ui:end */ holds only pure code;
it may use safeText, icon, plural and cmdLineHtml (function declarations
elsewhere in app.js), which are extracted alongside it — nothing else is
loaded, so a call to anything outside the block fails the test. Records are
built in the server's shape (notification_actions.describe for the buttons,
notifications.build_klipper for Klipper). Skipped without node.
"""
import json
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from app.collectors.bambu_raw import CANCEL_CODES
from app.domain.models import PrinterKind, PrinterState, PrinterStatus
from app.services import fleet_commands, hms_catalog, notification_actions as na, notifications, printer_commands
from app.services import settings_service

ROOT = Path(__file__).resolve().parents[1]
JS = (ROOT / "app" / "web" / "app.js").read_text(encoding="utf-8")
CSS = (ROOT / "app" / "web" / "styles.css").read_text(encoding="utf-8")
HTML = (ROOT / "app" / "web" / "index.html").read_text(encoding="utf-8")


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
    return "\n".join(_function(n) for n in ("safeText", "icon", "plural", "cmdLineHtml")) + "\n" + _block("nf-ui")


def run(expr: str):
    """Evaluate `expr` (JSON-serializable) against the nf-ui block in node."""
    return _node(_lib() + "\nconsole.log(JSON.stringify(" + expr + "));")


def js(v) -> str:
    return json.dumps(v, ensure_ascii=False)


def rec(key, severity="pause", text="Текст уведомления", actions=(), blocking=False, since=None, hint=None,
        wiki=None, auto=None, src=None):
    """A record as notifications._record builds it; actions: keys or (key, bambu_id)."""
    src = src or {"pe": "print_error"}.get(key.split(":")[0], key.split(":")[0])
    n = {"key": key, "src": src, "code": key.split(":", 1)[1], "severity": severity, "text": text,
         "unknown": text is None, "hint": hint, "wiki": wiki, "since": since, "seen_at": since,
         "actions": na.describe((a, None) if isinstance(a, str) else a for a in actions), "blocking": blocking,
         "routine": src == "hms" and severity in ("common", "info")}
    if auto is not None:
        n["auto"] = auto
    return n


def prn(*items, pid="b1", label="P1", kind="bambu", state="paused", online=True, grace=False, model="P2S",
        resume=True, note=None, pct=None):
    """A /api/printers row (caps / caps_note as the API adds them)."""
    return {"id": pid, "label": label, "kind": kind, "state": state, "online": online, "grace_period_active": grace,
            "device_type": model, "caps": {"resume": resume}, "caps_note": note, "progress_pct": pct,
            "notifications": list(items)}


PE = rec("pe:0500_8062", actions=(("ignore", 27), ("resume", 28)), wiki="https://e.bambulab.com/index.php?e=05008062")
STOP = rec("pe:0300_8007", actions=(("resume", 2), ("stop", 5)), blocking=True)
CANCEL = rec("pe:0300_400C", severity="common", text="Задача отменена.", actions=("clean",), hint=notifications.CANCEL_HINT)
FATAL = rec("hms:0C00_0100_0001_0004", severity="fatal", hint=notifications.NO_COMMAND_HINT)
SERIOUS = rec("hms:0300_1100_0002_0001", severity="serious")
SILENT = rec("hms:0500_0600_0002_0070", severity="serious", text=None, hint=notifications.UNKNOWN_HINT)
LUBE = [rec("hms:0501_0400_0003_000%d" % i, severity="common", hint=notifications.LUBE_HINT) for i in (2, 4)]
MEM = rec("hms:0500_0100_0003_0015", severity="common")
ADMIN = {"admin": True}
VIEWER = {"admin": False}
CTL = _function("cmdLineHtml")


def test_block_is_pure():
    code = re.sub(r"//[^\n]*", "", _block("nf-ui"))
    for banned in ("document.", "window.", "fetch(", "apiFetch", "cachedAllPrinters", "localStorage", "cardUi",
                   "nfUi", "statusLabel(", "computeUiStatus(", "setTimeout"):
        assert banned not in code, banned
    assert not re.search(r"\bNET_LOST\b", code)                     # ctl-ui's: the block keeps its own copy


# --- the server's tables, mirrored -----------------------------------------------------------

def test_levels_match_the_server():
    sev = run("NF_SEV")
    assert {k: v["r"] for k, v in sev.items()} == notifications.RANK
    assert set(run("NF_SEV_ORDER")) == set(notifications.RANK)
    assert run("[nfCls('info'), nfCls('bogus'), nfCls('pause'), nfRank('bogus'), nfRank('info'), nfSevLabel('bogus')]") == [
        "common", "common", "pause", 0, 1, "Уведомление"]


def test_tables_match_the_server():
    assert run("NF_PREFIX") == hms_catalog.MODEL_PREFIX
    assert run("NF_CANCEL_KEYS") == ["pe:" + na.pe_label(c) for c in CANCEL_CODES]
    assert run("NF_RESET") == {
        "maintenance": fleet_commands.MSG_MAINT, "offline": fleet_commands.MSG_OFFLINE, "grace": fleet_commands.MSG_GRACE,
        "not_wired": printer_commands.NOTE_NOT_WIRED, "empty": na.RESET_EMPTY, "hms_only": na.RESET_HMS_ONLY,
        "hms_fatal": na.RESET_HMS_FATAL, "pe": na.RESET_PE, "pause": na.RESET_PAUSE, "blocking": na.RESET_BLOCKING,
        "auto_tail": na.RESET_AUTO_TAIL, "klipper": na.RESET_KLIPPER, "klipper_none": na.RESET_KLIPPER_NONE,
        "creality": na.RESET_CREALITY}
    assert run("NF_RESET_BLOCKING") == na.RESET_BLOCKING_TEXT
    net_lost = re.search(r'var NET_LOST = "([^"]*)";', JS).group(1)
    assert run("NF_NET_LOST") == net_lost
    assert run("[nfPrefix('p2s'), nfPrefix('A1 mini'), nfPrefix('H2-S'), nfPrefix('K1C'), nfPrefix(null)]") == [
        "22E", "030", "093", None, None]
    assert [hms_catalog.model_prefix(m) for m in ("p2s", "A1 mini", "H2-S", "K1C", None)] == ["22E", "030", "093", None, None]


# --- order and levels ------------------------------------------------------------------------

def test_sort_worst_and_attention():
    items = [MEM, SERIOUS, PE, LUBE[0], FATAL, rec("klipper:startup", severity="info")]
    out = run("nfSort(%s).map(function (n) { return n.key; })" % js(items))
    assert out == [FATAL["key"], PE["key"], SERIOUS["key"], MEM["key"], LUBE[0]["key"], "klipper:startup"]  # stable
    cases = [prn(), prn(SERIOUS, MEM), prn(MEM, PE), prn(SERIOUS, FATAL), prn(CANCEL), prn(rec("x:y", severity="bogus"))]
    got = run("%s.map(function (p) { return [nfWorst(p), nfAttention(p), nfUrgency(p)]; })" % js(cases))
    assert got == [[None, False, 0], ["serious", False, 0], ["pause", True, 3], ["fatal", True, 4],
                   ["common", False, 0], ["bogus", False, 0]]
    assert run("nfItems({})") == [] and run("nfItems({notifications: 'x'})") == []


def test_a_fatal_hms_on_a_printing_printer_is_no_decision():
    # owner, 27.10: C1 (X1C) printed for weeks with a «fatal» lidar-lens HMS and sat on top of
    # «Проблемные сверху»; only a paused / errored printer is held by its records
    for st in ("printing", "idle", "finished", "cancelled"):
        p = prn(FATAL, MEM, state=st)
        assert run("[nfWorst(%s), nfAttention(%s), nfUrgency(%s), nfStripHtml(%s, true)]" % ((js(p),) * 4)) == [
            "fatal", False, 0, ""], st
        # still on its card, grey: nothing to decide (IA §5, loud only while fresh — test_badge_loudness)
        assert 'class="nf-badge quiet"' in run("nfBadgeHtml(%s, false)" % js(p))
    for st in ("paused", "error"):
        p = prn(FATAL, state=st)
        assert run("[nfAttention(%s), nfUrgency(%s)]" % (js(p), js(p))) == [True, 4], st
        assert "nf-strip fatal" in run("nfStripHtml(%s, true)" % js(p))
    bell = run("nfBellHtml(%s, false)" % js([prn(FATAL, pid="c1", label="C1", state="printing"),
                                             prn(PE, pid="p3", label="P3")]))
    assert "1 принтер требует внимания" in bell


def test_code_label_and_since():
    assert run("[nfCodeLabel(%s), nfCodeLabel(%s), nfCodeLabel(%s)]" % (js(FATAL), js(PE), js(rec("klipper:shutdown")))) == [
        "HMS_0C00_0100_0001_0004", "0500_8062", "shutdown"]
    ts = 1792252020.0                                   # node and Python share the machine's time zone
    d = datetime.fromtimestamp(ts)
    assert run("[nfSinceText(%s), nfClock(%s)]" % (ts, ts)) == [d.strftime("с %d.%m %H:%M"), d.strftime("%H:%M")]
    assert run("[nfSinceText(null), nfSinceText(0), nfSinceText('x'), nfSinceText(NaN), nfClock(undefined)]") == [""] * 5


# --- the card: badge and cause strip ---------------------------------------------------------

INFO_IC = '<svg class="pc-ic" aria-hidden="true"><use href="#i-info"></use></svg>'


def test_badge_counts_and_takes_the_worst_level():
    # a decision wanted (the bell's predicate): «⚠ N» in the worst level's colour, the tip says why
    p = prn(MEM, SERIOUS, PE, LUBE[0])
    html = run("nfBadgeHtml(%s, false)" % js(p))
    tip = "4 уведомления: пауза-ошибка 1, серьёзно 1, уведомление 2 · нужно решение"
    assert html == ('<button type="button" class="nf-badge pause" data-nf-open="b1" data-fk="badge-b1" aria-haspopup="dialog"'
                    ' aria-expanded="false" data-tip="%s — открыть список" aria-label="%s. Открыть список">⚠ 4</button>' % (tip, tip))
    assert 'aria-expanded="true"' in run("nfBadgeHtml(%s, true)" % js(p))
    assert run("nfBadgeHtml(%s, false, true)" % js(p)) == '<span class="nf-badge pause as-pill" data-tip="%s">⚠ 4</span>' % tip
    assert run("nfBadgeHtml(%s, false)" % js(prn())) == ""
    # IA §5 (plan step 4, deliberately changed): nothing to decide — the grey «ⓘ N», whatever the level
    one = run("nfBadgeHtml(%s, false, true)" % js(prn(rec("klipper:startup", severity="info"))))
    assert one == '<span class="nf-badge quiet as-pill" data-tip="1 уведомление: инфо 1 · решения не ждут">%s1</span>' % INFO_IC
    two = run("nfBadgeHtml(%s, false)" % js(prn(SERIOUS, MEM, state="printing")))
    assert 'class="nf-badge quiet"' in two and ">%s2</button>" % INFO_IC in two and "⚠" not in two
    assert 'data-tip="2 уведомления: серьёзно 1, уведомление 1 · решения не ждут — открыть список"' in two


def test_badge_loudness():
    # IA §5 / §10: loud only for «нужно решение» or a fatal younger than 30 min; the rest grey
    now = 1792252020.0
    fresh, old = dict(FATAL, since=now - 29 * 60), dict(FATAL, since=now - 31 * 60)
    assert run("NF_FRESH_S") == 1800

    def badge(p, t=now):
        return run("nfBadgeHtml(%s, false, false, %s)" % (js(p), js(t)))

    def loud(p, t=now):
        return run("[nfLoud(%s, %s), nfFreshFatal(%s, %s)]" % (js(p), js(t), js(p), js(t)))
    # C1's lidar: fresh — loud while it prints, then grey; the bell never counts it
    c1 = prn(fresh, SERIOUS, pid="c1", label="C1", state="printing")
    assert loud(c1) == [True, True]
    assert 'class="nf-badge fatal"' in badge(c1) and "· критичное за последние 30 мин" in badge(c1)
    assert loud(c1, now + 2 * 60) == [False, False] and 'class="nf-badge quiet"' in badge(c1, now + 2 * 60)
    assert 'class="nf-badge quiet"' in badge(prn(old, state="printing"))
    assert 'class="nf-bell calm"' in run("nfBellHtml(%s)" % js([c1]))
    # no time, no now, a hidden one or maintenance: never fresh
    for p, t in ((prn(dict(FATAL, since=None), state="printing"), now), (c1, None), (c1, "x"),
                 (prn(mute(fresh), SERIOUS, state="printing"), now),
                 (dict(c1, maintenance={"since": 1.0}), now)):
        assert loud(p, t)[1] is False, (p, t)
    # a decision wanted: loud whatever the age, the tip says why
    p3 = prn(PE, MEM, pid="p3", label="P3")
    assert loud(p3, None) == [True, False] and "· нужно решение" in badge(p3, None)
    held = prn(old, state="error")
    assert loud(held) == [True, False] and 'class="nf-badge fatal"' in badge(held)
    # the card asks with the clock; the list and the popover get it from nfOpts
    assert "${nfBadgeHtml(p, nfPopOpenFor(p.id), false, Date.now() / 1000)}" in _decl("renderPrinterCard")
    assert "now: Date.now() / 1000" in _decl("nfOpts")


def test_a_grey_badge_says_why_in_its_list():
    quiet = pop(prn(SERIOUS, MEM, label="P4", state="printing"))
    assert ('<div class="nf-sum"><b>Печать идёт</b> · серьёзно — 1, уведомление — 1 · <span class="nf-dim">решения не ждут — '
            'значок серый</span></div>') in quiet
    assert quiet.startswith('<div class="nf-pop-h"><span class="nf-badge quiet as-pill"')
    assert "nf-dim" not in pop(prn(PE, MEM, label="P3"))                     # loud: nothing to explain
    assert "nf-dim" not in pop(prn(state="printing"))                        # no records


def test_strip_only_for_fatal_and_pause():
    for items in ((SERIOUS, MEM), (CANCEL, SERIOUS), ()):
        assert run("nfStripHtml(%s, true)" % js(prn(*items))) == ""
    html = run("nfStripHtml(%s, true)" % js(prn(SERIOUS, PE, MEM)))
    assert html.startswith('<button type="button" class="nf-strip pause" data-nf-open="b1" data-fk="strip-b1" aria-haspopup="dialog"'
                           ' aria-label="Пауза-ошибка: 0500_8062 — Текст уведомления">')
    assert "<code>0500_8062</code> Текст уведомления" in html and "#i-pause" in html
    assert '<span class="sub">ещё 2 уведомления</span>' in html and html.endswith('<span class="go">Решить ›</span></button>')
    fatal = run("nfStripHtml(%s, true)" % js(prn(FATAL, PE)))
    assert 'class="nf-strip fatal"' in fatal and "<code>HMS_0C00_0100_0001_0004</code>" in fatal and "#i-alert" in fatal
    assert fatal.endswith("Подробнее ›</span></button>")             # the fatal HMS has no buttons


def test_strip_says_solve_only_when_the_viewer_gets_buttons():
    def go(p, admin):
        return re.search(r'<span class="go">([^<]*)</span>', run("nfStripHtml(%s, %s)" % (js(p), js(admin)))).group(1)
    assert go(prn(PE), True) == "Решить ›"
    assert go(prn(PE), False) == "Подробнее ›"                           # a viewer sees no buttons
    assert go(prn(PE, resume=False), True) == "Подробнее ›"              # print commands do not reach it
    assert go(prn(PE, grace=True), True) == "Подробнее ›"
    assert go(prn(rec("pe:0700_8011")), True) == "Подробнее ›"           # no buttons for this code
    klipper = notifications.build_klipper({"host_state": "shutdown", "host_message": "mcu 'mcu': Unable to connect"})
    kp = prn(*klipper, pid="k1", kind="klipper", state="error", model=None, resume=False)
    assert go(kp, True) == "Решить ›"                                    # Klipper: always, the restart asks first
    assert "Klipper остановлен (shutdown): mcu &#39;mcu&#39;: Unable to connect" in run("nfStripHtml(%s, true)" % js(kp))


def test_strip_subline_auto_and_forced_choice():
    auto = {"enabled": True, "tries": 1, "max": 3, "next_in_s": 75, "skip": None}
    html = run("nfStripHtml(%s, true)" % js(prn(dict(PE, auto=auto), MEM)))
    assert '<span class="sub">' in html and "автопропуск: попытка 1 из 3</span>" in html and "ещё 1" not in html
    req = run("nfStripHtml(%s, true)" % js(prn(STOP, MEM)))
    assert '<span class="sub req"><svg class="pc-ic" aria-hidden="true"><use href="#i-lock"></use></svg>выбор обязателен</span>' in req
    assert "ещё 1" not in req
    silent = run("nfStripHtml(%s, false)" % js(prn(rec("hms:0C00_0300_0001_0009", severity="fatal", text=None))))
    assert "Описание кода Bambu не публикует" in silent


# --- the list: items, buttons, confirm ---------------------------------------------------------

def item(p, n, st=None, opts=None, surf="pop"):
    return run("nfItemHtml(%s, %s, %s, %s, %s)" % (js(p), js(n), js(surf), js(st), js(opts or ADMIN)))


_BTN = re.compile(r'<button type="button" class="nf-btn([^"]*)" data-nf-act="([^"]*)" data-pid="([^"]*)" data-key="([^"]*)"'
                  r' data-fk="[^"]*"(?: data-tip="([^"]*)")?( aria-disabled="true")?>(<span class="spin-sm"></span>)?([^<]*)</button>')


def btns(html):
    """[(action, classes, tip, disabled, busy, label)]."""
    return [(a, cls.split(), tip or None, bool(dis), bool(spin), label)
            for cls, a, pid, key, tip, dis, spin, label in _BTN.findall(html)]


def test_item_buttons_only_for_an_admin_who_can_act():
    p = prn(PE)
    assert btns(item(p, PE)) == [("ignore", ["primary"], "Кнопка Bambu №27", False, False, "Игнорировать и продолжить"),
                                 ("resume", [], "Кнопка Bambu №28", False, False, "Устранено — продолжить")]
    assert 'data-pid="b1" data-key="pe:0500_8062"' in item(p, PE)
    for html in (item(p, PE, opts=VIEWER), item(prn(PE, resume=False), PE), item(prn(PE, online=False), PE),
                 item(p, PE, opts={"admin": True, "canAct": False})):
        assert btns(html) == [] and 'class="nf-wiki"' in html        # «Подробнее» stays for everyone
    assert btns(item(prn(PE, resume=False), PE, opts={"admin": True, "canAct": True}))   # the caller decides
    assert btns(item(prn(PE, kind="creality", resume=False), PE)) == []
    stop = btns(item(prn(STOP), STOP))
    assert [(a, cls) for a, cls, *_ in stop] == [("resume", ["primary"]), ("stop", ["danger"])]


def test_item_texts_hints_and_links():
    html = item(prn(PE), dict(PE, since=1792252020.0, hint="Подсказка"))
    assert html.startswith('<div class="nf-item pause"><div class="nf-top"><span class="nf-sev pause">Пауза-ошибка</span>'
                           '<code class="nf-code" data-tip="Источник: print_error (диалог на экране принтера)">0500_8062</code>'
                           '<span class="nf-since" data-tip="Когда появилось">с ')
    assert '<div class="nf-text">Текст уведомления</div>' in html and "#i-info" in html and "<span>Подсказка</span>" in html
    assert ('<a class="nf-wiki" href="https://e.bambulab.com/index.php?e=05008062" target="_blank" rel="noopener noreferrer"'
            in html) and "#i-ext" in html
    assert "nf-since" not in item(prn(PE), PE)                           # no time, no «с …»
    silent = item(prn(SILENT), SILENT)
    assert '<span class="nf-unk">Описание кода Bambu не публикует (в таблице пусто)</span>' in silent
    assert 'class="nf-item serious"' in silent and 'data-tip="Источник: HMS (MQTT print.hms[])">HMS_0500_0600_0002_0070' in silent
    lube = item(prn(LUBE[0]), LUBE[0])
    assert "#i-tool" in lube and notifications.LUBE_HINT in lube and 'class="nf-item common"' in lube
    assert "nf-acts" not in lube                                         # no buttons, no link: no row
    assert '<div class="nf-req">' in item(prn(STOP), STOP)
    for bad in ("javascript:alert(1)", "http://example.org/x", ""):
        assert "nf-wiki" not in item(prn(PE), dict(PE, wiki=bad))


def test_item_confirm_busy_done_and_answer():
    p = prn(STOP)
    html = item(p, STOP, {"confirm": "stop"})
    assert btns(html) == []
    assert ('<div class="pc-confirm" role="group" aria-label="Подтверждение"><span class="q">'
            '<svg class="pc-ic" aria-hidden="true"><use href="#i-stop"></use></svg>Остановить печать? Задание будет потеряно.</span>'
            '<button type="button" class="cf-stop" data-nf-act="stop" data-pid="b1" data-key="pe:0300_8007" data-yes="1"'
            ' data-fk="yes-b1|pe:0300_8007|pop">Стоп</button>'
            '<button type="button" class="cf-cancel" data-nf-cancel="b1|pe:0300_8007" data-fk="no-b1|pe:0300_8007|pop">Отмена</button></div>'
            ) in html
    # a confirmation for a button that is not there (any more), or for a viewer: the row as usual
    assert btns(item(p, STOP, {"confirm": "ams_abort"}))
    assert "pc-confirm" not in item(p, STOP, {"confirm": "stop"}, VIEWER)
    klipper = notifications.build_klipper({"host_state": "error", "host_message": "x"})[0]
    kp = prn(klipper, pid="k1", kind="klipper", state="error", resume=False)
    ask = item(kp, klipper, {"confirm": "klippy_restart"})
    assert 'class="cf-go" data-nf-act="klippy_restart"' in ask and ">Перезапустить</button>" in ask and "Перезапустить Klipper?" in ask
    busy = btns(item(p, STOP, {"busy": "resume", "line": {"k": "wait", "t": "Продолжить печать — ждём принтер…"}}))
    assert [(a, dis, spin) for a, _, _, dis, spin, _ in busy] == [("resume", True, True), ("stop", True, False)]
    assert all(dis for _, _, _, dis, _, _ in btns(item(p, STOP, {"done": True})))
    bad = item(p, STOP, {"line": {"k": "bad", "t": "Отказ: Принтер уже не на паузе"}})
    assert bad.endswith('<div class="pc-cmd bad" role="status"><span class="lead">✕</span><span class="t">Отказ: Принтер уже не на паузе'
                        '</span><button type="button" class="x" data-nf-dismiss="b1|pe:0300_8007" aria-label="Закрыть">×</button></div></div>')
    assert 'data-fk="yes-b1|pe:0300_8007|farm"' in item(p, STOP, {"confirm": "stop"}, surf="farm")


def test_routine_notes_fold_under_more():
    p = prn(LUBE[0], PE, MEM, LUBE[1], SERIOUS)
    html = run("nfListHtml(%s, 'pop', {}, {}, %s)" % (js(p), js(ADMIN)))
    assert html.count('<div class="nf-item ') == 2 and html.index("0500_8062") < html.index("0300_1100_0002_0001")
    more = re.search(r'<button type="button" class="nf-more" data-nf-more="pop\|b1" aria-expanded="false" data-fk="more-pop\|b1">'
                     r'<span>([^<]*)</span><span class="codes">([^<]*)</span>', html)
    assert more.groups() == ("ещё 3 плановых", "0501_0400_0003_0002 · 0500_0100_0003_0015 · 0501_0400_0003_0004")
    assert "nf-coms" not in html
    opened = run("nfListHtml(%s, 'pop', {}, {'pop|b1': true}, %s)" % (js(p), js(ADMIN)))
    assert 'aria-expanded="true"' in opened and "<span>Свернуть плановые</span>" in opened and "codes" not in opened
    assert opened.count('<div class="nf-item ') == 5 and '<div class="nf-coms">' in opened
    assert 'aria-expanded="false"' in run("nfListHtml(%s, 'farm', {}, {'pop|b1': true}, %s)" % (js(p), js(ADMIN)))
    # nothing but routine notes: the first one stays in view
    only = run("nfListHtml(%s, 'pop', {}, {}, %s)" % (js(prn(*LUBE, MEM)), js(ADMIN)))
    assert only.count('<div class="nf-item ') == 1 and "ещё 2 плановых" in only
    single = run("nfListHtml(%s, 'pop', {}, {}, %s)" % (js(prn(MEM)), js(ADMIN)))
    assert single.count('<div class="nf-item ') == 1 and "nf-more" not in single
    assert "<span>ещё 1 плановое</span>" in run("nfListHtml(%s, 'pop', {}, {}, %s)" % (js(prn(PE, MEM)), js(ADMIN)))


def test_list_reads_each_records_state():
    p = prn(PE, STOP)
    ui = {"b1|pe:0300_8007": {"confirm": "stop"}, "b2|pe:0500_8062": {"busy": "ignore"}}
    html = run("nfListHtml(%s, 'pop', %s, {}, %s)" % (js(p), js(ui), js(ADMIN)))
    assert html.count("pc-confirm") == 1 and "spin-sm" not in html     # another printer's state is not this one's


# --- popover, farm list, bell ----------------------------------------------------------------

STATUS = "function (p) { return {cls: p.state, label: p.state.toUpperCase()}; }"


def pop(p, ui=None, lines=None, opts=None):
    o = dict(opts or ADMIN, lines=lines or {})
    return run("nfPopPrinterHtml(%s, %s, {}, Object.assign(%s, {status: %s}))" % (js(p), js(ui or {}), js(o), STATUS))


def test_printer_popover():
    html = pop(prn(PE, MEM, label="P3"))
    assert html.startswith('<div class="nf-pop-h"><span class="nf-badge pause as-pill"')
    assert ('<div class="nf-pop-ttl"><b id="nfPopTitle">P3</b><span class="bd-model">P2S</span></div>'
            '<span class="status-badge paused">PAUSED</span>'
            '<button type="button" class="nf-x" data-nf-close data-fk="close" aria-label="Закрыть">×</button></div>') in html
    assert '<div class="nf-sum"><b>Печать стоит и ждёт решения</b> · пауза-ошибка — 1, уведомление — 1</div>' in html
    assert html.endswith('<div class="nf-foot">Тексты — из таблицы Bambu (ru), кнопки — из таблицы действий Bambu для P2S (22E),'
                         ' ответы — подтверждение принтера по MQTT.</div>')
    assert "nf-line" not in html
    empty = pop(prn(state="printing"))
    assert '<div class="nf-body"><div class="nf-empty">✓ Уведомлений нет</div></div>' in empty and "nf-badge" not in empty
    assert "<b>Печать идёт</b></div>" in empty
    no_status = run("nfPopPrinterHtml(%s, {}, {}, {})" % js(prn(PE)))
    assert "status-badge" not in no_status and "nf-btn" not in no_status   # no opts: a viewer
    kl = pop(prn(*notifications.build_klipper({"host_state": "shutdown"}), kind="klipper", state="error", model=None))
    assert "Источник — Moonraker" in kl and 'class="bd-model">KLIPPER<' in kl and "<b>Печать остановлена</b>" in kl
    assert "(" not in re.search(r'<div class="nf-foot">([^<]*)', pop(prn(PE, model="K2"))).group(1).split("K2")[1][:3]


def test_popover_answer_line():
    line = {"k": "bad", "t": "Игнорировать и продолжить — отказ: Принтер уже не на паузе"}
    html = pop(prn(PE), lines={"b1": line})
    assert ('<div class="nf-line"><div class="pc-cmd bad" role="status"><span class="lead">✕</span><span class="t">'
            'Игнорировать и продолжить — отказ: Принтер уже не на паузе</span><button type="button" class="x"'
            ' data-nf-dismiss-card="b1" aria-label="Закрыть">×</button></div></div>') in html
    # the record shows its own answer: the printer's line steps aside
    own = pop(prn(PE), ui={"b1|pe:0500_8062": {"line": {"k": "ok", "t": "x — принято принтером"}}}, lines={"b1": line})
    assert "nf-line" not in own and "x — принято принтером" in own


def _farm_printers():
    return [prn(SERIOUS, pid="h1", label="H1", state="printing"),
            prn(PE, MEM, pid="p3", label="P3"),
            prn(FATAL, pid="c1", label="C1", state="printing", resume=False),
            prn(STOP, pid="p10", label="P10"),
            prn(STOP, pid="p2", label="P2"),
            prn(pid="p0", label="P0", state="printing")]


def farm(printers, open_other=False, opts="{admin: true}"):
    return run("nfPopFarmHtml(%s, {}, %s, Object.assign(%s, {status: %s}))"
               % (js(printers), js({"farm-other": True} if open_other else {}), opts, STATUS))


def test_farm_list_needs_a_decision_then_the_rest():
    # IA §5 (plan step 4, deliberately changed): «Нужно решение · N» — the bell's printers by name
    # (P2 < P10), with the records that want the decision; «Прочие» folded under it
    html = farm(_farm_printers())
    assert re.findall(r'data-nf-goto="([^"]*)"', html) == ["p2", "p3", "p10"]
    assert html.index('<div class="nf-sec">Нужно решение · 3</div>') < html.index('<section class="nf-grp"')
    grp = html.split('<section class="nf-grp" aria-label="P3">')[1].split("</section>")[0]
    assert '<span class="status-badge paused">PAUSED</span><span class="nf-grp-go">к карточке ›</span></button>' in grp
    assert 'data-fk="act-ignore-p3|pe:0500_8062|farm"' in grp                 # buttons per printer
    assert grp.count('<div class="nf-item ') == 1 and "HMS_0500_0100" not in grp   # the decision only, not the memory note
    assert "nf-btn" not in farm(_farm_printers(), True, "{admin: false}")
    assert ('<button type="button" class="nf-other" data-nf-more="farm-other" aria-expanded="false" data-fk="more-farm-other">'
            '<span>Прочие · 2 на 2 принтерах</span><span class="nf-dim">долгие HMS, плановое</span>') in html
    assert "nf-orow" not in html and "nf-sum" not in html and "nf-sevrow" not in html
    # unfolded: a line per printer, its badge and worst record; it leads to the card
    opened = farm(_farm_printers(), True)
    assert 'aria-expanded="true"' in opened
    rows = opened.split('<div class="nf-orows">')[1]
    assert re.findall(r'<button type="button" class="nf-orow" data-nf-goto="([^"]*)" data-fk="goto-([^"]*)">', rows) == [
        ("c1", "c1"), ("h1", "h1")]                                          # the worst level first
    assert '<span class="nf-badge quiet as-pill"' in rows and '<b>C1</b><span class="t">Текст уведомления</span>' in rows
    assert "nf-btn" not in rows and "nf-item" not in rows
    # a fresh fatal is loud in «Прочие» too and goes first there
    fresh = [dict(p, notifications=[dict(FATAL, since=1000.0)]) if p["id"] == "h1" else p for p in _farm_printers()]
    rows = run("nfPopFarmHtml(%s, {}, {'farm-other': true}, {admin: true, now: 1100})" % js(fresh)).split('<div class="nf-orows">')[1]
    assert re.findall(r'class="nf-orow" data-nf-goto="([^"]*)"', rows) == ["h1", "c1"] and "nf-badge fatal as-pill" in rows
    # nothing to decide: said so, the rest still folded below
    calm = farm([p for p in _farm_printers() if p["id"] in ("h1", "c1", "p0")])
    assert ('<div class="nf-sec">Нужно решение · 0</div><div class="nf-body"><div class="nf-empty">✓ Решений не требуется</div>'
            '</div>') in calm
    assert "Прочие · 2 на 2 принтерах" in calm
    only = farm([p for p in _farm_printers() if p["id"] in ("p2", "p0")])
    assert "nf-other" not in only                                            # no «Прочие» without them
    empty = run("nfPopFarmHtml(%s, {}, {}, {})" % js([prn(), prn(pid="b2")]))
    assert empty.endswith('<div class="nf-body"><div class="nf-empty">✓ На ферме уведомлений нет</div></div>')
    assert "nf-sec" not in empty


def test_reset_errors_is_at_the_foot_of_the_bell_list():
    # IA §5 (plan step 4): an admin's «Сбросить ошибки (N)» moved from the fleet bar to the foot of
    # the bell's list — only when there is something to reset; the same dry-run dialog opens
    ts = 1792252020.0
    printers = _farm_printers() + [prn(dict(CANCEL, since=ts), pid="p4", label="P4", state="cancelled"),
                                   prn(CANCEL, pid="p5", label="P5", state="cancelled")]
    html = farm(printers)
    foot = ('<div class="nf-pop-foot"><button type="button" class="fb-btn reset" data-nf-reset data-fk="reset" data-tip="'
            'Закрыть диалоги «Задача отменена» на экранах — сначала покажет, кого заденет"><svg class="pc-ic" aria-hidden="true">'
            '<use href="#i-reset"></use></svg>Сбросить ошибки (2)</button></div>')
    assert html.endswith(foot)
    assert run("nfResetSplit(%s).go.length" % js(printers)) == 2               # the dialog's own count
    assert "data-nf-reset" not in farm(printers, opts="{admin: false}")        # a viewer: never
    assert "data-nf-reset" not in farm(_farm_printers())                       # nothing to reset: no button
    # all hidden: no list, still the button (a hidden «Задача отменена» is still on the screen)
    hidden = [prn(mute(CANCEL), pid="p4", label="P4", state="cancelled")]
    assert farm(hidden).endswith("Сбросить ошибки (1)</button></div>") and "На ферме уведомлений нет" in farm(hidden)
    # the DOM layer: the button opens the dialog, which closes the list; closing it focuses the bell
    assert 'else if ((el = t.closest("[data-nf-reset]"))) {\n          openResetDialog();' in _decl("onNfPopClick")
    assert "nfClose(false);" in _decl("openResetDialog")
    assert 'action === "reset_errors" ? document.getElementById("nfBell")' in _decl("closeBulkDialog")
    css = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)
    assert re.search(r"\.nf-pop-foot \{[^}]*position: sticky; bottom: 0;[^}]*background: var\(--bg-elev\)", css)


def test_bell_counts_only_printers_that_need_a_decision():
    printers = [prn(PE), prn(FATAL, pid="b2"), prn(SERIOUS, MEM, pid="b3"), prn(CANCEL, pid="b4"), prn(pid="b5")]
    html = run("nfBellHtml(%s, false)" % js(printers))
    label = "Уведомления фермы: 2 принтера требуют внимания"
    assert html == ('<button type="button" class="nf-bell" id="nfBell" data-nf-bell data-fk="bell" aria-haspopup="dialog"'
                    ' aria-expanded="false" data-tip="%s" aria-label="%s"><svg class="pc-ic" aria-hidden="true"><use href="#i-bell"></use>'
                    '</svg><span class="nf-badge fatal as-pill">⚠ 2</span></button>' % (label, label))
    calm = run("nfBellHtml(%s, true)" % js(printers[2:]))
    assert 'class="nf-bell calm"' in calm and 'aria-label="Уведомления фермы: Срочных уведомлений нет"' in calm
    assert 'aria-expanded="true"' in calm and "nf-badge" not in calm
    assert "1 принтер требует внимания" in run("nfBellHtml(%s)" % js(printers[:1]))
    assert 'class="nf-badge pause as-pill">⚠ 1' in run("nfBellHtml(%s)" % js(printers[:1]))


def test_a_printer_in_maintenance_wants_no_attention_badge_strip_or_bell():
    # spec_maintenance §7 / §10.4: its records are still sent (debug), nothing shows them
    maint = {"since": 1000.0, "note": None, "by": "admin"}
    m = dict(prn(PE, MEM, pid="m1", label="M1"), maintenance=maint)
    f = dict(prn(FATAL, pid="m2", label="M2", state="error"), maintenance=maint)
    plain = prn(STOP, pid="p2", label="P2")
    assert run("[nfAttention(%s), nfUrgency(%s), nfAttention(%s)]" % (js(m), js(f), js(plain))) == [False, 0, True]
    assert run("[nfBadgeHtml(%s, false), nfBadgeHtml(%s, false, true), nfStripHtml(%s, true)]" % (js(m), js(m), js(f))) == [
        "", "", ""]
    assert "nf-strip" in run("nfStripHtml(%s, true)" % js(dict(m, maintenance=None)))   # out of the mode: as before
    bell = run("nfBellHtml(%s, false)" % js([m, f, plain]))
    assert "1 принтер требует внимания" in bell and 'nf-badge pause as-pill">⚠ 1' in bell
    farm = run("nfPopFarmHtml(%s, {}, {'farm-other': true}, {admin: true})" % js([m, f, plain]))
    assert re.findall(r'data-nf-goto="([^"]*)"', farm) == ["p2"]
    assert '<div class="nf-sec">Нужно решение · 1</div>' in farm and "nf-other" not in farm
    empty = run("nfPopFarmHtml(%s, {}, {}, {})" % js([m, f]))
    assert "На ферме уведомлений нет" in empty
    assert run("nfBellHtml(%s, false)" % js([m, f])).startswith('<button type="button" class="nf-bell calm"')


def test_no_browser_push_for_a_printer_in_maintenance():
    # spec_maintenance F14: the push is held back, the state is still tracked (after the
    # mode only a change from then on notifies)
    load = JS[JS.index("const settings = loadNotificationSettings();"):]
    load = load[:load.index("renderLiveChip(true);")]   # the end of load()'s success path
    guard = "if (prev && prev !== p.state && !inMaint(p)) {"
    assert load.count(guard) == 1 and load.count("showBrowserNotification(") == 3
    assert load.index("prevPrinterStates[p.id] = p.state;") > load.rindex("showBrowserNotification(")


# --- texts: auto-ignore, answers, card lines ---------------------------------------------------

@pytest.mark.parametrize("auto,want", [
    (None, None),
    ({"enabled": False, "tries": 1, "max": 3}, None),
    ({"enabled": True, "tries": 0, "max": 3, "next_in_s": None, "skip": None},
     {"t": "Автопропуск включён · ждём первой попытки", "s": "автопропуск: ждём первой попытки"}),
    ({"enabled": True, "tries": 1, "max": 3, "next_in_s": 75, "skip": None},
     {"t": "Автопропуск включён · попытка 1 из 3 · следующая через 2 мин", "s": "автопропуск: попытка 1 из 3"}),
    ({"enabled": True, "tries": 2, "max": 3, "next_in_s": 0, "skip": None},
     {"t": "Автопропуск включён · попытка 2 из 3 · следующая сейчас", "s": "автопропуск: попытка 2 из 3"}),
    ({"enabled": True, "tries": 2, "max": 3, "next_in_s": None, "skip": None},
     {"t": "Автопропуск включён · попытка 2 из 3", "s": "автопропуск: попытка 2 из 3"}),
    ({"enabled": True, "tries": 3, "max": 3, "next_in_s": None, "skip": None},
     {"t": "Автопропуск: 3 попытки не помогли — решите вручную", "s": "автопропуск не помог"}),
    ({"enabled": True, "tries": 0, "max": 3, "next_in_s": None, "skip": "tries_used"},
     {"t": "Автопропуск: 3 попытки не помогли — решите вручную", "s": "автопропуск не помог"}),
    ({"enabled": True, "tries": 0, "max": 3, "next_in_s": None, "skip": "no_job_id"},
     {"t": "Автопропуск не сработает: в отчёте нет номера задания", "s": "автопропуск не сработает"}),
    ({"enabled": True, "tries": 0, "max": 3, "next_in_s": None, "skip": "maintenance"},
     {"t": "Автопропуск выключен: принтер на обслуживании", "s": "автопропуск выключен"}),
])
def test_auto_text(auto, want):
    assert run("nfAutoText(%s)" % js(auto)) == want


def test_auto_text_follows_the_server_view():
    from app.services import auto_ignore
    auto_ignore._tries.clear()
    try:
        auto_ignore._tries[("b1", "0", 0x05008062)] = (1, 1000.0)
        view = auto_ignore.view("b1", 0x05008062, now=1030.0)
        assert run("nfAutoText(%s).t" % js(view)) == "Автопропуск включён · попытка 1 из 3 · следующая через 2 мин"
    finally:
        auto_ignore._tries.clear()


def test_auto_text_of_a_printer_in_maintenance_follows_the_server_view():
    from app.services import auto_ignore

    class _St:
        def to_dict(self):
            return {"id": "b1", "label": "P1", "kind": "bambu", "state": "paused", "online": True}

    class _Store:
        def get_all(self):
            return [_St()]

    auto_ignore._tries.clear()
    auto_ignore._skips.clear()
    try:
        ctx = {"gcode_state": "PAUSE", "err": 0x05008062, "job_id": "0"}
        auto_ignore.check_once(_Store(), ["0500_8062"], lambda pid: ctx, lambda pid, err: {}, lambda a: None,
                               now=1000.0, maint=lambda pid: True)
        view = auto_ignore.view("b1", 0x05008062, now=1030.0)
        assert run("nfAutoText(%s).t" % js(view)) == "Автопропуск выключен: принтер на обслуживании"
    finally:
        auto_ignore._tries.clear()
        auto_ignore._skips.clear()


def test_action_lines():
    lost = run("NF_NET_LOST")
    cases = [({"status": "confirmed", "detail": "подтверждено принтером"}, False),
             ({"status": "sent", "detail": "ответа нет"}, False),
             ({"status": "sent", "detail": lost}, False),
             ({"status": "refused", "detail": "Принтер уже не на паузе"}, False),
             ({"status": "rejected", "detail": ""}, False),
             ({"status": "refused", "detail": "Диалог уже закрыт"}, True),
             ({"status": "confirmed"}, True)]
    got = run("%s.map(function (c) { return nfActLine('Закрыть диалог', c[0], c[1]); })" % js(cases))
    assert got == [{"k": "ok", "t": "Закрыть диалог — принято принтером"},
                   {"k": "unk", "t": "Закрыть диалог — отправлено, ответа нет"},
                   {"k": "unk", "t": "Закрыть диалог — " + lost},
                   {"k": "bad", "t": "Отказ: Принтер уже не на паузе"},
                   {"k": "bad", "t": "Отказ"},
                   {"k": "bad", "t": "Закрыть диалог — отказ: Диалог уже закрыт"},
                   {"k": "ok", "t": "Закрыть диалог — принято принтером"}]
    assert run("nfWaitLine('Стоп')") == {"k": "wait", "t": "Стоп — ждём принтер…"}


def test_cancel_text_top_text_and_pruned_state():
    ts = 1792252020.0
    hhmm = datetime.fromtimestamp(ts).strftime("%H:%M")
    assert run("nfCancelText(%s)" % js(prn(dict(CANCEL, since=ts), MEM, state="cancelled", pct=23.4))) == (
        "Задание отменено в %s на 23%%" % hhmm)
    assert run("nfCancelText(%s)" % js(prn(MEM, state="cancelled", pct=0))) == "Задание отменено на 0%"
    assert run("nfCancelText(%s)" % js(prn(state="cancelled"))) == "Задание отменено"
    echo2 = rec("pe:0500_400E", severity="common", actions=("clean",), since=ts)
    assert run("nfCancelText(%s)" % js(prn(echo2, state="cancelled"))) == "Задание отменено в " + hhmm
    assert run("nfTopText(%s)" % js(prn(MEM, SERIOUS, PE))) == "0500_8062 — Текст уведомления"
    assert run("nfTopText(%s)" % js(prn(MEM, SILENT))) == "HMS_0500_0600_0002_0070"
    assert run("nfTopText(%s)" % js(prn(MEM, *LUBE))) == ""                 # routine notes only: nothing to say
    ui = {"b1|pe:0500_8062": {"busy": "ignore"}, "b1|pe:0300_8007": {"done": True}, "b2|hms:x": {"line": {}}}
    assert run("nfPruneUi(%s, %s)" % (js(ui), js([prn(PE), prn(pid="b2")]))) == {"b1|pe:0500_8062": {"busy": "ignore"}}
    assert run("nfPruneUi(null, null)") == {}


def test_cancel_text_of_a_creality_row():
    # a K1C's state 4 after a cancel (creality_client STATE_CODES) reaches the card as
    # cancelled: no cancel echo (Bambu's pe codes) to time it, the progress it stopped at
    from app.clients.creality_client import CrealityK1Client
    from app.services.normalizer import normalize_ws_dict

    def row(*frames):
        c = CrealityK1Client("host", "K1")
        c.connected = True
        for f in frames:
            c.on_message(None, js(f))
        st = normalize_ws_dict("c1", "K1", PrinterKind.CREALITY, c.get_data(), device_type="k1c")
        return prn(*st.notifications, pid="c1", label="K1", kind="creality", model="k1c", state=st.state.value,
                   pct=st.progress_pct)

    start = {"state": 1, "printJobTime": 0, "printLeftTime": 0, "err": {"errcode": 0, "key": 0}}
    stopped = row(start, {"state": 4})
    assert stopped["state"] == "cancelled"
    assert run("nfCancelText(%s)" % js(stopped)) == "Задание отменено на 0%"
    # the client's progress = printJobTime / (printJobTime + printLeftTime)
    stopped = row(dict(start, printJobTime=300, printLeftTime=500), {"state": 4})
    assert stopped["state"] == "cancelled" and run("nfCancelText(%s)" % js(stopped)) == "Задание отменено на 37%"
    # a stopped printer with an error is ERROR (no cancel line), as before state 4 was mapped
    bad = row(start, {"state": 4, "err": {"errcode": 2, "key": 5}})
    assert bad["state"] == "error" and [n["key"] for n in bad["notifications"]] == ["creality:2/5"]


def test_server_and_device_strings_are_escaped():
    evil = '<img src=x onerror=alert(1)>"\''
    n = rec('pe:0500_8062', text=evil, hint=evil, actions=(("ignore", 27),))
    n["actions"][0]["label"] = evil
    p = prn(n, pid='b"1', label=evil, model=evil)
    out = run("[nfStripHtml(%s, true), nfItemHtml(%s, %s, 'pop', {}, {admin: true}), nfBadgeHtml(%s, false),"
              " nfPopPrinterHtml(%s, {}, {}, {admin: true, status: function () { return {cls: %s, label: %s}; }}),"
              " nfPopFarmHtml([%s], {}, {}, {admin: true}), nfResetDialogHtml(nfResetDialog([%s]))]"
              % (js(p), js(p), js(n), js(p), js(p), js(evil), js(evil), js(p), js(dict(p, notifications=[CANCEL]))))
    for html in out:
        assert "<img" not in html and "onerror=alert(1)>" not in html
        for attr in re.findall(r'(?:data-tip|aria-label|data-nf-open|data-pid|data-id|class)="([^"]*)"', html):
            assert "<" not in attr and "'" not in attr
    assert 'data-nf-open="b&quot;1"' in out[0] and 'aria-label="Пауза-ошибка: 0500_8062 — &lt;img' in out[0]


# --- «Сбросить ошибки» ------------------------------------------------------------------------

@pytest.fixture
def fleet(monkeypatch):
    from app.services import auto_ignore
    monkeypatch.setattr(settings_service, "get", lambda key, **k: [] if key == "printer_light_disabled" else {})
    monkeypatch.setattr(settings_service, "_cache", {"auto_ignore_print_errors": ["0500_8062"]})
    monkeypatch.setattr(printer_commands, "_capability", {
        pid: {"print_cmds": pid != "b-blocked", "via": "fun", "ts": 0}
        for pid in ("b-cancel", "b-pause", "b-auto", "b-auto3", "b-noid", "b-forced", "b-forced2", "b-pe", "b-fatal",
                    "b-hms", "b-empty", "b-off", "b-grace", "b-blocked", "b-cancel-hms")})
    auto_ignore._tries.clear()
    auto_ignore._skips.clear()
    yield
    auto_ignore._tries.clear()
    auto_ignore._skips.clear()


def _status(pid, *records, kind=PrinterKind.BAMBU, state=PrinterState.PAUSED, online=True, grace=False):
    return PrinterStatus(id=pid, label=pid, kind=kind, online=online, state=state if online else PrinterState.OFFLINE,
                         grace_period_active=grace, device_type="P2S", notifications=[dict(n) for n in records])


def test_reset_skip_reasons_match_the_server(fleet):
    from app.services import auto_ignore
    from app.services.normalizer import normalize_ws_dict

    def klipper(pid, webhooks):
        return normalize_ws_dict(pid, pid, PrinterKind.KLIPPER, {"state": "standby", "debug": {"webhooks_state": webhooks}})

    auto_ignore._tries[("b-auto3", "0", 0x05008062)] = (3, 1000.0)
    auto_ignore._skips[("b-noid", "None", 0x05008062, "no_job_id")] = 1000.0
    statuses = [
        _status("b-cancel", CANCEL, state=PrinterState.CANCELLED),
        _status("b-cancel-hms", CANCEL, SERIOUS, MEM, state=PrinterState.CANCELLED),
        _status("b-pause", rec("pe:0700_8011", actions=(("resume", 4),))),
        _status("b-auto", PE), _status("b-auto3", PE), _status("b-noid", PE),
        _status("b-forced", STOP),
        _status("b-forced2", rec("pe:0300_C001", severity="common", actions=("clean",), blocking=True)),
        _status("b-pe", rec("pe:0300_4000", severity="fatal"), state=PrinterState.ERROR),
        _status("b-fatal", FATAL, SERIOUS, state=PrinterState.PRINTING),
        _status("b-hms", SERIOUS, MEM, state=PrinterState.PRINTING),
        _status("b-empty", state=PrinterState.IDLE),
        _status("b-off", CANCEL, online=False), _status("b-grace", CANCEL, grace=True),
        _status("b-blocked", CANCEL), _status("b-new", CANCEL),
        klipper("k-down", "shutdown"), klipper("k-gone", "disconnected"), klipper("k-ok", "ready"),
        PrinterStatus(id="c-err", label="c", kind=PrinterKind.CREALITY, online=True, state=PrinterState.ERROR,
                      notifications=[rec("creality:2/5", severity="fatal")]),
        PrinterStatus(id="c-ok", label="c", kind=PrinterKind.CREALITY, online=True, state=PrinterState.PRINTING),
        PrinterStatus(id="m-1", label="m", kind=PrinterKind.MKS, online=True, state=PrinterState.PRINTING),
    ]
    rows, want = [], {}
    for st in statuses:
        d = st.to_dict()
        caps, note = printer_commands.caps_for(d)
        for n in d["notifications"]:                                   # as /api/printers adds it
            auto = na.auto_state(d["id"], n)
            if auto is not None:
                n["auto"] = auto
        rows.append(dict(d, caps=caps, caps_note=note))
        want[st.id] = na.reset_skip_reason(d, caps, note)
    got = dict(run("%s.map(function (p) { return [p.id, nfResetSkipWhy(p)]; })" % js(rows)))
    assert got == want
    # every branch of the server's rule is met above
    assert want["b-cancel"] is None and want["b-cancel-hms"] is None
    assert want["b-auto"] == na.RESET_PAUSE % "0500_8062" + na.RESET_AUTO_TAIL
    assert want["b-auto3"] == want["b-noid"] == na.RESET_PAUSE % "0500_8062"
    assert {want[k] for k in ("b-off", "b-grace", "b-blocked", "b-new", "b-empty", "k-down", "k-gone", "k-ok", "c-err",
                              "c-ok", "m-1", "b-forced", "b-forced2", "b-pe", "b-fatal", "b-hms", "b-pause")} >= {
        na.RESET_KLIPPER, na.RESET_KLIPPER_NONE, na.RESET_CREALITY, na.RESET_EMPTY, na.RESET_HMS_ONLY, na.RESET_HMS_FATAL,
        na.RESET_PE % "0300_4000", na.RESET_BLOCKING % "0300_C001", na.RESET_BLOCKING_TEXT["0300_8007"],
        fleet_commands.MSG_OFFLINE, fleet_commands.MSG_GRACE, printer_commands.NOTE_NOT_WIRED}
    # and the record it closes is the server's
    for d in rows:
        server = na.resettable(d["notifications"])
        assert run("(nfResettable(%s) || {}).key || null" % js(d)) == (server["key"] if server else None)


def test_reset_skip_reason_of_a_printer_in_maintenance_matches_the_server(fleet):
    # spec_maintenance §10.3: «на обслуживании» before the reset's own rule (fleet_commands.skip_reason)
    maint = {"since": 1000.0, "note": None, "by": "admin"}
    statuses = [_status("b-cancel", CANCEL, state=PrinterState.CANCELLED), _status("b-off", CANCEL, online=False),
                _status("b-pause", rec("pe:0700_8011", actions=(("resume", 4),))), _status("b-empty", state=PrinterState.IDLE)]
    rows, want = [], {}
    for st in statuses:
        d = st.to_dict()
        caps, note = printer_commands.caps_for(d)
        for m in (None, maint):
            row = dict(d, id=d["id"] + ("-m" if m else ""), maintenance=m)
            rows.append(dict(row, caps=caps, caps_note=note))
            want[row["id"]] = fleet_commands.skip_reason("reset_errors", row, caps, note)
    got = dict(run("%s.map(function (p) { return [p.id, nfResetSkipWhy(p)]; })" % js(rows)))
    assert got == want
    assert {want[k] for k in want if k.endswith("-m")} == {fleet_commands.MSG_MAINT}
    assert want["b-cancel"] is None                                      # out of maintenance: reset as before
    # the dialog's split: into «Не попадут» with the reason
    out = run("nfResetSplit(%s)" % js(rows))
    assert [x["p"]["id"] for x in out["go"]] == ["b-cancel"]
    assert {x["p"]["id"]: x["why"] for x in out["skip"]}["b-cancel-m"] == "на обслуживании"


def test_reset_split_dialog_and_revalidation():
    ts = 1792252020.0
    printers = [prn(dict(CANCEL, since=ts), SERIOUS, MEM, pid="p4", label="P4", state="cancelled"),
                prn(dict(CANCEL, since=ts), pid="p10", label="P10", state="cancelled", model="X1C"),
                prn(PE, pid="p3", label="P3"),
                prn(pid="p0", label="P0", state="printing"),
                prn(CANCEL, pid="p1", label="P1", state="cancelled", online=False)]
    split = run("(function (s) { return {go: s.go.map(function (x) { return [x.p.id, x.n.key]; }),"
                " skip: s.skip.map(function (x) { return [x.p.id, x.why]; })}; })(nfResetSplit(%s))" % js(printers))
    assert split == {"go": [["p4", "pe:0300_400C"], ["p10", "pe:0300_400C"]],
                     "skip": [["p0", "уведомлений нет"], ["p1", "офлайн"],
                              ["p3", "пауза-ошибка 0500_8062 — решается кнопками в уведомлении"]]}
    dlg = run("nfResetDialog(%s)" % js(printers))
    assert dlg["action"] == "reset_errors" and dlg["step"] == 1 and dlg["sel"] == {"p4": True, "p10": True}
    assert dlg["go"][0] == {"id": "p4", "label": "P4", "model": "P2S", "key": "pe:0300_400C", "code": "0300_400C",
                            "severity": "common", "text": "Задача отменена.", "since": ts, "hms": 2}
    assert dlg["go"][1]["model"] == "X1C" and dlg["go"][1]["hms"] == 0
    html = run("nfResetDialogHtml(%s)" % js(dlg))
    since = datetime.fromtimestamp(ts).strftime("с %d.%m %H:%M")
    assert '<div class="bd-ic reset">' in html and '<h3 id="bdTitle">Сбросить ошибки на 2 принтерах?</h3>' in html
    assert ('<label class="bd-row"><input type="checkbox" data-bd="toggle" data-id="p4" checked><span class="bd-l1">'
            '<span class="bd-name">P4</span><span class="bd-model">P2S</span></span><span class="nf-sev common">Уведомление</span>'
            '<span class="bd-job"><span>0300_400C · Задача отменена. · %s</span></span><span class="bd-eta">HMS останутся: 2</span></label>'
            % since) in html
    assert '<span class="bd-eta"></span></label>' in html                 # P10: nothing stays
    assert "<span>Попадут · 2</span>" in html and ">Снять все</button>" in html and "<span>Не попадут · 3</span>" in html
    assert "<b>P3</b><span>P2S — пауза-ошибка 0500_8062 — решается кнопками в уведомлении</span>" in html
    assert html.endswith('<button type="button" class="bd-btn go" data-bd="go"><svg class="pc-ic" aria-hidden="true">'
                         '<use href="#i-reset"></use></svg>Сбросить выбранные (2)</button></div></div>')
    dlg["sel"] = {"p4": True}
    one = run("nfResetDialogHtml(%s)" % js(dlg))
    assert "на 1 принтере?" in one and ">Выбрать все</button>" in one and 'class="bd-row unchecked"' in one
    dlg["sel"] = {}
    assert 'data-bd="go" aria-disabled="true"' in run("nfResetDialogHtml(%s)" % js(dlg))
    # nothing to reset: no «Выбрать все»
    assert "bd-link" not in run("nfResetDialogHtml(nfResetDialog(%s))" % js(printers[2:]))

    # «Сбросить выбранные»: the latest printers decide
    dlg["sel"] = {"p4": True, "p10": True}
    latest = [printers[0], prn(pid="p10", label="P10", state="idle")]       # P10 was closed on its screen
    rv = run("nfResetRevalidate(%s, %s)" % (js(dlg), js(latest)))
    assert rv["ids"] == ["p4"]
    assert [(r["id"], r["status"], r["detail"]) for r in rv["rows"]] == [
        ("p4", "wait", ""), ("p10", "skipped", "состояние изменилось, не отправлено")]
    other = [prn(rec("pe:0500_400E", severity="common", actions=("clean",)), pid="p4", label="P4", state="cancelled")]
    assert run("nfResetRevalidate(%s, %s).ids" % (js(dlg), js(other))) == []      # another record than the one shown
    assert run("nfResetRevalidate(%s, []).ids" % js(dlg)) == []

    # step 2: the bulk response merged into the rows
    dlg.update(step=2, busy=False, rows=[dict(rv["rows"][0], status="confirmed", detail="подтверждено"),
                                         dict(rv["rows"][1]), {"id": "x", "label": "X", "model": "P2S", "code": "0300_400C",
                                                               "text": "Задача отменена.", "status": "rejected", "detail": "Диалог уже закрыт"}])
    res = run("nfResetDialogHtml(%s)" % js(dlg))
    assert '<h3 id="bdTitle">Сброс: подтверждено 1 из 3</h3>' in res
    assert ('<div class="bd-sum"><span class="bd-st ok">✓ 1</span><span class="bd-st bad">✕ 1</span>'
            '<span class="bd-st">— 1</span></div>') in res
    assert '<span class="bd-st bad">✕ Диалог уже закрыт</span>' in res and "— состояние изменилось, не отправлено" in res
    assert res.endswith('<div class="bd-foot"><button type="button" class="bd-btn" data-bd="close">Готово</button></div></div>')
    dlg["rows"][0]["status"] = "wait"
    waiting = run("nfResetDialogHtml(%s)" % js(dlg))
    assert "Сброс: ждём ответа принтеров…" in waiting and 'data-bd="close" aria-disabled="true"' in waiting


# --- CSS and icons ---------------------------------------------------------------------------

def test_icons_of_the_block_are_in_the_sprite():
    sprite = set(re.findall(r'<symbol id="i-([\w-]+)"', HTML))
    used = set(re.findall(r'icon\("([\w-]+)"\)', _block("nf-ui")))
    assert used and used <= sprite
    assert {"bell", "alert", "info", "reset", "ext", "tool", "chev"} <= sprite


def test_css_has_every_level_and_the_pause_tokens_in_every_theme():
    themes = re.findall(r'^\s*html\[data-theme="([\w-]+)"\] \{', CSS, re.M)
    themed = set(re.findall(r'html\[data-theme="([\w-]+)"\] \{ --sev-pause:', CSS))
    assert themed == set(themes) - {""} and len(themed) == 5           # + the default theme on :root
    assert re.search(r":root \{\s*--sev-pause: #fb923c;", CSS)
    for sel in ("nf-sev", "nf-badge", "nf-item"):
        for lvl in ("fatal", "pause", "serious", "common"):
            assert ".%s.%s " % (sel, lvl) in CSS, (sel, lvl)
    for sel in (".nf-strip.pause ", ".nf-strip.fatal ", ".nf-pop ", ".nf-back ", ".nf-more ", ".nf-bell ", ".nf-bell.calm ",
                ".nf-grp ", ".nf-line ", ".fb-btn.reset ", ".bd-ic.reset ", ".bd-dry ", ".printer-card.nf-flash "):
        assert sel in CSS, sel
    # translucent tints are laid over an opaque base (theme-soft-colors-translucent)
    for m in re.finditer(r"\.nf-[\w.-]+ \{[^}]*linear-gradient\(var\(--[\w-]+-soft\)", CSS):
        assert "background-color: var(--bg-elev)" in m.group(0) or ".nf-strip." in m.group(0), m.group(0)
    for mock in (".mock-note", ".mn-", "view-now", ".show-err-toggle"):
        assert mock not in CSS, mock
    # every class the block draws has a rule
    drawn = set(re.findall(r'class="((?:nf|bd)-[\w-]+)', _block("nf-ui")))
    for cls in drawn:
        assert re.search(r"\.%s\b" % re.escape(cls), CSS), cls


# --- DOM wiring: index.html slots, the card, the popover layer (spec step 10) -----------------

def _decl(name: str) -> str:
    """The whole `function name(...) {...}` by brace count."""
    m = re.search(r"function %s\([^)]*\) \{" % name, JS)
    assert m, "%s() not found in app.js" % name
    depth = 0
    for i in range(m.end() - 1, len(JS)):
        depth += {"{": 1, "}": -1}.get(JS[i], 0)
        if depth == 0:
            return JS[m.start():i + 1]
    raise AssertionError("unbalanced %s()" % name)


def _dom() -> str:
    """The notifications' DOM layer: from its state up to the end of initNotifications."""
    start = JS.index('var nfPopEl = document.getElementById("nfPop")')
    return JS[start:JS.index("\n      })();", JS.index("(function initNotifications()"))]


def test_index_has_the_bell_slot_and_the_popover():
    side = re.search(r'<div class="topbar-side">(.*?)</header>', HTML, re.S).group(1)
    assert side.index('<span id="nfBellSlot"></span>') < side.index('class="status-chip"')
    assert ('<div class="nf-back" id="nfBack" hidden></div>\n    <div class="nf-pop" id="nfPop" role="dialog" aria-modal="false" '
            'aria-labelledby="nfPopTitle" tabindex="-1" hidden></div>\n    <div class="app-toast" id="appToast">') in HTML
    assert 'id="nfPopTitle"' in _block("nf-ui")                         # both popovers title themselves


def test_every_element_looked_up_by_id_exists():
    # a missing element throws at the start of the IIFE and the whole page breaks (spec §8.8)
    ids = set(re.findall(r"getElementById\([\"']([\w-]+)[\"']\)", JS))
    drawn = set(re.findall(r'(?<![\w-])id="([\w-]+)"', JS))             # markup app.js draws itself (the bell)
    have = set(re.findall(r'(?<![\w-])id="([\w-]+)"', HTML))
    assert {"nfPop", "nfBack", "nfBellSlot", "nfBell"} <= ids
    assert ids and not ids - have - drawn, sorted(ids - have - drawn)


def test_the_show_errors_toggle_is_gone_everywhere():
    assert "Ошибки в карточках" not in HTML
    for gone in ("showErrorsToggle", "SHOW_ERRORS_KEY", "loadShowErrors", "saveShowErrors", "syncShowErrorsControl",
                 "alert-box", "alert-value"):
        assert gone not in HTML and gone not in JS and gone not in CSS, gone
    # its stored value is dropped once
    assert 'try { localStorage.removeItem("forge-ops-show-errors"); } catch (e) {}' in _decl("initTheme")


def test_card_draws_the_badge_the_strip_and_its_line():
    card = _decl("renderPrinterCard")
    assert card.index("${nfBadgeHtml(p, nfPopOpenFor(p.id), false, Date.now() / 1000)}") < card.index('<div class="status-badge ${uiStatus}">')
    strip = ("${nfStripHtml(p, isAdmin()) + cmdLineHtml(cardUi[p.id] && cardUi[p.id].nfLine, "
             "'data-nf-dismiss-card=\"' + safeText(p.id, \"\") + '\"')}")
    assert card.index("pc-progress") < card.index(strip) < card.index("pc-chips")
    # a cancelled job's stage line comes from the block, also without a progress
    assert 'var cancelText = p.state === "cancelled" ? nfCancelText(p) : "";' in card
    assert "var stage = changeText || cancelText || stageLabel(p.stage);" in card
    assert "!showProgress && !changeText && cancelText" in card


def test_a_cancelled_card_shows_only_its_line():
    # the mockup: no bar, percent, layers or job — «Задание отменено в … на N%» alone, an empty ring
    card = _decl("renderPrinterCard")
    assert 'const showProgress = p.state !== "cancelled" && (isPrinting || p.progress_pct != null);' in card
    assert "const ringOffset = progressOffset(showProgress ? progress : 0);" in card
    assert ".printer-card.cancelled .progress-fill" not in CSS
    assert ".printer-card.cancelled .progress-ring" not in CSS


def test_the_goto_flash_survives_the_card_re_render():
    # nfOpen re-renders the card right after «к карточке ›» (the badge's aria-expanded): the new
    # node takes the running flash over where it was, so both flashes are seen
    fleet = _decl("renderFleet")
    assert fleet.index("if (el) nfCarryFlash(el, fresh);") < fleet.index("if (el) el.replaceWith(fresh);")
    carry = _decl("nfCarryFlash")
    assert 'x.animationName === "nfFlash"' in carry and 'a.playState === "finished"' in carry
    assert 'to.style.animationDelay = -Math.round(Number(a.currentTime) || 0) + "ms";' in carry
    assert 'card.style.animationDelay = "";' in _decl("nfGoto")


def test_a_refusal_turns_into_resumed_on_the_printer():
    bad = {"k": "bad", "t": "Игнорировать и продолжить — отказ: Принтер уже не на паузе", "key": "pe:0500_8062"}
    pe = rec("pe:0500_8062", actions=("ignore",))
    info = {"k": "info", "t": "Печать уже идёт — продолжили на экране принтера"}
    assert run("nfResumedLine(%s, %s)" % (js(bad), js(prn(state="printing")))) == info
    assert run("nfResumedLine(%s, %s)" % (js(bad), js(prn(pe, state="printing")))) is None    # still listed
    assert run("nfResumedLine(%s, %s)" % (js(bad), js(prn(state="paused")))) is None
    assert run("nfResumedLine(%s, %s)" % (js(dict(bad, key=None)), js(prn(state="printing")))) is None  # no record
    assert run("nfResumedLine(%s, %s)" % (js(dict(bad, k="ok")), js(prn(state="printing")))) is None
    assert run("nfResumedLine(null, %s)" % js(prn(state="printing"))) is None
    assert 'if (line.k === "bad") line.key = String(key);' in _decl("runNfAction")
    load = _decl("load")
    assert load.index("nfResumedLines(cachedAllPrinters);") < load.index("applyAndRender();")
    assert "nfFadeLine(p.id, next)" in _decl("nfResumedLines")


def test_the_reset_sends_the_records_the_dialog_showed():
    dlg = {"go": [{"id": "p4", "key": "pe:0300_400C"}, {"id": "p10", "key": "pe:0500_400E"}, {"id": 7, "key": "pe:x"}]}
    assert run("nfResetKeys(%s, ['p4', '7'])" % js(dlg)) == {"p4": "pe:0300_400C", "7": "pe:x"}
    assert run("nfResetKeys(%s, [])" % js(dlg)) == {}
    assert 'postBulk(d.action, ids, d.action === "reset_errors" ? nfResetKeys(d, ids) : null)' in _decl("bulkDialogSend")
    assert "if (keys) body.keys = keys;" in _decl("postBulk")


def test_phone_bell_light_contrast_and_no_dead_token():
    assert ".nf-bell { min-height: 36px; gap: 4px; padding: 0 3px 0 8px; }" in CSS      # ≤640: still a 36 px target
    assert "min-height: 30px; gap: 4px" not in CSS
    for sel in (".nf-since", ".nf-badge.common", ".nf-strip .sub:not(.req)", ".nf-unk", ".nf-sev.common",
                ".nf-more .codes", ".nf-foot", ".nf-badge.quiet", ".nf-dim", ".nf-sec"):
        assert 'html[data-theme="light"] %s' % sel in CSS, sel
    assert "--alert-text" not in CSS                     # its .alert-value went with «Ошибки в карточках»


def test_render_sort_and_fleet_bar_use_the_block():
    assert "renderNf(focused);" in _decl("applyAndRender")
    assert "nfUrgency(a)" in _decl("sortPrinters")
    # «Сбросить ошибки» counts in the bell's list now (IA §5), the fleet bar has none
    assert "nfResetSplit" not in _decl("renderFleetBulk") and "nfResetSplit(all).go.length" in _decl("nfPopFarmHtml")
    # the reset dialog shares #bulkModal: drawn by the block, checked again by it before sending
    assert 'bulkDlg.action === "reset_errors" ? nfResetDialogHtml(bulkDlg) : bulkDialogHtml(bulkDlg)' in _decl("renderBulkDialog")
    assert 'd.action === "reset_errors" ? nfResetRevalidate(d, cachedAllPrinters)' in _decl("bulkDialogGo")
    opener = _decl("openResetDialog")
    assert "nfResetDialog(cachedAllPrinters)" in opener and "postBulk" not in opener   # the dry run sends nothing


def test_every_control_the_block_draws_is_wired():
    drawn = set(re.findall(r"data-nf-([a-z-]+)", _block("nf-ui")))
    assert {"open", "bell", "act", "cancel", "dismiss", "dismiss-card", "more", "close", "goto"} <= drawn
    dom = _dom()
    for a in drawn:
        assert "[data-nf-%s]" % a in dom, a
    # the reset dialog's data-bd controls are the bulk modal's (toggle: its change handler)
    bd = set(re.findall(r'data-bd="([a-z0-9]+)"', _decl("nfResetDialogHtml")))
    handled = set(re.findall(r'what === "([a-z0-9]+)"', _decl("onBulkDialogClick"))) | {"toggle"}
    assert bd and bd <= handled, bd - handled


def test_actions_post_the_admin_route():
    from app.api.admin import NotificationActionRequest, router
    act = _decl("runNfAction")
    assert '"/api/admin/printers/" + encodeURIComponent(pid) + "/notification-action"' in act
    assert "{ key: String(key), action: action, confirm: !!confirmed }" in act
    assert set(NotificationActionRequest.model_fields) == {"key", "action", "confirm"}
    assert "/api/admin/printers/{printer_id}/notification-action" in {r.path for r in router.routes}
    # a dangerous action asks first and nothing is sent before the answer; one at a time per record
    assert act.index("if (st.busy || st.done) return;") < act.index("if (a.confirm && !confirmed)") < act.index("postJson(")
    assert "st.done = out.status === \"confirmed\";" in act


def test_escape_cancels_a_question_before_it_closes_the_list():
    dom = _dom()
    esc = dom[dom.index('if (e.key !== "Escape" || !nfPop || bulkDlg) return;'):]
    assert esc.index("nfCancelConfirm(asks[0])") < esc.index("nfClose();")
    # the focus goes to the × on open and back to the opener on close
    assert 'var x = nfPopEl.querySelector("[data-nf-close]");' in _decl("nfOpen")
    assert "if (restore !== false) nfFocusFk(op);" in _decl("nfClose")
    # the two popovers never stay open together
    assert "closePop(true);" in _decl("nfOpen") and "nfClose(false);" in _decl("openPop")


def test_browser_notifications_say_why():
    load = _decl("load")
    assert "var why = nfTopText(p);" in load
    # its printer and whether its notifications open with it (plan step 15, deliberately changed: a click on
    # the push opens the printer — tests/test_web_phone_ui.py::test_a_push_opens_its_printer)
    assert 'showBrowserNotification("⚠️ Пауза печати", p.label + (why ? ": " + why : ""), p.id, !!why);' in load
    fallback = 'why = why || (nfHidden(p).length ? "" : p.last_error);'
    assert fallback in load
    assert 'p.state === "error" && prev !== "error"' in load                 # a cancel is its own state: silent

    def error_why(p, last_error):
        # the error push's reason, as load() computes it (its two lines, run against the block)
        return run("(function (p) { var why = nfTopText(p); %s return why; })(%s)" % (fallback, js(dict(p, last_error=last_error))))

    lidar = "HMS_0C00_0100_0001_0004: Текст уведомления [fatal]"
    assert error_why(prn(FATAL, state="error"), lidar) == "HMS_0C00_0100_0001_0004 — Текст уведомления"
    assert error_why(prn(MEM, state="error"), "Ошибка печати 0500_8062: x") == "Ошибка печати 0500_8062: x"   # routine: last_error
    assert error_why(prn(state="error"), "Klipper остановлен") == "Klipper остановлен"
    # a hidden record: its text comes neither from the record nor from last_error, built from every record
    assert error_why(prn(mute(FATAL), state="error"), lidar) == ""
    assert error_why(prn(mute(PE), MEM, state="error"), "Ошибка печати 0500_8062: Текст уведомления") == ""
    k = rec("klipper:shutdown", severity="fatal", text="Klipper остановлен (shutdown)")
    assert error_why(prn(mute(k), kind="klipper", state="error"), "K1: Klipper остановлен (shutdown)") == ""
    assert error_why(prn(mute(k), SERIOUS, kind="klipper", state="error"), "x") == "HMS_0300_1100_0002_0001 — Текст уведомления"


# --- hidden records («Скрыть», owner 27.10: C1's lidar HMS for weeks) ------------------------

def mute(n):
    return dict(n, muted=True)


def test_hidden_records_draw_no_attention():
    p = prn(mute(PE), SERIOUS, MEM)                                          # paused, its pause hidden
    assert run("[nfWorst(%s), nfAttention(%s), nfUrgency(%s), nfStripHtml(%s, true)]" % ((js(p),) * 4)) == [
        "serious", False, 0, ""]
    badge = run("nfBadgeHtml(%s, false)" % js(p))
    tip = "2 уведомления: серьёзно 1, уведомление 1 · скрыто 1 · решения не ждут"
    # nothing left to decide: grey (IA §5)
    assert 'class="nf-badge quiet"' in badge and ">%s2</button>" % INFO_IC in badge and 'data-tip="%s — открыть список"' % tip in badge
    # the same records not hidden (muted false / absent / not exactly true): as before
    for q in (prn(PE, SERIOUS, MEM), prn(dict(PE, muted=False), SERIOUS, MEM), prn(dict(PE, muted="yes"), SERIOUS, MEM)):
        assert run("[nfWorst(%s), nfAttention(%s), nfUrgency(%s)]" % ((js(q),) * 3)) == ["pause", True, 3]
        assert "nf-strip pause" in run("nfStripHtml(%s, true)" % js(q))
    # the strip takes the worst record that is not hidden, «ещё N» counts only those
    strip = run("nfStripHtml(%s, true)" % js(prn(mute(FATAL), STOP, MEM, state="error")))
    assert "<code>0300_8007</code>" in strip and "HMS_0C00" not in strip
    strip = run("nfStripHtml(%s, true)" % js(prn(PE, mute(SERIOUS), MEM)))
    assert '<span class="sub">ещё 1 уведомление</span>' in strip
    assert run("nfTopText(%s)" % js(prn(mute(PE), SERIOUS))) == "HMS_0300_1100_0002_0001 — Текст уведомления"
    assert run("nfTopText(%s)" % js(prn(mute(PE), MEM))) == ""             # no push reason from a hidden one


def test_all_hidden_the_badge_is_a_grey_bell_off_pill():
    p = prn(mute(FATAL), mute(MEM), pid="c1", label="C1", state="error")
    off = '<svg class="pc-ic" aria-hidden="true"><use href="#i-bell-off"></use></svg>'
    assert run("nfBadgeHtml(%s, false)" % js(p)) == (
        '<button type="button" class="nf-badge muted" data-nf-open="c1" data-fk="badge-c1" aria-haspopup="dialog"'
        ' aria-expanded="false" data-tip="2 скрытых уведомления — открыть список" aria-label="2 скрытых уведомления.'
        ' Открыть список">%s2</button>' % off)
    assert run("nfBadgeHtml(%s, false, true)" % js(p)) == (
        '<span class="nf-badge muted as-pill" data-tip="2 скрытых уведомления">%s2</span>' % off)
    assert "1 скрытое уведомление" in run("nfBadgeHtml(%s, false)" % js(prn(mute(FATAL))))
    # nothing else on the card: no strip, no attention even while the printer stands
    assert run("[nfStripHtml(%s, true), nfAttention(%s), nfUrgency(%s), nfWorst(%s)]" % ((js(p),) * 4)) == [
        "", False, 0, None]
    assert run("nfBadgeHtml(%s, false)" % js(dict(p, maintenance={"since": 1.0}))) == ""     # maintenance: nothing


def test_bell_farm_and_tab_title_leave_hidden_records_out():
    printers = [prn(mute(STOP), pid="p2", label="P2"),                       # all hidden: out of the bell and the list
                prn(PE, MEM, pid="p3", label="P3"),
                prn(mute(FATAL), pid="c1", label="C1", state="error"),
                prn(SERIOUS, mute(PE), pid="h1", label="H1")]
    bell = run("nfBellHtml(%s, false)" % js(printers))
    assert "1 принтер требует внимания" in bell and 'class="nf-badge pause as-pill">⚠ 1</span>' in bell
    farm = run("nfPopFarmHtml(%s, {}, {'farm-other': true}, {admin: true})" % js(printers))
    assert re.findall(r'data-nf-goto="([^"]*)"', farm) == ["p3", "h1"]
    assert '<div class="nf-sec">Нужно решение · 1</div>' in farm and "<span>Прочие · 1 на 1 принтере</span>" in farm
    h1 = farm.split('data-nf-goto="h1"')[1].split("</button>")[0]
    # H1's hidden pause holds nothing: it sits in «Прочие», grey, counted without the hidden one
    assert 'class="nf-badge quiet as-pill" data-tip="1 уведомление: серьёзно 1 · скрыто 1 · решения не ждут"' in h1
    calm = run("nfPopFarmHtml(%s, {}, {}, {})" % js([printers[0], printers[2]]))
    assert "На ферме уведомлений нет" in calm
    # the tab title counts what the bell counts
    lib = _lib() + "\n" + _block("tab-ui")
    got = _node(lib + "\nconsole.log(JSON.stringify(tabTitle(tabSummary(%s, function (p) { return p.state; }, nfAttention),"
                      " 'Кузница', false)));" % js(printers))
    assert got == "(1⚠) Кузница"


def test_the_list_folds_hidden_records_at_the_bottom():
    p = prn(SERIOUS, mute(PE), MEM, mute(FATAL))
    html = run("nfListHtml(%s, 'pop', {}, {}, %s)" % (js(p), js(ADMIN)))
    assert html.count('<div class="nf-item ') == 1 and "muted" not in html.split("nf-more")[0]
    hid = re.search(r'<button type="button" class="nf-more hid" data-nf-more="hid\|pop\|b1" aria-expanded="false"'
                    r' data-fk="more-hid\|pop\|b1"><span>([^<]*)</span><span class="codes">([^<]*)</span>', html)
    assert hid.groups() == ("скрытые · 2", "0C00_0100_0001_0004 · 0500_8062")        # worst first
    assert html.index("ещё 1 плановое") < html.index("скрытые · 2")             # at the very bottom
    opened = run("nfListHtml(%s, 'pop', {}, {'hid|pop|b1': true}, %s)" % (js(p), js(ADMIN)))
    assert "<span>Свернуть скрытые</span>" in opened and 'aria-expanded="true"' in opened
    tail = opened.split('data-nf-more="hid|pop|b1"')[1]
    assert tail.count('<div class="nf-item ') == 2
    assert '<div class="nf-item fatal muted">' in tail and '<div class="nf-item pause muted">' in tail
    assert tail.count('data-nf-mute="off"') == 2 and ">Вернуть</button>" in tail
    # the paused printer's buttons stay on its hidden pause: nothing else solves it
    assert [b[0] for b in btns(tail)] == ["ignore", "resume"]
    assert 'class="nf-wiki"' in tail                                         # «Подробнее» stays
    assert 'aria-expanded="false"' in run("nfListHtml(%s, 'farm', {}, {'hid|pop|b1': true}, %s)" % (js(p), js(ADMIN)))
    # a viewer sees the same folding, no Скрыть / Вернуть
    view = run("nfListHtml(%s, 'pop', {}, {'hid|pop|b1': true}, %s)" % (js(p), js(VIEWER)))
    assert "Свернуть скрытые" in view and view.count("nf-item fatal muted") == 1
    assert "data-nf-mute" not in view and "nf-mute" not in view
    # only hidden records: just the folded group
    only = run("nfListHtml(%s, 'pop', {}, {}, %s)" % (js(prn(mute(FATAL))), js(ADMIN)))
    assert "nf-item" not in only and "скрытые · 1" in only


def test_hide_and_restore_buttons_are_an_admins_and_never_held():
    def mute_btn(html):
        return re.findall(r'<button type="button" class="nf-mute( is-wait)?" data-nf-mute="(on|off)" data-pid="([^"]*)"'
                          r' data-key="([^"]*)" data-fk="([^"]*)" data-tip="[^"]*"( aria-disabled="true")?>(.*?)</button>', html)
    html = item(prn(PE), PE)
    assert mute_btn(html) == [("", "on", "b1", "pe:0500_8062", "mute-b1|pe:0500_8062|pop", "",
                               '<svg class="pc-ic" aria-hidden="true"><use href="#i-bell-off"></use></svg>Скрыть')]
    acts = html.split('<div class="nf-acts">')[1]
    assert acts.index("nf-wiki") < acts.index("nf-mute")                    # next to «Подробнее»
    assert "nf-mute" not in item(prn(PE), PE, opts=VIEWER)
    # not a printer command: no print control, offline, a busy / confirmed record — still there, enabled
    for p, st, o in ((prn(PE, resume=False), None, None), (prn(PE, online=False, state="offline"), None, None),
                     (prn(PE), {"busy": "ignore"}, None), (prn(PE), {"done": True}, None),
                     (prn(PE, kind="creality", resume=False), None, None), (prn(PE), None, {"admin": True, "canAct": False})):
        got = mute_btn(item(p, PE, st, o))
        assert len(got) == 1 and got[0][1] == "on" and got[0][5] == "", (p, st, o)
    wait = mute_btn(item(prn(PE), PE, {"muting": True}))
    assert wait[0][0] == " is-wait" and wait[0][5] == ' aria-disabled="true"' and '<span class="spin-sm"></span>' in wait[0][6]
    # no buttons, no link: a row of its own (still no actions row)
    lube = item(prn(LUBE[0]), LUBE[0])
    assert '<div class="nf-mrow"><button type="button" class="nf-mute"' in lube and "nf-acts" not in lube
    assert "nf-mrow" not in item(prn(LUBE[0]), LUBE[0], opts=VIEWER)
    assert "nf-mute" not in item(prn(STOP), STOP, {"confirm": "stop"})       # a question first
    back = mute_btn(item(prn(mute(PE)), mute(PE)))
    assert back[0][1] == "off" and back[0][6].endswith("Вернуть") and "#i-bell" in back[0][6]
    # a hidden record keeps the printer's buttons (a hidden pause still holds the printer), «Вернуть» last
    assert btns(item(prn(mute(PE)), mute(PE))) == btns(item(prn(PE), PE)) != []
    blocking = item(prn(mute(STOP)), mute(STOP))
    assert [b[0] for b in btns(blocking)] == ["resume", "stop"] and blocking.index('data-nf-act="stop"') < blocking.index("Вернуть")
    assert btns(item(prn(mute(PE)), mute(PE), opts=VIEWER)) == [] and btns(item(prn(mute(PE), resume=False), mute(PE))) == []
    assert "nf-mute" not in item(prn(mute(STOP)), mute(STOP), {"confirm": "stop"})   # a question first, as before
    # the answer line of a refused «Скрыть» shows on the record
    bad = item(prn(PE), PE, {"line": {"k": "bad", "t": "Скрыть — отказ: Принтер не найден", "mute": True}})
    assert "Скрыть — отказ: Принтер не найден" in bad and 'data-nf-dismiss="b1|pe:0500_8062"' in bad


def test_popover_summary_and_empty_state_with_hidden_records():
    html = pop(prn(PE, mute(FATAL), MEM, label="P3"))
    assert '<div class="nf-sum"><b>Печать стоит и ждёт решения</b> · пауза-ошибка — 1, уведомление — 1, скрыто — 1</div>' in html
    assert html.startswith('<div class="nf-pop-h"><span class="nf-badge pause as-pill" data-tip="2 уведомления: '
                           'пауза-ошибка 1, уведомление 1 · скрыто 1 · нужно решение">⚠ 2</span>')
    allhid = pop(prn(mute(FATAL), pid="c1", label="C1", state="printing", model="X1C"))
    assert allhid.startswith('<div class="nf-pop-h"><span class="nf-badge muted as-pill"')
    assert '<div class="nf-sum"><b>Печать идёт</b> · скрыто — 1</div>' in allhid
    body = allhid.split('<div class="nf-body">')[1]
    assert body.startswith('<div class="nf-empty">✓ Уведомлений нет</div><button type="button" class="nf-more hid"')


def test_hidden_strings_are_escaped():
    evil = '<img src=x onerror=alert(1)>"\''
    n = mute(rec("hms:0C00_0100_0001_0004", severity="fatal", text=evil, hint=evil))
    n["code"] = evil
    p = prn(n, pid='c"1', label=evil)
    out = run("[nfListHtml(%s, 'pop', {}, {'hid|pop|c\"1': true}, {admin: true}), nfBadgeHtml(%s, false),"
              " nfListHtml(%s, 'pop', {}, {}, {admin: true})]" % (js(p), js(p), js(p)))
    for html in out:
        assert "<img" not in html and "onerror=alert(1)>" not in html
        for attr in re.findall(r'(?:data-tip|aria-label|data-nf-open|data-nf-more|data-pid|data-key|class)="([^"]*)"', html):
            assert "<" not in attr and "'" not in attr
    assert "Вернуть" in out[0] and 'data-pid="c&quot;1"' in out[0]


def test_the_api_flag_is_what_the_block_reads():
    from app.services import notification_mutes
    recs = [dict(FATAL), dict(MEM)]
    notification_mutes.mark("c1", recs, {"c1": [FATAL["key"]]})
    assert run("%s.map(function (n) { return nfMuted(n); })" % js(recs)) == [True, False]


def test_hide_puts_the_admin_route_and_updates_the_record():
    from app.api.admin import NotificationMuteRequest, router
    act = _decl("runNfMute")
    assert '"/api/admin/printers/" + encodeURIComponent(pid) + "/notification-mute"' in act
    assert 'method: "PUT"' in act and "JSON.stringify({ key: String(key), muted: !!muted })" in act
    assert set(NotificationMuteRequest.model_fields) == {"key", "muted"}
    assert "/api/admin/printers/{printer_id}/notification-mute" in {r.path for r in router.routes}
    # admins only, one PUT at a time per record; a busy record / card does not hold it
    assert act.index("if (!isAdmin()) return;") < act.index("if (st.muting) return;") < act.index("apiFetch(")
    assert "st.busy" not in act and "st.done" not in act and "nfCanAct" not in act
    # success: the record in place, then a render; failure: the record's line
    ok = act.index("n.muted = body.muted;")
    assert act.index('st.line = { k: "bad"') < ok < act.rindex("applyAndRender();")
    click = _decl("onNfPopClick")
    assert click.index("[data-nf-mute]") < click.index("[data-nf-act]")
    assert 'el.getAttribute("data-nf-mute") === "on"' in click


def test_hidden_record_css():
    for sel in (".nf-mute {", ".nf-mrow {", ".nf-item.muted {", ".nf-badge.muted {", ".nf-mute:hover", ".nf-badge.quiet {"):
        assert sel in CSS, sel
    assert 'html[data-theme="light"] .nf-mute' in CSS and 'html[data-theme="light"] .nf-badge.muted' in CSS
    # dimmed by colour, not opacity: the code and «с …» keep the themes' contrast (opacity 0.6 put them at 2.4:1)
    css = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)
    muted = [(s.strip(), body) for sel, body in re.findall(r"([^{}]*)\{([^}]*)\}", css)
             for s in sel.split(",") if ".nf-item.muted" in s]
    assert len(muted) >= 5 and not any("opacity" in body for _s, body in muted)
    assert not any(c in s for s, _body in muted for c in (".nf-top", ".nf-code", ".nf-since"))
    assert ".nf-item.muted > .nf-auto, .nf-item.muted > .nf-req { color: var(--text-dim); }" in CSS
    assert ".nf-item.muted .nf-sev { background: var(--bg-soft); color: var(--text-dim);" in CSS     # a grey chip
    assert int(re.search(r"app\.js\?v=(\d+)", HTML).group(1)) >= 106           # a cache-buster only grows
    assert int(re.search(r"styles\.css\?v=(\d+)", HTML).group(1)) >= 89
