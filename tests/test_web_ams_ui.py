"""The pure AMS / drying / filament-change helpers of app/web/app.js, run in node.

The block between /* ams-ui:begin */ and /* ams-ui:end */ holds only pure code;
it may use safeText, icon, amsUnitName and amsFmtMin (function declarations
elsewhere in app.js), which are extracted alongside it. Functions outside the
block (the card's AMS chip, the popover click handler) are extracted whole by
brace matching and run against stubs. Skipped without node.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
JS = (ROOT / "app" / "web" / "app.js").read_text(encoding="utf-8")


def _function(name: str) -> str:
    m = re.search(r"function %s\([^)]*\) \{.*?\n\s*\}" % name, JS, re.S)
    assert m, "%s() not found in app.js" % name
    return m.group(0)


def _block() -> str:
    m = re.search(r"/\* ams-ui:begin \*/(.*?)/\* ams-ui:end \*/", JS, re.S)
    assert m, "ams-ui block not found in app.js"
    return m.group(1)


def _js_function(name: str) -> str:
    """The whole `[async] function name(...) {...}` declaration of app.js, also
    with nested blocks: braces matched, string literals and // comments skipped."""
    m = re.search(r"(?:async )?function %s\([^)]*\) \{" % name, JS)
    assert m, "%s() not found in app.js" % name
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
    raise AssertionError("%s(): unbalanced braces" % name)


def _node(src: str):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    out = subprocess.run([node, "-e", src], capture_output=True, text=True, timeout=60, encoding="utf-8")
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _lib(*extra: str) -> str:
    """The helper block with what it uses, plus the `extra` app.js functions."""
    src = "\n".join(_function(n) for n in ("safeText", "icon", "amsUnitName", "amsFmtMin"))
    return src + "\n" + _block() + "".join("\n" + _js_function(n) for n in extra)


def run(expr: str, *extra: str):
    """Evaluate `expr` (JSON-serializable) against the helper block in node;
    `extra` names more (pure) app.js functions it calls."""
    return _node(_lib(*extra) + "\nconsole.log(JSON.stringify(" + expr + "));")


def test_block_is_pure():
    block = _block()
    for banned in ("document.", "window.", "fetch(", "apiFetch", "cachedAllPrinters", "localStorage"):
        assert banned not in block, banned


DRYING = {"type": "n3f", "status": "drying", "phase": "dehumidify", "active": True, "remaining_min": 712,
          "temp": 55, "hours": 12, "filament": "PETG", "blockers": [], "feeding": False,
          "max_temp": 65, "max_by": None, "suggest": {"filament": "PETG", "temp": 65, "hours": 12},
          "presets": [{"filament": m, "temp": 45, "hours": 12}
                      for m in ("PLA", "PETG", "ABS", "ASA", "PA", "PC", "TPU", "PVA", "PPS")]}


def _dry(**kw):
    d = json.loads(json.dumps(DRYING))
    d.update(kw)
    return d


def test_dry_status_text():
    assert run("dryStatusText(%s)" % json.dumps({"dry": DRYING, "dry_time": 712})) == \
        "Сушка · осушение · 55 °C · PETG · осталось 11:52"
    off = _dry(status="off", phase=None, active=False, remaining_min=None, temp=None, filament=None)
    assert run("dryStatusText(%s)" % json.dumps({"dry": off})) == "Сушка выключена"
    assert run("dryStatusText({dry: null, dry_time: 90})") == "Сушка · осталось 1:30"
    assert run("dryStatusText({dry: null, dry_time: null})") == "Сушка выключена"
    # a device string is escaped
    evil = _dry(filament="<img src=x>")
    assert "<img" not in run("dryStatusText(%s)" % json.dumps({"dry": evil}))


def test_dry_active():
    assert run("[dryActive({dry: {active: true}}), dryActive({dry: {active: false}, dry_time: 50}),"
               " dryActive({dry: null, dry_time: 5}), dryActive({dry_time: 0}), dryActive(null)]") == \
        [True, False, True, False, False]


