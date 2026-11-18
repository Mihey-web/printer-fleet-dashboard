"""QA 17.11, part C1 — the main interface (app/web/app.js, styles.css, index.html).

The testers' checks (docs/dev/qa-2026-11-17 on the qa/* branches: code.md Н6, Н8, Н12–Н14 and its
hard-coded values, ui.md 1–14, hunt.md 2, 12, 33–35, 42, 44–48, func.md F9) moved here, plus the
coordinator's two for the users' form. Each fails on main 0f67c79. The JS ones run app.js's own
functions in node: pulled out by name (braces matched, strings, comments and regex literals
skipped, as hunt-repros/p8a__lib.js does) and evaluated in one scope with stubs. Skipped without node.
"""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "app" / "web"
JS = (WEB / "app.js").read_text(encoding="utf-8")
CSS = (WEB / "styles.css").read_text(encoding="utf-8")
HTML = (WEB / "index.html").read_text(encoding="utf-8")

# p8a__lib.js (hunt-repros), reading app.js from this checkout
LOADER = r"""
"use strict";
const SRC = require("fs").readFileSync(%(path)s, "utf8");
function prevNonSpace(i) { for (i--; i >= 0 && /\s/.test(SRC[i]); i--); return SRC[i] || ""; }
function closeOf(startIdx, open, close) {
  let i = SRC.indexOf(open, startIdx), depth = 0, q = null;
  for (; i < SRC.length; i++) {
    const c = SRC[i];
    if (q) { if (c === "\\") i++; else if (c === q) q = null; continue; }
    if (c === "'" || c === '"' || c === "`") { q = c; continue; }
    if (c === "/" && SRC[i + 1] === "/") { i = SRC.indexOf("\n", i); continue; }
    if (c === "/" && SRC[i + 1] !== "*" && /[(,=:\[!&|?{};]/.test(prevNonSpace(i))) {
      let cls = false;
      for (i++; i < SRC.length; i++) {
        const r = SRC[i];
        if (r === "\\") { i++; continue; }
        if (r === "[") cls = true; else if (r === "]") cls = false; else if (r === "/" && !cls) break;
      }
      continue;
    }
    if (c === open) depth++;
    else if (c === close && --depth === 0) return i + 1;
  }
  throw new Error("unbalanced");
}
function fn(name) {
  const m = new RegExp("function " + name + "\\(").exec(SRC);
  if (!m) throw new Error(name + " not found");
  const start = SRC.slice(m.index - 6, m.index) === "async " ? m.index - 6 : m.index;
  return SRC.slice(start, closeOf(m.index, "{", "}"));
}
function v(name) {
  const m = new RegExp("var " + name + " = ").exec(SRC);
  if (!m) throw new Error(name + " not found");
  const at = m.index + m[0].length, c = SRC[at];
  if (c === "{") return SRC.slice(m.index, closeOf(at, "{", "}")) + ";";
  if (c === "[") return SRC.slice(m.index, closeOf(at, "[", "]")) + ";";
  return SRC.slice(m.index, SRC.indexOf(";", m.index) + 1);
}
function load(fns, vars, extra) {
  const code = (vars || []).map(v).join("\n") + "\n" + fns.map(fn).join("\n") + "\n" + (extra || "") +
    "\nreturn {" + fns.concat(vars || []).join(",") + "};";
  return new Function(code)();
}
const tick = async (n) => { for (let i = 0; i < (n || 6); i++) await new Promise((r) => setTimeout(r, 0)); };
"""


def node(body, env=None):
    """Run `body` (after the loader) in node; it prints one JSON value with out(...)."""
    exe = shutil.which("node")
    if exe is None:
        pytest.skip("node is not installed")
    src = LOADER % {"path": json.dumps(str(WEB / "app.js"))} + \
        "\nfunction out(x) { console.log(JSON.stringify(x)); }\n(async () => {\n" + body + \
        "\n})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });\n"
    run_env = dict(os.environ, **(env or {}))
    res = subprocess.run([exe, "-"], input=src, capture_output=True, text=True, timeout=60, encoding="utf-8", env=run_env)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout.strip().splitlines()[-1])


def fn_src(name):
    m = re.search(r"(?:async )?function %s\(" % name, JS)
    assert m, name
    return JS[m.start():JS.index("\n      }\n", m.start()) + 8]


# --- Н6: «Применить и перезапустить» -----------------------------------------------------------

APPLY = r"""
let waits = 0, said = [];
const api = load(["prnApply"], [], `
  function prnWaitForRestart() { globalThis.__w++; }
  function replyIn(el, r) { globalThis.__said.push(r); }
  function apiFetch() { return globalThis.__answer(); }`);
async function run(answer) {
  globalThis.__w = 0; globalThis.__said = []; globalThis.__answer = answer;
  await api.prnApply(null); await tick();
  return { waits: globalThis.__w, said: globalThis.__said };
}
const res = (ok, status, body) => () => Promise.resolve({ ok, status, json: () => body === undefined ? Promise.reject(new Error("no json")) : Promise.resolve(body) });
out({
  ok: await run(res(true, 200, { status: "restarting" })),
  refused: await run(res(false, 403, { detail: "Нужны права администратора" })),
  broken: await run(res(false, 502)),
  offline: await run(() => Promise.reject(new TypeError("Failed to fetch"))),
  session: await run(() => Promise.reject(new Error("Unauthorized"))),
});
"""


