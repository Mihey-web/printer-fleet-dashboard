"""What a Bambu model can do, by model name prefix: the one table every module asks.

A model is the name the registry or the live device gives ("X1C", "P1S", "H2D", "A1MINI"...),
in any case. Each capability lists the name prefixes of the models that have it; a name that
starts with none of them (an unknown model, "", None) has none. Where the lib can answer from
the live device (bambu_extras.feature), the callers ask it first and fall back to this table.
"""
from typing import Any, Dict, Tuple

CAPABILITIES: Dict[str, Tuple[str, ...]] = {
    # A chamber temperature sensor (the lib's Features.CHAMBER_TEMPERATURE: H2 / P2 / X1 / X1E / X2).
    "chamber_sensor": ("H2", "P2", "X1", "X2"),
    # An active chamber heater (M141, up to printer_commands.CHAMBER_MAX); P and X1 have none.
    "chamber_heater": ("H2",),
    # A second chamber lamp (the lib's Features.CHAMBER_LIGHT_2, a model-only flag).
    "second_light": ("H2", "X2"),
    # The hotend goes to 350 °C (the others to 300 °C).
    "hotend_350": ("H2",),
    # Two nozzles: filament mapping and swaps are not supported (printer_commands.MSG_DUAL_NOZZLE).
    "dual_nozzle": ("H2D", "H2C", "X2D"),
    # Files only over FTPS: no port-6000 file channel (live 16.11: a P1S login times out).
    "files_over_ftps": ("P1", "A1"),
    # FTPS shows only a USB stick; the internal storage is the file channel's.
    "usb_over_ftps": ("P2", "H2", "X2"),
    # The running job's slice info and files come from the printer over the file channel
    # (Bambu Studio's get_project_file); older firmware can still give them over FTP.
    "current_job_over_file_channel": ("H2", "P2", "X1", "X2"),
    # Every file on the printer can be read (X1: SD card and history over the file channel,
    # P1 / A1: FTPS); P2 / H2 only from a USB stick.
    "reads_every_file": ("X1", "P1", "A1"),
}


def has(model: Any, capability: str) -> bool:
    """True when `model` (any case) has `capability` (a key of CAPABILITIES)."""
    return str(model or "").upper().startswith(CAPABILITIES[capability])
