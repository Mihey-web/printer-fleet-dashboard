"""JobMachine on synthetic samples (spec_job_journal §11.1). Job names are invented."""
import json
import logging
import sqlite3
from types import SimpleNamespace

import pytest

from app.domain.models import PrinterKind, PrinterState, PrinterStatus
from app.services import job_journal as jj
from app.services.job_journal import JobMachine, Sample

NAME = "Деталь A"
NAME2 = "fixture_job"
GENERIC = "/data/Metadata/plate_1.gcode"
PID = "p-test"
T0 = 1_760_000_000.0


# --- generator: seq(seg(...), gap(...), ...) -> [Sample], 80 s apart as in the history ---
def seg(n, state, step=None, **fields):
    return ("seg", n, state, step, fields)


def gap(sec):
    return ("gap", sec)


def _interp(v, i, n):
    if not isinstance(v, tuple):
        return v
    a, b = v
    val = a + (b - a) * (i / (n - 1) if n > 1 else 0)
    return int(round(val)) if isinstance(a, int) and isinstance(b, int) else val


def seq(*segments, step=80, t0=T0, kind="bambu"):
    out, t = [], t0
    for part in segments:
        if part[0] == "gap":
            t += part[1]
            continue
        _, n, state, seg_step, fields = part
        for i in range(n):
            vals = {k: _interp(v, i, n) for k, v in fields.items()}
            vals.setdefault("kind", kind)
            out.append(Sample(ts=t, state=state, **vals))
            t += seg_step or step
    return out


def err(text):
    """err_code / err_text as sample_from_status gets them from last_error."""
    code, txt = jj.parse_error(text)
    return {"err_code": code, "err_text": txt}


VOLATILE = {"last_seen", "end_progress", "layer", "total_layers", "ctx"}


class FakeDB:
    """Applies ops like the storage layer will: rows must end up equal to job.row()."""

    def __init__(self):
        self.rows, self.jobs = {}, []

    def apply(self, ops):
        for op in ops:
            if op.op == "open":
                assert op.job.id is None
                op.job.id = len(self.rows) + 1
                self.rows[op.job.id] = dict(op.fields)
                self.jobs.append(op.job)
            else:
                assert op.job.id in self.rows, op
                self.rows[op.job.id].update(op.fields)

    def check(self):
        for job in self.jobs:
            if job.result is not None:
                assert self.rows[job.id] == job.row()
            else:   # an open print is saved on events and checkpoints only
                assert self.rows[job.id] == job._emitted
                assert {k: v for k, v in self.rows[job.id].items() if k not in VOLATILE} ==                     {k: v for k, v in job.row().items() if k not in VOLATILE}
        assert sum(1 for r in self.rows.values() if r["result"] is None) <= 1   # one open print


def run(samples, m=None, kind="bambu", db=None):
    m = m or JobMachine(PID, kind=kind)
    db = db or FakeDB()
    log = []
    for s in samples:
        ops = m.feed(s)
        db.apply(ops)
        log.extend(ops)
    db.check()
    return SimpleNamespace(m=m, db=db, ops=log, jobs=list(db.jobs), rows=[j.row() for j in db.jobs])


def printing(n, p=(0, 50), e=(6000, 3000), name=NAME, **kw):
    return seg(n, "printing", progress=p, eta=e, name=name, **kw)


# --- scenarios 1-28: each returns the run, the tests assert on it ---
def sc01_finished():
    ss = seq(seg(2, "idle"),
             seg(3, "printing", name=NAME, progress=0),                      # preparation: no ETA yet
             seg(45, "printing", name=NAME, progress=(0, 99), eta=(3600, 60)),
             seg(1, "finished", name=NAME, progress=100, eta=0),
             seg(2, "idle"))
    return run(ss), ss


def test_01_plain_print_finished_exact_slicer_plan():
    r, ss = sc01_finished()
    assert len(r.rows) == 1
    row = r.rows[0]
    assert row["result"] == "finished" and row["start_exact"] == 1 and row["end_exact"] == 1
    assert row["started_at"] == ss[2].ts and row["ended_at"] == ss[50].ts
    assert row["planned_src"] == "slicer" and row["planned_sec"] == 3 * 80 + 3600   # ETA + preparation
    assert row["actual_sec"] == int(ss[50].ts - ss[2].ts)
    assert row["job_name"] == NAME and row["ctx"] is None and row["end_progress"] == 100
    assert [op.op for op in r.ops][0] == "open" and r.ops[-1].op == "close"
    assert row["source"] == "live" and row["kind"] == "bambu"


def sc02_pause():
    ss = seq(seg(1, "idle"), printing(10, p=(0, 30), e=(5000, 3500)),
             seg(5, "paused", progress=30, eta=3500, name=NAME, **err("Ошибка печати 0700_8011: Пруток закончился")),
             printing(10, p=(31, 99), e=(3400, 60)), seg(1, "finished", progress=100, name=NAME))
    return run(ss), ss


def test_02_pause_and_resume():
    r, ss = sc02_pause()
    row = r.rows[0]
    assert row["result"] == "finished" and row["pauses"] == 1 and row["paused_sec"] == 5 * 80
    entry = json.loads(row["pauses_json"])[0]
    assert entry == {"at": int(ss[11].ts), "sec": 400, "code": "0700_8011", "text": "Пруток закончился"}


def sc03_three_pauses():
    pause = lambda **kw: seg(3, "paused", step=30, progress=40, eta=3000, name=NAME, **kw)   # noqa: E731
    ss = seq(seg(1, "idle", step=30), printing(5, step=30),
             pause(**err("Ошибка печати 0700_8011: Пруток закончился")), printing(5, step=30, p=(40, 45)),
             pause(stage="paused_user", reason="stage:paused_user"), printing(5, step=30, p=(45, 50)),
             pause(**err("Ошибка печати 0C00_8009: Маркер стола не найден")), printing(5, step=30, p=(50, 55)),
             step=30)
    return run(ss), ss


def test_03_three_pauses_reasons_in_order():
    r, _ = sc03_three_pauses()
    row = r.rows[0]
    entries = json.loads(row["pauses_json"])
    assert row["pauses"] == 3 and row["result"] is None
    assert [e["code"] for e in entries] == ["0700_8011", "stage:paused_user", "0C00_8009"]
    assert entries[1]["text"] is None and entries[0]["text"] == "Пруток закончился"
    assert row["paused_sec"] == 3 * 3 * 30 and all(e["sec"] == 90 for e in entries)


