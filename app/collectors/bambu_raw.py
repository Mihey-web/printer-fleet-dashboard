"""Raw Bambu report fields that the vendored pybambu drops or keeps private.

The lib parses each `print` report into its Device model but does not keep:
`fun2`, the AMS units' `info` bit field and `dry_sf_reason`, `ams_status`,
`ams.tray_tar`, and the per-extruder `device.extruder` fields other than the
target slot and temperatures. `fun` is kept only as a private string.
RawReport keeps them as plain ints, merged like the lib merges P1 deltas (a
key missing from a report keeps its previous value).

update() runs on the paho thread for every report and must never raise or
stop the lib from parsing: each field is read on its own, and a malformed one
keeps its previous value. snapshot() hands the poll thread a private copy.
"""
import logging
import threading
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# device.extruder.state bit 19: a filament change (load / unload) is running.
EXT_STATE_BUSY_BIT = 19
# ams_status (legacy protocol, mirrored on new firmware): main state in bits 8-15.
AMS_MAIN_FILAMENT_CHANGE = 1
_EXTRUDER_KEYS = ("stat", "snow", "star", "info")


def _int(value: Any) -> Optional[int]:
    """An int, or a decimal string of one ("254"); anything else is None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip(), 10)
        except ValueError:
            return None
    return None


def _hex(value: Any) -> Optional[int]:
    """A hex bit-field string ("10942023"), or an int as is."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return int(value.strip(), 16)
        except ValueError:
            return None
    return None


def change_busy(ext_state: Optional[int], extruders: Dict[int, Dict[str, int]],
                ams_status: Optional[int]) -> bool:
    """True while the printer runs a filament change (load or unload).

    New protocol (an extruder block is reported): device.extruder.state bit 19.
    Legacy protocol (P1): ams_status main state 1.
    """
    if ext_state is not None and extruders:
        return bool(ext_state >> EXT_STATE_BUSY_BIT & 1)
    if ams_status is not None:
        return (ams_status >> 8) & 0xFF == AMS_MAIN_FILAMENT_CHANGE
    return False


class RawReport:
    def __init__(self):
        self._lock = threading.Lock()
        self._fun: Optional[int] = None
        self._fun_seen = False
        self._fun2: Optional[int] = None
        self._home_flag_seen = False
        self._job_id: Optional[str] = None
        self._ams_status: Optional[int] = None
        self._tray_tar: Optional[int] = None
        self._ams: Dict[int, Dict[str, Any]] = {}
        self._ext_state: Optional[int] = None
        self._extruders: Dict[int, Dict[str, int]] = {}
        self._busy = False
        self._changes_done = 0   # busy -> not busy transitions seen

    def update(self, p: Any) -> None:
        if not isinstance(p, dict):
            return
        with self._lock:
            for part in (self._update_flags, self._update_ams, self._update_extruder):
                try:
                    part(p)
                except Exception:
                    logger.debug("raw report field skipped", exc_info=True)
            busy = change_busy(self._ext_state, self._extruders, self._ams_status)
            if self._busy and not busy:
                self._changes_done += 1
            self._busy = busy

    def _update_flags(self, p: dict) -> None:
        if "fun" in p:
            fun = _hex(p["fun"])
            if fun is not None:
                self._fun, self._fun_seen = fun, True
        if "fun2" in p:
            fun2 = _hex(p["fun2"])
            if fun2 is not None:
                self._fun2 = fun2
        if "home_flag" in p and _int(p["home_flag"]) is not None:
            self._home_flag_seen = True
        # The print-error dialog answers (ignore / resume) carry the job id.
        if p.get("job_id") not in (None, ""):
            self._job_id = str(p["job_id"])
        if "ams_status" in p:
            status = _int(p["ams_status"])
            if status is not None:
                self._ams_status = status

    def _update_ams(self, p: dict) -> None:
        ams = p.get("ams")
        if not isinstance(ams, dict):
            return
        if "tray_tar" in ams:
            tar = _int(ams["tray_tar"])
            if tar is not None:
                self._tray_tar = tar
        units = ams.get("ams")
        if not isinstance(units, list):
            return
        for unit in units:
            if not isinstance(unit, dict):
                continue
            uid = _int(unit.get("id"))
            if uid is None:
                continue
            cur = self._ams.setdefault(uid, {"info": None, "sf": None})
            if "info" in unit:
                info = _hex(unit["info"])
                if info is not None:
                    cur["info"] = info
            sf = unit.get("dry_sf_reason")
            if isinstance(sf, list):
                codes = [_int(c) for c in sf]
                if all(c is not None for c in codes):
                    cur["sf"] = tuple(codes)

    def _update_extruder(self, p: dict) -> None:
        device = p.get("device")
        ext = device.get("extruder") if isinstance(device, dict) else None
        if not isinstance(ext, dict):
            return
        if "state" in ext:
            state = _int(ext["state"])
            if state is not None:
                self._ext_state = state
        info = ext.get("info")
        if not isinstance(info, list):
            return
        for entry in info:
            if not isinstance(entry, dict):
                continue
            eid = _int(entry.get("id"))
            if eid is None:
                continue
            cur = self._extruders.setdefault(eid, {})
            for key in _EXTRUDER_KEYS:
                if key in entry:
                    value = _int(entry[key])
                    if value is not None:
                        cur[key] = value

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "fun": self._fun,
                "fun_seen": self._fun_seen,
                "fun2": self._fun2,
                "home_flag_seen": self._home_flag_seen,
                "job_id": self._job_id,
                "ams_status": self._ams_status,
                "tray_tar": self._tray_tar,
                "ams": {uid: dict(unit) for uid, unit in self._ams.items()},
                "ext_state": self._ext_state,
                "extruders": {eid: dict(e) for eid, e in self._extruders.items()},
                "busy": self._busy,
                "changes_done": self._changes_done,
            }