def test_apply_says_applied_only_when_the_server_restarts():
    got = node(APPLY)
    assert got["ok"] == {"waits": 1, "said": []}
    for case in ("refused", "broken", "offline", "session"):
        assert got[case]["waits"] == 0, case                      # no «Сервис перезапущен, конфигурация применена»
        assert got[case]["said"][0]["k"] == "bad" and got[case]["said"][0]["t"].startswith("Не применено"), case
    assert "Нужны права администратора" in got["refused"]["said"][0]["t"]
    assert "ошибка 502" in got["broken"]["said"][0]["t"]
    assert "нет связи" in got["offline"]["said"][0]["t"] and "сессия истекла" in got["session"]["said"][0]["t"]
    # the button goes through it, and the old «.then(() => prnWaitForRestart())» on any answer is gone
    assert "if (what === 'apply') { prnApply(txt); return; }" in JS
    assert ".catch(function () { prnWaitForRestart(); })" not in JS


# --- Н8 / hunt 12: the 3-s poll ---------------------------------------------------------------

POLL = r"""
const api = load(["load", "loadTick"], ["POLL_MS", "pollSeq"], `
  var fleetPainted = true, fleetEvBusy = null, cachedAllPrinters = [], prnNeedLive = false, prevPrinterStates = {};
  var tabState = { lastOkAt: Date.now() }, grid = { innerHTML: "" }, shown = [], polls = [], loads = 0;
  var window = { Notification: {} }, Notification = { permission: "granted" };
  function fleetLoadSince() {} function nfResumedLines() {} function applyAndRender() {} function updateTabStatus(ps, st) { tabState.stale = st; globalThis.__st.push(st); }
  function jobsTick() {} function histPrinterOptions() {} function renderLiveChip() {} function loadAdminPrinters() {}
  function graceEta(p) { return p; }
  function loadNotificationSettings() { return { enabled: true, finish: true, error: true, paused: true }; }
  function inMaint() { return false; } function nfTopText() { return ""; } function nfHidden() { return []; }
  function showBrowserNotification(title, body) { shown.push(title + " " + body); }
  function apiFetch() {
    loads++;
    return new Promise(function (res) { polls.push(function (state) {
      res({ json: function () { return [{ id: "P1", label: "P1", state: state }]; } }); }); });
  }
  globalThis.__p = { polls: () => polls, shown: () => shown, loads: () => loads, tab: tabState };`);
globalThis.__st = [];
const S = globalThis.__p, P = S.polls;
// hunt-repros/p8c__load_overlap_notify.js
api.load(); await tick(); P()[0]("printing"); await tick();   // t=0  : printing
api.load(); await tick();                                     // t=3 s: asked while printing, slow answer
api.load(); await tick(); P()[2]("finished"); await tick();   // t=6 s: finished -> notification #1
P()[1]("printing"); await tick();                             // the t=3 s answer lands late
api.load(); await tick(); P()[3]("finished"); await tick();   // t=9 s: finished again
const shown = S.shown().slice();
// the timer: no second poll while one is on its way (a hanging fetch: up to POLL_STUCK_MS)
const before = S.loads();
api.loadTick(); api.loadTick(); await tick();
const whileBusy = S.loads() - before;
P()[4]("finished"); await tick();
api.loadTick(); await tick();
const afterAnswer = S.loads() - before;
// F9: no answer for two polls -> «нет связи» at the next tick, while the poll still hangs
S.tab.lastOkAt = Date.now() - 2 * api.POLL_MS - 1500;
globalThis.__st = [];
api.loadTick(); await tick();
out({ shown, whileBusy, afterAnswer, stale: globalThis.__st, pollMs: api.POLL_MS });
"""


def test_slow_poll_answer_does_not_roll_back_nor_notify_twice():
    got = node(POLL)
    assert got["shown"] == ["✅ Печать завершена P1"]           # hunt 12: was 2
    assert got["whileBusy"] == 1                                # the first tick polls, the second waits
    assert got["afterAnswer"] == 2
    assert got["stale"] == [True]                               # F9: said at once, not after 30 s


def test_poll_constants_are_named_and_the_timer_is_guarded():
    assert "setInterval(loadTick, POLL_MS);" in JS and "setInterval(load, 3000)" not in JS
    assert re.search(r"var POLL_MS = 3000, LIVE_DOWN_MS = 2 \* POLL_MS \+ 1000, POLL_STUCK_MS = \d+;", JS)
    assert "if (seq < pollShown) return;" in fn_src("load")


