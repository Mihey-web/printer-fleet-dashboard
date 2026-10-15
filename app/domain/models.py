from dataclasses import dataclass, asdict, field
from enum import Enum
from typing import Optional, Dict, Any
from time import time


class PrinterKind(str, Enum):
    BAMBU = "bambu"
    CREALITY = "creality"
    KLIPPER = "klipper"
    MKS = "mks"


class PrinterState(str, Enum):
    IDLE = "idle"
    PRINTING = "printing"
    PAUSED = "paused"
    FINISHED = "finished"
    ERROR = "error"
    OFFLINE = "offline"
    UNKNOWN = "unknown"


@dataclass
class PrinterStatus:
    id: str
    label: str
    kind: PrinterKind
    online: bool
    state: PrinterState
    progress_pct: Optional[float] = None
    job_name: Optional[str] = None
    eta_seconds: Optional[int] = None
    print_time_seconds: Optional[int] = None
    nozzle_temp: Optional[float] = None
    bed_temp: Optional[float] = None
    chamber_temp: Optional[float] = None
    target_nozzle_temp: Optional[float] = None
    target_bed_temp: Optional[float] = None
    target_chamber_temp: Optional[float] = None
    current_layer: Optional[int] = None
    total_layers: Optional[int] = None
    wifi_signal: Optional[int] = None
    firmware_version: Optional[str] = None
    stage: Optional[str] = None
    fan_speed_pct: Optional[int] = None
    feedrate_pct: Optional[int] = None
    last_update_ts: float = 0.0
    last_error: Optional[str] = None
    device_type: Optional[str] = None
    grace_period_active: bool = False
    last_successful_fetch: float = 0.0
    debug: Dict[str, Any] = field(default_factory=dict)
    ams: Optional[Dict[str, Any]] = None
    fans: Optional[Dict[str, Any]] = None
    fw_update: Optional[bool] = None
    light_on: Optional[bool] = None
    # Bambu only (None elsewhere). A running filament change:
    # {"action": "load"|"unload"|None, "step": slug|None, "ams": int|None, "slot": int|None}.
    filament_change: Optional[Dict[str, Any]] = None
    filament_swap_ok: Optional[bool] = None   # firmware takes ams_change_filament; None until its version is known
    filament_loaded: Optional[bool] = None    # filament in the extruder; None when unknown
    dual_nozzle: Optional[bool] = None        # two extruders / track switch: no load / unload from here
    ams_dry_ok: Optional[bool] = None         # firmware supports remote AMS drying (fun2 bit 5); None without fun2
    door_open: Optional[bool] = None          # models with a door sensor (firmware version known), else None
    sdcard: Optional[str] = None              # "normal" | "missing" | "abnormal"; None when unknown / eMMC printers
    nozzle: Optional[Dict[str, Any]] = None   # {"diameter": 0.4, "type": "hardened_steel"} (lib slugs)
    signature_required: Optional[bool] = None  # print.fun bit 29: unsigned print commands refused; None without fun
    ctl_ready: Optional[bool] = None   # Klipper: klippy host "ready" (commands accepted); None elsewhere / unknown
    light_ctl: Optional[bool] = None   # Klipper: a light output_pin was found; None until looked up / elsewhere

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["kind"] = self.kind.value
        data["state"] = self.state.value
        return data


def now_ts() -> float:
    return time()
