"""The temperature chart of the chip popover (app/web/app.js), run in node.

The chart reuses the AMS chart code (amsChart, amsChartOverlay), so the AMS
output is locked first: tests/fixtures/web/ams_chart_golden.json holds the
strings those functions returned for fixed AMS inputs before the temperature
chart extended them. Skipped without node.
"""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app.services import temp_history

ROOT = Path(__file__).resolve().parents[1]
JS = (ROOT / "app" / "web" / "app.js").read_text(encoding="utf-8")
GOLDEN = json.loads((ROOT / "tests" / "fixtures" / "web" / "ams_chart_golden.json").read_text(encoding="utf-8"))


def _function(name: str) -> str:
    """A multi-line function up to the first closing brace on its own line."""
    m = re.search(r"function %s\([^)]*\) \{.*?\n\s*\}" % name, JS, re.S)
    assert m, "%s() not found in app.js" % name
    return m.group(0)


def _js_function(name: str) -> str:
    """The whole function declaration (braces matched, strings and // comments skipped)."""
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


def _node(src: str):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    # the script on stdin: a long -e argument hits the Windows command line limit
    out = subprocess.run([node, "-"], input=src, capture_output=True, text=True, timeout=60, encoding="utf-8")
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _ams_chart_lib() -> str:
    """amsChart and what it calls; the AMS geometry is the real one of app.js."""
    geo = re.search(r"var AMS_GW = [^;]+;", JS)
    assert geo, "AMS_G* geometry not found in app.js"
    names = ("adNum", "pad2", "amsChartOverlay", "amsChart")
    # safeText's regex literals hold quotes that _js_function would take for strings
    return (geo.group(0) + "\nvar amsHours = 24;\n" + _function("safeText") + "\n" +
            "\n".join(_js_function(n) for n in names) + "\n")


def ams_charts(cases):
    """amsChart(...args) per case, with the AMS tab's amsHours set to case["amsHours"]."""
    calls = "".join("amsHours = %d; out.push(amsChart.apply(null, %s));\n"
                    % (c["amsHours"], json.dumps(c["args"], ensure_ascii=False)) for c in cases)
    return _node(_ams_chart_lib() + "var out = [];\n" + calls + "console.log(JSON.stringify(out));")


def ams_overlays(cases):
    calls = "".join("out.push(amsChartOverlay.apply(null, %s));\n" % json.dumps(c["args"], ensure_ascii=False)
                    for c in cases)
    return _node(_ams_chart_lib() + "var out = [];\n" + calls + "console.log(JSON.stringify(out));")


# --- the AMS charts stay byte-for-byte ---------------------------------------------------------

def test_ams_chart_unchanged_for_ams_inputs():
    cases = GOLDEN["chart"]
    assert {c["name"] for c in cases} >= {"humidity_pct_zones_dry_thr_24h", "humidity_index_dry_6h",
                                          "temp_no_opts_7d", "no_points"}
    for case, out in zip(cases, ams_charts(cases)):
        assert out == case["out"], case["name"]


def test_ams_chart_zones_from_the_goal():
    # plan step 11: the AMS tab passes its zones (from the auto-dry goal); `zones: true` stays the
    # fixed 25 / 40 (the golden cases above); a band outside the chart's range is not drawn
    bands = [[0, 5, "var(--ok)"], [5, 15, "var(--warn)"], [15, 20, "var(--danger)"], [30, 40, "var(--info)"]]
    out = ams_charts([{"amsHours": 24, "args": [[{"t": 0, "v": 7}, {"t": 10, "v": 9}], 0, 10, 0, 20,
                                                 "var(--warn)", {"zones": bands}]}])[0]
    rects = re.findall(r'<rect x="26" y="([\d.]+)" width="344" height="([\d.]+)" fill="(var\(--\w+\))" opacity="0.05"/>', out)
    assert [r[2] for r in rects] == ["var(--ok)", "var(--warn)", "var(--danger)"]
    assert [float(r[1]) for r in rects] == [16.0, 32.0, 16.0]          # 64 px of plot: 5, 10 and 5 points of 20


def test_ams_chart_overlay_unchanged_for_ams_inputs():
    cases = GOLDEN["overlay"]
    for case, out in zip(cases, ams_overlays(cases)):
        assert out == case["out"], case["name"]


