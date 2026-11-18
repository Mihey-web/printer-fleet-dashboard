"""Fixes of the QA findings of 17.11.2026 (docs/dev/qa-2026-11-17, part C2): the job card,
«Файлы на принтере», the K2's «Сервис», the shared file pool, the login page and the English
dictionary. Each test is the tester's proof turned around: it failed before the fix.

* code.md Н4: the job card and its server words are in the translation check and translated;
* code.md Н10, hunt.md 19: no «1ч 60м»;
* code.md Н12: the file pool says when its list or the copy's progress could not be read;
* code.md Н13: no dead printerName in file-pool.js;
* code.md Н14: one HTML escaping; the K2's service list and its descriptions from the server;
  numbers and dates in the interface's language, not 'ru-RU';
* ui.md 3: the login page's errors in the interface's language;
* hunt.md 13, 41, 43: a late answer of another printer or a removed file does not stay on screen.
The JS parts run in node (skipped without node), with a fake DOM just big enough for them.
"""
import importlib.util
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "app" / "web"
CYR = re.compile(r"[А-Яа-яЁё]")


def _source(name):
    return (WEB / name).read_text(encoding="utf-8")


def _node(src):
    """Run a node script; its last stdout line is JSON."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    out = subprocess.run([node, "-"], input=src, capture_output=True, text=True, timeout=60, encoding="utf-8")
    assert out.returncode == 0, out.stderr + out.stdout
    return json.loads(out.stdout.strip().splitlines()[-1])


def _extractor():
    spec = importlib.util.spec_from_file_location("i18n_extract", ROOT / "scripts" / "i18n_extract.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# the translation runtime and the English dictionary in node: no DOM, the lookup only
I18N_LIB = ("globalThis.window = undefined;\n" + _source("i18n.js") + "\n"
            + (WEB / "i18n" / "en.js").read_text(encoding="utf-8") + "\n")


def _tr(texts):
    return _node(I18N_LIB + "console.log(JSON.stringify(%s.map(function (s) { return I18N.tr('en', s); })));"
                 % json.dumps(texts, ensure_ascii=False))


# --- Н4: the job card in the translation check ----------------------------------------------------

SERVER_WORDS = ["app/services/project_summary.py", "app/services/creality_parts.py",
                "app/services/creality_features.py", "app/clients/klipper_client.py",
                "app/clients/creality_client.py"]


def test_the_translation_check_reads_the_job_card_and_its_server_words():
    """tests/test_i18n.py checks what i18n_extract.collect() finds: preview.js and the modules
    whose words reach the job card were not in it, so the card stayed Russian unnoticed."""
    m = _extractor()
    files = {r["file"] for r in m.collect()}
    assert "app/web/preview.js" in files
    assert set(SERVER_WORDS) <= set(m.SERVER_FILES) and set(SERVER_WORDS) <= files


def test_job_card_js_strings_are_in_the_english_dictionary():
    m = _extractor()
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    rows = [{"file": "app/web/preview.js", "line": line, "text": p}
            for line, s in m.js_strings(_source("preview.js")) for p in m.pieces(s)]
    miss = m.uncovered(rows, m.load_dictionary("en"))
    assert rows and [r["text"] for r in miss] == []


@pytest.mark.parametrize("rel", SERVER_WORDS)
def test_server_strings_that_reach_the_panel_are_in_the_english_dictionary(rel):
    m = _extractor()
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    rows = [{"file": rel, "line": line, "text": p}
            for line, s in m.py_strings((ROOT / rel).read_text(encoding="utf-8"))
            for part in m._py_format_parts(s) for p in m.pieces(part.replace("%%", "%"))]
    miss = m.uncovered(rows, m.load_dictionary("en"))
    assert rows and [r["text"] for r in miss] == []


# a K2 Plus job with every fact the card shows; the names are the user's own (Latin here, so any
# Cyrillic left in the English card is the panel's own text)
FULL_CARD = {"title": "Bracket", "time_s": 7190, "grams": 352.54,
             "filaments": [{"type": "PETG", "color": "#FFF144", "grams": 352.54, "meters": 117.26,
                            "profile": "Generic PETG @K2", "nozzle_temp": 245.0}],
             "objects": [{"name": "Bracket", "count": 2, "skipped": 1}, {"name": "Cover", "count": 1}],
             "nozzle_mm": 0.4, "layer_mm": 0.2, "first_layer_mm": 0.25, "infill": "15%", "infill_pattern": "гироид",
             "walls": 3, "supports": "древовидные, авто", "support_grams": 4.4, "bed": "текстурированный PEI",
             "bed_temp": 70.0, "size_mm": [99.4, 171.9, 228.0], "pauses": 2, "by_object": True, "ironing": True,
             "timelapse": True, "slicer": "Creality Print 6.2"}


def test_job_card_reads_in_english():
    """What the browser shows in English: preview.js's markup, each text node and attribute through
    the real translator (app/web/i18n.js + en.js) — no Russian left."""
    shown = _node(I18N_LIB + """