def test_dry_limit_text():
    assert run("dryLimitText(%s)" % json.dumps(_dry(max_temp=65))) == "45–65 °C"
    assert run("dryLimitText(%s)" % json.dumps(_dry(max_temp=45, max_by="PLA"))) == \
        "не выше 45 °C — в AMS есть PLA"
    assert run("dryLimitText(%s)" % json.dumps(_dry(max_temp=55, max_by="PETG", feeding=True))) == \
        "не выше 55 °C — идёт печать из этого AMS (PETG)"
    # an AMS HT feeding PA: capped at the type maximum, nothing to name
    assert run("dryLimitText(%s)" % json.dumps(_dry(type="n3s", max_temp=85, max_by=None, feeding=True))) == \
        "не выше 85 °C — идёт печать из этого AMS"
    assert run("dryLimitText(%s)" % json.dumps(_dry(max_temp=45, max_by="?"))) == \
        "не выше 45 °C — в AMS есть катушка без материала"


def test_filament_change_text():
    assert run('filamentChangeText({action: "load", step: "cut"})') == "Загрузка филамента · отрезка филамента"
    assert run('filamentChangeText({action: "unload", step: "pull"})') == \
        "Выгрузка филамента · извлечение текущего филамента"
    assert run("filamentChangeText({action: null, step: null})") == "Смена филамента"
    assert run("filamentChangeText(null)") is None


def test_nozzle_chip():
    hf = run('nozzleChip({diameter: 0.4, type: "high_flow_hardened_steel"})')
    assert hf["text"] == "Ø0.4 HF" and "высокий поток" in hf["tip"] and hf["standard"] is False
    std = run('nozzleChip({diameter: 0.4, type: "hardened_steel"})')
    assert std == {"text": "Ø0.4", "tip": "Сопло 0.4 мм · закалённая сталь", "standard": True}
    assert run('nozzleChip({diameter: 0.6, type: "stainless_steel"})')["standard"] is False
    assert run('nozzleChip({diameter: 0.4, type: "tpu_high_flow"})')["text"] == "Ø0.4 TPU"
    assert run('nozzleChip({diameter: 0.4, type: null})')["tip"] == "Сопло 0.4 мм · тип неизвестен"
    assert run("nozzleChip({diameter: null, type: 'brass'})") is None
    assert run("nozzleChip(null)") is None


def _printer(**kw):
    p = {"id": "b1", "label": "P2S <1>", "kind": "bambu", "state": "idle", "online": True,
         "print_cmds": True, "filament_swap_ok": True, "filament_loaded": True, "dual_nozzle": False,
         "filament_change": None, "ams_dry_ok": True, "grace_period_active": False,
         "ams": {"tray_now": 0, "units": [
             {"index": 0, "humidity_pct": 20, "dry_time": None, "dry": _dry(status="off", active=False, phase=None,
                                                                         remaining_min=None, temp=None, filament=None),
              "slots": [{"type": "PETG", "color": "FF0000", "empty": False, "nozzle_temp": [230, 270]},
                        {"type": "<img src=x>", "color": "javascript:x", "empty": False, "nozzle_temp": None},
                        {"type": None, "color": None, "empty": True, "nozzle_temp": None},
                        {"type": None, "color": None, "empty": True, "nozzle_temp": None}]},
             {"index": 1, "humidity": 2, "dry_time": None, "dry": None,
              "slots": [{"type": "ABS", "color": "00FF00", "empty": False, "nozzle_temp": [240, 280]}]}]}}
    p.update(kw)
    return p


def _pop(p):
    return run("amsPopHtml(%s)" % json.dumps(p))


def test_ams_popover_idle():
    html = _pop(_printer())
    assert re.findall(r'data-ams-load="(\d+:\d+)"', html) == ["0:1", "1:0"]
    assert 'class="pc-pop-preset on" disabled' in html            # the loaded slot (1)
    assert html.count("disabled") == 3                             # loaded + 2 empty slots
    assert "data-ams-unload" in html and "data-dry-open=\"0\"" in html
    assert "<img" not in html and "&lt;img" in html                # device strings escaped
    assert "javascript" not in html                                # colours only as hex
    assert "AMS 2 Pro" in html and "AMS 2" in html


def test_ams_popover_while_printing():
    html = _pop(_printer(state="printing"))
    loads = re.findall(r'<button[^>]*data-ams-load="[^"]*"[^>]*>', html)
    assert loads and all("disabled" in b for b in loads)
    assert "Только в простое — принтер печатает или на паузе" in html
    assert "data-ams-unload" not in html
    assert "data-dry-open" in html                                 # drying stays possible


