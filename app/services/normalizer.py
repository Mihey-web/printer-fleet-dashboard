import copy
import logging
from typing import Any, Dict, Optional

from app.domain.models import PrinterKind, PrinterStatus, PrinterState, now_ts
from app.services import ams_drying, bambu_extras
from app.services.utils import map_state, as_float, as_int, safe_job_name
# Hard import: a broken vendored tree must fail loudly, not silently drop the fans.
from app.vendor.pybambu.const import FansEnum, Features
from app.vendor.pybambu.utils import get_HMS_severity


logger = logging.getLogger(__name__)

# Sanity range for a chamber reading (°C). device.ctc.info.temp is packed
# (target << 16) | current, and the vendored lib unpacks it without a range check.
_CHAMBER_MAX = 200
# The vendored lib marks a loaded spool with no material assigned as "?"
# (const.UNKNOWN_TRAY_LABEL).
_UNKNOWN_TRAY = "?"
# pybambu 1.0.1 linked every HMS code to its own wiki page; the vendored lib
# falls back to the wiki's HMS home page for codes missing from its link table.
_HMS_WIKI_HOME = "/hms/home"
_HMS_WIKI_PAGE = "https://wiki.bambulab.com/en/x1/troubleshooting/hmscode/"
# Bambu's own tables have an empty text for some HMS codes (ru and en alike: 90
# codes, 11 of them fatal and 67 serious), and the lib drops those from
# hms.errors. These severities are still shown, as "unknown" with the code's
# wiki page, the way pybambu 1.0.1 showed every code it had no text for.
_SILENT_HMS_SHOWN = ("fatal", "serious")


def kind_debug_enabled(state: PrinterState) -> bool:
    return state == PrinterState.PRINTING


def _read(obj: Any, name: str) -> Any:
    """getattr for the vendored lib's index-based properties.

    active_nozzle_* / active_*_index look the active extruder up in a dict
    keyed 0/1/15 and raise KeyError for any other index; getattr's default only
    swallows AttributeError.
    """
    try:
        return getattr(obj, name, None)
    except (KeyError, IndexError, TypeError):
        return None


def _in_chamber_range(value: Optional[float]) -> bool:
    return value is not None and 0 < value < _CHAMBER_MAX


# Models with a chamber temperature sensor (the lib's Features.CHAMBER_TEMPERATURE:
# H2 / P2 / X1 / X1E / X2), by model name prefix.
_CHAMBER_SENSOR_PREFIXES = ("H2", "P2", "X1", "X2")


def _has_chamber_sensor(device: Any, device_type: Optional[str]) -> bool:
    """The lib's Features.CHAMBER_TEMPERATURE (a model-only flag); while the lib
    cannot answer (firmware version unknown) the same model-family test on the
    live device's model, else the registry's."""
    flag = bambu_extras.feature(device, Features.CHAMBER_TEMPERATURE)
    if flag is not None:
        return flag
    model = _read(getattr(device, "info", None), "device_type") or device_type or ""
    return str(model).upper().startswith(_CHAMBER_SENSOR_PREFIXES)


def _hms_wiki(code: Any, wiki: Any) -> Optional[str]:
    """The lib's wiki link for an HMS code, or the code's own page when the lib
    only has the generic HMS home page."""
    wiki = str(wiki or "")
    if wiki and not wiki.rstrip("/").endswith(_HMS_WIKI_HOME):
        return wiki
    bare = str(code or "")
    if bare.startswith("HMS_"):
        bare = bare[len("HMS_"):]
    return f"{_HMS_WIKI_PAGE}{bare}" if bare else (wiki or None)


def _hms_raw_code(item: Any) -> tuple:
    """("HMS_xxxx_xxxx_xxxx_xxxx", severity) of a raw hms entry, named as the lib names it."""
    attr, code = int(item["attr"]), int(item["code"])
    if attr <= 0 or code <= 0:
        return "HMS_", get_HMS_severity(code)
    return f"HMS_{attr >> 16:04X}_{attr & 0xFFFF:04X}_{code >> 16:04X}_{code & 0xFFFF:04X}", get_HMS_severity(code)