# --- the temperature chart (the temp-chart block and what it calls) ---------------------------

CSS = (ROOT / "app" / "web" / "styles.css").read_text(encoding="utf-8")
T0 = 1760000000          # 2025-10-09 08:53:20 UTC = 11:53 MSK
H = 3600
NOZZLE_PRESETS = [["PLA 220", 220], ["PETG 250", 250], ["ABS 260", 260], ["Выкл", 0]]


def _block(tag: str) -> str:
    m = re.search(r"/\* %s:begin \*/(.*?)/\* %s:end \*/" % (tag, tag), JS, re.S)
    assert m, "%s block not found in app.js" % tag
    return m.group(1)


def _temp_lib() -> str:
    """The AMS chart lib + amsTipTime, the chip, the setter popover and the temp-chart block."""
    return (_ams_chart_lib() + _js_function("amsTipTime") + "\n" + _function("icon") + "\n" +
            _function("tempChip") + "\n" + _js_function("tempPopHtml") + "\n" + _block("temp-chart") + "\n")


def _node_msk(src: str):
    """_node with TZ=Europe/Moscow: the tooltip's clock is deterministic."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    out = subprocess.run([node, "-"], input=src, capture_output=True, text=True, timeout=60, encoding="utf-8",
                         env=dict(os.environ, TZ="Europe/Moscow"))
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def trun(expr: str):
    """Evaluate `expr` (JSON-serializable) against the temperature chart helpers in node."""
    return _node_msk(_temp_lib() + "\nconsole.log(JSON.stringify(" + expr + "));")


def _payload(monkeypatch, rows, hours, to):
    """GET /api/printers/{id}/temps as the server builds it (temp_history.query_temps)
    from history rows (recorded_at, nozzle, target, bed, target, chamber, target)."""
    monkeypatch.setattr(temp_history, "_rows", lambda db, pid, fr, t: [r for r in rows if fr <= r[0] <= t])
    out = temp_history.query_temps("unused.db", "p1", to - hours * H, to)
    out["hours"] = hours
    return out


def _paths(svg: str):
    """The d attributes of the chart's paths: the curve, its area, the target and the band."""
    return {"line": re.findall(r'<path d="([^"]*)" fill="none" stroke="[^"]*" stroke-width="1.6"', svg),
            "area": re.findall(r'<path d="([^"]*)" fill="url\(#', svg),
            "tgt": re.findall(r'<path class="tgt" d="([^"]*)"', svg),
            "band": re.findall(r'<path class="band" d="([^"]*)"', svg)}


def test_temp_chart_block_is_pure():
    block = _block("temp-chart")
    for banned in ("document.", "window.", "fetch(", "apiFetch", "cachedAllPrinters", "localStorage",
                   "setInterval", "setTimeout"):
        assert banned not in block, banned


def test_series_breaks_on_holes_and_drops_heater_off_targets():
    resp = {"step": 90, "t": [T0, T0 + 90, T0 + 180, T0 + 880, T0 + 970],
            "series": {"nozzle": {"v": [200, 210, None, 220, 221], "target": [220, 220, None, 0, 230],
                                  "stats": {"min": 200, "avg": 213, "max": 221, "n": 4}}}}
    s = trun("tempSeries(%s, 'nozzle', null, %d)" % (json.dumps(resp), T0 + 1000))
    # a 700 s hole (> 600 s) gets a null point at the last time before it
    assert [p["t"] for p in s["cur"]] == [T0, T0 + 90, T0 + 180, T0 + 180, T0 + 880, T0 + 970]
    assert [p["v"] for p in s["cur"]] == [200, 210, None, None, 220, 221]
    assert [p["v"] for p in s["tgt"]] == [220, 220, None, None, None, 230]     # 0 = heater off: no line
    assert s["band"] is None and s["live"] is False and s["stats"]["n"] == 4
    # the hole threshold grows with the step: 2.5 steps of 360 s = 900 s
    s24 = trun("tempSeries(%s, 'nozzle', null, %d)" % (json.dumps(dict(resp, step=360)), T0 + 1000))
    assert [p["v"] for p in s24["cur"]] == [200, 210, None, 220, 221]


