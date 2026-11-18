"""The demo farm's print catalogue: neutral, invented parts (never a real farm's job names),
their slicer facts, and the stored pictures / job cards (data/job_previews) the panel shows.

A template is one sliced project: the parts on its plate, its filaments, time, weight and
print settings. project() turns it into the job card summary app/services/project_summary.py
builds from a real 3MF / G-code (the same keys, the same Russian words), scaled for a longer
or shorter variant of the job; preview() / pick() store its pictures through job_previews.
"""
from app.services import job_previews, project_summary

import demo_art

# grams of 1 m of 1.75 mm filament
GRAMS_PER_M = {"PLA": 2.98, "PETG": 3.05, "ABS": 2.5, "ASA": 2.57, "TPU": 2.91, "PA-CF": 2.89}
NOZZLE_TEMP = {"PLA": 220, "PETG": 255, "ABS": 260, "ASA": 260, "TPU": 230, "PA-CF": 290}
PROFILE = {
    "bambu": {"PLA": "Bambu PLA Basic", "PLA-M": "Bambu PLA Matte", "PETG": "Bambu PETG HF", "ABS": "Bambu ABS",
              "ASA": "Bambu ASA", "TPU": "Bambu TPU 95A HF", "PA-CF": "Bambu PA6-CF"},
    "creality": {"PLA": "Hyper PLA", "PLA-M": "CR-PLA Matte", "PETG": "Hyper PETG", "ABS": "Hyper ABS",
                 "ASA": "Generic ASA", "TPU": "CR-TPU", "PA-CF": "Generic PA-CF"},
    "klipper": {"PLA": "Generic PLA", "PLA-M": "Generic PLA Matte", "PETG": "Generic PETG", "ABS": "Generic ABS",
                "ASA": "Generic ASA", "TPU": "Generic TPU", "PA-CF": "Generic PA-CF"},
}
SLICER = {"bambu": "Bambu Studio 2.3.0", "creality": "Creality Print 6.2.3", "klipper": "OrcaSlicer 2.3.1"}


class Template:
    def __init__(self, key, name, objects, filaments, grams, minutes, layer=0.2, infill=15, pattern="gyroid",
                 walls=2, support=None, bed="Textured PEI Plate", bed_temp=None, ironing=False, timelapse=False):
        self.key, self.name = key, name
        self.objects = objects            # [(part name, shape, count)]
        self.filaments = filaments        # [(type, colour, share of the weight)]; "PLA-M": matte PLA
        self.grams, self.time_s = grams, minutes * 60
        self.layer, self.infill, self.pattern, self.walls = layer, infill, pattern, walls
        self.support, self.bed, self.bed_temp = support, bed, bed_temp
        self.ironing, self.timelapse = ironing, timelapse

    @property
    def count(self):
        return sum(c for _, _, c in self.objects)

    @property
    def job_name(self):
        """The name the printer reports: the part and how many of it («Кронштейн x8»)."""
        return self.name + (" x%d" % self.count if self.count > 1 and len(self.objects) == 1 else "")

    @property
    def colors(self):
        return [c for _, c, _ in self.filaments]

    @property
    def multi(self):
        return len(self.filaments) > 1

    def ftype(self, i):
        t = self.filaments[i][0]
        return "PLA" if t == "PLA-M" else t


