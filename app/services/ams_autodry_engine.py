"""AMS auto-drying engine («Автосушка AMS»): the daemon thread around the
pure rules in ams_autodry.

Every TICK_S it reads the stored telemetry (no lock), the AMS history only
where a rule needs it (ams_autodry.needs_rows), decides every unit under one
lock and sends at most one start per printer per tick through
printer_commands.send — the dashboard's validated path, no payloads of its
own. It never sends a stop on its own: stops come only from admin actions
(stop_all). In shadow mode (config dry_run, the shipped default) it journals
what it would start and sends nothing; run_now / stop_all always send.

Config, state and journal live in settings (ams_autodry, ams_autodry_state,
ams_autodry_journal). The journal and the per-unit status are the only
channels: no Telegram (owner decision). Every drying command claims its
unit (_inflight) until its result is applied — the engine's own and the
dashboard's (claim / unclaim): a tick skips a claimed unit, run-now and
stop-all skip or wait, a second dashboard command gets 409.
"""
import copy
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

from app.domain.models import PrinterState
from app.services import ams_autodry as rules
from app.services import ams_drying, bambu_extras, printer_commands, printer_registry, settings_service
from app.services.audit_service import get_audit_service

logger = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


TICK_S = 60
FIRST_TICK_DELAY_S = 120          # let the collectors connect after a restart
STALE_S = 300                     # a status older than this (hung poll loop) is not "online"
CLOCK_JUMP_S = 6 * 3600
CLAIM_WAIT_S = 12.0
PERSIST_EVERY_S = 900             # volatile-only state changes are written at most this often (no drying tracked)
JOURNAL_MAX = 50
MANUAL_WORKERS = 8
# Farm time for the start window (MSK, no DST); read here, never in the pure rules.
TZ_OFFSET_MIN = _env_int("FORGE_TZ_OFFSET_MIN", 180)
AUTO_USER = "auto-dry"

MSG_NOT_FOUND = "Блок не найден"
MSG_BUSY = "Блок занят — команда уже выполняется"
MSG_OFFLINE = "Принтер офлайн"
MSG_DRYING = "Уже сушится — пропускаем"
MSG_UNCONFIRMED = "Уже запущена — ждём подтверждения от AMS"
MSG_NOT_DRYING = "Не сушится — пропускаем"
MSG_CLAIM_TIMEOUT = "Блок занят — повторите через несколько секунд"
MSG_NO_SPOOLS = "Катушек нет"
MSG_CANNOT_DRY = "Этот AMS не сушит удалённо"
MSG_INTERNAL = "внутренняя ошибка автосушки"
MSG_OTHER_UNIT = "ждём старта другого блока этого принтера"

_lock = threading.RLock()
_persist_lock = threading.Lock()  # serializes capture + write of the settings rows (taken before _lock)
_wake = threading.Event()         # manual operations and config saves: tick soon
_store = None
_ams_store = None
_states: Dict[str, dict] = {}     # unit key -> engine state (ams_autodry.new_state)
_status: Dict[str, dict] = {}     # unit key -> last status
_inflight: set = set()            # unit keys with a command being sent (claimed atomically)
_journal: List[dict] = []
_journal_seq = 0
_journal_ver = 0
_journal_saved_ver = 0
_last_tick: Optional[float] = None
_persisted_last_tick: Optional[float] = None
_tick_error: Optional[str] = None
_started = False
_started_at: Optional[float] = None
_clock_wait = False
_persisted_blob: Optional[dict] = None
_persisted_at = 0.0
_unit_errors: Dict[str, str] = {}
_time = time.time
_sleep = time.sleep


def _reset() -> None:
    """Forget everything (tests)."""
    global _store, _ams_store, _journal_seq, _journal_ver, _journal_saved_ver, _last_tick
    global _persisted_last_tick, _tick_error, _started, _started_at, _clock_wait, _persisted_blob, _persisted_at
    with _lock:
        _states.clear()
        _status.clear()
        _inflight.clear()
        _journal.clear()
        _unit_errors.clear()
        _store = _ams_store = None
        _journal_seq = _journal_ver = _journal_saved_ver = 0
        _last_tick = _persisted_last_tick = _tick_error = _started_at = None
        _started = _clock_wait = False
        _persisted_blob, _persisted_at = None, 0.0
        _wake.clear()


# --- wiring ---------------------------------------------------------------------------