def test_live_point_joins_only_a_fresh_line():
    resp = {"step": 90, "t": [T0, T0 + 90], "series": {"bed": {"v": [60, 61], "target": [65, 65]}}}
    fresh = trun("tempSeries(%s, 'bed', {cur: 62.4, tgt: 0}, %d)" % (json.dumps(resp), T0 + 300))
    assert fresh["live"] is True and fresh["cur"][-1] == {"t": T0 + 300, "v": 62.4}
    assert fresh["tgt"][-1] == {"t": T0 + 300, "v": None}                      # off now: no target at the end
    stale = trun("tempSeries(%s, 'bed', {cur: 62.4, tgt: 65}, %d)" % (json.dumps(resp), T0 + 90 + 601))
    assert stale["live"] is False and len(stale["cur"]) == 2
    assert trun("tempSeries(%s, 'bed', null, %d)" % (json.dumps(resp), T0 + 120))["live"] is False
    assert trun("tempSeries(%s, 'bed', {cur: null, tgt: 65}, %d)" % (json.dumps(resp), T0 + 120))["live"] is False
    # fresh from this sensor's last value: its empty last buckets do not cut the live end off
    tail = {"step": 90, "t": [T0, T0 + 90, T0 + 180], "series": {"bed": {"v": [60, 61, None], "target": [65, 65, 65]}}}
    j = trun("tempSeries(%s, 'bed', {cur: 62, tgt: 65}, %d)" % (json.dumps(tail), T0 + 300))
    assert [p["v"] for p in j["cur"]] == [60, 61, 62] and [p["v"] for p in j["tgt"]] == [65, 65, 65]
    only_null = {"step": 90, "t": [T0], "series": {"bed": {"v": [None], "target": [None]}}}
    assert trun("tempSeries(%s, 'bed', {cur: 62, tgt: 65}, %d)" % (json.dumps(only_null), T0 + 60))["live"] is False
    # a 7-day bucket is the mean of 42 min of rows: fresh up to 2.5 steps (6300 s)
    week = dict(resp, step=2520, series={"bed": {"v": [60, 61], "target": [65, 65], "lo": [59, 60], "hi": [61, 62]}})
    w = trun("tempSeries(%s, 'bed', {cur: 63, tgt: 65}, %d)" % (json.dumps(week), T0 + 90 + 3000))
    assert w["live"] is True and w["band"][-1] == {"t": T0 + 3090, "lo": 63, "hi": 63}


def test_missing_series_or_empty_window_shows_no_data():
    for resp in ({"step": 90, "t": [], "series": {}},
                 {"step": 90, "t": [T0], "series": {"bed": {"v": [60], "target": [None]}}},
                 {"step": 90, "t": [T0], "series": {"nozzle": {"v": [None], "target": [None]}}}):
        v = trun("tempChartView(%s, 'nozzle', 6, {cur: 215, tgt: 220}, %d)" % (json.dumps(resp), T0 + 60))
        assert v["html"] == '<div class="ams-nodata">нет данных за период</div>'
        assert v["on"] is False and v["dot"] is None and v["stats"] == "" and not v["tgt"] and not v["band"]


def test_six_hours_from_the_server_payload(monkeypatch):
    to = T0 + 6 * H
    # the nozzle heats to 220 for a print, the printer is off-line for 40 min, then idles with the heater off
    rows = [(T0 + 45 + 90 * i, 30.0 + i if i < 190 else 220.0, 220.0, 60.0, 60.0, 35.0, None) for i in range(200)]
    rows += [(T0 + 45 + 90 * i, 40.0, None, 30.0, 0.0, 30.0, None) for i in range(227, 240)]
    resp = _payload(monkeypatch, rows, 6, to)
    assert resp["step"] == 90 and "lo" not in resp["series"]["nozzle"]
    v = trun("tempChartView(%s, 'nozzle', 6, {cur: 41, tgt: 0}, %d)" % (json.dumps(resp), to))
    p = _paths(v["html"])
    assert v["html"].startswith('<svg class="ams-chart" viewBox="0 0 300 112" preserveAspectRatio="none">')
    assert 'id="tcg-nozzle"' in v["html"] and 'stroke="var(--warn)"' in v["html"]
    assert '>-6ч</text>' in v["html"] and '>сейчас</text>' in v["html"]
    assert len(p["line"]) == 1 and p["line"][0].count("M") == 2            # two runs around the hole
    assert len(p["area"]) == 1 and p["area"][0].count("Z") == 2
    assert len(p["tgt"]) == 1 and p["tgt"][0].count("M") == 1                # the target only while heating
    assert 'stroke-dasharray="4 3"' in v["html"] and p["band"] == []
    assert v["tgt"] is True and v["band"] is False and v["on"] is True
    assert v["ymin"] == 30 and v["ymax"] == 220 and v["fr"] == to - 6 * H and v["to"] == to
    st = resp["series"]["nozzle"]["stats"]
    assert v["stats"] == "мин %d° · ср %d° · макс %d°" % (round(st["min"]), round(st["avg"]), round(st["max"]))
    # the live end sits on the right edge at the card's value
    assert v["s"]["live"] is True and v["s"]["cur"][-1] == {"t": to, "v": 41}
    assert v["dot"]["left"] == pytest.approx(292 / 300 * 100)
    assert v["dot"]["top"] == pytest.approx((8 + 88 * (1 - (41 - 30) / 190)) / 112 * 100)