@pytest.mark.parametrize("over,note", [
    ({"filament_swap_ok": False}, "Прошивка не поддерживает смену филамента командой"),
    ({"filament_swap_ok": None}, "Версия прошивки ещё не получена"),
    ({"dual_nozzle": True}, "Принтеры с двумя экструдерами пока не поддерживаются"),
])
def test_ams_popover_without_filament_swap(over, note):
    html = _pop(_printer(**over))
    assert "data-ams-load" not in html and "data-ams-unload" not in html
    assert note in html


def test_ams_popover_load_unload_note_sits_above_drying():
    # Why load / unload is off: once, right under the first unit's slots and
    # above every drying control, so it does not read as a drying limit
    # (drying is allowed while printing).
    html = _pop(_printer(state="printing"))
    note = html.index('</div><div class="pc-pop-note">Только в простое — принтер печатает или на паузе</div>')
    assert html.index('data-ams-load="0:1"') < note < html.index("Сушка выключена") < html.index("data-dry-open")
    assert html.count("Только в простое") == 1
    # no slot buttons: right under the first unit's name
    html = _pop(_printer(filament_swap_ok=None))
    assert ('<div class="pc-pop-h">AMS 1 · AMS 2 Pro · 💧20%</div><div class="pc-pop-note">'
            'Версия прошивки ещё не получена — загрузка и выгрузка пока недоступны</div>') in html
    assert html.index("Версия прошивки") < html.index("Сушка выключена")
    # a popover without units still says why
    assert "Только в простое" in _pop(_printer(state="printing", ams={"tray_now": 255, "units": []}))


def test_ams_popover_change_running_and_nothing_loaded():
    html = _pop(_printer(filament_change={"action": "load", "step": "heat"}))
    assert "Идёт смена филамента" in html and "data-ams-unload" not in html
    assert "data-ams-unload" not in _pop(_printer(filament_loaded=False))
    assert "data-ams-unload" not in _pop(_printer(ams={"tray_now": 255, "units": _printer()["ams"]["units"]}))


def test_ams_popover_drying_controls():
    p = _printer()
    p["ams"]["units"][0]["dry"] = DRYING
    html = _pop(p)
    assert 'data-dry-stop="0"' in html and "data-dry-open" not in html
    assert "Сушка · осушение · 55 °C · PETG · осталось 11:52" in html
    blocked = _printer()
    blocked["ams"]["units"][0]["dry"]["blockers"] = [{"code": 2, "text": "AMS занят <b>"}]
    html = _pop(blocked)
    assert "data-dry-open" not in html
    assert 'class="pc-pop-note warn">Сушка недоступна: AMS занят &lt;b&gt;' in html
    assert "data-dry-open" not in _pop(_printer(ams_dry_ok=False))
    stopping = _printer()
    stopping["ams"]["units"][0]["dry"]["status"] = "stopping"
    assert "data-dry-open" not in _pop(stopping)
    cooling = _printer()
    cooling["ams"]["units"][0]["dry"]["status"] = "cooling"
    assert "data-dry-open" in _pop(cooling)                       # Studio: cooling is idle


def test_dry_popover():
    html = run("dryPopHtml(%s, %s)" % (json.dumps(_printer()), json.dumps({"index": 0, "dry": DRYING})))
    assert len(re.findall(r"data-dry-mat=", html)) == 9
    assert re.findall(r'class="pc-pop-preset on" data-dry-mat="(\w+)"', html) == ["PETG"]
    assert re.search(r'data-dry="temp" value="65"', html) and re.search(r'data-dry="hours" value="12"', html)
    assert 'data-dry-start="0"' in html and "data-ams-back" in html and "45–65 °C" in html
    chosen = run("dryPopHtml(%s, %s, {filament: 'PLA', temp: 45, hours: 12})"
                 % (json.dumps(_printer()), json.dumps({"index": 0, "dry": DRYING})))
    assert re.findall(r'class="pc-pop-preset on" data-dry-mat="(\w+)"', chosen) == ["PLA"]


def test_dry_form_check():
    dry = json.dumps(_dry(max_temp=55))
    assert run("dryFormCheck(%s, 'PETG', '55', ' 12 ')" % dry) == {"temp": 55, "hours": 12, "filament": "PETG"}
    assert run("dryFormCheck(%s, 'PETG', '56', '12')" % dry) == {"bad": "temp"}
    assert run("dryFormCheck(%s, 'PETG', '4x', '12')" % dry) == {"bad": "temp"}
    assert run("dryFormCheck(%s, 'PETG', '50', '25')" % dry) == {"bad": "hours"}
    assert run("dryFormCheck(%s, null, '50', '12')" % dry) == {"bad": "mat"}


