import copy
import logging
from typing import Any, Dict, Optional

from app.domain.models import PrinterKind, PrinterStatus, PrinterState, now_ts
from app.services.utils import map_state, as_float, as_int, safe_job_name

try:
    from pybambu.const import FansEnum
except Exception:  # pragma: no cover - pybambu absent only in exotic envs
    FansEnum = None


logger = logging.getLogger(__name__)


def kind_debug_enabled(state: PrinterState) -> bool:
    return state == PrinterState.PRINTING


def _bambu_error_text(hms_obj: Any, print_error_code: Optional[int]) -> Optional[str]:
    """Turn Bambu's HMS list + print_error code into one readable line.

    pybambu's ``hms.errors`` is a flat dict keyed by index:
    ``{"0-Error": "HMS_xxxx_yyyy: <text>", "0-Severity": "fatal", "0-Wiki": url, ...}``.
    We keep the resolved text (and severity tag) and append the raw print_error
    code in hex — the same form the printer's own screen shows. Returns None when
    there is nothing wrong, so a healthy printer keeps last_error empty.
    """
    messages = []
    errors = getattr(hms_obj, "errors", None) or {}
    if isinstance(errors, dict):
        def _idx(key: str) -> int:
            try:
                return int(key.split("-", 1)[0])
            except (ValueError, IndexError):
                return 0

        for key in sorted((k for k in errors if str(k).endswith("-Error")), key=_idx):
            text = errors.get(key)
            if not text:
                continue
            prefix = str(key)[:-len("-Error")]
            severity = errors.get(f"{prefix}-Severity")
            msg = f"{text} [{severity}]" if severity else str(text)
            # pybambu's bundled HMS table doesn't cover newer models (e.g. P2S),
            # so the code resolves to "unknown". Append the wiki link so the code
            # is still actionable — the user can look the exact fault up.
            resolved = str(text).split(":", 1)[-1].strip().lower()
            wiki = errors.get(f"{prefix}-Wiki")
            if resolved in ("", "unknown") and wiki:
                msg = f"{msg} (см. {wiki})"
            messages.append(msg)

    if print_error_code:  # non-zero raw code
        messages.append(f"Код ошибки печати: 0x{print_error_code:08X}")

    return " | ".join(messages) if messages else None


def _build_ams(ams_obj: Any) -> Optional[Dict[str, Any]]:
    """Compact AMS snapshot for PrinterStatus.ams.

    pybambu pre-creates 4 AMSInstance placeholders even with no AMS attached
    (X1C fleet: tray_now=254, tray=[]), so units without trays are dropped and
    "no real units" collapses to None — the frontend hides the chip on that.
    A tray's own ``empty`` flag is False even for empty slots (P2S, verified
    live), so emptiness is derived from color/type instead. Colors arrive as
    RRGGBBAA; the alpha byte is stripped here so the frontend gets a
    CSS-ready RRGGBB.
    """
    if not ams_obj:
        return None
    units = []
    for phys_idx, unit in enumerate(getattr(ams_obj, "data", None) or []):
        trays = getattr(unit, "tray", None) or []
        if not trays:
            continue
        slots = []
        for tray in trays:
            color = getattr(tray, "color", None) or ""
            mat_type = getattr(tray, "type", None) or ""
            # An empty slot is signalled either by no colour + no material, or by
            # an explicit type marker ("Empty" on X1C). The marker slot still
            # carries a leftover colour, so keying emptiness only on colour left it
            # rendered as a black-filament chip.
            is_empty_marker = mat_type.strip().lower() == "empty"
            empty = is_empty_marker or (color[:8] in ("", "00000000") and not mat_type)
            slots.append({
                "type": None if empty else (mat_type or None),
                "color": None if empty else color[:6],
                "name": None if empty else getattr(tray, "name", None),
                "remain_pct": as_int(getattr(tray, "remain", None)),
                "empty": empty,
            })
        units.append({
            # Stable physical unit position in ams_obj.data. History must be keyed
            # by this, not by the position in the filtered `units` list — dropping
            # tray-less placeholder units would otherwise shift indices and mix
            # different physical units' history under one unit_index.
            "index": phys_idx,
            "humidity": as_int(getattr(unit, "humidity_index", None)),
            # humidity_raw and dry_time are grafted onto AMSInstance by
            # bambu_collector's print_update wrapper (pybambu discards both).
            "humidity_pct": as_int(getattr(unit, "humidity_raw", None)),
            "dry_time": as_int(getattr(unit, "dry_time", None)),
            "temp": as_float(getattr(unit, "temperature", None)),
            "slots": slots,
        })
    if not units:
        return None
    return {"tray_now": as_int(getattr(ams_obj, "tray_now", None)), "units": units}


