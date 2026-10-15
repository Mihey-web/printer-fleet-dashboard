"""The auto-dry engine (app/services/ams_autodry_engine.py) against a fake
store, a fake AMS history and a scripted printer_commands.send: what it
sends, journals, audits and persists. Ticks and manual operations get an
explicit `now`; the settings DB lives in tmp_path."""
import copy
import datetime
import json
import logging
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.domain.models import PrinterKind, PrinterState, PrinterStatus
from app.services import ams_autodry as ad
from app.services import ams_autodry_engine as eng
from app.services import ams_drying, printer_commands, printer_registry, settings_service

UTC = datetime.timezone.utc
T0 = datetime.datetime(2026, 10, 12, 9, 0, tzinfo=UTC).timestamp()     # 12:00 MSK
H = 3600


def slot(t, color="FFF144"):
    return {"type": t, "color": color, "name": None, "remain_pct": 80, "empty": False, "nozzle_temp": [220, 260]}


EMPTY = {"type": None, "color": None, "name": None, "remain_pct": None, "empty": True, "nozzle_temp": None}
PETG2 = [slot("PETG"), slot("PETG"), EMPTY, EMPTY]


def make_dry(slots, active=False, remaining=None, status=None, temp=None, filament=None, feeding=False):
    d = {"type": "n3f", "status": status or ("drying" if active else "off"), "phase": None, "active": active,
         "remaining_min": remaining, "temp": temp, "hours": 12 if active else None, "filament": filament,
         "blockers": [], "feeding": feeding}
    d.update(ams_drying._policy("n3f", slots, feeding))
    return d


class Unit:
    """One AMS 2 Pro: live values the fake store reports."""

    def __init__(self, pid, idx=0, rh=30, temp=26.0, slots=PETG2):
        self.pid, self.idx, self.rh, self.temp, self.slots = pid, idx, rh, temp, slots
        self.active, self.remaining, self.status, self.dtemp, self.filament = False, None, None, None, None

    def dry_now(self, rem=500, temp=55, filament="PETG", status="drying"):
        self.active, self.remaining, self.dtemp, self.filament, self.status = True, rem, temp, filament, status

    def stop(self):
        self.active, self.remaining, self.dtemp, self.filament, self.status = False, None, None, None, None

    def as_dict(self):
        return {"index": self.idx, "humidity": 5, "humidity_pct": self.rh, "temp": self.temp,
                "dry_time": self.remaining if self.active else None, "slots": copy.deepcopy(self.slots),
                "dry": make_dry(self.slots, self.active, self.remaining, self.status, self.dtemp, self.filament)}


class FakeStore:
    def __init__(self, units, clock):
        self.units, self.clock = units, clock
        self.online = {}
        self.stale = {}
        self.state = {}
        self.grace = {}

    def _status(self, pid):
        units = [u for u in self.units if u.pid == pid]
        return PrinterStatus(id=pid, label="L-" + pid, kind=PrinterKind.BAMBU, online=self.online.get(pid, True),
                             state=self.state.get(pid, PrinterState.IDLE),
                             last_update_ts=self.clock() - self.stale.get(pid, 5), device_type="P2S",
                             grace_period_active=self.grace.get(pid, False),
                             ams_dry_ok=True, ams={"tray_now": None, "units": [u.as_dict() for u in units]})

    def get_all(self):
        return [self._status(pid) for pid in dict.fromkeys(u.pid for u in self.units)]

    def get_one(self, pid):
        return self._status(pid) if any(u.pid == pid for u in self.units) else None


class FakeHistory:
    """ams_store stand-in: rows every 90 s from each unit's (constant) values; a
    drying shows from the first query that sees it (it just started) on."""

    def __init__(self, units):
        self.units = units
        self.calls = 0
        self.rh = {}
        self.dry_since = {}

    def query(self, pid, idx, fr, to):
        self.calls += 1
        u = next(x for x in self.units if x.pid == pid and x.idx == idx)
        rh = self.rh.get((pid, idx), u.rh)
        if u.active:
            since = self.dry_since.setdefault((pid, idx), to)
        else:
            since = self.dry_since.pop((pid, idx), None)
        out, t = [], to
        while t >= fr:
            drying = u.active and since is not None and t >= since
            out.append({"recorded_at": t, "humidity_idx": 5, "humidity_pct": rh, "temp": u.temp,
                        "dry_time": u.remaining if drying else None})
            t -= 90
        return out[::-1]


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(settings_service, "DB_PATH", str(tmp_path / "settings.db"))
    settings_service.init_db()
    settings_service.reset_cache()
    eng._reset()
    clock = SimpleNamespace(now=T0)
    units = [Unit("bambu-1")]
    store = FakeStore(units, lambda: clock.now)
    history = FakeHistory(units)
    sent, audit = [], []
    results = []           # scripted send results (FIFO), default: confirmed

    def send(pid, action, params):
        sent.append((pid, action, dict(params)))
        r = results.pop(0) if results else {"success": True, "status": "confirmed",
                                            "detail": "подтверждено принтером", "replied": True}
        if callable(r):
            r = r(pid, action, params)
        if r.get("success") and action == "dry_start":
            u = next(x for x in units if x.pid == pid and x.idx == params["ams"])
            u.dry_now(rem=params["hours"] * 60, temp=params["temp"], filament=params["filament"])
        if r.get("success") and action == "dry_stop":
            u = next(x for x in units if x.pid == pid and x.idx == params["ams"])
            u.stop()
        return r

    monkeypatch.setattr(printer_commands, "send", send)
    monkeypatch.setattr(printer_commands, "live_status", lambda pid, prn=None: store.get_one(pid))
    monkeypatch.setattr(printer_registry, "get_printer", lambda pid: {"id": pid, "kind": "bambu", "model": "P2S"})
    monkeypatch.setattr(printer_registry, "get_label", lambda pid: {"bambu-1": "P1", "bambu-2": "P2"}.get(pid))
    monkeypatch.setattr(eng, "get_audit_service",
                        lambda: SimpleNamespace(log=lambda user, action, *a, **k: audit.append((user, action))))
    printer_commands._capability.clear()
    eng.set_sources(store, history)
    ns = SimpleNamespace(clock=clock, units=units, store=store, history=history, sent=sent, audit=audit,
                         results=results)

    def configure(**kw):
        c = copy.deepcopy(ad.DEFAULT_CONFIG)
        c.update(enabled=True, dry_run=False)
        c.update(kw)
        eng.save_config(ad.validate_config(c))

    def tick(dt=0):
        clock.now += dt
        eng.tick(clock.now)

    ns.configure, ns.tick = configure, tick
    yield ns
    eng._reset()
    printer_commands._capability.clear()
    settings_service.reset_cache()


def stored(key):
    settings_service.reset_cache()
    return settings_service.get(key)


def journal_kinds():
    return [e["kind"] for e in eng.snapshot(eng._last_tick or T0)["journal"]][::-1]


def status_of(key="bambu-1__0"):
    snap = eng.snapshot(eng._last_tick or T0)
    return next(u for u in snap["units"] if u["key"] == key)["status"]


# --- starts -------------------------------------------------------------------------

