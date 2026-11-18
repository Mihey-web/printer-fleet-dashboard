"""A synthetic farm for looking at the panel: no printers, no network.

    cd <repo root> && python docs/dev/themes-2026-11/demo_farm.py [--port 8765]

Then open http://127.0.0.1:8765/ and sign in as admin / demo-pass-123.

The four Bambu printers come from the captured MQTT reports in tests/fixtures/bambu,
replayed through the vendored pybambu and normalize_bambu exactly as the golden tests
do. The rest of the farm is cloned from them and from hand-made Creality / Klipper /
MKS statuses, in the states worth looking at: printing, paused with an error,
finished, cancelled, idle, error, offline, maintenance, AMS drying, a K2 Plus with its
CFS. Everything is written with a fixed seed, so every run looks the same:

* the history: the last 24 h row by row (PLANS: the timeline's shape), 29 days before
  it every 5 min, the AMS humidity of the last 24 h;
* the print journal: 30 days of prints with the slicer's planned grams (History →
  «Пластик»: 7 and 30 days), a few cancelled / failed by the share printed, a few
  without a weight, the ones printing now open;
* the job cards: a plate picture and a project summary per print (docs/dev/themes-2026-11/
  demo_catalog.py: neutral invented parts, pictures drawn by demo_art.py), the plate map
  of «Пропустить деталь» for a Bambu plate with several parts;
* «Файлы на принтере» and «Общие файлы по моделям»: demo_files.py answers in place of the
  printers. A start, a delete and a sync are refused: nothing leaves this process.

Everything is written to the repository root's printer_history.db, ams_history.db,
users.db and data/ (job_previews, file_pool), as a real install does: run it in a
scratch checkout (git worktree), not in the one you keep data in. Every 2 s the live
statuses move a little (temperatures, ETA), like a farm does, so the cards re-render.
"""
import argparse
import copy
import dataclasses
import json
import math
import os
import random
import sqlite3
import sys
import threading
import time
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
sys.path[:0] = [ROOT, os.path.join(ROOT, "tests"), HERE]
os.chdir(ROOT)
os.environ.setdefault("FORGE_ADMIN_PASSWORD", "demo-pass-123")
os.environ.setdefault("COOKIE_SECURE", "0")
os.environ.setdefault("FORGE_JWT_SECRET", "demo-farm-secret-not-for-production-use-0123456789")

import bambu_testkit as kit  # noqa: E402
import uvicorn  # noqa: E402
from app.api.main import create_app  # noqa: E402
from app.collectors.bambu_collector import BambuCollector  # noqa: E402
from app.domain.models import PrinterKind, PrinterState, PrinterStatus  # noqa: E402
from app.services import notifications, printer_commands, printer_registry  # noqa: E402
from app.services.ams_store import AmsStore  # noqa: E402
from app.services.normalizer import normalize_bambu  # noqa: E402
from app.services.state_store import StateStore  # noqa: E402

import demo_catalog as cat  # noqa: E402
import demo_files  # noqa: E402

NOW = time.time()
RNG = random.Random(20261102)
DAY = 86400.0


def rng(*parts):
    """A random stream of its own per purpose / printer: one change does not reshuffle the rest."""
    return random.Random(zlib.crc32("/".join(map(str, parts)).encode("utf-8")))


def replay(model):
    c = BambuCollector("fx", "192.0.2.10", "00000000", kit.FAKE_SERIALS[model], model)
    c.client.client = kit.StubPaho()
    for line in kit.load_fixture(model):
        kit.deliver(c, line["msg"])
    status = normalize_bambu("fx", model, c.fetch(), device_type=model)
    c.close()
    return status


def slot(kind, color, name, remain):
    return {"type": kind, "color": color, "name": name, "remain_pct": remain, "empty": False,
            "nozzle_temp": [190, 230] if kind == "PLA" else [230, 270]}


EMPTY = {"type": None, "color": None, "name": None, "remain_pct": -1, "empty": True, "nozzle_temp": None}


def with_slots(ams, slots, humidity=None, tray_now=None, temp=None):
    ams = copy.deepcopy(ams)
    u = ams["units"][0]
    u["slots"] = slots
    if humidity is not None:
        u["humidity_pct"] = humidity
    if temp is not None:
        u["temp"] = temp
    if tray_now is not None:
        ams["tray_now"] = tray_now
    return ams


def cfs_slot(i, kind, name, color, percent, selected=False):
    return {"id": i, "type": kind, "name": name, "color": "#" + color, "percent": percent, "empty": False,
            "selected": selected}


