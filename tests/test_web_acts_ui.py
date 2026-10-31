"""«История → Действия» (IA §7, plan step 12): the pure /* acts-ui */ block of app/web/app.js
run in node (TZ=Europe/Moscow), and source checks of its markup, wiring and the contract
with GET /api/admin/audit, /audit/counts and /audit.csv. Skipped without node.
"""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app.api import admin as api_admin

ROOT = Path(__file__).resolve().parents[1]
JS = (ROOT / "app" / "web" / "app.js").read_text(encoding="utf-8")
HTML = (ROOT / "app" / "web" / "index.html").read_text(encoding="utf-8")
CSS = (ROOT / "app" / "web" / "styles.css").read_text(encoding="utf-8")
T = 1793300000          # 29.10.2026 21:53:20 MSK


def _block(tag):
    m = re.search(r"/\* %s:begin \*/(.*?)/\* %s:end \*/" % (tag, tag), JS, re.S)
    assert m, tag
    return m.group(1)


def _js_function(name):
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
    # safeText's regex literals hold quotes: cut it by its closing line, not by braces
    m = re.search(r"function safeText\([^)]*\) \{.*?\n\s*\}", JS, re.S)
    assert m
    return m.group(0)


def _node(src):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    out = subprocess.run([node, "-"], input=src, capture_output=True, text=True, timeout=60, encoding="utf-8",
                         env=dict(os.environ, TZ="Europe/Moscow"))
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _lib():
    return "\n".join([_safe_text(), _js_function("amsUnitName"), _js_function("pad2"), _js_function("uaDevice"),
                      _js_function("fmtAuditTime"), _block("acts-ui")])


def run(expr):
    return _node(_lib() + "\nconsole.log(JSON.stringify(" + expr + "));")


def _plain(html):
    return re.sub(r"<[^>]+>", "", html)


def test_block_is_pure():
    block = _block("acts-ui")
    for banned in ("document.", "window.", "fetch(", "apiFetch", "cachedAllPrinters", "localStorage", "histFilter"):
        assert banned not in block, banned


# --- the groups ---------------------------------------------------------------------------------

def _producers():
    """Every literal audit action (its first part) the server writes."""
    keys = set()
    for path in list((ROOT / "app").rglob("*.py")):
        if "vendor" in path.parts:
            continue
        src = path.read_text(encoding="utf-8")
        for m in re.finditer(r"""(?:_audit\([^,]+,\s*|audit\.log\([^,]+,\s*|_audit\(AUTO_USER,\s*|audit\()["']([a-z_]+)[:"'%]""", src):
            keys.add(m.group(1))
        for m in re.finditer(r"""_unit_audit\(["']([a-z_]+)["']""", src):
            keys.add(m.group(1))
    return keys


def test_every_action_the_server_writes_has_a_group():
    keys = _producers()
    assert {"login_ok", "token_refresh", "printer_command", "autodry_start", "auto_ignore", "notif_mute",
            "printer_maintenance", "autodry_run_now", "user_create", "settings_update"} <= keys, keys
    groups = run("%s.map(function (k) { return [k, actGroupOf(k + ':x')]; })" % json.dumps(sorted(keys)))
    assert [k for k, g in groups if not g] == [], groups
    got = dict(run("%s.map(function (k) { return [k, actGroupOf(k)]; })" % json.dumps(sorted(keys))))
    assert got["token_refresh"] == "svc"
    assert got["printer_command"] == got["printer_bulk"] == "cmd"
    assert got["autodry_start"] == got["autodry_config"] == got["auto_ignore"] == "auto"
    assert got["login_ok"] == got["login_fail"] == got["logout"] == got["rate_limit_block"] == got["user_delete"] == "access"
    assert got["printer_update"] == got["printers_apply"] == got["settings_update"] == got["notif_mute"] == "park"
    assert got["printer_maintenance"] == "park"
    assert run("[actGroupOf('something_new'), actGroupOf(null)]") == ["", ""]     # only under «Все»


def test_the_groups_fit_the_api():
    groups = run("ACT_GROUPS")
    assert [g["label"] for g in groups] == ["Команды", "Автоматика", "Парк и настройки", "Доступ и входы"]
    for g in groups:
        assert len(g["prefixes"]) + 1 <= api_admin.MAX_AUDIT_PREFIXES        # «служебные» may ride along
        for p in g["prefixes"]:
            assert api_admin._AUDIT_PREFIX.fullmatch(p), p
    assert all(api_admin._AUDIT_PREFIX.fullmatch(p) for p in run("ACT_SERVICE"))
    # the page size fits the list's Query(le=…)
    assert run("AUDIT_LIMIT") == 50
    assert re.search(r"limit: int = Query\(100, ge=1, le=500\)", (ROOT / "app" / "api" / "admin.py").read_text(encoding="utf-8"))


