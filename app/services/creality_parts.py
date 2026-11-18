"""Creality parts of the running job (Klipper exclude_object over WS :9999).

The printer answers {"method": "get", "params": {"reqPrintObjects": 1}} (the
vendored client asks every 2 s) with three keys, as on K2 Plus, K1C and
Ender-5 Max (16.11.2026):

    objects           JSON text: [{"name", "center": [x, y], "polygon": [[x, y], ...]}]
    excluded_objects  JSON text: ["NAME", ...]   (the parts already skipped)
    current_object    the name of the part being printed now

A part is known by its name (Klipper's), the slicer's labels: "PART.STL_ID_0_COPY_0".
The printer's own web page skips one with {"method": "set", "params":
{"excludeObjects": ["NAME"]}}, one name per frame (skip_frames).
"""
import json
import math
import re
from typing import Any, Dict, List, Optional

MAX_PARTS = 500
MAX_POINTS = 400
MAX_NAME = 200
# Bed width / depth in mm by registry model: the plate the map draws the parts on.
BED_MM = {"k1c": (220, 220), "k1": (220, 220), "k1max": (300, 300),
          "k2plus": (350, 350), "ender5max": (400, 400)}
_SUFFIX = re.compile(r"(\.(stl|3mf|obj|step|stp|amf))?_id_\d+_copy_\d+$", re.I)


def _json_list(value: Any) -> Optional[list]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    return value if isinstance(value, list) else None


def _point(p: Any) -> Optional[List[float]]:
    if (isinstance(p, (list, tuple)) and len(p) >= 2
            and all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in p[:2])):
        return [round(float(p[0]), 2), round(float(p[1]), 2)]
    return None


def _valid_name(name: Any) -> bool:
    return isinstance(name, str) and 0 < len(name) <= MAX_NAME and bool(name.strip())


def parse_objects(value: Any) -> Optional[List[Dict[str, Any]]]:
    """The job's parts in the printer's order: {"name", "polygon" (≥ 3 points or None), "center"}.
    None when the value is not a list (not reported / unreadable)."""
    items = _json_list(value)
    if items is None:
        return None
    out, seen = [], set()
    for o in items[:MAX_PARTS]:
        if not isinstance(o, dict) or not _valid_name(o.get("name")) or o["name"] in seen:
            continue
        seen.add(o["name"])
        raw = o.get("polygon") if isinstance(o.get("polygon"), list) else []
        poly = [q for q in (_point(p) for p in raw[:MAX_POINTS]) if q]
        out.append({"name": o["name"], "polygon": poly if len(poly) >= 3 else None, "center": _point(o.get("center"))})
    return out


def parse_excluded(value: Any) -> Optional[List[str]]:
    items = _json_list(value)
    if items is None:
        return None
    return [n for n in items[:MAX_PARTS] if _valid_name(n)]


class Reader:
    """Parses the three keys only when the printer's text changes: the client's
    state is cumulative, so every frame carries them again."""

    def __init__(self):
        self._raw: Dict[str, Any] = {}
        self._parsed: Dict[str, Any] = {}

    def absorb(self, data: Dict[str, Any]) -> Dict[str, Any]:
        out = {}
        for key, parse, field in (("objects", parse_objects, "print_objects"),
                                  ("excluded_objects", parse_excluded, "excluded_objects")):
            if key not in data:
                continue
            if self._raw.get(key) != data[key] or field not in self._parsed:
                self._raw[key] = data[key]
                self._parsed[field] = parse(data[key])
            out[field] = self._parsed[field]
        if "current_object" in data:
            cur = data["current_object"]
            out["current_object"] = cur if _valid_name(cur) else None
        return out


def labels(parts: List[Dict[str, Any]]) -> List[str]:
    """Readable names: "Колесо_2.STL_ID_1_COPY_0" → "Колесо 2 №2" (numbered when a model repeats)."""
    bases = [(_SUFFIX.sub("", p["name"]).replace("_", " ").strip() or "Деталь") for p in parts]
    total = {b: bases.count(b) for b in bases}
    seen: Dict[str, int] = {}
    out = []
    for b in bases:
        seen[b] = seen.get(b, 0) + 1
        out.append(b + (" №%d" % seen[b] if total[b] > 1 else ""))
    return out


def skipped(data: Dict[str, Any]) -> Optional[List[str]]:
    """The status's skipped_objects: excluded names that are parts of this job, in its order."""
    parts = data.get("print_objects")
    if not parts:
        return None
    done = set(data.get("excluded_objects") or [])
    return [p["name"] for p in parts if p["name"] in done]


def view(data: Dict[str, Any], model: Optional[str]) -> Dict[str, Any]:
    """GET /api/printers/{id}/parts: what the job card's plate map draws."""
    parts = data.get("print_objects") or []
    names = labels(parts)
    known = {p["name"] for p in parts}
    cur = data.get("current_object")
    bed = BED_MM.get(str(model or "").lower())
    return {
        "parts": [{"id": p["name"], "name": n, "polygon": p["polygon"], "center": p["center"]} for p, n in zip(parts, names)],
        "skipped": skipped(data) or [],
        "current": cur if cur in known else None,
        "bed": list(bed) if bed else None,
    }


MSG_NONE = "Не выбраны детали"
MSG_UNLABELED = "Принтер не сообщает детали задания — в слайсере не включена маркировка деталей"
MSG_UNKNOWN = "Таких деталей нет в текущем задании"
MSG_DONE = "Эти детали уже пропущены"
MSG_ALL = "Пропустить все детали нельзя — для этого есть «Стоп»"


def skip_frames(names: Any, data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The "set" frames that skip `names` of the running job: its own parts only,
    never all of them, never twice. ValueError (ru) otherwise."""
    if (not isinstance(names, list) or not names or len(names) > MAX_PARTS
            or any(not _valid_name(n) for n in names)):
        raise ValueError(MSG_NONE)
    parts = [p["name"] for p in data.get("print_objects") or []]
    if not parts:
        raise ValueError(MSG_UNLABELED)
    if not set(names) <= set(parts):
        raise ValueError(MSG_UNKNOWN)
    done = set(data.get("excluded_objects") or [])
    new = [n for n in parts if n in set(names) and n not in done]
    if not new:
        raise ValueError(MSG_DONE)
    if not set(parts) - done - set(new):
        raise ValueError(MSG_ALL)
    return [{"excludeObjects": [n]} for n in new]
