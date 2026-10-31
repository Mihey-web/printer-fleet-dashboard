"""The auto-dry helpers of app/web/app.js (the /* autodry-ui */ block), run in node,
plus source checks of the section markup. The block holds only pure code; it
may use safeText, icon, amsUnitName, amsFmtMin and the ams-ui block. Node runs
with TZ=Europe/Moscow so adClock is deterministic. Skipped without node.
"""
import datetime
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app.services import ams_autodry

ROOT = Path(__file__).resolve().parents[1]
JS = (ROOT / "app" / "web" / "app.js").read_text(encoding="utf-8")
HTML = (ROOT / "app" / "web" / "index.html").read_text(encoding="utf-8")
CSS = (ROOT / "app" / "web" / "styles.css").read_text(encoding="utf-8")
NOW = datetime.datetime(2026, 10, 12, 9, 0, tzinfo=datetime.timezone.utc).timestamp()   # 12:00 MSK
H = 3600


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
    out = subprocess.run([node, "-"], input=src, capture_output=True, text=True, timeout=60, encoding="utf-8",
                         env=dict(os.environ, TZ="Europe/Moscow"))
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _lib() -> str:
    src = "\n".join(_function(n) for n in ("safeText", "icon", "amsUnitName", "amsFmtMin"))
    return src + "\n" + _block("ams-ui") + "\n" + _block("autodry-ui")


def run(expr: str):
    return _node(_lib() + "\nconsole.log(JSON.stringify(" + expr + "));")


def test_block_is_pure():
    block = _block("autodry-ui")
    for banned in ("document.", "window.", "fetch(", "apiFetch", "cachedAllPrinters", "localStorage"):
        assert banned not in block, banned


# --- status lines -----------------------------------------------------------------------

CFG = dict(ams_autodry.DEFAULT_CONFIG, dry_run=False)
LIMITS = {"interrupt_limit": 2, "tz_offset_min": 180}
PLAN = {"mode": "auto", "name": "PETG", "filament": "PETG", "temp": 65, "hours": 12, "why": "пресет Bambu для PETG",
        "warn": False, "limit": None, "empty": False}


def unit(status, **kw):
    u = {"key": "bambu-1__0", "label": "P1", "name": "AMS 1", "rh": 23, "temp": 26, "online": True,
         "dry": {"type": "n3f", "active": False, "remaining_min": None, "max_temp": 65, "feeding": False,
                 "blockers": []},
         "plan": PLAN, "runs_24h": 1, "interrupts": 0, "status": status}
    u.update(kw)
    return u


def line(status, cfg=None, **kw):
    return run("autodryLine(%s, %s, %s, %s)" % (json.dumps(unit(status, **kw)), json.dumps(cfg or CFG),
                                               json.dumps(LIMITS), NOW))