I18N.set('en');
globalThis.window = {I18N: globalThis.I18N}; globalThis.document = {addEventListener() {}};
""" + _source("preview.js") + """
var html = window.ForgePreview.markup(%s, 'Bracket', false, '');
var un = function (s) { return s.replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&'); };
var texts = html.split(/<[^>]*>/).map(un).filter(function (s) { return s.trim(); });
console.log(JSON.stringify(texts.map(function (s) { return [s, I18N.tr('en', s)]; })));
""" % json.dumps(FULL_CARD, ensure_ascii=False))
    russian = [pair for pair in shown if CYR.search(pair[1])]
    assert russian == []
    out = dict(shown)
    assert out["Время по расчёту"] == "Estimated time" and out["2ч 0м"] == "2h 0m"
    assert out["15%, гироид"] == "15%, gyroid" and out["древовидные, авто · 4.4 г"] == "tree, auto · 4.4 g"
    assert out["текстурированный PEI, 70°"] == "Textured PEI Plate, 70°"
    assert out["0.2 мм, первый 0.25"] == "0.2 mm, first 0.25" and out["99 × 172 × 228 мм"] == "99 × 172 × 228 mm"
    assert out["Детали на столе "] == "Parts on the plate " and out["3 штуки"] == "3 pieces"


def test_plate_map_lines_read_in_english():
    """The plate map's lines are written while the admin picks parts (skipBar in preview.js)."""
    assert _tr(["Пропустить 3 детали", "Пропустить 21 деталь", "Пропустить 1 деталь",
                "Пропущено: 2 (серые). Выбрано: 1", "Нажмите на деталь, чтобы выбрать её для пропуска",
                "Bracket · выбрана", "Bracket · пропущена", "Bracket · печатается сейчас",
                "Точно пропустить? Нажмите ещё раз", "Детали на столе", "21 штука",
                "Bracket + ещё 3", "Корпус + ещё 3", "K2-01 · пластина 2"]) == [
        "Skip 3 parts", "Skip 21 parts", "Skip 1 part",
        "Skipped: 2 (grey). Selected: 1", "Tap a part to select it for skipping",
        "Bracket · selected", "Bracket · skipped", "Bracket · printing now",
        "Skip for sure? Press again", "Parts on the plate", "21 pieces",
        "Bracket + 3 more", "Корпус + 3 more", "K2-01 · plate 2"]


# --- Н10 / hunt 19: «1ч 60м» -----------------------------------------------------------------------

def _card(summary, window="{}"):
    return _node("globalThis.window = %s; globalThis.document = {addEventListener() {}};\n" % window
                 + _source("preview.js")
                 + "\nconsole.log(JSON.stringify(window.ForgePreview.markup(%s, 'x', false, '')));"
                 % json.dumps(summary, ensure_ascii=False))


@pytest.mark.parametrize("seconds,shown", [(7190, "2ч 0м"), (7170, "2ч 0м"), (3590, "1ч 0м"), (35990, "10ч 0м"),
                                           (43198, "12ч 0м"), (3660, "1ч 1м"), (59, "1м")])
def test_job_card_time_never_shows_60_minutes(seconds, shown):
    found = re.search(r"Время по расчёту</small><span>([^<]*)<", _card({"time_s": seconds}))
    assert found and found[1] == shown


def test_printer_files_time_never_shows_60_minutes():
    # hunt-repros/p0__preview_duration.test.js: the one-line `const duration =` of printer-files.js
    line = next(x for x in _source("printer-files.js").split("\n") if "const duration =" in x)
    expr = line[line.index("=") + 1:].strip().rstrip(";")
    assert _node("console.log(JSON.stringify([7170, 7190, 3590, 3660].map(%s)));" % expr) == [
        "2 ч 0 мин", "2 ч 0 мин", "1 ч 0 мин", "1 ч 1 мин"]


# --- the dialog modules in node --------------------------------------------------------------------

# p8b__fake_dialog.js of the hunt: the module with a fake <dialog>; requests answered by hand
FAKE_DIALOG = r"""
function loadDialogModule(src, window) {
  let dlg = null;
  function fakeDialog() {
    const l = {};
    return {
      open: false, innerHTML: "", listeners: l,
      setAttribute() {}, addEventListener(t, f) { (l[t] = l[t] || []).push(f); },
      showModal() { this.open = true; },
      close() { this.open = false; (l.close || []).forEach(f => f()); },
      querySelector() { return null; }
    };
  }
  window = window || {};
  const document = { createElement: () => (dlg = fakeDialog()), body: { append() {} } };
  new Function("window", "document", src)(window, document);
  const pending = {}, asked = [];
  function fetcher(url) {
    asked.push(url);
    let resolve, reject;
    const p = new Promise((r, j) => { resolve = r; reject = j; });
    pending[url] = { resolve: (body, status) => resolve({ ok: !status || status < 400, status: status || 200, json: async () => body }),
                     reject: e => reject(e) };
    return p;
  }
  function click(sel, dataset) {
    const t = { closest: s => (s === sel ? { dataset: dataset || {}, disabled: false } : null) };
    (dlg.listeners.click || []).forEach(f => f({ target: t }));
  }
  const flush = () => new Promise(r => setTimeout(r, 0));
  return { window, dlg: () => dlg, fetcher, pending, asked, click, flush };
}
"""


def _dialog(module, body, window="{}"):
    return _node(FAKE_DIALOG + "const m = loadDialogModule(%s, %s);\n(async () => {\n%s\n})().then(() => process.exit(0),"
                 " e => { console.error(e); process.exit(1); });" % (json.dumps(_source(module)), window, body))


# the shared file pool (file-pool.js mounts into a root element); setInterval is driven by hand
FILE_POOL = r"""
const timers = [];
globalThis.setInterval = (f, ms) => { timers.push(f); return timers.length; };
globalThis.clearInterval = id => { if (id) timers[id - 1] = null; };
const window = %(window)s;
new Function("window", %(src)s)(window);
const L = {}, root = { innerHTML: "", addEventListener(t, f) { L[t] = f; }, querySelector() { return null; } };
const pending = {}, bodies = {};
function fetcher(url, opts) {
  bodies[url] = opts && opts.body;
  return new Promise((r, j) => { pending[url] = { ok: (b, status) => r({ ok: !status || status < 400, status: status || 200, json: async () => b }),
                                                  fail: e => j(e) }; });
}
const tgt = (sel, ds, extra) => ({ target: { closest: s => (s === sel ? Object.assign({ dataset: ds }, extra) : null) } });
const flush = () => new Promise(r => setTimeout(r, 0));
const GROUP = { key: "g", kind: "bambu", title: "P1S", printers: [{ id: "p1", label: "P1" }], syncable: true };
const POOL = files => ({ printers: [{ id: "p1", label: "P1", listed: true }], files });
(async () => {
%(body)s
})().then(() => process.exit(0), e => { console.error(e); process.exit(1); });
"""


def _file_pool(body, window="{}"):
    return _node(FILE_POOL % {"window": window, "src": json.dumps(_source("file-pool.js")), "body": body})


# --- hunt 13: «Файлы на принтере» of printer B shows A's late list ------------------------------------

def test_printer_files_late_list_of_another_printer_is_dropped():
    out = _dialog("printer-files.js", """
  m.window.ForgeFiles.open({ id: "A", label: "Принтер-A", kind: "creality" }, m.fetcher);
  m.dlg().close();                                            // closed while A's list loads
  m.window.ForgeFiles.open({ id: "B", label: "Принтер-B", kind: "creality" }, m.fetcher);
  m.pending["/api/admin/printers/B/files"].resolve({ kind: "creality", files: [{ name: "b.gcode", title: "ФАЙЛ-B", path: "/b.gcode" }] });
  await m.flush();
  m.pending["/api/admin/printers/A/files"].resolve({ kind: "creality", files: [{ name: "a.gcode", title: "ФАЙЛ-A", path: "/a.gcode" }] });
  await m.flush();
  const html = m.dlg().innerHTML;
  m.click("[data-pf-file]", { pfFile: "0" });
  console.log(JSON.stringify({ b: html.includes("ФАЙЛ-B"), a: html.includes("ФАЙЛ-A"), next: m.asked[m.asked.length - 1] }));
""")
    assert out == {"b": True, "a": False, "next": "/api/admin/printers/B/files/detail?path=%2Fb.gcode"}


def test_printer_files_list_error_of_another_printer_is_dropped():
    out = _dialog("printer-files.js", """
  m.window.ForgeFiles.open({ id: "A", label: "Принтер-A", kind: "creality" }, m.fetcher);
  m.dlg().close();
  m.window.ForgeFiles.open({ id: "B", label: "Принтер-B", kind: "creality" }, m.fetcher);
  m.pending["/api/admin/printers/B/files"].resolve({ kind: "creality", files: [{ name: "b.gcode", title: "ФАЙЛ-B", path: "/b.gcode" }] });
  await m.flush();
  m.pending["/api/admin/printers/A/files"].resolve({ detail: "ОШИБКА-A" }, 502);
  await m.flush();
  console.log(JSON.stringify({ b: m.dlg().innerHTML.includes("ФАЙЛ-B"), a: m.dlg().innerHTML.includes("ОШИБКА-A") }));
""")
    assert out == {"b": True, "a": False}


USB_WARNING = "Не удалось прочитать список файлов на флешке принтера — повторите через минуту"


def test_printer_files_shows_the_servers_warning_above_the_list():
    """The server may list the internal files while the USB stick could not be read: its
    `warning` stands above the list, also after a file was opened and closed, until a list without one."""
    out = _dialog("printer-files.js", """
  const files = [{ name: "a.gcode", title: "ФАЙЛ-A", path: "/a.gcode" }];
  m.window.ForgeFiles.open({ id: "A", label: "A", kind: "creality" }, m.fetcher);
  m.pending["/api/admin/printers/A/files"].resolve({ kind: "creality", files, warning: %s });
  await m.flush();
  const first = m.dlg().innerHTML;
  m.click("[data-pf-file]", { pfFile: "0" });
  m.pending["/api/admin/printers/A/files/detail?path=%%2Fa.gcode"].resolve({});
  await m.flush();
  m.click("[data-pf-back]");
  const back = m.dlg().innerHTML;
  m.click("[data-pf-reload]");
  m.pending["/api/admin/printers/A/files"].resolve({ kind: "creality", files });
  await m.flush();
  console.log(JSON.stringify({ first, back, again: m.dlg().innerHTML }));
""" % json.dumps(USB_WARNING, ensure_ascii=False))
    for html in (out["first"], out["back"]):
        assert '<p class="pf-note" role="status">' + USB_WARNING + "</p>" in html
        assert html.index(USB_WARNING) < html.index("ФАЙЛ-A")
    assert USB_WARNING not in out["again"] and "ФАЙЛ-A" in out["again"]
    assert _tr([USB_WARNING]) == ["Could not read the file list from the printer's USB stick — try again in a minute"]


# --- hunt 41 + Н14: the K2's «Сервис» --------------------------------------------------------------

ACTIONS_JS = json.dumps([{"key": "svc_bed_mesh", "title": "Калибровка стола", "about": "ОПИСАНИЕ-С-СЕРВЕРА"},
                         {"key": "svc_fw_restart", "title": "Перезапуск прошивки", "about": "около минуты"}],
                        ensure_ascii=False)


def test_service_late_answer_of_another_printer_is_dropped():
    out = _dialog("printer-service.js", """
  const ACTIONS = %s;
  const k2 = id => ({ id, label: "K2-" + id, kind: "creality", klipper_api: true, online: true, state: "idle" });
  m.window.ForgeService.open(k2("A"), m.fetcher);
  m.dlg().close();
  m.window.ForgeService.open(k2("B"), m.fetcher);
  m.pending["/api/admin/printers/B/service"].resolve({ job: null, lines: [], klipper: true, actions: ACTIONS });
  await m.flush();
  const before = m.dlg().innerHTML;
  m.pending["/api/admin/printers/A/service"].resolve({ job: { state: "running", title: "Калибровка стола" }, lines: ["КОНСОЛЬ-A"],
                                                      klipper: true, actions: ACTIONS });
  await m.flush();
  const html = m.dlg().innerHTML;
  console.log(JSON.stringify({ buttons: (before.match(/data-ps="/g) || []).length, disabledBefore: before.includes(" disabled"),
                               header: html.includes("K2-B"), console: html.includes("КОНСОЛЬ-A"), disabled: html.includes(" disabled") }));
""" % ACTIONS_JS)
    assert out == {"buttons": 2, "disabledBefore": False, "header": True, "console": False, "disabled": False}


def test_service_command_answer_of_another_printer_is_dropped():
    out = _dialog("printer-service.js", """
  const ACTIONS = %s;
  const k2 = id => ({ id, label: "K2-" + id, kind: "creality", klipper_api: true, online: true, state: "idle" });
  m.window.ForgeService.open(k2("A"), m.fetcher);
  m.pending["/api/admin/printers/A/service"].resolve({ job: null, lines: [], klipper: true, actions: ACTIONS });
  await m.flush();
  m.click("[data-ps]", { ps: "svc_fw_restart" }); m.click("[data-ps]", { ps: "svc_fw_restart" });   // asks, then sends
  m.dlg().close();
  m.window.ForgeService.open(k2("B"), m.fetcher);
  m.pending["/api/admin/printers/B/service"].resolve({ job: null, lines: [], klipper: true, actions: ACTIONS });
  await m.flush();
  m.pending["/api/admin/printers/A/command"].resolve({ detail: "ОТВЕТ-A" });
  await m.flush();
  // a re-read the late answer may have asked for: answered too, so nothing waits forever
  m.pending["/api/admin/printers/B/service"].resolve({ job: null, lines: [], klipper: true, actions: ACTIONS });
  await m.flush();
  const html = m.dlg().innerHTML;
  console.log(JSON.stringify({ answer: html.includes("ОТВЕТ-A"), header: html.includes("K2-B"), disabled: html.includes(" disabled") }));
""" % ACTIONS_JS)
    assert out == {"answer": False, "header": True, "disabled": False}


def test_service_list_and_descriptions_come_from_the_server():
    """printer-service.js repeated klipper_service.ACTIONS with the macros' temperatures written
    in as text, while its comment said they were read from the printer's config."""
    from app.services import klipper_service
    actions = klipper_service.status("nobody", None)["actions"]
    assert [a["key"] for a in actions] == ["svc_bed_mesh", "svc_nozzle_pid", "svc_bed_pid", "svc_shaper",
                                           "svc_belts", "svc_fw_restart"]
    assert all(a["title"] and a["about"] for a in actions)
    # what a macro heats to is the printer's own config: not a number written here
    assert not any(re.search(r"\d+\s*°", a["about"]) for a in actions)
    src = _source("printer-service.js")
    assert "°" not in src and "svc_bed_mesh" not in src
    out = _dialog("printer-service.js", """
  m.window.ForgeService.open({ id: "A", label: "K2-A", kind: "creality", klipper_api: true, online: true, state: "idle" }, m.fetcher);
  const waiting = m.dlg().innerHTML;
  m.pending["/api/admin/printers/A/service"].resolve({ job: null, lines: [], klipper: true, actions: %s });
  await m.flush();
  console.log(JSON.stringify({ waiting: (waiting.match(/data-ps="/g) || []).length, html: m.dlg().innerHTML }));
""" % ACTIONS_JS)
    assert out["waiting"] == 0
    assert 'data-ps="svc_bed_mesh"' in out["html"] and "ОПИСАНИЕ-С-СЕРВЕРА" in out["html"]
    assert 'data-ps="svc_fw_restart"' in out["html"] and "около минуты" in out["html"]


def test_service_says_when_the_printer_could_not_be_asked():
    out = _dialog("printer-service.js", """
  m.window.ForgeService.open({ id: "A", label: "K2-A", kind: "creality", klipper_api: true, online: true, state: "idle" }, m.fetcher);
  m.pending["/api/admin/printers/A/service"].resolve({ detail: "Принтер не найден" }, 404);
  await m.flush();
  console.log(JSON.stringify(m.dlg().innerHTML));
""")
    assert "Принтер не найден" in out and "pf-error" in out


# --- hunt 43: a removed file stays picked -----------------------------------------------------------

def test_file_pool_removed_file_is_no_longer_picked():
    out = _file_pool("""
  window.ForgeFilePool.mount(root, fetcher);
  pending["/api/admin/file-pool"].ok({ groups: [GROUP], sync: {} }); await flush();
  L.click(tgt("[data-fp-open]", { fpOpen: "g" })); await flush();
  pending["/api/admin/file-pool/g"].ok(POOL([{ id: "f1", name: "x.3mf", title: "X", size: 2048, on: [], pi: true, readable: true }])); await flush();
  L.change(tgt("[data-fp-pick]", { fpPick: "g" }, { value: "f1", checked: true }));
  L.click(tgt("[data-fp-del]", { fpDel: "g", name: "x.3mf" })); await flush();
  pending["/api/admin/file-pool/g/stored?name=x.3mf"].ok({}); await flush();
  pending["/api/admin/file-pool"].ok({ groups: [GROUP], sync: {} }); pending["/api/admin/file-pool/g"].ok(POOL([])); await flush();
  const btn = /data-fp-plan="g"[^>]*>([^<]*)</.exec(root.innerHTML)[1];
  L.click(tgt("[data-fp-plan]", { fpPlan: "g" })); await flush();
  console.log(JSON.stringify({ btn, plan: bodies["/api/admin/file-pool/g/plan"] }));
""")
    assert out == {"btn": "Синхронизировать всё", "plan": "{}"}


def test_file_pool_keeps_a_pick_that_is_still_there():
    out = _file_pool("""
  window.ForgeFilePool.mount(root, fetcher);
  pending["/api/admin/file-pool"].ok({ groups: [GROUP], sync: {} }); await flush();
  L.click(tgt("[data-fp-open]", { fpOpen: "g" })); await flush();
  const two = [{ id: "f1", name: "x.3mf", title: "X", size: 2048, on: [], pi: true, readable: true },
               { id: "f2", name: "y.3mf", title: "Y", size: 2048, on: [], pi: true, readable: true }];
  pending["/api/admin/file-pool/g"].ok(POOL(two)); await flush();
  L.change(tgt("[data-fp-pick]", { fpPick: "g" }, { value: "f1", checked: true }));
  L.change(tgt("[data-fp-pick]", { fpPick: "g" }, { value: "f2", checked: true }));
  L.click(tgt("[data-fp-del]", { fpDel: "g", name: "y.3mf" })); await flush();
  pending["/api/admin/file-pool/g/stored?name=y.3mf"].ok({}); await flush();
  pending["/api/admin/file-pool"].ok({ groups: [GROUP], sync: {} }); pending["/api/admin/file-pool/g"].ok(POOL(two.slice(0, 1))); await flush();
  L.click(tgt("[data-fp-plan]", { fpPlan: "g" })); await flush();
  console.log(JSON.stringify(bodies["/api/admin/file-pool/g/plan"]));
""")
    assert json.loads(out) == {"files": ["f1"]}


# --- Н12: the file pool's errors -------------------------------------------------------------------

def test_file_pool_group_list_error_is_shown():
    """file-pool.js:104 — a failed list read looked like «no printers»."""
    out = _file_pool("""
  window.ForgeFilePool.mount(root, fetcher);
  pending["/api/admin/file-pool"].ok({ detail: "database is locked" }, 500); await flush();
  const failed = root.innerHTML;
  L.click(tgt("[data-fp-reload]", {})); await flush();
  pending["/api/admin/file-pool"].ok({ groups: [GROUP], sync: {} }); await flush();
  console.log(JSON.stringify({ failed, again: root.innerHTML }));
""")
    assert "database is locked" in out["failed"] and "Нет принтеров Bambu или Creality" not in out["failed"]
    assert "data-fp-reload" in out["failed"]
    assert "database is locked" not in out["again"] and 'data-fp-open="g"' in out["again"]


def test_file_pool_sync_poll_error_is_shown_and_the_poll_gives_up():
    """file-pool.js:110 — the progress poll returned silently on an error: the bar stood still
    without a word and the timer ran until the page was closed."""
    out = _file_pool("""
  const running = { group: "g", running: true, printers: [{ id: "p1", label: "P1", state: "copying", bytes_done: 50,
                                                            bytes_total: 100, files_done: 0, files_total: 2, errors: [] }] };
  window.ForgeFilePool.mount(root, fetcher);
  pending["/api/admin/file-pool"].ok({ groups: [GROUP], sync: running }); await flush();
  timers[0](); pending["/api/admin/file-pool/sync"].fail(new Error("Нет связи с сервером")); await flush();
  const once = root.innerHTML;
  let ticks = 1;
  while (timers[0] && ticks < 100) { timers[0](); pending["/api/admin/file-pool/sync"].fail(new Error("Нет связи с сервером")); await flush(); ticks++; }
  const stopped = root.innerHTML;
  L.click(tgt("[data-fp-watch]", {})); await flush();
  timers[timers.length - 1](); pending["/api/admin/file-pool/sync"].ok(Object.assign({}, running, { running: false })); await flush();
  console.log(JSON.stringify({ once, ticks, stopped, alive: timers.filter(Boolean).length, after: root.innerHTML }));
""")
    assert "Ход копирования не обновляется" in out["once"] and "Нет связи с сервером" in out["once"]
    assert out["ticks"] < 100 and "data-fp-watch" in out["stopped"]
    assert out["alive"] == 0 and "не обновляется" not in out["after"] and "Копирование закончено" in out["after"]


# --- Н13: dead code --------------------------------------------------------------------------------

def test_file_pool_has_no_dead_printer_name():
    assert "printerName" not in _source("file-pool.js")


# --- Н14: one HTML escaping, the interface's language for numbers and dates ------------------------

ESCAPERS = {"preview.js": "escape", "printer-files.js": "esc", "file-pool.js": "esc", "printer-service.js": "esc"}


def test_html_escaping_copies_agree():
    """Four copies of the same escaping: one behaviour, null and undefined are an empty string."""
    funcs = {}
    for name, fn in ESCAPERS.items():
        m = re.search(r"^\s*const %s = (.+);$" % fn, _source(name), re.M)
        assert m, name
        funcs[name] = m.group(1)
    inputs = [None, "__undefined__", 0, False, 12.5, "<a href=\"x\">Tom & 'Jerry'</a>"]
    out = _node("const inputs = %s.map(x => x === '__undefined__' ? undefined : x);\n"
                "console.log(JSON.stringify({%s}));" % (json.dumps(inputs), ", ".join(
                    "%s: inputs.map(%s)" % (json.dumps(n), f) for n, f in funcs.items())))
    want = ["", "", "0", "false", "12.5", "&lt;a href=&quot;x&quot;&gt;Tom &amp; &#39;Jerry&#39;&lt;/a&gt;"]
    assert out == {n: want for n in ESCAPERS}


EN_I18N = "{I18N: {locale: () => 'en-GB'}}"        # i18n.js's language: English (its locale only)


def test_job_card_numbers_follow_the_interface_language():
    card = {"grams": 352.54, "layer_mm": 0.2, "filaments": [{"type": "PETG", "meters": 117.26}]}
    assert "352,5 г" in _card(card)                                    # Russian (no I18N in node)
    html = _card(card, EN_I18N)
    assert "352.5 г" in html and "117.3 метра" in html      # metres in words, never «м» like minutes and "," not in re.sub(r"<[^>]*>", "", html)


def test_printer_files_numbers_and_dates_follow_the_interface_language():
    out = _dialog("printer-files.js", """
  m.window.ForgeFiles.open({ id: "A", label: "A", kind: "creality" }, m.fetcher);
  m.pending["/api/admin/printers/A/files"].resolve({ kind: "creality", files: [
    { name: "a.gcode", title: "A", path: "/a.gcode", grams: 352.54, size: 1572864, mtime: 1792236720 }] });
  await m.flush();
  console.log(JSON.stringify(m.dlg().innerHTML));
""", window=EN_I18N)
    assert "352.5 г" in out and "1.5 МБ" in out and re.search(r"\d\d/\d\d/\d\d", out)


def test_file_pool_sizes_follow_the_interface_language():
    out = _file_pool("""
  window.ForgeFilePool.mount(root, fetcher);
  pending["/api/admin/file-pool"].ok({ groups: [GROUP], sync: {} }); await flush();
  L.click(tgt("[data-fp-open]", { fpOpen: "g" })); await flush();
  pending["/api/admin/file-pool/g"].ok(POOL([{ id: "f1", name: "x.3mf", title: "X", size: 1572864, on: [], pi: true, readable: true }])); await flush();
  console.log(JSON.stringify(root.innerHTML));
""", window=EN_I18N)
    assert "1.5 МБ" in out


def test_no_ru_locale_written_in_the_c2_modules():
    for name in ("preview.js", "printer-files.js", "file-pool.js"):
        assert "'ru-RU'" not in _source(name).replace("I18N ? window.I18N.locale() : 'ru-RU'", ""), name


# --- ui.md 3: the login page's errors in the interface's language -----------------------------------

LOGIN = _source("login.html")
_LOGIN_HARNESS = r"""
var els = {};
function el(id) { return els[id] || (els[id] = { id: id, value: '', textContent: '', disabled: false,
  listeners: {}, addEventListener: function (t, f) { this.listeners[t] = f; } }); }
globalThis.document = { getElementById: el };
var replaced = [], reloads = 0;
globalThis.location = { protocol: 'https:', pathname: '/', search: '', hash: '',
  replace: function (u) { replaced.push(u); }, reload: function () { reloads++; } };
globalThis.window = { location: globalThis.location };
globalThis.sessionStorage = { getItem: function () { return String(Date.now()); }, setItem: function () {} };
var answer = %(answer)s;
globalThis.fetch = function (url) {
  if (url !== '/api/auth/login') return Promise.resolve({ ok: false, status: 404, json: function () { return Promise.resolve({}); } });
  return Promise.resolve({ ok: answer.status < 400, status: answer.status, json: function () { return Promise.resolve(answer.body); } });
};
%(script)s
el('u').value = 'admin'; el('p').value = 'pw';
el('f').listeners.submit({ preventDefault: function () {} });
setTimeout(function () { console.log(JSON.stringify({ err: el('e').textContent, disabled: el('b').disabled, reloads: reloads })); }, 50);
"""


def _login_error(status, body):
    script = re.findall(r"<script>(.*?)</script>", LOGIN, re.S)[-1]
    return _node(_LOGIN_HARNESS % {"answer": json.dumps({"status": status, "body": body}), "script": script})


@pytest.mark.parametrize("status,detail,shown", [
    (401, "Invalid username or password", "Неверный логин или пароль"),
    (429, "Too many login attempts. Try again later.", "Слишком много попыток входа — попробуйте позже"),
    (400, "Username and password required", "Введите логин и пароль"),
    (429, "Rate limit exceeded", "Слишком много попыток входа — попробуйте позже"),
    (403, "Свой текст сервера", "Свой текст сервера"),
    (422, [{"loc": ["body", "password"], "msg": "field required"}], "Ошибка входа"),
])
def test_login_errors_are_in_the_page_language(status, detail, shown):
    """app/api/auth.py answers in English; the page showed it as is in the Russian interface."""
    r = _login_error(status, {"detail": detail})
    assert r == {"err": shown, "disabled": False, "reloads": 0}


def test_login_errors_read_in_english():
    assert _tr(["Неверный логин или пароль", "Слишком много попыток входа — попробуйте позже",
                "Введите логин и пароль"]) == [
        "Invalid username or password", "Too many sign-in attempts — try again later", "Enter the username and password"]


def test_login_page_keeps_the_reload_of_the_same_address():
    # fixed on 17.11 (tests/test_hardening_2026_11.py): the sign-in at «/#/…» reloads the page
    assert "if (location.pathname === '/' && !location.search) location.reload();" in LOGIN
