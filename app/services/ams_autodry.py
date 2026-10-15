"""AMS auto-drying («Автосушка AMS»): the rules, as pure functions.

No I/O, no clock, no threads, no environment: ams_autodry_engine feeds the
stored telemetry, the AMS history and the time in, then executes the start
actions and journals the events that come out. One decision function owns
both the next step and the status text of a unit, so they never disagree.

The cycle (owner-approved, see the AMS tab explainer):
  1. rest: the unit's RH (not drying, not right after a drying) stays at or
     above start_rh for hold_h;
  2. a run by time (Bambu dries by time as well; RH is meaningless while
     heating) — the engine never stops a run on its own;
  3. settle for pause_h, then wait until the AMS cooled to within
     COOL_MARGIN_C of its temperature before the series (a warm AMS reads low);
  4. measure the rest RH: at or below target_rh — done; above — the next run of
     the series, up to max_runs. A series that used all its runs above the
     target pauses the unit for SERIES_BACKOFF_S (48 h).
Drying the engine did not start («чужая сушка») is tracked, counted as a run
and never touched; stopped early by someone, it holds the unit. Two
consecutive interruptions of our runs switch the unit's automation off until
an admin re-enables it.
"""
import copy
import logging
import math
import re
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

from app.services import ams_drying

logger = logging.getLogger(__name__)

MODES = ("auto", "pla", "petg", "tpu", "custom")
UNIT_MODES = ("inherit",) + MODES
INTERRUPT_LIMIT = 2
CONFIRM_S = 300               # our start must show dry.active within 5 min
END_TOLERANCE_MIN = 10        # last remaining <= 10 min -> finished normally
END_TOLERANCE_S = 900         # or now >= expected end - 15 min
LOST_GAP_S = 900              # unseen > 15 min before the end -> "lost", not an interruption
RESTART_S = 900               # drying again within 15 min of an early end: the same program restarted
BACKDATE_S = 300              # a drying first seen more than 5 min after it began is dated from the history
ONSET_LOOKBACK_S = 25 * 3600  # history read for that: the longest program (24 h) and the rest before it
STOP_GRACE_S = 180            # an active reading this soon after our stop is the stop winding down
MEASURE_WINDOW_S = 900        # median of the last 15 min of rest samples
COOL_MARGIN_C = 3             # measure once the AMS is within 3 °C of its pre-run rest temperature
COOL_EXTRA_S = 7200           # ... but never later than settle_until + 2 h
MIN_SAMPLES = 3
MAX_GAP_S = 1200              # coverage: no hole > 20 min in the hold window
STUCK_S = 7200                # active > planned end + 2 h, or remaining frozen 2 h -> fault "stuck"
DAY_S = 86400
SERIES_BACKOFF_S = 48 * 3600  # a series that ran out of runs above the target: next one not before 48 h
RETRY_BASE_MIN, RETRY_MAX_MIN = 10, 360
# A refusal that says "try again in a few seconds" (the printer just came back
# and has not sent fresh telemetry yet, the AMS is busy a moment): retry soon,
# without growing the backoff (owner, 15.10: P2 waited 10 min, then longer).
TRANSIENT_RETRY_S = 120
TRANSIENT_REFUSAL_MARK = "повторите через несколько секунд"
BLOCKED_NOTE_AFTER_S = 3600   # a due start blocked this long gets a journal row ...
BLOCKED_NOTE_EVERY_S = 6 * 3600   # ... at most this often
DRY_RUN_REPEAT_S = 3600
FOREIGN_CONVERT_S = 120       # a dashboard start the tick saw as foreign within 2 min is converted
# The lowest the AMS 2 Pro ever reported on this farm (the lib drops humidity_raw 0):
# below it a reading counts as "dry enough" whatever the target.
RH_SENSOR_FLOOR = 5
# Bambu's AMS HT-only materials (an AMS 2 Pro dries them at its 65 °C ceiling).
HT_PREFERRED = ("ABS", "ASA", "PC", "PA", "PVA", "PPS")
MODEL_NAMES = {"n3f": "AMS 2 Pro", "n3s": "AMS HT"}
DRY_STATUS_RU = {"error": "ошибка", "overheat": "перегрев"}
STATUS_KINDS = ("pending", "off", "offline", "unsupported", "empty", "no_sensor", "wait", "dry_run",
                "refused", "blocked", "printing", "limit", "window", "starting", "dry", "foreign",
                "fault", "cool", "pause", "backoff")

DEFAULT_CONFIG: Dict[str, Any] = {
    # Owner decision: shipped in shadow mode — the engine evaluates and journals
    # what it would do but sends nothing until an admin turns dry_run off.
    "enabled": True,
    "dry_run": True,
    "start_rh": 25,
    "hold_h": 2,
    "target_rh": 20,
    "mode": "auto",
    "custom_temp": 55,
    "custom_hours": 8,
    "pause_h": 1,
    "max_runs": 2,
    "window": None,
    "while_printing": True,
    "manual_hold_h": 24,
    "units": {},
}
UNIT_DEFAULT = {"enabled": True, "mode": "inherit", "temp": 55, "hours": 8}
NIGHT_WINDOW = {"from": "22:00", "to": "08:00"}
# max_runs 0 = no limit (owner, 12.10: "humidity not above 5 %, otherwise dry endlessly"):
# a series repeats until the rest RH reaches the target, with no daily cap and no 48 h backoff.
RANGES = {"start_rh": (6, 60), "target_rh": (5, 55), "hold_h": (1, 24), "pause_h": (1, 12),
          "max_runs": (0, 24), "manual_hold_h": (0, 168), "custom_temp": (45, 85), "custom_hours": (1, 24)}
MAX_UNITS = 64

_KEY_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}__\d{1,3}$")
_HM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_BOOLS = {"enabled": "Автосушка включена", "dry_run": "Пробный режим", "while_printing": "Сушить во время печати"}
_INT_TEXT = {
    "start_rh": "Порог старта: целое 6–60 %",
    "hold_h": "Сколько держится: целое 1–24 ч",
    "target_rh": "Цель: целое 5–55 % и ниже порога старта",
    "custom_temp": "Своя температура: целое 45–85 °C",
    "custom_hours": "Своя длительность: целое 1–24 ч",
    "pause_h": "Пауза между прогонами: целое 1–12 ч",
    "max_runs": "Лимит: целое 0–24 прогонов в сутки (0 — без лимита)",
    "manual_hold_h": "После ручной остановки: целое 0–168 ч",
}
MSG_NOT_OBJECT = "Ожидается объект настроек автосушки"
MSG_MODE = "Режим: Авто по пластику, PLA, PETG, TPU или Своё"
MSG_UNIT_MODE = "Режим блока: Как общие, Авто по пластику, PLA, PETG, TPU или Своё"
MSG_WINDOW = "Окно запуска: время ЧЧ:ММ, начало и конец разные"
# A unit's own fields (the browser checks them first and focuses the unit's input).
_UNIT_TEXT = {"enabled": "Автосушка блока: нужно да/нет", "mode": MSG_UNIT_MODE,
              "temp": "Своя температура блока: целое 45–85 °C", "hours": "Своя длительность блока: целое 1–24 ч"}

# Engine state fields that change on every tick of a drying or idle unit; a
# change in these alone is persisted at most every 15 min, unless a drying is
# tracked (then the engine writes the state on every check).
VOLATILE = ("last_seen_at", "last_remaining_min", "remaining_changed_at", "rest_temp")


class Decision(NamedTuple):
    actions: list
    state: dict
    events: list
    status: dict


# --- config -------------------------------------------------------------------------

def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _int_in(v: Any, lo: int, hi: int) -> bool:
    return _is_int(v) and lo <= v <= hi


def _valid_window(w: Any) -> bool:
    if w is None:
        return True
    return (isinstance(w, dict) and set(w) == {"from", "to"}
            and all(isinstance(w[k], str) and _HM_RE.match(w[k]) for k in ("from", "to"))
            and w["from"] != w["to"])