# --- F9: the cards say «нет связи» too --------------------------------------------------------

def test_stale_cards_are_dimmed():
    upd = fn_src("updateTabStatus")
    assert 'grid.classList.toggle("fleet-stale", !!stale);' in upd
    assert re.search(r"\.printer-grid\.fleet-stale \.printer-card \{ opacity: 0\.55;", CSS)


# --- Н12: errors said, not swallowed ----------------------------------------------------------

SETTINGS = r"""
const api = load(["loadServerSettings"], [], `
  function isAdmin() { return true; }
  function srvLoadFailed(on) { globalThis.__f.push(on); }
  function fillServerSettings(v) { globalThis.__fill.push(v); }
  function apiFetch() { return globalThis.__answer(); }`);
const origErr = console.error; console.error = () => {};
async function run(answer) {
  globalThis.__f = []; globalThis.__fill = []; globalThis.__answer = answer;
  api.loadServerSettings(); await tick();
  return { failed: globalThis.__f, filled: globalThis.__fill.length };
}
const r = {
  e500: await run(() => Promise.resolve({ ok: false, status: 500, json: () => Promise.reject(new SyntaxError("x")) })),
  net: await run(() => Promise.reject(new TypeError("Failed to fetch"))),
  ok: await run(() => Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ values: { telegram_enabled: true }, telegram: {} }) })),
};
console.error = origErr;
out(r);
"""


def test_settings_that_did_not_load_are_said_and_switched_off():
    got = node(SETTINGS)
    assert got["e500"] == {"failed": [True], "filled": 0}       # ui.md 4: was silent, switches «off»
    assert got["net"] == {"failed": [True], "filled": 0}
    assert got["ok"] == {"failed": [False], "filled": 1}
    box = re.search(r'<div class="srv-load-err" id="srvLoadError" role="alert" hidden>(.*?)</div>', HTML)
    assert box and 'id="srvRetryBtn"' in box.group(1) and "Настройки не загрузились" in box.group(1)
    failed = fn_src("srvLoadFailed")
    assert "el.disabled = on;" in failed and "box.hidden = !on;" in failed
    assert "srvEl('srvRetryBtn').addEventListener('click', loadServerSettings);" in JS


def test_proxy_check_and_api_fetch_swallow_nothing():
    check = JS[JS.index("srvEl('proxyCheckNowBtn').addEventListener"):]
    check = check[:check.index("})();")]
    assert ".catch(function () {})" not in check
    assert "srvError('proxyError', 'Проверка не прошла: '" in check
    api = fn_src("apiFetch")
    assert "catch (e)" not in api and "throw new Error('Unauthorized');" in api


AUTOIGNORE = r"""
const api = load(["loadAutoIgnore", "aiIsDirty"], ["ai"], `
  function isAdmin() { return true; } function aiRender() {} function aiLoadTexts() {}
  function aiSame(a, b) { return JSON.stringify(a) === JSON.stringify(b); }
  function apiFetch() { return Promise.resolve({ ok: false, status: 500, json: () => Promise.resolve({}) }); }
  globalThis.__ai = function () { return ai; };`);
const origErr = console.error; console.error = () => {};
await api.loadAutoIgnore();
console.error = origErr;
out(globalThis.__ai());
"""


def test_auto_ignore_list_that_did_not_load_is_not_an_empty_one():
    got = node(AUTOIGNORE)
    assert got["saved"] is None and got["failed"] is True       # «Сохранить» cannot write [] over the server's list


# --- Н13: dead code ---------------------------------------------------------------------------

def test_dead_functions_are_gone():
    for name in ("isAuthenticated", "fmtAvg", "adMatBadges", "AI_CODE_RE"):
        assert not re.search(r"\b%s\b" % name, JS), name


DEAD_CSS = ("panel-badge", "live-strip", "settings-dropdown", "sort-btn", "strip-card", "attention-item", "sdd-head",
            "settings-btn", "sub-setting", "fleet-head", "badge-viewer", "seg-range", "jr-more", "dash-log-end")


def test_dead_css_classes_are_gone():
    for cls in DEAD_CSS:
        assert not re.search(r"\.%s\b" % re.escape(cls), CSS), cls


# --- Н14: one duration format, one escaping ---------------------------------------------------

DUR = r"""
const api = load(["timeFmt", "fleetDur", "amsFmtMin", "adFmtHM", "safeText", "escHtml"], []);
out({
  t: [3599, 7190, 35990, 3590, 45, 29, 0, null, 60, 5400, 43198].map(api.timeFmt),
  fleet: [0, 3599, 7190, 172800].map(api.fleetDur),
  ams: [712, 40, 0, 361].map(api.amsFmtMin),
  ad: [425, 40, 0, 59.6].map(api.adFmtHM),
  esc: [api.escHtml(null), api.escHtml(undefined), api.escHtml("<a&'\"b>"), api.escHtml(0)],
  safe: [api.safeText(null, ""), api.safeText("<a&'\"b>", "")],
});
"""


