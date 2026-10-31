"""«Админ» polish (IA §8, plan step 13): the pure /* adm-ui */ block of app/web/app.js run in
node (TZ=Europe/Moscow) — the park table's maintenance and control cells, the Creality light
setting, the users' rows with their in-row forms, the auto-ignore codes — and source checks of
the markup and wiring (no prompt / confirm / alert left in «Парк» maintenance and «Доступ»).
Skipped without node.
"""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
JS = (ROOT / "app" / "web" / "app.js").read_text(encoding="utf-8")
HTML = (ROOT / "app" / "web" / "index.html").read_text(encoding="utf-8")
CSS = (ROOT / "app" / "web" / "styles.css").read_text(encoding="utf-8")
T = 1793300000            # 29.10.2026 21:53:20 MSK
NOW_MS = (T + 3 * 3600) * 1000


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


def _cut(name):
    """A function cut by its closing line (its regex literals hold quotes)."""
    m = re.search(r"function %s\([^)]*\) \{.*?\n      \}" % name, JS, re.S)
    assert m, name
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
    parts = [re.search(r"function safeText\([^)]*\) \{.*?\n\s*\}", JS, re.S).group(0), _cut("uaDevice"),
             _js_function("pad2"), _js_function("fmtAuditTime"), _js_function("maint2"), _js_function("maintSinceText"),
             _js_function("maintTipText"), _block("adm-ui")]
    return "\n".join(parts)


def run(expr):
    return _node(_lib() + "\nconsole.log(JSON.stringify(" + expr + "));")


def _plain(html):
    return re.sub(r"<[^>]+>", "", html)


def test_block_is_pure():
    block = _block("adm-ui")
    for banned in ("document.", "window.", "fetch(", "apiFetch", "cachedAllPrinters", "localStorage",
                   "prompt(", "confirm(", "alert("):
        assert banned not in block, banned


# --- «Парк» --------------------------------------------------------------------------------------

def test_control_cell_is_the_probes_verdict():
    full = {"kind": "bambu", "caps": {"pause": True, "light": True, "tune": True, "ams": True}, "caps_note": None}
    assert _plain(run("parkCtlHtml(%s)" % json.dumps(full))) == "печать · свет · t° и скорость · AMS"
    assert 'class="act-tag ok"' in run("parkCtlHtml(%s)" % json.dumps(full))
    light = {"kind": "bambu", "caps": {"pause": False, "light": True, "tune": False, "ams": False},
             "caps_note": "прошивка <блокирует> печать"}
    html = run("parkCtlHtml(%s)" % json.dumps(light))
    assert _plain(html) == "свет" and 'class="act-tag"' in html and 'data-tip="прошивка &lt;блокирует&gt; печать"' in html
    probing = {"kind": "bambu", "online": True, "caps": {"pause": None, "light": None, "tune": None, "ams": None}}
    assert _plain(run("parkCtlHtml(%s)" % json.dumps(probing))) == "проверяется"
    assert _plain(run("parkCtlHtml(%s)" % json.dumps(dict(probing, kind="klipper", online=False)))) == "нет связи"
    mks = {"kind": "mks", "caps": {"pause": False, "light": False, "tune": False, "ams": False}, "caps_note": "управление не подключено"}
    assert _plain(run("parkCtlHtml(%s)" % json.dumps(mks))) == "не подключено"
    assert _plain(run("parkCtlHtml(null)")) == "—"


def test_maintenance_cell_has_no_switch():
    row = {"id": "k<1>", "maintenance_since": T, "maintenance_note": "хотенд <старый>", "maintenance_by": "admin"}
    html = run("parkMaintHtml(%s, %d)" % (json.dumps(row), NOW_MS))
    assert "🔧 с 29.10" in html or "🔧 с 21:53" in html
    assert 'data-tip="На обслуживании с 29.10.2026 21:53 · поставил admin · хотенд &lt;старый&gt;"' in html
    assert 'data-act="card" data-id="k&lt;1&gt;">к карточке ›</button>' in html
    assert "maint-on" not in html and "maint-off" not in html and "Снять" not in html
    assert _plain(run("parkMaintHtml(%s, %d)" % (json.dumps({"id": "x", "maintenance_since": None}), NOW_MS))) == "—"


def test_creality_light_setting():
    assert run("[parkLightOn([], 'c1'), parkLightOn(['c1'], 'c1'), parkLightOn(null, 'c1'), parkLightOn(['c2'], null)]") == \
        [True, False, True, True]
    assert run("parkLightList(['c2'], 'c1', false)") == ["c1", "c2"]
    assert run("parkLightList(['c1', 'c2', 'c1'], 'c1', true)") == ["c2"]
    assert run("parkLightList(null, 'c1', true)") == []


# --- «Доступ» ------------------------------------------------------------------------------------

U = {"id": 3, "username": "dana", "role": "viewer",
     "last_login": {"created_at": T, "ip_address": "164.5.249.57", "user_agent": "Mozilla/5.0 (Windows NT 10.0) Edg/130.0"}}


