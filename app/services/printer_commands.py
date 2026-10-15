"""Команды принтерам: свет, пауза/продолжить/стоп, скорость, обдув и т.п.

Channels (SUPPORTED_ACTIONS):
  · Bambu — local MQTT, the full command set, the firmware checks the signature;
  · Creality — WS :9999 "set": pause/resume/stop and the light (no reply);
  · Klipper — Moonraker JSON-RPC with a reply: pause/resume/cancel and the light pin.
Every send() returns {"success", "status", "detail", "replied"} (_outcome /
_refused); what a printer can do at all is caps_for() (/api/printers "caps").
Ниже — про Bambu-канал, он сложнее.

Канал — локальный MQTT через уже подключённый клиент коллектора (свежую
TLS-сессию X1C обрывает лимитом подключений, поэтому новый клиент не
создаётся). Ответ принтера ловится в report-топике (tee в bambu_collector):
новые прошивки принимают неподписанный print-класс только в Developer Mode,
иначе отвечают "mqtt message verify failed". Облачный канал выпилен: подпись
проверяется и там, так что облако ничего не давало (см. память
bambu-cloud-command-channel).

Капабилити: принимает ли принтер неподписанный print-класс. Где принтер
шлёт print.fun (X1, P2S, H2-серия), ответ — бит 29 (нужна подпись), без
зонда (note_signature из poll-цикла). Где fun нет (P1), фоновый зонд
(безвредный стоп сушки AMS 0, как в Bambu Studio) раз в 10 минут шлёт
команду и смотрит на ответ; он же берёт принтер на себя, если реальный
ответ «verify failed» опроверг fun. Результат отдаётся в /api/printers как
print_cmds (True/False/None) — фронт прячет паузу/стоп/AMS там, где
прошивка их блокирует.

Команды AMS (загрузка/выгрузка, сушка) проверяются по живой телеметрии
клиента коллектора, а не по снимку опроса.
"""
import copy
import json
import logging
import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple

from app.domain.models import PrinterKind, PrinterState, PrinterStatus
from app.services import ams_drying, bambu_extras
from app.services.normalizer import (TRAY_EXTERNAL, TRAY_NONE, _nozzle_range, _tray_now, normalize_bambu,
                                     normalize_ws_dict)
from app.vendor.pybambu.commands import AMS_FILAMENT_DRYING_TEMPLATE, SWITCH_AMS_TEMPLATE

logger = logging.getLogger(__name__)
_monotonic = time.monotonic  # indirection so tests can drive the in-flight / refresh clock
_sleep = time.sleep

_store = None
_lock = threading.Lock()

REPLY_TIMEOUT = 8.0
PROBE_INTERVAL = 600
# The capability probe sends a drying stop: it never publishes within this
# long after any drying start attempt (auto-dry, run-now, dashboard).
PROBE_DRY_QUIET_S = 900
_dry_started: Dict[str, float] = {}   # printer_id -> monotonic of the last dry_start attempt (armed before publish)
MSG_PROBE_SKIPPED = "пропущено: идёт запуск сушки"

# Ответы на локальные команды: bambu_collector делает tee сырых сообщений
# report-топика сюда (pybambu их парсит только для телеметрии).
_seq_lock = threading.Lock()
_seq_counter = 50000
_local_awaited: Dict[str, Dict[str, Any]] = {}

# printer_id -> {"print_cmds": bool|None, "via": "local"|"fun"|"persisted"|None, "ts": float}
_cap_lock = threading.Lock()
_capability: Dict[str, Dict[str, Any]] = {}
# Printers whose print.fun verdict a real reply contradicted ("verify failed"
# although fun said no signature is needed): the probe decides for them again,
# until a restart.
_passive_distrust: set = set()

# Capability keys (/api/printers "caps", see caps_for). tune = temperatures,
# chamber M141, fans and print speed; ams = AMS load / unload / drying and the
# external spool unload.
CAP_KEYS = ("light", "pause", "resume", "stop", "cooldown", "eject", "tune", "ams")
LIGHT_ACTIONS = ("light_on", "light_off")
PRINT_CONTROL = ("pause", "resume", "stop")
ACTION_CAP = {
    "light_on": "light", "light_off": "light", "pause": "pause", "resume": "resume", "stop": "stop",
    "cooldown": "cooldown", "eject": "eject",
    "speed": "tune", "fans": "tune", "preheat": "tune", "chamber": "tune",
    "ams_load": "ams", "ams_unload": "ams", "dry_start": "ams", "dry_stop": "ams",
}
# Actions per printer kind. None = the full Bambu set (see _build_payload).
# Creality: WS :9999 "set" — print control and the light (lightSw), no
# speed / fans / AMS. Klipper: Moonraker — print control and the light pin.
CREALITY_ACTIONS = ("pause", "resume", "stop", "light_on", "light_off")
KLIPPER_ACTIONS = ("pause", "resume", "stop", "light_on", "light_off")
SUPPORTED_ACTIONS: Dict[str, Optional[tuple]] = {
    "bambu": None,
    "creality": CREALITY_ACTIONS,
    "klipper": KLIPPER_ACTIONS,
}
KIND_TITLES = {"bambu": "Bambu", "creality": "Creality", "klipper": "Klipper"}

MSG_KIND = "Команды поддерживаются только для Bambu, Creality и Klipper"
MSG_LIGHT_UNVERIFIED = "Управление светом на этом принтере ещё не проверено"
MSG_NO_LIGHT = "У принтера нет управляемой подсветки"
MSG_NOT_SUPPORTED = "Принтер не поддерживает эту команду"
MSG_NO_COLLECTOR = "коллектор принтера не найден"
MSG_EJECT_INFLIGHT = "Стол уже опускается — подождите"
# A second eject within this window is refused (claimed before the publish,
# like _claim_inflight): each eject is a relative G1 Z30, so a double tap or a
# second tab would move the bed 60 mm.
EJECT_GUARD_SECONDS = 20.0

# Raw Bambu gcode_state sets for the commands that heat / move (fail closed on anything else).
RUNNING_GCODE_STATES = {"RUNNING", "PREPARE", "SLICING"}
COOLDOWN_GCODE_STATES = {"IDLE", "FINISH", "FAILED", "PAUSE"}
EJECT_GCODE_STATES = {"IDLE", "FINISH", "FAILED"}

# Refusals by printer state; mirrored verbatim in app.js CTL_MSG (parity test).
STATE_MSG = {
    "pause_paused": "Уже на паузе",
    "pause_finished": "Печать завершена",
    "pause_error": "Принтер в ошибке — см. карточку",
    "pause_other": "Принтер не печатает",
    "resume": "Принтер не на паузе",
    "stop": "Печать не идёт — останавливать нечего",
    "cooldown": "Во время печати остудить нельзя — сначала пауза или стоп",
    "eject": "Стол опускается, только когда принтер стоит — не во время печати и не на паузе",
    "unknown": "Состояние принтера неизвестно — повторите через несколько секунд",
}

# Why print control (pause / resume / stop) is not available (caps_note).
NOTE_BLOCKED = "прошивка блокирует (нет Developer Mode)"
NOTE_UNKNOWN = "доступ к управлению ещё проверяется"
NOTE_NOT_WIRED = "управление не подключено"
NOTE_KLIPPY = "Klipper не готов"


def set_store(store) -> None:
    """StateStore передаётся из main.py — отсюда берутся коллекторы."""
    global _store
    _store = store


def build_light(on: bool, node: str = "chamber_light") -> Dict[str, Any]:
    """ledctrl for one chamber lamp: "chamber_light", or "chamber_light2" (H2 / X2)."""
    return {"system": {"sequence_id": "0", "command": "ledctrl",
                       "led_node": node,
                       "led_mode": "on" if on else "off",
                       "led_on_time": 500, "led_off_time": 500,
                       "loop_times": 0, "interval_time": 0}}


def build_print_action(action: str) -> Dict[str, Any]:
    return {"print": {"sequence_id": "0", "command": action, "param": ""}}


def build_error_ignore(err: int, job_id: str) -> Dict[str, Any]:
    """Bambu Studio's «Ignore this and Resume» on a print-error dialog (its
    button 27): err is print_error as a decimal string, job_id as the report
    has it."""
    return {"print": {"sequence_id": "0", "command": "ignore", "err": str(int(err)), "param": "reserve",
                      "job_id": str(job_id)}}