# What the printing (and the just finished / cancelled) printers print now: the template, the
# job name the printer reports, and the minutes its slicer planned (the card's elapsed + ETA).
LIVE = {
    "x1c-1": ("organizer", 640),
    "p2s-1": ("cable", 190),
    "h2s-1": ("gear", 135),
    "p1s-1": ("planter", 655),
    "p2s-3": ("vent", 330),
    "p2s-2": ("stand", 205),
    "x1c-2": ("lamp", 800),
    "h2s-2": ("wheel", 400),
    "v-1": ("camera", 175),
    "k2-1": ("battery", 540),
}


def live_job(pid, kind, model):
    """(job name, planned grams, preview key, project key, template, scale) of the print on a card."""
    t = cat.BY_KEY[LIVE[pid][0]]
    scale = live_scale(t, LIVE[pid][1])
    grams = round(t.grams * scale, 1)
    name = t.job_name
    if kind == "creality":
        name = "%s_%s_%s.gcode" % (t.job_name, t.ftype(0), demo_files._hms(t.time_s * scale))
    elif kind == "klipper":
        name = t.job_name + ".gcode"
    return name, grams, cat.preview(t), cat.project_key(t, kind, scale, model if kind == "bambu" else None), t, scale


def live_scale(t, minutes):
    scale = minutes * 60.0 / t.time_s
    scale = min(scale, 590.0 / t.grams)          # a plate is 20-600 g
    return round(max(0.5, scale), 2)


