"""Команды принтерам Bambu: свет, пауза/продолжить/стоп, скорость, обдув и т.п.

Канал — локальный MQTT через уже подключённый клиент коллектора (свежую
TLS-сессию X1C обрывает лимитом подключений, поэтому новый клиент не
создаётся). Ответ принтера ловится в report-топике (tee в bambu_collector):
новые прошивки принимают неподписанный print-класс только в Developer Mode,
иначе отвечают "mqtt message verify failed". Облачный канал выпилен: подпись
проверяется и там, так что облако ничего не давало (см. память
bambu-cloud-command-channel).

Капабилити: фоновый зонд (безвредный dry_stop) раз в 10 минут выясняет,
принимает ли принтер print-класс. Результат отдаётся в /api/printers как
print_cmds (True/False/None) — фронт прячет паузу/стоп/сушку там, где
прошивка их блокирует. Переключение принтера в Developer Mode
подхватывается автоматически следующим зондом.
"""
import json
import logging
import threading
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_store = None
_lock = threading.Lock()

REPLY_TIMEOUT = 8.0
PROBE_INTERVAL = 600

# Ответы на локальные команды: bambu_collector делает tee сырых сообщений
# report-топика сюда (pybambu их парсит только для телеметрии).
_seq_lock = threading.Lock()
_seq_counter = 50000
_local_awaited: Dict[str, Dict[str, Any]] = {}

# printer_id -> {"print_cmds": bool|None, "via": "cloud"|"local"|None, "ts": float}
_cap_lock = threading.Lock()
_capability: Dict[str, Dict[str, Any]] = {}


def set_store(store) -> None:
    """StateStore передаётся из main.py — отсюда берутся коллекторы."""
    global _store
    _store = store


def build_light(on: bool) -> Dict[str, Any]:
    return {"system": {"sequence_id": "0", "command": "ledctrl",
                       "led_node": "chamber_light",
                       "led_mode": "on" if on else "off",
                       "led_on_time": 500, "led_off_time": 500,
                       "loop_times": 0, "interval_time": 0}}


def build_print_action(action: str) -> Dict[str, Any]:
    return {"print": {"sequence_id": "0", "command": action, "param": ""}}


def build_dry_stop() -> Dict[str, Any]:
    return {"print": {"sequence_id": "0", "command": "ams_filament_drying",
                      "ams_id": 0, "cooling_temp": 40, "duration": 0,
                      "humidity": 0, "mode": 0, "rotate_tray": False,
                      "temp": 0}}


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


def build_cooldown() -> Dict[str, Any]:
    """Вырубить весь нагрев и вентиляторы."""
    return build_gcode(["M104 S0", "M140 S0",
                        "M106 P1 S0", "M106 P2 S0", "M106 P3 S0"])


def build_eject() -> Dict[str, Any]:
    """Отвести стол вниз на 30 мм относительным ходом — без хоуминга, безопасно на простое."""
    return build_gcode(["M17", "G91", "G1 Z30 F900", "G90", "M400"])


def build_skip_objects(obj_list) -> Dict[str, Any]:
    return {"print": {"sequence_id": "0", "command": "skip_objects",
                      "obj_list": [int(x) for x in obj_list]}}


def build_ams_load(slot: int) -> Dict[str, Any]:
    """Загрузить/сменить филамент из слота AMS (target = индекс лотка)."""
    return {"print": {"sequence_id": "0", "command": "ams_change_filament",
                      "target": slot, "curr_temp": 220, "tar_temp": 220}}


def build_ams_unload() -> Dict[str, Any]:
    """Выгрузить филамент в AMS (target 255 — маркер выгрузки)."""
    return {"print": {"sequence_id": "0", "command": "ams_change_filament",
                      "target": 255, "curr_temp": 220, "tar_temp": 220}}


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