# --- the words of an action -----------------------------------------------------------------------

@pytest.mark.parametrize("action,what,printer", [
    ("printer_command:pause:bambu-3:st=confirmed", "Пауза подтверждено", "bambu-3"),
    ("printer_command:light_on:k1:bulk=b7,st=sent", "Свет вкл (группой) отправлено", "k1"),
    ("printer_command:notif_ignore:bambu-1:code=0500_8062,st=confirmed", "Игнорировать и продолжить · 0500_8062 подтверждено", "bambu-1"),
    ("printer_command:ams_load:b1:ams=0,slot=2,st=rejected", "Загрузить филамент · AMS 1 · слот 3 отклонено", "b1"),
    ("printer_command:dry_start:b1:ams=128,temp=55,hours=12,filament=PETG,st=confirmed",
     "Сушить · AMS HT 1 · 55° · 12 ч · PETG подтверждено", "b1"),
    ("printer_command:frobnicate:b1:st=weird", "frobnicate weird", "b1"),
    ("printer_bulk:pause:b7:confirmed=7,sent=0,rejected=1,skipped=2", "Пауза — всем · подтвердили 7 · отказ 1 · пропущено 2", None),
    ("printer_maintenance:p-1:on", "Обслуживание: вкл", "p-1"),
    ("printer_maintenance:p-1:off", "Обслуживание: снято", "p-1"),
    ("notif_mute:bambu-1:pe:0500_8062:on", "Скрыто уведомление 0500_8062", "bambu-1"),
    ("notif_mute:k1:klipper:shutdown:off", "Возвращено уведомление shutdown", "k1"),
    ("autodry_run_now:bambu-1:0:55/12/PETG", "Сушить сейчас · AMS 1 · 55° · 12 ч · PETG", "bambu-1"),
    ("autodry_start:p-4587:0:55/12/PETG", "Автосушка: старт · AMS 1 · 55° · 12 ч · PETG", "p-4587"),
    ("autodry_stop:bambu-2:0", "Стоп сушки · AMS 1", "bambu-2"),
    ("autodry_release:bambu-2:1", "Пауза снята · AMS 2", "bambu-2"),
    ("autodry_disable:bambu-2:0", "Автосушка: блок отключён · AMS 1", "bambu-2"),
    ("autodry_stop:bambu-2__0", "Стоп сушки · AMS 1", "bambu-2"),                 # a key the server could not split
    ("auto_ignore:bambu-3:0500_8062:confirmed", "Автопропуск 0500_8062 · нажал ✓", "bambu-3"),
    ("auto_ignore:bambu-3:0500_8062:rejected", "Автопропуск 0500_8062 · отклонено", "bambu-3"),
    ("auto_ignore:bambu-3:0C00_8009:skip:tries_used", "Автопропуск 0C00_8009 · не нажал: попытки кончились", "bambu-3"),
    ("auto_ignore:bambu-3:0C00_8009:skip:state_IDLE", "Автопропуск 0C00_8009 · не нажал: принтер не на паузе", "bambu-3"),
    ("auto_ignore:bambu-3:0C00_8009:skip:maintenance", "Автопропуск 0C00_8009 · не нажал: принтер на обслуживании", "bambu-3"),
    ("autodry_config", "Изменены правила автосушки", None),
    ("login_ok", "Вход", None),
    ("login_fail", "Неудачный вход", None),
    ("token_refresh", "Обновление токена", None),
    ("printers_apply", "Применена конфигурация", None),
    ("job_verdict:42:good", "Оценка печати №42", None),
    ("<img src=x>", "<img src=x>", None),
])
def test_action_words(action, what, printer):
    out = run("actText(%s)" % json.dumps(action))
    assert htmlunescape(_plain(out["what"])) == what and out["printer"] == printer
    assert "<img" not in out["what"]


def htmlunescape(s):
    import html
    return html.unescape(s)


def test_tags_carry_the_outcome():
    assert 'class="act-tag ok">нажал ✓<' in run("actText('auto_ignore:b:0500_8062:confirmed').what")
    assert 'class="act-tag warn">не нажал' in run("actText('auto_ignore:b:0500_8062:skip:tries_used').what")
    assert 'class="act-tag ok">подтверждено<' in run("actText('printer_command:stop:b:st=confirmed').what")
    assert 'class="act-tag err">Неудачный вход<' in run("actText('login_fail').what")
    assert run("[actWho('auto-dry'), actWho('auto-ignore'), actWho(null), actWho('<b>')]") == \
        ["автосушка", "автопропуск", "—", "&lt;b&gt;"]