def build_farm():
    base = {m: replay(m) for m in ("X1C", "P2S", "H2S", "P1S")}
    p2s_ams = base["P2S"].ams
    farm = []

    def add(src, pid, label, **kw):
        kw = dict({"last_update_ts": NOW, "last_successful_fetch": NOW}, **kw)
        if pid in LIVE:
            kind = (kw.get("kind") or src.kind).value
            name, grams, preview, project, _, _ = live_job(pid, kind, kw.get("device_type") or src.device_type)
            kw.setdefault("job_name", name)
            kw.update(planned_filament_g=grams, preview_key=preview, project_key=project)
        s = dataclasses.replace(src, id=pid, label=label, **kw)
        farm.append(s)
        return s

    # printing ------------------------------------------------------------------------
    add(base["X1C"], "x1c-1", "X1C · Цех 1", progress_pct=62.0, eta_seconds=4 * 3600 + 12 * 60, current_layer=186,
        total_layers=300,
        ams=with_slots(p2s_ams, [slot("PLA", "E5E7EB", "Bambu PLA Matte", 64), slot("PLA", "60A5FA", "Bambu PLA Basic", 81),
                                 slot("PLA", "FACC15", "Bambu PLA Basic", 37), slot("PETG", "1F2937", "Bambu PETG HF", 90)],
                       humidity=19, tray_now=0))
    pick_ids = cat.part_ids(cat.BY_KEY["cable"])
    add(base["P2S"], "p2s-1", "P2S · Цех 1", progress_pct=34.0, eta_seconds=2 * 3600 + 5 * 60, current_layer=41,
        total_layers=120, skipped_objects=[pick_ids[6]],
        ams=with_slots(p2s_ams, [slot("PLA", "2DD4BF", "Bambu PLA Basic", 78), slot("PLA", "F5F5F5", "Bambu PLA Matte", 42),
                                 slot("PETG", "1F2937", "Bambu PETG HF", 91), slot("PLA", "F97316", "Bambu PLA Basic", 12)],
                       humidity=22, tray_now=0))
    add(base["H2S"], "h2s-1", "H2S · Лаборатория", progress_pct=2.0, eta_seconds=2 * 3600 + 8 * 60, current_layer=3,
        total_layers=150,
        ams=with_slots(base["H2S"].ams, [slot("PA-CF", "2B2B2B", "Bambu PA6-CF", 83), slot("PETG", "F5F5F5", "Bambu PETG HF", 56),
                                          EMPTY, EMPTY], humidity=12, tray_now=0))
    add(base["P1S"], "p1s-1", "P1S · Цех 2", progress_pct=81.0, eta_seconds=2 * 3600 + 5 * 60, current_layer=203, total_layers=250)
    add(base["P2S"], "p2s-3", "P2S · Сушка", progress_pct=12.0, eta_seconds=5 * 3600, current_layer=9, total_layers=210,
        ams=dict(with_slots(p2s_ams, [slot("PETG", "60A5FA", "Generic PETG", 66), slot("PETG", "60A5FA", "Generic PETG", 100),
                                      slot("PA-CF", "374151", "Generic PA-CF", 55), EMPTY], humidity=41, tray_now=0, temp=52.0)))
    p2s3 = farm[-1]
    p2s3.ams["units"][0]["dry"] = dict(p2s3.ams["units"][0]["dry"], status="drying", active=True, remaining_min=214,
                                       temp=55, hours=8, filament="PETG")

    # paused with an error --------------------------------------------------------------
    pe = None
    try:
        pe = notifications._print_error(0x07008011, {}, "P2S", None)
    except Exception:
        pe = None
    pause_n = [pe] if pe else []
    add(base["P2S"], "p2s-2", "P2S · Цех 2", state=PrinterState.PAUSED, progress_pct=57.0,
        eta_seconds=88 * 60, current_layer=88, total_layers=154, nozzle_temp=212.0, target_nozzle_temp=0.0,
        stage="paused_filament_runout", last_error=(pe or {}).get("text") or "Закончился филамент",
        notifications=pause_n + list(base["P2S"].notifications),
        ams=with_slots(p2s_ams, [slot("PLA", "EF4444", "Bambu PLA Basic", 0), slot("PLA", "FACC15", "Bambu PLA Basic", 64),
                                 EMPTY, slot("PLA", "A855F7", "Bambu PLA Silk", 30)], humidity=58, tray_now=0))

    # finished / cancelled / idle ------------------------------------------------------
    add(base["X1C"], "x1c-2", "X1C · Цех 2", state=PrinterState.FINISHED, progress_pct=100.0,
        eta_seconds=0, current_layer=140, total_layers=140, nozzle_temp=41.0, bed_temp=38.0, target_nozzle_temp=0.0,
        target_bed_temp=0.0, stage="idle", notifications=[], last_error=None,
        ams=with_slots(p2s_ams, [slot("PLA", "F5F5F5", "Bambu PLA Basic", 21), slot("PLA", "B45309", "Bambu PLA Basic", 48),
                                 EMPTY, slot("PLA", "22C55E", "Bambu PLA Basic", 93)], humidity=24, tray_now=255))
    add(base["H2S"], "h2s-2", "H2S · Офис", state=PrinterState.CANCELLED, progress_pct=23.0,
        eta_seconds=None, nozzle_temp=66.0, bed_temp=52.0, target_nozzle_temp=0.0, target_bed_temp=0.0, stage="idle",
        notifications=[], last_error=None,
        ams=with_slots(base["H2S"].ams, [slot("PLA", "22C55E", "Bambu PLA Basic", 88), slot("PLA", "0EA5E9", "Bambu PLA Basic", 47),
                                          slot("PLA", "FFFFFF", "Bambu PLA Basic", 95), slot("TPU", "111827", "Generic TPU", 70)],
                       humidity=18))
    add(PrinterStatus(id="k1-1", label="K1 Max · Офис", kind=PrinterKind.CREALITY, online=True, state=PrinterState.IDLE,
                      nozzle_temp=27.0, bed_temp=25.0, target_nozzle_temp=0.0, target_bed_temp=0.0, device_type="K1 Max",
                      light_on=False, fan_speed_pct=0), "k1-1", "K1 Max · Офис")
    add(PrinterStatus(id="k1c-1", label="K1C · Цех 3", kind=PrinterKind.CREALITY, online=True, state=PrinterState.IDLE,
                      nozzle_temp=29.0, bed_temp=26.0, target_nozzle_temp=0.0, target_bed_temp=0.0, device_type="k1c",
                      light_on=True, fan_speed_pct=0), "k1c-1", "K1C · Цех 3")

    # Creality K2 Plus printing from its CFS ---------------------------------------------
    cfs = [{"id": 1, "external": False, "temp": 31, "humidity": 34,
            "slots": [cfs_slot(0, "PLA", "Hyper PLA", "F97316", 72, True), cfs_slot(1, "PLA", "Hyper PLA", "1F2937", 58),
                      cfs_slot(2, "PETG", "Hyper PETG", "F5F5F5", 90), cfs_slot(3, "PLA", "CR-PLA Matte", "84CC16", 25)]},
           {"id": 0, "external": True, "temp": None, "humidity": None,
            "slots": [cfs_slot(0, "TPU", "CR-TPU", "111827", 40)]}]
    add(PrinterStatus(id="k2-1", label="K2 Plus · Цех 3", kind=PrinterKind.CREALITY, online=True, state=PrinterState.PRINTING,
                      progress_pct=46.0, eta_seconds=4 * 3600 + 50 * 60, print_time_seconds=250 * 60,
                      nozzle_temp=219.5, bed_temp=60.2, chamber_temp=38.0, target_nozzle_temp=220.0, target_bed_temp=60.0,
                      target_chamber_temp=40.0, chamber_limit=60, current_layer=118, total_layers=256,
                      device_type="k2plus", light_on=True, fan_speed_pct=100, cfs=cfs,
                      feeder_settings={"auto_refill": True}), "k2-1", "K2 Plus · Цех 3")

    # Klipper: printing and an error ----------------------------------------------------
    add(PrinterStatus(id="v-1", label="Voron 2.4", kind=PrinterKind.KLIPPER, online=True, state=PrinterState.PRINTING,
                      progress_pct=46.0, eta_seconds=95 * 60, print_time_seconds=80 * 60,
                      nozzle_temp=249.6, bed_temp=104.8, target_nozzle_temp=250.0, target_bed_temp=105.0,
                      current_layer=112, total_layers=240, device_type="Voron 2.4", ctl_ready=True, light_ctl=True,
                      light_on=True), "v-1", "Voron 2.4")
    kl = notifications.build_klipper({"host_state": "shutdown", "host_message": "MCU 'mcu' shutdown: Timer too close"})
    add(PrinterStatus(id="e-1", label="Ender 3 · Klipper", kind=PrinterKind.KLIPPER, online=True, state=PrinterState.ERROR,
                      nozzle_temp=31.0, bed_temp=29.0, target_nozzle_temp=0.0, target_bed_temp=0.0, device_type="Ender 3",
                      ctl_ready=False, notifications=kl, last_error=notifications.error_line(kl) or "Klipper остановлен"),
        "e-1", "Ender 3 · Klipper")

    # offline -------------------------------------------------------------------------
    add(PrinterStatus(id="mks-1", label="Flying Bear · Склад", kind=PrinterKind.MKS, online=False, state=PrinterState.OFFLINE,
                      device_type="Ghost 6"), "mks-1", "Flying Bear · Склад", last_successful_fetch=NOW - 3 * 3600)
    add(base["P1S"], "p1s-2", "P1S · Склад", online=False, state=PrinterState.OFFLINE, progress_pct=None, job_name=None,
        eta_seconds=None, nozzle_temp=None, bed_temp=None, notifications=[], last_error=None)

    # maintenance (the plaque comes from the registry) ----------------------------------
    add(base["X1C"], "x1c-3", "X1C · Ремонт", state=PrinterState.IDLE, progress_pct=None, job_name=None, eta_seconds=None,
        nozzle_temp=24.0, bed_temp=23.0, target_nozzle_temp=0.0, target_bed_temp=0.0, stage="idle", notifications=[],
        last_error=None)
    return farm