@pytest.mark.parametrize("status,kw,cls,card,main,sub,link", [
    ({"kind": "pending"}, {}, "off", "проверяем…", "—", "автосушка ещё не проверила блок", None),
    ({"kind": "off", "scope": "master"}, {}, "off", "выкл (общий выключатель)", "Выкл", "общий выключатель", None),
    ({"kind": "off", "scope": "master", "drying": True}, {}, "off",
     "выкл (общий выключатель) · идущую сушку не трогаем", "Выкл", "общий выключатель", None),
    ({"kind": "off", "scope": "unit"}, {}, "off", "выкл", "Выкл", "только наблюдаем · сейчас 23%", None),
    ({"kind": "off", "scope": "unit", "drying": True}, {}, "off", "выкл · идущую сушку не трогаем", "Выкл",
     "только наблюдаем · сейчас 23%", None),
    ({"kind": "off", "scope": "unit", "reason": "interrupts"}, {}, "alert", "⚠ сушка срывается · блок отключён",
     "⚠ Выкл: 2 срыва подряд", "проверьте AMS, потом «включить снова»", "reenable"),
    ({"kind": "offline"}, {}, "off", "принтер офлайн", "Офлайн", "ждём связи с принтером", None),
    ({"kind": "unsupported", "reason": "no_fun2"}, {}, "off", "прошивка не даёт сушить удалённо", "Недоступна",
     "прошивка не сообщает о поддержке удалённой сушки", None),
    ({"kind": "unsupported", "reason": "locked"}, {}, "off", "прошивка блокирует сторонние команды", "Недоступна",
     "команды с дашборда заблокированы — не отправляем", None),
    ({"kind": "empty"}, {}, "off", "нет катушек", "Нет катушек", "зарядите катушки — автосушка их ждёт", None),
    ({"kind": "no_sensor"}, {}, "off", "нет данных о влажности", "Нет данных", "датчик влажности молчит", None),
    ({"kind": "wait", "rest": {"reason": "below", "last": 23}}, {}, "wait", "ждёт · 23% (старт от 25%)", "Ждёт",
     "23% — ниже старта 25%", None),
    ({"kind": "wait", "rest": {"reason": "holding", "last": 26, "above_since": NOW - 40 * 60,
                               "ready_at": NOW + 80 * 60}}, {"rh": 26}, "wait",
     "ждёт · 26% держится 40\u00a0мин из 2\u00a0ч", "Ждёт", "≥25% с 11:20 · старт в 13:20", None),
    ({"kind": "wait", "rest": {"reason": "after_dry", "ready_at": NOW + 2 * H}}, {}, "wait",
     "ждёт · покой считаем с 12:00", "Ждёт", "после сушки — 2\u00a0ч покоя до старта", None),
    ({"kind": "wait", "rest": {"reason": "no_data"}}, {}, "wait", "ждёт · мало данных о влажности", "Ждёт",
     "мало замеров за последние 2\u00a0ч", None),
    ({"kind": "wait", "rest": {"reason": "drying"}}, {}, "wait", "ждёт · мало данных о влажности", "Ждёт",
     "мало замеров за последние 2\u00a0ч", None),
    ({"kind": "dry_run"}, {}, "foreign", "пробный режим: запустили бы сейчас", "Пробный режим",
     "65° · 12\u00a0ч — команды не отправляются", None),
    ({"kind": "refused", "until": NOW + 600, "detail": "нет связи"}, {}, "alert", "старт не прошёл · повтор в 12:10",
     "Отказ", "нет связи · повтор в 12:10", None),
    ({"kind": "blocked", "text": "AMS занят"}, {}, "wait", "ждёт: AMS занят", "Ждёт", "AMS занят", None),
    ({"kind": "printing"}, {}, "wait", "ждёт конца печати", "Ждёт печать", "сушка во время печати выключена в правилах",
     None),
    ({"kind": "limit", "until": NOW + 14 * H, "runs_24h": 2}, {"runs_24h": 2}, "wait",
     "лимит 2 прогона в сутки · дальше после 13.10 02:00", "Лимит",
     "2 из 2 за 24\u00a0ч (с чужой сушкой) · следующий не раньше 13.10 02:00", None),
    ({"kind": "window", "opens_at": NOW + 10 * H, "window": {"from": "22:00", "to": "08:00"}}, {}, "wait",
     "ждёт окна с 22:00", "Ждёт окна", "окно 22:00–08:00 (МСК)", None),
    ({"kind": "starting"}, {}, "dry", "запускаем сушку…", "Запуск", "ждём подтверждения от AMS", None),
    ({"kind": "dry", "series": 1, "origin": "auto", "temp": 65, "filament": "PETG", "remaining_min": 425}, {}, "dry",
     "сушит · прогон 1 · ещё 7\u00a0ч 05\u00a0мин", "Сушит · прогон 1", "осталось 7:05 · 65° · PETG · 1 из 2 за сутки", None),
    ({"kind": "dry", "series": 1, "origin": "now", "temp": 45, "filament": "PLA", "remaining_min": 40}, {}, "dry",
     "сушит (вручную) · ещё 40\u00a0мин", "Сушит вручную", "осталось 40\u00a0мин · 45° · PLA · в лимит не входит", None),
    ({"kind": "dry", "series": 1, "origin": "manual", "temp": 45, "filament": "PLA", "remaining_min": 40}, {}, "dry",
     "сушит (вручную) · ещё 40\u00a0мин", "Сушит вручную", "осталось 40\u00a0мин · 45° · PLA · в лимит не входит", None),
    ({"kind": "foreign", "since": NOW - 2 * H, "remaining_min": 600}, {}, "foreign",
     "чужая сушка · ещё 10\u00a0ч 00\u00a0мин — не вмешиваемся", "Чужая сушка",
     "с 10:00 · осталось 10:00 · засчитаем как прогон", None),
    ({"kind": "fault", "reason": "overheat", "remaining_min": 300}, {}, "alert",
     "⚠ ошибка сушки AMS (перегрев) — проверьте блок", "⚠ Ошибка сушки", "перегрев · сами не останавливаем", None),
    ({"kind": "fault", "reason": "error", "remaining_min": 300}, {}, "alert",
     "⚠ ошибка сушки AMS (ошибка) — проверьте блок", "⚠ Ошибка сушки", "ошибка · сами не останавливаем", None),
    ({"kind": "fault", "reason": "stuck", "remaining_min": 30}, {}, "alert",
     "⚠ сушка идёт дольше плана — проверьте блок", "⚠ Дольше плана", "осталось 30\u00a0мин · сами не останавливаем", None),
    ({"kind": "cool", "end_kind": "finish", "series": 1, "until": NOW + H, "dry_end_at": NOW}, {}, "cool",
     "остывает после прогона 1 · замер в 13:00", "Остывает", "закончилась в 12:00 · замер в 13:00, цель ≤ 20%", None),
    ({"kind": "cool", "end_kind": "foreign_end", "series": 1, "until": NOW + H, "dry_end_at": NOW}, {}, "cool",
     "остывает после чужой сушки · замер в 13:00", "Остывает", "закончилась в 12:00 · замер в 13:00, цель ≤ 20%",
     None),
    ({"kind": "cool", "end_kind": "stop", "series": 1, "until": NOW + H, "dry_end_at": NOW}, {}, "cool",
     "остывает после остановки · замер в 13:00", "Остывает", "закончилась в 12:00 · замер в 13:00, цель ≤ 20%", None),
    ({"kind": "cool", "measuring": True, "temp": 38, "temp_max": 29, "t_before": 26, "deadline": NOW + 2 * H,
      "until": NOW + 2 * H, "end_kind": "finish", "series": 1, "dry_end_at": NOW - H}, {}, "cool",
     "остывает · 38° → ≤29°, замер не позже 14:00", "Остывает",
     "тёплый AMS занижает влажность — ждём 29° (до сушки было 26°)", None),
    ({"kind": "cool", "measuring": True, "temp": 27, "temp_max": 29, "t_before": 26, "deadline": NOW + 2 * H,
      "until": NOW + 2 * H, "end_kind": "finish", "series": 1, "dry_end_at": NOW - H}, {}, "cool",
     "остывает · замер влажности…", "Замер", "ждём свежих замеров влажности", None),
    ({"kind": "pause", "reason": "manual_stop", "until": NOW + 24 * H, "interrupts": 0}, {}, "pause",
     "пауза до 13.10 12:00 — остановлена вручную", "Пауза до 13.10 12:00", "остановлена вручную · правило 24\u00a0ч",
     "release"),
    ({"kind": "pause", "reason": "stop_all", "until": NOW + 24 * H, "interrupts": 0}, {}, "pause",
     "пауза до 13.10 12:00 — остановлена вручную", "Пауза до 13.10 12:00", "остановлена вручную · правило 24\u00a0ч",
     "release"),
    ({"kind": "pause", "reason": "foreign_stop", "until": NOW + 24 * H, "interrupts": 0}, {}, "pause",
     "пауза до 13.10 12:00 — чужую сушку остановили раньше срока", "Пауза до 13.10 12:00",
     "кто-то остановил сушку — не трогаем 24\u00a0ч", "release"),
    ({"kind": "pause", "reason": "interrupted", "until": NOW + 24 * H, "interrupts": 1}, {}, "alert",
     "⚠ сушка срывается · пауза до 13.10 12:00", "⚠ Срыв · пауза до 13.10 12:00",
     "погасла раньше срока (1 из 2 подряд)", "release"),
    ({"kind": "backoff", "until": NOW + 48 * H}, {}, "alert", "⚠ цель не достигается · новая серия после 14.10 12:00",
     "⚠ Цель не достигается", "2 прогона подряд выше 20% — проверьте осушитель/цель", "release"),
])
def test_autodry_line_kinds(status, kw, cls, card, main, sub, link):
    out = line(status, **kw)
    assert (out["cls"], out["card"], out["main"], out["sub"], out["link"]) == (cls, card, main, sub, link)
    assert out["noKey"] is (cls == "alert" and status["kind"] != "refused")


def test_numbers_keep_their_units_on_one_line():
    # «ч» / «мин» never wrap alone onto the next line of a status or a plan cell
    for kind, fields in (("wait", {"rest": {"reason": "holding", "last": 26, "above_since": NOW - 40 * 60,
                                            "ready_at": NOW + 80 * 60}}),
                         ("wait", {"rest": {"reason": "no_data"}}),
                         ("wait", {"rest": {"reason": "after_dry", "ready_at": NOW + H}}),
                         ("dry_run", {}), ("limit", {"until": NOW + H, "runs_24h": 2}),
                         ("dry", {"series": 1, "origin": "auto", "remaining_min": 425}),
                         ("dry", {"series": 1, "origin": "now", "remaining_min": 40}),
                         ("fault", {"reason": "stuck", "remaining_min": 30}),
                         ("foreign", {"since": NOW - H, "remaining_min": 600}),
                         ("pause", {"reason": "manual_stop", "until": NOW + H}),
                         ("pause", {"reason": "foreign_stop", "until": NOW + H})):
        out = line(dict(fields, kind=kind))
        text = " | ".join((out["card"], out["main"], out["sub"]))
        assert not re.search(r"\d (ч|мин)\b", text), (kind, text)
    cell = run("autodryRunCell(%s, %s)" % (json.dumps({"dry": {}, "materials": [{"type": "PETG"}]}), json.dumps(PLAN)))
    assert "65° · 12\u00a0ч</b>" in cell


