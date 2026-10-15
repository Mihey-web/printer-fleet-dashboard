"""AMS auto-drying rules (app/services/ams_autodry.py): pure, table-driven.

States come from new_state(), observations from obs(); history rows are
[ts, rh, dry_time, temp]. NOW is 12:00 farm time (MSK).
"""
import copy
import datetime
import re
from pathlib import Path

import pytest

from app.services import ams_autodry as ad
from app.services import ams_drying

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 10, 12, 9, 0, tzinfo=UTC).timestamp()     # 12:00 MSK
H = 3600
KEY = "bambu-1__0"


def slot(t, color="FFF144"):
    return {"type": t, "color": color, "name": None, "remain_pct": 80, "empty": False, "nozzle_temp": [220, 260]}


EMPTY = {"type": None, "color": None, "name": None, "remain_pct": None, "empty": True, "nozzle_temp": None}
UNSET = {"type": None, "color": "FFFFFF", "name": None, "remain_pct": None, "empty": False, "nozzle_temp": None}
PETG2 = [slot("PETG"), slot("PETG"), EMPTY, EMPTY]
NOTHING = [EMPTY, EMPTY, EMPTY, EMPTY]


def dry(slots=PETG2, dtype="n3f", feeding=False, active=False, status=None, remaining=None, temp=None,
        hours=None, filament=None, blockers=()):
    d = {"type": dtype, "status": status or ("drying" if active else "off"), "phase": None, "active": active,
         "remaining_min": remaining, "temp": temp, "hours": hours, "filament": filament,
         "blockers": [{"code": i, "text": t} for i, t in enumerate(blockers)], "feeding": feeding}
    d.update(ams_drying._policy(dtype, slots, feeding))
    return d


