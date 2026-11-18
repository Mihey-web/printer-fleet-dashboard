"""What a person needs to know about a sliced job: material, time, settings, parts.

Bambu: built from the job's 3MF metadata (slice_info.config always; project_settings.config
only where the printer lets us read it). Creality: from the printer's G-code file listing.
Unknown values are left out, never guessed.
"""
import math
import re
from collections import Counter

_PATTERNS = {
    "grid": "сетка", "gyroid": "гироид", "cubic": "кубическое", "adaptivecubic": "адаптивное кубическое",
    "honeycomb": "соты", "3dhoneycomb": "3D-соты", "line": "линии", "rectilinear": "прямолинейное",
    "alignedrectilinear": "выровненное прямолинейное", "triangles": "треугольники",
    "tri-hexagon": "треугольники-шестиугольники", "lightning": "молния", "crosshatch": "перекрёстное",
    "zig-zag": "зигзаг", "concentric": "концентрическое", "supportcubic": "кубическое с опорой",
    "quartercubic": "четвертькубическое", "hilbertcurve": "кривая Гильберта",
}
_SUPPORTS = {"normal(auto)": "обычные, авто", "tree(auto)": "древовидные, авто",
             "normal(manual)": "обычные, вручную", "tree(manual)": "древовидные, вручную"}
# bed type → (name, the per-filament bed temperature key)
_BEDS = {
    "Textured PEI Plate": ("текстурированный PEI", "textured_plate_temp"),
    "Cool Plate": ("холодный", "cool_plate_temp"),
    "Cool Plate (SuperTack)": ("SuperTack", "supertack_plate_temp"),
    "Supertack Plate": ("SuperTack", "supertack_plate_temp"),
    "Engineering Plate": ("инженерный", "eng_plate_temp"),
    "High Temp Plate": ("высокотемпературный", "hot_plate_temp"),
    "Smooth PEI Plate": ("гладкий PEI", "hot_plate_temp"),
    "Textured Cool Plate": ("текстурированный холодный", "textured_cool_plate_temp"),
    "Epoxy Resin Plate": ("эпоксидный", "epoxy_resin_plate_temp"),     # Creality Print
}
_GENERIC = re.compile(r"(^|/)(Metadata/plate_\d+\.gcode|project_file(\.3mf)?)$", re.I)
_SUFFIX = re.compile(r"_[A-Za-z0-9+-]+_(\d+d)?\d+h\d+m\d+s$")  # Creality Print: _PETG_8h55m7s
_EXTENSION = re.compile(r"\.(stl|step|stp|obj|3mf|amf)$", re.I)


def generic_name(name):
    """A firmware placeholder (plate_1.gcode, project_file) rather than the job's real name."""
    return not isinstance(name, str) or not name.strip() or bool(_GENERIC.search(name.strip()))


def short_name(name):
    """The part's name of a print file: "Шестерня.gcode.3mf", Creality's "Колесо.STL_PETG_8h55m7s.gcode" → "Шестерня",
    "Колесо"; "" when nothing is left (the callers pick their own fallback). app.js jobShortName is the same
    expression (it falls back to the base name instead); tests/test_qa_fix_phase2.py keeps the two equal."""
    base = str(name or "").replace("\\", "/").rsplit("/", 1)[-1]
    base = re.sub(r"\.gcode\.3mf$", "", base, flags=re.I)
    base = re.sub(r"\.(gcode|gco|3mf)$", "", base, flags=re.I)
    return _EXTENSION.sub("", _SUFFIX.sub("", base)).strip()


def _number(value, scale=1.0):
    try:
        n = float(str(value).strip().rstrip("%")) * scale
    except (TypeError, ValueError):
        return None
    return round(n, 3) if math.isfinite(n) else None


def _first(value):
    return value[0] if isinstance(value, list) and value else value


def _item(value, index):
    if isinstance(value, list):
        return value[index] if 0 <= index < len(value) else None
    return value


def _color(value):
    text = str(value or "").strip()
    return text[:7].upper() if re.fullmatch(r"#[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?", text) else None


def _clean(summary):
    return {k: v for k, v in summary.items() if v not in (None, "", [], {})}


def _title(objects):
    names = [o["name"] for o in objects]
    if not names:
        return None
    if len(names) <= 2:
        return " + ".join(names)
    return "%s + ещё %d" % (names[0], len(names) - 1)