# Below 45 °C the printer ignores a drying start (ha-bambulab); 45 was verified
# live on a P2S AMS 2 Pro (fw 01.02, 2026-10-12).
DRY_COOLING_TEMP = 45


def build_dry_stop(ams_id: int = 0) -> Dict[str, Any]:
    """Stop drying AMS `ams_id`: Bambu Studio's stop (mode 0, everything zeroed),
    the vendored lib's AMS_FILAMENT_DRYING_TEMPLATE. Harmless when the unit is
    not drying, so it is also the capability probe's payload."""
    payload = copy.deepcopy(AMS_FILAMENT_DRYING_TEMPLATE)
    payload["print"]["ams_id"] = ams_id
    return payload


def build_dry_start(ams_id: int, temp: int, hours: int, filament: str) -> Dict[str, Any]:
    """Start drying AMS `ams_id`: temp °C for `hours` h, preset `filament` ("PLA"...)."""
    payload = build_dry_stop(ams_id)
    payload["print"].update({"mode": 1, "temp": temp, "duration": hours, "filament": filament,
                             "cooling_temp": DRY_COOLING_TEMP})
    return payload


def build_speed(level: int) -> Dict[str, Any]:
    """Режим скорости печати на лету: 1 тихий · 2 стандарт · 3 спорт · 4 ludicrous."""
    return {"print": {"sequence_id": "0", "command": "print_speed",
                      "param": str(level)}}


def build_gcode(lines) -> Dict[str, Any]:
    """Произвольный G-code (только Developer Mode / fw 01.07). Строки через \\n."""
    return {"print": {"sequence_id": "0", "command": "gcode_line",
                      "param": "".join(str(ln) + "\n" for ln in lines)}}


# Bambu-раскладка вентиляторов в M106: P1 обдув детали, P2 вспомогательный, P3 камера
FAN_MAP = {"part": "P1", "aux": "P2", "chamber": "P3"}


def build_fans(part=None, aux=None, chamber=None) -> Dict[str, Any]:
    """M106 по каждому заданному вентилятору; проценты 0-100 -> S 0-255."""
    lines = []
    for key, val in (("part", part), ("aux", aux), ("chamber", chamber)):
        if val is not None:
            lines.append("M106 %s S%d" % (FAN_MAP[key], round(val * 255 / 100)))
    return build_gcode(lines)


def build_preheat(nozzle=None, bed=None) -> Dict[str, Any]:
    """Неблокирующий нагрев: M104 сопло, M140 стол (без ожидания M109/M190)."""
    lines = []
    if nozzle is not None:
        lines.append("M104 S%d" % nozzle)
    if bed is not None:
        lines.append("M140 S%d" % bed)
    return build_gcode(lines)


# Активный подогрев камеры есть только у H2-серии (H2D/H2S, потолок 65 °C).
# У P-серии и X1C нагревателя камеры нет — там M141 гнать некуда.
CHAMBER_MAX = 65


def has_chamber_heater(device_type: Optional[str]) -> bool:
    return str(device_type or "").upper().startswith("H2")


def has_second_light(model: Optional[str]) -> bool:
    """H2 / X2 have a second chamber lamp: the lib's Features.CHAMBER_LIGHT_2, a
    model-only flag (asking the lib itself logs an ERROR and says False until
    the firmware version is known)."""
    m = str(model or "").upper()
    return m.startswith("H2") or m.startswith("X2")


def bambu_model(printer_id: str, prn: Optional[Dict[str, Any]]) -> str:
    """Upper-case model for the lamp-2 / chamber-heater decisions: the live
    device's info.device_type, then the stored status device_type, then the
    registry model ("" when none is known)."""
    collector = _store.get_collector(printer_id) if _store is not None else None
    try:
        live_device = getattr(collector, "live_device", None)
        dev = live_device() if callable(live_device) else None
        m = getattr(getattr(dev, "info", None), "device_type", None)
    except Exception:
        m = None
    if not m or str(m).lower() == "unknown":
        st = _store.get_one(printer_id) if _store is not None and hasattr(_store, "get_one") else None
        m = (st.device_type if st is not None else None) or (prn or {}).get("model")
    return str(m or "").upper()


def build_chamber(temp: int) -> Dict[str, Any]:
    """Целевая температура камеры: M141 (S0 — выключить нагрев).

    Проверено live на H2S (fw 01.02.00.00): `M141 S0` → result SUCCESS, ctc
    переходит из (65, 65) state 2 в (0, 65) state 0 и камера остывает; `M141 S65`
    возвращает нагрев. Команда неблокирующая — M191 (ждать прогрева) не шлём.
    """
    return build_gcode(["M141 S%d" % temp])


def build_cooldown(model: Optional[str] = None) -> Dict[str, Any]:
    """Вырубить весь нагрев и вентиляторы; на H2 ещё и подогрев камеры (M141 S0)."""
    lines = ["M104 S0", "M140 S0", "M106 P1 S0", "M106 P2 S0", "M106 P3 S0"]
    if has_chamber_heater(model):
        lines.append("M141 S0")
    return build_gcode(lines)


def build_eject() -> Dict[str, Any]:
    """Отвести стол вниз на 30 мм относительным ходом — без хоуминга, безопасно на простое."""
    return build_gcode(["M17", "G91", "G1 Z30 F900", "G90", "M400"])


def build_ams_load(ams_id: int, slot_id: int, curr_temp: int, tar_temp: int) -> Dict[str, Any]:
    """Load the filament of an AMS slot (Bambu Studio's ams_change_filament).

    ams_id / slot_id address the slot (new firmware); target is the older
    firmware's tray index: ams_id * 4 + slot_id for the 4-slot units (ids < 16),
    the unit id itself for an AMS HT (128+). curr_temp: the nozzle temperature
    for cutting / retracting the loaded filament; tar_temp: for the new one.
    """
    payload = copy.deepcopy(SWITCH_AMS_TEMPLATE)
    tray = ams_id * 4 + slot_id if ams_id < 16 else 0
    payload["print"].update({"ams_id": ams_id, "slot_id": slot_id, "target": tray or ams_id,
                             "curr_temp": curr_temp, "tar_temp": tar_temp})
    return payload


def build_ams_unload(ams_id: int, temp: int) -> Dict[str, Any]:
    """Unload the extruder back into AMS `ams_id` (255 = external spool).
    slot_id 255 marks an unload on new firmware, target 255 on older firmware."""
    payload = copy.deepcopy(SWITCH_AMS_TEMPLATE)
    payload["print"].update({"ams_id": ams_id, "slot_id": 255, "target": 255,
                             "curr_temp": temp, "tar_temp": temp})
    return payload


def _opt_int(params: Dict[str, Any], key: str, lo: int, hi: int, name: str):
    v = params.get(key)
    if v is None:
        return None
    if not isinstance(v, int) or isinstance(v, bool) or not (lo <= v <= hi):
        raise ValueError("%s: целое %d-%d" % (name, lo, hi))
    return v


def _req_int(params: Dict[str, Any], key: str, lo: int, hi: int, name: str) -> int:
    v = _opt_int(params, key, lo, hi, name)
    if v is None:
        raise ValueError("%s: целое %d-%d" % (name, lo, hi))
    return v


def _build_payload(action: str, params: Dict[str, Any], model: Optional[str] = None) -> Dict[str, Any]:
    """Print-class payload of a Bambu command (lights: _send_bambu_light)."""
    if action in PRINT_CONTROL:
        return build_print_action(action)
    if action == "speed":
        level = params.get("level")
        if not isinstance(level, int) or isinstance(level, bool) or not (1 <= level <= 4):
            raise ValueError("Режим скорости: 1-4")
        return build_speed(level)
    if action == "fans":
        part = _opt_int(params, "part", 0, 100, "Обдув детали")
        aux = _opt_int(params, "aux", 0, 100, "Вспомогательный обдув")
        chamber = _opt_int(params, "chamber", 0, 100, "Обдув камеры")
        if part is None and aux is None and chamber is None:
            raise ValueError("Не задан ни один вентилятор")
        return build_fans(part, aux, chamber)
    if action == "preheat":
        nozzle = _opt_int(params, "nozzle", 0, 300, "Сопло")
        bed = _opt_int(params, "bed", 0, 120, "Стол")
        if nozzle is None and bed is None:
            raise ValueError("Не задана температура нагрева")
        return build_preheat(nozzle, bed)
    if action == "chamber":
        return build_chamber(_req_int(params, "temp", 0, CHAMBER_MAX, "Камера"))
    if action == "cooldown":
        return build_cooldown(model)
    if action == "eject":
        return build_eject()
    raise ValueError(f"Неизвестная команда: {action}")


