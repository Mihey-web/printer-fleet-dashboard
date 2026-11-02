"""A synthetic farm for looking at the panel: no printers, no network.

    cd <repo root> && python docs/dev/themes-2026-11/demo_farm.py [--port 8765]

Then open http://127.0.0.1:8765/ and sign in as admin / demo-pass-123.

The four Bambu printers come from the captured MQTT reports in tests/fixtures/bambu,
replayed through the vendored pybambu and normalize_bambu exactly as the golden tests
do. The rest of the farm is cloned from them and from hand-made Creality / Klipper /
MKS statuses, in the states worth looking at: printing, paused with an error,
finished, cancelled, idle, error, offline, maintenance, AMS drying. The history
(24 h of rows for the timeline, the temperatures, the AMS humidity) and the print
journal are written with a fixed seed, so every run looks the same.

Everything is written to the repository root's printer_history.db, ams_history.db
and users.db, as a real install does: run it in a scratch checkout (git worktree),
not in the one you keep data in. Every 2 s the live statuses move a little
(temperatures, progress), like a farm does, so the cards re-render as they do live.
"""
import argparse
import copy
import dataclasses
import os
import random
import sqlite3
import sys
import threading
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path[:0] = [ROOT, os.path.join(ROOT, "tests")]
os.chdir(ROOT)
os.environ.setdefault("FORGE_ADMIN_PASSWORD", "demo-pass-123")
os.environ.setdefault("COOKIE_SECURE", "0")
os.environ.setdefault("FORGE_JWT_SECRET", "demo-farm-secret-not-for-production-use-0123456789")

import bambu_testkit as kit  # noqa: E402
import uvicorn  # noqa: E402
from app.api.main import create_app  # noqa: E402
from app.collectors.bambu_collector import BambuCollector  # noqa: E402
from app.domain.models import PrinterKind, PrinterState, PrinterStatus  # noqa: E402
from app.services import notifications, printer_registry  # noqa: E402
from app.services.ams_store import AmsStore  # noqa: E402
from app.services.normalizer import normalize_bambu  # noqa: E402
from app.services.state_store import StateStore  # noqa: E402

NOW = time.time()
RNG = random.Random(20261102)


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