def test_banner_lines():
    ad = {"now": NOW, "config": dict(CFG, dry_run=True),
          "engine": {"started": True, "started_at": NOW - 600, "last_tick": None, "error": None, "clock_wait": False}}
    lines = run("autodryBanner(%s, true)" % json.dumps(ad))
    assert len(lines) == 2 and 'id="adDryRunOff"' in lines[0] and lines[0].startswith("<div>Пробный режим:")
    assert lines[1] == "<div>Автосушка не отвечает (проверок ещё не было) — проверьте сервис.</div>"
    ad["engine"].update(last_tick=NOW - H)
    lines = run("autodryBanner(%s, false)" % json.dumps(ad))
    assert "adDryRunOff" not in lines[0]
    assert lines[1] == "<div>Автосушка не отвечает (последняя проверка в 11:00) — проверьте сервис.</div>"
    ad["engine"].update(last_tick=NOW - 60, clock_wait=True)
    ad["config"]["dry_run"] = False
    assert run("autodryBanner(%s, true)" % json.dumps(ad)) == [
        "<div>Часы сервера отстают — автосушка ждёт синхронизации времени.</div>"]
    ad["engine"].update(clock_wait=False, error="RuntimeError")
    assert "не отвечает" in run("autodryBanner(%s, true)" % json.dumps(ad))[0]
    # «Админ → Автоматика» (plan step 11): the trial mode has its own row there, the banner keeps
    # the engine's troubles only
    assert "autodryBanner(amsAutodry, false).slice(amsAutodry.config && amsAutodry.config.dry_run ? 1 : 0)" in \
        _js_function("adRenderBanner")


def test_status_kinds_covered():
    for kind in ams_autodry.STATUS_KINDS:
        out = line({"kind": kind})
        assert out["card"] and out["card"] != "—", kind


def test_a_unit_of_a_printer_in_maintenance():
    # spec_maintenance F18 / F19: its status line, and the run-now / stop dialogs list it disabled
    assert line({"kind": "maintenance"}) == {
        "cls": "off", "card": "принтер на обслуживании", "main": "Обслуживание",
        "sub": "автосушка не трогает блок, пока принтер на обслуживании", "noKey": False, "link": None}
    for name in ("adRunNowDlg", "adStopDlg"):
        src = _js_function(name)
        assert "if (u.maintenance) {" in src and "принтер на обслуживании" in src, name
    assert _js_function("adRunNowDlg").index("u.maintenance") < _js_function("adRunNowDlg").index("u.dry.active")


def test_repeat_prefix_and_escaping():
    out = line({"kind": "window", "opens_at": NOW + 10 * H, "window": {"from": "22:00", "to": "08:00"},
                "repeat": {"rh": 23, "target": 20}})
    assert out["card"] == "повтор (23% > 20%) · ждёт окна с 22:00"
    out = line({"kind": "blocked", "text": "<img src=x>"})
    assert "<img" not in out["card"] and "&lt;img" in out["sub"]
    out = line({"kind": "refused", "until": NOW + 600, "detail": "<b>x</b>"})
    assert "<b>" not in out["sub"]


# --- plans --------------------------------------------------------------------------------

CTX = {"cap": 65, "max_temp": 65, "softest": "PETG", "model": "AMS 2 Pro", "limit_text": "не выше 65° — потолок AMS 2 Pro"}


def test_plan_for():
    u = {"plans": {"auto": PLAN, "pla": dict(PLAN, name="PLA", temp=45)}, "plan_ctx": CTX}
    assert run("autodryPlanFor(%s, 'pla', null)" % json.dumps(u))["temp"] == 45
    assert run("autodryPlanFor(%s, 'auto', null)" % json.dumps(u)) == PLAN
    p = run("autodryPlanFor(%s, 'custom', {temp: 70, hours: 8})" % json.dumps(u))
    assert p == {"mode": "custom", "name": "Своё", "filament": "PETG", "temp": 65, "hours": 8,
                 "why": "задано вручную · потолок AMS 2 Pro 65°", "warn": False, "limit": None, "empty": False}
    low = dict(u, plan_ctx=dict(CTX, max_temp=55, limit_text="не выше 55° — идёт печать из этого AMS (PETG)"))
    p = run("autodryPlanFor(%s, 'custom', {temp: 60, hours: 30})" % json.dumps(low))
    assert (p["temp"], p["hours"], p["limit"], p["why"]) == (55, 24, "не выше 55° — идёт печать из этого AMS (PETG)",
                                                            "задано вручную")
    empty = {"plans": {"auto": None, "pla": PLAN}, "plan_ctx": CTX}
    assert run("autodryPlanFor(%s, 'auto', null)" % json.dumps(empty)) is None
    assert run("autodryPlanFor(null, 'auto', null)") is None


def test_run_cell_of_a_unit_with_unfed_slots_shows_its_plan():
    # owner, 14.10: slots without fed filament still hold spools - the unit dries, so the
    # cell shows the plan like any other (no "skipped" note any more)
    empty = {"dry": {"type": "n3f"}, "materials": []}
    petg = dict(PLAN, why="пресет Bambu", limit="не выше 55° — идёт печать из этого AMS (PETG)")
    html = run("autodryRunCell(%s, %s)" % (json.dumps(empty), json.dumps(petg)))
    assert html.startswith("<b>PETG → 65° · 12") and "катушек нет" not in html and "пресет Bambu" in html
    assert "не выше 55°" in html
    assert run("autodryRunCell(%s, null)" % json.dumps(empty)) == "<b>—</b>"
    full = {"dry": {"type": "n3f"}, "materials": [{"type": "PETG"}]}
    html = run("autodryRunCell(%s, %s)" % (json.dumps(full), json.dumps(PLAN)))
    assert '<span class="why">пресет Bambu для PETG</span>' in html
    assert run("autodryRunCell(%s, null)" % json.dumps(full)) == "<b>—</b>"
    assert run("autodryRunCell({dry: null}, null)") == '<b>—</b><span class="why">блок не на связи</span>'
    assert "return autodryRunCell(u, adPlanOf(u));" in _js_function("adRunCell")


def test_plan_for_matches_the_server_custom_branch():
    from app.services import ams_drying
    slots = [{"type": "PETG", "color": "FFF144", "empty": False}, {"type": "PLA", "color": "FFFFFF", "empty": False}]
    for feeding in (False, True):
        dry = {"type": "n3f", "feeding": feeding}
        dry.update(ams_drying._policy("n3f", slots, feeding))
        ctx = ams_autodry.plan_ctx(dry, slots)
        for t, h in ((44, 1), (45, 12), (50, 8), (65, 24), (70, 5)):
            cfg = dict(ams_autodry.DEFAULT_CONFIG, mode="custom", custom_temp=max(45, t), custom_hours=h)
            server = ams_autodry.plan_run(cfg, {"mode": "inherit"}, dry, slots)
            js = run("autodryPlanFor(%s, 'custom', {temp: %d, hours: %d})" % (json.dumps({"plan_ctx": ctx}),
                                                                               max(45, t), h))
            assert js == server, (feeding, t, h)