def seed_registry(farm):
    printer_registry.init_db()
    conn = sqlite3.connect(printer_registry.DB_PATH)
    conn.execute("DELETE FROM printers")
    for s in farm:
        conn.execute("INSERT INTO printers (id, kind, label, host, port, model, access_code, serial, deleted,"
                     " created_at, updated_at) VALUES (?, ?, ?, ?, NULL, ?, NULL, NULL, 0, ?, ?)",
                     (s.id, s.kind.value, s.label, "192.0.2.%d" % (10 + len(s.id)), s.device_type, NOW - 86400 * 40, NOW))
    conn.commit()
    conn.close()
    printer_registry.set_maintenance("x1c-3", True, note="Замена сопла и чистка экструдера", by="admin",
                                     now=NOW - 2 * 3600)


# A day of history: segments of (state, minutes), chosen per printer with a fixed seed.
PLANS = {
    "x1c-1": [("idle", 220), ("printing", 520), ("finished", 40), ("idle", 260), ("printing", 400)],
    "p2s-1": [("printing", 300), ("finished", 90), ("printing", 500), ("idle", 485), ("printing", 65)],
    "h2s-1": [("idle", 600), ("printing", 380), ("cancelled", 30), ("idle", 420), ("printing", 10)],
    "p1s-1": [("printing", 700), ("finished", 60), ("idle", 147), ("printing", 533)],
    "p2s-3": [("idle", 400), ("printing", 600), ("finished", 120), ("idle", 280), ("printing", 40)],
    "p2s-2": [("printing", 380), ("finished", 60), ("idle", 700), ("printing", 70), ("paused", 15), ("printing", 47),
              ("paused", 168)],
    "x1c-2": [("printing", 800), ("finished", 640)],
    "h2s-2": [("idle", 300), ("printing", 820), ("error", 20), ("idle", 108), ("printing", 92), ("cancelled", 100)],
    "k1-1": [("printing", 380), ("finished", 120), ("idle", 940)],
    "k1c-1": [("idle", 180), ("printing", 300), ("finished", 60), ("printing", 420), ("finished", 200), ("idle", 280)],
    "k2-1": [("printing", 610), ("finished", 95), ("idle", 485), ("printing", 250)],
    "v-1": [("idle", 200), ("printing", 600), ("finished", 60), ("idle", 500), ("printing", 80)],
    "e-1": [("printing", 520), ("error", 920)],
    "mks-1": [("idle", 900), ("offline", 540)],
    "p1s-2": [("printing", 610), ("offline", 830)],
    "x1c-3": [("printing", 820), ("idle", 500), ("maintenance", 120)],
}
STEP = 80  # s: a row every poll (the last 24 h)
MONTH_STEP = 300  # s: the 29 days before (the history's gap cap is 600 s)
MONTH_DAYS = 30