def state_refusal(action: str, state: Optional[str], gcode_state: Optional[str] = None) -> Optional[str]:
    """Why `action` must not be sent in this state (ru), or None. `state` is the
    normalized PrinterState value; `gcode_state` (Bambu raw, upper case, may be
    "") decides cooldown / eject when given."""
    if action in PRINT_CONTROL and state in (None, "unknown"):
        return STATE_MSG["unknown"]          # never «не печатает» for a printer that may be printing
    if action == "pause":
        if state == "printing":
            return None
        return {"paused": STATE_MSG["pause_paused"], "finished": STATE_MSG["pause_finished"],
                "error": STATE_MSG["pause_error"]}.get(state, STATE_MSG["pause_other"])
    if action == "resume":
        return None if state == "paused" else STATE_MSG["resume"]
    if action == "stop":
        return None if state in ("printing", "paused") else STATE_MSG["stop"]
    if action in ("cooldown", "eject"):
        allowed = COOLDOWN_GCODE_STATES if action == "cooldown" else EJECT_GCODE_STATES
        if gcode_state is not None:
            if gcode_state in allowed:
                return None
            busy = RUNNING_GCODE_STATES | ({"PAUSE"} if action == "eject" else set())
            return STATE_MSG[action] if gcode_state in busy else STATE_MSG["unknown"]
        blocked = ("printing",) if action == "cooldown" else ("printing", "paused")
        if state in blocked:
            return STATE_MSG[action]
        return STATE_MSG["unknown"] if state in (None, "unknown", "offline") else None
    return None


def _next_seq() -> str:
    global _seq_counter
    with _seq_lock:
        _seq_counter += 1
        return str(_seq_counter)


def note_local_reply(serial: Optional[str], payload: bytes) -> None:
    """Tee из pybambu on_message: ловим ответы принтера на наши команды."""
    if not _local_awaited:  # быстрый путь: телеметрия идёт постоянно
        return
    try:
        data = json.loads(payload)
    except Exception:
        return
    for cls in ("system", "print"):
        block = data.get(cls)
        if isinstance(block, dict) and "result" in block:
            with _seq_lock:
                waiter = _local_awaited.get(str(block.get("sequence_id")))
            if waiter is not None:
                waiter["result"] = block.get("result")
                waiter["reason"] = block.get("reason") or str(block.get("err_code") or "")
                waiter["errno"] = block.get("errno")
                waiter["soft_temp"] = block.get("soft_temp")
                waiter["event"].set()


def _dry_quiet(printer_id: str) -> bool:
    """Was a drying start attempted on this printer less than PROBE_DRY_QUIET_S ago?"""
    t = _dry_started.get(printer_id)
    return t is not None and _monotonic() - t < PROBE_DRY_QUIET_S


def _send_local(printer_id: str, payload: Dict[str, Any], guard: Optional[Callable[[], bool]] = None) -> Dict[str, Any]:
    """Publish and wait for the reply. `guard` runs under the publish lock right
    before the publish; True skips the command ({"skipped": True})."""
    collector = _store.get_collector(printer_id) if _store is not None else None
    client = getattr(collector, "client", None)
    if client is None:
        return {"success": False, "detail": MSG_NO_COLLECTOR, "replied": False}
    seq = _next_seq()
    sendable = json.loads(json.dumps(payload))
    for cls in ("system", "print"):
        if cls in sendable:
            sendable[cls]["sequence_id"] = seq
    waiter = {"event": threading.Event(), "result": None, "reason": None, "errno": None, "soft_temp": None}
    with _seq_lock:
        _local_awaited[seq] = waiter
    try:
        with _lock:
            if guard is not None and guard():
                return {"success": False, "detail": MSG_PROBE_SKIPPED, "replied": False, "skipped": True}
            try:
                ok = bool(client.publish(sendable))
            except Exception as e:
                return {"success": False, "detail": str(e), "replied": False}
        if not ok:
            return {"success": False, "detail": "publish failed", "replied": False}
        got = waiter["event"].wait(REPLY_TIMEOUT)
    finally:
        with _seq_lock:
            _local_awaited.pop(seq, None)
    if got and waiter["result"] not in (None, "success", "SUCCESS"):
        reason = str(waiter["reason"] or waiter["result"])
        verify = "verify failed" in reason
        detail = None
        if not verify and sendable.get("print", {}).get("command") == "ams_change_filament":
            detail = _change_refusal_text(waiter["errno"], waiter["soft_temp"])
        if detail is None:
            detail = "🔒 прошивка блокирует сторонние команды" if verify else "принтер отклонил: %s" % reason
        return {"success": False, "detail": detail, "replied": True, "verify_failed": verify}
    return {"success": True,
            "detail": "подтверждено принтером" if got else "отправлено (без подтверждения)",
            "replied": got}


# Lamp 2 (H2 / X2) is published before lamp 1 and normally answers first (live:
# ~50 ms earlier); after lamp 1's verdict it gets this long before it counts as
# unconfirmed.
LAMP2_REPLY_GRACE = 1.0


def _new_waiter() -> Dict[str, Any]:
    return {"event": threading.Event(), "result": None, "reason": None, "errno": None, "soft_temp": None,
            "seq": None}


def _forget_waiter(waiter: Dict[str, Any]) -> None:
    with _seq_lock:
        _local_awaited.pop(waiter.get("seq"), None)