def rows(rh=30, temp=26.0, hours=2.5, step=90, now=NOW, dry_time=None):
    """Rest history ending at `now`, ascending."""
    n = int(hours * H // step)
    return [[now - (n - i) * step, rh, dry_time, temp] for i in range(n + 1)]


def obs(**kw):
    slots = kw.pop("slots", PETG2)
    o = {"key": KEY, "present": True, "online": True, "printing": False, "busy": False, "dry_ok": True,
         "cmd_ok": True, "dry": dry(slots), "rh": 30, "temp": 26, "slots": slots, "rows": rows(),
         "clock_jump": False, "tz_offset_min": 180}
    o.update(kw)
    return o


def active_obs(remaining=500, status="drying", temp=55, filament="PETG", **kw):
    return obs(dry=dry(active=True, status=status, remaining=remaining, temp=temp, hours=12, filament=filament),
               **kw)


def cfg(**kw):
    c = copy.deepcopy(ad.DEFAULT_CONFIG)
    c.update(enabled=True, dry_run=False)
    c.update(kw)
    return ad.validate_config(c)


def state(**kw):
    s = ad.new_state()
    s.update(kw)
    return s


def kinds(d):
    return [e["kind"] for e in d.events]


PETG_PLAN = {"mode": "auto", "name": "PETG", "filament": "PETG", "temp": 65, "hours": 12,
             "why": "пресет Bambu для PETG", "warn": False, "limit": None, "empty": False}


def running(**kw):
    """Our auto run 2 h in, confirmed by the AMS."""
    s = state(phase="running", origin="auto", series=1, run_started_at=NOW - 2 * H,
              run_expected_end=NOW + 10 * H, run_temp=65, run_hours=12, run_filament="PETG",
              confirm_by=NOW - 2 * H + 300, seen_active=True, last_seen_at=NOW - 60, last_remaining_min=600,
              remaining_changed_at=NOW - 60, rh_before=27, t_before=26, runs=[NOW - 2 * H])
    s.update(kw)
    return s


def settle(**kw):
    """A finished run whose pause just ran out."""
    s = state(phase="settle", origin="auto", series=1, end_kind="finish", dry_end_at=NOW - H - 60,
              settle_until=NOW - 60, measure_entry=5, rh_before=27, t_before=26, run_temp=65,
              run_started_at=NOW - 13 * H, runs=[NOW - 13 * H])
    s.update(kw)
    return s


# --- config and state ---------------------------------------------------------------

def test_default_config_ships_in_shadow_mode():
    c = ad.DEFAULT_CONFIG
    assert ad.validate_config(copy.deepcopy(c)) == c
    # owner: the engine evaluates and journals but sends nothing until the banner button
    assert c["enabled"] is True and c["dry_run"] is True
    assert (c["start_rh"], c["hold_h"], c["target_rh"], c["pause_h"], c["max_runs"]) == (25, 2, 20, 1, 2)
    assert c["window"] is None and c["while_printing"] is True and c["manual_hold_h"] == 24
    assert c["mode"] == "auto" and c["units"] == {}
    assert "telegram" not in c                      # owner: no Telegram at all


def _with(**kw):
    c = copy.deepcopy(ad.DEFAULT_CONFIG)
    c.update(kw)
    return c


START = "Порог старта: целое 6–60 %"
TARGET = "Цель: целое 5–55 % и ниже порога старта"
WINDOW = "Окно запуска: время ЧЧ:ММ, начало и конец разные"
LIMIT = "Лимит: целое 0–24 прогонов в сутки (0 — без лимита)"


@pytest.mark.parametrize("raw,text", [
    ("x", "Ожидается объект настроек автосушки"),
    (_with(foo=1), "Неизвестное поле настроек автосушки: foo"),
    (_with(rev=3), "Неизвестное поле настроек автосушки: rev"),
    (_with(telegram=True), "Неизвестное поле настроек автосушки: telegram"),
    ({k: v for k, v in ad.DEFAULT_CONFIG.items() if k != "hold_h"}, "Не хватает поля настроек автосушки: hold_h"),
    (_with(enabled="yes"), "Автосушка включена: нужно да/нет"),
    (_with(dry_run=1), "Пробный режим: нужно да/нет"),
    (_with(while_printing=None), "Сушить во время печати: нужно да/нет"),
    (_with(start_rh=5), START), (_with(start_rh=61), START), (_with(start_rh=True), START),
    (_with(start_rh=25.5), START), (_with(start_rh="25"), START),
    (_with(hold_h=0), "Сколько держится: целое 1–24 ч"), (_with(hold_h=25), "Сколько держится: целое 1–24 ч"),
    (_with(target_rh=4), TARGET), (_with(target_rh=56), TARGET),
    (_with(target_rh=25, start_rh=25), TARGET), (_with(target_rh=30, start_rh=25), TARGET),
    (_with(mode="fast"), "Режим: Авто по пластику, PLA, PETG, TPU или Своё"),
    (_with(mode="inherit"), "Режим: Авто по пластику, PLA, PETG, TPU или Своё"),
    (_with(custom_temp=44), "Своя температура: целое 45–85 °C"),
    (_with(custom_temp=86), "Своя температура: целое 45–85 °C"),
    (_with(custom_hours=0), "Своя длительность: целое 1–24 ч"),
    (_with(custom_hours=25), "Своя длительность: целое 1–24 ч"),
    (_with(pause_h=0), "Пауза между прогонами: целое 1–12 ч"),
    (_with(pause_h=13), "Пауза между прогонами: целое 1–12 ч"),
    (_with(max_runs=-1), LIMIT), (_with(max_runs=25), LIMIT),
    (_with(window="22:00"), WINDOW), (_with(window={"from": "22:00"}), WINDOW),
    (_with(window={"from": "24:00", "to": "08:00"}), WINDOW), (_with(window={"from": "8:00", "to": "09:00"}), WINDOW),
    (_with(window={"from": "08:00", "to": "08:00"}), WINDOW),
    (_with(window={"from": "22:00", "to": "08:00", "x": 1}), WINDOW),
    (_with(manual_hold_h=-1), "После ручной остановки: целое 0–168 ч"),
    (_with(manual_hold_h=169), "После ручной остановки: целое 0–168 ч"),
])
def test_validate_config_errors(raw, text):
    with pytest.raises(ValueError) as e:
        ad.validate_config(raw)
    assert str(e.value) == text


@pytest.mark.parametrize("units,text", [
    ([], "Блоки: ожидается объект"),
    ({"bad key": {}}, "Некорректный блок: bad key"),
    ({"p1__x": {}}, "Некорректный блок: p1__x"),
    ({"p1": {}}, "Некорректный блок: p1"),
    ({KEY: "x"}, "Некорректный блок: bambu-1__0"),
    ({KEY: {"enabled": 1}}, "Автосушка блока: нужно да/нет"),
    ({KEY: {"mode": "fast"}}, "Режим блока: Как общие, Авто по пластику, PLA, PETG, TPU или Своё"),
    ({KEY: {"temp": 90}}, "Своя температура блока: целое 45–85 °C"),
    ({KEY: {"hours": 0}}, "Своя длительность блока: целое 1–24 ч"),
    ({KEY: {"foo": 1}}, "Неизвестное поле блока: foo"),
    ({"p%d__0" % i: {} for i in range(65)}, "Слишком много блоков: не больше 64"),
])
def test_validate_units_errors(units, text):
    with pytest.raises(ValueError) as e:
        ad.validate_config(_with(units=units))
    assert str(e.value) == text


def test_validate_units_fills_and_sorts():
    out = ad.validate_config(_with(units={"p-b__1": {"enabled": False}, "p-a__0": {"mode": "custom", "temp": 50}},
                                   window={"from": "22:00", "to": "08:00"}))
    assert list(out["units"]) == ["p-a__0", "p-b__1"]
    assert out["units"]["p-a__0"] == {"enabled": True, "mode": "custom", "temp": 50, "hours": 8}
    assert out["units"]["p-b__1"] == {"enabled": False, "mode": "inherit", "temp": 55, "hours": 8}
    assert out["window"] == {"from": "22:00", "to": "08:00"}


def test_normalize_config():
    # an older stored config gains the new fields, unknown keys are dropped
    out = ad.normalize_config({"enabled": False, "start_rh": 30, "telegram": True, "rev": 4})
    assert out == dict(ad.DEFAULT_CONFIG, enabled=False, start_rh=30)
    # an invalid stored value: the safe default (shadow mode: sends nothing)
    assert ad.normalize_config({"start_rh": 99}) == ad.DEFAULT_CONFIG
    assert ad.normalize_config(None) == ad.DEFAULT_CONFIG
    # never aliases DEFAULT_CONFIG
    out = ad.normalize_config({})
    out["units"]["x__0"] = {}
    assert ad.DEFAULT_CONFIG["units"] == {}


def test_load_state_upgrades():
    st = ad.load_state({"phase": "settle", "series": 1, "gone_field": 3})
    assert st["phase"] == "settle" and st["series"] == 1 and "gone_field" not in st
    assert st["runs"] == [] and st["backoff_until"] is None and st["display"] == {}
    assert ad.load_state(None) == ad.new_state()


def test_unit_keys():
    assert ad.unit_key("bambu-1", 0) == KEY
    assert ad.parse_key(KEY) == ("bambu-1", 0)
    assert ad.parse_key("p-a_b__128") == ("p-a_b", 128)
    assert ad.parse_key("nope") is None and ad.parse_key("p__x") is None and ad.parse_key(None) is None


def test_window_wraps_midnight():
    night = {"from": "22:00", "to": "08:00"}
    assert ad.in_window(night, 23 * 60) and ad.in_window(night, 7 * 60 + 59)
    assert not ad.in_window(night, 8 * 60) and not ad.in_window(night, 12 * 60)
    assert ad.in_window(night, 22 * 60) and ad.in_window(night, 0)
    assert ad.in_window({"from": "09:00", "to": "17:00"}, 12 * 60)
    assert not ad.in_window({"from": "09:00", "to": "17:00"}, 17 * 60)
    assert ad.in_window(None, 0)
    assert ad.local_minute(NOW, 180) == 12 * 60
    assert ad.local_minute(NOW, 0) == 9 * 60


def test_next_window_open():
    assert ad.next_window_open({"from": "22:00", "to": "08:00"}, NOW, 180) == NOW + 10 * H
    assert ad.next_window_open({"from": "09:00", "to": "17:00"}, NOW, 180) == NOW + 21 * H
    assert ad.next_window_open({"from": "12:30", "to": "17:00"}, NOW + 59, 180) == NOW + 30 * 60


def test_module_is_pure():
    src = Path(ad.__file__).read_text(encoding="utf-8")
    for banned in ("os.environ", "time.time", "threading", "settings_service", "import time"):
        assert banned not in src, banned
    assert not re.search(r"(?<![\w.])open\(", src)


# --- plans --------------------------------------------------------------------------

def _dry_for(slots, dtype="n3f", feeding=False):
    return dry(slots, dtype=dtype, feeding=feeding)


@pytest.mark.parametrize("case,mode,ucfg,slots,dtype,feeding,expected", [
    (1, "auto", None, [slot("PETG"), slot("PETG")], "n3f", False,
     dict(name="PETG", filament="PETG", temp=65, hours=12, why="пресет Bambu для PETG", warn=False, limit=None)),
    (2, "auto", None, [slot("PLA"), slot("PETG")], "n3f", False,
     dict(name="PLA+PETG", filament="PLA", temp=45, hours=12, why="смесь — по самому нежному: PLA", limit=None)),
    (3, "auto", None, [slot("PA-CF"), slot("PETG")], "n3f", False,
     dict(name="PA-CF+PETG", filament="PA", temp=65, hours=12, warn=True,
          why="потолок AMS 2 Pro 65° · PA-CF просит 85° — это для AMS HT")),
    (4, "auto", None, [slot("PA-CF"), slot("PETG"), slot("ABS")], "n3f", False,
     dict(name="PA-CF+PETG+ABS", warn=True, why="потолок AMS 2 Pro 65° · PA-CF, ABS просит 80–85° — это для AMS HT")),
    (5, "auto", None, [slot("PETG"), slot("PETG")], "n3f", True,
     dict(temp=55, limit="не выше 55° — идёт печать из этого AMS (PETG)")),
    (6, "petg", None, [slot("PLA")], "n3f", False,
     dict(name="PETG", filament="PETG", temp=45, hours=12, why="пресет Bambu", limit="не выше 45° — в AMS есть PLA")),
    (7, "custom", {"mode": "inherit"}, [slot("PETG")], "n3f", False,
     dict(name="Своё", filament="PETG", temp=65, hours=8, why="задано вручную · потолок AMS 2 Pro 65°", limit=None)),
    (9, "tpu", None, [slot("TPU")], "n3f", False,
     dict(name="TPU", filament="TPU", temp=45, hours=12, why="пресет Bambu; AMS 2 Pro может не досушить TPU",
          limit="не выше 45° — в AMS есть TPU")),
    (10, "auto", None, [slot("ABS")], "n3s", False,
     dict(name="ABS", filament="ABS", temp=80, hours=8, why="пресет Bambu для ABS", warn=False, limit=None)),
    (11, "auto", None, [slot("TPU"), slot("PLA")], "n3s", False,
     dict(name="TPU+PLA", filament="PLA", temp=45, hours=18, why="смесь — по самому нежному: PLA")),
    (12, "auto", None, [UNSET], "n3f", False,
     dict(name="?", filament="PLA", temp=45, hours=12, why="пресет Bambu для PLA", limit=None)),
])
def test_plan_run_cases(case, mode, ucfg, slots, dtype, feeding, expected):
    c = cfg(mode=mode, custom_temp=70, custom_hours=8)
    plan = ad.plan_run(c, ucfg or {"mode": "inherit"}, _dry_for(slots, dtype, feeding), slots)
    for k, v in expected.items():
        assert plan[k] == v, (case, k, plan)
    assert plan["empty"] is False


def test_plan_run_unit_with_unfed_slots():
    # owner, 14.10: spools sit in the AMS without filament fed in - the slots read empty,
    # the spools still need drying: the PLA-safe minimum, for the engine and run-now alike
    d = _dry_for(NOTHING)
    want = {"mode": "auto", "name": "слоты не заправлены", "filament": "PLA", "temp": 45, "hours": 12,
            "why": "пластик неизвестен — 45°, безопасно для любого; свой режим блока — если знаете пластик",
            "warn": False, "limit": None, "empty": True}
    assert ad.plan_run(cfg(), {"mode": "inherit"}, d, NOTHING) == want
    assert ad.plan_run(cfg(), {"mode": "inherit"}, d, NOTHING, manual=True) == want


def test_plan_run_unit_custom_and_limit_text_of_unknown_spool():
    plan = ad.plan_run(cfg(), {"mode": "custom", "temp": 60, "hours": 5}, _dry_for([UNSET, slot("PETG")]),
                       [UNSET, slot("PETG")])
    assert (plan["temp"], plan["hours"], plan["name"]) == (45, 5, "Своё")
    assert plan["limit"] == "не выше 45° — в AMS есть катушка без материала"


def test_materials_of():
    assert ad.materials_of([slot("PA-CF", "111111"), EMPTY, slot("PETG"), slot("PA-CF", "222222"), UNSET]) == [
        {"type": "PA-CF", "material": "PA", "color": "111111", "count": 2, "ht": True},
        {"type": "PETG", "material": "PETG", "color": "FFF144", "count": 1, "ht": False},
        {"type": "?", "material": "PLA", "color": "FFFFFF", "count": 1, "ht": False}]
    assert ad.materials_of(NOTHING) == [] and ad.materials_of(None) == []


def test_plans_for_and_ctx():
    plans = ad.plans_for(_dry_for(NOTHING), NOTHING)
    assert plans["auto"]["temp"] == 45 and plans["auto"]["empty"] is True
    assert plans["pla"]["temp"] == 45 and set(plans) == {"auto", "pla", "petg", "tpu"}
    for max_slots, softest in (([slot("PLA")], "PLA"), ([slot("PETG")], "PETG"), ([slot("TPU"), slot("PETG")], "TPU")):
        d = _dry_for(max_slots)
        ctx = ad.plan_ctx(d, max_slots)
        assert ctx["cap"] == 65 and ctx["model"] == "AMS 2 Pro" and ctx["softest"] == softest
        assert ctx["max_temp"] == d["max_temp"] and ctx["limit_text"] == ad.limit_text(d)
        # the UI reproduces the custom branch from the ctx: same clamp, why and limit
        for t in (45, 50, 65, 70):
            plan = ad.plan_run(cfg(mode="custom", custom_temp=t), {"mode": "inherit"}, d, max_slots)
            temp = max(45, min(ctx["cap"], t))
            limit = ctx["limit_text"] if temp > ctx["max_temp"] else None
            assert plan["temp"] == min(temp, ctx["max_temp"]) and plan["limit"] == limit
            assert plan["filament"] == ctx["softest"]
    assert ad.plan_ctx(_dry_for([slot("PETG")], feeding=True), [slot("PETG")])["limit_text"] == \
        "не выше 55° — идёт печать из этого AMS (PETG)"
    assert ad.limit_text(_dry_for([slot("PETG")])) == "не выше 65° — потолок AMS 2 Pro"


# --- measurements -------------------------------------------------------------------

def test_rest_stats_steady():
    r = ad.rest_stats(rows(rh=27), 25, 2 * H, None, NOW)
    assert r["ok"] and r["reason"] == "ok" and r["p10"] == 27 and r["last"] == 27


def test_rest_stats_spike_is_rejected():
    # H3 11.10: a lid-open spike 16 -> 26, decaying to 18 over 2.3 h
    pts = []
    for i in range(101):
        t = NOW - (100 - i) * 90
        age = NOW - t
        rh = 16 if age > 2.4 * H else round(18 + 8 * max(0.0, (age - 0.1 * H) / (2.3 * H)))
        pts.append([t, rh, None, 26.0])
    r = ad.rest_stats(pts, 25, 2 * H, None, NOW)
    assert not r["ok"] and r["reason"] == "below"


def test_rest_stats_tolerates_noise():
    pts = rows(rh=26)
    for i in (10, 40, 70):
        pts[-i][1] = 24
    assert ad.rest_stats(pts, 25, 2 * H, None, NOW)["ok"]


def test_rest_stats_low_decile_decides_not_the_median():
    # a third of the window below the threshold: the median is above it, the low decile is not
    pts = rows(rh=30)
    for i in range(0, len(pts) - 1, 3):
        pts[i][1] = 22
    r = ad.rest_stats(pts, 25, 2 * H, None, NOW)
    assert not r["ok"] and r["p10"] == 22 and r["reason"] == "holding"


def test_rest_stats_needs_a_sample_every_10_min():
    # 7 samples 19 min apart leave no 20-min hole in 2 h, but 2 h needs 12 samples
    pts = [[NOW - i * 1140, 30, None, 26.0] for i in range(7)][::-1]
    r = ad.rest_stats(pts, 25, 2 * H, None, NOW)
    assert r["reason"] == "no_data" and r["n"] == 7 and not r["ok"]


def test_rest_stats_no_data_drying_after_dry_holding_below():
    holey = [r for r in rows(rh=30) if not NOW - 50 * 60 < r[0] < NOW - 25 * 60]
    assert ad.rest_stats(holey, 25, 2 * H, None, NOW)["reason"] == "no_data"
    assert ad.rest_stats(rows(rh=30)[:5], 25, 2 * H, None, NOW)["reason"] == "no_data"
    assert ad.rest_stats([], 25, 2 * H, None, NOW)["reason"] == "no_data"
    wet = rows(rh=30)
    wet[-20][2] = 400
    assert ad.rest_stats(wet, 25, 2 * H, None, NOW)["reason"] == "drying"
    r = ad.rest_stats(rows(rh=30), 25, 2 * H, NOW - H, NOW)
    assert r["reason"] == "after_dry" and r["ready_at"] == NOW + H and not r["ok"]
    holding = [[t, 30 if NOW - t <= 40 * 60 else 20, None, 26.0] for t, _, _, _ in rows()]
    r = ad.rest_stats(holding, 25, 2 * H, None, NOW)
    assert r["reason"] == "holding" and r["above_since"] == holding[-27][0] and r["ready_at"] == r["above_since"] + 2 * H
    r = ad.rest_stats(rows(rh=21), 25, 2 * H, None, NOW)
    assert r["reason"] == "below" and r["p10"] == 21 and r["above_since"] is None


def test_measure_rh_and_temp():
    pts = rows(rh=20, temp=30.0, hours=1)
    pts[-1][1], pts[-2][1], pts[-1][3] = 23, 22, 31.0
    pts[-3][2] = 200                       # a drying row is not a rest sample
    assert ad.measure_rh(pts, NOW) == 20
    assert ad.measure_temp(pts, NOW) == 30
    assert ad.measure_rh(pts[-3:], NOW) is None and ad.measure_temp([], NOW) is None
    assert ad.measure_rh([[NOW - 60, 21, None, 30], [NOW - 30, 22, None, 30], [NOW, 22, None, 30],
                          [NOW - 120, 21, None, 30]], NOW) == 22     # median 21.5 rounds half up


def test_retry_delay():
    assert [ad.retry_delay_s(n) // 60 for n in range(1, 8)] == [10, 20, 40, 80, 160, 320, 360]


def test_needs_rows():
    c = cfg()
    assert ad.needs_rows(settle(settle_until=NOW + 60), c, KEY, NOW) is None
    assert ad.needs_rows(settle(), c, KEY, NOW) == 1200
    assert ad.needs_rows(state(), c, KEY, NOW) == 2 * H + 1800
    assert ad.needs_rows(state(), cfg(hold_h=4), KEY, NOW) == 4 * H + 1800
    assert ad.needs_rows(state(), cfg(enabled=False), KEY, NOW) is None
    assert ad.needs_rows(state(), cfg(units={KEY: {"enabled": False}}), KEY, NOW) is None
    assert ad.needs_rows(state(hold_until=NOW + 60), c, KEY, NOW) is None
    assert ad.needs_rows(state(disabled_reason="interrupts"), c, KEY, NOW) is None
    assert ad.needs_rows(state(repeat_pending=True, repeat_since=NOW - 60), c, KEY, NOW) is None
    assert ad.needs_rows(state(repeat_pending=True, repeat_since=NOW - 25 * H), c, KEY, NOW) == 2 * H + 1800
    assert ad.needs_rows(state(backoff_until=NOW + 60), c, KEY, NOW) is None
    assert ad.needs_rows(running(), c, KEY, NOW) is None


def test_needs_rows_skips_units_a_cheap_gate_answers():
    # the observation's own gates come before the rest check in decide(): no history read for them
    c = cfg()
    assert ad.needs_rows(state(), c, KEY, NOW, obs()) == 2 * H + 1800
    # unfed slots are no gate (owner, 14.10): such a unit needs its history like any other
    assert ad.needs_rows(state(), c, KEY, NOW, obs(slots=NOTHING, dry=dry(NOTHING))) == 2 * H + 1800
    for name, o in (("offline", obs(online=False)),
                    ("absent", obs(present=False, online=False, dry=None)), ("no_fun2", obs(dry_ok=None)),
                    ("locked", obs(cmd_ok=False)), ("no_sensor", obs(rh=None))):
        assert ad.needs_rows(state(), c, KEY, NOW, o) is None, name
        assert ad.decide(state(), dict(o, rows=[]), c, NOW).status["kind"] in ("empty", "offline", "unsupported",
                                                                                "no_sensor"), name
    assert ad.needs_rows(settle(), c, KEY, NOW, obs(online=False)) == 1200     # a due measure still reads


# --- decide: idle gates -------------------------------------------------------------

def test_decide_master_off():
    d = ad.decide(state(), obs(), cfg(enabled=False), NOW)
    assert d.status == {"kind": "off", "scope": "master"} and d.actions == [] and d.events == []


def test_decide_unit_off():
    d = ad.decide(state(), obs(), cfg(units={KEY: {"enabled": False}}), NOW)
    assert d.status["kind"] == "off" and d.status["scope"] == "unit" and d.actions == []


def test_decide_disabled_unit():
    d = ad.decide(state(disabled_reason="interrupts"), obs(), cfg(), NOW)
    assert d.status == {"kind": "off", "scope": "unit", "reason": "interrupts"} and d.actions == []


def test_decide_starts_when_rest_rh_held():
    d = ad.decide(state(), obs(), cfg(), NOW)
    assert d.status["kind"] == "starting"
    assert d.actions == [{"type": "start", "trigger": "start", "plan": PETG_PLAN, "rh_before": 30, "t_before": 26,
                          "series": 1, "start_rh": 25, "hold_h": 2, "target_rh": 20}]
    assert d.state["rest_temp"] == 26 and d.events == []


@pytest.mark.parametrize("name,st,o,c,status", [
    ("window", {}, {}, {"window": {"from": "22:00", "to": "08:00"}},
     {"kind": "window", "opens_at": NOW + 10 * H, "window": {"from": "22:00", "to": "08:00"}}),
    ("printing", {}, {"printing": True}, {"while_printing": False}, {"kind": "printing"}),
    ("limit", {"runs": [NOW - 3 * H, NOW - 10 * H]}, {}, {},
     {"kind": "limit", "until": NOW - 10 * H + 24 * H, "runs_24h": 2}),
    ("blocked", {}, {"dry": dry(blockers=["AMS занят (калибровка, чтение RFID или загрузка/выгрузка) — подождите"])},
     {}, {"kind": "blocked", "text": "AMS занят (калибровка, чтение RFID или загрузка/выгрузка) — подождите"}),
    ("busy", {}, {"busy": True}, {}, {"kind": "blocked", "text": "идёт смена филамента"}),
    ("stopping", {}, {"dry": dry(status="stopping")}, {}, {"kind": "blocked", "text": "сушка останавливается"}),
    ("refused", {"retry_at": NOW + 600, "last_refusal": "нет связи"}, {}, {},
     {"kind": "refused", "until": NOW + 600, "detail": "нет связи"}),
    ("no_fun2", {}, {"dry_ok": None}, {}, {"kind": "unsupported", "reason": "no_fun2"}),
    ("locked", {}, {"cmd_ok": False}, {}, {"kind": "unsupported", "reason": "locked"}),
    ("no_sensor", {}, {"rh": None}, {}, {"kind": "no_sensor"}),
    ("offline", {}, {"online": False}, {}, {"kind": "offline"}),
    ("absent", {}, {"present": False, "online": False, "dry": None}, {}, {"kind": "offline"}),
    ("pause", {"hold_until": NOW + H, "hold_reason": "manual_stop", "interrupts": 1}, {}, {},
     {"kind": "pause", "reason": "manual_stop", "until": NOW + H, "interrupts": 1}),
    ("backoff", {"backoff_until": NOW + 5 * H}, {}, {}, {"kind": "backoff", "until": NOW + 5 * H}),
])
def test_decide_gates(name, st, o, c, status):
    d = ad.decide(state(**st), obs(**o), cfg(**c), NOW)
    assert d.status == status, name
    assert d.actions == [] and d.events == []


@pytest.mark.parametrize("c,temp", [({}, 45), ({"mode": "petg"}, 65), ({"mode": "pla"}, 45),
                                    ({"units": {KEY: {"mode": "custom", "temp": 60, "hours": 6}}}, 60)])
def test_unit_with_unfed_slots_dries_in_every_mode(c, temp):
    # owner, 14.10: spools not fed in are still there - no empty gate;
    # auto picks the PLA-safe 45°, an explicit mode is the owner's word on the plastic
    d = ad.decide(state(), obs(slots=NOTHING, dry=dry(NOTHING), rh=40, rows=rows(rh=40)), cfg(**c), NOW)
    assert d.status == {"kind": "starting"} and [a["trigger"] for a in d.actions] == ["start"]
    assert d.actions[0]["plan"]["temp"] == temp


def test_decide_wait_reports_the_rest_check():
    d = ad.decide(state(), obs(rows=rows(rh=21)), cfg(), NOW)
    assert d.status["kind"] == "wait" and d.status["rest"]["reason"] == "below" and d.actions == []


def test_decide_dry_run_waits_an_hour_between_would_be_starts():
    d = ad.decide(state(dry_run_at=NOW - 600), obs(), cfg(dry_run=True), NOW)
    assert d.status == {"kind": "dry_run"} and d.actions == []
    d = ad.decide(state(dry_run_at=NOW - H - 1), obs(), cfg(dry_run=True), NOW)
    assert [a["trigger"] for a in d.actions] == ["start"]


def test_decide_default_config_is_shadow_mode():
    # DEFAULT_CONFIG: enabled + dry_run -> the decision is made, the engine turns it into a journal row
    d = ad.decide(state(), obs(), ad.DEFAULT_CONFIG, NOW)
    assert len(d.actions) == 1
    st, ev, status = ad.apply_start_result(d.state, d.actions[0], {"dry_run": True}, "auto", NOW)
    assert status["kind"] == "dry_run" and st["phase"] == "idle" and st["runs"] == []
    assert ev == [{"kind": "dry_run", "series": 1, "temp": 65, "hours": 12, "filament": "PETG",
                   "trigger": "start", "rh_before": 30, "t_before": 26, "dk": "65/12/PETG/1"}]
    d2 = ad.decide(st, obs(), ad.DEFAULT_CONFIG, NOW + 600)
    assert d2.status["kind"] == "dry_run" and d2.actions == []


# --- decide: foreign drying ---------------------------------------------------------

def test_foreign_drying_is_tracked_and_counted_never_touched():
    d = ad.decide(state(), active_obs(remaining=719), cfg(), NOW)
    assert kinds(d) == ["foreign_start"]
    ev = d.events[0]
    assert ev["bind"] == "foreign_entry" and ev["temp"] == 55 and ev["filament"] == "PETG"
    st = d.state
    assert st["phase"] == "foreign" and st["runs"] == [NOW] and st["series"] == 1 and st["foreign_since"] == NOW
    assert d.status["kind"] == "foreign" and d.actions == []
    for i in range(1, 4):
        d = ad.decide(st, active_obs(remaining=719 - i), cfg(), NOW + i * 60)
        st = d.state
        assert d.actions == [] and d.events == [] and d.status["kind"] == "foreign"
        assert d.status["remaining_min"] == 719 - i and d.status["since"] == NOW
    assert st["runs"] == [NOW]


def _foreign(**kw):
    s = state(phase="foreign", series=1, foreign_since=NOW - 12 * H, last_seen_at=NOW - 60,
              last_remaining_min=3, remaining_changed_at=NOW - 60, rh_before=27, t_before=26,
              runs=[NOW - 12 * H], foreign_entry=3)
    s.update(kw)
    return s


def test_foreign_ends_normally():
    d = ad.decide(_foreign(), obs(), cfg(), NOW)
    assert kinds(d) == ["foreign_end"]
    ev = d.events[0]
    assert ev["early"] is False and ev["bind"] == "measure_entry" and ev["dur"] == 12 * H
    assert d.state["phase"] == "settle" and d.state["hold_until"] is None and not d.state["closed"]
    assert d.state["end_kind"] == "foreign_end" and d.state["settle_until"] == NOW + H
    assert d.status == {"kind": "cool", "end_kind": "foreign_end", "series": 1, "until": NOW + H, "dry_end_at": NOW}


def test_foreign_early_stop_is_never_restarted():
    # 08.10 13:47: all units stopped with 375 min left: someone stopped it on purpose
    c = cfg()
    d = ad.decide(_foreign(last_remaining_min=375, foreign_since=NOW - 5.5 * H, runs=[NOW - 5.5 * H]), obs(), c, NOW)
    ev = d.events[0]
    assert ev["kind"] == "foreign_end" and ev["early"] is True and ev["hold_h"] == 24
    st = d.state
    assert st["closed"] and st["hold_until"] == NOW + 24 * H and st["hold_reason"] == "foreign_stop"
    assert st["interrupts"] == 0
    starts, t = [], NOW
    while t < NOW + 30 * H:
        t += 1800
        d = ad.decide(st, obs(rows=rows(rh=30, now=t)), c, t)
        st = d.state
        measures = [e for e in d.events if e["kind"] == "measure"]
        if measures:
            assert measures[0]["outcome"] == "stopped"
        starts += [(t, a) for a in d.actions]
        if t < NOW + 24 * H:
            assert not d.actions
    assert starts and starts[0][0] >= NOW + 24 * H and starts[0][1]["series"] == 1


def test_foreign_end_across_a_gap_is_not_early():
    d = ad.decide(_foreign(last_remaining_min=375, last_seen_at=NOW - 40 * 60), obs(), cfg(), NOW)
    assert d.events[0]["early"] is False and d.state["hold_until"] is None


def test_foreign_end_counts_the_time_since_it_was_last_seen():
    # seen with 12 min left, gone 13 min later (a restart): it ran its course
    d = ad.decide(_foreign(last_remaining_min=12, last_seen_at=NOW - 13 * 60), obs(), cfg(), NOW)
    assert d.events[0]["early"] is False and d.state["hold_until"] is None and not d.state["closed"]
    # gone a minute after it showed 12 min left: 11 min were still to go — stopped
    d = ad.decide(_foreign(last_remaining_min=12, last_seen_at=NOW - 60), obs(), cfg(), NOW)
    assert d.events[0]["early"] is True and d.state["hold_reason"] == "foreign_stop"


def test_foreign_preempts_settle_and_continues_the_series():
    d = ad.decide(settle(measure_entry=7, settle_until=NOW + 600), active_obs(), cfg(), NOW)
    assert kinds(d) == ["measure", "foreign_start"]
    assert d.events[0] == {"kind": "measure", "outcome": "cancelled", "entry": 7}
    assert d.state["series"] == 2 and d.state["t_before"] == 26 and d.state["measure_entry"] is None


def test_foreign_with_repeat_pending_continues_the_series():
    d = ad.decide(state(repeat_pending=True, repeat_since=NOW - 60, series=1, last_rest_rh=23, t_before=25),
                  active_obs(), cfg(), NOW)
    assert d.state["series"] == 2 and d.state["rh_before"] == 23 and d.state["repeat_pending"] is False


# A drying already going when the engine first sees it (first deploy, lost state, an
# outage): dated from the AMS history, not from the tick that noticed it.

def _rest(t_from, t_to, rh=13, temp=36.0):
    return [[t, rh, None, temp] for t in range(int(t_from), int(t_to) + 1, 90)]


def _drying(t_from, t_to, left=719, rh=11, temp=55.0):
    return [[t, rh, max(1, left - (t - int(t_from)) // 60), temp] for t in range(int(t_from), int(t_to) + 1, 90)]


def test_a_drying_found_mid_run_is_back_dated():
    # P2 on 12.10: drying since 07:48, first seen by the engine at 15:34 («с 15:34», no cooling gate)
    t0 = NOW - 8 * H
    hist = _rest(t0 - 3 * H, t0 - 90) + _drying(t0, NOW)
    d = ad.decide(state(), active_obs(remaining=240, rh=11, rows=hist), cfg(), NOW)
    assert kinds(d) == ["foreign_start"]
    st = d.state
    assert st["foreign_since"] == t0 and st["runs"] == [t0] and d.status["since"] == t0
    assert (st["t_before"], st["rh_before"]) == (36, 13)             # the rest right before it, not the drying now
    ev = d.events[0]
    assert ev["ago"] == 8 * H and (ev["rh_before"], ev["t_before"]) == (13, 36)
    assert ad.journal_text(ev) == "Чужая сушка — запущена не автосушкой, не вмешиваемся · 55° · PETG · идёт уже 8 ч"
    # the cooling gate then works: measured only once the AMS is back near 36°
    st = dict(st, last_remaining_min=2)
    d = ad.decide(st, obs(rows=_rest(NOW + 4 * H - 1800, NOW + 4 * H, rh=12, temp=26.0)), cfg(), NOW + 4 * H)
    assert kinds(d) == ["foreign_end"]
    late = NOW + 5 * H + 60
    d = ad.decide(d.state, obs(rows=_rest(late - 1800, late, rh=12, temp=45.0)), cfg(), late)
    assert d.status["kind"] == "cool" and d.status["measuring"] is True and d.status["temp_max"] == 39


def test_a_short_pause_does_not_split_the_drying_found():
    # P3 on 12.10: drying since 23:57, dark for a minute at 04:12, restarted at 04:13 with 719 min
    t0 = NOW - 12 * H
    hist = (_rest(t0 - 2 * H, t0 - 90, rh=18, temp=25.0) + _drying(t0, t0 + 4 * H, left=680, temp=45.0)
            + [[t0 + 4 * H + 60, 17, None, 48.0]] + _drying(t0 + 4 * H + 120, NOW, left=719, temp=45.0))
    d = ad.decide(state(), active_obs(remaining=480, rows=hist), cfg(), NOW)
    assert d.state["foreign_since"] == t0 and d.state["t_before"] == 25 and d.state["rh_before"] == 18
    # a long pause does split it: the drying began after the pause
    t1 = NOW - 3 * H
    hist = _drying(NOW - 20 * H, NOW - 9 * H) + _rest(NOW - 9 * H + 90, t1 - 90, temp=30.0) + _drying(t1, NOW)
    d = ad.decide(state(), active_obs(remaining=540, rows=hist), cfg(), NOW)
    assert d.state["foreign_since"] == t1 and d.state["t_before"] == 30


def test_a_drying_found_without_history_is_dated_by_the_time_left():
    d = ad.decide(state(), active_obs(remaining=240, rows=[]), cfg(), NOW)       # a 12 h program, 4 h left
    assert d.state["foreign_since"] == NOW - 8 * H and d.state["runs"] == [NOW - 8 * H]
    assert d.events[0]["ago"] == 8 * H


def test_a_fresh_foreign_start_is_not_back_dated():
    hist = _rest(NOW - 3 * H, NOW - 90, rh=30, temp=26.0) + [[NOW, 30, 719, 27.0]]
    d = ad.decide(state(), active_obs(remaining=719, rows=hist), cfg(), NOW)
    assert d.state["foreign_since"] == NOW and d.state["runs"] == [NOW] and not d.events[0].get("ago")
    assert d.state["t_before"] == 26                    # no idle reading yet: the last rest row
    # after our run ended: never dated before that end
    st = settle(dry_end_at=NOW - 40 * 60, settle_until=NOW + 20 * 60)
    hist = _drying(NOW - 12 * H, NOW)
    d = ad.decide(st, active_obs(remaining=700, rows=hist), cfg(), NOW)
    assert d.state["foreign_since"] == NOW - 40 * 60 and d.state["series"] == 2 and d.state["t_before"] == 26


def test_needs_rows_for_a_drying_the_engine_did_not_start():
    c = cfg()
    assert ad.needs_rows(state(), c, KEY, NOW, active_obs()) == ad.ONSET_LOOKBACK_S
    assert ad.needs_rows(settle(settle_until=NOW + 600), c, KEY, NOW, active_obs()) == ad.ONSET_LOOKBACK_S
    assert ad.needs_rows(state(), cfg(enabled=False), KEY, NOW, active_obs()) == ad.ONSET_LOOKBACK_S
    assert ad.needs_rows(state(stop_sent_at=NOW - 30), c, KEY, NOW, active_obs()) is None     # our stop
    assert ad.needs_rows(running(), c, KEY, NOW, active_obs()) is None
    assert ad.needs_rows(_foreign(), c, KEY, NOW, active_obs()) is None


# --- decide: our runs ---------------------------------------------------------------

def test_a_restart_over_our_run_becomes_foreign():
    # a second start overwrites the first (ha-bambulab #1448): the timer jumps back up
    d = ad.decide(running(last_remaining_min=300), active_obs(remaining=719, temp=55, filament="PLA"), cfg(), NOW)
    assert kinds(d) == ["foreign_start"]
    ev = d.events[0]
    assert ev["takeover"] is True and ev["bind"] == "foreign_entry" and ev["temp"] == 55
    assert ad.journal_text(ev) == "Чужая сушка перезапустила прогон автосушки — не вмешиваемся · 55° · PLA"
    st = d.state
    assert st["phase"] == "foreign" and st["origin"] is None and st["run_expected_end"] is None
    assert st["runs"] == [NOW - 2 * H, NOW] and st["series"] == 1 and st["foreign_since"] == NOW
    assert d.status["kind"] == "foreign" and not d.actions
    # hours later, past our planned end: still foreign, no "stuck" fault
    t = NOW + 12 * H
    d = ad.decide(st, active_obs(remaining=719 - 12 * 60 + 5), cfg(), t)
    assert d.events == [] and d.status["kind"] == "foreign"
    d = ad.decide(d.state, obs(rows=rows(now=t + 60)), cfg(), t + 60)
    assert kinds(d) == ["foreign_end"] and d.events[0]["early"] is False


def test_small_timer_wobble_is_not_a_restart():
    d = ad.decide(running(last_remaining_min=300), active_obs(remaining=305), cfg(), NOW)
    assert d.events == [] and d.state["phase"] == "running"


# The external program stops a drying and starts its own 1-6 min later (7 times in the
# real history): a restart, like a timer jump — not an interruption, not a second run.

def test_outside_stop_and_restart_over_our_run_is_a_takeover():
    c = cfg()
    d = ad.decide(running(), obs(), c, NOW)                         # dark 2 h into our run
    assert kinds(d) == ["interrupted"] and d.state["interrupts"] == 1
    st = dict(d.state, measure_entry=41)                            # the engine bound the end row
    d = ad.decide(st, active_obs(remaining=719, temp=55, filament="PETG"), c, NOW + 120)
    assert kinds(d) == ["foreign_start"]
    ev = d.events[0]
    assert ev["takeover"] is True and ev["replace"] == 41 and ev["bind"] == "foreign_entry"
    assert "drop" not in ev and "reenabled" not in ev
    assert ad.journal_text(ev) == "Чужая сушка перезапустила прогон автосушки — не вмешиваемся · 55° · PETG"
    st = d.state
    assert st["phase"] == "foreign" and st["origin"] is None and st["measure_entry"] is None
    assert st["interrupts"] == 0 and st["hold_until"] is None and st["hold_reason"] is None
    assert st["runs"] == [NOW - 2 * H, NOW + 120] and st["series"] == 1 and st["foreign_since"] == NOW + 120
    assert (st["rh_before"], st["t_before"]) == (27, 26)
    assert d.status["kind"] == "foreign" and not d.actions
    # it then runs its course like any foreign drying
    d = ad.decide(st, active_obs(remaining=2), c, NOW + 12 * H - 60)
    assert d.events == [] and d.status["kind"] == "foreign"
    d = ad.decide(d.state, obs(rows=rows(now=NOW + 12 * H)), c, NOW + 12 * H)
    assert kinds(d) == ["foreign_end"] and d.events[0]["early"] is False


def test_outside_restart_undoes_the_disable_it_caused():
    d = ad.decide(running(interrupts=1), obs(), cfg(), NOW)
    assert kinds(d) == ["interrupted", "disabled"] and d.events[1]["bind"] == "disabled_entry"
    st = dict(d.state, measure_entry=41, disabled_entry=42)
    d = ad.decide(st, active_obs(remaining=719), cfg(), NOW + 300)
    assert kinds(d) == ["foreign_start"]
    ev = d.events[0]
    assert ev["replace"] == 41 and ev["drop"] == 42 and ev["reenabled"] is True
    assert d.state["interrupts"] == 1 and d.state["disabled_reason"] is None and d.state["disabled_entry"] is None
    assert d.status["kind"] == "foreign"


def test_outside_restart_of_a_dashboard_run_lifts_the_hold_only():
    d = ad.decide(running(origin="manual", interrupts=1), obs(), cfg(), NOW)
    d = ad.decide(dict(d.state, measure_entry=41), active_obs(remaining=719), cfg(), NOW + 60)
    assert d.events[0]["takeover"] is True and d.state["interrupts"] == 1 and d.state["hold_until"] is None


def test_outside_restart_of_a_foreign_drying_continues_it():
    c = cfg()
    d = ad.decide(_foreign(last_remaining_min=375, foreign_since=NOW - 5 * H, runs=[NOW - 5 * H]), obs(), c, NOW)
    assert d.events[0]["early"] is True and d.state["hold_reason"] == "foreign_stop"
    d = ad.decide(dict(d.state, measure_entry=51), active_obs(remaining=719), c, NOW + 120)
    assert kinds(d) == ["foreign_start"]
    ev = d.events[0]
    assert ev["restart"] is True and ev["replace"] == 51 and not ev.get("takeover")
    assert ad.journal_text(ev) == "Чужая сушка перезапущена — не вмешиваемся · 55° · PETG"
    st = d.state
    assert st["phase"] == "foreign" and not st["closed"] and st["end_kind"] is None
    assert st["runs"] == [NOW - 5 * H] and st["series"] == 1 and st["foreign_since"] == NOW - 5 * H
    assert st["hold_until"] is None and st["hold_reason"] is None
    assert d.status["kind"] == "foreign" and d.status["since"] == NOW - 5 * H


def test_a_late_restart_is_a_new_drying():
    d = ad.decide(running(), obs(), cfg(), NOW)
    d = ad.decide(dict(d.state, measure_entry=41), active_obs(remaining=719), cfg(), NOW + 16 * 60)
    assert kinds(d) == ["measure", "foreign_start"] and not d.events[1].get("takeover")
    assert d.state["interrupts"] == 1 and d.state["hold_reason"] == "interrupted" and d.state["series"] == 2


def test_a_quick_start_after_a_normal_end_is_a_new_run():
    d = ad.decide(running(last_remaining_min=3), obs(), cfg(), NOW)
    assert kinds(d) == ["finish"]
    d = ad.decide(dict(d.state, measure_entry=41), active_obs(remaining=719), cfg(), NOW + 120)
    assert kinds(d) == ["measure", "foreign_start"] and d.state["series"] == 2
    assert d.state["runs"] == [NOW - 2 * H, NOW + 120]


def test_a_quick_start_after_a_foreign_drying_ran_out_is_a_new_run():
    # only an early end makes a drying 2 min later the same program restarted
    d = ad.decide(_foreign(), obs(), cfg(), NOW)                  # 3 min left at the last look: ran out
    assert kinds(d) == ["foreign_end"] and d.events[0]["early"] is False
    d = ad.decide(dict(d.state, measure_entry=41), active_obs(remaining=719), cfg(), NOW + 120)
    assert kinds(d) == ["measure", "foreign_start"] and not d.events[1].get("restart")
    assert d.state["runs"] == [NOW - 12 * H, NOW + 120] and d.state["series"] == 2
    assert d.state["foreign_since"] == NOW + 120


def test_measure_above_target_repeats_on_the_same_tick():
    d = ad.decide(settle(), obs(rows=rows(rh=22)), cfg(), NOW)
    assert kinds(d) == ["measure"]
    m = d.events[0]
    assert (m["outcome"], m["entry"], m["rh_after"], m["rh_before"], m["series"]) == ("repeat", 5, 22, 27, 1)
    assert d.actions == [{"type": "start", "trigger": "repeat", "plan": PETG_PLAN, "rh_before": 22,
                          "t_before": 26, "series": 2, "start_rh": 25, "hold_h": 2, "target_rh": 20}]
    assert d.state["repeat_pending"] is True and d.state["last_rest_rh"] == 22


def test_running_unconfirmed_waits():
    st = running(seen_active=False, confirm_by=NOW + 100, run_started_at=NOW - 200, last_remaining_min=None)
    d = ad.decide(st, obs(), cfg(), NOW)
    assert d.status == {"kind": "starting"} and d.events == [] and d.actions == []


def test_running_never_active_is_a_silent_refusal():
    st = running(seen_active=False, confirm_by=NOW - 1, run_started_at=NOW - 301, last_remaining_min=None,
                 runs=[NOW - 301])
    d = ad.decide(st, obs(), cfg(), NOW)
    assert kinds(d) == ["refused"]
    assert d.events[0]["kind"] == "refused" and d.events[0]["reason"] == "silent"
    assert d.events[0]["dk"] == "silent:" and d.events[0]["delay"] == 600
    assert d.state["phase"] == "idle" and d.state["refusals"] == 1 and d.state["retry_at"] == NOW + 600
    assert d.state["runs"] == []                   # the start never happened: not counted
    assert d.status == {"kind": "refused", "until": NOW + 600, "detail": "сушка не началась"}


def test_silent_refusal_of_a_repeat_keeps_the_repeat():
    st = running(series=2, seen_active=False, confirm_by=NOW - 1, last_remaining_min=None)
    d = ad.decide(st, obs(), cfg(), NOW)
    assert d.state["repeat_pending"] is True and d.state["series"] == 1


@pytest.mark.parametrize("origin,text", [("manual", "Сушка с дашборда не началась"),
                                         ("now", "Ручной запуск не прошёл: сушка не началась")])
def test_a_manual_start_that_never_began_delays_no_auto_start(origin, text):
    # nothing retries a dashboard or «Сушить все сейчас» start: its silent failure says no
    # «повтор» and leaves no backoff behind for the auto trigger
    params = {"temp": 55, "hours": 8, "filament": "PETG", "rh": 30, "temp_now": 26}
    if origin == "manual":
        st, _ = ad.apply_user_start(state(), params, NOW)
    else:
        st, _, _ = ad.apply_start_result(state(), _action("now"), {"success": True, "status": "confirmed"},
                                         "now", NOW)
    d = ad.decide(st, obs(), cfg(), NOW + 400)
    assert kinds(d) == ["refused"] and ad.journal_text(d.events[0]) == text
    st = d.state
    assert (st["phase"], st["origin"], st["series"], st["runs"]) == ("idle", None, 0, [])
    assert (st["refusals"], st["retry_at"], st["last_refusal"]) == (0, None, None)
    assert d.status == {"kind": "pending"} and not d.actions       # the next check decides as for any idle unit
    d = ad.decide(st, obs(rows=rows(now=NOW + 460)), cfg(), NOW + 460)
    assert [a["trigger"] for a in d.actions] == ["start"] and d.actions[0]["series"] == 1


def test_running_normal_end_resets_interrupts():
    d = ad.decide(running(last_remaining_min=3, interrupts=1), obs(), cfg(), NOW)
    assert kinds(d) == ["finish"]
    assert d.state["interrupts"] == 0 and d.state["end_kind"] == "finish" and d.state["phase"] == "settle"
    assert d.events[0]["series"] == 1 and d.events[0]["temp"] == 65 and d.events[0]["bind"] == "measure_entry"


def test_running_end_by_expected_time_is_normal():
    d = ad.decide(running(last_remaining_min=40, run_expected_end=NOW + 600), obs(), cfg(), NOW)
    assert kinds(d) == ["finish"]


def test_interruptions_hold_and_then_disable_the_unit():
    c = cfg()
    d = ad.decide(running(), obs(), c, NOW)
    assert kinds(d) == ["interrupted"]
    ev = d.events[0]
    assert ev["ran"] == 2 * H and ev["planned"] == 12 * H and ev["n"] == 1 and ev["hold_h"] == 24
    assert d.state["hold_until"] == NOW + 24 * H and d.state["hold_reason"] == "interrupted"
    assert d.state["interrupts"] == 1 and d.state["disabled_reason"] is None
    d = ad.decide(running(interrupts=1), obs(), c, NOW)
    assert kinds(d) == ["interrupted", "disabled"]
    assert d.events[1]["n"] == 2 and d.state["disabled_reason"] == "interrupts"
    assert c == cfg()                              # the config is never written


def test_interrupted_dashboard_run_holds_without_counting():
    d = ad.decide(running(origin="manual", interrupts=1), obs(), cfg(), NOW)
    assert kinds(d) == ["interrupted"]
    assert d.state["interrupts"] == 1 and d.state["hold_until"] == NOW + 24 * H


def test_interrupted_run_now_counts_toward_the_disable():
    # «Сушить все сейчас» is our command too: its run going dark counts like an auto run
    d = ad.decide(running(origin="now", interrupts=1), obs(), cfg(), NOW)
    assert kinds(d) == ["interrupted", "disabled"]
    assert d.state["interrupts"] == 2 and d.state["disabled_reason"] == "interrupts"


def test_interruption_without_manual_hold():
    d = ad.decide(running(), obs(), cfg(manual_hold_h=0), NOW)
    assert d.state["hold_until"] is None and d.state["interrupts"] == 1


def test_stop_sent_while_running_closes_the_series():
    st = running(stop_sent_at=NOW - 30, hold_until=NOW + 24 * H - 30, hold_reason="stop_all")
    d = ad.decide(st, obs(), cfg(), NOW)
    assert d.events == []
    assert d.state["closed"] and d.state["end_kind"] == "stop" and d.state["phase"] == "settle"
    assert d.state["hold_until"] == NOW + 24 * H - 30 and d.state["interrupts"] == 0
    assert d.state["runs"] == [NOW - 2 * H]
    d = ad.decide(d.state, obs(rows=rows(rh=30, now=NOW + H + 60)), cfg(), NOW + H + 60)
    assert [e["outcome"] for e in d.events] == ["stopped"] and d.state["series"] == 0 and not d.actions


def test_stop_sent_while_unconfirmed_is_no_refusal():
    st = running(seen_active=False, confirm_by=NOW + 200, stop_sent_at=NOW - 30, last_remaining_min=None)
    d = ad.decide(st, obs(), cfg(), NOW)
    assert d.events == [] and d.state["closed"] and d.state["phase"] == "settle" and d.state["refusals"] == 0


def test_lost_after_a_gap():
    d = ad.decide(running(last_seen_at=NOW - 20 * 60, last_remaining_min=300), obs(), cfg(), NOW)
    assert kinds(d) == ["lost"]
    assert d.state["hold_until"] is None and d.state["interrupts"] == 0 and d.state["end_kind"] == "lost"


def test_a_run_ending_during_a_long_outage_is_lost():
    # seen 10 min into our 12 h run, then the unit was gone for 10 days (other units kept
    # ticking: no clock jump): nobody saw it end, so it is no normal finish of 240 h
    t0 = NOW - 10 * 24 * H
    st = running(series=2, interrupts=1, run_started_at=t0, run_expected_end=t0 + 12 * H, confirm_by=t0 + 300,
                 last_seen_at=t0 + 600, last_remaining_min=709, remaining_changed_at=t0 + 600, runs=[])
    d = ad.decide(st, obs(), cfg(), NOW)
    assert kinds(d) == ["lost"] and d.state["end_kind"] == "lost"
    assert d.state["interrupts"] == 1 and d.state["hold_until"] is None


def test_a_run_seen_near_its_end_before_an_outage_finished():
    t0 = NOW - 3 * 24 * H
    st = running(run_started_at=t0, run_expected_end=t0 + 12 * H, confirm_by=t0 + 300, interrupts=1, runs=[],
                 last_seen_at=t0 + 12 * H - 600, last_remaining_min=12, remaining_changed_at=t0 + 12 * H - 600)
    d = ad.decide(st, obs(), cfg(), NOW)
    assert kinds(d) == ["finish"] and d.state["interrupts"] == 0
    assert d.events[0]["dur"] == 12 * H                       # not the 72 h until we saw it again
    assert ad.journal_text(d.events[0]) == "Прогон 1 закончен · 65° · 12 ч"
    d = ad.decide(_foreign(foreign_since=t0, last_seen_at=t0 + 11 * H, last_remaining_min=60, run_hours=12,
                           runs=[]), obs(), cfg(), NOW)
    assert d.events[0]["kind"] == "foreign_end" and d.events[0]["dur"] == 12 * H and not d.events[0]["early"]


def test_clock_jump_is_lost_not_finish():
    st = running(run_expected_end=NOW - H, last_remaining_min=300)
    d = ad.decide(st, obs(clock_jump=True), cfg(), NOW)
    assert kinds(d) == ["lost"]


def test_measure_at_target_is_done():
    d = ad.decide(settle(), obs(rows=rows(rh=19)), cfg(), NOW)
    assert [e["outcome"] for e in d.events] == ["done"]
    assert d.state["repeat_pending"] is False and d.state["series"] == 0 and d.state["phase"] == "idle"
    assert d.status["kind"] == "wait" and not d.actions


def test_measure_under_the_sensor_floor_is_done():
    d = ad.decide(settle(), obs(rows=rows(rh=9)), cfg(target_rh=10, start_rh=15), NOW)
    assert [e["outcome"] for e in d.events] == ["done"]


def test_measure_exactly_at_the_target_is_done():
    d = ad.decide(settle(), obs(rows=rows(rh=20)), cfg(), NOW)
    assert [e["outcome"] for e in d.events] == ["done"] and not d.actions


def test_rolling_limit_at_measure_ends_the_series():
    d = ad.decide(settle(runs=[NOW - 13 * H, NOW - 20 * H]), obs(rows=rows(rh=30)), cfg(), NOW)
    assert [e["outcome"] for e in d.events] == ["limit"]
    assert d.state["series"] == 0 and not d.state["repeat_pending"] and d.state["backoff_until"] is None
    assert d.status["kind"] == "wait" and not d.actions          # the next series needs a new rest check


def test_series_at_max_runs_above_target_backs_off_48h():
    # owner: a series that ends at max runs without reaching the target -> next series not before 48 h
    c = cfg()
    d = ad.decide(settle(series=2), obs(rows=rows(rh=30)), c, NOW)
    assert kinds(d) == ["measure", "unreachable"]
    assert d.events[0]["outcome"] == "unreachable"
    assert d.events[1] == {"kind": "unreachable", "series": 2, "target": 20, "rh": 30}
    st = d.state
    assert st["backoff_until"] == NOW + 48 * H and st["series"] == 0 and not st["repeat_pending"]
    assert d.status == {"kind": "backoff", "until": NOW + 48 * H} and not d.actions
    for t in range(int(NOW + 2 * H), int(NOW + 48 * H), 2 * H):
        d = ad.decide(st, obs(rows=rows(rh=30, now=t)), c, t)
        st = d.state
        assert not d.actions and d.status["kind"] == "backoff"
    d = ad.decide(st, obs(rows=rows(rh=30, now=NOW + 48 * H)), c, NOW + 48 * H)
    assert [a["trigger"] for a in d.actions] == ["start"] and d.state["backoff_until"] is None


def test_owner_config_5_percent_without_limit_is_valid():
    # owner, 12.10: "humidity not above 5 %, otherwise dry endlessly"
    c = ad.validate_config(_with(start_rh=6, target_rh=5, hold_h=1, max_runs=0, manual_hold_h=1))
    assert (c["start_rh"], c["target_rh"], c["max_runs"]) == (6, 5, 0)


def test_unlimited_series_keeps_repeating_above_the_target_without_backoff():
    c = cfg(target_rh=5, start_rh=6, max_runs=0)
    for series in (2, 6, 20, 50):
        d = ad.decide(settle(series=series, runs=[NOW - k * H for k in range(1, 12)]), obs(rows=rows(rh=7)), c, NOW)
        assert [e["outcome"] for e in d.events if e["kind"] == "measure"] == ["repeat"], series
        assert d.state["backoff_until"] is None and "unreachable" not in kinds(d)
        # the repeat is either pending or already started on this tick
        assert d.state["repeat_pending"] or [a["trigger"] for a in d.actions] == ["repeat"]


def test_unlimited_mode_has_no_daily_limit_on_the_start():
    c = cfg(target_rh=5, start_rh=6, max_runs=0, hold_h=1)
    d = ad.decide(settle(runs=[NOW - k * H for k in range(1, 10)]), obs(rows=rows(rh=12)), c, NOW)
    assert d.status["kind"] != "limit"
    assert [e["outcome"] for e in d.events if e["kind"] == "measure"] == ["repeat"]


@pytest.mark.parametrize("rh,outcome", [(5, "done"), (6, "repeat"), (9, "repeat")])
def test_target_5_percent_is_met_only_at_5(rh, outcome):
    c = cfg(target_rh=5, start_rh=6, max_runs=0)
    d = ad.decide(settle(series=1), obs(rows=rows(rh=rh)), c, NOW)
    assert [e["outcome"] for e in d.events if e["kind"] == "measure"] == [outcome]


def test_backoff_blocks_the_repeat_after_a_foreign_run():
    st = settle(series=1, end_kind="foreign_end", origin=None, backoff_until=NOW + 20 * H)
    d = ad.decide(st, obs(rows=rows(rh=30)), cfg(), NOW)
    assert [e["outcome"] for e in d.events if e["kind"] == "measure"] == ["backoff"]
    assert not d.actions and not d.state["repeat_pending"] and d.status["kind"] == "backoff"


def test_the_rest_after_a_run_is_counted_from_the_end_of_the_pause():
    # the pause after a run reads dry (a warm AMS): the hours at or above the start come after it
    c = cfg()
    end = NOW - H - 60                                            # settle(): the run ended here

    def hist(t):
        return [[x, 15 if x <= end + H else 30, None, 26.0] for x in range(int(end), int(t) + 1, 90)]

    d = ad.decide(settle(), obs(rows=hist(NOW)), c, NOW)
    assert [e["outcome"] for e in d.events] == ["done"]
    st = d.state
    for t in (NOW + 30 * 60, NOW + 90 * 60, end + 3 * H - 60):
        d = ad.decide(st, obs(rows=hist(t)), c, t)
        st = d.state
        assert d.status["kind"] == "wait" and d.status["rest"]["reason"] == "after_dry", t
        assert d.status["rest"]["ready_at"] == end + 3 * H and not d.actions
    d = ad.decide(st, obs(rows=hist(end + 3 * H)), c, end + 3 * H)
    assert [a["trigger"] for a in d.actions] == ["start"]


def test_temperature_gate_waits_for_the_ams_to_cool():
    st = settle(t_before=26)
    d = ad.decide(st, obs(rows=rows(rh=18, temp=38.0)), cfg(), NOW)
    assert d.events == [] and d.status["kind"] == "cool" and d.status["measuring"] is True
    assert (d.status["temp"], d.status["temp_max"], d.status["t_before"]) == (38, 29, 26)
    assert d.status["deadline"] == NOW - 60 + 2 * H
    d = ad.decide(st, obs(rows=rows(rh=18, temp=29.0)), cfg(), NOW)
    assert [e["outcome"] for e in d.events] == ["done"] and d.events[0]["t_after"] == 29
    assert d.events[0]["warm"] is False
    late = NOW - 60 + 2 * H
    d = ad.decide(st, obs(rows=rows(rh=18, temp=35.0, now=late)), cfg(), late)
    m = d.events[0]
    assert m["outcome"] == "done" and m["warm"] is True and m["t_after"] == 35


def test_measure_without_rows_waits_then_uses_the_live_value():
    st = settle()
    d = ad.decide(st, obs(rows=[], rh=19, temp=26), cfg(), NOW)
    assert d.status["kind"] == "cool" and d.status["measuring"] is True and d.events == []
    late = NOW - 60 + 2 * H
    d = ad.decide(st, obs(rows=[], rh=19, temp=26), cfg(), late)
    assert [e["outcome"] for e in d.events] == ["done"] and d.events[0]["rh_after"] == 19


def test_measure_fails_without_any_value():
    late = NOW - 60 + 2 * H
    d = ad.decide(settle(), obs(rows=[], online=False), cfg(), late)
    assert [e["outcome"] for e in d.events] == ["failed"] and d.state["series"] == 0


def test_measure_while_held():
    d = ad.decide(settle(hold_until=NOW + H, hold_reason="manual_stop"), obs(rows=rows(rh=30)), cfg(), NOW)
    assert [e["outcome"] for e in d.events] == ["held"] and not d.actions and not d.state["repeat_pending"]


def test_repeat_expires_after_a_day():
    st = state(repeat_pending=True, repeat_since=NOW - 25 * H, series=1, last_rest_rh=23)
    d = ad.decide(st, obs(), cfg(), NOW)
    assert [a["trigger"] for a in d.actions] == ["start"] and d.actions[0]["series"] == 1


def test_a_stale_repeat_expires_behind_any_gate():
    # a repeat was due, then the unit was offline for 3 days: a foreign drying afterwards
    # starts a new series instead of continuing the old one (series 2 -> the 48 h backoff)
    st = state(repeat_pending=True, repeat_since=NOW, series=1, last_rest_rh=30, t_before=25)
    t = NOW
    for _ in range(3):
        t += 24 * H
        d = ad.decide(st, obs(online=False), cfg(), t)
        st = d.state
    assert d.status["kind"] == "offline" and st["repeat_pending"] is False and st["series"] == 0
    d = ad.decide(st, active_obs(remaining=719), cfg(), t + 60)
    assert kinds(d) == ["foreign_start"] and d.state["series"] == 1 and d.state["rh_before"] == 30


def test_a_repeat_goes_on_when_the_slots_read_empty():
    # filament pulled out of the feeders, spools still in the AMS: the series keeps drying
    st = state(repeat_pending=True, repeat_since=NOW - 600, series=1, last_rest_rh=23, t_before=25)
    d = ad.decide(st, obs(slots=NOTHING, dry=dry(NOTHING)), cfg(), NOW)
    assert [a["trigger"] for a in d.actions] == ["repeat"] and d.actions[0]["plan"]["temp"] == 45


def test_a_stale_repeat_is_no_series_for_a_dashboard_start():
    st = state(repeat_pending=True, repeat_since=NOW - 25 * H, series=1, last_rest_rh=23, t_before=21,
               rest_temp=24)
    st, _ = ad.apply_user_start(st, {"temp": 45, "hours": 1, "filament": "PLA", "rh": 28, "temp_now": 27}, NOW)
    assert (st["series"], st["rh_before"], st["t_before"]) == (1, 28, 24)
    assert ad.repeat_due(state(repeat_pending=True, repeat_since=NOW - 60), NOW)
    assert not ad.repeat_due(state(repeat_pending=True, repeat_since=None), NOW)
    assert not ad.repeat_due(state(repeat_pending=False, repeat_since=NOW), NOW)


def test_repeat_prefix_on_waiting_statuses():
    st = state(repeat_pending=True, repeat_since=NOW - 60, series=1, last_rest_rh=23)
    d = ad.decide(st, obs(), cfg(window={"from": "22:00", "to": "08:00"}), NOW)
    assert d.status["kind"] == "window" and d.status["repeat"] == {"rh": 23, "target": 20}


def test_master_off_still_tracks_the_end_of_a_run():
    c = cfg(enabled=False)
    d = ad.decide(running(), active_obs(remaining=300), c, NOW)
    assert d.status == {"kind": "off", "scope": "master", "drying": True} and not d.actions
    d = ad.decide(running(last_remaining_min=2), obs(), c, NOW)
    assert kinds(d) == ["finish"]
    d = ad.decide(d.state, obs(rows=rows(rh=30, now=NOW + H + 60)), c, NOW + H + 60)
    assert [e["outcome"] for e in d.events] == ["held"] and not d.actions


def test_hold_expires():
    d = ad.decide(state(hold_until=NOW - 1, hold_reason="manual_stop"), obs(), cfg(), NOW)
    assert d.state["hold_until"] is None and d.state["hold_reason"] is None and d.actions


def test_fault_is_reported_once_never_stopped():
    st = running()
    d = ad.decide(st, active_obs(status="overheat", remaining=500), cfg(), NOW)
    assert kinds(d) == ["fault"] and d.events[0]["reason"] == "overheat"
    assert d.status["kind"] == "fault" and d.status["reason"] == "overheat" and not d.actions
    d = ad.decide(d.state, active_obs(status="overheat", remaining=499), cfg(), NOW + 60)
    assert d.events == [] and d.status["kind"] == "fault"


def test_fault_stuck():
    d = ad.decide(running(run_expected_end=NOW - 2 * H - 1), active_obs(remaining=30), cfg(), NOW)
    assert kinds(d) == ["fault"] and d.events[0]["reason"] == "stuck"
    st = _foreign(last_remaining_min=300, remaining_changed_at=NOW - 2 * H - 1)
    d = ad.decide(st, active_obs(remaining=300), cfg(), NOW)
    assert kinds(d) == ["fault"] and d.status["reason"] == "stuck"
    d = ad.decide(st, active_obs(remaining=299), cfg(), NOW)
    assert d.events == [] and d.status["kind"] == "foreign"


def test_blocked_journal_after_an_hour_then_every_six():
    blk = "AMS занят (калибровка, чтение RFID или загрузка/выгрузка) — подождите"
    o = lambda t: obs(dry=dry(blockers=[blk]), rows=rows(now=t))
    st, c = state(), cfg()
    d = ad.decide(st, o(NOW), c, NOW)
    assert d.status == {"kind": "blocked", "text": blk} and d.state["blocked_since"] == NOW and not d.events
    d = ad.decide(d.state, o(NOW + 59 * 60), c, NOW + 59 * 60)
    assert not d.events
    d = ad.decide(d.state, o(NOW + H), c, NOW + H)
    assert d.events == [{"kind": "blocked", "text": blk, "dk": blk}]
    d = ad.decide(d.state, o(NOW + 3 * H), c, NOW + 3 * H)
    assert not d.events
    d = ad.decide(d.state, o(NOW + 7 * H), c, NOW + 7 * H)
    assert kinds(d) == ["blocked"]
    d = ad.decide(d.state, obs(rows=rows(rh=20, now=NOW + 8 * H)), c, NOW + 8 * H)
    assert d.state["blocked_since"] is None


def test_stop_grace_is_not_a_foreign_start():
    st = state(stop_sent_at=NOW - 60, hold_until=NOW + 24 * H, hold_reason="stop_all")
    d = ad.decide(st, active_obs(), cfg(), NOW)
    assert d.events == [] and d.state["phase"] == "idle" and d.status["kind"] == "pause"
    d = ad.decide(state(stop_sent_at=NOW - 60), active_obs(), cfg(), NOW)
    assert d.status == {"kind": "blocked", "text": "сушка останавливается"} and d.events == []
    d = ad.decide(state(stop_sent_at=NOW - 200), active_obs(), cfg(), NOW)
    assert kinds(d) == ["foreign_start"]


def test_our_stop_winding_down_is_not_shown_as_drying():
    # «Остановить все» succeeded but the stored status still says drying for a tick
    st = running(stop_sent_at=NOW - 30, hold_until=NOW + 24 * H - 30, hold_reason="stop_all")
    d = ad.decide(st, active_obs(remaining=500), cfg(), NOW)
    assert d.status == {"kind": "pause", "reason": "stop_all", "until": NOW + 24 * H - 30, "interrupts": 0}
    assert d.events == [] and d.state["phase"] == "running" and d.state["stop_sent_at"] == NOW - 30
    d = ad.decide(_foreign(last_remaining_min=300, stop_sent_at=NOW - 30), active_obs(remaining=299), cfg(), NOW)
    assert d.status == {"kind": "blocked", "text": "сушка останавливается"} and d.events == []
    # still drying after the grace: the stop did not land, the run is shown again
    d = ad.decide(running(stop_sent_at=NOW - 200), active_obs(remaining=500), cfg(), NOW)
    assert d.status["kind"] == "dry"


def test_stop_mark_clears_once_inactive():
    d = ad.decide(state(stop_sent_at=NOW - 200), obs(rows=rows(rh=20)), cfg(), NOW)
    assert d.state["stop_sent_at"] is None


def test_offline_while_tracking_keeps_everything():
    st = running()
    d = ad.decide(st, obs(online=False), cfg(), NOW)
    assert d.status == {"kind": "offline", "tracking": True} and d.events == []
    assert d.state["phase"] == "running" and d.state["last_seen_at"] == st["last_seen_at"]


def test_decide_does_not_mutate_its_inputs():
    st, o, c = running(), active_obs(), cfg()
    snap = copy.deepcopy((st, o, c))
    ad.decide(st, o, c, NOW)
    ad.decide(settle(), obs(), c, NOW)
    assert (st, o, c) == snap


# --- the bounded series (3+ days) ---------------------------------------------------

class PureSim:
    """decide() every 10 min against a unit whose rest RH never drops: every
    start succeeds, the AMS dries for the planned hours and cools at once."""

    def __init__(self, c, rest_rh=30, rest_temp=25.0):
        self.c, self.rh, self.temp = c, rest_rh, rest_temp
        self.st = ad.new_state()
        self.run_until = None
        self.starts, self.events = [], []

    def history(self, t):
        out, step = [], 120
        k = int(3 * H // step)
        for i in range(k + 1):
            ts = t - (k - i) * step
            drying = any(a <= ts < b for a, b in self.runs)
            out.append([ts, self.rh, 400 if drying else None, 55.0 if drying else self.temp])
        return out

    def run(self, days):
        self.runs = []
        t = NOW
        end = NOW + days * 24 * H
        while t < end:
            active = self.run_until is not None and t < self.run_until
            o = obs(rh=self.rh, temp=self.temp, rows=self.history(t),
                    dry=dry(active=active, remaining=int((self.run_until - t) // 60) if active else None))
            d = ad.decide(self.st, o, self.c, t)
            self.st = d.state
            self.events += [(t, e) for e in d.events]
            for a in d.actions:
                self.st, ev, _ = ad.apply_start_result(self.st, a, {"success": True, "status": "confirmed"},
                                                       "auto", t)
                self.events += [(t, e) for e in ev]
                self.starts.append((t, a["series"]))
                self.run_until = t + a["plan"]["hours"] * H
                self.runs.append((t, self.run_until))
            t += 600
        return self


def test_bounded_series_with_48h_backoff():
    c = cfg()
    sim = PureSim(c).run(days=6)
    starts = sim.starts
    assert starts, "the engine never started"
    assert max(s for _, s in starts) <= c["max_runs"]
    for t0, _ in starts:                           # any 24 h window: at most max_runs starts
        assert sum(1 for t, _ in starts if t0 <= t < t0 + 24 * H) <= c["max_runs"]
    # series 1: runs at 0 h and right after the first one ends (12 h + one 10-min tick: a
    # warm AMS still reads 30 % > 20 %, so no settle wait); the second one ends at 24 h 10 m
    # above the target -> 48 h off -> series 2 at 72 h 20 m
    m = 60
    assert starts == [(NOW, 1), (NOW + 12 * H + 10 * m, 2), (NOW + 72 * H + 20 * m, 1), (NOW + 84 * H + 30 * m, 2)]
    unreachable = [t for t, e in sim.events if e["kind"] == "unreachable"]
    assert unreachable == [NOW + 24 * H + 20 * m, NOW + 96 * H + 40 * m]
    assert all(e["outcome"] in ("repeat", "unreachable") for _, e in sim.events if e["kind"] == "measure")


def test_reaching_the_target_ends_the_series_without_backoff():
    c = cfg()
    sim = PureSim(c, rest_rh=30)
    # the second run dries the unit: the measure after it reads 18 %
    orig = sim.history

    def history(t):
        out = orig(t)
        if len(sim.runs) >= 2 and t >= sim.runs[1][1]:
            for r in out:
                if r[0] >= sim.runs[1][1]:
                    r[1] = 18
        return out

    sim.history = history
    sim.run(days=2)
    outcomes = [e["outcome"] for _, e in sim.events if e["kind"] == "measure"]
    assert outcomes[:2] == ["repeat", "done"]
    assert sim.st["backoff_until"] is None and [s for _, s in sim.starts] == [1, 2]


# --- apply_*: start results, dashboard commands, release ----------------------------

def _action(trigger="start", series=1, plan=PETG_PLAN):
    return {"type": "start", "trigger": trigger, "plan": dict(plan), "rh_before": 27, "t_before": 26,
            "series": series, "start_rh": 25, "hold_h": 2, "target_rh": 20}


@pytest.mark.parametrize("status", ["confirmed", "sent"])
def test_apply_start_success(status):
    st, ev, out = ad.apply_start_result(state(retry_at=NOW - 1, refusals=2), _action(),
                                        {"success": True, "status": status, "detail": "x"}, "auto", NOW)
    assert out == {"kind": "starting"}
    assert st["phase"] == "running" and st["origin"] == "auto" and st["runs"] == [NOW]
    assert st["run_expected_end"] == NOW + 12 * H and st["confirm_by"] == NOW + 300
    assert (st["run_temp"], st["run_hours"], st["run_filament"]) == (65, 12, "PETG")
    assert (st["rh_before"], st["t_before"], st["series"]) == (27, 26, 1)
    assert ev == [{"kind": "start", "series": 1, "temp": 65, "hours": 12, "filament": "PETG", "start_rh": 25,
                   "hold_h": 2, "target_rh": 20, "rh": 27, "rh_before": 27, "t_before": 26, "status": status}]


def test_apply_start_repeat_and_run_now():
    st, ev, _ = ad.apply_start_result(settle(measure_entry=9), _action("repeat", 2),
                                      {"success": True, "status": "confirmed"}, "auto", NOW)
    assert [e["kind"] for e in ev] == ["measure", "repeat"] and ev[0]["outcome"] == "cancelled"
    st, ev, _ = ad.apply_start_result(state(hold_until=NOW + H, hold_reason="manual_stop", retry_at=NOW + 60,
                                            refusals=3, backoff_until=NOW + H), _action("now"),
                                      {"success": True, "status": "confirmed"}, "now", NOW)
    assert ev[0]["kind"] == "run_now" and st["runs"] == [] and st["origin"] == "now"
    assert st["hold_until"] is None and st["retry_at"] is None and st["refusals"] == 0
    assert st["backoff_until"] == NOW + H          # a manual run does not end the 48 h pause


def test_apply_start_refusals():
    st, ev, out = ad.apply_start_result(state(), _action(), {"success": False, "status": "rejected",
                                                             "detail": "принтер отклонил: x"}, "auto", NOW)
    assert ev == [{"kind": "refused", "reason": "printer", "detail": "принтер отклонил: x", "delay": 600,
                   "dk": "printer:принтер отклонил: x"}]
    assert st["refusals"] == 1 and st["retry_at"] == NOW + 600 and st["phase"] == "idle"
    assert out == {"kind": "refused", "until": NOW + 600, "detail": "принтер отклонил: x"}
    st, ev, _ = ad.apply_start_result(st, _action(), {"success": False, "status": "refused",
                                                      "detail": "Сушка уже идёт"}, "auto", NOW + 700)
    assert ev[0]["reason"] == "check" and ev[0]["delay"] == 1200 and st["refusals"] == 2
    _, ev, _ = ad.apply_start_result(state(), _action(), {"success": False, "detail": "?"}, "auto", NOW)
    assert ev[0]["reason"] == "check"              # no status: refused (nothing was sent)
    st, ev, out = ad.apply_start_result(state(), _action("now"), {"success": False, "status": "rejected",
                                                                  "detail": "нет"}, "now", NOW)
    assert ev == [{"kind": "refused", "reason": "now", "detail": "нет", "dk": "now:нет"}]
    assert out is None and st["retry_at"] is None and st["refusals"] == 0


def test_apply_user_start():
    st, ev = ad.apply_user_start(state(rest_temp=24), {"temp": 45, "hours": 1, "filament": "PLA", "rh": 28,
                                                       "temp_now": 27}, NOW)
    assert st["phase"] == "running" and st["origin"] == "manual" and st["runs"] == [] and st["series"] == 1
    assert (st["rh_before"], st["t_before"], st["run_expected_end"]) == (28, 24, NOW + H)
    assert ev == [{"kind": "manual", "temp": 45, "hours": 1, "filament": "PLA", "replace": None,
                   "rh_before": 28, "t_before": 24}]
    # the tick saw our dashboard start first: the foreign row is rewritten, the count undone
    fst = _foreign(foreign_since=NOW - 60, runs=[NOW - 5 * H, NOW - 60], foreign_entry=11, series=2)
    st, ev = ad.apply_user_start(fst, {"temp": 45, "hours": 1, "filament": "PLA", "rh": 28, "temp_now": 27}, NOW)
    assert st["runs"] == [NOW - 5 * H] and ev[0]["replace"] == 11 and st["series"] == 2 and st["origin"] == "manual"
    st, ev = ad.apply_user_start(settle(measure_entry=4), {"temp": 45, "hours": 1, "filament": "PLA", "rh": 28,
                                                           "temp_now": 27}, NOW)
    assert [e["kind"] for e in ev] == ["measure", "manual"] and st["series"] == 2


@pytest.mark.parametrize("phase", ["idle", "settle", "running", "foreign"])
def test_apply_stop_in_every_phase(phase):
    base = {"idle": state(series=1, repeat_pending=True), "settle": settle(), "running": running(),
            "foreign": _foreign(last_remaining_min=300)}[phase]
    st, ev = ad.apply_stop(base, NOW, 24 * H, "stop_all")
    assert st["hold_until"] == NOW + 24 * H and st["hold_reason"] == "stop_all" and st["stop_sent_at"] == NOW
    assert not st["repeat_pending"]
    drying = phase in ("running", "foreign")
    assert ev == [{"kind": "manual_stop", "hold_h": 24, "reason": "stop_all", "drying": drying,
                   "bind": "measure_entry" if drying else None, "pending": drying}]
    if phase == "idle":
        assert st["series"] == 0
    if phase == "settle":
        assert st["closed"]
    later = NOW + 4 * H
    d = ad.decide(st, obs(rows=rows(rh=30, now=later)), cfg(), later)
    assert not d.actions                           # a deliberate stop is never answered by a restart


def test_apply_stop_without_hold():
    st, ev = ad.apply_stop(state(), NOW, 0, "manual_stop")
    assert st["hold_until"] is None and ev[0]["hold_h"] == 0


def test_release():
    st, ev = ad.release(state(hold_until=NOW + H, hold_reason="interrupted", retry_at=NOW, refusals=2,
                              interrupts=2, blocked_since=NOW, disabled_reason="interrupts",
                              backoff_until=NOW + H))
    assert ev == [{"kind": "reenabled"}]
    for k in ("hold_until", "hold_reason", "retry_at", "blocked_since", "disabled_reason", "backoff_until"):
        assert st[k] is None, k
    assert st["refusals"] == 0 and st["interrupts"] == 0
    assert ad.release(state(hold_until=NOW + H))[1] == [{"kind": "released"}]
    assert ad.release(state(backoff_until=NOW + H))[1] == [{"kind": "released"}]
    assert ad.release(state())[1] == []


# --- texts --------------------------------------------------------------------------

@pytest.mark.parametrize("ev,text", [
    ({"kind": "start", "series": 1, "temp": 65, "hours": 12, "filament": "PETG", "start_rh": 25, "hold_h": 2},
     "Прогон 1 · 65° · 12 ч (PETG): влажность ≥25% держалась 2 ч"),
    ({"kind": "repeat", "series": 2, "temp": 65, "hours": 12, "filament": "PETG", "rh": 22, "target_rh": 20},
     "Прогон 2 · 65° · 12 ч (PETG): после остывания 22% — выше цели 20%"),
    ({"kind": "run_now", "temp": 45, "hours": 1, "filament": "PLA"}, "Вручную · 45° · 1 ч (PLA)"),
    ({"kind": "manual", "temp": 55, "hours": 8, "filament": "PETG"}, "Запущена с дашборда · 55° · 8 ч (PETG)"),
    ({"kind": "finish", "series": 1, "temp": 65, "dur": 12 * H}, "Прогон 1 закончен · 65° · 12 ч"),
    ({"kind": "finish", "series": 2, "temp": 65, "dur": 11 * H + 58 * 60}, "Прогон 2 закончен · 65° · 11 ч 58 мин"),
    ({"kind": "foreign_start", "temp": None, "filament": None},
     "Чужая сушка — запущена не автосушкой, не вмешиваемся"),
    ({"kind": "foreign_start", "temp": 55, "filament": "PETG"},
     "Чужая сушка — запущена не автосушкой, не вмешиваемся · 55° · PETG"),
    ({"kind": "foreign_end", "dur": 12 * H, "early": False, "hold_h": 24},
     "Чужая сушка закончилась · шла 12 ч · засчитана как прогон"),
    ({"kind": "foreign_end", "dur": 5 * H + 45 * 60, "early": True, "hold_h": 24},
     "Чужая сушка закончилась · шла 5 ч 45 мин · остановлена раньше срока — не трогаем 24 ч"),
    ({"kind": "foreign_end", "dur": 15 * 60, "early": True, "hold_h": 0},
     "Чужая сушка закончилась · шла 15 мин · остановлена раньше срока"),
    ({"kind": "interrupted", "ran": 15 * 60, "planned": 12 * H, "hold_h": 24, "n": 1},
     "Погасла через 15 мин из 12 ч — не по команде автосушки · не трогаем 24 ч"),
    ({"kind": "interrupted", "ran": 15 * 60, "planned": 12 * H, "hold_h": 24, "n": 2},
     "Погасла через 15 мин из 12 ч — не по команде автосушки · не трогаем 24 ч · 2-й раз подряд"),
    ({"kind": "interrupted", "ran": 15 * 60, "planned": 12 * H, "hold_h": 0, "n": 1},
     "Погасла через 15 мин из 12 ч — не по команде автосушки"),
    ({"kind": "disabled", "n": 2},
     "Автосушка блока отключена: 2 срыва подряд — проверьте AMS и нажмите «включить снова»"),
    ({"kind": "lost"}, "Прогон прервался, пока принтер или сервер был без связи"),
    ({"kind": "fault", "reason": "error"}, "Ошибка сушки AMS: ошибка — проверьте блок, сами не останавливаем"),
    ({"kind": "fault", "reason": "overheat"}, "Ошибка сушки AMS: перегрев — проверьте блок, сами не останавливаем"),
    ({"kind": "fault", "reason": "stuck"}, "Сушка идёт дольше плана — проверьте блок, сами не останавливаем"),
    ({"kind": "manual_stop", "hold_h": 24}, "Остановлена вручную · не трогаем 24 ч"),
    ({"kind": "manual_stop", "hold_h": 0}, "Остановлена вручную"),
    ({"kind": "refused", "reason": "printer", "detail": "принтер отклонил: x", "delay": 600},
     "Принтер не принял старт: принтер отклонил: x · повтор через 10 мин"),
    ({"kind": "refused", "reason": "check", "detail": "Сушка уже идёт", "delay": 1200},
     "Старт отложен: Сушка уже идёт · повтор через 20 мин"),
    ({"kind": "refused", "reason": "silent", "delay": 2400},
     "Принтер принял команду, но сушка не началась · повтор через 40 мин"),
    ({"kind": "refused", "reason": "silent", "origin": "manual"}, "Сушка с дашборда не началась"),
    ({"kind": "refused", "reason": "silent", "origin": "now"}, "Ручной запуск не прошёл: сушка не началась"),
    ({"kind": "refused", "reason": "now", "detail": "нет связи"}, "Ручной запуск не прошёл: нет связи"),
    ({"kind": "blocked", "text": "Не хватает питания — подключите блок питания AMS"},
     "Пора сушить, но не хватает питания — подключите блок питания AMS"),
    ({"kind": "blocked", "text": "AMS занят — подождите"}, "Пора сушить, но AMS занят — подождите"),
    ({"kind": "released"}, "Пауза снята вручную"),
    ({"kind": "reenabled"}, "Автосушка блока включена заново"),
    ({"kind": "dry_run", "series": 1, "temp": 65, "hours": 12, "filament": "PETG"},
     "Пробный режим: запустили бы прогон 1 · 65° · 12 ч (PETG)"),
    ({"kind": "unreachable", "series": 2, "target": 20, "rh": 30},
     "Цель не достигается — проверьте осушитель/цель · новая серия не раньше чем через 48 ч"),
])
def test_journal_text(ev, text):
    assert ad.journal_text(ev) == text


@pytest.mark.parametrize("ev,suffix", [
    ({"outcome": "done", "target": 20}, " · цель 20% достигнута"),
    ({"outcome": "repeat", "target": 20, "series": 1}, " · выше цели 20% — будет прогон 2"),
    ({"outcome": "limit", "max_runs": 2}, " · выше цели, но лимит 2 прогона за сутки — ждём нового старта"),
    ({"outcome": "limit", "max_runs": 5}, " · выше цели, но лимит 5 прогонов за сутки — ждём нового старта"),
    ({"outcome": "limit", "max_runs": 1}, " · выше цели, но лимит 1 прогон за сутки — ждём нового старта"),
    ({"outcome": "unreachable", "target": 20, "series": 2}, " · выше цели 20% после 2 прогонов подряд"),
    ({"outcome": "held"}, " · выше цели, но блок на паузе"),
    ({"outcome": "backoff"}, " · выше цели, но после неудачной серии ждём 48 ч"),
    ({"outcome": "stopped"}, " · без повтора"),
    ({"outcome": "failed"}, " · замер не удался"),
    ({"outcome": "cancelled"}, " · замер отменён"),
    ({"outcome": "done", "target": 20, "warm": True, "t_after": 35}, " · цель 20% достигнута (AMS ещё 35°)"),
])
def test_measure_suffix(ev, suffix):
    assert ad.measure_suffix(ev) == suffix


def test_fmt_and_plural():
    assert [ad.fmt_dur(s) for s in (12 * H, 11 * H + 58 * 60, 15 * 60, 30, 0)] == \
        ["12 ч", "11 ч 58 мин", "15 мин", "1 мин", "0 мин"]
    assert [ad.plural(n, "прогон", "прогона", "прогонов") for n in (1, 2, 5, 11, 21, 22)] == \
        ["прогон", "прогона", "прогонов", "прогонов", "прогон", "прогона"]