def test_one_duration_format_carries_minutes_into_hours():
    got = node(DUR)
    assert got["t"] == ["1ч", "2ч", "10ч", "1ч", "< 1м", "< 1м", "—", "—", "1м", "1ч 30м", "12ч"]   # never «60м» (was «1ч 59м», «59м»)
    assert got["fleet"] == ["1м", "1ч", "2ч", "2 дн"]
    assert got["ams"] == ["11ч 52м", "40м", "0м", "6ч 1м"]       # was «11:52», «6:01» next to «осталось 2ч 58м»
    assert got["ad"] == ["7ч\u00a05м", "40м", "0м", "1ч"]
    assert got["esc"] == ["", "", "&lt;a&amp;&#39;&quot;b&gt;", "0"]   # escHtml(null) printed «null»
    assert got["esc"][2] == got["safe"][1]
    # the timeline's tooltip had its own «1ч5м»
    assert "em>=60?Math.floor(em/60)" not in JS and "details.push('Осталось: ' + timeFmt(best.eta_sec));" in JS


# --- hard-coded values ------------------------------------------------------------------------

def test_notification_icon_is_the_printers_picture():
    show = fn_src("showBrowserNotification")
    assert "bambu-x1c.png" not in show
    assert "icon: pr ? imgFor(pr) : \"/pwa/icon-192.png\"" in show and "findPrinter(pid)" in show


def test_no_port_and_no_magic_numbers_in_the_texts_and_timers():
    assert ":9999" not in JS
    assert "Date.now() - start > PRN_RESTART_MAX_MS" in JS and "> 90000" not in JS
    assert "tabState.lastOkAt > LIVE_DOWN_MS" in JS and "> 30000" not in JS
    assert "Date.now() - fleetEvAt > FLEET_EV_STALE_MS" in JS and "2 * 60 * 1000) fleetLoadSince" not in JS
    assert "setTimeout(loadServerSettings, 4000)" not in JS and "SRV_BOT_RECHECK_MS = [4000, 28000]" in JS


def test_number_and_date_formats_follow_the_panel_language():
    calls = re.findall(r"toLocale(?:Time|Date)?String\((.{0,60})", JS)
    assert len(calls) == 8 and all(      # 8: «Пластик»'s grams (17.11) too
        c.startswith(("uiLocale()", 'typeof I18N === "object" ? I18N.locale() : "ru-RU"'))
                                   for c in calls), calls
    assert not re.search(r"toLocale\w*\(\s*['\"]ru", JS)
    got = node(r"""
const api = load(["uiLocale", "filamentGramsText"], []);
const ru = [api.uiLocale(), api.filamentGramsText(218.34)];
globalThis.I18N = { locale: () => "en-GB" };
out(ru.concat([api.uiLocale(), api.filamentGramsText(218.34)]));""")
    assert got == ["ru-RU", "≈ 218,3 г", "en-GB", "≈ 218.3 г"]


# --- ui.md 1, 8: «История → Печати» on a phone ------------------------------------------------

def test_phone_journal_row_gives_the_job_name_a_line_and_places_the_grams():
    phone = re.findall(r"@media \(max-width: 640px\) \{(.*?)\n    \}", CSS, re.S)
    jobs = next(b for b in phone if ".jobs-table" in b)
    assert 'grid-template-areas: "printer when state" "name name name" "dur mat pauses";' in jobs
    tablet = next(b for b in re.findall(r"@media \(max-width: 1100px\) \{(.*?)\n    \}", CSS, re.S) if ".jobs-table" in b)
    assert '"printer when name state" "dur dur mat pauses"' in tablet
    assert "minmax(7em, 1fr)" in tablet                          # the name never comes down to 0 px
    assert ".jobs-table td.job-material { grid-area: mat;" in tablet           # was a 4th line of its own
    assert ".jobs-table td.job-printer { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }" in tablet
    row = node(r"""
const api = load(["jobRowHtml", "filamentGramsText", "jobPausesCell", "timeFmt", "safeText"], ["JOB_COLS"], `
  function jobWhenHtml() { return ""; } function jobDurCellHtml() { return ""; } function jobStateHtml() { return ""; }
  function jobLayersText() { return ""; } function previewButton() { return ""; } function jobShortName(s) { return s; }
  function jobReasonText() { return ""; }`);
out([api.jobRowHtml({ id: 1, label: "P1", planned_filament_g: null }), api.jobRowHtml({ id: 2, label: "P1", planned_filament_g: 12 })]);""")
    assert 'class="job-material job-nil"' in row[0] and 'class="job-material"' in row[1]


# --- ui.md 2: the card's name -----------------------------------------------------------------

def test_cut_card_name_takes_its_own_line_where_that_costs_nothing():
    fit = fn_src("cardNameFit")
    assert "nm.scrollWidth > row.clientWidth - right.getBoundingClientRect().width - gap" in fit
    assert 'var free = oneCol || !card.querySelector(".pc-subrow");' in fit
    assert "cardNameFit(cards);" in fn_src("balanceChips")
    assert ".pc-toprow[data-wrap] { flex-wrap: wrap; row-gap: 4px; }" in CSS