# --- the form -------------------------------------------------------------------------------

FORM = {"master": True, "start": "25", "hold": "2", "target": "20", "mode": "auto", "custT": "55", "custH": "8",
        "pause": "1", "limit": "2", "win": "any", "winFrom": "", "winTo": "", "print": True, "holdOff": "24",
        "units": {"bambu-1__0": {"enabled": True, "mode": "inherit", "temp": "55", "hours": "8"}}}
BASE = dict(ams_autodry.DEFAULT_CONFIG, units={"p-x__0": {"enabled": False, "mode": "pla", "temp": 55, "hours": 8}})


RANGES = {k: list(v) for k, v in ams_autodry.RANGES.items()}


def form_cfg(ranges=RANGES, **kw):
    f = json.loads(json.dumps(FORM))
    f.update(kw)
    return run("autodryFormToConfig(%s, %s, %s)" % (json.dumps(f), json.dumps(BASE), json.dumps(ranges)))


def test_form_to_config():
    out = form_cfg()["config"]
    assert out["dry_run"] is True                         # never edited by the form: from the saved config
    assert "rev" not in out and set(out) == set(ams_autodry.DEFAULT_CONFIG)
    assert out["units"]["p-x__0"] == BASE["units"]["p-x__0"]     # a unit missing from the form is kept
    assert out["units"]["bambu-1__0"] == {"enabled": True, "mode": "inherit", "temp": 55, "hours": 8}
    assert ams_autodry.validate_config(out) == out
    assert form_cfg(win="night")["config"]["window"] == {"from": "22:00", "to": "08:00"}
    assert form_cfg(win="custom", winFrom="21:30", winTo=" 07:00")["config"]["window"] == \
        {"from": "21:30", "to": "07:00"}
    assert form_cfg(win="any")["config"]["window"] is None
    assert form_cfg(master=False, print=False)["config"]["enabled"] is False


@pytest.mark.parametrize("kw,field,error", [
    ({"hold": ""}, "hold", "Не дёргаться от скачков: нужно целое число"),
    ({"target": "1.5"}, "target", "Держать влажность не выше: нужно целое число"),
    ({"target": "4"}, "target", "Держать влажность не выше: целое 5–55 %"),
    ({"target": "56"}, "target", "Держать влажность не выше: целое 5–55 %"),
    ({"pause": "-"}, "pause", "Пауза между прогонами: нужно целое число"),
    ({"limit": "a"}, "limit", "Лимит: нужно целое число"),
    ({"holdOff": " "}, "holdOff", "После ручной остановки: нужно целое число"),
    ({"custT": "x"}, "custT", "Своя температура: нужно целое число"),
    ({"win": "custom", "winFrom": "25:00", "winTo": "08:00"}, "winFrom", "Окно запуска: время ЧЧ:ММ"),
    ({"win": "custom", "winFrom": "22:00", "winTo": "8"}, "winTo", "Окно запуска: время ЧЧ:ММ"),
    ({"units": {"bambu-1__0": {"enabled": True, "mode": "custom", "temp": "hot", "hours": "8"}}},
     "u:bambu-1__0:temp", "Своя температура блока: нужно целое число"),
])
def test_form_errors(kw, field, error):
    assert form_cfg(**kw) == {"error": error, "field": field}


def test_one_number_start_is_derived_from_the_goal():
    # owner, 12.10: one number "keep it not above N %"; the start is one point above
    for target, start in (("5", 6), ("20", 21), ("55", 56)):
        out = form_cfg(target=target, start="999")["config"]   # a stale start field is ignored
        assert (out["target_rh"], out["start_rh"]) == (int(target), start)
        assert ams_autodry.validate_config(out) == out
    assert form_cfg(target="5", limit="0")["config"]["max_runs"] == 0


def _server_error(cfg):
    with pytest.raises(ValueError) as e:
        ams_autodry.validate_config(cfg)
    return str(e.value)


@pytest.mark.parametrize("kw,field,cfg", [
    ({"custT": "90"}, "custT", {"custom_temp": 90}),
    ({"custT": "44", "mode": "custom"}, "custT", {"custom_temp": 44}),
    ({"custH": "30", "mode": "custom"}, "custH", {"custom_hours": 30}),
    ({"units": {"bambu-1__0": {"enabled": True, "mode": "custom", "temp": "90", "hours": "8"}}},
     "u:bambu-1__0:temp", {"units": {"bambu-1__0": {"mode": "custom", "temp": 90}}}),
    ({"units": {"bambu-1__0": {"enabled": True, "mode": "custom", "temp": "60", "hours": "0"}}},
     "u:bambu-1__0:hours", {"units": {"bambu-1__0": {"mode": "custom", "hours": 0}}}),
])
def test_form_range_errors_match_the_server(kw, field, cfg):
    # checked in the browser (the field gets focus) with the server's own words, never the unit key
    error = _server_error(dict(ams_autodry.DEFAULT_CONFIG, **cfg))
    assert form_cfg(**kw) == {"error": error, "field": field}
    assert "bambu-1__0" not in error and "temp" not in error and "hours" not in error


def test_form_out_of_range_unit_values_outside_custom_mode_are_kept_from_the_config():
    out = form_cfg(units={"p-x__0": {"enabled": True, "mode": "pla", "temp": "99", "hours": "77"}})["config"]
    assert out["units"]["p-x__0"] == {"enabled": True, "mode": "pla", "temp": 55, "hours": 8}
    assert ams_autodry.validate_config(out) == out
    # no ranges (an older server): only integers are checked here, the server says the rest
    assert form_cfg(ranges=None, custT="90")["config"]["custom_temp"] == 90


def test_canon_ignores_default_unit_entries_and_key_order():
    a = dict(ams_autodry.DEFAULT_CONFIG, units={"k__0": {"enabled": True, "mode": "inherit", "temp": 55, "hours": 8}})
    b = dict(reversed(list(ams_autodry.DEFAULT_CONFIG.items())))
    assert run("autodryCanon(%s) === autodryCanon(%s)" % (json.dumps(a), json.dumps(b))) is True
    c = dict(ams_autodry.DEFAULT_CONFIG, units={"k__0": {"enabled": False, "mode": "inherit", "temp": 55, "hours": 8}})
    assert run("autodryCanon(%s) === autodryCanon(%s)" % (json.dumps(a), json.dumps(c))) is False


# --- charts ----------------------------------------------------------------------------------

def test_dry_bands():
    rows = [{"recorded_at": t, "dry_time": d} for t, d in
            ((0, None), (90, 700), (180, 699), (700, 690), (1400, 680), (1500, None), (2000, 10))]
    assert run("amsDryBands(%s, 600)" % json.dumps(rows)) == [[90, 700], [1400, 1490], [2000, 2090]]
    assert run("amsDryBands([], 600)") == []