def test_tick_starts_one_unit(env):
    env.configure()
    env.tick()
    assert env.sent == [("bambu-1", "dry_start", {"ams": 0, "temp": 65, "hours": 12, "filament": "PETG"})]
    assert env.audit == [("auto-dry", "autodry_start:bambu-1:0:65/12/PETG")]
    rows = eng.snapshot(T0)["journal"]
    assert [r["kind"] for r in rows] == ["start"]
    assert rows[0]["text"] == "Прогон 1 · 65° · 12 ч (PETG): влажность ≥25% держалась 2 ч"
    assert rows[0]["label"] == "P1" and rows[0]["name"] == "AMS 1" and rows[0]["rh_before"] == 30
    assert "dk" not in rows[0]
    st = stored("ams_autodry_state")["units"]["bambu-1__0"]
    assert st["phase"] == "running" and st["origin"] == "auto" and st["runs"] == [T0]
    assert [r["kind"] for r in stored("ams_autodry_journal")] == ["start"]
    assert status_of()["kind"] == "starting"
    env.tick(60)
    assert status_of()["kind"] == "dry" and len(env.sent) == 1


def test_default_shadow_mode_sends_nothing_and_dedupes(env):
    # the shipped config: enabled + dry_run
    env.tick()
    assert env.sent == [] and env.audit == []
    rows = eng.snapshot(T0)["journal"]
    assert [r["kind"] for r in rows] == ["dry_run"]
    assert rows[0]["text"] == "Пробный режим: запустили бы прогон 1 · 65° · 12 ч (PETG)"
    assert (rows[0]["rh_before"], rows[0]["t_before"]) == (30, 26)       # what would have triggered it
    for _ in range(180):
        env.tick(60)
    rows = eng.snapshot(env.clock.now)["journal"]
    assert [r["kind"] for r in rows] == ["dry_run"] and rows[0]["count"] in (3, 4)
    assert env.sent == [] and status_of()["kind"] == "dry_run"
    # the banner button: dry_run off -> the next tick sends
    env.configure()
    env.tick(60)
    assert [a for _, a, _ in env.sent] == ["dry_start"]


def test_one_start_per_printer_per_tick(env):
    env.units.append(Unit("bambu-1", idx=1))
    env.configure()
    env.tick()
    assert [(p, a, x["ams"]) for p, a, x in env.sent] == [("bambu-1", "dry_start", 0)]
    assert status_of("bambu-1__1") == {"kind": "blocked", "text": "ждём старта другого блока этого принтера"}
    env.tick(60)
    assert [(p, a, x["ams"]) for p, a, x in env.sent] == [("bambu-1", "dry_start", 0), ("bambu-1", "dry_start", 1)]


def test_two_printers_start_in_the_same_tick(env):
    env.units.append(Unit("bambu-2"))
    env.configure()
    env.tick()
    assert sorted(p for p, a, _ in env.sent if a == "dry_start") == ["bambu-1", "bambu-2"]
    assert not eng._inflight and journal_kinds() == ["start", "start"]


def test_execute_start_exception_releases_claim(env, monkeypatch, caplog):
    env.configure()

    def boom(pid, prn=None):
        raise RuntimeError("live view broke")

    monkeypatch.setattr(printer_commands, "live_status", boom)
    with caplog.at_level(logging.ERROR, logger="app.services.ams_autodry_engine"):
        env.tick()
    assert "bambu-1__0" not in eng._inflight and env.sent == []
    assert any("start bambu-1__0 failed" in r.getMessage() for r in caplog.records)
    assert journal_kinds() == ["refused"]           # internal error: retried after the backoff
    st = eng._states["bambu-1__0"]
    assert st["phase"] == "idle" and st["retry_at"] == T0 + 600
    monkeypatch.setattr(printer_commands, "live_status", lambda pid, prn=None: env.store.get_one(pid))
    env.tick(601)
    assert [a for _, a, _ in env.sent] == ["dry_start"]


def test_replanning_on_a_live_unit_with_unfed_slots_dries_pla_safe(env, monkeypatch):
    # the live view shows the filament pulled out of the feeders: the start still goes,
    # replanned to the PLA-safe minimum (owner, 14.10)
    env.configure()
    empty = Unit("bambu-1", slots=[EMPTY] * 4)
    live = FakeStore([empty], lambda: env.clock.now)
    monkeypatch.setattr(printer_commands, "live_status", lambda pid, prn=None: live.get_one(pid))
    env.tick()
    assert [a for _, a, _ in env.sent] == ["dry_start"]
    assert env.sent[0][2]["temp"] == 45 and env.sent[0][2]["filament"] == "PLA"


def test_execute_start_aborts_when_stopped_meanwhile(env):
    env.configure()
    cfg = eng.load_config()
    obs, _ = eng._collect(T0)
    o = dict(obs["bambu-1__0"], rows=[[r["recorded_at"], r["humidity_pct"], r["dry_time"], r["temp"]]
                                     for r in env.history.query("bambu-1", 0, T0 - 3 * H, T0)], clock_jump=False)
    d = ad.decide(ad.new_state(), o, cfg, T0)
    assert d.actions
    # stop-all lands between the decision and the send: hold set
    eng._states["bambu-1__0"], _ = ad.apply_stop(d.state, T0, 24 * H, "stop_all")
    eng._inflight.add("bambu-1__0")
    eng._status["bambu-1__0"] = d.status
    eng._execute_start("bambu-1__0", d.actions[0], "auto", T0)
    assert env.sent == [] and "bambu-1__0" not in eng._inflight
    assert journal_kinds() == [] and eng._status["bambu-1__0"] == {"kind": "pending"}


def test_live_active_unit_is_not_started(env, monkeypatch):
    env.configure()
    busy = Unit("bambu-1")
    busy.dry_now()
    live = FakeStore([busy], lambda: env.clock.now)
    monkeypatch.setattr(printer_commands, "live_status", lambda pid, prn=None: live.get_one(pid))
    env.tick()
    assert env.sent == [] and journal_kinds() == [] and status_of() == {"kind": "pending"}


def test_concurrent_run_now_and_tick_send_once(env, monkeypatch):
    env.configure()
    gate, entered = threading.Event(), threading.Event()
    sent = []

    def slow_send(pid, action, params):
        sent.append(action)
        entered.set()
        gate.wait(5)
        return {"success": True, "status": "confirmed", "detail": "ok"}

    monkeypatch.setattr(printer_commands, "send", slow_send)
    t = threading.Thread(target=env.tick)
    t.start()
    assert entered.wait(5)
    res = eng.run_now(["bambu-1__0"], now=T0)
    assert res[0]["ok"] is False and res[0]["detail"] == "Блок занят — команда уже выполняется"
    gate.set()
    t.join(5)
    assert sent == ["dry_start"]


def test_tick_leaves_alone_a_unit_run_now_is_sending(env, monkeypatch):
    # the reverse order: run-now claimed the unit first, a tick lands while it waits for the printer
    env.configure()
    gate, entered = threading.Event(), threading.Event()
    sent, res = [], []

    def slow_send(pid, action, params):
        sent.append(action)
        if len(sent) == 1:
            entered.set()
            gate.wait(5)
        return {"success": True, "status": "confirmed", "detail": "ok"}

    monkeypatch.setattr(printer_commands, "send", slow_send)
    t = threading.Thread(target=lambda: res.extend(eng.run_now(["bambu-1__0"], now=T0)))
    t.start()
    assert entered.wait(5)
    env.tick()                                      # the unit is wet enough for an auto start, but claimed
    gate.set()
    t.join(5)
    assert sent == ["dry_start"] and res[0]["ok"] is True
    assert journal_kinds() == ["run_now"]