def test_user_row_default():
    html = run("userRowHtml(%s, 'admin', {})" % json.dumps(U))
    cells = re.findall(r"<td[^>]*>(.*?)</td>", html, re.S)
    assert len(cells) == 4
    assert '<option value="viewer" selected>Зритель</option>' in cells[1] and "disabled" not in cells[1]
    assert _plain(cells[2]) == "29.10 21:53164.5.249.57 · Edge · Windows" and 'data-tip="Mozilla/5.0' in cells[2]
    assert 'data-uact="pw" data-uid="3">Сменить пароль<' in cells[3] and 'data-uact="del" data-uid="3">Удалить<' in cells[3]
    assert "title=" not in html                                      # tips are data-tip (IA §10)
    never = run("userRowHtml(%s, 'admin', {})" % json.dumps(dict(U, last_login=None)))
    assert ">не входил<" in never


def test_user_row_of_the_signed_in_admin():
    html = run("userRowHtml(%s, 'admin', {})" % json.dumps(dict(U, username="admin", role="admin")))
    assert "disabled data-tip=\"Свою роль менять нельзя" in html and "(вы)" in html
    assert 'data-uact="del"' not in html and 'data-uact="pw"' in html


def test_user_row_in_row_forms():
    pw = run("userRowHtml(%s, 'admin', {pw: '3'})" % json.dumps(U))
    assert 'data-pw-for="3"' in pw and 'type="password"' in pw and 'data-uact="pw-save" data-uid="3"' in pw
    assert 'data-uact="cancel"' in pw and 'data-uact="del"' not in pw
    role = run("userRowHtml(%s, 'admin', {role: {id: 3, role: 'admin'}})" % json.dumps(U))
    assert "Роль → «Админ»: сессии пользователя завершатся." in role and 'data-uact="role-yes" data-uid="3"' in role
    assert '<option value="admin" selected>Админ</option>' in role               # the asked role shows
    dele = run("userRowHtml(%s, 'admin', {del: 3})" % json.dumps(U))
    assert "Удалить «dana»?" in dele and 'data-uact="del-yes" data-uid="3"' in dele
    err = run("userRowHtml(%s, 'admin', {err: {3: 'Нельзя <так>'}})" % json.dumps(U))
    assert '<div class="adm-err-row">Нельзя &lt;так&gt;</div>' in err
    bad = run("userRowHtml(%s, '<x>', {del: 3})" % json.dumps(dict(U, username="<img src=x>")))
    assert "<img" not in bad


def test_users_wiring_has_no_native_dialogs():
    users = JS[JS.index("      var admUsers = [];"):JS.index("      (function() {\n        var addUserBtn")]
    assert "prompt(" not in users and "confirm(" not in users and "alert(" not in users
    for gone in ("_adminDeleteUser", "_adminToggleRole", "_adminEditUser", "lastLoginCell", "ROLE_BADGES", "fmtAdmDate"):
        assert gone not in JS, gone
    send = _js_function("admUserSend")
    assert "'/api/admin/users/' + encodeURIComponent(id)" in send and "admUsersUi.err[id] =" in send
    init = users[users.index("(function initAdminUsers() {"):]
    for need in ("act === 'pw-save'", "pass.length < 8", "admUserSend(id, 'PUT', { password: pass }, 'Пароль обновлён');",
                 "admUserSend(id, 'PUT', { role: admUsersUi.role.role }, 'Роль обновлена');",
                 "admUserSend(id, 'DELETE', null, 'Пользователь удалён');", "select.adm-role", "e.key === 'Escape'"):
        assert need in init, need
    assert "return userRowHtml(u, me, admUsersUi);" in _js_function("renderAdminUsers")


# --- «Автоматика → Автопропуск ошибок» ------------------------------------------------------------

def test_codes_are_typed_loosely_and_checked():
    assert run("['0500_8062', '05008062', ' 0c00-8009 ', '0300 400c', '0500_806', 'zzzz_8062', '', null].map(aiNormCode)") == \
        ["0500_8062", "0500_8062", "0C00_8009", "0300_400C", None, None, None, None]
    ok = run("aiAdd(['0500_8062'], '0c008009')")
    assert ok == {"codes": ["0500_8062", "0C00_8009"], "error": None}
    assert run("aiAdd(['0500_8062'], '0500-8062')") == {"codes": ["0500_8062"], "error": "Код 0500_8062 уже в списке"}
    assert run("aiAdd([], 'x')")["error"] == "Код вида 0500_8062 — восемь цифр 0–9 и A–F"
    full = run("aiAdd(%s, '0500_8062')" % json.dumps(["0300_%04X" % i for i in range(32)]))
    assert full["error"] == "Не больше 32 кодов" and len(full["codes"]) == 32
    # the client's rule is the server's (PUT /settings: auto_ignore_print_errors)
    from app.api import admin
    assert "[0-9A-F]{4}_[0-9A-F]{4}" in (ROOT / "app" / "api" / "admin.py").read_text(encoding="utf-8")
    assert run("AI_MAX") == admin.MAX_PE_CODES == 32
    assert run("[aiSame(['a'], ['a']), aiSame(['a'], ['b']), aiSame(null, [])]") == [True, False, True]