CANCEL_VARIANTS = [
    ("error", "Ошибка печати 0300_400C: Печать отменена"),
    ("error", "HMS_0500_0600_0002_0070: Обслуживание [serious] | Код ошибки печати: 0x0300400C"),
    ("cancelled", "Ошибка печати 0300_400C"),
]


def sc04_cancel(state, text):
    ss = seq(seg(1, "idle"), printing(10), seg(2, state, name=NAME, **err(text)), seg(2, "idle"))
    return run(ss), ss


@pytest.mark.parametrize("state,text", CANCEL_VARIANTS)
def test_04_cancel(state, text):
    r, ss = sc04_cancel(state, text)
    row = r.rows[0]
    assert len(r.rows) == 1 and row["result"] == "cancelled" and row["error_code"] == "0300_400C"
    assert row["ended_at"] == ss[11].ts and row["end_exact"] == 1


def sc05_late_cancel_code():
    ss = seq(seg(1, "idle"), printing(10), seg(1, "error", name=NAME),
             seg(1, "error", name=NAME, **err("Ошибка печати 0300_400C: Печать отменена")), seg(3, "idle"))
    return run(ss), ss


def test_05_cancel_code_in_the_second_error_sample_amends_stopped():
    r, ss = sc05_late_cancel_code()
    row = r.rows[0]
    kinds = [op.op for op in r.ops]
    assert kinds.count("close") == 1 and kinds.count("amend") == 1
    close = next(op for op in r.ops if op.op == "close")
    assert close.fields["result"] == "stopped"
    assert row["result"] == "cancelled" and row["error_code"] == "0300_400C" and row["error_text"] == "Печать отменена"
    assert row["ended_at"] == ss[11].ts   # the end stays where the print stopped


ERROR_VARIANTS = [
    ("Ошибка печати 0300_4000: Ошибка оси Z", "failed", "0300_4000", "Ошибка оси Z"),
    (None, "stopped", None, None),
    ("HMS_0500_0600_0002_0070: Требуется обслуживание [serious]", "stopped", None, None),
    ("HMS_0C00_0100_0001_0004: Сопло забито [fatal] (см. https://wiki)", "failed", "HMS_0C00_0100_0001_0004",
     "Сопло забито"),
]


def sc06_error(text):
    ss = seq(seg(1, "idle"), printing(10), seg(2, "error", name=NAME, **err(text)), seg(2, "idle"))
    return run(ss), ss


@pytest.mark.parametrize("text,result,code,etext", ERROR_VARIANTS)
def test_06_error_with_and_without_code(text, result, code, etext):
    r, _ = sc06_error(text)
    row = r.rows[0]
    assert (row["result"], row["error_code"], row["error_text"]) == (result, code, etext)


def sc07_power_loss_resumed(offline=3):
    ss = seq(seg(1, "idle"), printing(10, p=(10, 40), e=(5000, 3000)),
             seg(offline, "offline", online=False),
             seg(2, "paused", progress=40, eta=3000, name=NAME, **err("Ошибка печати 0300_8007: Питание пропадало")),
             printing(10, p=(41, 99), e=(2900, 60)), seg(1, "finished", progress=100, name=NAME))
    return run(ss), ss


def test_07_power_loss_then_finished():
    r, ss = sc07_power_loss_resumed()
    row = r.rows[0]
    assert row["result"] == "finished" and row["power_losses"] == 1 and row["offline_n"] == 0
    entry = json.loads(row["pauses_json"])[0]
    assert entry["at"] == int(ss[10].ts) and entry["code"] == "0300_8007"   # from the last printing sample
    assert entry["sec"] == int(ss[16].ts - ss[10].ts) == row["paused_sec"]


def test_07b_long_power_loss_is_a_pause_not_offline():
    r, ss = sc07_power_loss_resumed(offline=12)   # 16 min without data
    row = r.rows[0]
    assert row["result"] == "finished" and row["offline_n"] == 0 and row["offline_sec"] == 0
    assert row["paused_sec"] == int(ss[25].ts - ss[10].ts)


def sc08_power_loss_cancelled(resume_to=None):
    parts = [seg(1, "idle"), printing(10, p=(10, 40)), seg(2, "offline", online=False),
             seg(3, "paused", progress=40, eta=3000, name=NAME, **err("Ошибка печати 0300_8007: Питание пропадало"))]
    if resume_to is not None:
        parts.append(printing(5, p=(40, resume_to)))
    parts += [seg(1, "error", name=NAME, **err("Ошибка печати 0300_400C: Печать отменена")), seg(2, "idle")]
    return run(seq(*parts)), None


def test_08_power_loss_then_cancelled_from_the_pause():
    r, _ = sc08_power_loss_cancelled()
    row = r.rows[0]
    assert row["result"] == "power_loss" and row["error_code"] == "0300_8007"
    assert row["error_text"] == "Питание пропадало" and row["power_losses"] == 1


def test_08b_power_loss_resumed_and_progressed_then_cancelled_stays_cancelled():
    r, _ = sc08_power_loss_cancelled(resume_to=60)
    row = r.rows[0]
    assert row["result"] == "cancelled" and row["error_code"] == "0300_400C" and row["power_losses"] == 1


def sc09_offline_same_print():
    ss = seq(seg(1, "idle"), printing(10, p=(10, 40)), seg(18, "offline", online=False),
             printing(10, p=(45, 80)))
    return run(ss), ss


def test_09_offline_25_min_then_the_same_print():
    r, ss = sc09_offline_same_print()
    assert len(r.rows) == 1
    row = r.rows[0]
    assert row["result"] is None and row["offline_n"] == 1
    assert row["offline_sec"] == int(ss[29].ts - ss[10].ts) == 1520


def sc10_offline_then_idle(back=False):
    parts = [seg(1, "idle"), printing(10, p=(10, 40)), seg(18, "offline", online=False), seg(1, "idle")]
    if back:   # P1S: the first sample after a reconnect is a false idle
        parts.append(printing(3, p=(41, 42)))
    ss = seq(*parts)
    return run(ss), ss