def _print_settings(settings, filament=0):
    """The slicer's print settings a person reads (Bambu project_settings, an Orca G-code's config).
    The bed temperature is per filament of the project: the one of `filament` (the job's first,
    0-based), or with None (not known) only when every filament has the same."""
    if not settings:
        return {}
    bed, bed_temp = None, None
    if settings.get("curr_bed_type") in _BEDS:
        bed, key = _BEDS[settings["curr_bed_type"]]
        temps = settings.get(key)
        if filament is not None:
            bed_temp = _number(_item(temps, filament))
        elif not isinstance(temps, list) or len(set(temps)) == 1:
            bed_temp = _number(_first(temps))
    pattern = settings.get("sparse_infill_pattern")
    infill, walls = _number(settings.get("sparse_infill_density")), _number(settings.get("wall_loops"))
    return {
        "layer_mm": _number(settings.get("layer_height")),
        "first_layer_mm": _number(settings.get("initial_layer_print_height")),
        "infill": "%g%%" % infill if infill is not None and 0 <= infill <= 100 else None,   # not «nan%»
        "infill_pattern": _PATTERNS.get(pattern, pattern),
        "walls": walls if walls is not None and walls >= 0 else None,
        "supports": _SUPPORTS.get(settings.get("support_type"), "есть") if str(settings.get("enable_support")) == "1" else "нет",
        "bed": bed,
        "bed_temp": bed_temp,
        "by_object": True if settings.get("print_sequence") == "by object" else None,
        "ironing": True if settings.get("ironing_type") not in (None, "", "no ironing") else None,
    }


def bambu(document):
    """Summary from the raw 3MF metadata document print_material.collect_project builds."""
    files = document.get("files") or {}
    slice_info = (files.get("slice_info.config") or {}).get("config") or {}
    plates = slice_info.get("plate") or []
    number = document.get("plate")
    plate = None
    for candidate in plates:
        meta = {m.get("key"): m.get("value") for m in candidate.get("metadata", [])}
        if number is None and len(plates) == 1 or str(meta.get("index")) == str(number):
            plate = candidate
            break
    if plate is None:
        return {}
    meta = {m.get("key"): m.get("value") for m in plate.get("metadata", [])}
    settings = files.get("project_settings.config") or {}
    header = {h.get("key"): h.get("value") for item in slice_info.get("header", []) for h in item.get("header_item", [])}

    filaments = []
    for f in plate.get("filament", []):
        index = int(_number(f.get("id")) or 1) - 1
        filaments.append(_clean({
            "type": f.get("type"), "color": _color(f.get("color")),
            "grams": _number(f.get("used_g")), "meters": _number(f.get("used_m")),
            "profile": _item(settings.get("filament_settings_id"), index),
            "nozzle_temp": _number(_item(settings.get("nozzle_temperature"), index)),
        }))

    counts, skipped = Counter(), Counter()
    for o in plate.get("object", []):
        name = short_name(o.get("name")) or "Без названия"
        counts[name] += 1
        if str(o.get("skipped")).lower() == "true":
            skipped[name] += 1
    objects = [_clean({"name": n, "count": c, "skipped": skipped[n] or None}) for n, c in counts.items()]
    # The printable parts by the id the printer knows them by (skip_objects, pick_N.png colours).
    parts = [{"id": int(o["identify_id"]), "name": short_name(o.get("name")) or "Без названия"}
             for o in plate.get("object", [])
             if str(o.get("identify_id", "")).isdigit() and str(o.get("skipped")).lower() != "true"]

    first = plate.get("filament") or []      # the bed is heated for the plate's first filament
    facts = _print_settings(settings, int(_number(first[0].get("id")) or 1) - 1 if first else 0)
    if not settings and meta.get("support_used") in ("true", "false"):
        facts["supports"] = "есть" if meta["support_used"] == "true" else "нет"
    version = header.get("X-BBL-Client-Version") or ""
    version = ".".join(str(int(p)) for p in version.split(".")[:3] if p.isdigit())
    pauses = int(_number(meta.get("pause_count")) or 0)
    return _clean({
        "title": _title(objects),
        "plate": number if len(plates) > 1 else None,
        "time_s": int(_number(meta.get("prediction")) or 0) or None,
        "grams": _number(meta.get("weight")),
        "filaments": filaments,
        "objects": objects,
        "parts": parts if len(parts) > 1 else None,
        "skippable": True if meta.get("label_object_enabled") == "true" and len(parts) > 1 else None,
        "nozzle_mm": _number(meta.get("nozzle_diameters", "").split(",")[0]) or _number(_first(settings.get("nozzle_diameter"))),
        **facts,
        "nozzle_temp": filaments[0].get("nozzle_temp") if filaments else None,
        "pauses": pauses or None,
        "timelapse": True if meta.get("timelapse_type") not in (None, "0") else None,
        "slicer": "Bambu Studio " + version if version else None,
    })


def _split(value):
    return [v.strip() for v in re.split(r"[,;]", str(value or "")) if v.strip()]