def test_codes_list():
    html = run("aiCodesHtml(['0500_8062', '0C00_8009', '0300_400C'], {'0500_8062': 'Свет <на столе>', '0C00_8009': null})")
    rows = re.findall(r'<div class="ai-code">(.*?)</div>', html)
    assert len(rows) == 3
    assert "<code>0500_8062</code>" in rows[0] and "Свет &lt;на столе&gt;" in rows[0] and 'data-ai-del="0500_8062">убрать<' in rows[0]
    assert "текста у Bambu нет" in rows[1] and ">…<" in rows[2]               # not asked yet
    assert "Список пуст" in run("aiCodesHtml([], {})")


def test_codes_wiring():
    save = _js_function("aiSave")
    assert 'apiFetch("/api/admin/settings", { method: "PUT"' in save and "auto_ignore_print_errors: ai.codes" in save
    assert 'aiShowError(d && typeof d.detail === "string" ? d.detail' in save            # the server's word as is
    load = _js_function("loadAutoIgnore")
    assert load.index("if (aiIsDirty()) { aiRender(); return; }") < load.index('apiFetch("/api/admin/settings")')
    assert 'apiFetch("/api/admin/print-error-texts?codes=" + encodeURIComponent(need.join(",")))' in _js_function("aiLoadTexts")
    assert 'if (adminSub === "auto") { loadAutodry(); loadAutoIgnore(); }' in _js_function("adminLoadSub")
    # a reload on #/admin/auto: the codes load once the role is known — adminLoadSub waited for it and
    # adminLoadWaited runs it then (the run: tests/test_web_me_ui.py::test_no_admin_request_before_the_role)
    assert "adminLoadWaited();" in _js_function("updateAuthUI")
    assert "if (!isAdmin()) { if (!currentUser) adminLoadWaits = true; return; }" in _js_function("adminLoadSub")
    init = JS[JS.index("(function initAutoIgnore() {"):]
    init = init[:init.index("})();")]
    assert 'trips.addEventListener("click", function () { actsOpenGroup("auto"); });' in init
    assert "if (adForm || aiIsDirty()) return true;" in _js_function("appUserBusy")
    assert "var ad = !!adForm, ai = aiIsDirty(), on = ad || ai;" in _js_function("autoDirtyMarks")


def test_light_and_park_wiring():
    save = _js_function("prnSaveLight")
    assert "parkLightOn(prnLightOff, id) === on) return null;" in save
    assert "JSON.stringify({ printer_light_disabled: parkLightList(prnLightOff, id, on) })" in save
    prn = _js_function("prnSave")
    assert "var lightErr = kind === 'creality' && saved && saved.id ? await prnSaveLight(saved.id) : null;" in prn
    assert "document.getElementById('prnLight').checked = parkLightOn(prnLightOff, p ? p.id : null);" in _js_function("prnOpenForm")
    load = _js_function("loadAdminPrinters")
    assert "apiFetch('/api/admin/settings')" in load and "prnNeedLive = !cachedAllPrinters.length;" in load
    assert "if (prnNeedLive) { prnNeedLive = false; loadAdminPrinters(); }" in _js_function("load")
    rend = _js_function("renderAdminPrinters")
    assert "parkCtlHtml(live)" in rend and "p.serial" not in rend and "access_code" not in rend
    assert "setActiveView(\"fleet\");" in _js_function("admGotoCard") and "finGoto(pid);" in _js_function("admGotoCard")


def test_markup():
    park = HTML[HTML.index('data-asub-panel="park"'):HTML.index('data-asub-panel="auto"')]
    heads = re.findall(r"<th[^>]*>([^<]*)</th>", park[park.index('id="adminPrintersTable"'):park.index("</thead>")])
    assert heads == ["Название", "Модель", "Тип", "IP", "Прошивка", "Обслуживание", "Управление", "Действия"]
    form = park[park.index('id="prnForm"'):]
    assert 'id="prnSerial"' in form and 'id="prnAccessCode"' in form                     # in the form, not the table
    light = form[form.index('id="prnLight"') - 200:form.index('id="prnLight"')]
    assert 'data-kinds="creality"' in light
    auto = HTML[HTML.index('data-asub-panel="auto"'):HTML.index('data-asub-panel="access"')]
    assert auto.index('id="autodry"') < auto.index('id="aiSection"')
    for need in ('id="aiCodes"', 'id="aiCodeInput"', 'id="aiAddBtn"', 'id="aiTripsBtn"', 'id="aiSaveBtn" disabled', 'id="aiError" hidden'):
        assert need in auto, need
    access = HTML[HTML.index('data-asub-panel="access"'):HTML.index('data-asub-panel="system"')]
    assert re.findall(r"<th[^>]*>([^<]*)</th>", access) == ["Логин", "Роль", "Последний вход", ""]
    assert 'id="addUserOpen"' in access and 'id="addUserForm" hidden' in access
    css = CSS[CSS.index("/* === «Админ» polish"):]
    for need in (".adm-inrow {", ".adm-ai .ai-code {", ".adm-link.danger {"):
        assert need in css, need
    assert "animation" not in css