def set_sources(store, ams_store) -> None:
    """main(): the state store and the AMS history; restores state and journal."""
    global _store, _ams_store, _journal_seq, _persisted_last_tick, _persisted_blob, _persisted_at
    _store, _ams_store = store, ams_store
    try:
        saved = settings_service.get("ams_autodry_state")
        journal = settings_service.get("ams_autodry_journal")
    except Exception:
        logger.error("[autodry] reading the saved state failed", exc_info=True)
        saved, journal = {}, []
    saved = saved if isinstance(saved, dict) else {}
    units = saved.get("units")
    with _lock:
        try:
            _states.clear()
            for key, st in (units.items() if isinstance(units, dict) else ()):
                if rules.parse_key(key):
                    _states[key] = rules.load_state(st)
            rows = journal if isinstance(journal, list) else []
            _journal[:] = [copy.deepcopy(e) for e in rows if isinstance(e, dict)][-JOURNAL_MAX:]
            _journal_seq = max([e["id"] for e in _journal if _is_int(e.get("id"))], default=0)
            lt = saved.get("last_tick")
            _persisted_last_tick = float(lt) if isinstance(lt, (int, float)) and not isinstance(lt, bool) else None
        except Exception:                    # never a crash loop of the whole dashboard over our own rows
            logger.error("[autodry] the saved state is unreadable; starting from scratch", exc_info=True)
            _states.clear()
            _journal.clear()
            _journal_seq, _persisted_last_tick = 0, None
        _persisted_blob = _core_blob()
        _persisted_at = _time()


def start() -> None:
    """Start the daemon thread (idempotent)."""
    global _started, _started_at
    with _lock:
        if _started:
            return
        _started, _started_at = True, _time()
    threading.Thread(target=_loop, daemon=True, name="ams-autodry").start()
    logger.info("[autodry] engine started, first check in %d s", FIRST_TICK_DELAY_S)


def _loop() -> None:
    _sleep(FIRST_TICK_DELAY_S)
    while True:
        _safe_tick()
        _wake.wait(TICK_S)
        _wake.clear()


def _safe_tick() -> None:
    global _tick_error
    try:
        tick()
    except Exception as e:
        _tick_error = type(e).__name__          # GET shows this to every user: the details go to the log only
        logger.error("[autodry] tick failed", exc_info=True)


def load_config() -> Dict[str, Any]:
    try:
        stored = settings_service.get("ams_autodry")
    except Exception:
        logger.error("[autodry] reading the config failed", exc_info=True)
        stored = None
    return rules.normalize_config(stored)


def save_config(cfg: Dict[str, Any]) -> None:
    """Store a validated config and wake the engine (the engine never writes it)."""
    settings_service.set_many({"ams_autodry": copy.deepcopy(cfg)})
    _wake.set()


def is_busy(printer_id: str, ams: Any) -> bool:
    """Is a command being sent to this unit right now (the engine's or the dashboard's)?"""
    if not isinstance(ams, int) or isinstance(ams, bool):
        return False
    with _lock:
        return rules.unit_key(printer_id, ams) in _inflight


def claim(printer_id: str, ams: Any) -> bool:
    """Claim a unit for a dashboard drying command until unclaim(): ticks,
    run-now and stop-all leave it alone meanwhile. False: already claimed.
    Not a unit (bad `ams`, refused by the command's own checks): True."""
    if not _is_int(ams):
        return True
    with _lock:
        key = rules.unit_key(printer_id, ams)
        if key in _inflight:
            return False
        _inflight.add(key)
        return True


def unclaim(printer_id: str, ams: Any) -> None:
    if _is_int(ams):
        with _lock:
            _inflight.discard(rules.unit_key(printer_id, ams))


# --- observations ---------------------------------------------------------------------

def _label(status) -> str:
    try:
        return printer_registry.get_label(status.id) or status.label
    except Exception:
        return status.label


def _cmd_ok(status) -> Optional[bool]:
    """May the dashboard send AMS commands to this printer (the cards' caps)?"""
    try:
        caps, _ = printer_commands.caps_for({"kind": "bambu", "id": status.id, "online": status.online})
        return caps.get("ams")
    except Exception:
        logger.debug("caps_for failed", exc_info=True)
        return None


def _absent_obs(key: str) -> Dict[str, Any]:
    return {"key": key, "present": False, "online": False, "printing": False, "busy": False, "dry_ok": None,
            "cmd_ok": None, "dry": None, "rh": None, "temp": None, "slots": [], "tz_offset_min": TZ_OFFSET_MIN}