def test_10_offline_25_min_then_idle_is_lost():
    r, ss = sc10_offline_then_idle()
    row = r.rows[0]
    assert row["result"] == "lost" and row["ended_at"] == ss[10].ts and row["end_exact"] == 0


def test_10b_lost_then_the_same_print_back_within_grace_reopens():
    r, ss = sc10_offline_then_idle(back=True)
    assert len(r.rows) == 1
    row = r.rows[0]
    assert row["result"] is None and row["ended_at"] is None and row["blips"] == 1
    assert row["offline_n"] == 1 and row["offline_sec"] == int(ss[30].ts - ss[10].ts)
    assert "reopen" in [op.op for op in r.ops]


def sc11_offline_then_finished():
    ss = seq(seg(1, "idle"), printing(10, p=(10, 80), e=(5000, 1200)), seg(30, "offline", online=False),
             seg(1, "finished", progress=100, name=NAME))
    return run(ss), ss


def test_11_offline_40_min_then_finished():
    r, ss = sc11_offline_then_finished()
    row = r.rows[0]
    assert row["result"] == "finished" and row["end_exact"] == 0
    assert row["ended_at"] == min(ss[41].ts, ss[10].ts + 1200) == ss[10].ts + 1200


def sc12_offline_a_day():
    ss = seq(seg(1, "idle"), printing(5, p=(10, 20)), seg(26, "offline", step=3600, online=False),
             printing(2, p=(30, 31)))
    return run(ss), ss


def test_12_no_data_for_a_day_is_lost():
    r, ss = sc12_offline_a_day()
    assert len(r.rows) == 2
    first, second = r.rows
    assert first["result"] == "lost" and first["ended_at"] == ss[5].ts and first["end_exact"] == 0
    assert second["result"] is None and second["start_exact"] == 0 and second["blips"] == 0
    lost_op = next(op for op in r.ops if op.op == "close")
    assert lost_op.job.id == 1


def test_12b_expire_closes_without_a_sample():
    m = JobMachine(PID, kind="bambu")
    r = run(seq(seg(1, "idle"), printing(5)), m=m)
    assert m.expire(m.job.last_seen + jj.JOBS_MAX_GAP_S) == []
    ops = m.expire(m.job.last_seen + jj.JOBS_MAX_GAP_S + 1)
    r.db.apply(ops)
    assert [op.op for op in ops] == ["close"] and r.db.rows[1]["result"] == "lost" and m.job is None


def sc13_pi_restart():
    ss = seq(seg(1, "idle"), printing(10, p=(10, 30)), gap(180), seg(1, "offline", online=False),
             printing(10, p=(31, 50)))
    return run(ss), ss


def test_13_pi_restart_is_not_a_gap():
    r, _ = sc13_pi_restart()
    assert len(r.rows) == 1
    assert r.rows[0]["offline_n"] == 0 and r.rows[0]["offline_sec"] == 0 and r.rows[0]["result"] is None


def sc14_back_to_back():
    ss = seq(seg(1, "idle"), printing(10, p=(50, 99), e=(3000, 0)), printing(5, p=(0, 3), e=(25000, 24800)))
    return run(ss), ss


def test_14_back_to_back_without_finish():
    r, ss = sc14_back_to_back()
    assert len(r.rows) == 2
    first, second = r.rows
    assert first["result"] == "finished" and first["end_exact"] == 0 and first["ended_at"] == ss[10].ts
    assert second["result"] is None and second["start_exact"] == 0 and second["started_at"] == ss[11].ts
    assert second["job_name"] == NAME and second["planned_sec"] is None   # start unknown: no slicer plan


def sc15_p1s_reconnect():
    ss = seq(seg(1, "idle"), printing(10, p=(60, 75), e=(12000, 9540)), seg(3, "offline", online=False),
             seg(2, "paused", progress=75, eta=9540, name=NAME), seg(1, "printing", progress=0, eta=9540, name=NAME),
             printing(5, p=(76, 78), e=(9400, 9000)))
    return run(ss), ss


def test_15_p1s_progress_reset_after_reconnect_is_the_same_print():
    r, _ = sc15_p1s_reconnect()
    assert len(r.rows) == 1
    assert r.rows[0]["pauses"] == 1 and r.rows[0]["result"] is None and r.rows[0]["end_progress"] == 78


def sc16_idle_blip(long=False):
    if not long:   # live: 90 s idle, the same name and ETA
        ss = seq(seg(1, "idle"), printing(5, p=40, e=5000), seg(3, "idle"), printing(3, p=40, e=(4900, 4850)),
                 step=30)
    else:          # 12 min idle, then the same name with a full ETA
        ss = seq(seg(1, "idle"), printing(5, p=40, e=5000), seg(9, "idle"), printing(3, p=0, e=9000))
    return run(ss), ss


def test_16_idle_blip_reopens():
    r, _ = sc16_idle_blip()
    assert len(r.rows) == 1
    assert r.rows[0]["blips"] == 1 and r.rows[0]["result"] is None and r.rows[0]["ended_at"] is None
    assert [op.op for op in r.ops].count("reopen") == 1


def test_16b_long_idle_is_a_stop_and_a_new_print():
    r, ss = sc16_idle_blip(long=True)
    assert len(r.rows) == 2
    first, second = r.rows
    assert first["result"] == "stopped" and first["ended_at"] == ss[6].ts and first["end_exact"] == 1
    assert second["start_exact"] == 1 and second["blips"] == 0 and second["result"] is None


def sc17_x1c_error_blip():
    # 3 months of history: a Bambu print after FAILED never comes back, the next RUNNING starts from layer 0
    ss = seq(seg(1, "idle"), printing(5, p=(60, 71), e=(20000, 16260)), seg(3, "error", name=NAME),
             seg(1, "printing", progress=0, eta=16200, name=NAME), printing(3, p=(0, 1), e=(16100, 16000)))
    return run(ss), ss


def test_17_bambu_error_is_never_reopened():
    r, ss = sc17_x1c_error_blip()
    assert len(r.rows) == 2 and "reopen" not in [op.op for op in r.ops]
    first, second = r.rows
    assert first["result"] == "stopped" and first["ended_at"] == ss[6].ts and first["end_exact"] == 1
    assert first["blips"] == 0 and first["end_progress"] == 71
    assert second["result"] is None and second["started_at"] == ss[9].ts and second["start_exact"] == 1
    assert second["planned_src"] == "slicer" and second["blips"] == 0