T = Template
CATALOG = [
    T("bracket", "Кронштейн", [("Кронштейн", "bracket", 8)], [("PETG", "475569", 1.0)], 150, 210, infill=25, walls=3,
      bed_temp=70),
    T("gear", "Шестерня 24z", [("Шестерня 24z", "gear", 6)], [("PA-CF", "2B2B2B", 1.0)], 72, 130, layer=0.16, infill=40,
      pattern="cubic", walls=4, bed="Engineering Plate", bed_temp=100),
    T("sensor", "Корпус датчика", [("Корпус датчика", "case", 1), ("Крышка", "case_lid", 1)],
      [("PLA", "F5F5F5", 0.78), ("PLA", "2DD4BF", 0.22)], 165, 280, support="normal(auto)", bed_temp=55, timelapse=True),
    T("wheel", "Колесо", [("Колесо", "wheel", 4)], [("TPU", "111827", 0.7), ("PLA", "22C55E", 0.3)], 210, 380, infill=20,
      walls=3, bed_temp=35),
    T("cable", "Держатель кабеля", [("Держатель кабеля", "cable", 20)], [("PLA", "2DD4BF", 1.0)], 96, 155, bed_temp=55),
    T("battery", "Бокс для батарей", [("Бокс для батарей", "battery", 1), ("Крышка бокса", "battery_lid", 1)],
      [("PLA", "F97316", 0.74), ("PLA", "1F2937", 0.26)], 320, 470, layer=0.24, walls=3, bed_temp=55),
    T("bushing", "Втулка", [("Втулка", "bushing", 24)], [("PETG", "F5F5F5", 1.0)], 48, 75, infill=40,
      pattern="rectilinear", walls=3, bed_temp=70),
    T("organizer", "Органайзер", [("Органайзер", "organizer", 1), ("Вставка", "insert", 1), ("Вставка", "insert2", 1)],
      [("PLA-M", "E5E7EB", 0.7), ("PLA", "60A5FA", 0.15), ("PLA", "FACC15", 0.15)], 410, 580, infill=12, bed_temp=55),
    T("vent", "Вентиляционная решётка", [("Вентиляционная решётка", "vent", 2)], [("PETG", "60A5FA", 1.0)], 240, 310,
      infill=20, pattern="grid", walls=3, bed_temp=70),
    T("stand", "Фигурка совы", [("Фигурка совы", "stand", 4)],
      [("PLA", "EF4444", 0.6), ("PLA", "FACC15", 0.4)], 136, 185, pattern="grid", bed_temp=55, ironing=True),
    T("cover", "Крышка корпуса", [("Крышка корпуса", "cover", 2)], [("ABS", "374151", 1.0)], 180, 260, infill=20, walls=3,
      support="tree(auto)", bed="High Temp Plate", bed_temp=100),
    T("handle", "Ручка ящика", [("Ручка ящика", "handle", 6)], [("PLA-M", "9CA3AF", 1.0)], 84, 110, infill=20,
      pattern="grid", bed_temp=55),
    T("plug", "Заглушка трубы", [("Заглушка трубы", "plug", 30)], [("PETG", "F59E0B", 1.0)], 40, 65, bed_temp=70),
    T("sign", "Табличка", [("Табличка", "sign", 6)], [("PLA", "FFFFFF", 0.75), ("PLA", "2563EB", 0.25)], 34, 60,
      layer=0.16, bed="Cool Plate", bed_temp=35, ironing=True),
    T("camera", "Крепление камеры", [("Крепление камеры", "camera", 4)], [("ASA", "1F2937", 1.0)], 88, 140, infill=30,
      walls=4, support="tree(auto)", bed_temp=95),
    T("hinge", "Петля двери", [("Петля двери", "hinge", 4)], [("PETG", "F5F5F5", 1.0)], 118, 165, infill=25, walls=3,
      bed_temp=70),
    T("planter", "Кашпо", [("Кашпо", "planter", 1)], [("PLA-M", "84CC16", 1.0)], 470, 650, layer=0.24, infill=10,
      walls=3, bed_temp=55),
    T("lamp", "Основание лампы", [("Основание лампы", "lamp", 1)], [("PLA", "F5F5F5", 0.62), ("PLA", "B45309", 0.38)],
      560, 770, bed_temp=55, timelapse=True),
]
BY_KEY = {t.key: t for t in CATALOG}
SINGLE = [t for t in CATALOG if not t.multi]

# the parts' ids on the plate (Bambu identify_id): Bambu Studio numbers them like this
PART_ID0, PART_ID_STEP = 72, 17

_previews, _picks, _thumbs = {}, {}, {}


def _parts(t):
    return demo_art.layout([(shape, count) for _, shape, count in t.objects], t.colors)


def part_ids(t):
    return [PART_ID0 + PART_ID_STEP * i for i in range(t.count)]


def part_names(t):
    return [name for name, _, count in t.objects for _ in range(count)]


def preview(t):
    """The plate picture's stored key (the same one for every job of the template)."""
    if t.key not in _previews:
        _previews[t.key] = job_previews.save(demo_art.render(_parts(t)))
    return _previews[t.key]


def thumb_png(t, plate=True):
    """A printer's own thumbnail of the file (Creality draws the parts only)."""
    key = (t.key, plate)
    if key not in _thumbs:
        _thumbs[key] = demo_art.render(_parts(t), width=360, with_plate=plate)
    return _thumbs[key]