def _with_silent_hms(entries: list, raw: Any) -> list:
    """Put the fatal / serious codes the lib dropped for lack of text back, in
    the printer's order. hms.errors keeps the raw order minus those codes, so
    walking the raw list (hms._hms_list) alongside it finds them."""
    if not isinstance(raw, list):
        return entries
    pending, merged = list(entries), []
    for item in raw:
        try:
            code, severity = _hms_raw_code(item)
        except (KeyError, TypeError, ValueError):
            continue
        if pending and pending[0][0] == code:
            merged.append(pending.pop(0))
        elif code != "HMS_" and severity in _SILENT_HMS_SHOWN:
            merged.append((code, "unknown", severity, None))
    # The MQTT thread replaces hms.errors and the raw list one after the other:
    # anything the raw list does not explain is kept as the lib reported it.
    return merged + pending


def _bambu_error_text(hms_obj: Any, print_error_code: Optional[int], device_type: Optional[str] = None) -> Optional[str]:
    """Turn Bambu's HMS list + print_error code into one readable line.

    The vendored pybambu's ``hms.errors`` is a flat dict keyed by a 1-based index:
    ``{"Count": n, "1-Code": "HMS_xxxx_xxxx_xxxx_xxxx", "1-Error": "<text>",
    "1-Wiki": url, "1-Severity": "fatal"}``. The text comes from Bambu's own
    tables in the client's language (ru, falling back to en); codes whose table
    text is empty are dropped by the lib, and the fatal / serious ones are put
    back from the raw list. Each entry renders as
    ``"<code>: <text> [<severity>]"``, and the print_error code comes last as
    ``"Ошибка печати XXXX_XXXX: <text>"`` (the code as the printer's screen
    shows it, the text from the same tables when they have one). Returns None
    when there is nothing wrong, so a healthy printer keeps last_error empty.
    """
    entries = []   # (code, text, severity, wiki) in the printer's order
    errors = getattr(hms_obj, "errors", None) or {}
    if isinstance(errors, dict):
        def _idx(key: str) -> int:
            try:
                return int(key.split("-", 1)[0])
            except (ValueError, IndexError):
                return 0

        for key in sorted((k for k in errors if str(k).endswith("-Error")), key=_idx):
            text = str(errors.get(key) or "").strip()
            if not text:
                continue
            prefix = str(key)[:-len("-Error")]
            entries.append((errors.get(f"{prefix}-Code"), text,
                            errors.get(f"{prefix}-Severity"), errors.get(f"{prefix}-Wiki")))

    messages = []
    for code, text, severity, wiki in _with_silent_hms(entries, getattr(hms_obj, "_hms_list", None)):
        line = f"{code}: {text}" if code else text
        msg = f"{line} [{severity}]" if severity else line
        # Codes without a text in Bambu's tables read "unknown". Append the
        # wiki link so the code is still actionable — the user can look the
        # exact fault up.
        if text.lower() == "unknown":
            wiki = _hms_wiki(code, wiki)
            if wiki:
                msg = f"{msg} (см. {wiki})"
        messages.append(msg)

    if print_error_code:  # non-zero raw code
        messages.append(bambu_extras.print_error_line(print_error_code, str(device_type or "")))

    return " | ".join(messages) if messages else None


def _snapshot_items(data: dict) -> list:
    """Sorted copy of ams.data: the MQTT thread inserts AMS units while we read
    (a resize mid-copy raises RuntimeError — just copy again)."""
    for _ in range(3):
        try:
            return sorted(list(data.items()), key=lambda kv: kv[0])
        except RuntimeError:
            continue
    return []


def _nozzle_range(tray: Any) -> Optional[list]:
    """[min, max] nozzle temperature of the spool's profile (the lib keeps the
    raw strings, "0"/0 when unknown); None unless both are known."""
    lo = as_int(getattr(tray, "nozzle_temp_min", None))
    hi = as_int(getattr(tray, "nozzle_temp_max", None))
    if lo is None or hi is None or lo <= 0 or hi <= 0:
        return None
    return [lo, hi]


# PrinterStatus.ams["tray_now"]: nothing loaded / an external spool (either of
# the H2D's two). Not the ams_id numbering of star / snow, where 255 is the
# (main) external spool.
TRAY_NONE = 255
TRAY_EXTERNAL = 254


