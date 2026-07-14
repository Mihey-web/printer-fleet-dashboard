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

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["kind"] = self.kind.value
        data["state"] = self.state.value
        return data


def now_ts() -> float:
    return time()