def test_week_from_the_server_payload_has_the_band(monkeypatch):
    to = T0 + 168 * H
    rows = [(T0 + 45 + 90 * i, None, None, 60.0 + (i % 3), 60.0, None, None) for i in range(6720)
            if not 2000 <= i < 2400]                                          # a 10 h hole
    resp = _payload(monkeypatch, rows, 168, to)
    assert resp["step"] == 2520 and "nozzle" not in resp["series"]
    v = trun("tempChartView(%s, 'bed', 168, null, %d)" % (json.dumps(resp), to))
    p = _paths(v["html"])
    assert '>-7д</text>' in v["html"] and 'stroke="var(--info)"' in v["html"]
    assert p["line"][0].count("M") == 2 and len(p["band"]) == 2               # a band per run, broken by the hole
    assert v["html"].count('fill="var(--info)" opacity="0.12"') == 2
    assert v["band"] is True and v["dot"] is None                              # no live value: no dot
    nodata = trun("tempChartView(%s, 'nozzle', 168, {cur: 25, tgt: 0}, %d)" % (json.dumps(resp), to))
    assert nodata["on"] is False and "нет данных" in nodata["html"]


def test_chamber_without_targets_draws_no_target_line(monkeypatch):
    to = T0 + 24 * H
    rows = [(T0 + 45 + 90 * i, 25.0, None, 25.0, None, 30.0 + (i % 4) * 0.5, None) for i in range(960)]
    resp = _payload(monkeypatch, rows, 24, to)
    v = trun("tempChartView(%s, 'chamber', 24, {cur: 31, tgt: 0}, %d)" % (json.dumps(resp), to))
    assert _paths(v["html"])["tgt"] == [] and 'class="tgt"' not in v["html"] and v["tgt"] is False
    assert 'stroke="var(--ok)"' in v["html"] and '>-24ч</text>' in v["html"] and v["band"] is True
    assert v["ymin"] == 20 and v["ymax"] == 40                                 # at least 20° tall


def test_ams_chart_lifts_the_pen_on_null_points():
    out = trun("amsChart([{t: 0, v: 10}, {t: 10, v: 20}, {t: 20, v: null}, {t: 30, v: 30}], 0, 30, 0, 40, "
               "'var(--ok)', {geo: {W: 300, H: 112, PL: 28, PR: 8, PT: 8, PB: 16}, span: '-6ч', gid: 'x'})")
    p = _paths(out)
    assert p["line"] == ["M28.0 74.0 L116.0 52.0 M292.0 30.0 "]
    assert p["area"] == ["M28.0 74.0 L116.0 52.0 L116.0 96 L28.0 96 ZM292.0 30.0 L292.0 96 L292.0 96 Z"]
    assert 'id="x"' in out and '>-6ч</text>' in out and "stroke-linecap" not in out
    # the temperature chart's lone point (one bucket between two holes) is a dot, not nothing
    lone = trun("amsChart([{t: 0, v: 10}, {t: 10, v: 20}, {t: 20, v: null}, {t: 30, v: 30}], 0, 30, 0, 40, "
                "'var(--ok)', {geo: {W: 300, H: 112, PL: 28, PR: 8, PT: 8, PB: 16}, lone: true})")
    assert _paths(lone)["line"] == ["M28.0 74.0 L116.0 52.0 M292.0 30.0 L292.0 30.0 "]
    assert _paths(lone)["area"] == p["area"] and 'stroke-linejoin="round" stroke-linecap="round"/>' in lone
    assert trun("amsChart([{t: 0, v: null}], 0, 30, 0, 40, 'var(--ok)')") == \
        '<div class="ams-nodata">нет данных за период</div>'