def test_run_now_results(env):
    env.configure()                                # real mode; run-now also ignores dry_run
    env.units.append(Unit("bambu-2"))
    env.units[1].dry_now()
    env.tick()                                     # bambu-1 starts (auto), bambu-2 is foreign
    eng._states["bambu-1__0"]["seen_active"] = False
    res = eng.run_now(["bambu-1__0", "bambu-2__0", "nope__0", "bad"], now=T0 + 10)
    assert [(r["key"], r["ok"], r["status"], r["detail"]) for r in res] == [
        ("bambu-1__0", False, "skipped", "Уже сушится — пропускаем"),
        ("bambu-2__0", False, "skipped", "Уже сушится — пропускаем"),
        ("nope__0", False, "skipped", "Блок не найден"),
        ("bad", False, "skipped", "Блок не найден")]
    assert res[1]["label"] == "P2" and res[1]["name"] == "AMS 1"
    env.units[0].stop()
    res = eng.run_now(["bambu-1__0"], now=T0 + 20)
    assert res[0]["detail"] == "Уже запущена — ждём подтверждения от AMS"


def test_run_now_sends_clears_the_hold_and_is_not_counted(env):
    env.tick()                                     # shadow mode (default config): run-now still sends
    eng._states["bambu-1__0"].update(hold_until=T0 + 10 * H, hold_reason="manual_stop")
    env.results.append({"success": True, "status": "sent", "detail": "отправлено (без подтверждения)"})
    res = eng.run_now(["bambu-1__0"], now=T0 + 30)
    assert res == [{"key": "bambu-1__0", "label": "P1", "name": "AMS 1", "ok": True, "status": "sent",
                    "detail": "отправлено (без подтверждения)",
                    "plan": {"mode": "auto", "name": "PETG", "filament": "PETG", "temp": 65, "hours": 12,
                             "why": "пресет Bambu для PETG", "warn": False, "limit": None, "empty": False}}]
    assert env.sent == [("bambu-1", "dry_start", {"ams": 0, "temp": 65, "hours": 12, "filament": "PETG"})]
    st = eng._states["bambu-1__0"]
    assert st["origin"] == "now" and st["hold_until"] is None and st["runs"] == []
    assert journal_kinds()[-1] == "run_now"
    assert env.audit == []                          # admin actions are audited by the endpoint


def test_run_now_after_a_stale_repeat_starts_a_new_series(env):
    env.configure()
    eng._states["bambu-1__0"] = ad.load_state({"repeat_pending": True, "repeat_since": T0 - 25 * H, "series": 1,
                                               "last_rest_rh": 23})
    res = eng.run_now(["bambu-1__0"], now=T0)
    assert res[0]["ok"] is True
    st = eng._states["bambu-1__0"]
    assert st["series"] == 1 and st["rh_before"] == 30 and st["origin"] == "now"


def test_run_now_on_a_unit_that_cannot_dry(env, monkeypatch):
    monkeypatch.setattr(ad, "plan_run", lambda *a, **k: None)          # e.g. a drying type we have no table for
    res = eng.run_now(["bambu-1__0"], now=T0)
    assert res[0]["ok"] is False and res[0]["detail"] == "Этот AMS не сушит удалённо" and env.sent == []


def test_run_now_failure_journals_without_backoff(env):
    env.configure()
    env.units[0].rh = 15                            # no auto start
    env.tick()
    env.results.append({"success": False, "status": "rejected", "detail": "принтер отклонил: x"})
    res = eng.run_now(["bambu-1__0"], now=T0 + 30)
    assert res[0]["ok"] is False and res[0]["status"] == "rejected"
    assert journal_kinds() == ["refused"] and eng._states["bambu-1__0"]["retry_at"] is None


def test_stop_all_targets(env):
    env.configure()
    env.units.append(Unit("bambu-2"))
    env.units.append(Unit("bambu-3", rh=15))
    env.units[1].dry_now(rem=300)
    env.tick()                                     # bambu-1: our start; bambu-2: foreign; bambu-3: waits
    env.units[0].stop()                            # bambu-1 not confirmed yet (store shows it idle)
    res = eng.stop_all(None, now=T0 + 30)
    assert sorted((r["key"], r["ok"]) for r in res) == [("bambu-1__0", True), ("bambu-2__0", True)]
    assert sorted(x["ams"] for _, a, x in env.sent if a == "dry_stop") == [0, 0]
    for key in ("bambu-1__0", "bambu-2__0"):
        st = eng._states[key]
        assert st["hold_until"] == T0 + 30 + 24 * H and st["hold_reason"] == "stop_all"
        assert eng._status[key] == {"kind": "pending"}
    res = eng.stop_all(["bambu-3__0", "zzz__1"], now=T0 + 40)
    assert [(r["key"], r["detail"]) for r in res] == [("bambu-3__0", "Не сушится — пропускаем"),
                                                     ("zzz__1", "Блок не найден")]
    for _ in range(10):
        env.tick(60)
    kinds = journal_kinds()
    assert kinds.count("manual_stop") == 2 and "interrupted" not in kinds and "foreign_end" not in kinds
    assert eng._states["bambu-1__0"]["interrupts"] == 0
    assert [a for _, a, _ in env.sent].count("dry_start") == 1


def test_stop_all_status_while_the_stop_lands(env):
    env.configure()
    env.tick()
    env.tick(60)
    env.tick(600)
    res = eng.stop_all(None, now=env.clock.now + 5)
    assert res[0]["ok"] is True
    env.units[0].dry_now(rem=718)                   # the stored status lags: still drying
    env.tick(10)
    assert status_of() == {"kind": "pause", "reason": "stop_all", "until": env.clock.now - 5 + 24 * H,
                           "interrupts": 0}
    env.units[0].stop()
    env.tick(60)
    assert status_of()["kind"] == "cool" and journal_kinds() == ["start", "manual_stop"]


def test_stop_all_waits_for_an_in_flight_claim(env, monkeypatch):
    env.units[0].dry_now()
    env.tick()                                     # foreign
    eng._inflight.add("bambu-1__0")
    threading.Timer(1.0, lambda: eng._inflight.discard("bambu-1__0")).start()
    t = time.monotonic()
    res = eng.stop_all(["bambu-1__0"], now=T0 + 5)
    assert res[0]["ok"] is True and time.monotonic() - t >= 0.9
    monkeypatch.setattr(eng, "CLAIM_WAIT_S", 0.3)
    eng._inflight.add("bambu-1__0")
    res = eng.stop_all(["bambu-1__0"], now=T0 + 6)
    assert res[0]["ok"] is False and res[0]["detail"] == "Блок занят — повторите через несколько секунд"
    eng._inflight.discard("bambu-1__0")


def test_manual_ops_persist_immediately(env):
    env.configure()
    env.units[0].rh = 15
    env.tick()
    env.units[0].dry_now()
    eng.stop_all(["bambu-1__0"], now=T0 + 10)
    assert stored("ams_autodry_state")["units"]["bambu-1__0"]["hold_reason"] == "stop_all"
    eng.release(["bambu-1__0"], now=T0 + 20)
    assert stored("ams_autodry_state")["units"]["bambu-1__0"]["hold_until"] is None
    eng.run_now(["bambu-1__0"], now=T0 + 30)
    assert stored("ams_autodry_state")["units"]["bambu-1__0"]["origin"] == "now"
    eng.note_user_command("bambu-1", "dry_stop", {"ams": 0}, now=T0 + 40)
    assert stored("ams_autodry_state")["units"]["bambu-1__0"]["hold_reason"] == "manual_stop"
    assert [r["kind"] for r in stored("ams_autodry_journal")] == ["manual_stop", "released", "run_now", "manual_stop"]