def _tray_now(ams_obj: Any, units: list) -> Optional[int]:
    """Active slot in the 1.0.1 ``tray_now`` convention, from the lib's
    active_ams_index / active_tray_index.

    255 = nothing loaded, 254 = external spool; otherwise the cumulative slot
    index over the units shown (a classic AMS has 4 slots, an AMS HT 1) — the
    same numbering as app.js amsChip and /api/ams/current.
    """
    a = as_int(_read(ams_obj, "active_ams_index"))
    t = as_int(_read(ams_obj, "active_tray_index"))
    if a is None or t is None:
        return None
    if a == 255:   # snow 0xFFFF / legacy 255 = none; 0xFF00 / legacy 254 = external
        return TRAY_NONE if t in (3, 255) else TRAY_EXTERNAL
    if a == 254:   # second external spool (H2D vir_slot)
        return TRAY_EXTERNAL
    base = 0
    for unit in units:
        n = len(unit["slots"])
        if unit["index"] == a:
            return base + t if 0 <= t < n else None
        base += n
    return None


def _dry_info(entry: Dict[str, Any], unit: Any, raw: Dict[str, Any], feeding: bool) -> Optional[Dict[str, Any]]:
    """The unit's drying block (ams_drying.build_dry_info); never fails the status."""
    setting = (as_int(getattr(unit, "drying_temperature", None)),
               as_int(getattr(unit, "drying_duration", None)),
               getattr(unit, "drying_filament", None) or None)
    try:
        return ams_drying.build_dry_info(entry, getattr(unit, "model", None),
                                         (raw.get("ams") or {}).get(entry["index"]),
                                         raw.get("fun2"), feeding, setting)
    except Exception:
        logger.debug("AMS %s drying info failed", entry.get("index"), exc_info=True)
        return None


def _build_ams(ams_obj: Any, raw: Optional[Dict[str, Any]] = None, printing: bool = False) -> Optional[Dict[str, Any]]:
    """Compact AMS snapshot for PrinterStatus.ams.

    The vendored lib keeps AMS units in a dict keyed by the unit id (0-3, AMS HT
    128-135). Units without trays are dropped and "no real units" collapses to
    None — the frontend hides the chip on that. Emptiness is derived from the
    material type / colour rather than the tray's own flag. Colors arrive as
    RRGGBBAA; the alpha byte is stripped here so the frontend gets a CSS-ready
    RRGGBB. `raw` (RawReport snapshot) and `printing` feed each unit's drying
    block: an AMS 2 Pro / AMS HT that feeds a running print gets lower limits.
    """
    if not ams_obj:
        return None
    data = getattr(ams_obj, "data", None)
    if not isinstance(data, dict):
        return None
    raw = raw or {}
    feeding_unit = as_int(_read(ams_obj, "active_ams_index")) if printing else None
    units = []
    for unit_id, unit in _snapshot_items(data):
        trays = getattr(unit, "tray", None) or []
        if not trays:
            continue
        slots = []
        for tray in trays:
            color = getattr(tray, "color", None) or ""
            mat_type = getattr(tray, "type", None) or ""
            name = getattr(tray, "name", None)
            # "?" = spool loaded, material not set: the 1.0.1 meaning was an
            # empty type (empty only if the colour is empty too) and no name.
            if mat_type == _UNKNOWN_TRAY:
                mat_type = ""
            if name == _UNKNOWN_TRAY:
                name = None
            # An empty slot is signalled either by no colour + no material, or by
            # an explicit type marker ("Empty"). The marker slot may still carry a
            # leftover colour, so keying emptiness only on colour left it rendered
            # as a black-filament chip.
            is_empty_marker = mat_type.strip().lower() == "empty"
            empty = is_empty_marker or (color[:8] in ("", "00000000") and not mat_type)
            slots.append({
                "type": None if empty else (mat_type or None),
                "color": None if empty else color[:6],
                "name": None if empty else name,
                "remain_pct": as_int(getattr(tray, "remain", None)),
                "empty": empty,
                "nozzle_temp": None if empty else _nozzle_range(tray),
            })
        entry = {
            # The physical unit id (dict key). History is keyed by it, never by
            # the position in the filtered `units` list.
            "index": unit_id,
            "humidity": as_int(getattr(unit, "humidity_index", None)),
            # humidity = % from humidity_raw (AMS 2 Pro / HT), remaining_drying_time
            # = minutes left; the lib defaults both to 0 when never reported.
            "humidity_pct": as_int(getattr(unit, "humidity", None)) or None,
            "dry_time": as_int(getattr(unit, "remaining_drying_time", None)) or None,
            "temp": as_float(getattr(unit, "temperature", None)),
            "slots": slots,
        }
        entry["dry"] = _dry_info(entry, unit, raw, feeding_unit == unit_id)
        units.append(entry)
    if not units:
        return None
    return {"tray_now": _tray_now(ams_obj, units), "units": units}


