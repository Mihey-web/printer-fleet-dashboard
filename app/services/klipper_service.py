"""«Сервис» of a Creality with Klipper's API (Moonraker :7125, the K2 Plus): calibrations and
a firmware restart from the card's ⋯ menu (admins, an idle printer only).

Each action runs one macro of the printer's own config, read live on 16.11.2026
(/printer/objects/query?configfile); none of them waits for the printer's screen:
* G29 — the K2's auto levelling: bed 50 °C, nozzle 140 °C, homing, NOZZLE_CLEAR (wipes the
  nozzle on the bed), Z_TILT_ADJUST, BED_MESH_CALIBRATE, CXSAVE_CONFIG, heaters off;
* NOZZLE_PID — part fan on, PID_CALIBRATE extruder at 230 °C, SAVE_CONFIG (Klipper restarts);
* BEDPID — PID_CALIBRATE bed at 100 °C, SAVE_CONFIG (Klipper restarts);
* INPUTSHAPER — homing, the head to the middle, Z 10, SHAPER_CALIBRATE, CXSAVE_CONFIG;
* BELT_TENSION — XY homing, the CFS head to its safe place, motors off, both belt modules
  measured and set (BELT_MDL_SET MDL_N=140);
* FIRMWARE_RESTART — Moonraker's /printer/firmware_restart.
Left out: AUTOTUNE_SHAPERS (only a variable, no G-code), NOZZLE_PID_HIGH (320 °C),
BED_MANUAL_CAL / Z_AXIS_CALIBRATION / PRINT_CALIBRATION / CALIBRATE_CUT_POS (the K2's own
screen flows and the cutter's motor parameters).

Moonraker answers /printer/gcode/script when the script has run: the call runs in a
thread, one per printer; status() gives its state and the printer's last console lines.
"""
import logging
import threading
import time
from typing import Any, Dict, Optional

from app.clients import moonraker

logger = logging.getLogger(__name__)

# action -> (script, title, what it does); None = the firmware restart (its own Moonraker call).
# The one list of the «Сервис» buttons: status() sends it to printer-service.js. The temperatures
# are the printer's own config (above), not written here: another firmware may heat otherwise.
ACTIONS: Dict[str, tuple] = {
    "svc_bed_mesh": ("G29", "Калибровка стола",
                     "автовыравнивание: нагрев стола и сопла, очистка сопла о стол, карта стола — 10–20 мин"),
    "svc_nozzle_pid": ("NOZZLE_PID", "PID сопла",
                       "нагрев сопла с обдувом по настройкам принтера; в конце Klipper сохранит настройку и перезапустится"),
    "svc_bed_pid": ("BEDPID", "PID стола",
                    "нагрев стола по настройкам принтера; в конце Klipper сохранит настройку и перезапустится"),
    "svc_shaper": ("INPUTSHAPER", "Калибровка резонансов", "голова в центр, замер вибраций по X и Y, сохранение"),
    "svc_belts": ("BELT_TENSION", "Натяжение ремней", "моторы отключатся, модули натяжения подтянут оба ремня"),
    "svc_fw_restart": (None, "Перезапуск прошивки", "Klipper и платы принтера перезапустятся, около минуты"),
}
SCRIPT_TIMEOUT_S = 45 * 60          # the longest of them (levelling, shaper) takes ~10-20 min
LINES = 4
IDLE_PRINT_STATES = ("standby", "complete", "cancelled", "error")

MSG_RUNNING = "Уже идёт «%s» — дождитесь конца"
MSG_NO_API = "Klipper API принтера не отвечает — повторите через минуту"
MSG_PRINTING = "Принтер не свободен: печатает или стоит на паузе"
MSG_EXECUTING = "Принтер сейчас выполняет команды — дождитесь, пока он освободится"
MSG_NOT_READY = "Klipper не готов"
MSG_STARTED = "запущено: %s"
MSG_DONE = "Готово"
MSG_FAILED = "Принтер ответил ошибкой"
MSG_LOST = "Связь с Klipper прервалась (после SAVE_CONFIG он перезапускается) — проверьте результат на принтере"