def test_note_user_command_start_is_not_foreign(env):
    env.configure()
    env.units[0].rh = 15
    env.tick()
    eng.note_user_command("bambu-1", "dry_start", {"ams": 0, "temp": 45, "hours": 1, "filament": "PLA"},
                          now=T0 + 10)
    env.units[0].dry_now(rem=60, temp=45, filament="PLA")
    env.tick(60)
    assert journal_kinds() == ["manual"]
    st = eng._states["bambu-1__0"]
    assert st["origin"] == "manual" and st["runs"] == [] and st["phase"] == "running"
    assert status_of()["kind"] == "dry" and status_of()["origin"] == "manual"


def test_a_drying_going_before_the_first_check_is_dated_from_history(env, monkeypatch):
    # first deploy: the unit has been drying for 8 h (P2 on 12.10 showed «с 15:34» for a drying since 07:48)
    t0 = T0 - 8 * H
    env.units[0].dry_now(rem=240, temp=55)

    def query(pid, idx, fr, to):
        out, t = [], fr - fr % 90
        while t <= to:
            if t >= fr:
                drying = t >= t0
                out.append({"recorded_at": t, "humidity_idx": 5, "humidity_pct": 11 if drying else 13,
                            "temp": 55.0 if drying else 36.0, "dry_time": 240 if drying else None})
            t += 90
        return out

    monkeypatch.setattr(env.history, "query", query)
    env.tick()
    st = eng._states["bambu-1__0"]
    assert st["phase"] == "foreign" and st["foreign_since"] == t0 and st["runs"] == [t0]
    assert (st["t_before"], st["rh_before"]) == (36, 13)
    assert status_of()["kind"] == "foreign" and status_of()["since"] == t0
    row = eng.snapshot(T0)["journal"][0]
    assert row["text"].endswith(" · идёт уже 8 ч") and (row["rh_before"], row["t_before"]) == (13, 36)


def test_note_user_command_converts_fresh_foreign(env):
    env.configure()
    env.units[0].dry_now(rem=60, temp=45, filament="PLA")
    env.tick()                                     # the tick sees the dashboard's start first: foreign
    assert journal_kinds() == ["foreign_start"]
    eng.note_user_command("bambu-1", "dry_start", {"ams": 0, "temp": 45, "hours": 1, "filament": "PLA"},
                          now=T0 + 30)
    rows = eng.snapshot(T0 + 30)["journal"]
    assert [r["kind"] for r in rows] == ["manual"] and rows[0]["text"] == "Запущена с дашборда · 45° · 1 ч (PLA)"
    assert eng._states["bambu-1__0"]["runs"] == []


def test_note_user_command_stop_holds_without_count(env):
    env.configure()
    env.tick()                                     # our auto run
    env.tick(60)
    eng.note_user_command("bambu-1", "dry_stop", {"ams": 0}, now=env.clock.now + 5)
    env.units[0].stop()
    env.tick(60)
    st = eng._states["bambu-1__0"]
    assert st["phase"] == "settle" and st["closed"] and st["interrupts"] == 0
    assert st["hold_reason"] == "manual_stop" and journal_kinds() == ["start", "manual_stop"]
    env.tick(2 * H)
    rows = eng.snapshot(env.clock.now)["journal"]
    assert rows[0]["kind"] == "manual_stop" and rows[0]["text"].endswith(" · без повтора")
    assert rows[0]["rh_after"] == 30 and rows[0]["pending"] is False
    assert [a for _, a, _ in env.sent] == ["dry_start"]


def test_note_user_command_never_raises(env, monkeypatch):
    monkeypatch.setattr(ad, "apply_stop", lambda *a, **k: 1 / 0)
    eng.note_user_command("bambu-1", "dry_stop", {"ams": 0})
    eng.note_user_command("bambu-1", "dry_stop", {})
    eng.note_user_command("bambu-1", "pause", {"ams": 0})


def test_is_busy(env):
    assert eng.is_busy("bambu-1", 0) is False and eng.is_busy("bambu-1", None) is False
    eng._inflight.add("bambu-1__0")
    assert eng.is_busy("bambu-1", 0) is True
    eng._inflight.discard("bambu-1__0")


# --- interruptions, capability, clock, staleness -------------------------------------

def _interrupted_run(env):
    env.tick(60)                                    # start
    env.tick(60)                                    # confirmed
    env.units[0].stop()                             # the AMS goes dark 1 minute in
    env.tick(60)


def test_auto_disable_is_state_only(env):
    env.configure(manual_hold_h=0)
    env.tick()
    env.tick(60)
    env.units[0].stop()
    env.tick(60)
    assert eng._states["bambu-1__0"]["interrupts"] == 1
    env.tick(H + 60)                                # measure: RH 30 -> repeat at once
    env.tick(60)
    env.units[0].stop()
    env.tick(60)
    st = eng._states["bambu-1__0"]
    assert st["disabled_reason"] == "interrupts" and st["interrupts"] == 2
    assert ("auto-dry", "autodry_disable:bambu-1:0") in env.audit
    assert eng.load_config()["units"] == {}                         # the config is untouched
    assert journal_kinds()[-2:] == ["interrupted", "disabled"]
    env.tick(2 * H)
    assert status_of() == {"kind": "off", "scope": "unit", "reason": "interrupts"}
    starts = [a for _, a, _ in env.sent].count("dry_start")
    env.tick(3 * H)
    assert [a for _, a, _ in env.sent].count("dry_start") == starts
    eng.release(["bambu-1__0"], now=env.clock.now + 1)
    assert journal_kinds()[-1] == "reenabled" and eng._states["bambu-1__0"]["disabled_reason"] is None
    assert status_of() == {"kind": "pending"}         # until the woken engine decides again
    assert eng._wake.is_set()


def test_outside_stop_and_restart_is_journaled_as_a_takeover(env):
    # the external program stops our run, then starts its own 2 min later
    env.configure()
    eng._states["bambu-1__0"] = ad.load_state({"interrupts": 1})      # one real interruption before
    env.tick()                                      # start
    env.tick(60)                                    # confirmed
    env.units[0].stop()
    env.tick(60)
    assert journal_kinds() == ["start", "interrupted", "disabled"]
    assert ("auto-dry", "autodry_disable:bambu-1:0") in env.audit
    env.units[0].dry_now(rem=719, temp=55)
    env.tick(120)
    rows = eng.snapshot(env.clock.now)["journal"]
    assert [r["kind"] for r in rows] == ["foreign_start", "start"]           # the end row became the takeover
    assert rows[0]["text"] == "Чужая сушка перезапустила прогон автосушки — не вмешиваемся · 55° · PETG"
    assert rows[0]["pending"] is False and rows[0]["ts"] == env.clock.now
    st = eng._states["bambu-1__0"]
    assert st["phase"] == "foreign" and st["interrupts"] == 1 and st["disabled_reason"] is None
    assert st["hold_until"] is None and st["foreign_entry"] == rows[0]["id"] and len(st["runs"]) == 2
    assert env.audit[-1] == ("auto-dry", "autodry_release:bambu-1:0")
    assert status_of()["kind"] == "foreign"
    assert [r["kind"] for r in stored("ams_autodry_journal")] == ["start", "foreign_start"]


def test_capability_false_sends_nothing(env):
    env.configure()
    printer_commands._capability["bambu-1"] = {"print_cmds": False, "via": "fun", "ts": 0}
    for _ in range(24):
        env.tick(H)
    assert env.sent == [] and status_of() == {"kind": "unsupported", "reason": "locked"}
    assert journal_kinds() == []


def test_clock_behind_skips_ticks(env):
    env.configure()
    settings_service.set_many({"ams_autodry_state": {"units": {}, "last_tick": T0 + H}})
    eng._reset()
    eng.set_sources(env.store, env.history)
    env.tick()
    assert env.sent == [] and eng.snapshot(T0)["engine"]["clock_wait"] is True
    env.tick(H + 61)
    assert eng.snapshot(env.clock.now)["engine"]["clock_wait"] is False and len(env.sent) == 1


