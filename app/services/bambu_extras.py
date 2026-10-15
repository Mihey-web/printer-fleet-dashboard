"""Bambu telemetry the dashboard derives on top of the vendored pybambu.

Inputs are the lib's Device and a RawReport snapshot (app/collectors/bambu_raw,
the report fields the lib drops). Everything here is pure and tolerant: a
device or snapshot without the needed parts yields None.

Protocol facts (bit layouts and enum values) follow Bambu Studio's device
code; nothing is copied from it.
"""
import functools
import logging
from typing import Any, Dict, Optional

from app.collectors.bambu_raw import change_busy
from app.vendor.pybambu.const import Features, Print_Fun_Values
from app.vendor.pybambu.utils import get_print_error_text

logger = logging.getLogger(__name__)

# Filament-change step from the ams_status sub state (bits 0-7). New firmware
# mirrors ams_status in device.extruder.info[].stat. Bambu Studio's progress
# panel shows exactly these values for both protocols; anything else has no step.
FILAMENT_STEPS = {
    2: "heat",     # heat the nozzle
    3: "cut",      # cut the filament
    4: "pull",     # pull the current filament back
    5: "push",     # push the new filament
    6: "push",     # push the new filament
    7: "purge",    # purge the old filament
    8: "check",    # check the filament position
    0x0B: "check",
}
# Loading from an external spool Studio shows a step only for these sub
# states (nothing is cut or pulled back into an AMS); 6 = confirm on the
# printer's screen that the filament extrudes.
EXTERNAL_LOAD_STEPS = {2: "heat", 5: "push", 6: "confirm", 7: "purge"}
# ams_status main state (bits 8-15) of a cold pull: not a filament change.
AMS_MAIN_COLD_PULL = 7
# star / snow: (ams_id << 8) | slot_id; 0xFFFF = none; slot 0xFF = unload target.
SLOT_NONE = 0xFFFF
SLOT_UNLOAD = 0xFF
# ams_id of the external spools (star / snow, ams_change_filament): 255 = the
# (main) external spool, 254 = the H2D's second one. Not tray_now, where 254 is
# any external spool and 255 means nothing loaded (normalizer.TRAY_*).
EXTERNAL_SPOOLS = (254, 255)


def _active_extruder(raw: Dict[str, Any]) -> Dict[str, int]:
    """The extruder entry a filament change runs on: the loading extruder
    (device.extruder.state bits 15-18), else the active one (bits 4-7)."""
    state = raw.get("ext_state")
    extruders = raw.get("extruders") or {}
    if state is None:
        return {}
    return extruders.get((state >> 15) & 0xF) or extruders.get((state >> 4) & 0xF) or {}


def _legacy_target(tray_tar: Optional[int]):
    """(action, ams, slot) from the legacy ams.tray_tar."""
    if tray_tar is None:
        return None, None, None
    if tray_tar == 255:
        return "unload", None, None
    if tray_tar == 254:
        return "load", 255, 0
    if 0x80 <= tray_tar <= 0x87:   # AMS HT: one slot, addressed by its unit id
        return "load", tray_tar, 0
    if 0 <= tray_tar < 0x80:
        return "load", tray_tar >> 2, tray_tar & 3
    return None, None, None


