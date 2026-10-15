"""AMS drying: status, settings, limits and presets of an AMS 2 Pro / AMS HT.

Pure functions over the normalized AMS unit plus the raw report fields the
vendored pybambu drops (app/collectors/bambu_raw.py). Protocol facts and the
material values follow Bambu Studio (device code and BBL filament profiles);
nothing is copied from it.

Only AMS 2 Pro (Bambu's internal name "n3f") and AMS HT ("n3s") can dry.
"""
from typing import Any, Dict, List, Optional, Tuple

TEMP_MIN = 45
TYPE_MAX = {"n3f": 65, "n3s": 85}
HOURS_MIN, HOURS_MAX = 1, 24

# Per material: drying temperature (idle, while this AMS feeds a print) and
# hours, each as (n3f, n3s); heat-distortion and softening temperatures (°C).
# Table order is the preset order shown to the user.
MATERIALS: Dict[str, Dict[str, Any]] = {
    "PLA": {"idle": (45, 45), "print": (45, 45), "hours": (12, 12), "hdt": 45, "soft": 50},
    "PETG": {"idle": (65, 65), "print": (55, 55), "hours": (12, 12), "hdt": 75, "soft": 60},
    "ABS": {"idle": (65, 80), "print": (65, 75), "hours": (12, 8), "hdt": 90, "soft": 80},
    "ASA": {"idle": (65, 80), "print": (65, 80), "hours": (12, 8), "hdt": 100, "soft": 85},
    "PA": {"idle": (65, 85), "print": (65, 85), "hours": (12, 12), "hdt": 165, "soft": 150},
    "PC": {"idle": (65, 80), "print": (65, 80), "hours": (12, 8), "hdt": 105, "soft": 90},
    "TPU": {"idle": (65, 75), "print": (45, 45), "hours": (12, 18), "hdt": 45, "soft": 50},
    "PVA": {"idle": (65, 85), "print": (65, 70), "hours": (12, 18), "hdt": 75, "soft": 75},
    "PPS": {"idle": (65, 85), "print": (65, 85), "hours": (12, 12), "hdt": 165, "soft": 150},
}
# A loaded spool whose material is unknown is treated as PLA (the most
# heat-sensitive common material), as Bambu Studio does.
FALLBACK_MATERIAL = "PLA"
# Longest prefix first: PETG before PET, PCTG before PC, PPS / PPA before PA.
_PREFIXES = (("PETG", "PETG"), ("PCTG", "PETG"), ("PET", "PETG"), ("PLA", "PLA"), ("ABS", "ABS"),
             ("ASA", "ASA"), ("PPS", "PPS"), ("PPA", "PA"), ("PA", "PA"), ("PC", "PC"),
             ("TPU", "TPU"), ("TPE", "TPU"), ("PVA", "PVA"))

# ams.ams[].info bits 0-3: unit type. 1 = AMS, 2 = AMS Lite (cannot dry).
UNIT_TYPES = {3: "n3f", 4: "n3s"}
LIB_MODELS = {"AMS 2 Pro": "n3f", "AMS HT": "n3s"}
# info bits 4-7: drying status; bits 22-23: its phase.
DRY_STATUS = {0: "off", 1: "checking", 2: "drying", 3: "cooling", 4: "stopping", 5: "error",
              6: "overheat", 7: "testing"}
DRY_PHASE = {1: "heating", 2: "dehumidify"}
# Statuses that count as "drying" (Bambu Studio); off and cooling are idle.
ACTIVE_STATUSES = ("checking", "drying", "error", "overheat")
# fun2 bit 5: the printer accepts remote drying and reports its status.
FUN2_REMOTE_DRY_BIT = 5

# ams.ams[].dry_sf_reason: why drying cannot start (6 = drying in progress is
# not a reason to show). Texts are ours.
BLOCKER_TEXT = {
    0: "Принтер занят задачей",
    1: "Не хватает питания: сушится слишком много AMS — подключите блок питания или остановите другую сушку",
    2: "AMS занят (калибровка, чтение RFID или загрузка/выгрузка) — подождите",
    3: "В выходе AMS остался филамент: при нагреве он может застрять — сначала выгрузите его",
    4: "AMS запускает сушку — подождите",
    5: "Недоступно в 2D-режиме (лазер или резак)",
    7: "Идёт обновление прошивки — подождите",
    8: "Не хватает питания — подключите блок питания AMS",
    10: "В выходе AMS остался филамент — выгрузите его вручную",
}
BLOCKER_UNKNOWN = "Система занята — повторите через несколько секунд"
BLOCKER_DRYING = 6
# The order Bambu Studio picks the reason to show in; unknown codes go last.
_BLOCKER_PRIORITY = (2, 3, 10, 1, 8, 5, 4, 7, 0)


def remote_dry_supported(fun2: Optional[int]) -> Optional[bool]:
    """fun2 bit 5 (None when the printer sends no fun2)."""
    return None if fun2 is None else bool(fun2 >> FUN2_REMOTE_DRY_BIT & 1)


def material_of(tray_type: Any) -> Optional[str]:
    """Drying material of a spool's tray type ("PETG HF" -> "PETG"), or None."""
    t = str(tray_type or "").strip().upper()
    if t in ("", "?", "EMPTY"):
        return None
    for prefix, material in _PREFIXES:
        if t.startswith(prefix):
            return material
    return None