def _collect(now: float) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    """({key: observation without history rows}, {key: display}) of every
    AMS that can dry in the stored status, plus the units the engine knows
    that are not there (present=False)."""
    obs: Dict[str, Dict[str, Any]] = {}
    disp: Dict[str, Dict[str, Any]] = {}
    store = _store
    for s in (store.get_all() if store is not None else []):
        if getattr(s.kind, "value", s.kind) != "bambu":
            continue
        units = (s.ams or {}).get("units") or []
        if not units:
            continue
        online = bool(s.online) and not s.grace_period_active and now - (s.last_update_ts or 0) < STALE_S
        printing = s.state in (PrinterState.PRINTING, PrinterState.PAUSED)
        cmd_ok, label = _cmd_ok(s), _label(s)
        for u in units:
            dry, idx = u.get("dry"), u.get("index")
            if dry is None or not isinstance(idx, int) or isinstance(idx, bool):
                continue
            key = rules.unit_key(s.id, idx)
            obs[key] = {"key": key, "present": True, "online": online, "printing": printing,
                        "busy": s.filament_change is not None, "dry_ok": s.ams_dry_ok, "cmd_ok": cmd_ok,
                        "dry": copy.deepcopy(dry), "rh": u.get("humidity_pct"), "temp": u.get("temp"),
                        "slots": copy.deepcopy(u.get("slots") or []), "tz_offset_min": TZ_OFFSET_MIN}
            disp[key] = {"label": label, "name": bambu_extras.ams_unit_name(idx), "device_type": s.device_type,
                         "model": rules.MODEL_NAMES.get(dry.get("type"))}
    with _lock:
        known = list(_states)
    for key in known:
        if key not in obs:
            obs[key] = _absent_obs(key)
    return obs, disp


def _rows(key: str, lookback: int, now: float) -> list:
    """[ts, rh, dry_time, temp] rows of the AMS history (ascending)."""
    pk = rules.parse_key(key)
    if _ams_store is None or pk is None:
        return []
    try:
        rows = _ams_store.query(pk[0], pk[1], now - lookback, now)
    except Exception:
        logger.error("[autodry] reading the AMS history of %s failed", key, exc_info=True)
        return []
    return [[r.get("recorded_at"), r.get("humidity_pct"), r.get("dry_time"), r.get("temp")] for r in rows]


def _unit_of(status, ams: int) -> Optional[Dict[str, Any]]:
    if status is None:
        return None
    return next((u for u in (status.ams or {}).get("units") or [] if u.get("index") == ams), None)


def _log_unit_error(key: str, e: Exception) -> None:
    msg = "%s: %s" % (type(e).__name__, e)
    if _unit_errors.get(key) != msg:
        _unit_errors[key] = msg
        logger.error("[autodry] %s: %s", key, msg, exc_info=True)


# --- the tick -------------------------------------------------------------------------

def tick(now: Optional[float] = None) -> None:
    """One pass over every unit (the thread calls it every TICK_S; tests directly)."""
    global _clock_wait, _last_tick, _tick_error
    now = _time() if now is None else now
    ref = _last_tick if _last_tick is not None else _persisted_last_tick
    if ref is not None and now < ref - 60:
        # a brownout reboot with fake-hwclock: wait for NTP rather than act on a wrong clock
        if not _clock_wait:
            logger.warning("[autodry] the clock is behind the last check; waiting for NTP")
        _clock_wait = True
        return
    _clock_wait = False
    jump = ref is not None and now - ref > CLOCK_JUMP_S
    cfg = load_config()
    units, disp = _collect(now)
    with _lock:
        need = {k: rules.needs_rows(rules.load_state(_states.get(k)), cfg, k, now, units[k]) for k in units}
    rows = {k: _rows(k, lb, now) for k, lb in need.items() if lb}
    starts = []
    printers = set()
    with _lock:
        for key in sorted(units):
            if key in _inflight:
                continue
            obs = dict(units[key], rows=rows.get(key, []), clock_jump=jump)
            try:
                st0 = rules.load_state(_states.get(key))
                st0["display"] = disp.get(key) or st0["display"]
                d = rules.decide(st0, obs, cfg, now)
            except Exception as e:
                _log_unit_error(key, e)
                continue
            _unit_errors.pop(key, None)
            _states[key] = d.state
            _status[key] = d.status
            _emit(key, d.events, now)
            for a in d.actions:                                   # only "start"
                pid = rules.parse_key(key)[0]
                if pid in printers:                               # one start per printer per tick
                    _status[key] = {"kind": "blocked", "text": MSG_OTHER_UNIT}
                    continue
                printers.add(pid)
                _inflight.add(key)
                starts.append((key, a))
    if len(starts) == 1:
        _execute_start(starts[0][0], starts[0][1], "auto", now)
    elif starts:                                  # different printers: each waits up to REPLY_TIMEOUT
        with ThreadPoolExecutor(max_workers=min(MANUAL_WORKERS, len(starts)), thread_name_prefix="autodry-start") as ex:
            for key, a in starts:
                ex.submit(_execute_start, key, a, "auto", now)
    _last_tick = now
    _persist(now)
    _tick_error = None