def build_farm():
    base = {m: replay(m) for m in ("X1C", "P2S", "H2S", "P1S")}
    p2s_ams = base["P2S"].ams
    farm = []

    def add(src, pid, label, **kw):
        kw = dict({"last_update_ts": NOW, "last_successful_fetch": NOW}, **kw)
        s = dataclasses.replace(src, id=pid, label=label, **kw)
        farm.append(s)
        return s

    # printing ------------------------------------------------------------------------
    add(base["X1C"], "x1c-1", "X1C · Цех 1", progress_pct=62.0, job_name="Корпус_пульта_v2",
        eta_seconds=4 * 3600 + 12 * 60, current_layer=186, total_layers=300)
    add(base["P2S"], "p2s-1", "P2S · Цех 1", progress_pct=34.0, job_name="Кронштейн_полки_x8",
        eta_seconds=2 * 3600 + 5 * 60, current_layer=41, total_layers=120,
        ams=with_slots(p2s_ams, [slot("PLA", "2DD4BF", "Bambu PLA Basic", 78), slot("PLA", "F5F5F5", "Bambu PLA Matte", 42),
                                 slot("PETG", "1F2937", "Bambu PETG HF", 91), slot("PLA", "F97316", "Bambu PLA Basic", 12)],
                       humidity=22, tray_now=0))
    add(base["H2S"], "h2s-1", "H2S · Лаборатория")
    add(base["P1S"], "p1s-1", "P1S · Цех 2", progress_pct=81.0, job_name="Шестерня_z24",
        eta_seconds=38 * 60, current_layer=203, total_layers=250)
    add(base["P2S"], "p2s-3", "P2S · Сушка", progress_pct=12.0, job_name="Петли_двери",
        eta_seconds=5 * 3600, current_layer=9, total_layers=210,
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
    add(base["P2S"], "p2s-2", "P2S · Цех 2", state=PrinterState.PAUSED, progress_pct=57.0, job_name="Крышка_редуктора",
        eta_seconds=3 * 3600, current_layer=88, total_layers=154, nozzle_temp=212.0, target_nozzle_temp=0.0,
        stage="paused_filament_runout", last_error=(pe or {}).get("text") or "Закончился филамент",
        notifications=pause_n + list(base["P2S"].notifications),
        ams=with_slots(p2s_ams, [slot("PLA", "EF4444", "Bambu PLA Basic", 0), slot("PLA", "FACC15", "Bambu PLA Basic", 64),
                                 EMPTY, slot("PLA", "A855F7", "Bambu PLA Silk", 30)], humidity=58, tray_now=0))

    # finished / cancelled / idle ------------------------------------------------------
    add(base["X1C"], "x1c-2", "X1C · Цех 2", state=PrinterState.FINISHED, progress_pct=100.0, job_name="Табличка_склад",
        eta_seconds=0, current_layer=140, total_layers=140, nozzle_temp=41.0, bed_temp=38.0, target_nozzle_temp=0.0,
        target_bed_temp=0.0, stage="idle", notifications=[], last_error=None)
    add(base["H2S"], "h2s-2", "H2S · Офис", state=PrinterState.CANCELLED, progress_pct=23.0, job_name="Органайзер_v3",
        eta_seconds=None, nozzle_temp=66.0, bed_temp=52.0, target_nozzle_temp=0.0, target_bed_temp=0.0, stage="idle",
        notifications=[], last_error=None,
        ams=with_slots(base["H2S"].ams, [slot("PLA", "22C55E", "Bambu PLA Basic", 88), slot("PLA", "0EA5E9", "Bambu PLA Basic", 47),
                                          slot("PLA", "FFFFFF", "Bambu PLA Basic", 95), slot("TPU", "111827", "Generic TPU", 70)],
                       humidity=18))
    add(PrinterStatus(id="k1-1", label="K1 Max · Офис", kind=PrinterKind.CREALITY, online=True, state=PrinterState.IDLE,
                      nozzle_temp=27.0, bed_temp=25.0, target_nozzle_temp=0.0, target_bed_temp=0.0, device_type="K1 Max",
                      light_on=False, fan_speed_pct=0), "k1-1", "K1 Max · Офис")

    # Klipper: printing and an error ----------------------------------------------------
    add(PrinterStatus(id="v-1", label="Voron 2.4", kind=PrinterKind.KLIPPER, online=True, state=PrinterState.PRINTING,
                      progress_pct=46.0, job_name="Крепление_камеры.gcode", eta_seconds=95 * 60, print_time_seconds=80 * 60,
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


MODEL = {"x1c": "X1C", "p2s": "P2S", "h2s": "H2S", "p1s": "P1S"}


def seed_registry(farm):
    printer_registry.init_db()
    conn = sqlite3.connect(printer_registry.DB_PATH)
    conn.execute("DELETE FROM printers")
    for s in farm:
        conn.execute("INSERT INTO printers (id, kind, label, host, port, model, access_code, serial, deleted,"
                     " created_at, updated_at) VALUES (?, ?, ?, ?, NULL, ?, NULL, NULL, 0, ?, ?)",
                     (s.id, s.kind.value, s.label, "192.0.2.%d" % (10 + len(s.id)), s.device_type, NOW - 86400 * 30, NOW))
    conn.commit()
    conn.close()
    printer_registry.set_maintenance("x1c-3", True, note="Замена сопла и чистка экструдера", by="admin",
                                     now=NOW - 2 * 3600)


# A day of history: segments of (state, minutes), chosen per printer with a fixed seed.
PLANS = {
    "x1c-1": [("idle", 120), ("printing", 420), ("finished", 40), ("idle", 60), ("printing", 800)],
    "p2s-1": [("printing", 300), ("finished", 90), ("printing", 500), ("idle", 200), ("printing", 350)],
    "h2s-1": [("idle", 600), ("printing", 380), ("cancelled", 30), ("idle", 420), ("printing", 10)],
    "p1s-1": [("printing", 700), ("finished", 60), ("idle", 100), ("printing", 580)],
    "p2s-3": [("idle", 400), ("printing", 600), ("finished", 120), ("printing", 320)],
    "p2s-2": [("printing", 380), ("paused", 45), ("printing", 600), ("paused", 415)],
    "x1c-2": [("printing", 900), ("finished", 540)],
    "h2s-2": [("idle", 300), ("printing", 820), ("error", 20), ("idle", 200), ("cancelled", 100)],
    "k1-1": [("printing", 380), ("finished", 120), ("idle", 940)],
    "v-1": [("idle", 200), ("printing", 600), ("finished", 60), ("printing", 580)],
    "e-1": [("printing", 520), ("error", 920)],
    "mks-1": [("idle", 900), ("offline", 540)],
    "p1s-2": [("printing", 610), ("offline", 830)],
    "x1c-3": [("printing", 820), ("idle", 500), ("maintenance", 120)],
}
STEP = 80  # s: a row every poll


def seed_history(store, farm):
    by_id = {s.id: s for s in farm}
    rows = []
    for pid, plan in PLANS.items():
        s = by_id[pid]
        t = NOW - 24 * 3600
        for state, minutes in plan:
            end = min(t + minutes * 60, NOW)
            while t < end:
                online = state != "offline"
                printing = state in ("printing", "paused")
                nozzle = (220 + RNG.uniform(-1.5, 1.5)) if printing else (30 + RNG.uniform(-1, 1)) if online else None
                bed = (65 + RNG.uniform(-0.6, 0.6)) if printing else (27 + RNG.uniform(-0.5, 0.5)) if online else None
                rows.append((pid, s.label, s.kind.value, int(online), state, None, None, None, None, nozzle, bed,
                             None, None, None, s.device_type, t, 0, t,
                             36.0 if printing and s.kind == PrinterKind.BAMBU else None,
                             220.0 if printing else 0.0, 65.0 if printing else 0.0, None))
                t += STEP
    conn = store._db
    conn.execute("DELETE FROM printer_history")
    conn.executemany(
        "INSERT INTO printer_history (printer_id, label, kind, online, state, progress, job_name, eta_sec, print_time,"
        " nozzle_temp, bed_temp, layer, total_layer, last_error, device_type, recorded_at, grace_period_active,"
        " last_successful_fetch, chamber_temp, target_nozzle_temp, target_bed_temp, target_chamber_temp)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    seed_jobs(conn, by_id)
    conn.commit()


JOB_NAMES = ["Корпус_пульта_v2", "Кронштейн_полки_x8", "Шестерня_z24", "Табличка_склад", "Органайзер_v3",
             "Петли_двери", "Крышка_редуктора", "Крепление_камеры", "Ручка_ящика_x4", "Заглушка_трубы_x12"]


def seed_jobs(conn, by_id):
    conn.execute("DELETE FROM print_jobs")
    for pid, plan in PLANS.items():
        s = by_id[pid]
        t = NOW - 24 * 3600
        for i, (state, minutes) in enumerate(plan):
            start, end = t, t + minutes * 60
            t = end
            if state != "printing":
                continue
            nxt = plan[i + 1][0] if i + 1 < len(plan) else None
            open_job = end >= NOW - 1
            result = None if open_job else {"finished": "ok", "cancelled": "cancelled", "error": "failed",
                                            "paused": None, "offline": "unknown"}.get(nxt, "ok")
            if result is None and not open_job:
                continue
            name = RNG.choice(JOB_NAMES)
            conn.execute(
                "INSERT INTO print_jobs (printer_id, kind, model, job_name, started_at, start_exact, ended_at, end_exact,"
                " last_seen, result, planned_sec, planned_src, actual_sec, end_progress, source, updated_at)"
                " VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, 'slicer', ?, ?, 'live', ?)",
                (pid, s.kind.value, s.device_type, s.job_name if open_job and s.job_name else name, start,
                 None if open_job else end, None if open_job else 1, min(end, NOW), result,
                 int(minutes * 60 * RNG.uniform(0.9, 1.1)), None if open_job else int(end - start),
                 None if open_job else (100.0 if result == "ok" else RNG.uniform(10, 90)), NOW))


def seed_ams(farm):
    store = AmsStore()
    conn = store._db
    conn.execute("DELETE FROM ams_history")
    for s in farm:
        if not s.ams:
            continue
        for u in s.ams["units"]:
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    farm = build_farm()
    store = StateStore()
    seed_registry(farm)
    for s in farm:
        store.upsert(s)
    seed_history(store, farm)
    seed_ams(farm)
    threading.Thread(target=jiggle, args=(store,), daemon=True).start()
    uvicorn.run(create_app(store), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