def test_17b_bambu_error_still_takes_a_late_cancel_code():
    ss = seq(seg(1, "idle"), printing(5, p=(60, 71), e=(20000, 16260)), seg(1, "error", name=NAME),
             seg(1, "error", name=NAME, **err("Ошибка печати 0300_400C: Печать отменена")),
             seg(1, "printing", progress=0, eta=16200, name=NAME))
    r = run(ss)
    assert [row["result"] for row in r.rows] == ["cancelled", None]
    assert r.rows[0]["error_code"] == "0300_400C" and r.rows[1]["start_exact"] == 1


def sc18_generic_name():
    ss = seq(seg(1, "idle"), printing(4, p=(0, 2), name=GENERIC), printing(5, p=(3, 10)),
             printing(2, p=(11, 12), name=GENERIC))
    return run(ss), ss


def test_18_generic_name_then_the_real_one():
    r, _ = sc18_generic_name()
    assert len(r.rows) == 1 and r.rows[0]["job_name"] == NAME


def sc19_name_change():
    ss = seq(seg(1, "idle"), printing(5, p=(10, 40)), printing(5, p=(40, 45), name=NAME2))
    return run(ss), ss


def test_19_name_change_mid_print():
    r, ss = sc19_name_change()
    assert len(r.rows) == 2
    first, second = r.rows
    assert first["result"] == "stopped" and first["end_exact"] == 0 and first["ended_at"] == ss[5].ts
    assert second["job_name"] == NAME2 and second["start_exact"] == 0 and second["result"] is None


def sc20_total_layers():
    ss = seq(seg(1, "idle"), printing(3, p=(0, 2), total_layers=813, layer=(0, 2)),
             printing(5, p=(2, 10), total_layers=1832, layer=(3, 150)))
    return run(ss), ss


def test_20_total_layers_echo_is_not_a_boundary():
    r, _ = sc20_total_layers()
    assert len(r.rows) == 1
    assert r.rows[0]["total_layers"] == 1832 and r.rows[0]["layer"] == 150


def sc21_job_seq():
    ss = seq(seg(1, "idle", seq=5), printing(3, p=50, e=3000, seq=5), step=30)
    t = ss[-1].ts + 30
    ss.append(Sample(ts=t, state="printing", progress=1, eta=3000, name=NAME, kind="bambu", seq=6,
                     seq_started_at=t - 10))
    ss += seq(printing(2, p=(1, 2), e=3000, seq=6), t0=t + 30, step=30)
    return run(ss), ss


def test_21_job_seq_boundary_between_polls():
    r, ss = sc21_job_seq()
    assert len(r.rows) == 2
    first, second = r.rows
    assert second["started_at"] == ss[4].ts - 10 and second["start_exact"] == 1
    assert first["result"] == "stopped" and first["ended_at"] <= second["started_at"]


def test_21b_job_seq_reset_is_not_a_boundary():
    ss = seq(seg(1, "idle", seq=7), printing(3, seq=7, p=(10, 12)), printing(3, seq=0, p=(13, 15)), step=30)
    r = run(ss)
    assert len(r.rows) == 1


def sc22_restart_from_ctx():
    first = run(seq(seg(1, "idle", seq=3), printing(5, p=(10, 20), seq=3), step=30))
    stored = dict(first.db.rows[1], id=1)
    m = JobMachine.restore(stored)
    assert m.job.id == 1 and m.seq is None and m.job.lp == 10   # lp as of the last saved checkpoint
    t = first.jobs[0].last_seen + 30
    db = FakeDB()
    db.rows[1], db.jobs = dict(first.db.rows[1]), [m.job]
    r = run(seq(printing(3, p=(21, 22), seq=0), t0=t, step=30), m=m, db=db)
    later = run(seq(printing(1, p=1, e=6000, seq=1, seq_started_at=None), t0=t + 120, step=30), m=m, db=db)
    return r, later


def test_22_seq_after_restart_is_not_a_boundary():
    r, later = sc22_restart_from_ctx()
    assert len(r.rows) == 1 and r.rows[0]["result"] is None
    assert len(later.rows) == 2 and later.rows[0]["result"] == "stopped"   # 0 -> 1 seen live: a boundary


def sc23_grace():
    ss = seq(seg(1, "idle"), printing(5), seg(3, "printing", progress=50, eta=3000, name=NAME, grace=True))
    return run(ss), ss


def test_23_grace_samples_are_ignored():
    r, ss = sc23_grace()
    assert r.jobs[0].last_seen == ss[5].ts
    assert all(r.m.feed(s) == [] for s in ss[6:])


def sc24_clock_back():
    ss = seq(seg(1, "idle"), printing(5))
    t = ss[-1].ts
    ss.append(Sample(ts=t - 7200, state="idle", kind="bambu"))
    ss += seq(printing(2, p=(51, 52)), t0=t + 80)
    return run(ss), ss


def test_24_clock_back_sample_is_skipped():
    r, ss = sc24_clock_back()
    assert len(r.rows) == 1 and r.rows[0]["result"] is None and r.rows[0]["last_seen"] == ss[-1].ts


def sc25_maint():
    ss = seq(seg(1, "idle"), printing(3, p=(10, 12)), printing(1, p=13, maint=True), printing(3, p=(14, 16)))
    return run(ss), ss


def test_25_maintenance_marks_the_print():
    r, _ = sc25_maint()
    assert r.rows[0]["maint"] == 1


def test_25b_maintenance_rows_are_no_data_but_mark_the_print():
    ss = seq(seg(1, "idle"), printing(3, p=(10, 12)), seg(2, "maintenance", maint=True), printing(2, p=(13, 14)))
    r = run(ss)
    assert len(r.rows) == 1 and r.rows[0]["maint"] == 1 and r.rows[0]["offline_n"] == 0


def sc26_mks(end_p):
    ss = seq(seg(1, "idle"), seg(10, "printing", progress=(0, end_p), print_time=(60, 3000)), seg(2, "idle"),
             kind="mks")
    return run(ss, kind="mks"), ss