def _execute_start(key: str, action: Dict[str, Any], origin: str, now: float):
    """Send one claimed start (origin "auto" or "now"). Always releases the
    claim; re-checks an auto start is still due and re-plans it against the
    live telemetry. Returns (send result, plan)."""
    result: Dict[str, Any] = {"success": False, "status": "refused", "detail": MSG_INTERNAL}
    plan = action["plan"]
    apply = True
    pid = ams = None
    try:
        cfg = load_config()
        pid, ams = rules.parse_key(key)
        ucfg = rules.unit_config(cfg, key)
        if origin == "auto":
            with _lock:
                st = rules.load_state(_states.get(key))
                held = st["hold_until"] and now < st["hold_until"]
                if st["phase"] != "idle" or held or not rules.automation_on(cfg, ucfg) or st["disabled_reason"]:
                    apply = False                 # a stop / toggle landed meanwhile: abort, no event
        if apply:
            prn = printer_registry.get_printer(pid) or {}
            unit = _unit_of(printer_commands.live_status(pid, prn), ams)
            replanned = plan
            if unit is not None and unit.get("dry"):
                if unit["dry"].get("active"):
                    # dries already (started a moment ago elsewhere): the next tick tracks it
                    apply = False
                    result = {"success": False, "status": "refused", "detail": MSG_DRYING}
                else:
                    replanned = rules.plan_run(cfg, ucfg, unit["dry"], unit.get("slots") or [],
                                               manual=(origin == "now"))
            if apply and replanned is None:
                result = {"success": False, "status": "refused", "detail": MSG_NO_SPOOLS}
            elif apply:
                plan = replanned
                if cfg["dry_run"] and origin == "auto":
                    result = {"dry_run": True}
                else:
                    result = printer_commands.send(pid, "dry_start", {"ams": ams, "temp": plan["temp"],
                                                                      "hours": plan["hours"],
                                                                      "filament": plan["filament"]})
    except Exception:
        logger.error("[autodry] start %s failed", key, exc_info=True)
    finally:
        with _lock:
            _inflight.discard(key)                # first, unconditionally
            if not apply:
                _status[key] = {"kind": "pending"}            # nothing sent: the next tick decides again
            else:
                try:
                    st2, ev, status = rules.apply_start_result(_states.get(key) or rules.new_state(),
                                                               dict(action, plan=plan), result, origin, now)
                    _states[key] = st2
                    _emit(key, ev, now)
                    if status:
                        _status[key] = status
                except Exception:
                    logger.error("[autodry] applying the start result of %s failed", key, exc_info=True)
    if apply and pid is not None:
        what = "dry-run, would start" if result.get("dry_run") else "start (%s)" % origin
        logger.info("[autodry] %s %s %s°/%s h/%s: %s", key, what, plan["temp"], plan["hours"], plan["filament"],
                    "-" if result.get("dry_run") else "%s %s" % (result.get("status"), result.get("detail")))
    if origin == "auto" and result.get("success") and pid is not None:
        _audit(AUTO_USER, "autodry_start:%s:%s:%s/%s/%s" % (pid, ams, plan["temp"], plan["hours"], plan["filament"]))
    return result, plan


# --- manual operations (admin endpoints) ---------------------------------------------