# The month before: how busy each printer is, whether it feeds several colours.
UTIL = {"x1c-1": 0.8, "x1c-2": 0.75, "x1c-3": 0.6, "p2s-1": 0.85, "p2s-2": 0.75, "p2s-3": 0.7, "h2s-1": 0.7,
        "h2s-2": 0.55, "p1s-1": 0.8, "p1s-2": 0.6, "k1-1": 0.55, "k1c-1": 0.6, "k2-1": 0.75, "v-1": 0.5,
        "e-1": 0.35, "mks-1": 0.06}
MULTI = {"x1c-1", "x1c-2", "x1c-3", "p2s-1", "p2s-2", "p2s-3", "h2s-1", "h2s-2", "k2-1"}
FAILED_BAMBU = ("0300_800A", "0C00_8001", "0300_8003", "0300_8008")
PAUSE_BAMBU = ("0700_8011", "0C00_8001", "0300_8001")
CANCEL_CODE = "0300_400C"
POWER_LOSS = "0300_8007"
KLIPPER_ERR = ("klipper:shutdown", "MCU 'mcu' shutdown: Timer too close")
MAINT_X1C3 = (NOW - 12 * DAY + 3 * 3600, NOW - 12 * DAY + 8 * 3600)


def pe_text(code):
    try:
        r = notifications._print_error(int(code.replace("_", ""), 16), {}, "X1C", None)
        return (r or {}).get("text")
    except Exception:
        return None


def day_load(ts):
    """The farm's load on the local day of `ts`: Sundays are quiet, the rest vary a little."""
    day = time.localtime(ts)
    if day.tm_wday == 6:
        return 0.45
    return rng("load", day.tm_year, day.tm_yday).uniform(0.7, 1.15)


def templates_for(pid):
    return cat.CATALOG if pid in MULTI else cat.SINGLE


def job_material(s, t, scale, r, weight=True):
    """The journal's planned grams / picture / card of a print, as the panel records them."""
    kind = s.kind.value
    if kind == "mks" or not weight:
        return None, None, None
    grams = round(t.grams * scale, 1)
    if kind == "klipper" and r.random() < 0.15:     # a G-code without its thumbnail
        return grams, None, None
    model = s.device_type if kind == "bambu" else None
    return grams, cat.preview(t), cat.project_key(t, kind, scale, model)