@pytest.mark.parametrize("end_p,result", [(100, "finished"), (40, "stopped")])
def test_26_mks_idle_ends_by_progress(end_p, result):
    r, ss = sc26_mks(end_p)
    row = r.rows[0]
    assert row["result"] == result and row["ended_at"] == ss[11].ts and row["end_exact"] == 1
    assert row["layer"] is None and row["total_layers"] is None and row["job_name"] is None


def sc27_creality():
    ss = seq(seg(1, "idle"), seg(5, "printing", name=NAME, progress=(1, 5), print_time=(600, 920), eta=3000),
             kind="creality")
    return run(ss, kind="creality"), ss


def test_27_creality_plan_from_the_printer():
    r, _ = sc27_creality()
    row = r.rows[0]
    assert row["planned_src"] == "printer" and row["planned_sec"] == 3600 and row["start_exact"] == 1


def sc28_klipper():
    ss = seq(seg(1, "idle"), seg(2, "printing", name=NAME, progress=(1, 3), print_time=(30, 90)),
             seg(5, "printing", name=NAME, progress=(5, 47), print_time=(150, 1410)),
             seg(1, "cancelled", name=NAME, progress=47), seg(2, "idle"), kind="klipper")
    return run(ss, kind="klipper"), ss


def test_28_klipper_cancelled_and_linear_plan():
    r, _ = sc28_klipper()
    row = r.rows[0]
    assert row["result"] == "cancelled" and row["error_code"] is None
    assert row["planned_src"] == "linear" and row["planned_sec"] == 3000   # 150 s at 5 %


SCENARIOS = [
    sc01_finished, sc02_pause, sc03_three_pauses, sc05_late_cancel_code, sc07_power_loss_resumed,
    sc09_offline_same_print, sc11_offline_then_finished, sc12_offline_a_day, sc13_pi_restart, sc14_back_to_back,
    sc15_p1s_reconnect, sc17_x1c_error_blip, sc18_generic_name, sc19_name_change, sc20_total_layers, sc21_job_seq,
    sc22_restart_from_ctx, sc23_grace, sc24_clock_back, sc25_maint, sc27_creality, sc28_klipper,
    lambda: sc08_power_loss_cancelled(), lambda: sc08_power_loss_cancelled(60), lambda: sc10_offline_then_idle(True),
    lambda: sc16_idle_blip(), lambda: sc16_idle_blip(True), lambda: sc26_mks(100), lambda: sc26_mks(40),
] + [lambda v=v: sc04_cancel(*v) for v in CANCEL_VARIANTS] + [lambda v=v: sc06_error(v[0]) for v in ERROR_VARIANTS]


# --- 29: parse_error ---
@pytest.mark.parametrize("text,expected", [
    (None, (None, None)),
    ("", (None, None)),
    ("Ошибка печати 0300_8007: Питание пропадало", ("0300_8007", "Питание пропадало")),
    ("Ошибка печати 0500_4004", ("0500_4004", None)),
    ("Код ошибки печати: 0x0300400c", ("0300_400C", None)),
    ("HMS_0500_0600_0002_0070: Обслуживание [serious] | Ошибка печати 0700_8011: Пруток закончился",
     ("0700_8011", "Пруток закончился")),
    ("HMS_0C00_0100_0001_0004: Сопло забито [fatal] | HMS_0500_0600_0002_0070: Обслуживание [serious]",
     ("HMS_0C00_0100_0001_0004", "Сопло забито")),
    ("HMS_0500_0600_0002_0070: Обслуживание [serious]", (None, None)),
    ("HMS_0300_0100_0001_0007: Требуется смазка [common] | HMS_0700_2000_0002_0001: unknown [fatal] (см. x)",
     ("HMS_0700_2000_0002_0001", None)),
    ("Klipper остановлен (shutdown): MCU 'mcu' shutdown: Timer too close",
     ("klipper:shutdown", "MCU 'mcu' shutdown: Timer too close")),
    ("Ошибка Klipper: Lost communication with MCU", ("klipper:error", "Lost communication with MCU")),
    ("Ошибка Creality 2/5", ("creality:2/5", "Ошибка Creality 2/5")),
    ("fetch returned None", (None, None)),
])
def test_29_parse_error(text, expected):
    assert jj.parse_error(text) == expected


def test_29b_parse_reason_and_norm_name():
    assert jj.parse_reason("stage:paused_user") == (None, None)
    assert jj.parse_reason("0700_8011: Пруток закончился") == ("0700_8011", "Пруток закончился")
    assert jj.parse_reason("0C00_0100_0001_0004: Сопло забито") == ("HMS_0C00_0100_0001_0004", "Сопло забито")
    assert jj.parse_reason(None) == (None, None)
    assert jj.norm_name(GENERIC) is None and jj.norm_name("Metadata/plate_12.gcode") is None
    assert jj.norm_name("  ") is None and jj.norm_name(NAME) == NAME and jj.norm_name(None) is None
    assert jj.norm_name("/sdcard/plate_1.gcode") == "/sdcard/plate_1.gcode"


# --- 30: names never reach the logs ---
def test_30_no_job_name_in_logs(caplog):
    with caplog.at_level(logging.DEBUG):
        for scenario in SCENARIOS:
            r = scenario()[0]
            for op in r.ops:
                assert NAME not in repr(op) and NAME2 not in repr(op) and NAME not in repr(op.job)
    assert caplog.records, "the machine should log its decisions at DEBUG"
    text = caplog.text
    for name in (NAME, NAME2, GENERIC, "plate_1"):
        assert name not in text
    s = Sample(ts=T0, state="printing", name=NAME)
    assert NAME not in repr(s) and "<set>" in repr(s)


# --- the rest of §4 ---
def _job(**kw):
    job = jj.Job(PID, "bambu", None, T0, 1)
    job.job_name = kw.pop("name", None)
    for k, v in kw.items():
        setattr(job, k, v)
    return job