GEO = {"W": 378, "H": 88, "PL": 26, "PR": 8, "PT": 8, "PB": 16}


def test_chart_overlay():
    fr, to = 0, 86400
    out = run("amsChartOverlay(%s, %d, %d, 10, 50, {dry: [[-100, 3600], [80000, 90000]], thr: "
              "[[25, 'старт', 'var(--warn)'], [20, 'цель', 'var(--ok)'], [60, 'x', 'var(--ok)']]})"
              % (json.dumps(GEO), fr, to))
    under, labels = out["under"], out["labels"]
    assert 'y1="48.0"' in under and 'y1="56.0"' in under and under.count('class="thr"') == 2
    assert '<rect x="26.0" y="8" width="14.3"' in under                 # clipped at fr
    assert 'x="344.5"' in under and 'width="25.5"' in under             # clipped at to (370 - 344.5)
    assert 'y="45.5"' in labels and ">старт 25%<" in labels
    assert 'y="64.5"' in labels and ">цель 20%<" in labels and "x 60%" not in labels
    assert run("amsChartOverlay(%s, 0, 100, 10, 50, {})" % json.dumps(GEO)) == {"under": "", "labels": ""}


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


def test_chart_draws_the_overlay_between_grid_and_area():
    src = _js_function("amsChart")
    assert "amsChartOverlay(" in src
    assert src.index("gl + ov.under") < src.index("fill=\"url(#") < src.index("ov.labels + yl")
    graphs = _js_function("amsDrawGraphs")
    assert "amsDryBands(rows, 600)" in graphs and graphs.count("dry: bands") == 3 and "thr: thr" in graphs


# --- journal and audit ------------------------------------------------------------------------

def test_audit_prefix_badge():
    # plan step 12: the audit table is «История → Действия», its words come from actText
    # (tests/test_web_acts_ui.py covers every auto-dry prefix there); the badge helper went with the old table
    assert "auditPrefixBadge" not in JS and "AUDIT_PREFIX_BADGES" not in JS


def test_journal_badges_cover_every_kind():
    kinds = ("start", "repeat", "run_now", "manual", "finish", "foreign_start", "foreign_end", "interrupted",
             "disabled", "lost", "fault", "manual_stop", "refused", "blocked", "released", "reenabled", "dry_run",
             "unreachable", "maintenance_on", "maintenance_off")
    badges = run("AD_JOURNAL_BADGES")
    assert set(kinds) <= set(badges)


def test_journal_row():
    e = {"id": 1, "ts": NOW - 600, "key": "k__0", "label": "<img src=x>", "name": "AMS 1", "kind": "dry_run",
         "text": "Пробный режим: запустили бы прогон 1 · 65° · 12 ч (PETG)", "rh_before": None, "rh_after": None,
         "t_before": None, "t_after": None, "pending": False, "count": 3}
    html = run("autodryJournalRow(%s, %s)" % (json.dumps(e), NOW))
    assert "×3" in html and "<img" not in html and "&lt;img" in html and ">11:50<" in html
    assert "badge-steel" in html and ">Пробный<" in html
    pend = dict(e, kind="finish", count=1, rh_before=26, pending=True, t_before=26)
    html = run("autodryJournalRow(%s, %s)" % (json.dumps(pend), NOW))
    # the AMS temperatures in a data-tip, not a title (IA §10, plan step 14 — deliberately changed)
    assert "RH 26% → ?" in html and 'data-tip="t° AMS: 26° → ?"' in html and "×" not in html and "title=" not in html
    done = dict(pend, rh_after=19, t_after=29, pending=False)
    html = run("autodryJournalRow(%s, %s)" % (json.dumps(done), NOW))
    assert "RH 26% → 19%" in html and 'data-tip="t° AMS: 26° → 29°"' in html
    only = dict(e, kind="start", count=1, rh_before=27)
    html = run("autodryJournalRow(%s, %s)" % (json.dumps(only), NOW))
    assert 'class="jr-rh none"' in html and "RH 27%" in html
    old = dict(only, ts=NOW - 30 * H)
    assert ">11.10 06:00<" in run("autodryJournalRow(%s, %s)" % (json.dumps(old), NOW))


def test_formatting():
    assert run("[adFmtHM(425), adFmtHM(40), adFmtHM(600), adFmtHM(0)]") == \
        ["7\u00a0ч 05\u00a0мин", "40\u00a0мин", "10\u00a0ч 00\u00a0мин", "0\u00a0мин"]
    assert run("[adPlural(1,'a','b','c'), adPlural(3,'a','b','c'), adPlural(11,'a','b','c'), adPlural(22,'a','b','c')]") == \
        ["a", "b", "c", "b"]
    assert run("[adClock(%d, %d), adClock(%d, %d)]" % (NOW, NOW, NOW + 13 * H, NOW)) == ["12:00", "13.10 01:00"]


# --- section DOM code, run in node against stub elements ----------------------------------------

_DOM_STUB = r"""
var els = {};
function stubEl(id) {
  return {id: id, hidden: false, innerHTML: "", textContent: "", disabled: false, value: "",
          classList: {on: {}, toggle: function (c, v) { this.on[c] = v === undefined ? !this.on[c] : !!v; },
                      add: function (c) { this.on[c] = true; }, remove: function (c) { this.on[c] = false; }},
          setAttribute: function (k, v) { this[k] = v; }, removeAttribute: function (k) { delete this[k]; },
          querySelectorAll: function () { return []; }, focus: function () {}, select: function () {}};
}
function adEl(id) { return els[id] || (els[id] = stubEl(id)); }
var document = {querySelectorAll: function () { return []; }};
var admin = true;
function isAdmin() { return admin; }
"""


def _dom(script: str, *names: str):
    return _node(_lib() + _DOM_STUB + "\n".join(_js_function(n) for n in names) + "\n" + script)


def test_a_bad_field_is_marked_until_the_form_is_valid():
    ids = re.search(r"var AD_FIELD_IDS = \{.*?\};", JS, re.S).group(0)
    out = _dom(ids + """
    var unitT = stubEl("u1");
    unitT.getAttribute = function (a) { return a === "data-ad-ct" ? "bambu-1__0" : null; };
    document.querySelectorAll = function (sel) {
      if (sel === "#adRows [data-ad-ct]") return [unitT];
      if (sel === "#autodry .num.bad") return [adEl("adCustT"), unitT].filter(function (x) { return x.classList.on.bad; });
      return [];
    };
    adMarkBad("custT");
    var a = [els.adCustT.classList.on.bad, els.adCustT["aria-invalid"]];
    adMarkBad("u:bambu-1__0:temp");
    var b = [els.adCustT.classList.on.bad, "aria-invalid" in els.adCustT, unitT.classList.on.bad];
    adMarkBad(null);
    console.log(JSON.stringify([a, b, unitT.classList.on.bad]));
    """, "adFieldEl", "adMarkBad")
    assert out == [[True, "true"], [False, False, True], False]
    # the form is checked against the server's ranges while typing and on save
    for fn in ("adOnFormInput", "adSave"):
        assert "autodryFormToConfig(adReadForm(), amsAutodry.config, adRanges())" in _js_function(fn)
    assert ".autodry .num.bad, .autodry .num.bad:focus { border-color: var(--danger); }" in CSS