def month_jobs(s):
    """29 days of prints before the last 24 h: (jobs, segments [(start, end, state)])."""
    r = rng("month", s.id)
    t0, t1 = NOW - MONTH_DAYS * DAY, NOW - DAY
    util = UTIL.get(s.id, 0.6)
    pool = templates_for(s.id)
    longs = [t for t in pool if t.time_s >= 300 * 60]
    jobs, segs = [], []
    t = t0 + r.uniform(0, 3 * 3600)
    segs.append((t0, t, "idle"))
    after = "idle"
    maint = s.id != "x1c-3"            # x1c-3 stands in maintenance once, 12 days ago
    while True:
        hour = time.localtime(t).tm_hour
        if 1 <= hour < 7 and r.random() < 0.85:      # the night: the next plate goes in in the morning
            morning = time.localtime(t)
            wake = time.mktime((morning.tm_year, morning.tm_mon, morning.tm_mday, 7, 30, 0, 0, 0, -1))
            gap = max(600.0, wake - t) + r.uniform(0, 100 * 60)
        else:                                         # the plate swap, then idle as the printer's and the day's load go
            u = min(0.95, util * day_load(t))
            gap = r.uniform(8, 45) * 60 + r.expovariate(1.0 / (2.6 * 3600 * (1 - u) / u))
            if s.kind == PrinterKind.MKS:              # the old Flying Bear: a print every few days
                gap += r.uniform(2, 4) * DAY
        start = t + gap
        if not maint and start > MAINT_X1C3[0] - 4 * 3600:
            maint = True
            if t < MAINT_X1C3[0]:
                segs.append((t, MAINT_X1C3[0], "idle"))
                segs.append((MAINT_X1C3[0], MAINT_X1C3[1], "maintenance"))
                t, after = MAINT_X1C3[1], "idle"
                continue
        evening = time.localtime(start).tm_hour >= 17
        tpl = r.choice(longs) if evening and longs and r.random() < 0.7 else r.choice(pool)
        scale = r.choice((0.85, 1.0, 1.0, 1.15))
        planned = int(round(tpl.time_s * scale))
        pauses = []
        if r.random() < 0.08:
            code = r.choice(PAUSE_BAMBU) if s.kind == PrinterKind.BAMBU else None
            if code == "0700_8011" and s.id not in MULTI:
                code = "0300_8001"
            sec = int(r.uniform(2, 40) * 60)
            pauses.append({"at": int(start + planned * r.uniform(0.2, 0.8)), "sec": sec, "code": code,
                           "text": pe_text(code) if code else None})
        paused = sum(p["sec"] for p in pauses)
        run = planned * r.uniform(0.97, 1.06) + paused
        x = r.random()
        result, code, text, progress = "finished", None, None, 100.0
        if x < 0.06:
            result, progress = "cancelled", r.uniform(4, 60)
            if s.kind == PrinterKind.BAMBU:
                code, text = CANCEL_CODE, pe_text(CANCEL_CODE)
        elif x < 0.105:
            result, progress = "failed", r.uniform(8, 85)
            if s.kind == PrinterKind.BAMBU:
                code = r.choice(FAILED_BAMBU)
                text = pe_text(code)
            elif s.kind == PrinterKind.KLIPPER:
                code, text = KLIPPER_ERR
        elif x < 0.115:
            result, progress = "stopped", r.uniform(20, 90)
        elif x < 0.12 and s.kind == PrinterKind.BAMBU:
            result, progress, code, text = "power_loss", r.uniform(10, 90), POWER_LOSS, pe_text(POWER_LOSS)
        elif x < 0.13:
            result, progress = "lost", r.uniform(30, 95)
        actual = run if result == "finished" else run * progress / 100.0
        end = start + actual
        if end > t1 - 900:
            break
        weight = r.random() > 0.006                  # started from the printer's screen: no slicer data
        grams, preview, project = job_material(s, tpl, scale, r, weight)
        jobs.append({"name": tpl.job_name, "start": start, "end": end, "result": result, "code": code, "text": text,
                     "planned": planned, "actual": int(actual), "progress": round(progress, 1), "pauses": pauses,
                     "grams": grams, "preview": preview, "project": project, "exact": result != "lost"})
        # the history: what stood between the prints, the print with its pauses, what it left
        hold = {"finished": ("finished", r.uniform(20, 150) * 60), "cancelled": ("cancelled", 20 * 60),
                "failed": ("error", 45 * 60), "stopped": ("idle", 0), "power_loss": ("offline", 25 * 60),
                "lost": ("offline", 80 * 60)}
        state, keep = hold[after] if after in hold else ("idle", 0)
        offline_gap = s.id == "mks-1" and r.random() < 0.5
        if keep:
            segs.append((t, min(start, t + keep), state))
        if t + keep < start:
            segs.append((t + keep, start, "offline" if offline_gap else "idle"))
        cursor = start
        for p in pauses:
            segs.append((cursor, p["at"], "printing"))
            segs.append((p["at"], p["at"] + p["sec"], "paused"))
            cursor = p["at"] + p["sec"]
        segs.append((cursor, end, "printing"))
        t, after = end, result
    state, keep = {"finished": ("finished", 90 * 60), "failed": ("error", 45 * 60)}.get(after, ("idle", 0))
    segs.append((t, min(t1, t + keep), state))
    segs.append((min(t1, t + keep), t1, "idle"))
    return jobs, [(a, b, st) for a, b, st in segs if b > a]