@pytest.mark.parametrize("job_kw,s_kw,prev_seq,expected", [
    ({"lp": 50, "le": 3000}, {"seq": 6, "progress": 50, "eta": 3000}, 5, "seq"),
    ({"lp": 50, "le": 3000}, {"seq": 0, "progress": 50, "eta": 3000}, 5, None),      # the counter restarted
    ({"lp": 50, "le": 3000}, {"seq": 6, "progress": 50, "eta": 3000}, None, None),   # first value after a restart
    ({"name": NAME}, {"name": NAME2}, None, "name"),
    ({"name": GENERIC}, {"name": NAME}, None, None),
    ({"name": NAME}, {"name": GENERIC}, None, None),
    ({"lp": 96, "le": 100}, {"progress": 3, "eta": 100}, None, "reset_done"),
    ({"lp": 75, "le": 9540}, {"progress": 0, "eta": 9540}, None, None),             # P1S reconnect artifact
    ({"lp": 40, "le": 5000}, {"progress": 0, "eta": 9000}, None, "reset_eta"),
    ({"lp": 40, "le": 5000}, {"progress": 0}, None, None),                          # no ETA: can't tell
    ({"lp": 15, "le": 5000}, {"progress": 1, "eta": 20000}, None, None),            # dropped < 20 points
    ({"lp": 40, "le": 5000}, {"progress": 8, "eta": 20000}, None, None),            # not back near 0
    # the counters start over: no 20-point minimum
    ({"lp": 13, "le": 53280, "layer": 200, "total_layers": 1817},
     {"progress": 0, "eta": 27240, "layer": 0, "total_layers": 228}, None, "reset_layer"),
    ({"lp": 10, "le": 13320, "layer": 34, "total_layers": 838},
     {"progress": 0, "eta": 13320, "layer": 35, "total_layers": 838}, None, None),  # the drop echoes the old layer
    ({"lp": 8, "le": 5000, "layer": 30}, {"progress": 0, "eta": 0, "layer": 0, "total_layers": 0}, None, None),
    ({"lp": 15, "le": 33121, "lpt": 6183}, {"progress": 0, "eta": 39293, "print_time": 0}, None, "reset_time"),
    ({"lp": 15, "le": 33121, "lpt": 6183}, {"progress": 0, "eta": 0, "print_time": 0}, None, None),
    # after a power loss Bambu restarts progress and ETA, the layer counter goes on
    ({"lp": 98, "le": 600, "layer": 1250, "total_layers": 1278, "pl_lp": 98},
     {"progress": 0, "eta": 33600, "layer": 1250}, None, None),
    ({"lp": 99, "le": 0, "layer": 800, "total_layers": 800, "pl_lp": 40},
     {"progress": 0, "eta": 0, "layer": 800}, None, "reset_done"),                # every layer done: a new print
])
def test_new_job_reason_rules(job_kw, s_kw, prev_seq, expected):
    s = Sample(ts=T0 + 80, state="printing", **s_kw)
    assert jj.new_job_reason(_job(**job_kw), s, prev_seq) == expected
    assert jj.same_job(_job(**job_kw), s, prev_seq) is (expected is None)


@pytest.mark.parametrize("lp,le,at,expected", [
    (97, 900, 160, ("finished", T0 + 160)),     # end = min(boundary, last_seen + ETA)
    (97, 30, 400, ("finished", T0 + 130)),
    (80, 90, 400, ("finished", T0 + 190)),      # 1.5 min left
    (40, 3000, 400, ("stopped", T0 + 100)),
    (None, None, 400, ("stopped", T0 + 100)),
    (0, 0, 400, ("stopped", T0 + 100)),         # cut in preparation, ETA still 0
    (40, 0, 400, ("stopped", T0 + 100)),        # ETA 0 at 40 %: frozen telemetry, no countdown
    (40, 3000, 800, ("lost", T0 + 100)),        # back after 700 s on another print
    (97, 900, 5000, ("finished", T0 + 1000)),   # nearly done before the outage
])
def test_classify_split(lp, le, at, expected):
    job = _job(lp=lp, le=le)
    job.last_seen = T0 + 100
    assert jj.classify_split(job, Sample(ts=T0 + at, state="printing")) == expected


def test_back_on_another_print_after_an_outage_is_lost():
    ss = seq(seg(1, "idle"), printing(5, p=(30, 40), e=(6000, 5000)), seg(90, "offline", online=False),
             printing(3, p=(30, 31), e=(4000, 3950), name=NAME2))
    r = run(ss)
    first, second = r.rows
    assert first["result"] == "lost" and first["ended_at"] == ss[5].ts and first["end_exact"] == 0
    assert second["job_name"] == NAME2 and second["start_exact"] == 0 and second["result"] is None


def sc_creality_frozen(restart_name, freeze_n):
    """K1C: the print stops, the telemetry freezes (printing, ETA / print time / layers 0), a new one starts."""
    return seq(seg(1, "idle"),
               seg(6, "printing", name=NAME, progress=(1, 15), print_time=(60, 5400), eta=(40000, 33100)),
               seg(freeze_n, "printing", name=NAME, progress=15, print_time=0, eta=0, layer=0, total_layers=0),
               seg(3, "printing", name=restart_name, progress=(0, 1), print_time=(0, 160), eta=(39300, 39200),
                   layer=(0, 1), total_layers=300),
               kind="creality")


@pytest.mark.parametrize("restart_name,freeze_n", [(NAME2, 20), (NAME, 3)])
def test_frozen_creality_telemetry_ends_the_print(restart_name, freeze_n):
    ss = sc_creality_frozen(restart_name, freeze_n)
    r = run(ss, kind="creality")
    assert len(r.rows) == 2 and "reopen" not in [op.op for op in r.ops]
    first, second = r.rows
    assert first["result"] == "stopped" and first["ended_at"] == ss[7].ts and first["end_exact"] == 1
    assert first["end_progress"] == 15 and first["actual_sec"] == int(ss[7].ts - ss[1].ts)
    assert second["started_at"] == ss[7 + freeze_n].ts and second["start_exact"] == 1
    assert second["job_name"] == restart_name and second["result"] is None


@pytest.mark.parametrize("last_layer,result", [(884, "finished"), (800, "stopped")])
def test_error_without_a_code_after_the_last_layer_is_finished(last_layer, result):
    ss = seq(seg(1, "idle"), printing(10, p=(60, 99), e=(3000, 0), layer=(500, last_layer), total_layers=884),
             seg(1, "error", progress=0, name=NAME), seg(1, "idle"))
    r = run(ss)
    row = r.rows[0]
    assert len(r.rows) == 1 and row["result"] == result and row["error_code"] is None
    assert row["ended_at"] == ss[11].ts and row["end_exact"] == 1