# --- counts, chips, «Кто», «Служебные» --------------------------------------------------------

COUNTS = [{"user": "dana", "key": "token_refresh", "n": 584}, {"user": "admin", "key": "token_refresh", "n": 297},
          {"user": "robot", "key": "login_ok", "n": 115}, {"user": "auto-dry", "key": "autodry_start", "n": 94},
          {"user": "admin", "key": "printer_command", "n": 78}, {"user": "admin", "key": "settings_update", "n": 2},
          {"user": None, "key": "login_fail", "n": 3}, {"user": "admin", "key": "new_thing", "n": 1}]


def test_counts_and_totals():
    c = run("actCounts(%s)" % json.dumps(COUNTS))
    assert c == {"all": 293, "svc": 881, "cmd": 78, "auto": 94, "park": 2, "access": 118}
    assert run("actCounts(%s, 'admin')" % json.dumps(COUNTS)) == {"all": 81, "svc": 297, "cmd": 78, "auto": 0, "park": 2, "access": 0}
    assert run("[actTotal(%s, {group: ''}), actTotal(%s, {group: 'cmd'}), actTotal(%s, {group: '', svc: true}),"
               " actTotal(null, {group: ''})]" % ((json.dumps(c),) * 3)) == [293, 78, 1174, None]


def test_chips_and_the_service_toggle():
    c = run("actCounts(%s)" % json.dumps(COUNTS))
    html = run("actChipsHtml(%s, {group: '', svc: false})" % json.dumps(c))
    assert _plain(html) == "Все 293Команды 78Автоматика 94Парк и настройки 2Доступ и входы 118"
    assert html.count('class="farm-chip on"') == 1 and 'data-act-group=""' in html
    html = run("actChipsHtml(%s, {group: 'auto', svc: true})" % json.dumps(dict(c, park=0)))
    assert 'class="farm-chip on" data-act-group="auto"' in html and "×" in html and "Все <b>1174</b>" in html
    assert 'class="farm-chip zero" data-act-group="park"' in html
    assert run("actGroupClick('', 'auto')") == "auto" and run("actGroupClick('auto', 'auto')") == ""
    assert run("[actGroupClick('auto', ''), actGroupClick('', 'bogus')]") == ["", ""]
    svc = run("actSvcHtml(%s, false)" % json.dumps(c))
    assert svc == {"text": "Служебные 881 · скрыты", "tip": "«Обновление токена» — 75 % строк за период. Клик — показать"}
    assert run("actSvcHtml(null, true).text") == "Служебные · показаны"


def test_who_choice():
    html = run("actWhoOptionsHtml(%s, '')" % json.dumps(COUNTS))
    assert re.findall(r'<option value="([^"]*)"', html) == ["", "admin", "auto-dry", "dana", "robot"]
    assert ">автосушка<" in html and ">все<" in html
    kept = run("actWhoOptionsHtml([], 'gone')")
    assert '<option value="gone" selected>gone</option>' in kept


# --- the requests -----------------------------------------------------------------------------

W = {"fr": T - 86400.5, "to": T + 0.25}


def test_list_and_counts_requests():
    q = run("actsQuery({group: '', who: '', svc: false}, %s, null, 0)" % json.dumps(W))
    assert q == "limit=50&offset=0&fr=%d&to=%d&exclude=token_refresh" % (T - 86401, T + 1)
    q = run("actsQuery({group: 'auto', who: 'auto-ignore', svc: false}, %s, 'p 1', 100)" % json.dumps(W))
    assert q == ("limit=50&offset=100&fr=%d&to=%d&printer=p%%201&user=auto-ignore&prefix=autodry,auto_ignore"
                 % (T - 86401, T + 1))
    q = run("actsQuery({group: 'cmd', who: '', svc: true}, %s, null, 0)" % json.dumps(W))
    assert q.endswith("&prefix=printer_command,printer_bulk,token_refresh")
    assert run("actsQuery({group: '', who: '', svc: true}, %s, null, 0)" % json.dumps(W)).endswith("&to=%d" % (T + 1))
    assert run("actsCountsQuery(%s, 'bambu-3')" % json.dumps(W)) == "fr=%d&to=%d&printer=bambu-3" % (T - 86401, T + 1)
    url = run("actsCsvUrl({group: 'access', who: '', svc: false}, %s, null)" % json.dumps(W))
    assert url == "/api/admin/audit.csv?fr=%d&to=%d&prefix=login,logout,user_,rate_limit" % (T - 86401, T + 1)


# --- a row --------------------------------------------------------------------------------------

ROW = {"id": 7, "user_id": "dana", "action": "printer_command:pause:bambu-3:bulk=b1,st=confirmed",
       "ip_address": "203.0.113.5", "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/130.0 Safari/537.36 Edg/130.0",
       "created_at": T}