def test_clock_jump_over_a_run_is_lost(env):
    # the unit was seen drying at the last check (5 min left), the next check is 7 h later:
    # the clock cannot be trusted, so the end is "lost", not a normal finish
    env.configure()
    env.tick()                                      # start
    env.tick(60)                                    # confirmed
    env.units[0].remaining = 5
    env.tick(60)
    env.units[0].stop()
    env.tick(7 * H)
    assert journal_kinds() == ["start", "lost"]
    assert eng._states["bambu-1__0"]["end_kind"] == "lost"


def test_stale_store_is_offline(env):
    env.configure()
    env.store.stale["bambu-1"] = 600
    env.tick()
    assert env.sent == [] and status_of() == {"kind": "offline"}


def test_a_printer_in_its_grace_period_is_offline(env):
    # a failed poll keeps the last good status (with a fresh timestamp) for a while: stale AMS readings
    env.configure()
    env.store.grace["bambu-1"] = True
    env.tick()
    assert env.sent == [] and status_of() == {"kind": "offline"}
    env.store.grace["bambu-1"] = False
    env.tick(60)
    assert [a for _, a, _ in env.sent] == ["dry_start"]


def test_history_not_queried_when_not_needed(env):
    env.configure(enabled=False)
    for _ in range(10):
        env.tick(60)
    assert env.history.calls == 0
    env.configure()
    env.tick(60)                                    # idle: the rest check reads history, then starts
    assert env.history.calls == 1
    for _ in range(10):
        env.tick(60)                                # running: no reads
    assert env.history.calls == 1
    env.units[0].remaining = 3
    env.tick(60)
    env.units[0].stop()
    env.tick(60)                                    # finish -> settle
    for _ in range(5):
        env.tick(60)                                # settling: no reads
    assert env.history.calls == 1
    env.units[0].temp = 26.0
    env.tick(H)                                     # the live RH (30 %) is above the target: the repeat
    assert env.history.calls == 1                   # needs no history read — a warm AMS only reads lower


def test_a_unit_far_above_the_target_repeats_without_the_settle_wait(env):
    # owner, 15.10: units idled 1–3 h between runs while far above a 5 % target. A warm AMS
    # reads drier than it is, so a live reading above the target already decides the repeat.
    env.configure()
    env.tick()
    env.units[0].remaining = 2
    env.tick(60)
    env.units[0].stop()
    env.units[0].temp = 40.0
    env.tick(60)                                    # finish -> settle
    env.tick(60)                                    # next tick: measured above the target, repeat now
    assert [a for _, a, _ in env.sent] == ["dry_start", "dry_start"]
    assert env.clock.now - T0 < 10 * 60


def test_transient_refusal_retries_in_two_minutes_without_backoff():
    st = ad.new_state()
    action = {"type": "start", "trigger": "start", "series": 1, "rh_before": 27, "t_before": 26,
              "plan": {"temp": 55, "hours": 12, "filament": "PETG", "name": "PETG", "mode": "petg"},
              "start_rh": 25, "hold_h": 2, "target_rh": 20}
    for _ in range(3):
        st, ev, status = ad.apply_start_result(
            st, action, {"success": False, "status": "refused",
                         "detail": "Нет свежей телеметрии принтера — повторите через несколько секунд"}, "auto", T0)
        assert st["retry_at"] == T0 + ad.TRANSIENT_RETRY_S and st["refusals"] == 0
    st, ev, status = ad.apply_start_result(st, action, {"success": False, "status": "rejected",
                                                       "detail": "отказ"}, "auto", T0)
    assert st["refusals"] == 1 and st["retry_at"] == T0 + ad.retry_delay_s(1)


def test_history_not_queried_for_an_offline_unit(env):
    env.configure()
    env.store.online["bambu-1"] = False
    env.tick(60)
    assert env.history.calls == 0 and status_of() == {"kind": "offline"}
    env.store.online["bambu-1"] = True
    env.tick(60)
    assert env.history.calls == 1 and [a for _, a, _ in env.sent] == ["dry_start"]


def test_state_survives_restart(env):
    env.configure()
    env.tick()
    eng._reset()
    eng.set_sources(env.store, env.history)
    assert eng._states["bambu-1__0"]["phase"] == "running"
    assert [r["kind"] for r in eng.snapshot(T0)["journal"]] == ["start"]
    assert eng._persisted_last_tick == T0 and eng._journal_seq == 1
    env.tick(60)
    assert len(env.sent) == 1 and status_of()["kind"] == "dry"


@pytest.mark.parametrize("saved,journal", [
    ({"units": [1]}, []),
    ({"units": {"bambu-1__0": "x"}, "last_tick": "yesterday"}, [{"id": "a"}, {"id": 3}, "row", {"id": True}]),
    ({"units": {"bambu-1__0": {"runs": [None, "x", T0 - 60, True], "hold_until": "soon", "series": "2",
                               "phase": "zzz", "interrupts": None, "display": [], "rest_temp": "warm"}}}, []),
    ("garbage", "garbage"),
])
def test_corrupt_saved_state_does_not_stop_the_engine(env, saved, journal):
    # version skew or a manual DB edit: main() calls set_sources unguarded, a crash here is a crash loop
    settings_service.set_many({"ams_autodry_state": saved, "ams_autodry_journal": journal})
    settings_service.reset_cache()
    eng._reset()
    eng.set_sources(env.store, env.history)
    env.configure()
    env.tick()
    assert [a for _, a, _ in env.sent] == ["dry_start"] and status_of()["kind"] == "starting"
    rows = eng.snapshot(T0)["journal"]
    assert rows[0]["kind"] == "start" and isinstance(rows[0]["id"], int)


def test_a_manual_action_before_the_first_check_keeps_the_saved_check_time(env):
    # the brownout guard compares the clock with the last check: a release, stop or run-now
    # before the first tick (120 s after start, or for as long as the clock waits) must not erase it
    settings_service.set_many({"ams_autodry_state": {"units": {"bambu-1__0": {"hold_until": T0 + H}},
                                                     "last_tick": T0 + 2 * H}})
    settings_service.reset_cache()
    eng._reset()
    eng.set_sources(env.store, env.history)
    eng.release(["bambu-1__0"], now=T0)
    assert stored("ams_autodry_state")["last_tick"] == T0 + 2 * H
    eng._reset()
    eng.set_sources(env.store, env.history)
    env.configure()
    env.tick()                                      # the clock is behind the saved check: wait for NTP
    assert env.sent == [] and eng.snapshot(T0)["engine"]["clock_wait"] is True


def test_load_state_sanitizes_a_corrupt_state():
    st = ad.load_state({"runs": [None, "x", T0 - 60, True], "hold_until": "soon", "series": "2", "phase": "zzz",
                        "interrupts": None, "display": [], "rest_temp": "warm", "origin": 5, "closed": "no"})
    assert st["runs"] == [T0 - 60] and st["hold_until"] is None and st["series"] == 0 and st["phase"] == "idle"
    assert st["interrupts"] == 0 and st["display"] == {} and st["rest_temp"] is None and st["origin"] is None
    assert st["closed"] is False
    good = ad.load_state({"phase": "settle", "hold_until": T0, "rest_temp": 26.5, "series": 2, "origin": "auto"})
    assert (good["phase"], good["hold_until"], good["rest_temp"], good["series"], good["origin"]) == \
        ("settle", T0, 26.5, 2, "auto")