def sc_reset_late():
    # Bambu: the drop sample keeps the old ETA and layer, the new file's values come two samples later
    ss = seq(seg(1, "idle"), printing(5, p=(6, 10), e=(14000, 13320), layer=(20, 34), total_layers=838, name=GENERIC),
             printing(2, p=0, e=13320, layer=35, total_layers=838, name=GENERIC),
             printing(3, p=(0, 1), e=(33360, 33300), layer=0, total_layers=927, name=GENERIC))
    return run(ss), ss


def test_a_reset_is_judged_again_when_the_new_eta_comes_late():
    r, ss = sc_reset_late()
    assert len(r.rows) == 2
    first, second = r.rows
    assert first["result"] == "stopped" and first["ended_at"] == ss[5].ts and first["end_exact"] == 0
    assert (first["end_progress"], first["layer"], first["last_seen"]) == (10, 34, ss[5].ts)
    assert second["started_at"] == ss[6].ts and second["start_exact"] == 0 and second["result"] is None


def test_a_restart_below_20_percent_by_the_layer_counter():
    ss = seq(seg(1, "idle"), printing(5, p=(9, 13), e=(54000, 53280), layer=(150, 200), total_layers=1817),
             printing(3, p=0, e=27240, layer=0, total_layers=228, name=GENERIC))
    r = run(ss)
    assert [row["result"] for row in r.rows] == ["stopped", None]
    assert r.rows[0]["ended_at"] == ss[5].ts and r.rows[1]["started_at"] == ss[6].ts


def sc_power_loss_layers():
    pl = err("Ошибка печати 0300_8007: Питание пропадало")
    ss = seq(seg(1, "idle"), printing(5, p=(94, 98), e=(2600, 600), layer=(1200, 1250), total_layers=1278),
             seg(1, "offline", online=False),
             seg(2, "paused", progress=98, eta=600, layer=1250, total_layers=1278, name=NAME, **pl),
             printing(1, p=98, e=600, layer=1250, total_layers=1278),
             printing(2, p=0, e=33600, layer=1250, total_layers=1278),
             seg(2, "error", progress=0, layer=0, total_layers=1278, name=NAME,
                 **err("Ошибка печати 0300_400C: Печать отменена")))
    return run(ss), ss


def test_power_loss_resume_with_the_layer_counting_on_is_one_print():
    r, _ = sc_power_loss_layers()
    assert len(r.rows) == 1
    row = r.rows[0]
    assert row["result"] == "power_loss" and row["error_code"] == "0300_8007" and row["power_losses"] == 1


def sc_blank_idle(idle_n, idle_name=None, back_p=(82, 83)):
    """A Bambu reconnect: idle samples without a job (no name, all zeros), then the print is back."""
    return seq(seg(1, "idle"), printing(5, p=(20, 24), e=(20000, 19000)),
               seg(idle_n, "idle", name=idle_name, progress=0, eta=0, layer=0, total_layers=0),
               printing(3, p=back_p, e=(4000, 3900)))


def test_the_same_print_back_after_a_long_blank_idle_is_reopened():
    ss = sc_blank_idle(51)   # 68 min
    r = run(ss)
    assert len(r.rows) == 1 and [op.op for op in r.ops].count("reopen") == 1
    row = r.rows[0]
    assert row["result"] is None and row["ended_at"] is None and row["blips"] == 1
    assert row["offline_n"] == 1 and row["offline_sec"] == int(ss[57].ts - ss[5].ts) and row["end_progress"] == 83


@pytest.mark.parametrize("idle_name,back_p", [(NAME, (82, 83)), (None, (0, 1))])
def test_a_long_idle_with_a_job_or_a_restart_stays_two_prints(idle_name, back_p):
    ss = sc_blank_idle(51, idle_name, back_p)
    r = run(ss)
    assert [row["result"] for row in r.rows] == ["stopped", None] and r.rows[1]["blips"] == 0
    assert r.rows[1]["start_exact"] == (1 if back_p[0] <= 5 else 0)


def test_a_print_first_seen_far_into_its_run_has_no_exact_start():
    assert run(seq(seg(1, "idle"), printing(3, p=(62, 63)))).rows[0]["start_exact"] == 0
    # Klipper jumps past the gcode header at once; its print time says it just began
    k = seq(seg(1, "idle"), seg(3, "printing", name=NAME, progress=(10, 12), print_time=(6, 166)), kind="klipper")
    assert run(k, kind="klipper").rows[0]["start_exact"] == 1


# the scenarios above in the log check (30) and the backfill == live check too
SCENARIOS += [
    sc_reset_late, sc_power_loss_layers,
    lambda: (run(sc_creality_frozen(NAME2, 20), kind="creality"), None),
    lambda: (run(sc_creality_frozen(NAME, 3), kind="creality"), None),
    lambda: (run(sc_blank_idle(51)), None), lambda: (run(sc_blank_idle(51, NAME)), None),
]


def test_reset_with_eta_jump_splits_without_an_idle():
    r = run(seq(seg(1, "idle"), printing(5, p=(30, 40), e=(6000, 5000)), printing(3, p=(0, 1), e=(9000, 8950))))
    assert [row["result"] for row in r.rows] == ["stopped", None]
    assert r.rows[1]["start_exact"] == 0 and r.rows[1]["job_name"] == NAME


def test_checkpoint_not_more_often_than_checkpoint_s():
    ss = seq(seg(1, "idle", step=30), printing(121, p=(1, 60), e=(7200, 3600), step=30), step=30)   # one hour
    r = run(ss)
    updates = [op for op in r.ops if op.op == "update"]
    assert len(updates) == 3600 // jj.JOBS_CHECKPOINT_S
    assert all(set(op.fields) <= set(jj.COLUMNS) and "job_name" not in op.fields for op in updates)
    assert {"last_seen", "end_progress", "ctx"} <= set(updates[0].fields)


def test_pause_list_is_capped_counters_are_not():
    parts = [seg(1, "idle"), printing(2, p=1)]
    for i in range(25):
        parts += [seg(1, "paused", progress=1 + i, eta=3000, name=NAME, **err("Ошибка печати 0700_8011")),
                  printing(1, p=2 + i)]
    r = run(seq(*parts))
    row = r.rows[0]
    assert row["pauses"] == 25 and len(json.loads(row["pauses_json"])) == jj.PAUSES_MAX
    assert row["paused_sec"] == 25 * 80