_lock = threading.Lock()
_jobs: Dict[str, Dict[str, Any]] = {}     # printer_id -> {"action", "title", "state", "started", "finished", "detail"}


def running(printer_id: str) -> Optional[Dict[str, Any]]:
    with _lock:
        job = _jobs.get(printer_id)
        return dict(job) if job is not None and job["state"] == "running" else None


def klipper_refusal(host: str) -> Optional[str]:
    """Why Klipper itself is not free (its own live state), or None. Fails closed."""
    try:
        status = moonraker.query(host, ["webhooks", "print_stats", "idle_timeout"])
    except moonraker.MoonrakerError:
        return MSG_NO_API
    if (status.get("webhooks") or {}).get("state") != "ready":
        return MSG_NOT_READY
    if (status.get("print_stats") or {}).get("state") not in IDLE_PRINT_STATES:
        return MSG_PRINTING
    if (status.get("idle_timeout") or {}).get("state") == "Printing":   # a G-code is running
        return MSG_EXECUTING
    return None


def start(printer_id: str, host: str, action: str) -> Dict[str, Any]:
    """Start the action in its thread: {"success", "detail"} (the raw form printer_commands' _outcome takes)."""
    script, title, _about = ACTIONS[action]
    with _lock:
        job = _jobs.get(printer_id)
        if job is not None and job["state"] == "running":
            return {"success": False, "detail": MSG_RUNNING % job["title"], "refused": True}
        _jobs[printer_id] = {"action": action, "title": title, "state": "running", "started": time.time(),
                             "finished": None, "detail": None}
    threading.Thread(target=_run, args=(printer_id, host, script), name="klipper-service", daemon=True).start()
    return {"success": True, "detail": MSG_STARTED % title}


def _run(printer_id: str, host: str, script: Optional[str]) -> None:
    state, detail = "done", MSG_DONE
    try:   # the console's newest line before the start, by the printer's own clock
        last = moonraker.gcode_store(host, 1)
        since = last[-1]["time"] if last and isinstance(last[-1].get("time"), (int, float)) else 0.0
    except moonraker.MoonrakerError:
        since = 0.0
    with _lock:
        if printer_id in _jobs:
            _jobs[printer_id]["since"] = since
    try:
        if script is None:
            moonraker.firmware_restart(host)
        else:
            moonraker.run_script(host, script, timeout=SCRIPT_TIMEOUT_S)
    except moonraker.MoonrakerError as exc:
        reason = str(exc)
        state = "failed"
        detail = MSG_FAILED if reason.startswith("HTTP") else MSG_LOST
        logger.info("[service] %s %s: %s", printer_id, script, reason)
    except Exception:
        state, detail = "failed", MSG_LOST
        logger.error("[service] %s %s failed", printer_id, script, exc_info=True)
    with _lock:
        job = _jobs.get(printer_id)
        if job is not None:
            job.update(state=state, detail=detail, finished=time.time())


def status(printer_id: str, host: Optional[str]) -> Dict[str, Any]:
    """The last job (or None) and the printer's last console lines since it started."""
    with _lock:
        job = dict(_jobs[printer_id]) if printer_id in _jobs else None
    lines = []
    if job is not None and host and job.get("since") is not None:
        try:
            lines = [x["message"][:200] for x in moonraker.gcode_store(host, 40)
                     if isinstance(x.get("time"), (int, float)) and x["time"] > job["since"]][-LINES:]
        except moonraker.MoonrakerError:
            lines = []
    if job is not None:
        job.pop("since", None)
    return {"job": job, "lines": lines,
            "actions": [{"key": k, "title": t, "about": about} for k, (_, t, about) in ACTIONS.items()]}