def canonical_filament(value: Any) -> Optional[str]:
    """A drying preset name as the client sent it ("petg" -> "PETG"), or None."""
    if not isinstance(value, str):
        return None
    name = value.strip().upper()
    return name if name in MATERIALS else None


def decode_info(info: Any) -> Optional[Dict[str, Any]]:
    """{"type": "n3f"|"n3s"|None, "type_code", "status", "phase"} of an
    ams.ams[].info bit field (hex string or int); None when malformed."""
    if isinstance(info, bool):
        return None
    if isinstance(info, str):
        try:
            info = int(info.strip(), 16)
        except ValueError:
            return None
    if not isinstance(info, int):
        return None
    code = info & 0xF
    return {"type": UNIT_TYPES.get(code), "type_code": code,
            "status": DRY_STATUS.get((info >> 4) & 0xF),
            "phase": DRY_PHASE.get((info >> 22) & 0x3)}


def _unit_type(index: int, lib_model: Optional[str], decoded: Optional[dict]) -> Optional[str]:
    if decoded is not None:
        if decoded["type"]:
            return decoded["type"]
        if decoded["type_code"] in (1, 2):   # classic AMS / AMS Lite
            return None
    if lib_model in LIB_MODELS:
        return LIB_MODELS[lib_model]
    return "n3s" if index >= 128 else None


def _blockers(codes: Tuple[int, ...]) -> List[Dict[str, Any]]:
    wanted = {c for c in codes if c != BLOCKER_DRYING}
    ordered = [c for c in _BLOCKER_PRIORITY if c in wanted]
    ordered += sorted(wanted - set(ordered))
    return [{"code": c, "text": BLOCKER_TEXT.get(c, BLOCKER_UNKNOWN)} for c in ordered]


def _cap(material: str, i: int, feeding: bool) -> int:
    """Highest drying temperature a loaded spool of `material` tolerates."""
    m = MATERIALS[material]
    if feeding:
        return min(m["print"][i], m["soft"], m["hdt"])
    return m["hdt"]


def _preset(material: str, i: int, feeding: bool, max_temp: int) -> Dict[str, Any]:
    m = MATERIALS[material]
    temp = m["print"][i] if feeding else m["idle"][i]
    return {"filament": material, "temp": min(temp, max_temp), "hours": m["hours"][i]}


def _policy(dry_type: str, slots: list, feeding: bool) -> Dict[str, Any]:
    i = 0 if dry_type == "n3f" else 1
    type_max = TYPE_MAX[dry_type]
    occupied = []   # (material, cap, shown tray type)
    for slot in slots:
        if slot.get("empty"):
            continue
        shown = slot.get("type") or "?"
        material = material_of(shown) or FALLBACK_MATERIAL
        occupied.append((material, _cap(material, i, feeding), shown))
    max_temp, max_by = type_max, None
    if occupied:
        lowest = min(occupied, key=lambda o: o[1])       # first one on ties
        max_temp = max(TEMP_MIN, min(type_max, lowest[1]))
        if lowest[1] < type_max:
            max_by = lowest[2]
    if occupied:
        # The occupied slot with the lowest recommended temperature decides.
        material = min(occupied, key=lambda o: o[1] if feeding else MATERIALS[o[0]]["idle"][i])[0]
    else:
        material = FALLBACK_MATERIAL
    return {"max_temp": max_temp, "max_by": max_by,
            "suggest": _preset(material, i, feeding, max_temp),
            "presets": [_preset(m, i, feeding, max_temp) for m in MATERIALS]}


def build_dry_info(unit: Dict[str, Any], lib_model: Optional[str], raw_unit: Optional[Dict[str, Any]],
                   fun2: Optional[int], feeding: bool,
                   setting: Tuple[Optional[int], Optional[int], Optional[str]]) -> Optional[Dict[str, Any]]:
    """The `dry` block of a normalized AMS unit, or None when it cannot dry.

    unit: the normalized unit (index, slots, dry_time); lib_model: the lib's
    AMSInstance.model ("Unknown" until get_version); raw_unit: RawReport's
    {"info", "sf"} of that unit; feeding: this unit feeds a running or paused
    print; setting: the lib's drying (temperature, hours, filament).
    """
    index = unit.get("index")
    if not isinstance(index, int):
        return None
    raw_unit = raw_unit or {}
    decoded = decode_info(raw_unit.get("info"))
    dry_type = _unit_type(index, lib_model, decoded)
    if dry_type is None:
        return None
    # Like Bambu Studio: status and reasons only when fun2 says remote drying is supported.
    remote = remote_dry_supported(fun2) is True
    status = decoded["status"] if remote and decoded else None
    remaining = unit.get("dry_time") or None
    temp, hours, filament = setting
    dry = {
        "type": dry_type,
        "status": status,
        "phase": decoded["phase"] if status in ("checking", "drying") else None,
        "active": status in ACTIVE_STATUSES if status is not None else bool(remaining),
        "remaining_min": remaining,
        "temp": temp or None,
        "hours": hours or None,
        "filament": filament or None,
        "blockers": _blockers(raw_unit.get("sf") or ()) if remote else [],
        "feeding": bool(feeding),
    }
    dry.update(_policy(dry_type, unit.get("slots") or [], bool(feeding)))
    return dry