def _run_now_html(u):
    return _dom("""
    var amsAutodry = {units: [%s], config: {}}, adForm = null, html = null;
    function adOpenDlg(h) { html = h; }
    function adBindDlg() {}
    adRunNowDlg();
    console.log(JSON.stringify(html));
    """ % json.dumps(u), "adRunNowDlg")


def test_run_now_dialog_says_the_feeding_limit_once():
    feeding = {"type": "n3f", "active": False, "remaining_min": None, "max_temp": 55, "feeding": True, "blockers": []}
    u = unit({"kind": "wait"}, dry=feeding, dry_ok=True, cmd_ok=True, phase="idle", seen_active=False,
             unit_cfg={"enabled": True}, hold_until=None, disabled_reason=None,
             plan_now=dict(PLAN, temp=55, limit="не выше 55° — идёт печать из этого AMS (PETG)"))
    html = _run_now_html(u)
    assert html.count("не выше 55°") == 1 and "идёт печать из этого AMS (PETG)" in html
    # a plan already under the limit has no limit line: then the warning tells it
    html = _run_now_html(dict(u, plan_now=dict(PLAN, temp=45, name="PLA", why="пресет Bambu для PLA")))
    assert "идёт печать из этого AMS — не выше 55°" in html


def test_render_without_data_hides_a_stale_banner():
    # GET /api/ams/autodry failed: the banner outside #adBody must not keep the last text
    # (or a «выключить пробный режим» button whose handler then does nothing)
    out = _dom("""
    var amsAutodry = null;
    function amsStripRender() {} function amsLegendRender() {} function adRenderJournal() {}
    adEl("adBanner").innerHTML = '<div>Пробный режим <button id="adDryRunOff">выключить пробный режим</button></div>';
    autodryRender();
    console.log(JSON.stringify({banner: [els.adBanner.hidden, els.adBanner.innerHTML],
                                body: els.adBody.hidden, na: els.adUnavailable.hidden}));
    """, "autodryRender")
    assert out == {"banner": [True, ""], "body": True, "na": False}


# --- the page: markup, wiring, cache-busting --------------------------------------------------

IDS = ("autodry", "adBanner", "adMaster", "adMasterSub", "adHold", "adTarget", "adMode",
       "adCustom", "adCustT", "adCustH", "adPause", "adLimit", "adWin", "adWinCustom", "adWinFrom", "adWinTo",
       "adPrint", "adHoldOff", "adDryAll", "adStopAll", "adRows", "adDirty", "adSaved", "adError", "adReset",
       "adSave", "adJournal", "adJournalMore", "adDlgBack", "adDlg", "amsStrip", "amsStripState", "amsStripActs",
       "amsRulesBtn", "amsLegend", "amsJournal", "adTrialSub", "adTrialBtn")


def test_section_markup():
    for i in IDS:
        assert 'id="%s"' % i in HTML, i
    sec = HTML[HTML.index('<section class="section-panel autodry"'):]
    assert "adTg" not in HTML and "Telegram" not in sec[:sec.index("</section>")]
    # IA plan step 11: the AMS tab is the strip (its commands on top), the blocks, the journal at the
    # bottom; the rules form is «Админ → Автоматика» (amsEmpty < journal < admin < autodry)
    ams = HTML[HTML.index('data-panel="ams"'):HTML.index('data-panel="admin"')]
    assert ams.index('id="amsStrip"') < ams.index('id="adDryAll"') < ams.index('id="adStopAll"') < \
        ams.index('id="amsRulesBtn"') < ams.index('id="amsRange"') < ams.index('id="amsGrid"') < \
        ams.index('id="amsEmpty"') < ams.index('id="amsJournal"') < ams.index('id="adJournal"')
    assert HTML.index('id="amsEmpty"') < HTML.index('id="amsJournal"') < HTML.index('data-panel="admin"') < \
        HTML.index('data-asub-panel="auto"') < HTML.index('id="autodry"') < HTML.index('data-asub-panel="access"')
    for gone in ("amsAdJump", "adRoleChip", "lgStart", "ad-cols", "ad-side"):
        assert gone not in HTML, gone
    form = HTML[HTML.index('data-asub-panel="auto"'):HTML.index('data-asub-panel="access"')]
    assert "<th>Статус</th>" not in form and "<th>Прогон</th>" not in form and "<th>Пластик</th>" not in form
    assert form.index('id="adMaster"') < form.index('id="adTrialBtn"') < form.index('id="adTarget"') < \
        form.index('id="adRows"') < form.index("Как это работает") < form.index('id="adSave"')
    assert '<details class="ad-more ad-how">' in form and '<thead><tr><th>Блок</th><th>Авто</th><th>Режим</th><th></th></tr></thead>' in form
    # IA plan step 3: «Настройки» are gone and so is their «Автосушка AMS → Открыть» (the
    # third way into the auto-dry); the Telegram proxies live in «Админ → Система»
    assert "autodryLink" not in HTML and "autodryLink" not in JS and "amsScrollToAutodry" not in JS
    assert 'data-panel="settings"' not in HTML
    start = HTML.index('data-asub-panel="system"')
    system = HTML[start:HTML.index("</section>\n      </div>\n    </section>", start)]
    assert 'id="tgSection"' in system and 'id="proxySection"' in system
    assert "anno" not in sec[:sec.index("</section>")]


def test_cache_bust_bumped():
    # at least the auto-dry release's versions; later changes bump them further
    assert int(re.search(r"styles\.css\?v=(\d+)", HTML).group(1)) >= 74
    assert int(re.search(r"app\.js\?v=(\d+)", HTML).group(1)) >= 82


def test_update_auth_ui_rerenders_autodry():
    m = re.search(r"function updateAuthUI\(\) \{(.*?)\n      \}", JS, re.S)
    assert m and "autodryRender" in m.group(1)


def test_audit_table_uses_prefix_badges():
    # the rows of «Действия» take their words from actText, the auto-dry's units included (plan step 12)
    assert "var t = actText(e.action)" in _js_function("actRowHtml")
    for prefix in ("autodry_run_now", "autodry_start", "autodry_stop", "autodry_release", "autodry_disable"):
        assert prefix + ":" in JS or prefix + ": " in JS, prefix