# --- ui.md 6, 7, 10, 13, 14 -------------------------------------------------------------------

def test_hidden_rows_and_buttons_stay_hidden():
    assert ".me-row[hidden], .jobs-more[hidden] { display: none; }" in CSS
    assert CSS.index(".me-row { display: flex;") < CSS.index(".me-row[hidden]")


def test_phone_touch_targets_are_40_px():
    block = CSS[CSS.index("/* touch targets on a phone (QA 17.11)"):]
    for sel in (".farm-chip", ".fr-toggle", ".hist-help", ".adm-link", "#adminPrintersTable .ghost-btn", "#tgTokenEye"):
        assert sel in block, sel
    assert "inset: min(0px, calc(50% - 20px)) min(0px, calc(50% - 20px));" in block
    assert ".nf-x { width: 40px; height: 40px; }" in block
    # the chips' rows: 34 + 2 × 3 = 40 px each, the 6 px gap shared, no row's area over the next one's
    assert ".farm-chip { min-height: 34px; }" in block and ".farm-chips { row-gap: 6px; }" in block
    assert ".farm-chip::after { inset: -3px min(0px, calc(50% - 20px)); }" in block
    # ui.md 14: «Сушка выклю…» at 360 px — the AMS row's line takes a second line in its 60 px
    assert re.search(r"\.ams-row-s \{ white-space: normal;[^}]*-webkit-line-clamp: 2;", block)
    assert 'button.nf-badge::after { content: ""; position: absolute; inset: -6px -3px; }' in CSS
    assert ".pc-ctl.pc-ctl-head::before { inset: -6px -8px; }" in CSS


def _hex(c):
    c = c.lstrip("#")
    return [int(c[i:i + 2], 16) / 255 for i in (0, 2, 4)]


def _lum(rgb):
    f = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in rgb]
    return 0.2126 * f[0] + 0.7152 * f[1] + 0.0722 * f[2]


def test_light_finished_badge_reads_at_4_5():
    light = CSS[CSS.index('html[data-theme="light"] {'):]
    tok = dict(re.findall(r"--(ok-\d+): (#[0-9a-f]{6})", light[:light.index("\n    }")]))
    assert 'html[data-theme="light"] .status-badge.finished { background: var(--ok-2); }' in CSS
    fg, bg = _lum(_hex(tok["ok-11"])), _lum(_hex(tok["ok-2"]))
    assert (bg + 0.05) / (fg + 0.05) >= 4.5                       # was 4.24 on --ok-3
    assert re.search(r'html\[data-theme="glass"\] \.status-badge:is\(\.cancelled, \.offline\),', CSS)


def test_lock_note_wraps_instead_of_being_cut():
    assert re.search(r"\.pc-ctl-lock > span \{[^}]*white-space: normal;[^}]*-webkit-line-clamp: 2;", CSS)


def test_short_and_raw_words_are_said_in_full():
    assert "долгие HMS" not in JS and "RH — до сушки" not in HTML
    assert "#adminPrintersTable .act-tag { white-space: normal;" in CSS


# --- ui.md 9: units -----------------------------------------------------------------------------

def test_ams_screen_writes_percent_and_degrees_one_way():
    nb = "\u00a0%"
    for line in (fn_src("amsLegendHtml"), fn_src("amsCardShell")):
        assert nb not in line
    assert '" °C"' not in fn_src("dryStatusText")


# --- ui.md 11, 12 -----------------------------------------------------------------------------

def test_card_menu_names_its_printer():
    got = node(r"""
const api = load(["ctlMenuHtml", "safeText"], [], `function maintMenuHtml() { return ""; } function icon() { return ""; }`);
out(api.ctlMenuHtml({ label: "P2S · Сушка", kind: "bambu" }, null, 0, { admin: false }));""")
    assert got.startswith('<div class="pm-title" translate="no">P2S · Сушка</div>')


def test_offline_card_says_since_when():
    got = node(r"""
const api = load(["offlineSinceText", "fleetDur", "timeFmt"], []);
out([api.offlineSinceText(null), api.offlineSinceText(7500), api.offlineSinceText(Infinity)]);""")
    assert got == ["Нет связи с принтером", "Нет связи · 2ч 5м", "Нет связи · больше суток"]
    assert "offlineSinceText(fleetSinceAge(fleetSince, p, Date.now() / 1000))" in JS


# --- hunt 2: «Печатей в день» in the browser's days -------------------------------------------

