from typing import Any, Dict, Optional
from app.domain.models import PrinterState


def map_state(value: Optional[str]) -> PrinterState:
    s = str(value or "").strip().lower()

    # Connection / availability
    if s in {"offline", "disconnected"}:
        return PrinterState.OFFLINE

    # Common print states. Bambu also reports "prepare" / "slicing" while the
    # job is actively warming up before extrusion — those count as printing
    # from the dashboard's perspective so the printer doesn't look idle.
    if s in {"printing", "running", "prepare", "slicing"}:
        return PrinterState.PRINTING
    if s in {"pause", "paused"}:
        return PrinterState.PAUSED
    if s in {"finish", "finished", "complete", "completed"}:
        return PrinterState.FINISHED
    if s in {"idle", "ready", "standby"}:
        return PrinterState.IDLE
    if s in {"error", "failed"}:
        return PrinterState.ERROR

    return PrinterState.UNKNOWN


def as_float(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        return float(v)
    except Exception:
        return None


def as_int(v: Any) -> Optional[int]:
    try:
        if v is None:
            return None
        return int(v)
    except Exception:
        return None


def safe_job_name(data: Dict[str, Any]) -> Optional[str]:
    for key in ("filename", "file", "gcode_file", "gcode_file_name", "project_name", "message"):
        value = data.get(key)
        if value:
            return str(value)
    return None