@pytest.mark.parametrize("cur,tgt,band,want", [
    ([25, 255], [255], None, [20, 260]),
    ([36.2, 36.8], [], None, [30, 50]),                 # flat: 20° around the middle
    ([3, 5], [], None, [0, 20]),                        # never below 0
    ([60, 61], [None], [[55, 70]], [50, 70]),           # the band widens the range
    ([], [], None, [0, 100]),
])
def test_y_range(cur, tgt, band, want):
    s = {"cur": [{"t": i, "v": v} for i, v in enumerate(cur)], "tgt": [{"t": 0, "v": v} for v in tgt],
         "band": [{"t": 0, "lo": b[0], "hi": b[1]} for b in band] if band else None}
    assert trun("tempYRange(%s)" % json.dumps(s)) == want


def test_texts():
    assert trun("tempStatsText({min: 24.6, avg: 180.2, max: 255.4, n: 9})") == "мин 25° · ср 180° · макс 255°"
    assert trun("[tempStatsText(null), tempStatsText({})]") == ["", ""]
    assert trun("[tempNowText(60.2, 80), tempNowText(219, 220), tempNowText(30, 0), tempNowText(null, 80)]") == \
        ["60° → 80°", "219°", "30°", ""]
    assert trun("[6, 24, 168].map(tempHoursLabel)") == ["6ч", "24ч", "7д"]
    # 11:53 MSK; over 24 h with the date; no target -> no «цель»
    s = json.dumps({"tgt": [{"t": T0, "v": 220}, {"t": T0, "v": None}]})
    assert trun("[tempTipText(%s, {t: %d, v: 214.6, i: 0}, 6), tempTipText(%s, {t: %d, v: 214.6, i: 1}, 168)]"
                % (s, T0, s, T0)) == ["11:53 · 215° · цель 220°", "09.10 11:53 · 215°"]


def test_chart_block_markup():
    html = trun("tempChartHtml('bed', 24)")
    assert html.startswith('<div class="tc" style="--tc:var(--info)">')
    btns = re.findall(r'<button type="button" data-trange="(\d+)"( class="active")? aria-pressed="(\w+)">([^<]*)</button>',
                      html)
    assert btns == [("6", "", "false", "6ч"), ("24", ' class="active"', "true", "24ч"), ("168", "", "false", "7д")]
    assert '<div class="tc-box"><div class="ams-nodata">загрузка…</div></div>' in html
    assert '<span data-lg="tgt" hidden>' in html and '<span data-lg="band" hidden>' in html
    assert '<span class="tc-mm"></span>' in html


def test_admin_popover_keeps_the_setter_on_top_and_the_chart_under_it():
    old = ('<div class="pc-pop-h">Сопло, °C</div><div class="pc-pop-presets">'
           '<button type="button" class="pc-pop-preset" data-set="220">PLA 220</button>'
           '<button type="button" class="pc-pop-preset" data-set="250">PETG 250</button>'
           '<button type="button" class="pc-pop-preset" data-set="260">ABS 260</button>'
           '<button type="button" class="pc-pop-preset" data-set="0">Выкл</button></div>'
           '<div class="pc-pop-row"><input type="text" inputmode="numeric" class="pc-pop-in" value="220">'
           '<button type="button" class="pc-pop-ok" data-ok="preheat">OK</button></div>')
    p = json.dumps(NOZZLE_PRESETS, ensure_ascii=False)
    without, with6 = trun("[tempPopHtml('nozzle', 220, %s), tempPopHtml('nozzle', 220, %s, 6)]" % (p, p))
    assert without == old                                    # no hours: exactly the setter as before
    assert with6.startswith(old) and with6[len(old):] == trun("tempChartHtml('nozzle', 6)")
    assert with6.index('data-ok="preheat"') < with6.index('class="tc"') < with6.index("data-trange")