def day_jobs(s, plan):
    """The prints of the last 24 h, from PLANS: a run of printing / paused segments is one print."""
    r = rng("day", s.id)
    t = NOW - DAY
    segs = []
    for state, minutes in plan:
        segs.append((state, t, min(t + minutes * 60, NOW)))
        t += minutes * 60
    jobs, i = [], 0
    last_print = max((k for k, seg in enumerate(segs) if seg[0] == "printing"), default=-1)
    while i < len(segs):
        if segs[i][0] != "printing":
            i += 1
            continue
        j = i
        while j + 1 < len(segs) and segs[j + 1][0] in ("printing", "paused"):
            j += 1
        start, end = segs[i][1], segs[j][2]
        nxt = segs[j + 1][0] if j + 1 < len(segs) else None
        open_job = end >= NOW - 1
        # the card's finished print is the last one of its history
        shown = s.id in LIVE and (open_job or (j >= last_print and s.state in (PrinterState.FINISHED,
                                                                                  PrinterState.CANCELLED)))
        pauses = [{"at": int(a), "sec": None if b >= NOW - 1 else int(b - a), "code": "0700_8011",
                   "text": pe_text("0700_8011")} for st, a, b in segs[i:j + 1] if st == "paused"]
        paused = sum(p["sec"] or int(NOW - p["at"]) for p in pauses)
        result = None if open_job else {"finished": "finished", "cancelled": "cancelled", "error": "failed",
                                        "offline": "lost"}.get(nxt, "finished")
        if shown:
            name, grams, preview, project, tpl, scale = live_job(s.id, s.kind.value, s.device_type)
            planned = int(tpl.time_s * scale)
            progress = 100.0 if result == "finished" else s.progress_pct
        else:
            run = end - start - paused
            pool = templates_for(s.id)
            near = sorted(pool, key=lambda x: abs(math.log(x.time_s / max(600.0, run))))[:3]
            tpl = r.choice(near)
            scale = round(min(max(0.5, run * 0.98 / tpl.time_s), 590.0 / tpl.grams, 2.5) / 0.05) * 0.05
            planned = int(tpl.time_s * scale)
            name = s.job_name if open_job and s.job_name else tpl.job_name
            grams, preview, project = job_material(s, tpl, scale, r)
            progress = 100.0 if result == "finished" else r.uniform(15, 80) if result else s.progress_pct
        code, text = None, None
        if result == "failed":
            code, text = (KLIPPER_ERR if s.kind == PrinterKind.KLIPPER else
                          (r.choice(FAILED_BAMBU), None) if s.kind == PrinterKind.BAMBU else (None, None))
            if code and text is None:
                text = pe_text(code)
        elif result == "cancelled" and s.kind == PrinterKind.BAMBU:
            code, text = CANCEL_CODE, pe_text(CANCEL_CODE)
        jobs.append({"name": name, "start": start, "end": None if open_job else end, "last": min(end, NOW),
                     "result": result, "code": code, "text": text, "planned": planned,
                     "actual": None if open_job else int(end - start), "progress": progress, "pauses": pauses,
                     "grams": grams, "preview": preview, "project": project, "exact": result != "lost",
                     "layer": s.current_layer if open_job else None, "total": s.total_layers if open_job else None})
        i = j + 1
    return jobs


def history_row(s, state, t):
    online = state != "offline"
    printing = state in ("printing", "paused")
    nozzle = (220 + RNG.uniform(-1.5, 1.5)) if printing else (30 + RNG.uniform(-1, 1)) if online else None
    bed = (65 + RNG.uniform(-0.6, 0.6)) if printing else (27 + RNG.uniform(-0.5, 0.5)) if online else None
    return (s.id, s.label, s.kind.value, int(online), state, None, None, None, None, nozzle, bed,
            None, None, None, s.device_type, t, 0, t,
            36.0 if printing and s.kind == PrinterKind.BAMBU else None,
            220.0 if printing else 0.0, 65.0 if printing else 0.0, None)