def test_confirm_texts():
    p = json.dumps(_printer())
    assert run("amsLoadConfirmText(%s, 1, 0)" % p) == (
        "Загрузить слот 5 (ABS) на «P2S <1>»? Принтер нагреет сопло, отрежет и уберёт текущий филамент.")
    empty = json.dumps(_printer(filament_loaded=False))
    assert run("amsLoadConfirmText(%s, 0, 1)" % empty).endswith("Принтер нагреет сопло и подаст филамент.")
    assert run("amsUnloadConfirmText(%s)" % p) == (
        "Выгрузить филамент на «P2S <1>»? Принтер нагреет сопло и вернёт филамент в AMS.")
    ext = json.dumps(_printer(ams={"tray_now": 254, "units": []}))
    assert run("amsUnloadConfirmText(%s)" % ext).endswith("и выведет филамент из экструдера.")


def test_external_unload_offered_without_an_ams():
    assert run("externalUnloadOk(%s)" % json.dumps(_printer(ams=None))) is True
    assert run("externalUnloadOk(%s)" % json.dumps(_printer(ams=None, filament_loaded=None))) is False
    assert run("externalUnloadOk(%s)" % json.dumps(_printer(ams=None, state="printing"))) is False
    assert run("externalUnloadOk(%s)" % json.dumps(_printer(ams=None, grace_period_active=True))) is False


def _tab_unit(**kw):
    u = {"printer_id": "b1", "unit_index": 0, "dry_time": None, "dry": _dry(status="off", active=False,
                                                                           remaining_min=None, phase=None)}
    u.update(kw)
    return u


def _tab(u, p, can):
    return run("amsTabDryHtml(%s, %s, %s)" % (json.dumps(u), json.dumps(p), json.dumps(can)))


def test_ams_tab_drying_line():
    p = _printer()
    drying = _tab_unit(dry=DRYING, dry_time=712)
    html = _tab(drying, p, True)
    assert 'class="ams-dry"' in html and "осталось 11:52" in html
    assert 'data-ams-dry-stop data-pid="b1" data-ams="0"' in html
    assert "data-ams-dry-stop" not in _tab(drying, p, False)              # viewer: status only
    # not drying: «Сушить…» for an admin, nothing for a viewer
    html = _tab(_tab_unit(), p, True)
    assert 'class="ams-dry off"' in html and 'data-ams-dry-open data-pid="b1" data-ams="0"' in html
    assert _tab(_tab_unit(), p, False) == ""
    assert _tab(_tab_unit(), _printer(ams_dry_ok=False), True) == ""
    # reasons it cannot start are shown to everyone, without the button
    blocked = _tab_unit(dry=_dry(status="off", active=False, blockers=[{"code": 1, "text": "Мало <питания>"}]))
    html = _tab(blocked, p, True)
    assert "Сушка недоступна: Мало &lt;питания&gt;" in html and "data-ams-dry-open" not in html
    assert "Сушка недоступна" in _tab(blocked, p, False)
    # a unit without a drying block (older firmware): drying from dry_time, no controls
    legacy = _tab_unit(dry=None, dry_time=90)
    assert "осталось 1:30" in _tab(legacy, p, True) and "data-ams-dry" not in _tab(legacy, p, True)
    assert _tab(_tab_unit(dry=None), p, True) == ""


def test_ams_tab_reasons_stand_out_from_the_status():
    # Each reason drying cannot start is a warn-coloured line of its own, as in
    # the card's AMS popover; the status before them stays plain.
    blocked = _tab_unit(dry=_dry(status="off", active=False, blockers=[
        {"code": 3, "text": "Филамент <в выходе>"}, {"code": 9, "text": "Система занята"}]))
    for can in (True, False):
        html = _tab(blocked, _printer(), can)
        assert "Сушка выключена" in html
        assert re.findall(r'<span class="warn">([^<]*)</span>', html) == [
            "Сушка недоступна: Филамент &lt;в выходе&gt;", "Сушка недоступна: Система занята"]
    css = (ROOT / "app" / "web" / "styles.css").read_text(encoding="utf-8")
    assert re.search(r"\.ams-dry\.off \.warn \{[^}]*color:\s*var\(--warn-11\)", css)