def test_read_only_popover_has_nothing_that_sends():
    html = trun("tempChartPopHtml('chamber', 168, '60° → 80°')")
    for sends in ("data-set", "data-ok", "data-speed", "data-menu", "<input", "pc-pop-preset", "pc-pop-ok"):
        assert sends not in html, sends
    assert html.startswith('<div class="pc-pop-h tc-h"><span>Камера, °C</span><span class="tc-now">60° → 80°</span></div>')
    assert html.count("data-trange") == 3 and 'data-trange="168" class="active"' in html
    assert '<span class="tc-now">&lt;b&gt;</span>' in trun("tempChartPopHtml('bed', 6, '<b>')")


def test_temp_chip_editable_or_chart_only():
    edit, chart, cold = trun("[tempChip('nozzle', 'Сопло', 215, 220, true), tempChip('chamber', 'Камера', 35, 0, false, 'temp'),"
                             " tempChip('bed', 'Стол', null, 60, false)]")
    assert ' pc-editable" data-edit="nozzle" data-cur="220"' in edit and "pc-chartable" not in edit
    assert ' pc-chartable" data-chart="chamber"' in chart
    assert "data-edit" not in chart and "data-cur" not in chart and 'href="#i-temp"' in chart
    assert cold == ""                                        # no value: no chip, nothing to open


def test_fetch_is_one_request_per_window_and_cached_a_minute():
    src = ("var TC_CACHE_MS = 60000, tcCache = {}, tcPending = {}, calls = [], fail = false, clock = 1000000;\n"
           "Date.now = function () { return clock; };\n"
           "function apiFetch(url) { calls.push(url); var bad = fail;\n"
           "  return Promise.resolve({ ok: !bad, status: bad ? 502 : 200, json: function () { return Promise.resolve({u: url}); } }); }\n"
           + _js_function("tempChartFetch") + "\n"
           "(async function () {\n"
           "  var out = {};\n"
           "  var a = tempChartFetch('p 1', 6), b = tempChartFetch('p 1', 6);\n"
           "  out.same = a === b; await a;\n"
           "  clock += 59000; await tempChartFetch('p 1', 6);\n"
           "  out.afterHit = calls.length;\n"
           "  await tempChartFetch('p 1', 24);\n"
           "  clock += 2000; await tempChartFetch('p 1', 6);\n"
           "  out.afterExpiry = calls.length; out.keys = Object.keys(tcCache).sort();\n"
           "  fail = true; try { await tempChartFetch('p2', 6); out.err = null; } catch (e) { out.err = e.message; }\n"
           "  fail = false; await tempChartFetch('p2', 6);\n"
           "  out.calls = calls; out.pending = Object.keys(tcPending).length;\n"
           "  console.log(JSON.stringify(out));\n"
           "})();\n")
    out = _node_msk(src)
    assert out["same"] is True and out["afterHit"] == 1 and out["afterExpiry"] == 3   # a reopen joins the request
    assert out["err"] == "HTTP 502" and out["pending"] == 0                         # a failure is not cached
    assert out["calls"] == ["/api/printers/p%201/temps?hours=6", "/api/printers/p%201/temps?hours=24",
                            "/api/printers/p%201/temps?hours=6", "/api/printers/p2/temps?hours=6",
                            "/api/printers/p2/temps?hours=6"]
    assert out["keys"] == ["p 1|24", "p 1|6"]


def test_wiring_period_switch_never_sends_and_stale_answers_are_dropped():
    pop = _js_function("onPopClick")
    assert '"[data-trange],' in pop
    assert pop.index("tempChartSwitch(") < pop.index("var sends")             # before any command path
    load = _js_function("tempChartLoad")
    assert load.count("tempChartCurrent(seq, pid, kind)") == 2               # the answer and the failure
    cur = _js_function("tempChartCurrent")
    for cond in ("seq === tcSeq", 'pcPop.style.display !== "none"', "pcPopPid === pid", "pcPop.dataset.kind === kind"):
        assert cond in cur, cond
    for fn in ("tempChartLoad", "tempChartDraw", "tempChartSwitch", "tempChartFetch", "tempPopTick"):
        body = _js_function(fn)
        assert "setInterval" not in body and "setTimeout" not in body, fn   # no timers: an open chart follows the poll
    assert "tcShown = null;" in load                                        # loading: a poll does not redraw the old answer
    # the card's chip opens it for everyone; a click on it does not close the popover it opens
    ctl = JS[JS.index("(function initPrinterControls() {"):JS.index("// --- Printer notifications")]
    assert ctl.count('closest(".pc-editable, .pc-chartable")') == 2
    assert "tempChartPopHtml(kind, hours" in ctl and 'tempPopHtml(kind, chip.dataset.cur || "", presets, hours)' in ctl
    assert "tempChartLoad(pid, kind, hours)" in ctl
    assert "tempPopTick();" in _js_function("applyAndRender")