def seed_history(store, farm):
    by_id = {s.id: s for s in farm}
    rows = []
    month = {}
    for s in farm:
        jobs, segs = month_jobs(s)
        month[s.id] = jobs
        for a, b, state in segs:
            t = a - (a % MONTH_STEP) + (MONTH_STEP if a % MONTH_STEP else 0)
            while t < b:
                rows.append(history_row(s, state, t))
                t += MONTH_STEP
    for pid, plan in PLANS.items():
        s = by_id[pid]
        t = NOW - 24 * 3600
        for state, minutes in plan:
            end = min(t + minutes * 60, NOW)
            while t < end:
                rows.append(history_row(s, state, t))
                t += STEP
    conn = store._db
    conn.execute("DELETE FROM printer_history")
    conn.executemany(
        "INSERT INTO printer_history (printer_id, label, kind, online, state, progress, job_name, eta_sec, print_time,"
        " nozzle_temp, bed_temp, layer, total_layer, last_error, device_type, recorded_at, grace_period_active,"
        " last_successful_fetch, chamber_temp, target_nozzle_temp, target_bed_temp, target_chamber_temp)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    seed_jobs(conn, by_id, month)
    conn.commit()
    return len(rows)


def seed_jobs(conn, by_id, month):
    conn.execute("DELETE FROM print_jobs")
    sql = ("INSERT INTO print_jobs (printer_id, kind, model, job_name, started_at, start_exact, ended_at, end_exact,"
           " last_seen, result, error_code, error_text, planned_sec, planned_src, actual_sec, paused_sec, pauses,"
           " pauses_json, end_progress, layer, total_layers, planned_filament_g, preview_key, project_key, source,"
           " updated_at) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, 'slicer', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'live', ?)")
    rows = []
    for pid, s in by_id.items():
        jobs = month.get(pid, []) + (day_jobs(s, PLANS[pid]) if pid in PLANS else [])
        for j in jobs:
            closed = j["result"] is not None
            end = j["end"]
            pauses = j["pauses"]
            rows.append((pid, s.kind.value, s.device_type, j["name"], j["start"], end,
                         (1 if j["exact"] else 0) if closed else None, j.get("last", end), j["result"],
                         j["code"], j["text"], j["planned"], j["actual"],
                         sum(p["sec"] or 0 for p in pauses), len(pauses),
                         json.dumps(pauses, ensure_ascii=False, separators=(",", ":")) if pauses else None,
                         j["progress"], j.get("layer"), j.get("total"), j["grams"], j["preview"], j["project"], NOW))
    conn.executemany(sql, rows)


def seed_ams(farm):
    store = AmsStore()
    conn = store._db
    conn.execute("DELETE FROM ams_history")
    for s in farm:
        units = list((s.ams or {}).get("units") or [])
        units += [{"index": b["id"], "humidity_pct": b["humidity"], "temp": b["temp"]}
                  for b in s.cfs or [] if not b["external"]]
        for u in units:
            base = u.get("humidity_pct") or 20
            t = NOW - 24 * 3600
            h = base + 12
            while t < NOW:
                h += RNG.uniform(-0.6, 0.5)
                h = max(5, min(80, h))
                conn.execute("INSERT INTO ams_history (printer_id, label, unit_index, humidity_idx, humidity_pct, temp,"
                             " dry_time, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                             (s.id, s.label, u.get("index", 0), u.get("humidity"), round(h), u.get("temp"), 0, t))
                t += 300
    conn.commit()


def jiggle(store):
    """Every 2 s: the live numbers move a little, the cards re-render as they do on a farm."""
    while True:
        time.sleep(2)
        for s in store.get_all():
            if not s.online:
                continue
            s.last_update_ts = s.last_successful_fetch = time.time()
            if s.nozzle_temp is not None:
                s.nozzle_temp = round((s.target_nozzle_temp or s.nozzle_temp) + RNG.uniform(-1.2, 1.2), 1) \
                    if s.state == PrinterState.PRINTING else s.nozzle_temp
            if s.state == PrinterState.PRINTING and s.eta_seconds:
                s.eta_seconds = max(60, s.eta_seconds - 2)


def report(store):
    """The month's plastic per day on the console: a quick look that the charts have something to show."""
    from app.services import job_queries
    st = job_queries.job_stats(os.path.join(ROOT, "printer_history.db"), NOW - MONTH_DAYS * DAY, NOW)
    days = [d["grams"] for d in st["per_day"]]
    print("demo farm: %d prints in %d days, %.1f kg, %.1f-%.1f kg a day, %d without a weight" % (
        st["total"]["jobs"], len(days), st["total"]["grams"] / 1000, min(days) / 1000, max(days) / 1000,
        st["total"]["no_weight"]), flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    farm = build_farm()
    store = StateStore()
    seed_registry(farm)
    for s in farm:
        store.upsert(s)
    rows = seed_history(store, farm)
    seed_ams(farm)
    printer_commands.set_store(store)
    app = create_app(store)
    demo_files.install(app, store, farm, NOW)
    report(store)
    print("demo farm: %d history rows; http://127.0.0.1:%d/ (admin / demo-pass-123)" % (rows, args.port), flush=True)
    threading.Thread(target=jiggle, args=(store,), daemon=True).start()
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