def pick(t):
    if t.key not in _picks:
        _picks[t.key] = job_previews.save(demo_art.render_pick(_parts(t), part_ids(t)))
    return _picks[t.key]


def _bed(t, kind):
    if kind == "creality" and t.bed == "Textured PEI Plate":
        return "Epoxy Resin Plate" if t.ftype(0) == "PLA" else t.bed
    return t.bed


def filaments(t, kind, grams, model=None):
    out = []
    for i, (ftype, color, share) in enumerate(t.filaments):
        base = t.ftype(i)
        g = round(grams * share, 2)
        f = {"type": base, "color": "#" + color, "grams": g, "meters": round(g / GRAMS_PER_M[base], 2),
             "nozzle_temp": NOZZLE_TEMP[base]}
        profile = PROFILE[kind][ftype]
        f["profile"] = profile + (" @BBL " + model if kind == "bambu" and model else "")
        out.append(project_summary._clean(f))
    return out


def project(t, kind, scale=1.0, model=None, size_mm=None):
    """The job card's summary (project_summary's keys) of this template sliced for `kind`."""
    grams = round(t.grams * scale, 2)
    objects = [{"name": n, "count": c} for n, c in _counted(t)]
    fils = filaments(t, kind, grams, model)
    bed, _ = project_summary._BEDS.get(_bed(t, kind), (None, None))
    s = {
        "title": project_summary._title(objects),
        "time_s": int(round(t.time_s * scale)),
        "grams": grams,
        "filaments": fils,
        "objects": objects,
        "nozzle_mm": 0.4,
        "layer_mm": t.layer,
        "first_layer_mm": 0.2 if t.layer != 0.2 else None,
        "infill": "%g%%" % t.infill,
        "infill_pattern": project_summary._PATTERNS.get(t.pattern, t.pattern),
        "walls": t.walls,
        "supports": project_summary._SUPPORTS.get(t.support, "есть") if t.support else "нет",
        "bed": bed,
        "bed_temp": t.bed_temp,
        "ironing": True if t.ironing else None,
        "timelapse": True if t.timelapse and kind == "bambu" else None,
        "nozzle_temp": fils[0].get("nozzle_temp"),
        "slicer": SLICER[kind],
    }
    if kind == "bambu" and t.count > 1:
        s["parts"] = [{"id": i, "name": n} for i, n in zip(part_ids(t), part_names(t))]
        s["skippable"] = True
        s["pick"] = pick(t)
    if kind == "creality":
        s["size_mm"] = size_mm or [round(150 + 7 * len(t.name)), round(110 + 5 * t.count), round(12 + t.time_s / 1800)]
    if t.support and kind != "bambu":
        s["support_grams"] = round(grams * 0.06, 1)
    return project_summary._clean(s)


def _counted(t):
    seen = {}
    for name, _, count in t.objects:
        seen[name] = seen.get(name, 0) + count
    return list(seen.items())


_projects = {}


def project_key(t, kind, scale=1.0, model=None):
    key = (t.key, kind, round(scale, 3), model)
    if key not in _projects:
        _projects[key] = job_previews.save_project(project(t, kind, scale, model))
    return _projects[key]


def creality_listing_item(t, path, size, mtime, scale=1.0):
    """One retGcodeFileInfo2 entry of the file (what a K1C / K2 sends): project_summary.creality reads it."""
    grams = t.grams * scale
    return {
        "path": path, "name": path.rsplit("/", 1)[-1], "file_size": size, "create_time": mtime,
        "timeCost": int(t.time_s * scale), "material": ";".join(t.ftype(i) for i in range(len(t.filaments))),
        "materialColors": ";".join("#" + c for c in t.colors),
        "filamentWeight": ";".join("%.2f" % (grams * share) for _, _, share in t.filaments),
        "consumables": int(grams / GRAMS_PER_M[t.ftype(0)] * 1000) if not t.multi else None,
        "nozzleTemp": NOZZLE_TEMP[t.ftype(0)] * 100, "bedTemp": (t.bed_temp or 60) * 100,
        "nozzle_diameter_from_file": "0.4", "printer_software_from_file": "Creality_Print_6.2.3",
        "thumbnail": "/mnt/UDISK/creality/local_gcode/humbnail/" + path.rsplit("/", 1)[-1].rsplit(".", 1)[0] + ".png",
    }