def _chip_temp(p):
    """(tooltip, content) of each AMS temperature / drying-time span of the card's AMS chip."""
    html = run("amsChip(%s, false)" % json.dumps(p), "amsChip")
    return re.findall(r'<span class="pc-ams-temp" data-tip="([^"]*)">(.*?)</span>', html)


def test_card_chip_shows_the_drying_time_left():
    # The API gives minutes: 719 is 11 h 59 min, shown in the AMS temperature's place.
    p = _printer()
    p["ams"]["units"][0].update(dry=_dry(remaining_min=719), dry_time=719, temp=47.9)
    [(tip, text)] = _chip_temp(p)
    assert text == '<svg class="pc-ic" aria-hidden="true"><use href="#i-hourglass"></use></svg>11:59'
    assert tip == "Сушка · осушение · 55 °C · PETG · осталось 11:59 · в AMS 48°"
    # not drying: the AMS temperature
    p["ams"]["units"][0].update(dry=_dry(status="off", active=False, remaining_min=None), dry_time=None, temp=31.4)
    assert _chip_temp(p) == [("Температура AMS",
                              '<svg class="pc-ic" aria-hidden="true"><use href="#i-steam"></use></svg>31°')]


# --- popover DOM code, run in node against a stub element ------------------------

def _dom_function(name: str, required: bool = True) -> str:
    m = re.search(r"(?:async )?function %s\([^)]*\) \{.*?\n\s*\}" % name, JS, re.S)
    assert m or not required, "%s() not found in app.js" % name
    return m.group(0) if m else ""


def test_popover_command_disables_its_buttons_until_the_reply():
    # A second click while the first command waits for the printer would send
    # it twice: the popover's buttons stay disabled until the reply.
    src = """
    var log = [], resolveSend = null;
    var btns = [{disabled: false}, {disabled: true}, {disabled: false}];
    var pcPop = {querySelectorAll: function () { return btns; }};
    function sendPrinterCmd(pid, action, extra) {
      log.push(["send", pid, action, extra]);
      return new Promise(function (r) { resolveSend = r; });
    }
    function closePop() { log.push(["close"]); }
    function states() { return btns.map(function (b) { return b.disabled; }); }
    """ + _dom_function("popCmd") + """
    (async function () {
      var out = {};
      var p = popCmd("b1", "ams_unload", null);
      out.during = states();
      resolveSend(true);
      out.ok = await p;
      out.after = states();
      p = popCmd("b1", "dry_stop", {ams: 0});
      out.during2 = states();
      resolveSend(false);
      out.ok2 = await p;
      out.after2 = states();
      out.log = log;
      console.log(JSON.stringify(out));
    })();
    """
    out = _node(src)
    assert out["during"] == [True, True, True] and out["during2"] == [True, True, True]
    assert out["after"] == out["after2"] == [False, True, False]      # a disabled slot stays disabled
    assert (out["ok"], out["ok2"]) == (True, False)
    assert out["log"] == [["send", "b1", "ams_unload", None], ["close"], ["send", "b1", "dry_stop", {"ams": 0}]]


def test_dry_form_start_sends_the_fields_as_typed():
    # «Начать сушку»: the selected preset's material, and temperature and hours
    # exactly as in the form's fields (the API takes hours, 1-24).
    src = _lib() + """
    var sent = [];
    function popCmd(pid, action, extra) { sent.push([pid, action, extra]); return Promise.resolve(true); }
    function swapPop() {}
    function closePop() {}
    var P = %s;
    function findPrinter(pid) { return pid === P.id ? P : null; }
    function el(data, on) {
      var e = {dataset: data || {}, value: "", on: !!on, focus: function () {}, select: function () {},
               hasAttribute: function () { return false; }};
      e.classList = {toggle: function (cls, v) { if (cls === "on") e.on = v; }};
      return e;
    }
    var temp = el(), hours = el();
    var mats = [el({dryMat: "PLA", temp: "45", hours: "12"}), el({dryMat: "PETG", temp: "65", hours: "12"}, true)];
    var pcPop = {
      querySelector: function (sel) {
        if (sel === '[data-dry="temp"]') return temp;
        if (sel === '[data-dry="hours"]') return hours;
        if (sel === ".pc-pop-preset.on[data-dry-mat]") return mats.filter(function (b) { return b.on; })[0] || null;
        return null;
      },
      querySelectorAll: function (sel) { return sel === "[data-dry-mat]" ? mats : []; }
    };
    """ % json.dumps(_printer()) + _js_function("onAmsPopClick") + """
    (async function () {
      await onAmsPopClick("b1", mats[0]);          // the PLA preset fills both fields
      var filled = [temp.value, hours.value, mats.map(function (b) { return b.on; })];
      hours.value = " 7 ";                          // then the hours are typed
      await onAmsPopClick("b1", el({dryStart: "0"}));
      console.log(JSON.stringify({filled: filled, sent: sent}));
    })();
    """
    out = _node(src)
    assert out["filled"] == ["45", "12", [True, False]]
    assert out["sent"] == [["b1", "dry_start", {"ams": 0, "temp": 45, "hours": 7, "filament": "PLA"}]]