def normalize_bambu(printer_id: str, label: str, result: Any, device_type: Optional[str] = None) -> PrinterStatus:
    pj = getattr(result, "print_job", None) if result is not None else None
    if pj is None:
        reason = "fetch returned None" if result is None else "device object has no print_job (MQTT telemetry not received)"
        return PrinterStatus(id=printer_id, label=label, kind=PrinterKind.BAMBU, online=False, state=PrinterState.OFFLINE, last_update_ts=now_ts(), device_type=device_type, last_error=reason, grace_period_active=False, last_successful_fetch=0.0)

    ext = {
        "filename": getattr(pj, "gcode_file", None),
        "message": getattr(pj, "subtask_name", None) or getattr(pj, "message", None),
    }
    raw_state = getattr(pj, "gcode_state", None)
    state = map_state(raw_state)

    temp_obj = getattr(result, "temperature", None)
    nozzle_temp = as_float(getattr(temp_obj, "nozzle_temp", None)) if temp_obj else None
    bed_temp = as_float(getattr(temp_obj, "bed_temp", None)) if temp_obj else None
    chamber_temp = as_float(getattr(temp_obj, "chamber_temp", None)) if temp_obj else None
    if chamber_temp is not None and chamber_temp <= 0:
        chamber_temp = None
    target_nozzle_temp = as_float(getattr(temp_obj, "target_nozzle_temp", None)) if temp_obj else None
    target_bed_temp = as_float(getattr(temp_obj, "target_bed_temp", None)) if temp_obj else None

    info_obj = getattr(result, "info", None)
    wifi_signal = as_int(getattr(info_obj, "wifi_signal", None)) if info_obj else None
    firmware_version = getattr(info_obj, "sw_ver", None) if info_obj else None
    # new_version_state: 1 = update available, 2 = up to date, 0/absent = unknown
    _nvs = as_int(getattr(info_obj, "new_version_state", None)) if info_obj else None
    fw_update = None if _nvs in (None, 0) else _nvs == 1

    stage_obj = getattr(result, "stage", None)
    stage = getattr(stage_obj, "description", None) if stage_obj else None

    speed_obj = getattr(result, "speed", None)
    feedrate_pct = as_int(getattr(speed_obj, "modifier", None)) if speed_obj else None

    fans_obj = getattr(result, "fans", None)
    fan_speed_pct = None
    fans = None
    if fans_obj:
        try:
            fan_speed_pct = as_int(fans_obj.get_fan_speed(fans_obj.PART_COOLING))
        except Exception as e:
            logger.debug("[%s] fan_speed read failed: %s", label, e)
        if FansEnum is not None:
            def _fan(member):
                try:
                    return as_int(fans_obj.get_fan_speed(member))
                except Exception:
                    return None
            fans = {
                "aux": _fan(FansEnum.AUXILIARY),
                "chamber": _fan(FansEnum.CHAMBER),
                "heatbreak": _fan(FansEnum.HEATBREAK),
            }
            if all(v is None for v in fans.values()):
                fans = None



    # pybambu's PrintJob exposes the real progress on ``print_percentage`` (fed
    # from MQTT ``mc_percent``). The other names are kept as harmless fallbacks
    # in case the upstream library renames the attribute in a future release.
    progress_candidates = {
        "print_percentage": getattr(pj, "print_percentage", None),
        "percent": getattr(pj, "percent", None),
        "mc_percent": getattr(pj, "mc_percent", None),
        "progress": getattr(pj, "progress", None),
    }
    layer_candidates = {
        "layer_num": getattr(pj, "layer_num", None),
        "current_layer": getattr(pj, "current_layer", None),
        "mc_cur_layer": getattr(pj, "mc_cur_layer", None),
        "layer_num_raw": getattr(pj, "layer_num_raw", None),
    }
    total_layer_candidates = {
        "total_layer_num": getattr(pj, "total_layer_num", None),
        "total_layers": getattr(pj, "total_layers", None),
        "mc_total_layer": getattr(pj, "mc_total_layer", None),
        "total_layer_num_raw": getattr(pj, "total_layer_num_raw", None),
    }

    progress_pct = next((as_float(value) for value in progress_candidates.values() if as_float(value) is not None), None)
    current_layer = next((as_int(value) for value in layer_candidates.values() if as_int(value) is not None), None)
    total_layers = next((as_int(value) for value in total_layer_candidates.values() if as_int(value) is not None), None)

    if progress_pct is None and current_layer is not None and total_layers not in (None, 0):
        progress_pct = round((current_layer / total_layers) * 100, 2)

    debug = {
        "raw_state": raw_state,
        "progress_candidates": progress_candidates,
        "layer_candidates": layer_candidates,
        "total_layer_candidates": total_layer_candidates,
        "available_print_job_attrs": sorted(name for name in dir(pj) if not name.startswith("_"))[:160],
    }

    hms_obj = getattr(result, "hms", None)
    if hms_obj:
        debug["hms_errors"] = getattr(hms_obj, "errors", None)
        debug["hms_error_count"] = getattr(hms_obj, "count", None)

    # Build a human-readable error line so the dashboard can show *what* is wrong
    # instead of a bare red badge. pybambu already resolves HMS codes to text in
    # hms.errors ("{i}-Error" -> "HMS_xxxx: <text>", "{i}-Severity" -> level);
    # print_error is a raw code the printer shows in hex.
    last_error = _bambu_error_text(hms_obj, as_int(getattr(pj, "print_error", None)))

    lights_obj = getattr(result, "lights", None)
    # chamber_light: "on"/"off"/"unknown" — карточке нужен трёхзначный флаг
    _chamber_light = getattr(lights_obj, "chamber_light", None) if lights_obj else None
    light_on = {"on": True, "off": False}.get(_chamber_light)
    if lights_obj:
        debug["lights"] = {
            "chamber_light": getattr(lights_obj, "chamber_light", None),
            "work_light": getattr(lights_obj, "work_light", None),
        }

    if info_obj:
        debug["nozzle_info"] = {
            "diameter": getattr(info_obj, "nozzle_diameter", None),
            "type": getattr(info_obj, "nozzle_type", None),
        }

    home_obj = getattr(result, "home_flag", None)
    if home_obj:
        debug["home_flags"] = {
            "door_open": getattr(home_obj, "door_open", None),
            "homed": getattr(home_obj, "homed", None),
            "sdcard": getattr(home_obj, "sdcard_present", None),
            "is_220v": getattr(home_obj, "is_220V", None),
        }

    cam_obj = getattr(result, "camera", None)
    if cam_obj:
        debug["camera"] = {
            "recording": getattr(cam_obj, "recording", None),
            "timelapse": getattr(cam_obj, "timelapse", None),
        }

    if state == PrinterState.PRINTING and (progress_pct is None or current_layer is None or total_layers is None):
        logger.warning(
            "Bambu debug %s state=%s progress=%s layers=%s total=%s attrs=%s",
            label,
            raw_state,
            progress_pct,
            current_layer,
            total_layers,
            debug["available_print_job_attrs"],
        )

    # pybambu often returns gcode_state='unknown' for idle printers with zero temps.
    # If MQTT connected + device object exists → printer IS online, just hasn't reported yet.
    if state == PrinterState.UNKNOWN:
        state = PrinterState.IDLE

    # pybambu reports remaining_time in minutes (see get_end_time in
    # pybambu/utils.py). Convert to seconds for our unified status.
    raw_remaining_time = as_int(getattr(pj, "remaining_time", None))
    eta_seconds = raw_remaining_time * 60 if raw_remaining_time is not None else None

    # PrintJob has no ``print_time`` attribute; derive elapsed seconds from
    # start_time if it's available.
    print_time_seconds = None
    start_time = getattr(pj, "start_time", None)
    if start_time is not None:
        try:
            from datetime import datetime, timezone
            now = datetime.now(start_time.tzinfo) if getattr(start_time, "tzinfo", None) else datetime.now()
            elapsed = (now - start_time).total_seconds()
            if elapsed > 0:
                print_time_seconds = int(elapsed)
        except Exception as e:
            logger.debug("[%s] print_time_seconds calc failed: %s", label, e)
            print_time_seconds = None

    return PrinterStatus(
        id=printer_id,
        label=label,
        kind=PrinterKind.BAMBU,
        online=True,
        state=state,
        progress_pct=progress_pct,
        job_name=safe_job_name(ext),
        eta_seconds=eta_seconds,
        print_time_seconds=print_time_seconds,
        nozzle_temp=nozzle_temp,
        bed_temp=bed_temp,
        chamber_temp=chamber_temp,
        target_nozzle_temp=target_nozzle_temp,
        target_bed_temp=target_bed_temp,
        current_layer=current_layer,
        total_layers=total_layers,
        wifi_signal=wifi_signal,
        firmware_version=firmware_version,
        stage=stage,
        fan_speed_pct=fan_speed_pct,
        feedrate_pct=feedrate_pct,
        last_update_ts=now_ts(),
        device_type=device_type,
        grace_period_active=False,
        last_successful_fetch=now_ts(),
        last_error=last_error,
        debug=debug if kind_debug_enabled(state) else {},
        ams=_build_ams(getattr(result, "ams", None)),
        fans=fans,
        fw_update=fw_update,
        light_on=light_on,
    )