def _build_payload(action: str, params: Dict[str, Any]) -> Dict[str, Any]:
    if action == "light_on":
        return build_light(True)
    if action == "light_off":
        return build_light(False)
    if action in ("pause", "resume", "stop"):
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
    if action == "cooldown":
        return build_cooldown()
    if action == "eject":
        return build_eject()
    if action == "skip_objects":
        objs = params.get("obj_list")
        if not isinstance(objs, list) or not objs or len(objs) > 64:
            raise ValueError("Список объектов: 1-64 id")
        if not all(isinstance(x, int) and not isinstance(x, bool) and x >= 0 for x in objs):
            raise ValueError("id объектов — целые неотрицательные числа")
        return build_skip_objects(objs)
    if action == "ams_load":
        return build_ams_load(_req_int(params, "slot", 0, 15, "Слот AMS"))
    if action == "ams_unload":
        return build_ams_unload()
    raise ValueError(f"Неизвестная команда: {action}")


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
                waiter["event"].set()


def _send_local(printer_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    collector = _store.get_collector(printer_id) if _store is not None else None
    client = getattr(collector, "client", None)
    if client is None:
        return {"success": False, "detail": "коллектор принтера не найден", "replied": False}
    seq = _next_seq()
    sendable = json.loads(json.dumps(payload))
    for cls in ("system", "print"):
        if cls in sendable:
            sendable[cls]["sequence_id"] = seq
    waiter = {"event": threading.Event(), "result": None, "reason": None}
    with _seq_lock:
        _local_awaited[seq] = waiter
    try:
        with _lock:
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
        detail = "🔒 прошивка блокирует сторонние команды" if verify else "принтер отклонил: %s" % reason
        return {"success": False, "detail": detail, "replied": True, "verify_failed": verify}
    return {"success": True,
            "detail": "подтверждено принтером" if got else "отправлено (без подтверждения)",
            "replied": got}


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


def get_capability(printer_id: str) -> Optional[bool]:
    """True — print-класс принимается; False — прошивка блокирует; None — не знаем."""
    with _cap_lock:
        return (_capability.get(printer_id) or {}).get("print_cmds")


def send(printer_id: str, action: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Отправить команду принтеру локально; ответ принтера — вердикт."""
    from app.services import printer_registry

    prn = printer_registry.get_printer(printer_id)
    if prn is None or prn.get("kind") != "bambu":
        return {"success": False, "detail": "Команды поддерживаются только для Bambu-принтеров"}
    try:
        payload = _build_payload(action, params or {})
    except ValueError as e:
        return {"success": False, "detail": str(e)}

    r = _send_local(printer_id, payload)
    if r.get("replied") and "print" in payload:
        _note_capability(printer_id, not r.get("verify_failed"), "local")
    return {"success": r["success"], "detail": r["detail"]}


def probe(printer_id: str) -> Optional[bool]:
    """Зонд print-класса: dry_stop безвреден (сушка не идёт — гарантирует вызывающий).

    Любой ответ принтера (успех или прикладная ошибка) значит, что подпись
    прошла; "verify failed" — блокировка. Молчание — None (не знаем).
    """
    from app.services import printer_registry

    prn = printer_registry.get_printer(printer_id)
    if prn is None or prn.get("kind") != "bambu":
        return None
    r = _send_local(printer_id, build_dry_stop())
    if r.get("replied"):
        ok = not r.get("verify_failed")
        _note_capability(printer_id, ok, "local")
        return ok
    _note_capability(printer_id, None, None)
    return None


def _probe_loop(store) -> None:
    time.sleep(60)  # дать коллекторам подключиться после старта
    while True:
        try:
            for st in store.get_all():
                d = st.to_dict()
                if d.get("kind") != "bambu" or not d.get("online"):
                    continue
                units = (d.get("ams") or {}).get("units") or []
                if units and (units[0].get("dry_time") or 0) > 0:
                    continue  # зонд dry_stop прервал бы реальную сушку
                probe(d["id"])
        except Exception:
            logger.error("capability probe failed", exc_info=True)
        time.sleep(PROBE_INTERVAL)


def start_probe_loop(store) -> None:
    load_persisted_capabilities()  # вердикты прошлого запуска — сразу, до первого зонда
    threading.Thread(target=_probe_loop, args=(store,), daemon=True,
                     name="cmd-probe").start()
