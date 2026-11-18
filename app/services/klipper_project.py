"""The full job card of a Creality with Klipper's own API (Moonraker :7125, the K2 Plus).

The printer's WebSocket listing gives only the plastic, weight, temperatures and time; the
settings (layer, infill, walls, supports, bed) live in the G-code itself. Creality Print, an
OrcaSlicer, writes them at the end of the file (project_summary.gcode_config), the slicer's
version at its head — two short reads by byte range. The supports' grams need the whole
file (print_material.support_grams), read only for a job that has supports.
The K1C and the Ender-5 Max have no such API and serve no file: their card stays short.
"""
import re
import time
from typing import Any, Dict, Optional, Tuple

from app.clients import moonraker
from app.services import print_material, project_summary

HEAD_BYTES = 8 * 1024
TAIL_BYTES = 256 * 1024          # the totals and the config block: ~20 KB in Creality Print 5 / Orca 2


def _read(response, limit: int) -> bytes:
    with response:
        return response.read(limit)


def _tail(host: str, rel: str) -> bytes:
    """The last TAIL_BYTES; a server ignoring the range is read through, keeping only the end."""
    response = moonraker.open_file(host, rel, "bytes=-%d" % TAIL_BYTES)
    with response:
        if getattr(response, "status", None) == 206:
            return response.read(TAIL_BYTES)
        deadline, kept, read = time.monotonic() + print_material.SUPPORT_TIME_S, b"", 0
        while True:
            block = response.read(moonraker.CHUNK)
            if not block:
                return kept
            read += len(block)
            if read > print_material.GCODE_LIMIT or time.monotonic() > deadline:
                raise moonraker.MoonrakerError("file too long")
            kept = (kept + block)[-TAIL_BYTES:]


def job_file(host: str, path: str) -> Optional[str]:
    """The printer's job path ("/mnt/UDISK/printer_data/gcodes/a.gcode") as Moonraker's path in
    its gcodes root; another spelling of the folder: the one file of that name."""
    root = (moonraker.roots(host).get("gcodes") or "").rstrip("/")
    norm = re.sub(r"/+", "/", str(path or ""))
    if root and norm.startswith(root + "/"):
        return moonraker.check_path(norm[len(root) + 1:])
    name = norm.rsplit("/", 1)[-1]
    same = [f["path"] for f in moonraker.list_gcodes(host) if f["path"].rsplit("/", 1)[-1] == name]
    return same[0] if name and len(same) == 1 else None


def read_settings(host: str, path: str) -> Optional[Tuple[str, Dict[str, Any], str]]:
    """(Moonraker path, the G-code's settings, its head) or None when the file has no config block."""
    rel = job_file(host, path)
    if rel is None:
        return None
    settings = project_summary.gcode_config(_tail(host, rel).decode("utf-8", "replace"))
    if not settings:
        return None
    head = _read(moonraker.open_file(host, rel, "bytes=0-%d" % (HEAD_BYTES - 1)), HEAD_BYTES)
    return rel, settings, head.decode("utf-8", "replace")


def has_supports(settings: Dict[str, Any]) -> bool:
    """Supports on, and E distances relative (support_grams sums E values, absolute ones would lie)."""
    return str(settings.get("enable_support")) == "1" and str(settings.get("use_relative_e_distances")) == "1"


def support_grams(host: str, rel: str) -> Optional[float]:
    with moonraker.open_file(host, rel) as response:
        return print_material.support_grams(response, time.monotonic() + print_material.SUPPORT_TIME_S)