def test_volatile_fields_throttled(env, monkeypatch):
    env.configure()
    env.units[0].rh = 15                            # waits below the start threshold: no drying tracked
    env.tick()
    writes = []
    real = settings_service.set_many
    monkeypatch.setattr(settings_service, "set_many", lambda values, **k: writes.append(sorted(values)) or real(values))
    for i in range(10):
        env.units[0].temp += 0.5                    # rest_temp: a volatile field
        env.tick(60)
    assert writes == []
    env.clock.now += 15 * 60
    env.units[0].temp += 0.5
    eng.tick(env.clock.now)
    assert writes == [["ams_autodry_state"]]


def test_state_written_on_every_check_while_drying(env, monkeypatch):
    # its last_seen_at / last_remaining_min and the check time judge the end after a restart
    env.configure()
    env.tick()
    env.tick(60)
    writes = []
    real = settings_service.set_many
    monkeypatch.setattr(settings_service, "set_many", lambda values, **k: writes.append(sorted(values)) or real(values))
    for i in range(10):
        env.units[0].remaining -= 1
        env.tick(60)
    assert writes == [["ams_autodry_state"]] * 10
    saved = stored("ams_autodry_state")
    assert saved["last_tick"] == env.clock.now and saved["units"]["bambu-1__0"]["last_seen_at"] == env.clock.now
    assert saved["units"]["bambu-1__0"]["last_remaining_min"] == env.units[0].remaining


def _restart(env):
    """The service restarts: what was in memory is gone, the saved state and journal are read back."""
    eng._reset()
    eng.set_sources(env.store, env.history)


def test_a_stop_during_a_short_restart_is_an_interruption(env):
    # stopped (Handy) a minute after the service went down; back 3 min later, first check 2 min after
    # that: an interruption — held, never answered by a new start — not a run lost out of sight
    env.configure()
    env.tick()                                      # start
    for _ in range(14):
        env.tick(60)                                # drying: a 15-min throttle would leave these unsaved
    env.clock.now += 60
    env.units[0].stop()
    env.clock.now += 3 * 60
    _restart(env)
    env.tick(2 * 60)
    st = eng._states["bambu-1__0"]
    assert (st["end_kind"], st["hold_reason"], st["interrupts"]) == ("interrupted", "interrupted", 1)
    assert journal_kinds() == ["start", "interrupted"]
    for _ in range(4 * 60):
        env.tick(60)
    assert [a for _, a, _ in env.sent] == ["dry_start"]


def test_a_foreign_stop_during_a_short_restart_holds_the_unit(env):
    env.configure()
    u = env.units[0]
    u.dry_now(rem=500)
    env.tick()                                      # someone else's drying
    for _ in range(13):
        u.remaining -= 1
        env.tick(60)
    env.clock.now += 60
    u.stop()                                        # stopped with 8 h to go
    env.clock.now += 3 * 60
    _restart(env)
    env.tick(2 * 60)
    st = eng._states["bambu-1__0"]
    assert st["end_kind"] == "foreign_end" and st["closed"] and st["hold_reason"] == "foreign_stop"
    for _ in range(4 * 60):
        env.tick(60)
    assert env.sent == []


def test_a_foreign_drying_ending_on_time_during_a_restart_is_no_early_stop(env):
    env.configure()
    u = env.units[0]
    u.dry_now(rem=20)
    env.tick()
    for _ in range(8):
        u.remaining -= 1
        env.tick(60)                                # 12 min left at the last check
    env.clock.now += 12 * 60
    u.stop()                                        # it runs out while the service is down
    _restart(env)
    env.tick(60)                                    # back 13 min after the last check
    st = eng._states["bambu-1__0"]
    assert st["end_kind"] == "foreign_end" and not st["closed"] and st["hold_until"] is None
    assert eng.snapshot(env.clock.now)["journal"][0]["text"].endswith(" · засчитана как прогон")