def test_day_stats_ask_in_the_browsers_time_zone():
    got = node(r"""
const api = load(["histTzQuery", "histWindow", "histDayStart", "histFilterFrom", "histOwn", "histPeriodOf"], ["HIST_PERIODS", "HIST_DEFAULT_PERIOD"]);
const w = api.histWindow({ period: "custom", from: "2026-11-16", to: "2026-11-16" }, Date.UTC(2026, 10, 18) / 1000);
const local0 = new Date(2026, 10, 16).getTime() / 1000;
out({ tz: api.histTzQuery(), w: [w.fr - local0, w.to - w.fr] });""", env={"TZ": "Asia/Omsk"})
    assert got["tz"] == "&tz=Asia%2FOmsk"
    assert got["w"] == [0, 86400]                                  # one local day, midnight to midnight
    assert "jobsFilterQuery({ fr: w.fr, to: w.to, printer: histFilter.printer }, now, false) + histTzQuery();" in JS


# --- hunt 33, 34, 35, 42 ----------------------------------------------------------------------

def test_card_humidity_colour_is_the_ams_tabs():
    got = node(r"""
const api = load(["amsChip", "dryActive", "safeText", "amsZones", "amsHumZone"], ["AMS_WARN_SPAN"],
  "function icon(n){return '';} function amsFmtMin(m){return m+' мин';}");
const p = { ams: { tray_now: null, units: [{ index: 0, humidity_pct: 22, humidity: 2, temp: 24,
  slots: [{ empty: false, type: "PLA", color: "FFFFFF", remain_pct: 80 }] }] } };
const z = api.amsZones({ target_rh: 20 });
out([/pc-ams-hum-(\w+)/.exec(api.amsChip(p, false, true, z))[1], api.amsHumZone(22, z)]);""")
    assert got[0] == got[1] == "warn"
    assert "amsChip(p, canAms, true, amsZonesNow())" in JS


def test_last_minute_of_a_print_is_not_a_dash():
    got = node(r"""
const api = load(["jobStateHtml", "jobLiveState", "timeFmt", "jobClock", "jbPad", "safeText"], [], "var JOB_RESULTS = {};");
out(api.jobStateHtml({ result: null, started_at: 1000, start_exact: true },
  { online: true, state: "printing", progress_pct: 99, eta_seconds: 45 }, 5000));""")
    assert "осталось —" not in got and "осталось < 1м" in got


def test_blocked_notifications_on_https_are_not_blamed_on_http():
    got = node(r"""
globalThis.window = { location: { protocol: "https:", hostname: "farm.example", port: "8443", host: "farm.example:8443" },
                      Notification: { permission: "denied" } };
globalThis.Notification = window.Notification;
const api = load(["updateNotificationSetting"], [], `
  function loadNotificationSettings(){ return {enabled:false}; } function saveNotificationSettings(){}
  function syncNotificationControls(){} function nfPermReply(t){ globalThis.__said = t; }`);
await api.updateNotificationSetting("enabled", true);
const https = globalThis.__said;
window.location = { protocol: "http:", hostname: "192.168.1.5", port: "8000", host: "192.168.1.5:8000" };
await api.updateNotificationSetting("enabled", true);
out([https, globalThis.__said]);""")
    assert "через HTTP" not in got[0] and "запрещены в браузере" in got[0]
    assert got[1] == "Уведомления не работают через HTTP. Откройте https://192.168.1.5"


def test_tab_percent_leaves_out_a_printer_in_maintenance():
    got = node(r"""
const api = load(["tabSummary", "tabTitle", "computeUiStatus", "isOffline", "hasTelemetry"], []);
const A = { id: "a", online: true, state: "printing", progress_pct: 10, eta_seconds: 600, nozzle_temp: 220, maintenance: { note: "тест" } };
const B = { id: "b", online: true, state: "printing", progress_pct: 80, eta_seconds: 3600, nozzle_temp: 220 };
out(api.tabTitle(api.tabSummary([A, B], api.computeUiStatus, () => false), "Кузница", false));""")
    assert got == "80% · Кузница"


# --- hunt 44: «Дальше» of a printer without link ----------------------------------------------

def test_finish_clock_of_a_grace_printer_stays_put():
    got = node(r"""
const api = load(["graceEta", "fleetFinishes", "finishRowHtml", "finClock", "fleetDur", "timeFmt", "safeText", "inMaint",
                  "isOffline", "hasTelemetry", "fleetSinceAge", "fleetStateKey", "fleetByName"], ["FIN_NEXT_SHOWN"]);
const t0 = Date.UTC(2026, 10, 17, 8, 0, 0) / 1000;
// the server repeats the last good status (eta 3600 s, fresh at t0 - 30) through the grace period
const P = { id: "p", label: "P1", online: true, state: "printing", progress_pct: 40, eta_seconds: 3600,
            nozzle_temp: 220, grace_period_active: true, last_successful_fetch: t0 - 30 };
function clock(now) {
  const p = api.graceEta(Object.assign({}, P), now);
  const html = api.finishRowHtml(api.fleetFinishes([p], {}, now), { open: false, hours: 12, phone: false, now: now });
  return /<span class="fin-t">([^<]*)<\/span>/.exec(html)[1];
}
const live = api.graceEta(Object.assign({}, P, { grace_period_active: false }), t0 + 120).eta_seconds;
out([clock(t0), clock(t0 + 120), live]);""")
    assert got[0] == got[1]                                        # was 2 min later after 2 min
    assert got[2] == 3600                                          # a live status is left as it is
    assert "cachedAllPrinters = data.map((p) => graceEta(Object.assign({}, p), Date.now() / 1000));" in JS