def _result(key: str, disp: Optional[Dict[str, Any]], ok: bool, status: str, detail: str,
            plan: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    pk = rules.parse_key(key)
    disp = disp or {}
    return {"key": key, "label": disp.get("label") or (pk[0] if pk else key),
            "name": disp.get("name") or (bambu_extras.ams_unit_name(pk[1]) if pk else ""),
            "ok": ok, "status": status, "detail": detail, "plan": plan}


def _disp_for(key: str, disp: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    return disp.get(key) or (_states.get(key) or {}).get("display") or {}


def _finish_manual(now: float) -> None:
    _persist(now, force=True)
    _wake.set()


def run_now(keys: List[str], now: Optional[float] = None) -> List[Dict[str, Any]]:
    """«Сушить все сейчас»: start these units now with their manual plan,
    ignoring the rest trigger, the window, the limit and shadow mode."""
    now = _time() if now is None else now
    keys = list(dict.fromkeys(keys))
    cfg = load_config()
    units, disp = _collect(now)
    results: Dict[str, Dict[str, Any]] = {}
    todo = []
    with _lock:
        for key in keys:
            o, d = units.get(key), _disp_for(key, disp)
            if rules.parse_key(key) is None or o is None or not o["present"] or o["dry"] is None:
                results[key] = _result(key, d, False, "skipped", MSG_NOT_FOUND)
                continue
            if key in _inflight:
                results[key] = _result(key, d, False, "skipped", MSG_BUSY)
                continue
            if not o["online"]:
                results[key] = _result(key, d, False, "skipped", MSG_OFFLINE)
                continue
            if o["dry"].get("active"):
                results[key] = _result(key, d, False, "skipped", MSG_DRYING)
                continue
            st = rules.load_state(_states.get(key))
            if st["phase"] == "running" and not st["seen_active"]:
                results[key] = _result(key, d, False, "skipped", MSG_UNCONFIRMED)
                continue
            plan = rules.plan_run(cfg, rules.unit_config(cfg, key), o["dry"], o["slots"], manual=True)
            if plan is None:                      # manual: only a drying type without a preset table
                results[key] = _result(key, d, False, "skipped", MSG_CANNOT_DRY)
                continue
            cont = st["phase"] == "settle" or rules.repeat_due(st, now)
            t_rest = st["rest_temp"] if st["rest_temp"] is not None else o["temp"]
            action = {"type": "start", "trigger": "now", "plan": plan,
                      "series": st["series"] + 1 if cont else 1,
                      "rh_before": st["last_rest_rh"] if cont and st["last_rest_rh"] is not None else o["rh"],
                      "t_before": st["t_before"] if cont and st["t_before"] is not None else rules.as_temp(t_rest)}
            _inflight.add(key)
            todo.append((key, action, d))
    if todo:
        with ThreadPoolExecutor(max_workers=min(MANUAL_WORKERS, len(todo)), thread_name_prefix="autodry-now") as ex:
            futs = {key: ex.submit(_execute_start, key, action, "now", now) for key, action, _ in todo}
        for key, action, d in todo:
            try:
                r, plan = futs[key].result()
            except Exception:
                r, plan = {"success": False, "status": "rejected", "detail": MSG_INTERNAL}, action["plan"]
            status = r.get("status") or ("sent" if r.get("success") else "refused")
            results[key] = _result(key, d, bool(r.get("success")), status, r.get("detail") or "", plan)
    _finish_manual(now)
    return [results[k] for k in keys]


def _claim_wait(key: str, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        with _lock:
            if key not in _inflight:
                _inflight.add(key)
                return True
        if time.monotonic() >= deadline:
            return False
        _sleep(0.2)


def stop_all(keys: Optional[List[str]], now: Optional[float] = None) -> List[Dict[str, Any]]:
    """«Остановить все»: stop these drying units (None: every drying unit,
    ours and foreign); each is then left alone for manual_hold_h."""
    now = _time() if now is None else now
    cfg = load_config()
    units, disp = _collect(now)
    with _lock:
        drying = {k for k, o in units.items() if o["present"] and o["dry"] and o["dry"].get("active")}
        drying |= {k for k, st in _states.items() if st.get("phase") in ("running", "foreign")}
        dmap = {k: _disp_for(k, disp) for k in set(units) | set(keys or [])}
    targets = list(dict.fromkeys(keys)) if keys is not None else sorted(drying)
    hold_s = cfg["manual_hold_h"] * 3600
    results: Dict[str, Dict[str, Any]] = {}
    todo = []
    for key in targets:
        if rules.parse_key(key) is None or key not in units:
            results[key] = _result(key, dmap.get(key), False, "skipped", MSG_NOT_FOUND)
        elif key not in drying:
            results[key] = _result(key, dmap.get(key), False, "skipped", MSG_NOT_DRYING)
        else:
            todo.append(key)

    def one(key: str) -> Dict[str, Any]:
        if not _claim_wait(key, CLAIM_WAIT_S):
            return {"success": False, "status": "skipped", "detail": MSG_CLAIM_TIMEOUT}
        r: Dict[str, Any] = {"success": False, "status": "rejected", "detail": MSG_INTERNAL}
        try:
            pid, ams = rules.parse_key(key)
            r = printer_commands.send(pid, "dry_stop", {"ams": ams})
        except Exception:
            logger.error("[autodry] stop %s failed", key, exc_info=True)
        finally:
            with _lock:
                _inflight.discard(key)
                if r.get("success"):
                    try:
                        st2, ev = rules.apply_stop(_states.get(key) or rules.new_state(), now, hold_s, "stop_all")
                        _states[key] = st2
                        _status[key] = {"kind": "pending"}       # until the woken engine decides again
                        if not st2["display"]:
                            st2["display"] = dmap.get(key) or {}
                        _emit(key, ev, now)
                    except Exception:
                        logger.error("[autodry] applying the stop of %s failed", key, exc_info=True)
        logger.info("[autodry] %s stop (admin): %s %s", key, r.get("status"), r.get("detail"))
        return r

    if todo:
        with ThreadPoolExecutor(max_workers=min(MANUAL_WORKERS, len(todo)), thread_name_prefix="autodry-stop") as ex:
            futs = {key: ex.submit(one, key) for key in todo}
        for key in todo:
            try:
                r = futs[key].result()
            except Exception:
                r = {"success": False, "status": "rejected", "detail": MSG_INTERNAL}
            status = r.get("status") or ("sent" if r.get("success") else "refused")
            results[key] = _result(key, dmap.get(key), bool(r.get("success")), status, r.get("detail") or "")
    _finish_manual(now)
    return [results[k] for k in targets]


def release(keys: List[str], now: Optional[float] = None) -> List[str]:
    """«снять паузу» / «включить снова». Returns the keys the engine knows."""
    now = _time() if now is None else now
    done = []
    with _lock:
        for key in dict.fromkeys(keys):
            if key not in _states:
                continue
            st2, ev = rules.release(_states[key])
            _states[key] = st2
            _status[key] = {"kind": "pending"}                   # until the woken engine decides again
            _emit(key, ev, now)
            done.append(key)
    _finish_manual(now)
    return done


def note_user_command(printer_id: str, action: str, params: Dict[str, Any], now: Optional[float] = None) -> None:
    """After a successful dashboard dry_start / dry_stop: a manual run (not
    foreign, not counted) or a manual stop (hold, no interruption). Never raises."""
    try:
        if action not in ("dry_start", "dry_stop") or _store is None:     # not wired: no engine to tell
            return
        ams = (params or {}).get("ams")
        if not isinstance(ams, int) or isinstance(ams, bool):
            return
        now = _time() if now is None else now
        key = rules.unit_key(printer_id, ams)
        cfg = load_config()
        status = _store.get_one(printer_id) if _store is not None and hasattr(_store, "get_one") else None
        unit = _unit_of(status, ams)
        with _lock:
            st = rules.load_state(_states.get(key))
            if not st["display"] and unit is not None and status is not None:
                st["display"] = {"label": _label(status), "name": bambu_extras.ams_unit_name(ams),
                                 "device_type": status.device_type,
                                 "model": rules.MODEL_NAMES.get((unit.get("dry") or {}).get("type"))}
            if action == "dry_start":
                st2, ev = rules.apply_user_start(st, {"temp": params.get("temp"), "hours": params.get("hours"),
                                                      "filament": params.get("filament"),
                                                      "rh": (unit or {}).get("humidity_pct"),
                                                      "temp_now": (unit or {}).get("temp")}, now)
            else:
                st2, ev = rules.apply_stop(st, now, cfg["manual_hold_h"] * 3600, "manual_stop")
            _states[key] = st2
            _emit(key, ev, now)
        _finish_manual(now)
    except Exception:
        logger.error("[autodry] dashboard command hook failed", exc_info=True)


# --- journal ----------------------------------------------------------------------------

def _row_by_id(entry: Any) -> Optional[dict]:
    if not entry:
        return None
    return next((r for r in reversed(_journal) if r.get("id") == entry), None)


def _newest_row(key: str) -> Optional[dict]:
    return next((r for r in reversed(_journal) if r.get("key") == key), None)


def _drop_row(key: str, entry: Any) -> None:
    row = _row_by_id(entry)
    if row is not None and row.get("key") == key:
        _journal.remove(row)


def _append_row(key: str, now: float, kind: str, text: str, ev: Dict[str, Any]) -> dict:
    global _journal_seq
    disp = (_states.get(key) or {}).get("display") or {}
    pk = rules.parse_key(key)
    _journal_seq += 1
    row = {"id": _journal_seq, "ts": now, "key": key, "label": disp.get("label") or (pk[0] if pk else key),
           "name": disp.get("name") or (bambu_extras.ams_unit_name(pk[1]) if pk else ""),
           "kind": kind, "text": text, "rh_before": ev.get("rh_before"), "rh_after": ev.get("rh_after"),
           "t_before": ev.get("t_before"), "t_after": ev.get("t_after"), "pending": bool(ev.get("pending")),
           "count": 1, "dk": ev.get("dk")}
    _journal.append(row)
    del _journal[:-JOURNAL_MAX]
    return row


def _complete_measure(key: str, ev: Dict[str, Any], now: float) -> None:
    row = _row_by_id(ev.get("entry"))
    suffix = rules.measure_suffix(ev)
    if ev.get("outcome") == "cancelled":
        if row is not None:
            row["text"] += suffix
            row["pending"] = False
        return
    if row is None:                          # the end row is gone: the result gets a row of its own
        _append_row(key, now, "finish", "Прогон %s закончен" % (ev.get("series") or 1) + suffix, ev)
        return
    row["text"] += suffix
    row["rh_after"], row["t_after"], row["pending"] = ev.get("rh_after"), ev.get("t_after"), False
    if row.get("rh_before") is None:
        row["rh_before"] = ev.get("rh_before")
    if row.get("t_before") is None:
        row["t_before"] = ev.get("t_before")


def _emit(key: str, events: List[Dict[str, Any]], now: float) -> None:
    """Journal the decision events of one unit. Caller holds _lock."""
    global _journal_ver
    for ev in events or ():
        kind = ev["kind"]
        _journal_ver += 1
        if kind == "measure":
            _complete_measure(key, ev, now)
            continue
        if kind == "disabled":
            pk = rules.parse_key(key)
            _audit(AUTO_USER, "autodry_disable:%s:%s" % pk)
        if ev.get("reenabled"):              # a restart undid the interruption that disabled the unit
            _audit(AUTO_USER, "autodry_release:%s:%s" % rules.parse_key(key))
        if ev.get("drop"):
            _drop_row(key, ev["drop"])
        text = rules.journal_text(ev)
        row = _row_by_id(ev.get("replace"))
        if row is not None:                  # the dashboard start the tick took for foreign / the end a restart undid
            row.update(kind=kind, text=text, ts=now, dk=None, pending=False, count=1)
        elif ev.get("dk") is not None:
            last = _newest_row(key)
            if last is not None and last.get("kind") == kind and last.get("dk") == ev["dk"]:
                last.update(ts=now, text=text, count=(last.get("count") or 1) + 1)
                row = last
        if row is None:
            row = _append_row(key, now, kind, text, ev)
        if ev.get("bind") and key in _states:
            _states[key][ev["bind"]] = row["id"]
        if kind != "dry_run":
            logger.info("[autodry] %s %s: %s", key, kind, text)


def _audit(user: str, action: str) -> None:
    try:
        get_audit_service().log(user, action)
    except Exception:
        logger.error("[autodry] audit %s failed", action, exc_info=True)


# --- persistence ------------------------------------------------------------------------

def _core_blob() -> Dict[str, Dict[str, Any]]:
    return {k: {f: v for f, v in st.items() if f not in rules.VOLATILE} for k, st in _states.items()}


def _persist(now: float, force: bool = False) -> None:
    """Write state (on a change outside the volatile fields, on every check
    while a drying is tracked, every PERSIST_EVERY_S, or when forced) and the
    journal (when it changed). Never raises."""
    global _persisted_blob, _persisted_at, _journal_saved_ver
    try:
        with _persist_lock:
            with _lock:
                core = _core_blob()
                # A tracked drying's end after a restart is judged by its last_seen_at / last_remaining_min
                # and guarded by last_tick: a 15-min-old copy turns a stop during a short restart into
                # "lost", and lets a brownout clock restored from a recent fake-hwclock save pass the guard.
                tracking = any(st.get("phase") in ("running", "foreign") for st in _states.values())
                write_state = (force or tracking or core != _persisted_blob
                               or now - _persisted_at >= PERSIST_EVERY_S)
                # before the first check of this run, keep the saved one (the brownout clock guard)
                last_tick = _last_tick if _last_tick is not None else _persisted_last_tick
                state_blob = {"units": copy.deepcopy(_states), "last_tick": last_tick} if write_state else None
                jver = _journal_ver
                journal = copy.deepcopy(_journal[-JOURNAL_MAX:]) if jver != _journal_saved_ver else None
            values = {}
            if state_blob is not None:
                values["ams_autodry_state"] = state_blob
            if journal is not None:
                values["ams_autodry_journal"] = journal
            if not values:
                return
            settings_service.set_many(values)
            with _lock:
                if state_blob is not None:
                    _persisted_blob, _persisted_at = core, now
                if journal is not None:
                    _journal_saved_ver = jver
    except Exception:
        logger.error("[autodry] persisting the state failed", exc_info=True)


# --- the GET payload ---------------------------------------------------------------------

_DRY_FIELDS = ("type", "active", "status", "remaining_min", "temp", "hours", "filament", "feeding", "max_temp",
               "max_by", "blockers")


def limits() -> Dict[str, Any]:
    return {"interrupt_limit": rules.INTERRUPT_LIMIT, "temp_min": ams_drying.TEMP_MIN,
            "hours": [ams_drying.HOURS_MIN, ams_drying.HOURS_MAX],
            "ranges": {k: list(v) for k, v in rules.RANGES.items()},
            "night_window": dict(rules.NIGHT_WINDOW), "tz_offset_min": TZ_OFFSET_MIN,
            "cool_margin_c": rules.COOL_MARGIN_C, "cool_extra_h": rules.COOL_EXTRA_S // 3600,
            "series_backoff_h": rules.SERIES_BACKOFF_S // 3600, "max_units": rules.MAX_UNITS}


def _snapshot_unit(key: str, o: Optional[Dict[str, Any]], disp: Optional[Dict[str, Any]], st: Dict[str, Any],
                   status: Optional[Dict[str, Any]], cfg: Dict[str, Any], now: float) -> Dict[str, Any]:
    pid, ams = rules.parse_key(key)
    present = bool(o and o["present"])
    disp = disp or st["display"] or {}
    dry = o["dry"] if present else None
    slots = o["slots"] if present else []
    ucfg = rules.unit_config(cfg, key)
    mats = rules.materials_of(slots)
    if dry and dry.get("type") != "n3f":
        for m in mats:
            m["ht"] = False                  # an AMS HT dries those materials itself
    if not present and (status or {}).get("kind") not in ("offline", "off"):
        status = {"kind": "offline"}
    return {
        "key": key, "printer_id": pid, "ams": ams,
        "label": disp.get("label") or pid, "name": disp.get("name") or bambu_extras.ams_unit_name(ams),
        "device_type": disp.get("device_type"), "model": disp.get("model"),
        "online": bool(present and o["online"]), "printing": bool(present and o["printing"]),
        "rh": o["rh"] if present else None, "temp": o["temp"] if present else None,
        "dry_ok": o["dry_ok"] if present else None, "cmd_ok": o["cmd_ok"] if present else None,
        "dry": {f: dry.get(f) for f in _DRY_FIELDS} if dry else None,
        "materials": mats, "unit_cfg": ucfg,
        "plan": rules.plan_run(cfg, ucfg, dry, slots) if dry else None,
        "plan_now": rules.plan_run(cfg, ucfg, dry, slots, manual=True) if dry else None,
        "plans": rules.plans_for(dry, slots) if dry else None,
        "plan_ctx": rules.plan_ctx(dry, slots) if dry else None,
        "status": status or {"kind": "pending"},
        "phase": st["phase"], "origin": st["origin"], "seen_active": st["seen_active"], "series": st["series"],
        "runs_24h": len([t for t in st["runs"] if t > now - rules.DAY_S]), "interrupts": st["interrupts"],
        "hold_until": st["hold_until"], "hold_reason": st["hold_reason"],
        "disabled_reason": st["disabled_reason"], "backoff_until": st["backoff_until"],
    }


def snapshot(now: Optional[float] = None) -> Dict[str, Any]:
    """GET /api/ams/autodry: config, limits, engine health, every unit with
    its plans and status, and the journal (newest first). Nothing secret."""
    now = _time() if now is None else now
    cfg = load_config()
    units, disp = _collect(now)
    with _lock:
        states = {k: rules.load_state(v) for k, v in _states.items()}
        status = copy.deepcopy(_status)
        journal = [{k: v for k, v in e.items() if k != "dk"} for e in reversed(_journal)]
        engine = {"started": _started, "started_at": _started_at, "last_tick": _last_tick, "error": _tick_error,
                  "clock_wait": _clock_wait}
    keys = set(states) | {k for k, o in units.items() if o["present"]}
    out = [_snapshot_unit(k, units.get(k), disp.get(k), states.get(k) or rules.new_state(), status.get(k), cfg, now)
           for k in keys if rules.parse_key(k)]
    out.sort(key=lambda u: (str(u["label"]), u["ams"]))
    return {"now": now, "config": cfg, "limits": limits(), "engine": engine, "units": out, "journal": journal}