def test_a_brownout_clock_behind_the_last_check_judges_no_end(env):
    # fake-hwclock restores its last save (12 min before the power loss — later than a 15-min
    # throttled write would be) + 3 min of boot: the check time saved while drying keeps the guard ahead
    env.configure(mode="custom", custom_hours=1)
    env.tick()                                      # a 1 h run
    u = env.units[0]
    end = eng._states["bambu-1__0"]["run_expected_end"]
    while env.clock.now < end - 31 * 60:
        u.remaining = int((end - env.clock.now) // 60)
        env.tick(60)
    crash = env.clock.now
    u.stop()                                        # it ends at its planned time while the Pi is dark
    _restart(env)
    env.clock.now = crash - 12 * 60 + 3 * 60
    eng.tick(env.clock.now)
    assert eng._clock_wait is True and eng._states["bambu-1__0"]["phase"] == "running"
    assert journal_kinds() == ["start"]
    env.clock.now = end + H                         # NTP: an hour after its end
    eng.tick(env.clock.now)
    st = eng._states["bambu-1__0"]
    assert eng._clock_wait is False and st["end_kind"] == "lost"
    assert (st["interrupts"], st["hold_until"]) == (0, None)


def test_journal_capped_and_dedup(env):
    for i in range(60):
        eng._emit("bambu-1__0", [{"kind": "released"}], T0 + i)
    assert len(eng._journal) == eng.JOURNAL_MAX and eng._journal[-1]["id"] == 60
    eng._emit("bambu-1__0", [{"kind": "refused", "reason": "check", "detail": "x", "delay": 600,
                              "dk": "check:x"}], T0 + 100)
    eng._emit("bambu-1__0", [{"kind": "refused", "reason": "check", "detail": "x", "delay": 1200,
                              "dk": "check:x"}], T0 + 200)
    last = eng._journal[-1]
    assert last["count"] == 2 and last["ts"] == T0 + 200 and last["text"].endswith("повтор через 20 мин")
    eng._emit("bambu-2__0", [{"kind": "refused", "reason": "check", "detail": "x", "delay": 600,
                              "dk": "check:x"}], T0 + 300)
    assert eng._journal[-1]["count"] == 1


def test_measure_completes_the_end_row(env):
    env.configure()
    env.tick()
    env.tick(60)
    env.units[0].remaining = 2
    env.tick(60)
    env.units[0].stop()
    env.units[0].temp = 40.0                        # still warm after the pause
    env.units[0].rh = 19                            # and reading below the target: wait for the cold measure
    env.tick(60)
    assert journal_kinds() == ["start", "finish"]
    env.tick(H)
    status = status_of()
    assert status["kind"] == "cool" and status["measuring"] is True and status["temp_max"] == 29
    env.units[0].temp = 27.0
    env.history.rh[("bambu-1", 0)] = 19
    env.tick(60)
    rows = eng.snapshot(env.clock.now)["journal"]
    finish = rows[0]
    assert finish["kind"] == "finish" and finish["rh_before"] == 30 and finish["rh_after"] == 19
    assert finish["t_before"] == 26 and finish["t_after"] == 27 and finish["pending"] is False
    assert finish["text"].endswith(" · цель 20% достигнута")


def test_measure_fallback_row_when_the_end_row_is_gone(env):
    env.configure()
    eng._states["bambu-1__0"] = ad.load_state({"phase": "settle", "series": 1, "end_kind": "finish",
                                               "settle_until": T0 - 60, "dry_end_at": T0 - H, "measure_entry": 999,
                                               "t_before": 26, "rh_before": 28})
    env.history.rh[("bambu-1", 0)] = 19
    env.tick()
    rows = eng.snapshot(T0)["journal"]
    assert rows[0]["kind"] == "finish" and rows[0]["text"] == "Прогон 1 закончен · цель 20% достигнута"
    assert rows[0]["rh_after"] == 19


def test_tick_survives_unit_exception(env, monkeypatch, caplog):
    env.configure()
    env.units.append(Unit("bambu-2"))
    real = ad.decide

    def flaky(state, obs, cfg, now):
        if obs["key"] == "bambu-1__0":
            raise ValueError("bad unit")
        return real(state, obs, cfg, now)

    monkeypatch.setattr(ad, "decide", flaky)
    with caplog.at_level(logging.ERROR, logger="app.services.ams_autodry_engine"):
        env.tick()
        env.tick(60)
    assert len([r for r in caplog.records if "bad unit" in r.getMessage()]) == 1
    assert [(p, a) for p, a, _ in env.sent] == [("bambu-2", "dry_start")]


def test_start_is_idempotent_and_logs(env, monkeypatch):
    calls = []
    monkeypatch.setattr(eng.threading, "Thread",
                        lambda *a, **k: SimpleNamespace(start=lambda: calls.append(k.get("name"))))
    eng.start()
    eng.start()
    assert calls == ["ams-autodry"]


def test_a_failed_check_shows_only_the_error_type(env, monkeypatch, caplog):
    # GET /api/ams/autodry is for every logged-in user: the details stay in the server log
    def boom(now=None):
        raise RuntimeError("cannot open /home/someone/forge/ams_history.db")

    real = eng.tick
    monkeypatch.setattr(eng, "tick", boom)
    with caplog.at_level(logging.ERROR, logger="app.services.ams_autodry_engine"):
        eng._safe_tick()
    snap = eng.snapshot(T0)
    assert snap["engine"]["error"] == "RuntimeError" and "/home" not in json.dumps(snap)
    assert any("tick failed" in r.getMessage() and r.exc_info for r in caplog.records)
    monkeypatch.setattr(eng, "tick", lambda: real(T0))
    eng._safe_tick()
    assert eng.snapshot(T0)["engine"]["error"] is None


# --- snapshot -------------------------------------------------------------------------

def test_snapshot_shape(env):
    snap = eng.snapshot(T0)
    assert set(snap) == {"now", "config", "limits", "engine", "units", "journal"}
    assert snap["config"] == ad.DEFAULT_CONFIG and snap["engine"]["last_tick"] is None
    u = snap["units"][0]
    assert u["status"] == {"kind": "pending"} and u["key"] == "bambu-1__0" and u["label"] == "P1"
    assert u["model"] == "AMS 2 Pro" and u["device_type"] == "P2S" and u["rh"] == 30
    assert u["plan"]["temp"] == 65 and u["plan_now"]["temp"] == 65 and set(u["plans"]) == {"auto", "pla", "petg", "tpu"}
    assert u["plan_ctx"] == {"cap": 65, "max_temp": 65, "softest": "PETG", "model": "AMS 2 Pro",
                             "limit_text": "не выше 65° — потолок AMS 2 Pro"}
    assert u["materials"] == [{"type": "PETG", "material": "PETG", "color": "FFF144", "count": 2, "ht": False}]
    assert "presets" not in u["dry"] and "suggest" not in u["dry"]
    assert snap["limits"]["interrupt_limit"] == 2 and snap["limits"]["series_backoff_h"] == 48
    assert snap["limits"]["night_window"] == {"from": "22:00", "to": "08:00"}
    text = json.dumps(snap, ensure_ascii=False)
    for secret in ("access_code", "serial", "telegram", "proxy", "MATERIALS"):
        assert secret not in text


def test_snapshot_keeps_absent_units(env):
    env.tick()
    env.units.clear()
    snap = eng.snapshot(T0 + 60)
    u = snap["units"][0]
    assert u["key"] == "bambu-1__0" and u["dry"] is None and u["online"] is False and u["plans"] is None
    assert u["label"] == "P1" and u["name"] == "AMS 1"
    env.tick(60)
    assert status_of()["kind"] == "offline"


# --- a multi-day simulation over real AMS history --------------------------------------

TRACES = json.loads((Path(__file__).parent / "fixtures" / "ams" / "autodry_traces.json").read_text(encoding="utf-8"))
TAU_S = 45 * 60          # AMS cooling time constant after a run (P2S 55 -> 45 °C in ~40 min)


class TraceUnit:
    """One AMS unit replaying a sanitized ams_history trace (5-min buckets):
    the trace's own dryings are foreign runs (a foreign start during our run
    overwrites ours, as the printer does); the engine's starts overlay a run
    at the set temperature, then an exponential cool-down, and leave the unit
    2 p.p. drier for 3 days."""

    def __init__(self, trace, t0, pid):
        self.t0, self.pid, self.idx = t0, pid, 0
        self.slots = [slot(t) for t in trace["slots"]] + [EMPTY] * (4 - len(trace["slots"]))
        self.data = {b: (rh, temp10 / 10.0, dry) for b, rh, temp10, dry in
                     zip(trace["bucket"], trace["rh"], trace["temp10"], trace["dry_min"])}
        self.end = t0 + trace["days"] * 86400
        self.ours = []             # (start, end, temp)
        self.starts_refused = 0
        self.cache = {}
        # the trace's drying starts and restarts (timer back up): they overwrite a run of ours
        self.foreign_starts = []
        for b in sorted(self.data):
            dry, prev = self.data[b][2], self.data.get(b - 1)
            if dry and (prev is None or not prev[2] or dry > prev[2] + 10):
                self.foreign_starts.append(t0 + b * 300)

    def bucket(self, t):
        return self.data.get(int((t - self.t0) // 300))

    def up(self, t):
        return self.bucket(t) is not None

    def run_end(self, run):
        cut = [s for s in self.foreign_starts if run[0] < s < run[1]]
        return min(cut) if cut else run[1]

    def run_at(self, t):
        return next((r for r in self.ours if r[0] <= t < self.run_end(r)), None)

    def env(self, t):
        rh, temp, foreign = self.bucket(t)
        run = self.run_at(t)
        drier = 2 * sum(1 for r in self.ours if self.run_end(r) <= t < self.run_end(r) + 3 * 86400)
        rh = max(5, rh - drier)
        if run is not None:
            return {"rh": rh, "temp": float(run[2]), "active": True, "remaining": max(1, int((run[1] - t) // 60)),
                    "dtemp": run[2], "filament": "PETG"}
        if foreign:
            return {"rh": rh, "temp": temp, "active": True, "remaining": foreign, "dtemp": 55, "filament": "PETG"}
        last = max((r for r in self.ours if self.run_end(r) <= t), key=self.run_end, default=None)
        if last is not None:
            temp = max(temp, temp + (last[2] - temp) * pow(2.718281828, -(t - self.run_end(last)) / TAU_S))
        return {"rh": rh, "temp": temp, "active": False, "remaining": None, "dtemp": None, "filament": None}

    def status(self, t):
        e = self.env(t)
        dry = make_dry(self.slots, e["active"], e["remaining"], None, e["dtemp"], e["filament"])
        unit = {"index": 0, "humidity": 5, "humidity_pct": e["rh"], "temp": round(e["temp"], 1),
                "dry_time": e["remaining"], "slots": copy.deepcopy(self.slots), "dry": dry}
        return PrinterStatus(id=self.pid, label=self.pid, kind=PrinterKind.BAMBU, online=True,
                             state=PrinterState.IDLE, last_update_ts=t, device_type="P2S", ams_dry_ok=True,
                             ams={"tray_now": None, "units": [unit]})


class TraceWorld:
    def __init__(self, units):
        self.units, self.now = units, None
        self.sent, self.events = [], []

    # store
    def get_all(self):
        return [u.status(self.now) for u in self.units if u.up(self.now)]

    def get_one(self, pid):
        u = next(u for u in self.units if u.pid == pid)
        return u.status(self.now) if u.up(self.now) else None

    # ams_store: a row every 90 s where the trace has data
    def query(self, pid, idx, fr, to):
        u = next(u for u in self.units if u.pid == pid)
        out, t = [], fr - fr % 90
        while t <= to:
            if t >= fr and u.up(t):
                row = u.cache.get(t) if t <= self.now - 90 else None    # the past never changes
                if row is None:
                    e = u.env(t)
                    row = {"recorded_at": t, "humidity_idx": 5, "humidity_pct": e["rh"],
                           "temp": round(e["temp"], 1), "dry_time": e["remaining"]}
                    if t <= self.now - 90:
                        u.cache[t] = row
                out.append(row)
            t += 90
        return out

    def send(self, pid, action, params):
        self.sent.append((self.now, pid, action, dict(params)))
        u = next(u for u in self.units if u.pid == pid)
        if action != "dry_start":
            return {"success": True, "status": "confirmed", "detail": "ok"}
        if not u.up(self.now) or u.env(self.now)["active"]:
            u.starts_refused += 1
            return {"success": False, "status": "refused", "detail": "Сушка уже идёт — сначала остановите её"}
        u.ours.append((self.now, self.now + params["hours"] * H, params["temp"]))
        return {"success": True, "status": "confirmed", "detail": "подтверждено принтером"}


def _simulate(monkeypatch, cfg, tick_s=120):
    t0 = T0 - (T0 + 3 * H) % 86400              # 00:00 farm time
    units = [TraceUnit(TRACES["traces"]["p2s_wet_week"], t0, "sim-p2s"),
             TraceUnit(TRACES["traces"]["h2s_foreign_runs"], t0, "sim-h2s")]
    world = TraceWorld(units)
    mem = {}
    monkeypatch.setattr(settings_service, "get", lambda key, **k: copy.deepcopy(mem.get(key, settings_service.DEFAULTS[key])))
    monkeypatch.setattr(settings_service, "set_many", lambda values, **k: mem.update(copy.deepcopy(values)))
    monkeypatch.setattr(printer_commands, "send", world.send)
    monkeypatch.setattr(printer_commands, "live_status", lambda pid, prn=None: world.get_one(pid))
    monkeypatch.setattr(printer_registry, "get_printer", lambda pid: {"id": pid, "kind": "bambu"})
    monkeypatch.setattr(printer_registry, "get_label", lambda pid: pid)
    monkeypatch.setattr(eng, "get_audit_service", lambda: SimpleNamespace(log=lambda *a, **k: None))
    real_emit = eng._emit

    def emit(key, events, now):
        world.events += [(now, key, dict(e)) for e in events]
        return real_emit(key, events, now)

    monkeypatch.setattr(eng, "_emit", emit)
    eng._reset()
    mem["ams_autodry"] = cfg
    eng.set_sources(world, world)
    end = max(u.end for u in units)
    t = t0
    while t < end:
        world.now = t
        if any(u.up(t) for u in units):             # history gaps: the server was down
            eng.tick(t)
        t += tick_s
    return world


def _starts(world, pid, kinds=("start", "repeat")):
    return [t for t, key, e in world.events if key.startswith(pid + "__") and e["kind"] in kinds]


def test_simulation_over_real_history(monkeypatch):
    cfg = copy.deepcopy(ad.DEFAULT_CONFIG)
    cfg.update(enabled=True, dry_run=False)
    world = _simulate(monkeypatch, cfg)
    try:
        assert not [s for s in world.sent if s[2] != "dry_start"], "the engine sent a stop on its own"
        assert all(u.starts_refused == 0 for u in world.units), "a start was sent to a drying unit"
        for u in world.units:
            ours = _starts(world, u.pid)
            foreign = _starts(world, u.pid, ("foreign_start",))
            evs = [(t, e) for t, key, e in world.events if key.startswith(u.pid + "__")]
            # every start: fewer than max_runs starts (ours + foreign) in the 24 h before it
            for t in ours:
                prior = [x for x in ours + foreign if t - 86400 < x < t]
                assert len(prior) < cfg["max_runs"], (u.pid, t)
            assert all(e["series"] <= cfg["max_runs"] for _, e in evs if e["kind"] in ("start", "repeat"))
            # a failed series: no new one for 48 h; an early foreign stop: hands off for 24 h
            for t, e in evs:
                if e["kind"] == "unreachable":
                    assert not [x for x in ours if t < x < t + 48 * H], (u.pid, "backoff")
                if e["kind"] == "foreign_end" and e["early"]:
                    assert not [x for x in ours if t < x < t + 24 * H], (u.pid, "foreign stop")
            # measures: not before the pause, and only on a cooled AMS (or at the deadline, flagged warm)
            ends = [t for t, e in evs if e["kind"] in ("finish", "foreign_end", "interrupted", "lost")]
            for t, e in evs:
                if e["kind"] != "measure" or e["outcome"] == "cancelled":
                    continue
                prev_end = max(x for x in ends if x <= t)
                # before the pause only when already above the target (the warm-reading shortcut)
                assert t - prev_end >= cfg["pause_h"] * H or (
                    e["outcome"] in ("repeat", "unreachable", "limit", "held", "backoff")
                    and e.get("rh_after") is not None and e["rh_after"] > cfg["target_rh"]), (u.pid, e)
                if t - prev_end >= cfg["pause_h"] * H and e.get("t_before") is not None \
                        and e.get("t_after") is not None and not e["warm"]:
                    assert e["t_after"] <= e["t_before"] + ad.COOL_MARGIN_C
        p2s = _starts(world, "sim-p2s")
        h2s_foreign_ends = [e for t, key, e in world.events if key == "sim-h2s__0" and e["kind"] == "foreign_end"]
        assert p2s, "the wet P2S week never triggered a start"
        assert any(e["early"] for e in h2s_foreign_ends), "the 08.10 early stop was not seen"
        assert len(h2s_foreign_ends) >= 5
        kinds = {e["kind"] for _, _, e in world.events}
        assert {"start", "finish", "measure", "foreign_start", "foreign_end"} <= kinds
        assert "fault" not in kinds and "interrupted" not in kinds
        # the 65 °C external run of 27.08 started over our repeat: tracked as foreign from then on
        assert any(e.get("takeover") for _, key, e in world.events if key == "sim-p2s__0")
    finally:
        eng._reset()


def test_simulation_in_shadow_mode_sends_nothing(monkeypatch):
    world = _simulate(monkeypatch, copy.deepcopy(ad.DEFAULT_CONFIG), tick_s=300)
    try:
        assert world.sent == []
        kinds = [e["kind"] for _, _, e in world.events]
        assert "dry_run" in kinds and "start" not in kinds and "foreign_start" in kinds
    finally:
        eng._reset()


def test_main_wires_the_engine_after_the_settings():
    src = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
    body = src[src.index("def main():"):]
    load = body.index("settings_service.load(config)")
    wire = body.index("ams_autodry_engine.set_sources(store, ams_store)")
    run = body.index("ams_autodry_engine.start()")
    assert load < wire < run < body.index("poll_loop(")


def test_note_user_command_without_sources_does_nothing(monkeypatch):
    eng._reset()
    monkeypatch.setattr(settings_service, "get", lambda *a, **k: 1 / 0)
    monkeypatch.setattr(settings_service, "set_many", lambda *a, **k: 1 / 0)
    eng.note_user_command("p1", "dry_stop", {"ams": 0})
    assert eng._states == {} and eng._journal == []