def test_stopped_refined_to_finished_by_a_late_finish():
    ss = seq(seg(1, "idle"), printing(10, p=(80, 97), e=(600, 30)), seg(1, "idle"), seg(1, "finished", progress=100))
    r = run(ss)
    assert r.rows[0]["result"] == "finished" and r.rows[0]["ended_at"] == ss[11].ts


def test_finished_print_is_never_reopened():
    ss = seq(seg(1, "idle"), printing(10, p=(50, 99)), seg(1, "finished", progress=100),
             seg(1, "printing", name=NAME))   # no progress yet: R3 can't tell
    r = run(ss)
    assert len(r.rows) == 2 and r.rows[0]["result"] == "finished" and r.rows[0]["blips"] == 0


def test_split_at_zero_progress_with_zero_eta_is_stopped():
    ss = seq(seg(1, "idle"), printing(3, p=0, e=0), printing(3, p=(0, 1), e=5000, name=NAME2))
    r = run(ss)
    assert [row["result"] for row in r.rows] == ["stopped", None]


def test_job_row_round_trip_through_ctx():
    r = run(seq(seg(1, "idle"), printing(5, p=(10, 20)),
                seg(2, "paused", progress=20, eta=3000, name=NAME, **err("Ошибка печати 0300_8007: Питание"))))
    job = r.jobs[0]
    stored = dict(job.row(), id=job.id, verdict=None, updated_at=T0)
    m = JobMachine.restore(stored)
    assert m.job.row() == job.row() and m.job.ctx() == job.ctx() and m.job.pause_list == job.pause_list
    # the restored print goes on: the pause closes, nothing else changes
    db = FakeDB()
    db.rows[job.id], db.jobs = dict(m.job.row()), [m.job]
    later = run(seq(printing(2, p=(21, 22)), t0=job.last_seen + 80), m=m, db=db)
    assert later.rows[0]["pauses"] == 1 and later.rows[0]["paused_sec"] == 2 * 80 + 80
    assert later.rows[0]["power_losses"] == 1


def test_sample_from_status_live():
    st = PrinterStatus(id="b1", label="P1", kind=PrinterKind.BAMBU, online=True, state=PrinterState.PAUSED,
                       progress_pct=42.5, job_name=f" {NAME} ", eta_seconds=1200, current_layer=10,
                       total_layers=200, stage="paused_user", device_type="P2S",
                       last_error="HMS_0500_0600_0002_0070: Обслуживание [serious]")
    s = jj.sample_from_status(st, T0, maint=True)
    assert (s.ts, s.state, s.progress, s.eta, s.name, s.model, s.kind) == (T0, "paused", 42.5, 1200, NAME, "P2S", "bambu")
    assert s.err_code is None and s.reason == "stage:paused_user" and s.maint and not s.grace and s.seq is None
    st.last_error = "Ошибка печати 0700_8011: Пруток закончился"
    st.grace_period_active = True
    s = jj.sample_from_status(st, T0)
    assert s.reason == s.err_code == "0700_8011" and s.grace


def test_sample_from_row_history():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE h (id INTEGER, printer_id TEXT, kind TEXT, state TEXT, online INTEGER, progress REAL,"
                 " eta_sec INTEGER, print_time INTEGER, layer INTEGER, total_layer INTEGER, job_name TEXT,"
                 " last_error TEXT, device_type TEXT, recorded_at REAL, grace_period_active INTEGER)")
    conn.execute("INSERT INTO h VALUES (1, 'b1', 'bambu', 'error', 1, 47.0, 600, NULL, 12, 300, ?,"
                 " 'Код ошибки печати: 0x0300400C', 'X1C', ?, 0)", (NAME, T0))
    conn.execute("INSERT INTO h VALUES (2, 'b1', 'bambu', 'printing', 1, 48.0, 590, NULL, 13, 300, ?, NULL,"
                 " 'X1C', ?, 1)", (NAME, T0 + 80))
    conn.execute("INSERT INTO h VALUES (3, 'b1', 'bambu', 'maintenance', 0, NULL, NULL, NULL, NULL, NULL, NULL,"
                 " NULL, 'X1C', ?, 0)", (T0 + 160,))
    a, b, c = (jj.sample_from_row(row) for row in conn.execute("SELECT * FROM h ORDER BY id"))
    assert (a.state, a.err_code, a.reason, a.progress, a.eta, a.total_layers, a.name) == (
        "error", "0300_400C", "0300_400C", 47.0, 600, 300, NAME)
    assert b.grace and not a.grace and a.seq is None and a.stage is None
    assert c.maint and not c.online and c.state == "maintenance"
    with_reason = jj.sample_from_row({"state": "paused", "recorded_at": T0, "reason": "stage:paused_user",
                                      "last_error": None})
    assert with_reason.reason == "stage:paused_user" and with_reason.err_code is None
    coded = jj.sample_from_row({"state": "paused", "recorded_at": T0, "reason": "0700_8011: Пруток закончился",
                                "last_error": "HMS_0C00_0100_0001_0004: Сопло забито [fatal]"})
    assert (coded.err_code, coded.err_text) == ("0700_8011", "Пруток закончился")


def test_backfill_and_live_give_the_same_prints():
    """sample_from_row on history-shaped rows == the synthetic run (rule set shared by both modes)."""
    _, ss = sc07_power_loss_resumed()
    rows = [{"recorded_at": s.ts, "state": s.state, "online": int(s.online), "progress": s.progress,
             "eta_sec": s.eta, "print_time": s.print_time, "layer": s.layer, "total_layer": s.total_layers,
             "job_name": s.name, "kind": "bambu", "device_type": None,
             "last_error": ("Ошибка печати %s: %s" % (s.err_code, s.err_text)) if s.err_code else None}
            for s in ss]
    live = run(ss)
    back = run([jj.sample_from_row(row) for row in rows], m=JobMachine(PID, kind="bambu", source="backfill"))
    strip = lambda row: {k: v for k, v in row.items() if k != "source"}   # noqa: E731
    assert [strip(r) for r in back.rows] == [strip(r) for r in live.rows]
    assert back.rows[0]["source"] == "backfill"
