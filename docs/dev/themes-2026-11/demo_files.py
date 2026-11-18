"""«Файлы на принтере» and «Общие файлы по моделям» for the demo farm, without printers.

install() replaces, inside this process only, the functions of app/services/printer_files.py
and app/services/file_pool.py that talk to a printer: the listing, a Bambu file's plates, a
Creality thumbnail, the copy. They answer from a fixed, invented set of files instead
(neutral part names from demo_catalog). Every way out is closed: a start, a delete and a
sync are refused with a demo note, Moonraker's HTTP is cut off, and a Creality thumbnail
never opens a URL. The panel's own pool storage (data/file_pool/<group>/) gets a few files.
"""
import copy
import os
import random
import time
import zlib

from app.clients import moonraker
from app.domain.models import PrinterState
from app.services import creality_models, file_pool, printer_commands, printer_files, printer_registry

import demo_catalog as cat

MSG_DEMO_START = "Демо-ферма: настоящих принтеров нет — задание никуда не отправлено"
MSG_DEMO_DELETE = "Демо-ферма: с принтеров ничего не удаляется"
MSG_DEMO_SYNC = "Демо-ферма: копирование на принтеры выключено — это витрина без настоящих принтеров"

# Bambu filament ids (tray_info_idx) the slicer writes per material
TRAY_IDX = {"PLA": "GFA00", "PETG": "GFG02", "ABS": "GFB00", "ASA": "GFB01", "TPU": "GFU01", "PA-CF": "GFN03"}
# P2 / H2 printers with a USB stick in (the rest keep files in their internal memory only)
WITH_STICK = {"p2s-1", "h2s-1", "h2s-2"}
SAMPLE_BAMBU = "3DBenchy.gcode.3mf"
SAMPLE_CREALITY = "Crealitylogo_PLA_0.2_45m10s.gcode"
# A file with several plates: (its name, the templates on its plates)
KIT = ("Комплект для мастерской", ["organizer", "handle", "hinge"])

FILES = {}       # printer id -> [entry]
PLATES = {}      # (printer id, path) -> [plate]
THUMBS = {}      # (printer id, path) -> template key (Creality's own thumbnail)
_store = None


def _rng(*parts):
    return random.Random(zlib.crc32("/".join(map(str, parts)).encode("utf-8")))