def normalize_ws_dict(printer_id: str, label: str, kind: PrinterKind, data: Dict[str, Any], device_type: Optional[str] = None) -> PrinterStatus:
    online = bool(data)
    if not online:
        return PrinterStatus(id=printer_id, label=label, kind=kind, online=False, state=PrinterState.OFFLINE, last_update_ts=now_ts(), device_type=device_type, last_error="No telemetry data received", grace_period_active=False, last_successful_fetch=0.0)

    progress_pct = as_float(data.get("progress"))
    eta_seconds = as_int(data.get("remaining_time"))
    print_time_seconds = as_int(data.get("print_time"))
    nozzle_temp = as_float(data.get("nozzle_temp"))
    bed_temp = as_float(data.get("bed_temp"))
    current_layer = as_int(data.get("current_layer"))
    total_layers = as_int(data.get("total_layers"))
    # Deep-copy: the client's get_data() returns a shallow copy, so the nested
    # "debug" dict/lists are still live references into the WS thread's state.
    # Copy them fully before we read/extend them here to avoid a read/write race.
    debug = copy.deepcopy(data.get("debug", {}))

    chamber_temp = as_float(data.get("chamber_temp"))
    if chamber_temp is not None and chamber_temp <= 0:
        chamber_temp = None
    target_nozzle_temp = as_float(data.get("target_nozzle_temp"))
    target_bed_temp = as_float(data.get("target_bed_temp"))
    fan_speed_pct = as_int(data.get("fan_speed_pct"))
    feedrate_pct = as_int(data.get("feedrate_pct"))
    stage = data.get("stage")
    firmware_version = data.get("firmware_version")

    if not safe_job_name(data):
        candidate_keys = [
            "filename",
            "file",
            "gcode_file",
            "gcode_file_name",
            "project_name",
            "message",
            "printInfo",
            "taskName",
            "jobName",
            "modelName",
            "printName",
        ]
        debug["job_name_candidates"] = {key: data.get(key) for key in candidate_keys if data.get(key)}

    data["debug"] = debug

    state = map_state(data.get("state"))

    # Detect a completed print from telemetry. Creality/Klipper firmware often keeps
    # reporting a stale "printing" (or an unmapped idle-ish) state after the job is
    # done, while progress sits at 100% and no time remains — and the nozzle is still
    # cooling so it stays hot. Treat that as FINISHED instead of PRINTING.
    looks_finished = False
    if kind in {PrinterKind.CREALITY, PrinterKind.KLIPPER}:
        progress_complete = progress_pct is not None and progress_pct >= 100
        layers_complete = (
            current_layer is not None
            and total_layers not in (None, 0)
            and current_layer >= total_layers
        )
        no_time_left = eta_seconds is None or eta_seconds <= 0
        # layers_complete only counts as "finished" evidence when progress isn't
        # reported at all. Otherwise a new print's first cycles — where firmware
        # still echoes the PREVIOUS job's completed layer count and hasn't set an
        # ETA yet — would flip the fresh job straight to FINISHED. A reported
        # progress (even 0-3%) proves the job isn't done.
        looks_finished = (progress_complete or (layers_complete and progress_pct is None)) and no_time_left
        # Creality K1/K1C/Ender Klipper firmware reports print_stats.state="paused"
        # once a job finishes (it auto-pauses at the end), which otherwise leaves the
        # card stuck on "Пауза" at 100%. A real mid-print pause keeps time remaining,
        # so no_time_left guards against misclassifying it as finished.
        if looks_finished and state in {
            PrinterState.PRINTING, PrinterState.IDLE, PrinterState.UNKNOWN, PrinterState.PAUSED,
        }:
            state = PrinterState.FINISHED
        debug[f"{kind.value}_looks_finished"] = looks_finished

    # Promote idle/unknown to PRINTING only when there's evidence of an *active* job.
    # A hot nozzle (cooling down) or stale print_time at 100% are NOT active signals,
    # so they're deliberately excluded to avoid resurrecting a finished print.
    if (
        kind in {PrinterKind.CREALITY, PrinterKind.KLIPPER}
        and state in {PrinterState.IDLE, PrinterState.UNKNOWN}
        and not looks_finished
    ):
        has_print_activity = any([
            (progress_pct is not None and 0 < progress_pct < 100),
            (eta_seconds is not None and eta_seconds > 0),
            (
                current_layer is not None and current_layer > 0
                and (total_layers in (None, 0) or current_layer < total_layers)
            ),
        ])
        debug[f"{kind.value}_detected_print_activity"] = has_print_activity
        data["debug"] = debug
        if has_print_activity:
            state = PrinterState.PRINTING

    # Creality K1/K1C/Ender firmware keeps state="printing" through a filament
    # runout — the only signal is materialStatus (0 = filament present). Demote
    # an active print to PAUSED so a runout doesn't masquerade as a healthy job.
    # Runs after looks_finished/promotion so it can't resurrect a finished print
    # and an idle printer with no filament loaded stays IDLE. The stage key
    # matches pybambu's, so the frontend shows "Пауза: закончился филамент".
    if kind == PrinterKind.CREALITY:
        material_status = as_int(data.get("material_status"))
        if material_status not in (None, 0) and state in {
            PrinterState.PRINTING, PrinterState.PAUSED,
        }:
            state = PrinterState.PAUSED
            stage = "paused_filament_runout"
            debug["creality_filament_runout"] = material_status

    return PrinterStatus(
        id=printer_id,
        label=label,
        kind=kind,
        online=True,
        state=state,
        progress_pct=progress_pct,
        job_name=safe_job_name(data),
        eta_seconds=eta_seconds,
        print_time_seconds=print_time_seconds,
        nozzle_temp=nozzle_temp,
        bed_temp=bed_temp,
        chamber_temp=chamber_temp,
        target_nozzle_temp=target_nozzle_temp,
        target_bed_temp=target_bed_temp,
        current_layer=current_layer,
        total_layers=total_layers,
        wifi_signal=None,
        firmware_version=firmware_version,
        stage=stage,
        fan_speed_pct=fan_speed_pct,
        feedrate_pct=feedrate_pct,
        last_update_ts=now_ts(),
        device_type=device_type or data.get("device_type"),
        grace_period_active=False,
        last_successful_fetch=now_ts(),
        debug=data.get("debug", {}) if kind_debug_enabled(state) else {},
    )