def test_poll_redraws_the_live_end_from_the_answer_it_has():
    src = (_temp_lib() + _js_function("tempChartLive") + "\n" + _js_function("tempLiveKey") + "\n"
           + _js_function("tempChartCurrent") + "\n" + _js_function("tempPopTick") + "\n" + r"""
var TC_CACHE_MS = 60000, tcCache = {}, tcSeq = 1, tcShown = null, tcRefreshAt = 0, clock = 1000000;
Date.now = function () { return clock; };
console.warn = function () {};
var p = {bed_temp: 60.2, target_bed_temp: 80}, hovered = false, fail = false, draws = [], fetches = [];
function findPrinter(id) { return id === "b1" ? p : null; }
function isOffline(x) { return !!x.off; }
var nowEl = {textContent: ""}, box = {matches: function (s) { return s === ":hover" && hovered; }};
var pcPopPid = "b1", pcPop = {style: {display: "block"}, dataset: {kind: "bed"},
  querySelector: function (s) { return s === ".tc-now" ? nowEl : s === ".tc-box" ? box : null; }};
function tempChartDraw(pid, kind, hours, resp) {
  draws.push(resp);
  tcShown = {pid: pid, kind: kind, hours: hours, resp: resp, key: tempLiveKey(tempChartLive(findPrinter(pid), TEMP_KINDS[kind]))};
}
function tempChartFetch(pid, hours) {
  fetches.push(pid + "|" + hours);
  if (fail) return Promise.reject(new Error("HTTP 502"));
  tcCache[pid + "|" + hours] = {at: clock, resp: "new" + fetches.length};
  return Promise.resolve("new" + fetches.length);
}
function tick() { tempPopTick(); return draws.length + "/" + fetches.length; }
(async function () {
  var out = {};
  tcCache["b1|6"] = {at: clock, resp: "held"};
  tempChartDraw("b1", "bed", 6, "held"); draws = [];          // the popover opened at 60° -> 80°
  out.same = tick();
  p.bed_temp = 60.4; out.rounded = tick();                    // the same on the chart: no redraw
  out.header = nowEl.textContent;
  p.bed_temp = 61; hovered = true; out.hover = tick();        // under the pointer: the hover stays put
  hovered = false; out.left = tick();                         // then caught up, no request (cache fresh)
  clock += 61000; p.bed_temp = 62; out.stale = tick();        // drawn at once, refreshed in the background
  await null; await null; out.refreshed = draws.slice();
  clock += 3000; p.bed_temp = 63; out.fresh = tick();
  clock += 61000; fail = true; p.bed_temp = 64; out.failed = tick(); await null; await null;
  clock += 3000; p.bed_temp = 65; out.throttled = tick();     // a failure is retried a minute later, not every poll
  clock += 60000; fail = false; p.bed_temp = 66; out.retried = tick();
  tcSeq++; await null; await null; out.switched = draws.slice(-1)[0];   // a period switch drops it
  p.off = true; out.offline = tick(); out.header_off = nowEl.textContent;
  pcPopPid = "b2"; p.off = false; out.other = tick();
  pcPopPid = "b1"; tcShown = null; out.loading = tick();
  console.log(JSON.stringify(out));
})();
""")
    out = _node_msk(src)
    assert out["same"] == "0/0" and out["rounded"] == "0/0" and out["header"] == "60° → 80°"
    assert out["hover"] == "0/0" and out["left"] == "1/0"
    assert out["stale"] == "2/1" and out["refreshed"] == ["held", "held", "new1"]
    assert out["fresh"] == "4/1"
    assert out["failed"] == "5/2" and out["throttled"] == "6/2" and out["retried"] == "7/3"
    assert out["switched"] == "new1"                                   # the answer to the old load is not drawn
    assert out["offline"] == "8/3" and out["header_off"] == ""         # the live end goes with the card's value
    assert out["other"] == "8/3" and out["loading"] == "8/3"