# --- hunt 45: «Хронология»'s axis on the local clock ------------------------------------------

AXIS = r"""
const extra = `
  var labels = [];
  var ctxStub = new Proxy({}, { get: function (t, k) {
    if (k === "fillText") return function (s) { labels.push(String(s)); };
    if (k === "measureText") return function () { return { width: 10 }; };
    if (k === "createLinearGradient" || k === "createPattern") return function () { return { addColorStop: function () {} }; };
    if (k in t) return t[k];
    return function () {};
  }, set: function (t, k, v) { t[k] = v; return true; } });
  var canvasStub = { offsetWidth: 1400, offsetHeight: 300, width: 0, height: 0, getContext: function () { return ctxStub; } };
  var document = { getElementById: function (id) { return id === "timeline-canvas" ? canvasStub : null; } };
  var window = { devicePixelRatio: 1 };
  function getTimelineColors() { return {}; }
  function makeOfflinePattern() { return null; } function makeMaintPattern() { return null; }
  function tlLabelW() { return 200; } function updateScaleLabel() {}
  var STATE_COLORS = { idle: "#888888" };
  var I18N; var tickStep = 0; var printerMap = {};
  var end = new Date(); end.setHours(0, 0, 0, 0);
  var viewEnd = end.getTime() / 1000, viewStart = viewEnd - 7 * 86400;
  var timelineRange = { from: viewStart, to: viewEnd };
  var timelineData = [{ printer_id: "P1", label: "P1", state: "idle", recorded_at: viewStart + 1 },
                      { printer_id: "P1", label: "P1", state: "idle", recorded_at: viewEnd - 1 }];
  globalThis.__axis = function () {
    labels = []; drawTimeline();
    return labels.filter(function (s) { return /\\d\\d:\\d\\d/.test(s); });
  };`;
load(["drawTimeline", "timelineBounds", "autoTickStep", "lightenColor", "tlTzOff"], ["HISTORY_NICE_TICKS"], extra);
out([...new Set(globalThis.__axis().map((s) => s.slice(-5)))]);
"""


@pytest.mark.parametrize("tz", ["Asia/Omsk", "Europe/Moscow"])
def test_timeline_ticks_fall_on_the_local_midnight(tz):
    hours = node(AXIS, env={"TZ": tz})
    assert "00:00" in hours, hours                                  # were 07:00 / 19:00 (Omsk), 03:00 / 15:00 (Moscow)


# --- hunt 46, 47: the AMS tab ------------------------------------------------------------------

def test_ams_temperature_chart_holds_a_70_degree_drying():
    got = node(r"""
const api = load(["amsChart", "amsChartOverlay", "amsTempRange"], ["AMS_GW"], `
  var amsHours = 24;
  function safeText(s, d) { return s == null ? d : String(s); }
  function adNum(v) { return String(v); }`);
const pts = [{ t: 3600, v: 26 }, { t: 7200, v: 70 }, { t: 10800, v: 70 }, { t: 14400, v: 28 }];
const r = api.amsTempRange(pts.map((p) => p.v));
const svg = api.amsChart(pts, 0, 24 * 3600, r[0], r[1], "var(--info)", { dry: [[7200, 10800]] });
const line = /<path d="([^"]*)" fill="none"/.exec(svg)[1];
const ys = [...line.matchAll(/[ML]([\d.-]+) ([\d.-]+)/g)].map((m) => +m[2]);
out({ r, ys, h: +/viewBox="0 0 (\d+) (\d+)"/.exec(svg)[2], cold: api.amsTempRange([12, 30]) });""")
    assert got["r"] == [20, 70] and got["cold"] == [10, 60]
    assert all(0 <= y <= got["h"] for y in got["ys"]), got        # was y = −8 above the chart
    assert "amsChart(tempPts, fr, to, tr[0], tr[1], 'var(--info)', { dry: bands })" in JS