def _validate_unit(key: Any, entry: Any) -> Dict[str, Any]:
    if not isinstance(key, str) or not _KEY_RE.match(key) or not isinstance(entry, dict):
        raise ValueError("Некорректный блок: %s" % key)
    out = dict(UNIT_DEFAULT)
    checks = {"enabled": lambda v: isinstance(v, bool), "mode": lambda v: v in UNIT_MODES,
              "temp": lambda v: _int_in(v, *RANGES["custom_temp"]),
              "hours": lambda v: _int_in(v, *RANGES["custom_hours"])}
    for field, v in entry.items():
        if field not in checks:
            raise ValueError("Неизвестное поле блока: %s" % field)
        if not checks[field](v):
            raise ValueError(_UNIT_TEXT[field])
        out[field] = v
    return out


def validate_config(raw: Any) -> Dict[str, Any]:
    """A normalized copy of a full auto-dry config, or ValueError(Russian text)."""
    if not isinstance(raw, dict):
        raise ValueError(MSG_NOT_OBJECT)
    for k in raw:
        if k not in DEFAULT_CONFIG:
            raise ValueError("Неизвестное поле настроек автосушки: %s" % k)
    for k in DEFAULT_CONFIG:
        if k not in raw:
            raise ValueError("Не хватает поля настроек автосушки: %s" % k)
    for k, label in _BOOLS.items():
        if not isinstance(raw[k], bool):
            raise ValueError("%s: нужно да/нет" % label)
    for k in ("start_rh", "hold_h"):
        if not _int_in(raw[k], *RANGES[k]):
            raise ValueError(_INT_TEXT[k])
    if not _int_in(raw["target_rh"], *RANGES["target_rh"]) or raw["target_rh"] >= raw["start_rh"]:
        raise ValueError(_INT_TEXT["target_rh"])
    if raw["mode"] not in MODES:
        raise ValueError(MSG_MODE)
    for k in ("custom_temp", "custom_hours", "pause_h", "max_runs"):
        if not _int_in(raw[k], *RANGES[k]):
            raise ValueError(_INT_TEXT[k])
    if not _valid_window(raw["window"]):
        raise ValueError(MSG_WINDOW)
    if not _int_in(raw["manual_hold_h"], *RANGES["manual_hold_h"]):
        raise ValueError(_INT_TEXT["manual_hold_h"])
    units = raw["units"]
    if not isinstance(units, dict):
        raise ValueError("Блоки: ожидается объект")
    if len(units) > MAX_UNITS:
        raise ValueError("Слишком много блоков: не больше %d" % MAX_UNITS)
    out = {k: copy.deepcopy(raw[k]) for k in DEFAULT_CONFIG}
    out["units"] = {key: _validate_unit(key, units[key]) for key in sorted(units, key=str)}
    return out


def normalize_config(stored: Any) -> Dict[str, Any]:
    """The config to run with: stored known keys over the defaults, validated;
    the (shadow-mode) defaults when the stored value is invalid."""
    base = copy.deepcopy(DEFAULT_CONFIG)
    if isinstance(stored, dict):
        for k in DEFAULT_CONFIG:
            if k in stored:
                base[k] = copy.deepcopy(stored[k])
    try:
        return validate_config(base)
    except ValueError as e:
        logger.error("[autodry] stored config is invalid (%s); using the defaults", e)
        return copy.deepcopy(DEFAULT_CONFIG)


def unit_key(printer_id: str, ams_id: int) -> str:
    return "%s__%d" % (printer_id, ams_id)


def parse_key(key: Any) -> Optional[Tuple[str, int]]:
    if not isinstance(key, str) or "__" not in key:
        return None
    pid, _, ams = key.rpartition("__")
    if not pid or not ams.isdigit():
        return None
    return pid, int(ams)


def unit_config(cfg: Dict[str, Any], key: str) -> Dict[str, Any]:
    return dict(UNIT_DEFAULT, **((cfg.get("units") or {}).get(key) or {}))


def effective_mode(cfg: Dict[str, Any], ucfg: Dict[str, Any]) -> str:
    return cfg["mode"] if ucfg.get("mode", "inherit") == "inherit" else ucfg["mode"]


def automation_on(cfg: Dict[str, Any], ucfg: Dict[str, Any]) -> bool:
    return bool(cfg.get("enabled")) and bool(ucfg.get("enabled", True))


# --- engine state -------------------------------------------------------------------

def new_state() -> Dict[str, Any]:
    return {
        "phase": "idle",              # idle | running (ours) | foreign | settle
        "origin": None,               # running/settle: auto | now (run-now) | manual (dashboard) | None (foreign)
        "series": 0,                  # 1-based position of the current/last run in the series, 0 = none
        "closed": False,              # the current run/settle ended by a stop: no repeat
        "end_kind": None,             # finish | foreign_end | interrupted | lost | stop
        "run_started_at": None, "run_expected_end": None,
        "run_temp": None, "run_hours": None, "run_filament": None,
        "confirm_by": None,           # our start must show dry.active by then
        "seen_active": False,
        "last_seen_at": None,         # VOLATILE
        "last_remaining_min": None,   # VOLATILE
        "remaining_changed_at": None,  # VOLATILE (stuck detector)
        "fault_noted": False,         # one fault journal row per run
        "foreign_since": None, "foreign_entry": None,
        "rh_before": None,            # rest RH before the current/last run
        "last_rest_rh": None,         # last measured rest RH
        "t_before": None,             # AMS °C at rest before the series' first run (cooling gate)
        "rest_temp": None,            # VOLATILE: last AMS °C seen while idle and not drying
        "dry_end_at": None,
        "settle_until": None,
        "measure_entry": None,        # journal row id the measure completes
        "repeat_pending": False, "repeat_since": None,
        "runs": [],                   # start ts of auto and foreign runs within the last 24 h
        "interrupts": 0,              # consecutive interruptions of our runs (origin auto/now)
        "hold_until": None, "hold_reason": None,   # manual_stop | stop_all | interrupted | foreign_stop
        "backoff_until": None,        # a series ended above the target at max runs: next series not before
        "stop_sent_at": None,         # a dashboard / stop-all stop was sent (any phase)
        "refusals": 0, "retry_at": None, "last_refusal": None,
        "blocked_since": None, "blocked_noted_at": None,
        "dry_run_at": None,           # dry_run: last "would start" (status dry_run for 1 h)
        "disabled_reason": None,      # "interrupts": automation off for this unit until released
        "disabled_entry": None,       # journal row id of the last "disabled" (a restart may undo it)
        "display": {},                # {"label", "name", "device_type", "model"}
    }


_STATE_KEYS = frozenset(new_state())
_PHASES = ("idle", "running", "foreign", "settle")
_COUNTS = ("series", "interrupts", "refusals")
_FLAGS = ("closed", "seen_active", "fault_noted", "repeat_pending")
_TEXTS = ("origin", "end_kind", "run_filament", "hold_reason", "last_refusal", "disabled_reason")
_ENTRIES = ("foreign_entry", "measure_entry", "disabled_entry")


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _state_value_ok(k: str, v: Any) -> bool:
    if k == "phase":
        return v in _PHASES
    if k == "display":
        return isinstance(v, dict)
    if k in _COUNTS:
        return _is_int(v) and v >= 0
    if k in _FLAGS:
        return isinstance(v, bool)
    if k in _TEXTS:
        return v is None or isinstance(v, str)
    if k in _ENTRIES:
        return v is None or _is_int(v)
    return v is None or _num(v)                  # timestamps, temperatures, RH, minutes


def load_state(d: Any) -> Dict[str, Any]:
    """A stored state (maybe from an older version) over a fresh one; a value
    of the wrong type (version skew, a manual edit) falls back to the default."""
    st = new_state()
    if isinstance(d, dict):
        st.update({k: copy.deepcopy(v) for k, v in d.items() if k in _STATE_KEYS})
    fresh = new_state()
    for k, v in st.items():
        if k == "runs":
            st[k] = [t for t in v if _num(t)] if isinstance(v, list) else []
        elif not _state_value_ok(k, v):
            st[k] = fresh[k]
    return st