def _nozzle(info_obj: Any) -> Optional[Dict[str, Any]]:
    """Diameter (mm) and type slug of the active nozzle, None when unknown."""
    diameter = as_float(_read(info_obj, "active_nozzle_diameter"))
    if diameter is None or diameter <= 0:
        return None
    return {"diameter": diameter, "type": _read(info_obj, "active_nozzle_type") or None}


def _raw_snapshot(result: Any) -> Dict[str, Any]:
    """RawReport snapshot of the report fields the lib drops ({} without one:
    test doubles, or a device built outside the collector)."""
    raw = getattr(result, "forge_raw", None)
    if raw is None:
        return {}
    try:
        return raw.snapshot()
    except Exception:
        logger.debug("raw report snapshot failed", exc_info=True)
        return {}


def normalize_bambu(printer_id: str, label: str, result: Any, device_type: Optional[str] = None) -> PrinterStatus:
    pj = getattr(result, "print_job", None) if result is not None else None
    if pj is None:
        reason = "fetch returned None" if result is None else "device object has no print_job (MQTT telemetry not received)"
        return PrinterStatus(id=printer_id, label=label, kind=PrinterKind.BAMBU, online=False, state=PrinterState.OFFLINE, last_update_ts=now_ts(), device_type=device_type, last_error=reason, grace_period_active=False, last_successful_fetch=0.0)
    raw = _raw_snapshot(result)

    # Bambu Studio names the gcode inside every .3mf "Metadata/plate_N.gcode" and
    # the printer reports that path in gcode_file, so it can't serve as a job name.
    # The human-readable name arrives in subtask_name; gcode_file stays as the
    # fallback for SD-card reprints, where subtask_name comes back empty.
    ext = {
        "filename": getattr(pj, "subtask_name", None) or getattr(pj, "gcode_file", None),
        "message": getattr(pj, "message", None),
    }
    raw_state = getattr(pj, "gcode_state", None)
    state = map_state(raw_state)

    temp_obj = getattr(result, "temperature", None)
    # The nozzle lives per extruder (H2D has two): read the active one.
    nozzle_temp = as_float(_read(temp_obj, "active_nozzle_temperature")) if temp_obj else None
    bed_temp = as_float(getattr(temp_obj, "bed_temp", None)) if temp_obj else None
    chamber_temp = as_float(getattr(temp_obj, "chamber_temp", None)) if temp_obj else None
    # Only models with an active chamber heater (H2 series) have a target; the lib
    # unpacks it from device.ctc. 0 = heater off -> None, so the card draws no
    # "→0°" arrow. A current value out of range means the whole packed reading is
    # garbage, so the target is dropped with it.
    target_chamber_temp = as_float(getattr(temp_obj, "target_chamber_temp", None)) if temp_obj else None
    if not _in_chamber_range(chamber_temp):
        chamber_temp = None
        target_chamber_temp = None
    if not _in_chamber_range(target_chamber_temp):
        target_chamber_temp = None
    if not _has_chamber_sensor(result, device_type):
        # P1P / P1S / A1 report a value although they have no chamber sensor
        # (the P1S a constant 5°): no reading, so the card shows no chamber chip.
        chamber_temp = target_chamber_temp = None
    target_nozzle_temp = as_float(_read(temp_obj, "active_nozzle_target_temperature")) if temp_obj else None
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
            # PART_COOLING is a FansEnum member; the Fans object has no such attribute.
            fan_speed_pct = as_int(fans_obj.get_fan_speed(FansEnum.PART_COOLING))
        except Exception as e:
            logger.debug("[%s] fan_speed read failed: %s", label, e)

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
        debug["hms_error_count"] = getattr(hms_obj, "error_count", None)

    # Build a human-readable error line so the dashboard can show *what* is wrong
    # instead of a bare red badge. The lib already resolves HMS codes to text in
    # hms.errors ("{i}-Code" -> "HMS_xxxx...", "{i}-Error" -> text, "{i}-Severity"
    # -> level); print_error is a raw code, looked up in the same tables here
    # (per model: the lib auto-detects it from get_version, the registry's until then).
    error_model = getattr(info_obj, "device_type", None) or device_type
    last_error = _bambu_error_text(hms_obj, as_int(getattr(pj, "print_error", None)), error_model)

    lights_obj = getattr(result, "lights", None)
    # "on"/"off"/"unknown" per lamp; the card needs a three-valued flag. H2/X2
    # have a second chamber lamp (one-lamp models keep chamber_light2 "unknown"):
    # either lamp lit = lit, as the lib's is_chamber_light_on.
    l1 = getattr(lights_obj, "chamber_light", None) if lights_obj else None
    l2 = getattr(lights_obj, "chamber_light2", None) if lights_obj else None
    if "on" in (l1, l2):
        light_on = True
    elif l1 == "off" and l2 in (None, "", "off", "unknown"):
        light_on = False
    else:
        light_on = None
    if lights_obj:
        debug["lights"] = {
            "chamber_light": l1,
            "chamber_light2": l2,
            "work_light": getattr(lights_obj, "work_light", None),
        }

    if info_obj:
        debug["nozzle_info"] = {
            "diameter": _read(info_obj, "active_nozzle_diameter"),
            "type": _read(info_obj, "active_nozzle_type"),   # e.g. "hardened_steel"
        }

    home_obj = getattr(result, "home_flag", None)
    if home_obj:
        debug["home_flags"] = {
            # X1 reports the door in home_flag, newer models in stat; the lib merges both.
            "door_open": getattr(info_obj, "door_open", None),
            "homed": getattr(home_obj, "homed", None),
            "sdcard": getattr(home_obj, "sdcard_status", None),   # normal / abnormal / missing
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
    # app/vendor/pybambu/utils.py). Convert to seconds for our unified status.
    raw_remaining_time = as_int(getattr(pj, "remaining_time", None))
    eta_seconds = raw_remaining_time * 60 if raw_remaining_time is not None else None

    # PrintJob has no ``print_time`` attribute; derive elapsed seconds from
    # start_time if it's available (the lib stamps it, UTC-aware, when it sees
    # the printer go from idle to busy; None after a restart mid-print).
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

    ams_obj = getattr(result, "ams", None)
    ams = _build_ams(ams_obj, raw, state in (PrinterState.PRINTING, PrinterState.PAUSED))
    # The loaded spool also matters without an AMS (254 = external spool, 255 = none).
    tray_now = ams["tray_now"] if ams else (_tray_now(ams_obj, []) if ams_obj is not None else None)

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
        target_chamber_temp=target_chamber_temp,
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
        ams=ams,
        fans=fans,
        fw_update=fw_update,
        light_on=light_on,
        filament_change=bambu_extras.filament_change(raw),
        filament_swap_ok=bambu_extras.feature(result, Features.AMS_SWITCH_COMMAND),
        filament_loaded=bambu_extras.filament_loaded(raw, tray_now),
        dual_nozzle=bambu_extras.dual_nozzle(result, raw),
        ams_dry_ok=ams_drying.remote_dry_supported(raw.get("fun2")),
        door_open=bambu_extras.door_open(result),
        sdcard=bambu_extras.sdcard(result, raw),
        nozzle=_nozzle(info_obj),
        signature_required=bambu_extras.signature_required(raw),
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
    light_on = data.get("light_on") if isinstance(data.get("light_on"), bool) else None
    # Klipper control fields, set in every state: the status carries `debug`
    # only while printing, and an idle or paused printer must stay controllable.
    ctl_ready = light_ctl = None
    if kind == PrinterKind.KLIPPER:
        host_state = debug.get("webhooks_state") if isinstance(debug, dict) else None
        ctl_ready = None if host_state is None else str(host_state).lower() == "ready"
        pin = data.get("light_pin")             # None = not looked up yet, "" = none found
        light_ctl = None if pin is None else bool(pin)
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
        light_on=light_on,
        ctl_ready=ctl_ready,
        light_ctl=light_ctl,
    )