# A fixed box with only `left` set shrinks to the room right of it; the stub
# takes its natural size from an "<!--WxH-->" marker in its html and wraps
# (gets taller) when narrower, like the real popover.
_POP_STUB = r"""
var window = {innerWidth: 375, innerHeight: 812};
var pcPop = null, pcPopPid = null, pcPopAnchorRect = null;
var thePop = (function () {
  var el = {dataset: {}, style: {left: "", top: "", display: "", visibility: ""}, innerHTML: ""};
  function natural() {
    var m = /<!--(\d+)x(\d+)-->/.exec(el.innerHTML);
    return m ? [+m[1], +m[2]] : [168, 60];
  }
  Object.defineProperty(el, "offsetWidth", {get: function () {
    var left = parseFloat(el.style.left) || 0;
    return Math.min(natural()[0], Math.min(340, window.innerWidth - 16), window.innerWidth - left);
  }});
  Object.defineProperty(el, "offsetHeight", {get: function () {
    var n = natural();
    return Math.ceil(n[1] * n[0] / el.offsetWidth);
  }});
  el.querySelector = function () { return null; };
  return el;
})();
function ensurePop() { pcPop = thePop; return thePop; }
function chip(left, top) {
  return {getBoundingClientRect: function () { return {left: left, top: top, right: left + 60, bottom: top + 24}; }};
}
function box() {
  var l = parseFloat(thePop.style.left), t = parseFloat(thePop.style.top);
  return {left: l, top: t, width: thePop.offsetWidth, right: l + thePop.offsetWidth,
          bottom: t + thePop.offsetHeight};
}
"""


def _pop_dom(script: str):
    src = _POP_STUB + "\n".join(_dom_function(n, n != "placePop") for n in ("placePop", "openPop", "swapPop"))
    return _node(src + "\n" + script)


def test_popover_on_a_phone_keeps_its_width_and_a_margin():
    # A narrow popover opened at the right, then the AMS one: measured at the
    # stale left the AMS popover came out narrower and flush with the right edge.
    out = _pop_dom("""
      openPop(chip(300, 100), "b1", "<!--168x60-->", "nozzle");
      openPop(chip(200, 300), "b1", "<!--340x300-->", "ams");
      var ams = box();
      openPop(chip(160, 300), "b1", "<!--200x150-->", "ams");
      swapPop("<!--340x380-->", "dry");
      console.log(JSON.stringify({ams: ams, dry: box()}));
    """)
    for b in (out["ams"], out["dry"]):
        assert b["width"] == 340
        assert b["left"] >= 8 and b["right"] <= 375 - 8


def test_next_popover_screen_does_not_cover_its_chip():
    # The AMS popover opened above a chip near the bottom of the screen; the
    # taller drying form must stay above the chip too, not slide over it.
    out = _pop_dom("""
      openPop(chip(20, 757), "b1", "<!--340x200-->", "ams");
      var ams = box();
      swapPop("<!--340x400-->", "dry");
      var dry = box();
      swapPop("<!--340x200-->", "ams");
      console.log(JSON.stringify({ams: ams, dry: dry, back: box()}));
    """)
    for b in out.values():
        assert b["bottom"] <= 757 - 6 and b["top"] >= 8
    # a screen with room below the chip opens below it
    out = _pop_dom("""
      openPop(chip(20, 100), "b1", "<!--340x200-->", "ams");
      swapPop("<!--340x400-->", "dry");
      console.log(JSON.stringify(box()));
    """)
    assert out["top"] == 100 + 24 + 6