def test_row_and_its_details():
    html = run("actRowHtml(%s, {'bambu-3': 'P3'}, false)" % json.dumps(ROW))
    cells = re.findall(r'<td class="([^"]+)">(.*?)</td>', html)
    assert [c for c, _ in cells] == ["act-t mono", "act-who", "act-what", "act-pr", "act-dev"]
    assert [_plain(v) for _, v in cells] == ["29.10 21:53", "dana", "Пауза (группой) подтверждено", "P3", "Edge · Windows"]
    assert 'data-act-id="7"' in html and 'aria-expanded="false"' in html and "act-xrow" not in html
    gone = run("actRowHtml(%s, {}, true)" % json.dumps(dict(ROW, user_agent="", action="printer_maintenance:old-1:on")))
    assert ">old-1<" in gone                                           # a printer gone from the farm: its id
    assert 'aria-expanded="true"' in gone and 'class="act-xrow" data-xfor="7"' in gone
    assert "IP <b class=\"mono\">203.0.113.5</b>" in gone and "printer_maintenance:old-1:on" in gone
    bad = run("actRowHtml(%s, {}, true)" % json.dumps(dict(ROW, user_id="<s>", user_agent="<script>", ip_address="<i>")))
    assert "<s>" not in bad and "<script>" not in bad and "<i>" not in bad


# --- the page: markup, wiring, CSS ---------------------------------------------------------------

def test_markup():
    panel = HTML[HTML.index('data-hsub="actions"'):HTML.index('id="jobsLoader"')]
    assert 'id="actsPanel" hidden' in panel
    assert panel.index('id="actsChips"') < panel.index('id="actsWho"') < panel.index('id="actsSvc"') < \
        panel.index('id="actsBody"') < panel.index('id="actsMore"')
    assert re.findall(r"<th>([^<]*)</th>", panel) == ["Время", "Кто", "Что", "Принтер", "Устройство"]
    assert "Показать ещё 50" in panel
    # «Доступ» no longer holds the journal, nor its endless loading
    assert "adminAuditTable" not in HTML and "adminAuditSentinel" not in HTML
    assert "setupAuditScroll" not in JS and "IntersectionObserver(function(entries)" not in JS


def test_wiring():
    assert 'else if (historySub === "actions") loadActs();' in _js_function("histLoadSub")
    load = _js_function("loadActs")
    assert load.index("if (!actsEls.panel || !isAdmin()) return;") < load.index('apiFetch("/api/admin/audit?"')
    assert "listOnly && acts.counts ? [] : [apiFetch(\"/api/admin/audit/counts?\" + actsCountsQuery(w, pid))]" in load
    assert "if (gen !== acts.gen) return;" in load
    more = _js_function("loadMoreActs")
    assert "offset = acts.offset + AUDIT_LIMIT" in more and "acts.w, histFilter.printer, offset" in more
    init = JS[JS.index("(function initActs() {"):]
    init = init[:init.index("})();")]
    for need in ("acts.filter.group = actGroupClick(acts.filter.group, b.dataset.actGroup);", "loadActs(true);",
                 "acts.filter.svc = !acts.filter.svc;", "acts.filter.who = actsEls.who.value", 'actsEls.more.addEventListener("click", loadMoreActs);',
                 "actsToggleRow(Number(tr.dataset.actId));"):
        assert need in init, need
    # a reload on «Действия» before the role is known: loaded once the role arrives
    auth = _js_function("updateAuthUI")
    assert 'isAdmin() && historySub === "actions" && acts && !acts.rows.length && !acts.busy' in auth
    # «⇩ Выгрузить» on «Печати» and «Действия», admins only, the list's filter
    role = _js_function("histRoleUI")
    assert 'histEls.csv.hidden = !(admin && (historySub === "jobs" || historySub === "actions"));' in role
    csv_click = JS[JS.index('document.getElementById("jobsCsvBtn").addEventListener'):]
    assert 'historySub === "actions" ? actsCsvUrl(acts.filter, histWindow(histFilter, now), histFilter.printer)' in csv_click[:600]
    # «Срабатывания ›» of «Автоматика» opens it on the automation's rows
    group = _js_function("actsOpenGroup")
    assert group.index("if (!isAdmin()) return;") < group.index('setHistorySub("actions");') < group.index('setActiveView("history");')


def test_styles():
    css = CSS[CSS.index("/* === История → Действия"):]
    for need in (".acts-table tr.act-row { cursor: pointer; }", ".act-tag.ok {", ".acts-table .act-dev { display: none !important; }"):
        assert need in css, need
    assert "animation" not in css