def test_viewer_toggles_look_disabled():
    # IA plan step 11: the form is «Админ → Автоматика» — a viewer has no such screen (routeForRole),
    # and the AMS tab holds no input of it; its strip's commands are hidden from a viewer (below)
    ams = HTML[HTML.index('data-panel="ams"'):HTML.index('data-panel="admin"')]
    assert "<input" not in ams and "<select" not in ams
    assert 'class="ams-strip-acts" id="amsStripActs" hidden' in ams
    # still: should a viewer ever get the form, every input is disabled and looks so
    css = CSS[CSS.index("/* === AMS auto-drying"):]
    assert ".autodry .toggle-switch input:disabled { cursor: default; }" in css
    assert ".autodry .toggle-switch input:disabled + .toggle-slider { opacity: .6; }" in css
    assert 'sec.querySelectorAll("input, select").forEach(function (el) { el.disabled = !admin; });' in \
        _js_function("autodryRender")


def test_css_scoping_and_no_animation():
    start = CSS.index("/* === AMS auto-drying")
    css = CSS[start:]
    assert "animation" not in css
    for bare in ("\n    .rules {", "\n    .rule {", "\n    .num {", "\n    .sel {", "\n    .dlg {", "\n    .btn2 {"):
        assert bare not in css, bare
    assert ".autodry .sel { color-scheme: dark; }" in css and '[data-theme="light"] .autodry .sel' in css


# --- the AMS tab (IA §6, plan step 11): the strip, the zones, the journal; «Автоматика → Блоки» ----

def _ad(**kw):
    ad = {"now": NOW, "config": dict(CFG, enabled=True, target_rh=5, dry_run=False),
          "engine": {"started": True, "started_at": NOW - H, "last_tick": NOW - 60, "error": None, "clock_wait": False},
          "units": [unit({"kind": "dry", "series": 2, "origin": "auto"}, dry={"active": True}),
                    unit({"kind": "foreign"}, key="b2__0", dry={"active": True}),
                    unit({"kind": "wait"}), unit({"kind": "starting"}), unit({"kind": "offline"}, dry=None)]}
    ad.update(kw)
    return ad


def _text(html):
    return re.sub(r"<[^>]+>", "", html)


def test_strip_says_what_dries_and_the_goal():
    html = run("autodryStripHtml(%s)" % json.dumps(_ad()))
    assert _text(html) == "Автосушка: вкл · сушит 3 из 5 (1 чужая) · цель ≤\u00a05\u00a0% · пробный режим выкл"
    assert '<b class="on">вкл</b>' in html and 'class="ams-strip-tail"' in html
    ad = _ad()
    ad["config"].update(enabled=False, dry_run=True)
    ad["units"] = ad["units"][2:3]
    html = run("autodryStripHtml(%s)" % json.dumps(ad))
    assert _text(html) == "Автосушка: выкл · сушит 0 из 1 · цель ≤\u00a05\u00a0% · пробный режим вкл"
    assert 'class="ams-strip-tail keep"' in html and '<b class="warn">вкл</b>' in html   # a phone keeps it
    ad = _ad()
    ad["engine"].update(last_tick=NOW - H, clock_wait=True)
    assert _text(run("autodryStripHtml(%s)" % json.dumps(ad))).endswith(" · ждёт синхронизации часов · не отвечает с 11:00")
    ad["engine"].update(last_tick=None, started_at=NOW - 600, clock_wait=False)
    assert _text(run("autodryStripHtml(%s)" % json.dumps(ad))).endswith(" · не отвечает")
    assert run("autodryStripHtml(null)") == "Автосушка недоступна"
    # the banner's engine check and the strip's are one
    assert "autodryStalled(ad)" in _js_function("autodryBanner") and "autodryStalled(ad)" in _js_function("autodryStripHtml")


def test_zones_follow_the_goal():
    assert run("amsZones({target_rh: 5})") == {"ok": 5, "warn": 15, "goal": True}
    assert run("[amsZones(null), amsZones({}), amsZones({target_rh: null})]") == [{"ok": 25, "warn": 40, "goal": False}] * 3
    assert run("[5, 6, 15, 16].map(function (v) { return amsHumZone(v, amsZones({target_rh: 5})); })") == \
        ["ok", "warn", "warn", "bad"]
    assert _text(run("amsLegendHtml(amsZones({target_rh: 5}))")) == \
        "сухо ≤\u00a05\u00a0% (цель автосушки)норма ≤\u00a015\u00a0%влажношла сушка"
    fixed = _text(run("amsLegendHtml(amsZones(null))"))
    assert "цель" not in fixed and fixed.startswith("сухо ≤\u00a025\u00a0%норма ≤\u00a040\u00a0%")
    # the chart from 0: room over the data in steps of 5, «влажно» in sight, never over 100
    assert run("[amsHumTop([3, 7, 9], amsZones({target_rh: 5})), amsHumTop([], amsZones({target_rh: 5})),"
               " amsHumTop([31], amsZones({target_rh: 5})), amsHumTop([99], amsZones(null))]") == [20, 20, 35, 100]
    assert run("amsZoneBands(amsZones({target_rh: 5}), 20)") == \
        [[0, 5, "var(--ok)"], [5, 15, "var(--warn)"], [15, 20, "var(--danger)"]]
    # the tab: the card class, the chart and the legend all read the same zones
    assert "amsHumZone(pct, amsZonesNow())" in _js_function("amsHumClass")
    graphs = _js_function("amsDrawGraphs")
    assert "amsChart(humPts, fr, to, 0, top, amsHumColorVar(hcls), { zones: amsZoneBands(z, top), dry: bands, thr: thr })" in graphs
    assert "[[adc.target_rh, 'цель', 'var(--ok)']]" in graphs and "'старт'" not in graphs   # one line: the goal
    assert "amsLegendHtml(amsZonesNow())" in _js_function("amsLegendRender")


def test_release_cell_of_a_held_unit():
    held = line({"kind": "pause", "reason": "manual_stop", "until": NOW + 24 * H, "interrupts": 0})
    html = run("autodryRelCell(%s, true, 'bambu-1__0')" % json.dumps(held))
    assert 'data-ad-release="bambu-1__0">снять паузу</button>' in html and ">Пауза до 13.10 12:00<" in html
    assert 'data-tip="остановлена вручную · правило 24\u00a0ч"' in html
    assert "button" not in run("autodryRelCell(%s, false, 'k')" % json.dumps(held))        # a viewer: the words only
    off = line({"kind": "off", "scope": "unit", "reason": "interrupts"})
    assert ">включить снова</button>" in run("autodryRelCell(%s, true, 'k')" % json.dumps(off))
    calm = line({"kind": "wait", "rest": {"reason": "below", "last": 23}})
    assert run("autodryRelCell(%s, true, 'k')" % json.dumps(calm)) == ""
    assert 'data-ad-release="&lt;k&gt;"' in run("autodryRelCell(%s, true, '<k>')" % json.dumps(held))