def _hms(seconds):
    h, rest = divmod(int(seconds), 3600)
    return "%dh%dm%ds" % (h, rest // 60, rest % 60)


def _group_files(group, kind, now):
    """The files of one model's printers: [(name, template keys, scale, size, mtime)]."""
    r = _rng("group", group)
    pool = [t for t in cat.CATALOG if kind == "bambu" or group == "creality-k2plus" or not t.multi]
    r.shuffle(pool)
    out = []
    for t in pool[:min(12, len(pool) - 1)]:        # one part left over for the panel's own uploads
        scale = r.choice((0.9, 1.0, 1.0, 1.1))
        size = int((1.3e6 + t.time_s * scale * 380) if kind == "bambu" else (0.4e6 + t.time_s * scale * 1450))
        size += r.randrange(0, 90000)
        mtime = int(now - r.uniform(0.3, 40) * 86400)
        if kind == "bambu":
            name = t.job_name + ".gcode.3mf"
        else:
            name = "%s_%s_%s.gcode" % (t.job_name, t.ftype(0), _hms(t.time_s * scale))
        out.append((name, [t.key], scale, size, mtime))
    if kind == "bambu":
        ts = [cat.BY_KEY[k] for k in KIT[1]]
        out.append((KIT[0] + ".gcode.3mf", KIT[1], 1.0, int(2.1e6 + sum(t.time_s for t in ts) * 380),
                    int(now - 6.5 * 86400)))
    return out


def _storage(printer_id, model, name, i):
    """(path, storage, via) of a Bambu file the way the printer lists it."""
    if model.startswith("X1"):
        return "/mnt/sdcard/" + name, "sdcard", None
    if model.startswith("P1"):
        return "/" + name, "sdcard", None
    if printer_id in WITH_STICK and (model.startswith("H2") or i % 2 == 0):
        return "/" + name, "udisk", "ftps"
    return "/userdata/model/" + name, "emmc", None


def _bambu_plates(keys, scale, model):
    plates = []
    for index, key in enumerate(keys, 1):
        t = cat.BY_KEY[key]
        grams = round(t.grams * scale, 2)
        plates.append({
            "index": index, "time_s": int(t.time_s * scale), "grams": grams,
            "filaments": [{"id": i + 1, "type": t.ftype(i), "color": color, "grams": round(grams * share, 2),
                           "tray_info_idx": TRAY_IDX[t.ftype(i)]}
                          for i, (_, color, share) in enumerate(t.filaments)],
            "title": cat.project_summary._title([{"name": n} for n, _ in cat._counted(t)]),
            "nozzle_mm": 0.4, "thumb": cat.preview(t),
        })
    return plates


def build(farm, now):
    """FILES / PLATES / THUMBS for every Bambu and Creality printer of the farm."""
    groups = {}
    for s in farm:
        prn = {"kind": s.kind.value, "model": s.device_type}
        g = file_pool.group_of(prn)
        if g:
            groups.setdefault(g, []).append(s)
    for g, members in groups.items():
        kind = g.split("-", 1)[0]
        files = _group_files(g, kind, now)
        GROUP_KEYS[g] = {k for _, keys, _, _, _ in files for k in keys}
        for s in members:
            r = _rng("printer", s.id)
            mine = list(files) if len(members) == 1 else [f for f in files if r.random() < 0.72]
            mine = mine[:15] if len(mine) >= 6 else files[:max(6, len(mine))]
            model = str(s.device_type or "").upper()
            entries = []
            for i, (name, keys, scale, size, mtime) in enumerate(mine):
                t = cat.BY_KEY[keys[0]]
                if kind == "bambu":
                    path, storage, via = _storage(s.id, model, name, i)
                    entries.append(printer_files._entry(path, name, size, mtime, storage, via=via))
                    PLATES[(s.id, path)] = _bambu_plates(keys, scale, model)
                else:
                    root = "/mnt/UDISK/printer_data/gcodes/" if g == "creality-k2plus" else "/usr/data/printer_data/gcodes/"
                    item = cat.creality_listing_item(t, root + name, size, mtime, scale)
                    entry = printer_files.creality_entries([item])[0]
                    if g == "creality-k2plus":
                        entry["mr"] = name          # Klipper's API lists it too: readable, deletable
                    entries.append(entry)
                    THUMBS[(s.id, entry["path"])] = t.key
            if kind == "bambu":
                sample = "/userdata/model/bbl/" + SAMPLE_BAMBU if not model.startswith("P1") else "/" + SAMPLE_BAMBU
                entries.append(printer_files._entry(sample, SAMPLE_BAMBU, 6_412_877, int(now - 200 * 86400),
                                                    "emmc" if sample.startswith("/userdata") else "sdcard", demo=True))
            else:
                root = "/mnt/UDISK/printer_data/gcodes/" if g == "creality-k2plus" else "/usr/data/printer_data/gcodes/"
                entries.append(printer_files._entry(root + SAMPLE_CREALITY, SAMPLE_CREALITY, 2_318_406,
                                                    int(now - 220 * 86400), demo=True))
            entries.sort(key=lambda e: (bool(e.get("demo")), -(e["mtime"] or 0)))
            FILES[s.id] = entries


# --- the replaced functions ----------------------------------------------------------------

def _busy(printer_id):
    st = _store.get_one(printer_id) if _store is not None else None
    if st is None or not st.online:
        return printer_files.MSG_OFFLINE
    return printer_files.MSG_BUSY if st.state in (PrinterState.PRINTING, PrinterState.PAUSED) else None


def list_files(printer_id):
    prn = printer_files._printer(printer_id)
    kind = (prn or {}).get("kind")
    if kind not in ("bambu", "creality"):
        raise LookupError(printer_files.MSG_NOT_SUPPORTED)
    st = _store.get_one(printer_id)
    if st is None or not st.online:
        raise LookupError(printer_files.MSG_OFFLINE)
    files = copy.deepcopy(FILES.get(printer_id, []))
    with printer_files._lock:
        printer_files._listings[printer_id] = {"at": time.monotonic(), "files": {f["path"]: f for f in files}}
    hidden = sum(1 for f in files if f.get("demo")) if printer_files.hide_samples() else 0
    if hidden:
        files = [f for f in files if not f.get("demo")]
    return {"kind": kind, "files": files, "hidden_samples": hidden,
            "klipper": printer_files.has_klipper_api(prn, wait=False)}


def file_detail(printer_id, path):
    entry = printer_files.listed(printer_id, path)
    if entry is None:
        raise LookupError(printer_files.MSG_UNKNOWN_FILE)
    if (printer_files._printer(printer_id) or {}).get("kind") != "bambu":
        return {"entry": entry}
    plates = PLATES.get((printer_id, path))
    if plates is None:                      # the firmware's own sample: the printer keeps it to itself
        raise LookupError(printer_files.MSG_UNREADABLE)
    return {"plates": copy.deepcopy(plates), "entry": entry}


def creality_thumb(printer_id, path):
    key = THUMBS.get((printer_id, path))
    if key is None or printer_files.listed(printer_id, path) is None:
        raise LookupError("Картинки нет")
    return cat.thumb_png(cat.BY_KEY[key], plate=False)


def has_klipper_api(prn, wait=True):
    return bool(prn) and prn.get("kind") == "creality" and creality_models.code_for(prn.get("model") or "") == "k2plus"


def start(printer_id, params):
    return printer_commands._refused(MSG_DEMO_START)


def delete_file(printer_id, path):
    return printer_commands._refused(MSG_DEMO_DELETE)


def target_refusal(prn):
    why = _busy(prn["id"])
    if why == printer_files.MSG_OFFLINE:
        return file_pool.WHY_OFFLINE
    return file_pool.WHY_BUSY if why else None


def storage_refusal(prn):
    if prn.get("kind") != "bambu":
        return None
    if _busy(prn["id"]) == printer_files.MSG_OFFLINE:
        return file_pool.WHY_OFFLINE
    if printer_files._usb_over_ftps(str(prn.get("model") or "").upper()) and prn["id"] not in WITH_STICK:
        return file_pool.WHY_NO_STICK
    return None


def start_sync(plan_id, group=None):
    raise ValueError(MSG_DEMO_SYNC)


def _no_network(*args, **kwargs):
    raise OSError("demo farm: no printers on the network")


# --- the panel's own storage ---------------------------------------------------------------

# How many files each model's pool holds on the panel itself (uploads), and how many days ago
POOL_UPLOADS = {"bambu-X1C": (2.5, 9.0), "bambu-P2S": (4.0,), "bambu-P1S": (12.0,), "creality-k1max": (3.0,),
                "creality-k2plus": (1.5,)}
GROUP_KEYS = {}  # group -> the templates its printers hold


def seed_pool(now):
    """A few files uploaded to the panel itself, per model: parts none of its printers hold yet
    (empty files of a believable size; the demo never reads them)."""
    for group, ages in POOL_UPLOADS.items():
        kind = group.split("-", 1)[0]
        spare = [t for t in cat.CATALOG if t.key not in GROUP_KEYS.get(group, ())
                 and (kind == "bambu" or group == "creality-k2plus" or not t.multi)]
        folder = file_pool.DIRECTORY / group
        folder.mkdir(parents=True, exist_ok=True)
        for t, days in zip(spare, ages):
            if kind == "bambu":
                name, size = t.job_name + ".gcode.3mf", int(1.3e6 + t.time_s * 380)
            else:
                name, size = "%s_%s_%s.gcode" % (t.job_name, t.ftype(0), _hms(t.time_s)), int(0.4e6 + t.time_s * 1450)
            path = folder / name
            if not path.exists() or path.stat().st_size != size:
                with open(path, "wb") as out:
                    out.truncate(size)
            os.utime(path, (now - days * 86400, now - days * 86400))


def install(app, store, farm, now):
    """Patch the printer-facing functions, build the file set, put the detail route in front."""
    global _store
    _store = store
    printer_files.set_store(store)
    moonraker._open = _no_network
    moonraker._connection = _no_network
    printer_files.list_files = list_files
    printer_files.file_detail = file_detail
    printer_files.creality_thumb = creality_thumb
    printer_files.has_klipper_api = has_klipper_api
    printer_files.start = start
    printer_files.delete_file = delete_file
    file_pool._target_refusal = target_refusal
    file_pool.storage_refusal = storage_refusal
    file_pool.start_sync = start_sync
    build(farm, now)
    seed_pool(now)

    from fastapi import Query
    from fastapi.responses import JSONResponse

    def demo_file_detail(printer_id: str, path: str = Query(..., max_length=1024)):
        """The route's own answer, with the AMS of the farm's status instead of a live printer's."""
        try:
            detail = printer_files.file_detail(printer_id, path)
        except LookupError as e:
            return JSONResponse(status_code=409, content={"detail": str(e)})
        prn = printer_registry.get_printer(printer_id) or {}
        st = store.get_one(printer_id)
        if prn.get("kind") == "bambu":
            slots = printer_files.ams_slots(((st.ams if st is not None else None) or {}).get("units") or [])
            detail["slots"] = slots
            for plate in detail.get("plates") or []:
                plate["mapping"] = printer_files.default_mapping(plate.get("filaments") or [], slots)
        detail["refusal"] = _busy(printer_id)
        return detail

    app.add_api_route("/api/admin/printers/{printer_id}/files/detail", demo_file_detail, methods=["GET"])
    app.router.routes.insert(0, app.router.routes.pop())