def test_target_holds_until_the_next_bucket():
    geo = "{W: 300, H: 112, PL: 28, PR: 8, PT: 8, PB: 16}"
    tgt = [{"t": 0, "v": None}, {"t": 10, "v": 70}, {"t": 20, "v": None},       # one bucket, then heater off
           {"t": 30, "v": 50}, {"t": 40, "v": 50}, {"t": 40, "v": None},        # a run, then a hole's marker
           {"t": 60, "v": 60}]
    o = trun("amsChartOverlay(%s, 0, 100, 0, 100, {target: %s, targetColor: 'var(--info)'})" % (geo, json.dumps(tgt)))
    assert re.findall(r'<path class="tgt" d="([^"]*)"', o["labels"]) == \
        ["M54.4 34.4H80.8M107.2 52.0H133.6V52.0H133.6M186.4 43.2"]
    # a preheat seen in one 24 h bucket is drawn one bucket wide, not as a bare "M"
    resp = {"step": 360, "t": [T0 + 360 * i for i in range(6)],
            "series": {"bed": {"v": [16, 16, 17, 18, 18, 17], "target": [None, None, 70, None, None, None]}}}
    v = trun("tempChartView(%s, 'bed', 24, null, %d)" % (json.dumps(resp), T0 + 2000))
    d = _paths(v["html"])["tgt"]
    assert len(d) == 1 and re.fullmatch(r"M[\d.]+ [\d.]+H[\d.]+", d[0]) and v["tgt"] is True
    m = re.fullmatch(r"M([\d.]+) [\d.]+H([\d.]+)", d[0])
    assert float(m.group(2)) - float(m.group(1)) == pytest.approx(264 * 360 / (24 * H), abs=0.1)


def test_wiring_touch_keeps_the_tooltip_and_the_keyboard_closed():
    hov = _js_function("amsAttachHover")
    assert "box.addEventListener('pointerleave', hide)" not in hov
    assert "if (!opts.clamp || e.pointerType === 'mouse') hide();" in hov   # the AMS tab hides as before
    src = (r"""
var pcPop = null, pcPopPid = null, pcPopAnchorRect = null, coarse = true, focused = [];
var TEMP_KINDS = {nozzle: {}, bed: {}, chamber: {}};
var window = {matchMedia: function (q) { return {matches: q === "(pointer: coarse)" && coarse}; }};
var thePop = {dataset: {}, style: {}, innerHTML: "", querySelector: function () {
  return {focus: function () { focused.push(thePop.dataset.kind + (coarse ? ":coarse" : "")); }, select: function () {}}; }};
function ensurePop() { pcPop = thePop; return thePop; }
function nfClose() {}
function placePop() {}
var pcPopBack = {hidden: true};   // a phone's sheet (plan step 15, tests/test_web_phone_ui.py)
function sheetPhone() { return false; }
function sheetOpened() {}
function pcPopClose() {}
var chip = {getBoundingClientRect: function () { return {left: 0, top: 0, right: 60, bottom: 24}; }};
""" + _js_function("openPop") + r"""
["nozzle", "bed", "chamber", "fan"].forEach(function (k) { openPop(chip, "b1", "", k); });
coarse = false; openPop(chip, "b1", "", "nozzle");
console.log(JSON.stringify(focused));
""")
    assert _node(src) == ["fan:coarse", "nozzle"]                 # a phone: no keyboard over the chart


def _rule(sel: str) -> str:
    m = re.search(re.escape(sel) + r"\s*\{([^}]*)\}", CSS)
    assert m, sel
    return m.group(1)


def test_css():
    assert "cursor: pointer" in _rule(".pc-chip.pc-chartable")
    assert "width: min(320px, calc(100vw - 16px))" in _rule(
        '.pc-pop[data-kind="nozzle"], .pc-pop[data-kind="bed"], .pc-pop[data-kind="chamber"]')
    assert "height: 112px" in _rule(".tc-box .ams-chart, .tc-box .ams-nodata")    # placeholder = chart: no jump
    assert "visibility: hidden" in _rule(".tc:not(.tc-on) .tc-legend")
    m = re.search(r"@media \(max-width: 640px\) \{\s*\.tc \.ams-range button \{([^}]*)\}", CSS)
    assert m and "min-height: 36px" in m.group(1) and "min-width: 36px" in m.group(1)