def test_blocks_table_rows():
    # «Автоматика → Блоки»: by printer name, auto, the mode with its plan under it, the release; no status
    units = [unit({"kind": "wait", "rest": {"reason": "below", "last": 4}}, key="p10__0", label="P10", ams=0,
                  device_type="P2S", model="AMS 2 Pro"),
             unit({"kind": "pause", "reason": "manual_stop", "until": NOW + 24 * H, "interrupts": 0}, key="p2__0",
                  label="P2", ams=0, device_type="P2S", model="AMS 2 Pro")]
    out = _dom("""
    var AD_MODE_OPTS = %s, amsAutodry = {now: %d, config: %s, limits: %s, units: %s}, adForm = null;
    adRenderRows();
    console.log(JSON.stringify(els.adRows.innerHTML));
    """ % (re.search(r"var AD_MODE_OPTS = (\[.*?\]);", JS, re.S).group(1), NOW, json.dumps(CFG), json.dumps(LIMITS),
           json.dumps(units)), "adRelCell", "adPlanOf", "adRunCell", "adRenderRows", "autodryUnit")
    rows = re.findall(r'<tr id="ad-row-([^"]+)"', out)
    assert rows == ["p2__0", "p10__0"]                                            # P2 before P10
    assert out.count("<td") == 8 and 'data-run="p2__0"' in out and 'data-rel="p2__0"' in out
    assert ">снять паузу</button>" in out and out.count("data-ad-release") == 1
    assert "PETG → 65° · 12\u00a0ч" in out and "c-st" not in out and "c-mat" not in out


def test_journal_shows_five_and_unfolds_up_to_fifty():
    rows = [{"id": i, "ts": NOW - i * 60, "key": "k__0", "label": "P1", "name": "AMS 1", "kind": "start", "text": "t",
             "rh_before": None, "rh_after": None, "t_before": None, "t_after": None, "pending": False, "count": 1}
            for i in range(60)]
    out = _dom("""
    %s
    var amsAutodry = {now: %d, journal: %s}, adJournalAll = false;
    adRenderJournal();
    function n() { return els.adJournal.innerHTML.split('class="jr-row"').length - 1; }
    var a = [n(), els.adJournalMore.hidden, els.adJournalMore.textContent, els.amsJournal.hidden];
    adJournalAll = true; adRenderJournal();
    var b = [n(), els.adJournalMore.textContent];
    amsAutodry.journal = amsAutodry.journal.slice(0, 4); adJournalAll = false; adRenderJournal();
    var c = [n(), els.adJournalMore.hidden];
    amsAutodry = null; adRenderJournal();
    console.log(JSON.stringify([a, b, c, els.amsJournal.hidden]));
    """ % (re.search(r"var AD_JOURNAL_SHORT = [^;]+;", JS).group(0), NOW, json.dumps(rows)), "adRenderJournal")
    assert out == [[5, False, "Все 50 ›", False], [50, "Свернуть"], [4, True], True]
    init = _js_function("autodryInit")
    assert 'on("adJournalMore", function () { adJournalAll = !adJournalAll; adRenderJournal(); });' in init


def test_strip_commands_are_the_admins_only():
    out = _dom("""
    var amsAutodry = %s, res = [];
    amsStripRender(); res.push([els.amsStripActs.hidden, /сушит/.test(els.amsStripState.innerHTML)]);
    admin = false; amsStripRender(); res.push(els.amsStripActs.hidden);
    admin = true; amsAutodry = null; amsStripRender(); res.push([els.amsStripActs.hidden, els.amsStripState.innerHTML]);
    console.log(JSON.stringify(res));
    """ % json.dumps(_ad()), "amsStripRender")
    assert out == [[False, True], True, [True, "Автосушка недоступна"]]
    # «Правила ›» opens «Админ → Автоматика»; every part of the auto-dry renders from one place
    init = _js_function("autodryInit")
    rules = init[init.index('on("amsRulesBtn"'):]
    assert rules.index("if (!isAdmin()) return;") < rules.index('setAdminSub("auto");') < rules.index('setActiveView("admin");')
    render = _js_function("autodryRender")
    assert render.index("amsStripRender();") < render.index("amsLegendRender();") < render.index("adRenderJournal();") < \
        render.index('if (!sec) return;')
    # «Автоматика» loads the snapshot itself and follows the engine while on screen
    assert 'if (adminSub === "auto") { loadAutodry(); loadAutoIgnore(); }' in _js_function("adminLoadSub")   # + plan step 13
    assert 'apiFetch("/api/ams/autodry")' in _js_function("loadAutodry")
    assert "else if (adAutoPaneActive()) loadAutodry();" in _js_function("amsInit")


def test_unsaved_edits_keep_their_mark_off_the_sub_tab():
    out = _dom("""
    var tab = stubEl("tab"), rules = adEl("amsRulesBtn"), adForm = null, aiD = false;
    function aiIsDirty() { return aiD; }
    document.querySelector = function (q) { return q === '#adminSub button[data-asub="auto"]' ? tab : null; };
    adForm = {}; adSetDirtyUI(true);
    var a = [tab.classList.on.dirty, rules.classList.on.dirty, tab["data-dirty"], els.adDirty.classList.on.on, els.adSave.disabled];
    adForm = null; adSetDirtyUI(false);
    var b = [tab.classList.on.dirty, rules.classList.on.dirty, "data-dirty" in tab, els.adSave.disabled];
    aiD = true; autoDirtyMarks();          // plan step 13: the auto-ignore codes unsaved mark it too
    console.log(JSON.stringify([a, b, [tab.classList.on.dirty, tab["data-dirty"]]]));
    """, "adSetDirtyUI", "autoDirtyMarks")
    assert out == [[True, True, "есть несохранённые правки автосушки", True, False], [False, False, False, True],
                   [True, "есть несохранённые коды автопропуска"]]
    # a reload after a deploy waits for them; the dot itself
    assert "if (adForm || aiIsDirty()) return true;" in _js_function("appUserBusy")
    assert ".ams-rules.dirty::after, .adm-seg button.dirty::after {" in CSS


def test_trial_row():
    out = _dom("""
    var amsAutodry = {config: {dry_run: true}}, row = stubEl("row"), res = [];
    adEl("adTrialBtn").closest = function () { return row; };
    adRenderTrial(); res.push([els.adTrialSub.textContent, els.adTrialBtn.textContent, els.adTrialBtn.hidden, row.classList.on.on]);
    amsAutodry.config.dry_run = false; admin = false; adRenderTrial();
    res.push([els.adTrialSub.textContent, els.adTrialBtn.textContent, els.adTrialBtn.hidden, row.classList.on.on]);
    console.log(JSON.stringify(res));
    """, "adRenderTrial")
    assert out == [["Включён: автосушка только пишет в журнал, команды на принтеры не отправляет.", "Выключить…", False, True],
                   ["Выключен: автосушка сама запускает сушку по правилам ниже.", "Включить", True, False]]
    # on: the dialog (real starts begin); off: at once, the safe way round, apart from the form's edits
    assert "adDryRunOffDlg(); else adTrialOn();" in _js_function("autodryInit")
    trial = _js_function("adTrialOn")
    assert "cfg.dry_run = true;" in trial and '"/api/admin/ams/autodry"' in trial and "adForm" not in trial
    assert "adRenderTrial();" in _js_function("autodryRender")