def filament_change(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """{"action": "load"|"unload"|None, "step": slug|None, "ams": int|None,
    "slot": int|None} while the printer runs a filament change, else None."""
    ext_state = raw.get("ext_state")
    extruders = raw.get("extruders") or {}
    ams_status = raw.get("ams_status")
    if not change_busy(ext_state, extruders, ams_status):
        return None
    new = new_protocol(raw)
    entry = _active_extruder(raw) if new else {}
    status = ams_status if ams_status is not None else entry.get("stat")
    if status is not None and (status >> 8) & 0xFF == AMS_MAIN_COLD_PULL:
        return None

    if new:
        action = ams = slot = None
        star = entry.get("star")
        if star is not None and star != SLOT_NONE:
            ams = (star >> 8) & 0xFF
            if star & 0xFF == SLOT_UNLOAD:
                action = "unload"
            else:
                action, slot = "load", star & 0xFF
    else:
        action, ams, slot = _legacy_target(raw.get("tray_tar"))

    sub = status & 0xFF if status is not None else None
    steps = EXTERNAL_LOAD_STEPS if action == "load" and ams in EXTERNAL_SPOOLS else FILAMENT_STEPS
    return {"action": action, "step": steps.get(sub), "ams": ams, "slot": slot}


def signature_required(raw: Dict[str, Any]) -> Optional[bool]:
    """print.fun bit 29: the printer accepts print-class commands only when they
    are signed (Developer Mode off). None when the printer sends no `fun` (P1
    series): only a probe tells there."""
    if not raw.get("fun_seen"):
        return None
    return bool(raw["fun"] & Print_Fun_Values.MQTT_SIGNATURE_REQUIRED)


def ams_unit_name(index: int) -> str:
    """Display name of an AMS unit by its physical id (app.js amsUnitName):
    0-3 = AMS / AMS 2 Pro / AMS Lite, 128-135 = AMS HT."""
    return "AMS HT %d" % (index - 127) if index >= 128 else "AMS %d" % (index + 1)


def feature(device: Any, flag: Features) -> Optional[bool]:
    """Device.supports_feature(flag), or None while it cannot answer.

    Most flags depend on the firmware version, which arrives with get_version
    after the first reports. Before that the lib logs an ERROR and returns
    False, so it is not asked at all; test doubles without the method give None.
    """
    supports = getattr(device, "supports_feature", None)
    info = getattr(device, "info", None)
    if not callable(supports) or info is None:
        return None
    if getattr(info, "sw_ver", None) in (None, "", "unknown"):
        return None
    try:
        return bool(supports(flag))
    except Exception:
        logger.debug("supports_feature(%s) failed", flag, exc_info=True)
        return None


def new_protocol(raw: Dict[str, Any]) -> bool:
    """The printer reports per-extruder state (device.extruder): X1C on current
    firmware, P2S, H2 series. P1 printers use the legacy fields only."""
    return raw.get("ext_state") is not None and bool(raw.get("extruders"))


def filament_loaded(raw: Dict[str, Any], tray_now: Optional[int]) -> Optional[bool]:
    """Is there filament in the (active) extruder?

    New protocol: device.extruder.info[].info bit 1. Legacy protocol: the
    tray_now convention (255 = nothing loaded). None when unknown.
    """
    if new_protocol(raw):
        state = raw["ext_state"]
        entry = raw["extruders"].get((state >> 4) & 0xF) or {}
        info = entry.get("info")
        return None if info is None else bool(info & 0b10)
    if tray_now is None:
        return None
    return tray_now != 255


def dual_nozzle(device: Any, raw: Dict[str, Any]) -> bool:
    """A printer with two extruders or a filament track switch (H2D, H2C, X2D):
    filament changes there need a feed direction, which the dashboard does not send."""
    if feature(device, Features.DUAL_NOZZLES):
        return True
    state = raw.get("ext_state")
    if state is not None and (state & 0xF) > 1:   # bits 0-3: extruder count
        return True
    for unit in (raw.get("ams") or {}).values():
        info = unit.get("info")
        if info is not None and (info >> 8) & 0xF == 0xE:   # AMS fed through the track switch
            return True
    return False


@functools.lru_cache(maxsize=64)
def print_error_line(code: int, device_type: str = "") -> str:
    """"Ошибка печати 0300_400C: <text>" from Bambu's tables (Russian, English
    fallback), or just the code when the tables have no text for it.

    The code is formatted as 8 hex digits, as the printer's screen and Bambu
    Studio show it; the lib's own PrintError mangles codes >= 0x10000000.
    Cached: a lookup miss decompresses the whole table (tens of ms on the Pi).
    """
    code8 = "%08X" % (code & 0xFFFFFFFF)
    label = "%s_%s" % (code8[:4], code8[4:])
    try:
        text = (get_print_error_text(code8, device_type or "", "ru") or "").strip()
    except Exception:
        logger.debug("print_error text lookup failed for %s", label, exc_info=True)
        text = ""
    if text and text != "unknown":
        return "Ошибка печати %s: %s" % (label, text)
    return "Ошибка печати %s" % label


# fun2 bit 0: the printer prints from its internal eMMC (P2S, H2 series), so a
# missing SD card is normal there.
FUN2_EMMC_BIT = 0
SDCARD_STATES = ("normal", "missing", "abnormal")


def door_open(device: Any) -> Optional[bool]:
    """The door sensor (H2 / P2 / X2 series, X1 on recent firmware), else None.

    The lib reads it from print.stat bit 23, on X1 models from home_flag bit 23
    (X1C's stat bit 23 is set with the door closed).
    """
    if feature(device, Features.DOOR_SENSOR) is not True:
        return None
    value = getattr(getattr(device, "info", None), "door_open", None)
    return value if isinstance(value, bool) else None


def sdcard(device: Any, raw: Dict[str, Any]) -> Optional[str]:
    """"normal" / "missing" / "abnormal" (home_flag bits 8-9), or None when no
    home_flag has been reported, or when the card is missing on a printer that
    prints from eMMC."""
    if not raw.get("home_flag_seen"):
        return None
    status = getattr(getattr(device, "home_flag", None), "sdcard_status", None)
    if status not in SDCARD_STATES:
        return None
    fun2 = raw.get("fun2")
    if status == "missing" and fun2 is not None and fun2 >> FUN2_EMMC_BIT & 1:
        return None
    return status