# --- small helpers ------------------------------------------------------------------

def _unique(items) -> list:
    out = []
    for x in items:
        if x not in out:
            out.append(x)
    return out


def _clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))


def _round(v: float) -> int:
    return int(math.floor(v + 0.5))


def local_minute(now: float, tz_offset_min: int) -> int:
    """Minute of the day in farm time."""
    return int(((now + tz_offset_min * 60) % DAY_S) // 60)


def _hm(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def in_window(window: Optional[Dict[str, str]], minute: int) -> bool:
    if not window:
        return True
    a, b = _hm(window["from"]), _hm(window["to"])
    if a < b:
        return a <= minute < b
    return minute >= a or minute < b              # wraps midnight


def next_window_open(window: Optional[Dict[str, str]], now: float, tz_offset_min: int) -> float:
    """Timestamp of the next window start ("from") in farm time."""
    if not window:
        return now
    local = (now + tz_offset_min * 60) % DAY_S
    return now + (_hm(window["from"]) * 60 - local) % DAY_S


def retry_delay_s(refusals: int) -> int:
    n = max(1, refusals)
    return min(RETRY_BASE_MIN * 2 ** (n - 1), RETRY_MAX_MIN) * 60


def plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _runs_gen(n: int) -> str:
    """«после N прогонов»: genitive."""
    return "прогона" if n % 10 == 1 and n % 100 != 11 else "прогонов"


def fmt_dur(sec: float) -> str:
    m = max(0, _round((sec or 0) / 60.0))
    h, mm = divmod(m, 60)
    if h and mm:
        return "%d ч %d мин" % (h, mm)
    if h:
        return "%d ч" % h
    return "%d мин" % mm


def _hours(h: Any) -> str:
    h = float(h or 0)
    return "%d" % h if h == int(h) else ("%.1f" % h).replace(".", ",")


def _lc_first(text: str) -> str:
    """Lower-case a sentence's first letter for use mid-sentence ("Не хватает" ->
    "не хватает"), but keep abbreviations ("AMS занят")."""
    if len(text) > 1 and text[0].isupper() and text[1].islower():
        return text[0].lower() + text[1:]
    return text


# --- materials and plans ------------------------------------------------------------

def materials_of(slots: Any) -> List[Dict[str, Any]]:
    """Occupied slots, unique by shown type in slot order."""
    out: List[Dict[str, Any]] = []
    for s in slots or []:
        if not isinstance(s, dict) or s.get("empty"):
            continue
        shown = s.get("type") or "?"
        hit = next((m for m in out if m["type"] == shown), None)
        if hit is not None:
            hit["count"] += 1
            continue
        material = ams_drying.material_of(shown) or ams_drying.FALLBACK_MATERIAL
        out.append({"type": shown, "material": material, "color": s.get("color"), "count": 1,
                    "ht": material in HT_PREFERRED})
    return out


def _idx(dry_type: str) -> int:
    return 0 if dry_type == "n3f" else 1


def softest_of(dry_type: str, slots: Any) -> str:
    i = _idx(dry_type)
    materials = _unique(m["material"] for m in materials_of(slots))
    if not materials:
        return ams_drying.FALLBACK_MATERIAL
    return min(materials, key=lambda m: ams_drying.MATERIALS[m]["idle"][i])


def limit_text(dry: Dict[str, Any]) -> str:
    mx, by = dry.get("max_temp"), dry.get("max_by")
    shown = "катушка без материала" if by == "?" else by
    if dry.get("feeding"):
        return "не выше %s° — идёт печать из этого AMS" % mx + (" (%s)" % shown if by else "")
    if by:
        return "не выше %s° — в AMS есть %s" % (mx, shown)
    return "не выше %s° — потолок %s" % (mx, MODEL_NAMES.get(dry.get("type"), "AMS"))


def plan_run(cfg: Dict[str, Any], ucfg: Dict[str, Any], dry: Optional[Dict[str, Any]], slots: Any,
             manual: bool = False) -> Optional[Dict[str, Any]]:
    """Temperature / hours / material of the next run, or None (auto mode on a
    unit without spools, unless `manual`; a unit that cannot dry)."""
    if not dry or dry.get("type") not in ams_drying.TYPE_MAX:
        return None
    dtype = dry["type"]
    i = _idx(dtype)
    cap = ams_drying.TYPE_MAX[dtype]
    mode = effective_mode(cfg, ucfg)
    mats = materials_of(slots)
    materials = _unique(m["material"] for m in mats)
    softest = softest_of(dtype, slots)
    warn = False
    table = ams_drying.MATERIALS
    if mode == "custom":
        src = ucfg if ucfg.get("mode") == "custom" else {"temp": cfg["custom_temp"], "hours": cfg["custom_hours"]}
        temp = _clamp(src["temp"], ams_drying.TEMP_MIN, cap)
        hours = _clamp(src["hours"], ams_drying.HOURS_MIN, ams_drying.HOURS_MAX)
        filament, name, why = softest, "Своё", "задано вручную"
        if src["temp"] > cap:
            why += " · потолок %s %d°" % (MODEL_NAMES[dtype], cap)
    elif mode in ("pla", "petg", "tpu"):
        m_name = mode.upper()
        temp, hours = table[m_name]["idle"][i], table[m_name]["hours"][i]
        filament = name = m_name
        why = "пресет Bambu"
        if m_name == "TPU" and i == 0:
            why += "; AMS 2 Pro может не досушить TPU"
    else:
        if not mats:
            # Spools can sit in the AMS without filament fed in: the slots read
            # empty, the spools still need drying (owner, 14.10). The plastic is
            # unknown, so the PLA-safe minimum — it deforms nothing.
            return {"mode": mode, "name": "слоты не заправлены", "filament": ams_drying.FALLBACK_MATERIAL,
                    "temp": ams_drying.TEMP_MIN, "hours": 12,
                    "why": "пластик неизвестен — %d°, безопасно для любого; свой режим блока — если знаете пластик"
                           % ams_drying.TEMP_MIN,
                    "warn": False, "limit": None, "empty": True}
        temp = table[softest]["idle"][i]
        hours = max(table[m]["hours"][i] for m in materials)
        filament = softest
        name = "+".join(m["type"] for m in mats)
        ht = [m["type"] for m in mats if m["material"] in HT_PREFERRED] if i == 0 else []
        if ht:
            ask = sorted({table[m["material"]]["idle"][1] for m in mats if m["material"] in HT_PREFERRED})
            span = "%d" % ask[0] if len(ask) == 1 else "%d–%d" % (ask[0], ask[-1])
            why = "потолок AMS 2 Pro %d° · %s просит %s° — это для AMS HT" % (cap, ", ".join(ht), span)
            warn = True
        elif len(materials) > 1:
            why = "смесь — по самому нежному: " + softest
        else:
            why = "пресет Bambu для " + materials[0]
    limit = None
    max_temp = dry.get("max_temp") or cap
    if temp > max_temp:
        temp, limit = max_temp, limit_text(dry)
    return {"mode": mode, "name": name, "filament": filament, "temp": temp, "hours": hours,
            "why": why, "warn": warn, "limit": limit, "empty": False}


def plans_for(dry: Dict[str, Any], slots: Any) -> Dict[str, Optional[Dict[str, Any]]]:
    """The preset plans of a unit (independent of the config); on a unit whose
    slots read empty, auto is the PLA-safe minimum."""
    return {m: plan_run(dict(DEFAULT_CONFIG, mode=m), {"mode": "inherit"}, dry, slots)
            for m in ("auto", "pla", "petg", "tpu")}


def plan_ctx(dry: Dict[str, Any], slots: Any) -> Dict[str, Any]:
    """What the browser needs to reproduce the custom plan (clamp, why, limit)."""
    return {"cap": ams_drying.TYPE_MAX[dry["type"]], "max_temp": dry.get("max_temp"),
            "softest": softest_of(dry["type"], slots), "model": MODEL_NAMES[dry["type"]],
            "limit_text": limit_text(dry)}


# --- measurements -------------------------------------------------------------------
# rows: [[ts, rh|None, dry_time|None, temp|None], ...] ascending (ams_history).

def _above_since(rows: list, start_rh: int) -> Optional[float]:
    since = None
    for r in reversed(rows):
        if r[1] is None:
            continue
        if r[1] < start_rh:
            break
        since = r[0]
    return since


def rest_stats(rows: list, start_rh: int, hold_s: int, not_before: Optional[float], now: float) -> Dict[str, Any]:
    """Has the rest RH stayed at or above start_rh for hold_s? The low decile
    decides, so sensor noise passes and a decaying lid-open spike does not."""
    w0 = now - hold_s
    win = [r for r in rows if w0 <= r[0] <= now]
    pts = [r for r in win if r[1] is not None]
    vals = [r[1] for r in pts]
    last = vals[-1] if vals else None
    base = {"p10": None, "last": last, "n": len(vals), "above_since": _above_since(rows, start_rh),
            "ready_at": None}
    if not_before and not_before > w0:
        return dict(base, ok=False, reason="after_dry", ready_at=not_before + hold_s)
    if any(r[2] and r[2] > 0 for r in win):
        return dict(base, ok=False, reason="drying")              # a drying the engine did not track
    ts = [r[0] for r in pts]
    gaps = ([ts[0] - w0] + [b - a for a, b in zip(ts, ts[1:])] + [now - ts[-1]]) if ts else [hold_s]
    if len(vals) < max(MIN_SAMPLES, hold_s // 600) or max(gaps) > MAX_GAP_S:
        return dict(base, ok=False, reason="no_data")
    p10 = sorted(vals)[int(0.1 * (len(vals) - 1))]
    if p10 >= start_rh:
        return dict(base, ok=True, reason="ok", p10=p10)
    if last >= start_rh and base["above_since"]:
        return dict(base, ok=False, reason="holding", p10=p10, ready_at=base["above_since"] + hold_s)
    return dict(base, ok=False, reason="below", p10=p10)


def _median_recent(rows: list, now: float, i: int) -> Optional[int]:
    vals = sorted(r[i] for r in rows if r[0] >= now - MEASURE_WINDOW_S and not r[2] and r[i] is not None)
    if len(vals) < MIN_SAMPLES:
        return None
    n = len(vals)
    mid = vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2.0
    return _round(mid)


def _drying_row(r: list) -> bool:
    return bool(r[2]) and r[2] > 0


def dry_onset(rows: list, dry: Dict[str, Any], now: float) -> Dict[str, Any]:
    """When did the drying going on now begin, and what were the AMS's rest RH
    and temperature right before it? From the history: the stretch of drying
    rows that reaches the newest row (a pause up to RESTART_S — an outside stop
    + start — does not split it), the rest values from the rest rows just
    before it, else from its first row. Without such a stretch: from the
    program's length and the time left. since=None: unknown."""
    out: Dict[str, Any] = {"since": None, "t_before": None, "rh_before": None}
    j = len(rows) - 1
    start = None
    while j >= 0 and _drying_row(rows[j]):
        start = j
        j -= 1
        k = j
        while k >= 0 and not _drying_row(rows[k]):
            k -= 1
        if k >= 0 and rows[start][0] - rows[k][0] <= RESTART_S:
            j = k                                    # a short pause: the same drying
    edge = rows[start] if start is not None else None
    for r in reversed(rows[:start] if start is not None else rows):
        if _drying_row(r):
            break                                    # only the rest rows right before it
        if out["t_before"] is None and r[3] is not None:
            out["t_before"] = as_temp(r[3])
        if out["rh_before"] is None and r[1] is not None:
            out["rh_before"] = as_temp(r[1])
        if out["t_before"] is not None and out["rh_before"] is not None:
            break
    if edge is not None:
        out["since"] = edge[0]
        if out["t_before"] is None:
            out["t_before"] = as_temp(edge[3])
        if out["rh_before"] is None:
            out["rh_before"] = as_temp(edge[1])
        return out
    hours, left = dry.get("hours"), dry.get("remaining_min")
    if _is_int(hours) and hours > 0 and _is_int(left) and 0 <= left <= hours * 60:
        out["since"] = now - (hours * 60 - left) * 60
    return out


def measure_rh(rows: list, now: float) -> Optional[int]:
    return _median_recent(rows, now, 1)


def measure_temp(rows: list, now: float) -> Optional[int]:
    return _median_recent(rows, now, 3)


def repeat_due(st: Dict[str, Any], now: float) -> bool:
    """Is a repeat of the series pending, and not older than a day?"""
    return bool(st["repeat_pending"]) and st["repeat_since"] is not None and now - st["repeat_since"] <= DAY_S


def _seen(obs: Dict[str, Any]) -> bool:
    return bool(obs.get("present") and obs.get("online") and obs.get("dry") is not None)


def _obs_gate(obs: Dict[str, Any]) -> Optional[Tuple[str, Dict[str, Any]]]:
    """The first gate of an idle unit its observation alone fails (status kind
    and fields), or None: it comes before the rest check in decide()."""
    if not _seen(obs):
        return "offline", {}
    if obs.get("dry_ok") is not True:
        return "unsupported", {"reason": "no_fun2"}
    if obs.get("cmd_ok") is False:
        return "unsupported", {"reason": "locked"}
    # No "empty" gate: slots without fed filament still hold spools (owner, 14.10).
    if obs.get("rh") is None:
        return "no_sensor", {}
    return None


def needs_rows(state: Dict[str, Any], cfg: Dict[str, Any], key: str, now: float,
               obs: Optional[Dict[str, Any]] = None) -> Optional[int]:
    """History lookback (s) decide() needs for this unit now, or None: the AMS
    history is read only for a drying the engine did not start (since when),
    a rest check or a due measure — not for a unit whose observation fails a
    gate that answers first."""
    st = state
    if obs is not None and _seen(obs) and obs["dry"].get("active"):
        if st["phase"] in ("idle", "settle") and not (st["stop_sent_at"] and now - st["stop_sent_at"] < STOP_GRACE_S):
            return ONSET_LOOKBACK_S
        return None
    if st["phase"] == "settle" and st["settle_until"] is not None and now >= st["settle_until"]:
        return MEASURE_WINDOW_S + 300
    if (st["phase"] == "idle" and automation_on(cfg, unit_config(cfg, key)) and not st["disabled_reason"]
            and not (st["hold_until"] and now < st["hold_until"])
            and not (st["backoff_until"] and now < st["backoff_until"])
            and not repeat_due(st, now) and not (obs is not None and _obs_gate(obs))):
        return cfg["hold_h"] * 3600 + 1800
    return None


# --- decide ---------------------------------------------------------------------------

def _ev(kind: str, **fields) -> Dict[str, Any]:
    return dict(kind=kind, **fields)


def _clear_run(st: Dict[str, Any]) -> None:
    st.update(run_started_at=None, run_expected_end=None, run_temp=None, run_hours=None, run_filament=None,
              confirm_by=None, seen_active=False)


def _ended_normally(st: Dict[str, Any], now: float, gap: bool) -> bool:
    """Did our run end on its own? Its last remaining reading was ~0, or it
    reached its planned end — across an outage (`gap`) only if it was seen
    running until near that end: the time now proves nothing then."""
    rem = st["last_remaining_min"]
    if rem is not None and rem <= END_TOLERANCE_MIN:
        return True
    seen = st["last_seen_at"] if gap else now
    return (st["run_expected_end"] is not None and seen is not None
            and seen >= st["run_expected_end"] - END_TOLERANCE_S)


def _restarted(st: Dict[str, Any], now: float) -> bool:
    """Is a drying back within RESTART_S of an early end (ours interrupted, a
    foreign one stopped early)? That is an outside stop + start of the same
    program (1-6 min apart in the real history), not a new run."""
    return (st["phase"] == "settle" and st["dry_end_at"] is not None and now - st["dry_end_at"] <= RESTART_S
            and (st["end_kind"] == "interrupted" or (st["end_kind"] == "foreign_end" and st["closed"])))


def _undo_early_end(st: Dict[str, Any], dry: Dict[str, Any], now: float) -> Dict[str, Any]:
    """Undo what the early end did and track the restarted drying as foreign:
    over our run it is a takeover (like a timer jump: a foreign run, counted),
    over a foreign one it continues that drying (same start, counted once).
    The returned event rewrites the end row (and drops the "disabled" row
    the interruption added)."""
    ev = _ev("foreign_start", temp=dry.get("temp"), filament=dry.get("filament"), rh_before=st["rh_before"],
             t_before=st["t_before"], bind="foreign_entry", replace=st["measure_entry"])
    if st["end_kind"] == "interrupted":
        ev["takeover"] = True
        if st["origin"] in ("auto", "now"):                  # the end counted toward the auto-disable
            if st["interrupts"] >= INTERRUPT_LIMIT and st["disabled_entry"]:
                ev["drop"] = st["disabled_entry"]
            st["interrupts"] = max(0, st["interrupts"] - 1)
            if st["disabled_reason"] == "interrupts" and st["interrupts"] < INTERRUPT_LIMIT:
                st["disabled_reason"], ev["reenabled"] = None, True
            st["disabled_entry"] = None
        if st["hold_reason"] == "interrupted":
            st["hold_until"] = st["hold_reason"] = None
        st["foreign_since"] = now
        st["runs"].append(now)                               # a foreign run counts toward the limit
    else:
        ev["restart"] = True
        if st["hold_reason"] == "foreign_stop":
            st["hold_until"] = st["hold_reason"] = None
    _clear_run(st)
    st.update(phase="foreign", origin=None, closed=False, end_kind=None, last_seen_at=now,
              last_remaining_min=dry.get("remaining_min"), remaining_changed_at=now, fault_noted=False,
              measure_entry=None, run_temp=dry.get("temp"), run_hours=dry.get("hours"),
              run_filament=dry.get("filament"))
    return ev


def _run_fields(st: Dict[str, Any], dry: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    dry = dry or {}
    foreign = st["phase"] == "foreign"
    return {"series": st["series"], "origin": st["origin"],
            "temp": (dry.get("temp") if foreign else st["run_temp"]) or dry.get("temp"),
            "filament": (dry.get("filament") if foreign else st["run_filament"]) or dry.get("filament"),
            "since": st["foreign_since"] if foreign else st["run_started_at"],
            "remaining_min": dry.get("remaining_min")}


def decide(state: Dict[str, Any], obs: Dict[str, Any], cfg: Dict[str, Any], now: float) -> Decision:
    """The next step of one unit: (start actions, new state, journal events, status)."""
    st = load_state(state)
    actions: List[Dict[str, Any]] = []
    events: List[Dict[str, Any]] = []
    ucfg = unit_config(cfg, obs["key"])
    on = automation_on(cfg, ucfg)
    off_scope = "master" if not cfg.get("enabled") else "unit"

    def out(kind: str, **fields) -> Decision:
        return Decision(actions, st, events, dict(kind=kind, **fields))

    def stopping() -> Decision:               # our stop was sent; the AMS still reports drying for a moment
        if st["hold_until"]:
            return out("pause", reason=st["hold_reason"], until=st["hold_until"], interrupts=st["interrupts"])
        return out("blocked", text="сушка останавливается")

    st["runs"] = [t for t in st["runs"] if t > now - DAY_S]
    if st["hold_until"] is not None and now >= st["hold_until"]:
        st["hold_until"] = st["hold_reason"] = None
    if st["backoff_until"] is not None and now >= st["backoff_until"]:
        st["backoff_until"] = None
    if st["repeat_pending"] and not repeat_due(st, now):         # a day old: the series is over
        st["repeat_pending"], st["series"] = False, 0
    dry = obs.get("dry")
    seen = _seen(obs)
    active = seen and bool(dry.get("active"))
    pause_s = cfg["pause_h"] * 3600
    if st["phase"] == "idle" and seen and not active and obs.get("temp") is not None:
        st["rest_temp"] = obs["temp"]
    if (st["phase"] in ("idle", "settle") and st["stop_sent_at"] and not active
            and now - st["stop_sent_at"] >= STOP_GRACE_S):
        st["stop_sent_at"] = None

    # A — a drying is being tracked
    if st["phase"] in ("running", "foreign"):
        if not seen:
            if on:
                return out("offline", tracking=True)
            return out("off", scope=off_scope, drying=True)
        if active:
            st["last_seen_at"] = now
            if st["stop_sent_at"] and now - st["stop_sent_at"] < STOP_GRACE_S:
                return stopping()
            rem = dry.get("remaining_min")
            if (st["phase"] == "running" and st["seen_active"] and rem is not None
                    and st["last_remaining_min"] is not None and rem > st["last_remaining_min"] + END_TOLERANCE_MIN):
                # The timer jumped back up: a second start (Handy, Studio, a schedule)
                # overwrote ours. From now on it is someone else's drying.
                events.append(_ev("foreign_start", temp=dry.get("temp"), filament=dry.get("filament"), takeover=True,
                                  rh_before=st["rh_before"], t_before=st["t_before"], bind="foreign_entry"))
                st.update(phase="foreign", origin=None, foreign_since=now, run_started_at=None,
                          run_expected_end=None, run_temp=dry.get("temp"), run_hours=dry.get("hours"),
                          run_filament=dry.get("filament"), confirm_by=None, fault_noted=False)
                st["runs"].append(now)
            if rem != st["last_remaining_min"]:
                st["remaining_changed_at"] = now
            st["last_remaining_min"] = rem
            if st["phase"] == "running" and not st["seen_active"]:
                st["seen_active"] = True
                st["refusals"], st["retry_at"] = 0, None
            fault = dry.get("status") if dry.get("status") in DRY_STATUS_RU else None
            if fault is None and ((st["run_expected_end"] and now > st["run_expected_end"] + STUCK_S)
                                  or (st["remaining_changed_at"] and now - st["remaining_changed_at"] > STUCK_S)):
                fault = "stuck"
            if fault:                                            # never auto-stop
                if not st["fault_noted"]:
                    st["fault_noted"] = True
                    events.append(_ev("fault", reason=fault, remaining_min=rem))
                return out("fault", reason=fault, **_run_fields(st, dry))
            if not on:
                return out("off", scope=off_scope, drying=True)
            return out("dry" if st["phase"] == "running" else "foreign", **_run_fields(st, dry))
        # not active
        if st["phase"] == "running" and not st["seen_active"] and not st["stop_sent_at"]:
            if st["confirm_by"] is not None and now < st["confirm_by"]:
                return out("starting")
            if st["origin"] in ("manual", "now"):
                # a dashboard / run-now start: nothing retries it, so no backoff for the auto trigger either
                events.append(_ev("refused", reason="silent", origin=st["origin"], dk="silent:%s" % st["origin"]))
                _clear_run(st)
                st.update(phase="idle", origin=None, series=0)
                return out("pending")                            # the next check decides as for any idle unit
            st["refusals"] += 1
            delay = retry_delay_s(st["refusals"])
            st["retry_at"] = now + delay
            st["last_refusal"] = "сушка не началась"
            events.append(_ev("refused", reason="silent", delay=delay, dk="silent:"))
            if st["run_started_at"] in st["runs"]:
                st["runs"].remove(st["run_started_at"])          # it never ran: not counted
            if st["series"] > 1:                                 # a repeat stays due
                st["repeat_pending"], st["repeat_since"] = True, now
                st["series"] -= 1
            else:
                st["series"] = 0
            _clear_run(st)
            st["phase"], st["origin"] = "idle", None
            return out("refused", until=st["retry_at"], detail="сушка не началась")
        offline_gap = bool(st["last_seen_at"]) and now - st["last_seen_at"] > LOST_GAP_S
        hold_h = cfg["manual_hold_h"]
        ran = now - (st["run_started_at"] or st["foreign_since"] or now)
        if offline_gap and st["run_hours"]:
            ran = min(ran, st["run_hours"] * 3600)               # it ended out of sight: not longer than planned
        common = {"rh_before": st["rh_before"], "t_before": st["t_before"], "pending": True, "bind": "measure_entry"}
        if st["stop_sent_at"]:                                   # our stop (dashboard / stop-all)
            st["closed"], st["end_kind"] = True, "stop"         # the stop row was journaled and bound
        elif st["phase"] == "foreign":
            # stopped early: more than END_TOLERANCE_MIN still to go now, by its last reading and the time
            # since (a restart of the service may have kept us from seeing it run out)
            left = (st["last_remaining_min"] or 0) * 60 - (now - (st["last_seen_at"] or now))
            early = left > END_TOLERANCE_MIN * 60 and not offline_gap and not obs.get("clock_jump")
            events.append(_ev("foreign_end", dur=ran, early=early, hold_h=hold_h, **common))
            st["end_kind"] = "foreign_end"
            if early:                                            # stopped on purpose: never restart it
                st["closed"] = True
                if hold_h:
                    st["hold_until"], st["hold_reason"] = now + hold_h * 3600, "foreign_stop"
        elif obs.get("clock_jump"):
            events.append(_ev("lost", **common))
            st["end_kind"] = "lost"
        elif _ended_normally(st, now, offline_gap):
            events.append(_ev("finish", series=st["series"], temp=st["run_temp"], dur=ran, origin=st["origin"],
                              **common))
            st["interrupts"], st["end_kind"] = 0, "finish"
        elif offline_gap:
            events.append(_ev("lost", **common))
            st["end_kind"] = "lost"
        else:
            counts = st["origin"] in ("auto", "now")
            n = st["interrupts"] + 1 if counts else st["interrupts"]
            events.append(_ev("interrupted", ran=ran, planned=(st["run_hours"] or 0) * 3600, hold_h=hold_h, n=n,
                              origin=st["origin"], **common))
            st["end_kind"] = "interrupted"
            if hold_h:
                st["hold_until"], st["hold_reason"] = now + hold_h * 3600, "interrupted"
            if counts:
                st["interrupts"] = n
                if n >= INTERRUPT_LIMIT:
                    events.append(_ev("disabled", n=n, bind="disabled_entry"))
                    st["disabled_reason"] = "interrupts"
        st["phase"], st["dry_end_at"], st["settle_until"] = "settle", now, now + pause_s
        st["stop_sent_at"], st["seen_active"], st["confirm_by"], st["fault_noted"] = None, False, None, False
        return out("cool", end_kind=st["end_kind"], series=st["series"], until=st["settle_until"], dry_end_at=now)

    # B — a drying we did not start
    if active:
        if st["stop_sent_at"] and now - st["stop_sent_at"] < STOP_GRACE_S:
            return stopping()
        if _restarted(st, now):
            events.append(_undo_early_end(st, dry, now))
            if not on:
                return out("off", scope=off_scope, drying=True)
            return out("foreign", **_run_fields(st, dry))
        cont = st["phase"] == "settle" or st["repeat_pending"]
        if st["phase"] == "settle" and st["measure_entry"]:
            events.append(_ev("measure", outcome="cancelled", entry=st["measure_entry"]))
        # Going for a while already (first look after a deploy, a lost state, an outage):
        # dated from the history, with the rest RH and AMS °C from right before it.
        onset = dry_onset(obs.get("rows") or [], dry, now)
        since = now
        if onset["since"] is not None and now - onset["since"] > BACKDATE_S:
            since = max(onset["since"], st["dry_end_at"] or onset["since"])     # never before our last end
        late = since < now
        t_rest = onset["t_before"] if late and onset["t_before"] is not None else as_temp(st["rest_temp"])
        rh_rest = onset["rh_before"] if late and onset["rh_before"] is not None else obs.get("rh")
        st["series"] = st["series"] + 1 if cont else 1
        st["t_before"] = (st["t_before"] if cont and st["t_before"] is not None
                          else t_rest if t_rest is not None else onset["t_before"])
        st["rh_before"] = st["last_rest_rh"] if cont and st["last_rest_rh"] is not None else rh_rest
        _clear_run(st)
        st.update(phase="foreign", origin=None, closed=False, end_kind=None, foreign_since=since, last_seen_at=now,
                  last_remaining_min=dry.get("remaining_min"), remaining_changed_at=now, fault_noted=False,
                  measure_entry=None, repeat_pending=False, run_temp=dry.get("temp"), run_hours=dry.get("hours"),
                  run_filament=dry.get("filament"))
        st["runs"].append(since)                                 # a foreign run counts toward the limit
        ev = _ev("foreign_start", temp=dry.get("temp"), filament=dry.get("filament"), rh_before=st["rh_before"],
                 t_before=st["t_before"], bind="foreign_entry")
        if late:
            ev["ago"] = now - since
        events.append(ev)
        if not on:
            return out("off", scope=off_scope, drying=True)
        return out("foreign", **_run_fields(st, dry))

    # C — settle, then measure once the AMS has cooled
    if st["phase"] == "settle":
        cool = {"end_kind": st["end_kind"], "series": st["series"], "dry_end_at": st["dry_end_at"]}
        rows = obs.get("rows") or []
        # A warm AMS reads drier than it is: a reading already above the target means
        # the cold one is higher still — measure now and repeat at once, no settle or
        # cooling wait (owner, 15.10: units idled 1–3 h between runs far above 5 %).
        rh_now = measure_rh(rows, now)
        if rh_now is None and seen:
            rh_now = obs.get("rh")
        above = rh_now is not None and rh_now > cfg["target_rh"] and not st["closed"]
        if not above and st["settle_until"] is not None and now < st["settle_until"]:
            return out("cool", until=st["settle_until"], **cool)
        deadline = (st["settle_until"] or now) + COOL_EXTRA_S
        t_now = measure_temp(rows, now)
        if t_now is None and seen and obs.get("temp") is not None:
            t_now = _round(obs["temp"])
        t_max = st["t_before"] + COOL_MARGIN_C if st["t_before"] is not None else None
        warm = t_max is not None and t_now is not None and t_now > t_max
        rh = measure_rh(rows, now)
        if rh is None and above:
            rh = rh_now
        if not above and (warm or rh is None) and now < deadline:
            return out("cool", measuring=True, temp=t_now, temp_max=t_max, t_before=st["t_before"],
                       deadline=deadline, until=deadline, **cool)
        if rh is None and seen:
            rh = obs.get("rh")
        series = st["series"]
        if st["closed"]:
            outcome = "stopped"
        elif rh is None:
            outcome = "failed"
        elif rh <= cfg["target_rh"] or rh < RH_SENSOR_FLOOR:
            outcome = "done"
        elif st["hold_until"] or not on or st["disabled_reason"]:
            outcome = "held"
        elif st["backoff_until"]:
            outcome = "backoff"
        elif cfg["max_runs"] and series >= cfg["max_runs"]:
            outcome = "unreachable"
        elif cfg["max_runs"] and len(st["runs"]) >= cfg["max_runs"]:
            outcome = "limit"
        else:
            outcome = "repeat"
        st["phase"] = "idle"
        if outcome == "repeat":
            st["repeat_pending"], st["repeat_since"] = True, now
        else:
            st["repeat_pending"], st["series"] = False, 0
        if outcome == "unreachable":
            st["backoff_until"] = now + SERIES_BACKOFF_S
        events.append(_ev("measure", entry=st["measure_entry"], rh_before=st["rh_before"], rh_after=rh,
                          t_before=st["t_before"], t_after=t_now, warm=bool(warm and rh is not None),
                          outcome=outcome, series=series, origin=st["origin"], end_kind=st["end_kind"],
                          run_temp=st["run_temp"], target=cfg["target_rh"], max_runs=cfg["max_runs"]))
        if outcome == "unreachable":
            events.append(_ev("unreachable", series=series, target=cfg["target_rh"], rh=rh))
        if rh is not None:
            st["last_rest_rh"] = rh
        st["rh_before"], st["measure_entry"], st["closed"], st["origin"] = rh, None, False, None
        _clear_run(st)
        # fall through: a repeat may start on this same tick

    # D — idle: gates, then the trigger (the first failing gate sets the status)
    prev_blocked = st["blocked_since"]
    st["blocked_since"] = None
    if not cfg.get("enabled"):
        return out("off", scope="master")
    if not ucfg.get("enabled", True):
        return out("off", scope="unit")
    if st["disabled_reason"]:
        return out("off", scope="unit", reason=st["disabled_reason"])
    if st["hold_until"]:
        return out("pause", reason=st["hold_reason"], until=st["hold_until"], interrupts=st["interrupts"])
    if st["backoff_until"]:
        return out("backoff", until=st["backoff_until"])
    gate = _obs_gate(obs)            # offline, unsupported, empty, no_sensor (needs_rows skips them)
    if gate is not None:
        if gate[0] == "empty":
            st["repeat_pending"], st["series"] = False, 0
        return out(gate[0], **gate[1])
    rep = {"repeat": {"rh": st["last_rest_rh"], "target": cfg["target_rh"]}} if st["repeat_pending"] else {}
    if st["repeat_pending"]:
        trigger, rh_before, series = "repeat", st["last_rest_rh"], st["series"] + 1
        t_before = st["t_before"] if st["t_before"] is not None else as_temp(st["rest_temp"])
    else:
        nb = st["dry_end_at"] + pause_s if st["dry_end_at"] else None
        rest = rest_stats(obs.get("rows") or [], cfg["start_rh"], cfg["hold_h"] * 3600, nb, now)
        if not rest["ok"]:
            return out("wait", rest=rest)
        if cfg["max_runs"] and len(st["runs"]) >= cfg["max_runs"]:
            return out("limit", until=min(st["runs"]) + DAY_S, runs_24h=len(st["runs"]))
        trigger, rh_before, series, t_before = "start", rest["p10"], 1, as_temp(st["rest_temp"])
    if cfg.get("dry_run") and st["dry_run_at"] and now - st["dry_run_at"] < DRY_RUN_REPEAT_S:
        return out("dry_run", **rep)
    if st["retry_at"] and now < st["retry_at"]:
        return out("refused", until=st["retry_at"], detail=st["last_refusal"], **rep)
    blockers = dry.get("blockers") or []
    blk = (blockers[0].get("text") if blockers else
           "идёт смена филамента" if obs.get("busy") else
           "сушка останавливается" if dry.get("status") == "stopping" else None)
    if blk:
        st["blocked_since"] = prev_blocked or now
        if now - st["blocked_since"] >= BLOCKED_NOTE_AFTER_S and (
                st["blocked_noted_at"] is None or now - st["blocked_noted_at"] >= BLOCKED_NOTE_EVERY_S):
            st["blocked_noted_at"] = now
            events.append(_ev("blocked", text=blk, dk=blk))
        return out("blocked", text=blk, **rep)
    if obs.get("printing") and not cfg["while_printing"]:
        return out("printing", **rep)
    tz = obs.get("tz_offset_min") or 0
    if not in_window(cfg["window"], local_minute(now, tz)):
        return out("window", opens_at=next_window_open(cfg["window"], now, tz), window=cfg["window"], **rep)
    plan = plan_run(cfg, ucfg, dry, obs.get("slots"))
    if plan is None:
        return out("empty")
    actions.append({"type": "start", "trigger": trigger, "plan": plan, "rh_before": rh_before,
                    "t_before": t_before, "series": series, "start_rh": cfg["start_rh"], "hold_h": cfg["hold_h"],
                    "target_rh": cfg["target_rh"]})
    return out("starting")


def as_temp(t: Any) -> Optional[int]:
    return _round(t) if isinstance(t, (int, float)) and not isinstance(t, bool) else None


# --- results of starts and stops ------------------------------------------------------

def _start_running(st: Dict[str, Any], origin: str, series: int, temp: int, hours: int, filament: str,
                   now: float) -> None:
    st.update(phase="running", origin=origin, series=series, closed=False, end_kind=None,
              run_started_at=now, run_expected_end=now + hours * 3600, run_temp=temp, run_hours=hours,
              run_filament=filament, confirm_by=now + CONFIRM_S, seen_active=False, fault_noted=False,
              last_seen_at=now, last_remaining_min=None, remaining_changed_at=now, repeat_pending=False,
              stop_sent_at=None, measure_entry=None, foreign_since=None, foreign_entry=None)


def apply_start_result(state: Dict[str, Any], action: Dict[str, Any], result: Dict[str, Any], origin: str,
                       now: float) -> Tuple[Dict[str, Any], List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Apply printer_commands.send()'s result ({"dry_run": True} in shadow
    mode) of a start: origin "auto" (the engine) or "now" (Сушить все сейчас)."""
    st = load_state(state)
    events: List[Dict[str, Any]] = []
    plan = action["plan"]
    if result.get("dry_run"):
        st["dry_run_at"] = now
        events.append(_ev("dry_run", series=action["series"], temp=plan["temp"], hours=plan["hours"],
                          filament=plan["filament"], trigger=action["trigger"], rh_before=action.get("rh_before"),
                          t_before=action.get("t_before"),
                          dk="%s/%s/%s/%s" % (plan["temp"], plan["hours"], plan["filament"], action["series"])))
        return st, events, {"kind": "dry_run"}
    code = result.get("status") or ("sent" if result.get("success") else "refused")
    if result.get("success"):
        if st["phase"] == "settle" and st["measure_entry"]:
            events.append(_ev("measure", outcome="cancelled", entry=st["measure_entry"]))
        _start_running(st, origin, action["series"], plan["temp"], plan["hours"], plan["filament"], now)
        st["rh_before"], st["t_before"] = action.get("rh_before"), action.get("t_before")
        if origin == "auto":
            st["runs"].append(now)
        if origin == "now":
            st["hold_until"] = st["hold_reason"] = None
            st["retry_at"], st["refusals"] = None, 0
        kind = "run_now" if origin == "now" else ("repeat" if action["trigger"] == "repeat" else "start")
        ev = _ev(kind, series=action["series"], temp=plan["temp"], hours=plan["hours"], filament=plan["filament"])
        if kind != "run_now":
            ev.update(start_rh=action.get("start_rh"), hold_h=action.get("hold_h"),
                      target_rh=action.get("target_rh"), rh=action.get("rh_before"))
        ev.update(rh_before=action.get("rh_before"), t_before=action.get("t_before"), status=code)
        events.append(ev)
        return st, events, {"kind": "starting"}
    detail = result.get("detail") or ""
    st["last_refusal"] = detail
    if origin == "auto":
        if TRANSIENT_REFUSAL_MARK in detail:
            delay = TRANSIENT_RETRY_S          # not a real refusal: no backoff growth
        else:
            st["refusals"] += 1
            delay = retry_delay_s(st["refusals"])
        st["retry_at"] = now + delay
        reason = "printer" if code == "rejected" else "check"   # the printer / transport said no vs not sent
        events.append(_ev("refused", reason=reason, detail=detail, delay=delay, dk="%s:%s" % (reason, detail)))
        return st, events, {"kind": "refused", "until": st["retry_at"], "detail": detail}
    events.append(_ev("refused", reason="now", detail=detail, dk="now:" + detail))
    return st, events, None


def apply_user_start(state: Dict[str, Any], params: Dict[str, Any], now: float
                     ) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """A drying started from the dashboard popover (origin "manual"): not
    counted toward the limit; a foreign row the tick journaled for it a moment
    ago is rewritten."""
    st = load_state(state)
    events: List[Dict[str, Any]] = []
    replace = None
    if st["phase"] == "foreign" and st["foreign_since"] and now - st["foreign_since"] <= FOREIGN_CONVERT_S:
        if st["foreign_since"] in st["runs"]:
            st["runs"].remove(st["foreign_since"])
        series, replace = st["series"], st["foreign_entry"]
    else:
        cont = st["phase"] == "settle" or repeat_due(st, now)
        if st["phase"] == "settle" and st["measure_entry"]:
            events.append(_ev("measure", outcome="cancelled", entry=st["measure_entry"]))
        series = st["series"] + 1 if cont else 1
        if not (cont and st["t_before"] is not None):
            st["t_before"] = as_temp(st["rest_temp"] if st["rest_temp"] is not None else params.get("temp_now"))
        st["rh_before"] = st["last_rest_rh"] if cont and st["last_rest_rh"] is not None else params.get("rh")
    temp, hours, filament = params.get("temp"), params.get("hours"), params.get("filament")
    _start_running(st, "manual", series, temp, hours, filament, now)
    st["hold_until"] = st["hold_reason"] = None
    st["retry_at"], st["refusals"] = None, 0
    events.append(_ev("manual", temp=temp, hours=hours, filament=filament, replace=replace,
                      rh_before=st["rh_before"], t_before=st["t_before"]))
    return st, events


def apply_stop(state: Dict[str, Any], now: float, hold_s: int, reason: str
               ) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """A stop sent from the dashboard or «Остановить все», in any phase: the
    unit is held, the series ends, no repeat."""
    st = load_state(state)
    drying = st["phase"] in ("running", "foreign")
    st["stop_sent_at"] = now
    if hold_s > 0:
        st["hold_until"], st["hold_reason"] = now + hold_s, reason
    st["repeat_pending"] = False
    if st["phase"] == "settle":
        st["closed"] = True
    if st["phase"] == "idle":
        st["series"] = 0
    hold_h = hold_s // 3600 if hold_s % 3600 == 0 else hold_s / 3600.0
    return st, [_ev("manual_stop", hold_h=hold_h, reason=reason, drying=drying,
                    bind="measure_entry" if drying else None, pending=drying)]


def release(state: Dict[str, Any]) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """«снять паузу» / «включить снова»: clears holds, backoffs, refusals,
    interruptions and the auto-disable. Idempotent."""
    st = load_state(state)
    was_disabled = st["disabled_reason"] is not None
    cleared = bool(st["hold_until"] or st["retry_at"] or st["refusals"] or st["interrupts"]
                   or st["blocked_since"] or st["disabled_reason"] or st["backoff_until"])
    st.update(hold_until=None, hold_reason=None, retry_at=None, refusals=0, interrupts=0, blocked_since=None,
              blocked_noted_at=None, disabled_reason=None, disabled_entry=None, backoff_until=None)
    if was_disabled:
        return st, [_ev("reenabled")]
    return st, [_ev("released")] if cleared else []


# --- texts (Russian) ------------------------------------------------------------------

def _run_head(ev: Dict[str, Any]) -> str:
    return "%s° · %s ч (%s)" % (ev.get("temp"), ev.get("hours"), ev.get("filament"))


def journal_text(ev: Dict[str, Any]) -> str:
    k = ev["kind"]
    if k == "start":
        return "Прогон %s · %s: влажность ≥%s%% держалась %s ч" % (ev.get("series"), _run_head(ev),
                                                                  ev.get("start_rh"), ev.get("hold_h"))
    if k == "repeat":
        return "Прогон %s · %s: после остывания %s%% — выше цели %s%%" % (ev.get("series"), _run_head(ev),
                                                                         ev.get("rh"), ev.get("target_rh"))
    if k == "run_now":
        return "Вручную · " + _run_head(ev)
    if k == "manual":
        return "Запущена с дашборда · " + _run_head(ev)
    if k == "finish":
        return "Прогон %s закончен · %s° · %s" % (ev.get("series"), ev.get("temp"), fmt_dur(ev.get("dur")))
    if k == "foreign_start":
        text = ("Чужая сушка перезапустила прогон автосушки — не вмешиваемся" if ev.get("takeover")
                else "Чужая сушка перезапущена — не вмешиваемся" if ev.get("restart")
                else "Чужая сушка — запущена не автосушкой, не вмешиваемся")
        if ev.get("temp") or ev.get("filament"):
            text += " · " + " · ".join(x for x in ("%s°" % ev["temp"] if ev.get("temp") else "",
                                                   str(ev.get("filament") or "")) if x)
        if ev.get("ago"):
            text += " · идёт уже " + fmt_dur(ev["ago"])
        return text
    if k == "foreign_end":
        text = "Чужая сушка закончилась · шла %s · " % fmt_dur(ev.get("dur"))
        if not ev.get("early"):
            return text + "засчитана как прогон"
        text += "остановлена раньше срока"
        return text + (" — не трогаем %s ч" % _hours(ev["hold_h"]) if ev.get("hold_h") else "")
    if k == "interrupted":
        text = "Погасла через %s из %s — не по команде автосушки" % (fmt_dur(ev.get("ran")),
                                                                     fmt_dur(ev.get("planned")))
        if ev.get("hold_h"):
            text += " · не трогаем %s ч" % _hours(ev["hold_h"])
        if (ev.get("n") or 0) >= 2:
            text += " · %d-й раз подряд" % ev["n"]
        return text
    if k == "disabled":
        return "Автосушка блока отключена: %s срыва подряд — проверьте AMS и нажмите «включить снова»" % ev.get("n")
    if k == "lost":
        return "Прогон прервался, пока принтер или сервер был без связи"
    if k == "fault":
        if ev.get("reason") == "stuck":
            return "Сушка идёт дольше плана — проверьте блок, сами не останавливаем"
        return "Ошибка сушки AMS: %s — проверьте блок, сами не останавливаем" % DRY_STATUS_RU.get(
            ev.get("reason"), "ошибка")
    if k == "manual_stop":
        hold = ev.get("hold_h")
        return "Остановлена вручную" + (" · не трогаем %s ч" % _hours(hold) if hold else "")
    if k == "refused":
        reason, detail = ev.get("reason"), ev.get("detail") or ""
        if reason == "now":
            return "Ручной запуск не прошёл: " + detail
        delay = fmt_dur(ev.get("delay"))
        if reason == "silent":
            if ev.get("origin") == "manual":
                return "Сушка с дашборда не началась"
            if ev.get("origin") == "now":
                return "Ручной запуск не прошёл: сушка не началась"
            return "Принтер принял команду, но сушка не началась · повтор через " + delay
        if reason == "printer":
            return "Принтер не принял старт: %s · повтор через %s" % (detail, delay)
        return "Старт отложен: %s · повтор через %s" % (detail, delay)
    if k == "blocked":
        return "Пора сушить, но " + _lc_first(str(ev.get("text") or ""))
    if k == "released":
        return "Пауза снята вручную"
    if k == "reenabled":
        return "Автосушка блока включена заново"
    if k == "dry_run":
        return "Пробный режим: запустили бы прогон %s · %s" % (ev.get("series"), _run_head(ev))
    if k == "unreachable":
        return "Цель не достигается — проверьте осушитель/цель · новая серия не раньше чем через %d ч" % (
            SERIES_BACKOFF_S // 3600)
    return k


def measure_suffix(ev: Dict[str, Any]) -> str:
    """What a measure appends to the row it completes."""
    o = ev.get("outcome")
    if o == "done":
        text = " · цель %s%% достигнута" % ev.get("target")
    elif o == "repeat":
        text = " · выше цели %s%% — будет прогон %d" % (ev.get("target"), (ev.get("series") or 0) + 1)
    elif o == "limit":
        n = ev.get("max_runs") or 0
        text = " · выше цели, но лимит %d %s за сутки — ждём нового старта" % (
            n, plural(n, "прогон", "прогона", "прогонов"))
    elif o == "unreachable":
        n = ev.get("series") or 0
        text = " · выше цели %s%% после %d %s подряд" % (ev.get("target"), n, _runs_gen(n))
    elif o == "held":
        text = " · выше цели, но блок на паузе"
    elif o == "backoff":
        text = " · выше цели, но после неудачной серии ждём %d ч" % (SERIES_BACKOFF_S // 3600)
    elif o == "stopped":
        text = " · без повтора"
    elif o == "failed":
        text = " · замер не удался"
    elif o == "cancelled":
        return " · замер отменён"
    else:
        text = ""
    if ev.get("warm") and ev.get("t_after") is not None:
        text += " (AMS ещё %s°)" % ev["t_after"]
    return text