def test_ams_tab_draws_only_the_period_it_shows():
    got = node(r"""
const api = load(["loadAms"], ["amsLoadGen"], `
  var amsHours = 720, amsAutodry = null, amsUnitsShown = [], draws = [], pending = [];
  function amsInit() {} function autodryRender() {} function amsUnitOrder() { return 0; } function amsCardShell() { return ""; }
  var document = { getElementById: function (id) { return id === "amsGrid" ? { innerHTML: "" } : id === "amsEmpty" ? { style: {} } : null; } };
  function amsDrawGraphs(key, u, rows, fr, to) { draws.push({ hoursOfData: Math.round((to - fr) / 3600), amsHoursNow: amsHours }); }
  function apiFetch(url) {
    if (url.indexOf("/api/ams/current") === 0) return Promise.resolve({ ok: true, json: function () { return [{ printer_id: "H1", unit_index: 0 }]; } });
    if (url.indexOf("/api/ams/autodry") === 0) return Promise.resolve({ ok: false });
    return new Promise(function (res) { pending.push(function () { res({ json: function () { return { rows: [] }; } }); }); });
  }
  globalThis.__a = { draws: () => draws, pending: () => pending, set: (h) => { amsHours = h; } };`);
const S = globalThis.__a;
api.loadAms(); await tick();                 // «30 дн»: its slow history on its way
S.set(24); api.loadAms(); await tick();      // «24 ч» pressed
S.pending()[1](); await tick();              // the 24 h answer first
S.pending()[0](); await tick();              // the 30-day one after it
out(S.draws());""")
    assert got == [{"hoursOfData": 24, "amsHoursNow": 24}]


# --- hunt 48: «Хронология»'s paging after a new filter ----------------------------------------

def test_timeline_paging_goes_on_after_the_filter_changes():
    got = node(r"""
const api = load(["loadTimeline", "loadMoreTimeline"], [], `
  var timelineLimit = 2, timelineTotal = 0, timelineHasMore = false, timelineLoadGen = 0, timelineLoadingMore = false;
  var timelineData = [], timelineRange = { from: null, to: null }, timelineOffset = 0, timelinePrinter = null;
  var histFilter = { period: "7d", printer: null }, reqs = [];
  var document = { getElementById: function () { return null; } };
  function histChronoWindow() { return { fr: 1000, to: 9000, clipped: false, days: 1 }; }
  function histChronoNote() { return ""; } function histLoadingText() { return ""; }
  function applyScale() {} function drawTimeline() {}
  function apiFetch(url) {
    return new Promise(function (res) {
      reqs.push({ url: url, go: function (rows, more) { res({ json: function () { return { rows: rows, has_more: more, total: 0 }; } }); } });
    });
  }
  globalThis.__t = { reqs: () => reqs, setPrinter: (p) => { histFilter.printer = p; } };`);
const S = globalThis.__t, R = S.reqs;
api.loadTimeline(); await tick();
R()[0].go([{ recorded_at: 8 }, { recorded_at: 9 }], true); await tick();     // page 1; page 2 goes out
S.setPrinter("P1"); api.loadTimeline(); await tick();                        // a printer picked meanwhile
R()[2].go([{ recorded_at: 7 }, { recorded_at: 8 }], true); await tick();     // P1's page 1, more to come
R()[1].go([{ recorded_at: 5 }, { recorded_at: 6 }], true); await tick();     // the old page 2: dropped
out(R().map((r) => r.url.replace(/.*\?/, "")).filter((u) => /printer=P1/.test(u)));""")
    assert any("offset=2" in u for u in got), got                  # was stuck on its first page


# --- the coordinator's two: «Добавить» пользователя -------------------------------------------

ADD_USER = r"""
const els = {};
function el(id, value) { els[id] = { id, value: value || "", textContent: "", hidden: false, disabled: false, setAttribute() {} }; return els[id]; }
el("newUsername", "dana"); el("newPassword", "parol-dlya-dany"); el("newUserRole", "viewer"); el("adminError");
el("addUserForm"); el("addUserOpen");
globalThis.document = { getElementById: (id) => els[id] || null };
let posts = 0, answer = null;
const api = load(["admAddUser", "admPwError"], ["PW_MIN_CHARS"], `
  function showToast() {} function loadAdminUsers() {}
  function apiFetch() { globalThis.__posts++; return new Promise((r) => { globalThis.__answer = r; }); }`);
globalThis.__posts = 0;
const btn = { disabled: false };
api.admAddUser(btn); api.admAddUser(btn);                     // a double press
const busy = btn.disabled, during = globalThis.__posts;
globalThis.__answer({ ok: true, json: () => Promise.resolve({}) }); await tick();
const pw = ["short", "a".repeat(72), "a".repeat(73), "я".repeat(36), "я".repeat(37)].map(api.admPwError);
els.newPassword.value = "я".repeat(40); els.newUsername.value = "eve";
api.admAddUser(btn);
out({ busy, during, after: btn.disabled, pw, err: els.adminError.textContent, posts: globalThis.__posts });
"""


def test_add_user_sends_once_and_checks_72_bytes():
    got = node(ADD_USER)
    assert got["busy"] is True and got["during"] == 1 and got["after"] is False
    long_ = "Пароль — не длиннее 72 байт: 72 латинских знака или 36 русских букв"
    assert got["pw"] == ["Пароль — не меньше 8 символов", None, long_, None, long_]
    assert got["err"] == long_ and got["posts"] == 1              # 40 Cyrillic letters: refused before the POST
    assert "var pwBad = admPwError(pass);" in JS                  # «Сменить пароль» checks the same