def _publish_nowait(printer_id: str, payload: Dict[str, Any], waiter: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """Publish with a fresh sequence_id without waiting: None when published,
    else a short error text. A `waiter` (_new_waiter) is registered for the
    printer's reply before the publish — note_local_reply fills it, the caller
    reads it when it needs to and drops it with _forget_waiter. Without one the
    reply finds no waiter and is ignored."""
    collector = _store.get_collector(printer_id) if _store is not None else None
    client = getattr(collector, "client", None)
    if client is None:
        return MSG_NO_COLLECTOR
    seq = _next_seq()
    sendable = json.loads(json.dumps(payload))
    for cls in ("system", "print"):
        if cls in sendable:
            sendable[cls]["sequence_id"] = seq
    if waiter is not None:
        waiter["seq"] = seq
        with _seq_lock:
            _local_awaited[seq] = waiter
    try:
        with _lock:
            return None if client.publish(sendable) else "publish failed"
    except Exception as e:
        return str(e)


def _apply_light_mode(printer_id: str, nodes, on: bool) -> None:
    """Write the confirmed mode into the lib's lights object as its own
    TurnChamberLight* do (value + *_override), so the next normalize reads it
    even if no lights_report follows (delta-only printers: minutes)."""
    collector = _store.get_collector(printer_id) if _store is not None else None
    try:
        live_device = getattr(collector, "live_device", None)
        dev = live_device() if callable(live_device) else None
        lights = getattr(dev, "lights", None)
        if lights is None:
            return
        mode = "on" if on else "off"
        for node in nodes:                                   # "chamber_light", "chamber_light2"
            setattr(lights, node, mode)
            setattr(lights, node + "_override", mode)
    except Exception:
        logger.warning("[commands] %s light state not applied", printer_id, exc_info=True)


def _send_bambu_light(printer_id: str, prn: Dict[str, Any], on: bool) -> Dict[str, Any]:
    """Both chamber lamps on H2 / X2. Lamp 2 goes first, so both switch
    together; lamp 1's reply is the verdict, and the detail says when lamp 2
    did not confirm (publish error, silence, refusal). A lamp's mode goes into
    the lib only after that lamp's own confirmed reply: an unanswered or
    refused one may have switched nothing, and its override would hide the
    printer's reports of it."""
    w2, note2 = None, None
    if has_second_light(bambu_model(printer_id, prn)):
        w2 = _new_waiter()
        note2 = _publish_nowait(printer_id, build_light(on, "chamber_light2"), w2)
        if note2:
            logger.warning("[commands] %s chamber_light2: %s", printer_id, note2)
    try:
        r = _send_local(printer_id, build_light(on))
        if r.get("success") and r.get("replied"):
            nodes = ["chamber_light"]
            if w2 is not None and not note2:
                if not w2["event"].wait(LAMP2_REPLY_GRACE):
                    note2 = "без ответа"
                elif w2["result"] not in (None, "success", "SUCCESS"):
                    note2 = "отклонила — %s" % (w2["reason"] or w2["result"])
                else:
                    nodes.append("chamber_light2")
                if note2:
                    logger.warning("[commands] %s chamber_light2: %s", printer_id, note2)
            _apply_light_mode(printer_id, nodes, on)
    finally:
        if w2 is not None:
            _forget_waiter(w2)
    if r.get("success") and note2:
        r = dict(r, detail="%s (вторая лампа: %s)" % (r["detail"], note2))
    return r


def _send_klipper(printer_id: str, action: str) -> Dict[str, Any]:
    """Klipper: Moonraker JSON-RPC through the collector's live WS session;
    Moonraker's reply (or its silence) is the verdict."""
    collector = _store.get_collector(printer_id) if _store is not None else None
    client = getattr(collector, "client", None)
    if client is None or not hasattr(client, "run_action"):
        return {"success": False, "detail": MSG_NO_COLLECTOR, "replied": False}
    try:
        return client.run_action(action, timeout=REPLY_TIMEOUT)
    except Exception as e:
        logger.error("[commands] %s klipper %s failed", printer_id, action, exc_info=True)
        return {"success": False, "detail": str(e), "replied": False}


def _outcome(r: Dict[str, Any]) -> Dict[str, Any]:
    """The send() result: {"success", "status", "detail", "replied"}. status:
    confirmed (the printer acknowledged), sent (no acknowledgement in time),
    rejected (the printer or the transport said no); `replied` keeps its
    meaning (True on a failure = the printer itself refused)."""
    ok, replied = bool(r.get("success")), bool(r.get("replied"))
    status = ("confirmed" if replied else "sent") if ok else "rejected"
    return {"success": ok, "status": status, "detail": r.get("detail") or "", "replied": replied}


def _refused(detail: str) -> Dict[str, Any]:
    """Nothing was sent: unknown kind / action, capability, parameters, state."""
    return {"success": False, "status": "refused", "detail": detail, "replied": False}


MSG_NOT_PAUSED_ON = "Принтер не стоит на паузе с этой ошибкой"
MSG_NO_JOB_ID = "В отчёте принтера нет номера задания"


def live_print_error(printer_id: str) -> Optional[Dict[str, Any]]:
    """{gcode_state, err, stage, job_id} from live telemetry, or None."""
    from app.services import printer_registry

    view = _live_view(printer_id, printer_registry.get_printer(printer_id) or {})
    if view is None:
        return None
    try:
        err = int(getattr(getattr(view.device, "print_job", None), "print_error", 0) or 0)
    except (TypeError, ValueError):
        err = 0
    return {"gcode_state": view.gcode_state, "err": err, "stage": view.status.stage,
            "job_id": (view.raw or {}).get("job_id")}


def ignore_print_error(printer_id: str, err: int) -> Dict[str, Any]:
    """Press «Ignore and resume» for print_error `err` — only while the printer is
    paused on exactly that error right now (live telemetry)."""
    ctx = live_print_error(printer_id)
    if ctx is None:
        return _refused(MSG_NO_TELEMETRY)
    if ctx["gcode_state"] != "PAUSE" or ctx["err"] != err:
        return _refused(MSG_NOT_PAUSED_ON)
    if not ctx["job_id"]:
        return _refused(MSG_NO_JOB_ID)
    return _outcome(_send_local(printer_id, build_error_ignore(err, ctx["job_id"])))


def _send_creality(printer_id: str, action: str) -> Dict[str, Any]:
    """Creality: команда уходит в живую WS-сессию коллектора (метод "set" на :9999).

    Подписи, как у Bambu, здесь нет — прошивка принимает pause/resume/stop без
    проверок, поэтому зонд капабилити не нужен. Подтверждения на команду
    прошивка тоже не шлёт: факт паузы видно только по смене state в телеметрии,
    которую и так забирает poll-цикл.
    """
    collector = _store.get_collector(printer_id) if _store is not None else None
    if collector is None or not hasattr(collector, "send_command"):
        return {"success": False, "detail": MSG_NO_COLLECTOR}
    try:
        r = collector.send_command(action)
    except Exception as e:
        logger.error("[commands] %s creality %s failed", printer_id, action, exc_info=True)
        return {"success": False, "detail": str(e)}
    if not isinstance(r, dict) or not r.get("success"):
        detail = (r or {}).get("detail") if isinstance(r, dict) else None
        return {"success": False, "detail": detail or "принтер не принял команду"}
    return {"success": True, "detail": "отправлено (без подтверждения)"}


def _persist_capabilities() -> None:
    """Сохранить известные (не None) вердикты в settings, чтобы они пережили
    рестарт. Никакой сбой персиста не должен ронять телеметрию/команды."""
    try:
        from app.services import settings_service
        with _cap_lock:
            snapshot = {pid: v["print_cmds"] for pid, v in _capability.items()
                        if isinstance(v.get("print_cmds"), bool)}
        settings_service.set_many({"printer_capability": snapshot})
    except Exception:
        logger.debug("persist capability failed", exc_info=True)


def load_persisted_capabilities() -> None:
    """Подтянуть сохранённые вердикты зонда на старте. Вызывать до/на старте
    poll-цикла — тогда после рестарта capability сразу известна, а не None."""
    try:
        from app.services import settings_service
        saved = settings_service.get("printer_capability") or {}
    except Exception:
        logger.debug("load capability failed", exc_info=True)
        return
    with _cap_lock:
        for pid, val in saved.items():
            if pid not in _capability and isinstance(val, bool):
                _capability[pid] = {"print_cmds": val, "via": "persisted", "ts": time.time()}


def _note_capability(printer_id: str, print_cmds: Optional[bool], via: Optional[str]) -> None:
    with _cap_lock:
        prev = (_capability.get(printer_id) or {}).get("print_cmds")
        # Transient silence (None) must not clobber a known verdict in memory —
        # a single missed probe would otherwise hide the control buttons until the
        # next probe (~10 min). Keep the last known bool; just refresh the ts.
        if print_cmds is None and isinstance(prev, bool):
            _capability[printer_id]["ts"] = time.time()
            return
        _capability[printer_id] = {"print_cmds": print_cmds, "via": via, "ts": time.time()}
    if prev != print_cmds:
        logger.info("[commands] %s print_cmds: %s -> %s (via %s)", printer_id, prev, print_cmds, via)
        # None (принтер молчит) не персистим — не затираем известный вердикт
        if print_cmds is not None:
            _persist_capabilities()


def note_signature(printer_id: str, required: Optional[bool]) -> None:
    """The verdict from telemetry: print.fun bit 29 (see bambu_extras.signature_required).

    Called by the poll loop for every Bambu status, so printers that report
    `fun` (X1, P2S, H2 series) get print_cmds without a probe; None (P1
    series) leaves the probe to decide.
    """
    if required is None or printer_id in _passive_distrust:
        return
    _note_capability(printer_id, not required, "fun")


def get_capability(printer_id: str) -> Optional[bool]:
    """True — print-класс принимается; False — прошивка блокирует; None — не знаем."""
    with _cap_lock:
        return (_capability.get(printer_id) or {}).get("print_cmds")


def capability_for(printer_id: str, kind: Optional[str], online: bool) -> Optional[bool]:
    """print_cmds для /api/printers — по нему фронт решает, рисовать ли кнопки.

    Bambu — вердикт зонда (прошивка может блокировать print-класс). Creality —
    подписи нет, зондировать нечего: канал доступен ровно пока жива WS-сессия.
    Остальные типы команд не поддерживают вовсе.
    """
    if kind == "bambu":
        return get_capability(printer_id)
    if kind == "creality":
        return bool(online)
    return None


# --- AMS: filament change -----------------------------------------------------
#
# ams_* commands are checked against the printer's live telemetry, never the
# poll loop's stored status (up to POLL_INTERVAL old): the device the
# collector's MQTT client keeps current, normalized on the spot. No live
# telemetry, or any doubt about the state, means no command (fail closed).

AMS_ACTIONS = ("ams_load", "ams_unload")
# Load / unload only from these raw gcode states. PREPARE, SLICING, RUNNING,
# PAUSE and anything new are refused as printing; UNKNOWN (the lib's value
# until the first report says otherwise) and "" as not known yet.
SWAP_GCODE_STATES = {"IDLE", "FINISH", "FAILED"}
UNKNOWN_GCODE_STATES = {"", "UNKNOWN"}
# ams_status main states (bits 8-15) that leave the AMS free: 0 idle, 3 assist
# (feeding the extruder). 1 = filament change (gated on its own), 2 RFID read,
# 4 calibration, 7 cold pull, 0x10 self check, 0x20 debug.
AMS_FREE_MAIN_STATES = (0, 3)
# A second load / unload within this window after one was sent is refused
# unless the printer has run a change since: before its first busy report a
# double click would otherwise pass every check. The mark is claimed right
# before the publish (check and set in one step), so of two requests racing
# for one printer only one is sent; a failed send gives it back.
INFLIGHT_SECONDS = 20.0
# Nozzle temperature for a spool without a profile range (Bambu Studio's
# default is 210; 220 is what this dashboard always sent and suits PLA / PETG).
DEFAULT_SWAP_TEMP = 220
SWAP_TEMP_MIN = 170


def _swap_temp_max(model: Optional[str]) -> int:
    # The H2 series hotend goes to 350 °C, the others to 300 °C.
    return 350 if str(model or "").upper().startswith("H2") else 300


MSG_NO_TELEMETRY = "Нет свежей телеметрии принтера — повторите через несколько секунд"
MSG_DUAL_NOZZLE = ("Принтеры с двумя экструдерами пока не поддерживаются — "
                   "загрузка и выгрузка с экрана принтера")
MSG_FW_UNKNOWN = "Версия прошивки принтера ещё не получена — повторите через несколько секунд"
MSG_FW_NO_SWAP = "Прошивка принтера не поддерживает смену филамента командой"
MSG_PRINTING = "Принтер занят печатью — загрузка и выгрузка филамента доступны только в простое"
MSG_STATE_UNKNOWN = "Состояние принтера ещё неизвестно — повторите через несколько секунд"
MSG_CHANGE_RUNNING = "Идёт смена филамента — дождитесь окончания"
MSG_INFLIGHT = "Команда смены филамента уже отправлена — дождитесь окончания"
MSG_AMS_BUSY = "AMS занят (калибровка, чтение RFID или самопроверка) — дождитесь окончания"
MSG_PICK_SLOT = "Укажите блок AMS и слот (обновите страницу)"
MSG_EXTERNAL_LOAD = "Загрузка с внешней катушки — только с экрана принтера"
MSG_ALREADY_LOADED = "Этот филамент уже загружен"
MSG_EXTRUDER_EMPTY = "Филамент не загружен в экструдер — выгружать нечего"
MSG_NOT_LOADED = "Филамент не загружен — выгружать нечего"
MSG_LOADED_UNKNOWN = "Нет данных о загруженном филаменте"
# errno of a refused ams_change_filament (Bambu Studio warns with these): -2
# the chamber, -4 the AMS is too hot for the filament, soft_temp = the
# temperature to wait for; -2 without it: the chamber or its target is above
# 45 °C, where low-temperature filament would clog the extruder.
MSG_HOT_CHAMBER = ("Камера слишком горячая — филамент может размягчиться. "
                   "Дождитесь, пока она остынет ниже %d °C: откройте дверцу или включите вентиляторы")
MSG_HOT_CHAMBER_45 = ("Камера горячее 45 °C (сейчас или по заданной цели) — PLA, PETG и TPU загружать нельзя, "
                      "чтобы не забить экструдер")
MSG_HOT_AMS = "AMS слишком горячий — филамент может размягчиться. Дождитесь, пока он остынет ниже %d °C"
MSG_HOT_AMS_WAIT = "AMS слишком горячий — филамент может размягчиться. Дождитесь, пока он остынет"


def _change_refusal_text(errno: Any, soft_temp: Any) -> Optional[str]:
    """Why the printer refused a load / unload, from the reply's errno (ru), or None."""
    if not _is_int(errno):
        return None
    soft = soft_temp if _is_int(soft_temp) and soft_temp > 0 else None
    if errno == -2:
        return MSG_HOT_CHAMBER % soft if soft else MSG_HOT_CHAMBER_45
    if errno == -4:
        return MSG_HOT_AMS % soft if soft else MSG_HOT_AMS_WAIT
    return None

_inflight_lock = threading.Lock()
_ams_inflight: Dict[str, tuple] = {}   # printer_id -> (sent at, RawReport changes_done then)


class _LiveView:
    """The printer's live telemetry: the lib's Device, its fresh normalized
    status and the RawReport snapshot."""

    def __init__(self, device, status, raw):
        self.device, self.status, self.raw = device, status, raw

    @property
    def gcode_state(self) -> str:
        return str(getattr(getattr(self.device, "print_job", None), "gcode_state", "") or "").upper()

    @property
    def units(self) -> list:
        return (self.status.ams or {}).get("units") or []

    @property
    def tray_now(self) -> Optional[int]:
        if self.status.ams:
            return self.status.ams.get("tray_now")
        ams_obj = getattr(self.device, "ams", None)
        return _tray_now(ams_obj, []) if ams_obj is not None else None


def _live_view(printer_id: str, prn: Dict[str, Any]) -> Optional[_LiveView]:
    collector = _store.get_collector(printer_id) if _store is not None else None
    live_device = getattr(collector, "live_device", None)
    if not callable(live_device):
        return None
    try:
        device = live_device()
        if device is None:
            return None
        status = normalize_bambu(printer_id, prn.get("label") or printer_id, device,
                                 device_type=prn.get("model"))
        raw = device.forge_raw.snapshot() if hasattr(device, "forge_raw") else {}
    except Exception:
        logger.error("[commands] %s live telemetry read failed", printer_id, exc_info=True)
        return None
    if not status.online:
        return None
    return _LiveView(device, status, raw)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _slot_temp(slot: Optional[Dict[str, Any]]) -> Optional[int]:
    """Middle of the spool profile's nozzle range, as Bambu Studio picks it."""
    rng = (slot or {}).get("nozzle_temp")
    if not rng or slot.get("empty"):
        return None
    return (rng[0] + rng[1]) // 2


def _external_temp(view: _LiveView) -> Optional[int]:
    """Middle of the profile range of the external spool in the extruder, as
    ha-bambulab picks it: the lib's external_spool[1] when the H2D feeds from
    its second one (ams_id 254), else [0]. None without a range."""
    spools = getattr(view.device, "external_spool", None) or ()
    try:
        second = getattr(getattr(view.device, "ams", None), "active_ams_index", None) == 254
    except Exception:
        second = False
    spool = spools[1] if second and len(spools) > 1 else (spools[0] if spools else None)
    rng = _nozzle_range(spool) if spool is not None else None
    return (rng[0] + rng[1]) // 2 if rng else None


def _clamp_swap_temp(temp: int, model: Optional[str]) -> int:
    return max(SWAP_TEMP_MIN, min(_swap_temp_max(model), temp))


def _locate(units: list, tray_now: Optional[int]):
    """(unit, slot index) of a cumulative tray_now, or (None, None)."""
    if tray_now is None:
        return None, None
    base = 0
    for unit in units:
        n = len(unit.get("slots") or [])
        if base <= tray_now < base + n:
            return unit, tray_now - base
        base += n
    return None, None


def _inflight_held(printer_id: str, raw: Dict[str, Any]) -> bool:
    """Is a load / unload still on its way? Drops a mark that lapsed or that a
    finished change settled. The caller holds _inflight_lock."""
    mark = _ams_inflight.get(printer_id)
    if mark is None:
        return False
    sent_at, done_then = mark
    if _monotonic() - sent_at >= INFLIGHT_SECONDS or raw.get("changes_done", 0) > done_then:
        del _ams_inflight[printer_id]
        return False
    return True


def _inflight_blocks(printer_id: str, raw: Dict[str, Any]) -> bool:
    with _inflight_lock:
        return _inflight_held(printer_id, raw)


def _claim_inflight(printer_id: str, raw: Dict[str, Any]) -> Optional[tuple]:
    """Take the in-flight mark for a load / unload about to be published: the
    new mark, or None when another one holds it."""
    with _inflight_lock:
        if _inflight_held(printer_id, raw):
            return None
        mark = (_monotonic(), raw.get("changes_done", 0))
        _ams_inflight[printer_id] = mark
        return mark


def _release_inflight(printer_id: str, mark: tuple) -> None:
    """Give the mark back after a failed send (unless a later claim replaced it)."""
    with _inflight_lock:
        if _ams_inflight.get(printer_id) is mark:
            del _ams_inflight[printer_id]


def _plan_load(params: Dict[str, Any], view: _LiveView, model: Optional[str]):
    ams_id, slot = params.get("ams"), params.get("slot")
    if not _is_int(ams_id):
        return MSG_PICK_SLOT
    if ams_id in bambu_extras.EXTERNAL_SPOOLS:
        return MSG_EXTERNAL_LOAD
    name = bambu_extras.ams_unit_name(ams_id)
    base, unit = 0, None
    for u in view.units:
        if u.get("index") == ams_id:
            unit = u
            break
        base += len(u.get("slots") or [])
    if unit is None:
        return "%s не найден" % name
    slots = unit.get("slots") or []
    if not _is_int(slot) or not 0 <= slot < len(slots):
        return "%s: нет такого слота" % name
    target = slots[slot]
    if target.get("empty"):
        return "Слот %d пуст" % (base + slot + 1)
    loaded = view.status.filament_loaded
    tray_now = view.tray_now
    if tray_now == base + slot and loaded is True:
        return MSG_ALREADY_LOADED
    tar_temp = _slot_temp(target) or DEFAULT_SWAP_TEMP
    curr_temp = tar_temp
    if loaded is not False:
        if tray_now == TRAY_NONE:
            curr_temp = DEFAULT_SWAP_TEMP
        elif tray_now == TRAY_EXTERNAL:
            curr_temp = _external_temp(view) or DEFAULT_SWAP_TEMP
        else:
            src_unit, src_slot = _locate(view.units, tray_now)
            if src_unit is not None and not src_unit["slots"][src_slot].get("empty"):
                curr_temp = _slot_temp(src_unit["slots"][src_slot]) or DEFAULT_SWAP_TEMP
    return build_ams_load(ams_id, slot, _clamp_swap_temp(curr_temp, model), _clamp_swap_temp(tar_temp, model))


def _external_unload(view: _LiveView, model: Optional[str]) -> Dict[str, Any]:
    return build_ams_unload(255, _clamp_swap_temp(_external_temp(view) or DEFAULT_SWAP_TEMP, model))


def _plan_unload(view: _LiveView, model: Optional[str]):
    loaded = view.status.filament_loaded
    if loaded is False:
        return MSG_EXTRUDER_EMPTY if bambu_extras.new_protocol(view.raw) else MSG_NOT_LOADED
    tray_now = view.tray_now
    if loaded is None or tray_now is None or tray_now == TRAY_NONE:
        return MSG_LOADED_UNKNOWN
    if tray_now == TRAY_EXTERNAL:
        return _external_unload(view, model)
    unit, slot = _locate(view.units, tray_now)
    if unit is None:
        return MSG_LOADED_UNKNOWN
    src = unit["slots"][slot]
    if src.get("empty"):
        # The P2S keeps a stale AMS index while it feeds from the external spool.
        return _external_unload(view, model)
    return build_ams_unload(unit["index"], _clamp_swap_temp(_slot_temp(src) or DEFAULT_SWAP_TEMP, model))


def _plan_filament_change(printer_id: str, action: str, params: Dict[str, Any], view: _LiveView,
                          model: Optional[str]):
    """ams_change_filament payload for a load / unload, or the reason (ru) why not."""
    st, raw = view.status, view.raw
    if st.dual_nozzle:
        return MSG_DUAL_NOZZLE
    if st.filament_swap_ok is None:
        return MSG_FW_UNKNOWN
    if st.filament_swap_ok is False:
        return MSG_FW_NO_SWAP
    if view.gcode_state in UNKNOWN_GCODE_STATES:
        return MSG_STATE_UNKNOWN
    if view.gcode_state not in SWAP_GCODE_STATES:
        return MSG_PRINTING
    if st.filament_change is not None or raw.get("busy"):
        return MSG_CHANGE_RUNNING
    if _inflight_blocks(printer_id, raw):
        return MSG_INFLIGHT
    ams_status = raw.get("ams_status")
    if ams_status is not None and (ams_status >> 8) & 0xFF not in AMS_FREE_MAIN_STATES:
        return MSG_AMS_BUSY
    if action == "ams_load":
        return _plan_load(params, view, model)
    return _plan_unload(view, model)


# --- AMS: drying ----------------------------------------------------------------
#
# Allowed on any AMS 2 Pro / AMS HT, also while printing (from that AMS too,
# within the print limits); checked against the live telemetry like the above.

DRY_ACTIONS = ("dry_start", "dry_stop")
MSG_PICK_UNIT = "Укажите блок AMS (обновите страницу)"
MSG_NO_REMOTE_DRY = "Прошивка принтера не сообщает о поддержке удалённой сушки"
MSG_DRYING = "Сушка уже идёт — сначала остановите её"
MSG_DRY_STOPPING = "Сушка останавливается — подождите"
MSG_DRY_HOURS = "Длительность сушки: целое %d–%d ч" % (ams_drying.HOURS_MIN, ams_drying.HOURS_MAX)
MSG_DRY_MATERIAL = "Материал: %s или %s" % (", ".join(list(ams_drying.MATERIALS)[:-1]),
                                            list(ams_drying.MATERIALS)[-1])


def _dry_temp_error(dry: Dict[str, Any]) -> str:
    msg = "Температура сушки: целое %d–%d °C" % (ams_drying.TEMP_MIN, dry["max_temp"])
    by = dry.get("max_by")
    shown = "катушка без материала" if by == "?" else by
    if dry.get("feeding"):
        return msg + ", идёт печать из этого AMS" + (" (%s)" % shown if by else "")
    if by:
        return msg + ", в AMS есть " + shown
    return msg


def _plan_drying(action: str, params: Dict[str, Any], view: _LiveView):
    """ams_filament_drying payload, or the reason (ru) why not."""
    ams_id = params.get("ams")
    if not _is_int(ams_id):
        return MSG_PICK_UNIT
    name = bambu_extras.ams_unit_name(ams_id)
    unit = next((u for u in view.units if u.get("index") == ams_id), None)
    if unit is None:
        return "%s не найден" % name
    dry = unit.get("dry")
    if dry is None:
        return "%s не умеет сушить (нужен AMS 2 Pro или AMS HT)" % name
    if action == "dry_stop":
        return build_dry_stop(ams_id)
    if view.status.ams_dry_ok is not True:
        return MSG_NO_REMOTE_DRY
    if dry["active"]:
        return MSG_DRYING
    if dry["status"] == "stopping":
        return MSG_DRY_STOPPING
    if dry["blockers"]:
        return "Сушка сейчас недоступна: %s" % dry["blockers"][0]["text"]
    temp, hours = params.get("temp"), params.get("hours")
    if not _is_int(temp) or not ams_drying.TEMP_MIN <= temp <= dry["max_temp"]:
        return _dry_temp_error(dry)
    if not _is_int(hours) or not ams_drying.HOURS_MIN <= hours <= ams_drying.HOURS_MAX:
        return MSG_DRY_HOURS
    filament = ams_drying.canonical_filament(params.get("filament"))
    if filament is None:
        return MSG_DRY_MATERIAL
    return build_dry_start(ams_id, temp, hours, filament)


def _note_reply(printer_id: str, payload: Dict[str, Any], r: Dict[str, Any]) -> None:
    """Any reply to a print-class command settles the capability verdict. A
    "verify failed" against a verdict from print.fun means fun cannot be
    trusted for that printer: the probe takes over again."""
    if not (r.get("replied") and "print" in payload):
        return
    if r.get("verify_failed"):
        with _cap_lock:
            via = (_capability.get(printer_id) or {}).get("via")
        if via == "fun" and printer_id not in _passive_distrust:
            _passive_distrust.add(printer_id)
            logger.warning("[commands] %s refused an unsigned command although print.fun says no "
                           "signature is needed; the probe decides for it from now on", printer_id)
    _note_capability(printer_id, not r.get("verify_failed"), "local")


# --- live refresh after a command ----------------------------------------------------
#
# The poll loop refreshes the store every POLL_INTERVAL (30 s or more): too
# slow to follow a 1-2 minute filament change, a drying start or a pause.
# After a successful command that printer is re-normalized from its live
# telemetry into the store every 2 s (store.upsert only: no history row, no
# notification), until nothing settles any more — no filament change running,
# no unit checking or stopping its drying, the state the command leads to
# reached (EXPECT) — but at least 10 s (the printer starts acting a second or
# two after the command) and at most 3 minutes (AMS) / 30 s (other commands).

FAST_REFRESH_INTERVAL = 2.0
FAST_REFRESH_MIN = 10.0
FAST_REFRESH_MAX = 180.0
FAST_REFRESH_CMD_MAX = 30.0
_TRANSIENT_DRY = ("checking", "stopping")
_fast_refresh_lock = threading.Lock()
_fast_refresh_until: Dict[str, tuple] = {}   # printer_id -> (deadline, keep going until, expect)

# The state a printer reaches after the command: refreshed until then (or the deadline).
EXPECT = {
    "pause": lambda s: s.state == PrinterState.PAUSED,
    "resume": lambda s: s.state == PrinterState.PRINTING,
    "stop": lambda s: s.state not in (PrinterState.PRINTING, PrinterState.PAUSED),
    "light_on": lambda s: s.light_on is True,
    "light_off": lambda s: s.light_on is False,
}


def _settling(view: _LiveView) -> bool:
    if view.status.filament_change is not None or view.raw.get("busy"):
        return True
    return any((u.get("dry") or {}).get("status") in _TRANSIENT_DRY for u in view.units)


def _fast_refresh_once(printer_id: str, prn: Dict[str, Any], expect=None) -> Optional[bool]:
    """Upsert the printer's live status (online only: an empty WS snapshot never
    overwrites the poll loop's grace handling). True while something settles
    (an AMS change / a transient drying state on Bambu, or `expect` not met
    yet), False when settled, None without live telemetry (nothing upserted)."""
    if prn.get("kind") == "bambu":
        view = _live_view(printer_id, prn)
        status = view.status if view is not None else None
        settling = _settling(view) if view is not None else False
    else:
        status, settling = live_status(printer_id, prn), False
    store = _store
    if status is None or store is None:
        return None
    store.upsert(status)
    return settling or (expect is not None and not expect(status))


def _refresh_now(printer_id: str, prn: Dict[str, Any]) -> None:
    """One live refresh in the caller's thread, for a confirmed light: its mode
    is in the live telemetry already (the lib's lights, the Klipper client's
    data), so /api/printers carries it from the reply on. The refresh loop's
    first pass comes 2 s later, and a page poll in between would undo the
    card's own update. A failure here never turns the reply into an error."""
    if _store is None or not hasattr(_store, "upsert"):
        return
    try:
        _fast_refresh_once(printer_id, prn)
    except Exception:
        logger.warning("[commands] %s refresh after the command failed", printer_id, exc_info=True)


def _fast_refresh_loop(printer_id: str, prn: Dict[str, Any]) -> None:
    try:
        while True:
            _sleep(FAST_REFRESH_INTERVAL)
            with _fast_refresh_lock:
                expect = _fast_refresh_until.get(printer_id, (0.0, 0.0, None))[2]
            settling = _fast_refresh_once(printer_id, prn, expect)
            with _fast_refresh_lock:
                deadline, keep_until, _expect = _fast_refresh_until.get(printer_id, (0.0, 0.0, None))
                now = _monotonic()
                if settling is None or now >= deadline or (not settling and now >= keep_until):
                    _fast_refresh_until.pop(printer_id, None)
                    return
    except Exception:
        logger.error("[commands] %s live refresh failed", printer_id, exc_info=True)
        with _fast_refresh_lock:
            _fast_refresh_until.pop(printer_id, None)


def _start_fast_refresh(printer_id: str, prn: Dict[str, Any], expect=None,
                        max_s: float = FAST_REFRESH_MAX) -> None:
    """Start the live refresh, or widen a running one: deadline = max(old,
    now + max_s), keep_until = max(old, now + FAST_REFRESH_MIN), expect = the
    newest one given."""
    if _store is None or not hasattr(_store, "upsert"):
        return
    now = _monotonic()
    with _fast_refresh_lock:
        old = _fast_refresh_until.get(printer_id)
        if old is None:
            _fast_refresh_until[printer_id] = (now + max_s, now + FAST_REFRESH_MIN, expect)
        else:
            _fast_refresh_until[printer_id] = (max(old[0], now + max_s), max(old[1], now + FAST_REFRESH_MIN),
                                               expect if expect is not None else old[2])
            return
    threading.Thread(target=_fast_refresh_loop, args=(printer_id, prn), daemon=True,
                     name="cmd-refresh-%s" % printer_id).start()


def _send_ams(printer_id: str, prn: Dict[str, Any], action: str, params: Dict[str, Any]) -> Dict[str, Any]:
    view = _live_view(printer_id, prn)
    if view is None:
        return _refused(MSG_NO_TELEMETRY)
    mark = None
    if action in AMS_ACTIONS:
        plan = _plan_filament_change(printer_id, action, params, view, prn.get("model"))
        if not isinstance(plan, str):
            mark = _claim_inflight(printer_id, view.raw)
            if mark is None:
                plan = MSG_INFLIGHT
    else:
        plan = _plan_drying(action, params, view)
    if isinstance(plan, str):
        return _refused(plan)
    if action == "dry_start":
        # before the publish (and kept when it fails): the probe's stop must never
        # land between this start and the AMS's first "checking" report
        _dry_started[printer_id] = _monotonic()
    r: Dict[str, Any] = {}
    try:
        r = _send_local(printer_id, plan)
    finally:
        if mark is not None and not r.get("success"):
            _release_inflight(printer_id, mark)
    _note_reply(printer_id, plan, r)
    if r["success"]:
        _start_fast_refresh(printer_id, prn)
    return _outcome(r)


# --- capabilities: what a printer can do at all (independent of its state) -----------

def get_store():
    return _store


def _light_enabled() -> frozenset:
    """Printer ids whose not-yet-verified light control (Creality lightSw) the
    owner enabled after a live check (setting printer_light_enabled)."""
    try:
        from app.services import settings_service
        v = settings_service.get("printer_light_enabled") or []
        return frozenset(x for x in v if isinstance(x, str)) if isinstance(v, list) else frozenset()
    except Exception:
        logger.debug("printer_light_enabled read failed", exc_info=True)
        return frozenset()


def caps_for(d: Dict[str, Any]) -> Tuple[Dict[str, Optional[bool]], Optional[str]]:
    """(caps, caps_note) for a PrinterStatus.to_dict() row. caps: CAP_KEYS ->
    True (a working command channel and the rights for it, whatever the print
    state), False (known impossible) or None (unknown). caps_note: why print
    control is not True, else None. Never reads d["debug"] (printing-only)."""
    kind, pid, online = d.get("kind"), d.get("id"), d.get("online")
    caps: Dict[str, Optional[bool]] = dict.fromkeys(CAP_KEYS, False)
    note = None
    if kind == "bambu":
        pc = get_capability(pid)                 # print-class verdict (fun / probe / persisted)
        caps["light"] = True                     # ledctrl is system class: no signature needed
        for k in ("pause", "resume", "stop", "cooldown", "eject", "tune", "ams"):
            caps[k] = pc
        note = None if pc is True else (NOTE_BLOCKED if pc is False else NOTE_UNKNOWN)
    elif kind == "creality":
        caps.update(pause=True, resume=True, stop=True)
        # the WS lightSw set is not live-verified: unknown until enabled after a live check
        caps["light"] = True if pid in _light_enabled() else None
    elif kind == "klipper":
        ready = d.get("ctl_ready") if online else None       # True / False / None (no host state yet)
        caps.update(pause=ready, resume=ready, stop=ready)
        if ready is True:
            caps["light"] = d.get("light_ctl")               # True pin / False none / None not looked up yet
        else:
            caps["light"] = None if ready is None else False
        note = NOTE_KLIPPY if ready is False else None
    else:                                        # mks and anything unknown
        note = NOTE_NOT_WIRED
    return caps, note


def _caps_now(printer_id: str, kind: Optional[str]):
    st = _store.get_one(printer_id) if _store is not None and hasattr(_store, "get_one") else None
    d = st.to_dict() if st is not None else {"id": printer_id, "kind": kind, "online": None}
    return caps_for(d)


def _cap_refusal(printer_id: str, kind: Optional[str], action: str) -> Optional[str]:
    """Why the stored capabilities forbid `action`, or None. Bambu print-class
    is left to the printer (its reply settles the verdict: do not block the
    learning); an unknown capability is left to the channel."""
    cap = ACTION_CAP.get(action)
    if cap is None or kind == "bambu":
        return None
    caps, note = _caps_now(printer_id, kind)
    if caps.get(cap) is True:
        return None
    if cap == "light":
        if kind == "creality" and caps.get("light") is None:
            return MSG_LIGHT_UNVERIFIED                      # not in printer_light_enabled
        if caps.get("light") is False:
            # a Klipper that is not ready says so; otherwise there is no light pin
            return (note[:1].upper() + note[1:]) if note else MSG_NO_LIGHT
        return None                                         # Klipper pin not looked up yet: the client answers
    if caps.get(cap) is None:
        return None
    return (note[:1].upper() + note[1:]) if note else MSG_NOT_SUPPORTED


# --- live state: one reader for every kind ---------------------------------------------

def live_status(printer_id: str, prn: Optional[Dict[str, Any]] = None) -> Optional[PrinterStatus]:
    """Fresh normalized status from the collector's live client (any kind), or
    None (offline / no client). Public: the auto-dry engine uses it too."""
    if prn is None:
        from app.services import printer_registry
        prn = printer_registry.get_printer(printer_id) or {}
    kind = prn.get("kind")
    if kind == "bambu":
        view = _live_view(printer_id, prn)
        return view.status if view is not None else None
    collector = _store.get_collector(printer_id) if _store is not None else None
    get_data = getattr(getattr(collector, "client", None), "get_data", None)
    if not callable(get_data) or kind not in ("creality", "klipper"):
        return None
    stored = _store.get_one(printer_id) if hasattr(_store, "get_one") else None
    try:
        st = normalize_ws_dict(printer_id, prn.get("label") or printer_id, PrinterKind(kind),
                               get_data() or {},
                               device_type=(stored.device_type if stored is not None else None) or prn.get("model"))
    except Exception:
        logger.error("[commands] %s live status failed", printer_id, exc_info=True)
        return None
    return st if st.online else None


def _state_check(printer_id: str, prn: Dict[str, Any], action: str) -> Optional[str]:
    """Why the printer's live state forbids `action`, or None. Cooldown / eject
    (Bambu only) fail closed on the raw gcode_state; print control is left to
    the printer when there is no live state."""
    if action in ("cooldown", "eject"):
        view = _live_view(printer_id, prn)
        if view is None:
            return MSG_NO_TELEMETRY
        if view.status.filament_change is not None or view.raw.get("busy"):
            return MSG_CHANGE_RUNNING
        return state_refusal(action, view.status.state.value, view.gcode_state)
    if action in PRINT_CONTROL:
        st = live_status(printer_id, prn)
        return state_refusal(action, st.state.value) if st is not None else None
    return None


_eject_marks: Dict[str, float] = {}      # printer_id -> monotonic of the last eject claim; under _inflight_lock


def _claim_eject(printer_id: str) -> Optional[float]:
    """Take the eject mark (check and set in one step): the mark, or None when
    an eject was claimed less than EJECT_GUARD_SECONDS ago."""
    with _inflight_lock:
        last = _eject_marks.get(printer_id)
        if last is not None and _monotonic() - last < EJECT_GUARD_SECONDS:
            return None
        mark = _monotonic()
        _eject_marks[printer_id] = mark
        return mark


def _release_eject(printer_id: str, mark: float) -> None:
    """Give the mark back after a failed send (unless a later claim replaced it)."""
    with _inflight_lock:
        if _eject_marks.get(printer_id) == mark:
            del _eject_marks[printer_id]


def send(printer_id: str, action: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Send one command; returns {"success", "status", "detail", "replied"} (see _outcome)."""
    from app.services import printer_registry

    params = params or {}
    prn = printer_registry.get_printer(printer_id)
    kind = (prn or {}).get("kind")
    if prn is None or kind not in SUPPORTED_ACTIONS:
        return _refused(MSG_KIND)
    allowed = SUPPORTED_ACTIONS[kind]
    if allowed is not None and action not in allowed:
        return _refused("%s не принимает команду «%s»" % (KIND_TITLES.get(kind, kind), action))
    model = bambu_model(printer_id, prn) if kind == "bambu" else ""
    if action == "chamber" and not has_chamber_heater(model):
        return _refused("Активный подогрев камеры есть только у H2-серии")
    refusal = _cap_refusal(printer_id, kind, action)
    if refusal:
        return _refused(refusal)
    if kind == "bambu" and (action in AMS_ACTIONS or action in DRY_ACTIONS):
        return _send_ams(printer_id, prn, action, params)
    payload = None
    if kind == "bambu" and action not in LIGHT_ACTIONS:
        try:
            payload = _build_payload(action, params, model)     # parameter errors first, no I/O
        except ValueError as e:
            return _refused(str(e))
    refusal = _state_check(printer_id, prn, action)
    if refusal:
        return _refused(refusal)
    mark = None
    if action == "eject":
        mark = _claim_eject(printer_id)
        if mark is None:
            return _refused(MSG_EJECT_INFLIGHT)
    r: Dict[str, Any] = {}
    try:
        if kind == "creality":
            r = _send_creality(printer_id, action)
        elif kind == "klipper":
            r = _send_klipper(printer_id, action)
        elif action in LIGHT_ACTIONS:
            r = _send_bambu_light(printer_id, prn, action == "light_on")
        else:
            r = _send_local(printer_id, payload)
            _note_reply(printer_id, payload, r)
    finally:
        if mark is not None and not r.get("success"):
            _release_eject(printer_id, mark)                 # a failed send gives the mark back
    out = _outcome(r)
    if out["success"]:
        if out["replied"] and action in LIGHT_ACTIONS:
            _refresh_now(printer_id, prn)
        _start_fast_refresh(printer_id, prn, EXPECT.get(action), FAST_REFRESH_CMD_MAX)
    return out


def probe(printer_id: str) -> Optional[bool]:
    """Зонд print-класса: стоп сушки AMS 0 безвреден (сушка не идёт — гарантирует вызывающий).

    Любой ответ принтера (успех или прикладная ошибка) значит, что подпись
    прошла; "verify failed" — блокировка. Молчание — None (не знаем).
    Never publishes within PROBE_DRY_QUIET_S after any drying start attempt.
    """
    from app.services import printer_registry

    prn = printer_registry.get_printer(printer_id)
    if prn is None or prn.get("kind") != "bambu":
        return None
    payload = build_dry_stop()
    r = _send_local(printer_id, payload, guard=lambda: _dry_quiet(printer_id))
    if r.get("skipped"):
        return None                                   # nothing was sent: nothing learned
    if r.get("replied"):
        _note_reply(printer_id, payload, r)
        return not r.get("verify_failed")
    _note_capability(printer_id, None, None)
    return None


def _drying_now(view: _LiveView) -> bool:
    return any((u.get("dry") or {}).get("active") or (u.get("dry_time") or 0) > 0 for u in view.units)


def _probe_cycle(store) -> None:
    """One probe pass over the online Bambu printers that do not report
    print.fun (P1 series), or whose fun a real reply contradicted. A printer
    with any AMS drying is skipped, judged on its live telemetry (a drying
    started on the printer's screen a moment ago is not in the stored status
    yet): the probe's stop must never cut a real drying short."""
    from app.services import printer_registry

    for st in store.get_all():
        d = st.to_dict()
        if d.get("kind") != "bambu" or not d.get("online") or _dry_quiet(d["id"]):
            continue
        view = _live_view(d["id"], printer_registry.get_printer(d["id"]) or {})
        if view is None or _drying_now(view):
            continue
        # No full report yet (just connected, e.g. right after a restart): whether an
        # AMS is drying is unknown, and the probe's stop would cut a drying short.
        # 13.10.2026: six drying runs died this way at Pi brownout restarts.
        raw = view.raw or {}
        if not (raw.get("fun_seen") or raw.get("home_flag_seen")):
            continue
        if view.status.signature_required is not None and d["id"] not in _passive_distrust:
            continue   # print.fun answers it (note_signature, from the poll loop)
        probe(d["id"])


def _probe_loop(store) -> None:
    time.sleep(180)  # дать коллекторам подключиться и получить полный отчёт после старта
    while True:
        try:
            _probe_cycle(store)
        except Exception:
            logger.error("capability probe failed", exc_info=True)
        time.sleep(PROBE_INTERVAL)


def start_probe_loop(store) -> None:
    load_persisted_capabilities()  # вердикты прошлого запуска — сразу, до первого зонда
    threading.Thread(target=_probe_loop, args=(store,), daemon=True,
                     name="cmd-probe").start()