def creality(item):
    """Summary from one retGcodeFileInfo2 entry (temperatures and sizes are ×100)."""
    types, colors, weights = _split(item.get("material")), _split(item.get("materialColors")), _split(item.get("filamentWeight"))
    count = max(len(types), len(colors), len(weights))
    filaments = [_clean({
        "type": _item(types, i), "color": _color(_item(colors, i)), "grams": _number(_item(weights, i)),
        "meters": _number(item.get("consumables"), 0.001) if count == 1 else None,
    }) for i in range(count)]
    size = [_number(item.get(k), 0.01) for k in ("modelX", "modelY", "modelZ")]
    software = str(item.get("printer_software_from_file") or "").replace("_", " ").strip()
    return _clean({
        "title": short_name(item.get("name") or item.get("path")) or None,
        "time_s": int(_number(item.get("timeCost")) or 0) or None,
        "grams": round(sum(f.get("grams", 0) for f in filaments), 2) or None,
        "filaments": [f for f in filaments if f],
        "nozzle_mm": _number(item.get("nozzle_diameter_from_file")) or None,
        "nozzle_temp": _number(item.get("nozzleTemp"), 0.01) or None,
        "bed_temp": _number(item.get("bedTemp"), 0.01) or None,
        "size_mm": size if all(v and v > 0 for v in size) else None,
        "slicer": software or None,
    })


def from_stored(document):
    """Stored project files: summaries since 16.11.2026; earlier ones kept the raw metadata."""
    if isinstance(document, dict) and "files" in document:
        return bambu(document)
    return document if isinstance(document, dict) else {}


# An OrcaSlicer G-code (Creality Print is one) ends with its settings as `; key = value` lines
# between "; CONFIG_BLOCK_START" and "; CONFIG_BLOCK_END"; per-filament values are lists.
_NAME_LISTS = {"filament_settings_id", "filament_type", "filament_colour"}          # "A";"B" / PETG;PLA
_NUMBER_LISTS = {"nozzle_temperature", "nozzle_diameter", "filament_density", "filament_diameter"}   # 240,220
_CONFIG_LINE = re.compile(r";\s*([a-z0-9_]+)\s*=\s?(.*)$")
_GENERATED = re.compile(r"^; generated by ([A-Za-z][A-Za-z_ ]*?)[ _]+V?(\d+(?:\.\d+){1,2})", re.M)


def gcode_config(text):
    """{key: value | [values]} of a G-code's config block; {} when it has none."""
    start = text.rfind("; CONFIG_BLOCK_START")
    end = text.find("; CONFIG_BLOCK_END", start)
    if start < 0 or end < 0:
        return {}
    out = {}
    for line in text[start:end].splitlines()[1:]:
        match = _CONFIG_LINE.match(line.strip())
        if not match:
            continue
        key, value = match[1], match[2].strip()
        if key in _NAME_LISTS:
            value = [v.strip().strip('"') for v in value.split(";")]
        elif key in _NUMBER_LISTS or key.endswith("_plate_temp"):
            value = [v.strip() for v in value.split(",")]
        out[key] = value
    return out


def with_gcode(summary, settings, head="", support_grams=None):
    """A Creality listing summary (creality()) completed from the job's own G-code: its settings,
    the slicer's version from the file's head, the supports' grams when they were counted."""
    if not settings:
        return summary
    out = dict(summary)
    # Per-filament values only when the file's filaments are the listing's, in order
    types = settings.get("filament_type") if isinstance(settings.get("filament_type"), list) else []
    filaments = summary.get("filaments") or []
    same = bool(filaments) and len(types) == len(filaments) and all(
        str(f.get("type", t)).lower() == str(t).lower() for f, t in zip(filaments, types))
    facts = _print_settings(settings, 0 if same else None)
    if summary.get("bed_temp") is not None:      # the printer's own, from its listing
        facts["bed_temp"] = None
    out.update({k: v for k, v in facts.items() if v is not None})   # an unnamed bed keeps the listing's temperature
    if support_grams is not None and out.get("supports") not in (None, "нет"):
        out["support_grams"] = support_grams
    if settings.get("timelapse_type") not in (None, "", "0"):
        out["timelapse"] = True
    out.setdefault("nozzle_mm", _number(_first(settings.get("nozzle_diameter"))) or None)
    if same:
        out["filaments"] = [_clean(dict(f, profile=_item(settings.get("filament_settings_id"), i) or None,
                                        nozzle_temp=_number(_item(settings.get("nozzle_temperature"), i))))
                            for i, f in enumerate(filaments)]
        out.setdefault("nozzle_temp", out["filaments"][0].get("nozzle_temp"))
    match = _GENERATED.search(head or "")
    if match:
        out["slicer"] = "%s %s" % (match[1].replace("_", " ").strip(), match[2])
    return _clean(out)
