"""Auto «Ignore and resume» for listed print errors (P3's 0500_8062, 14.10.2026)."""
import pytest
from fastapi import HTTPException

from app.api import admin
from app.collectors.bambu_raw import RawReport
from app.services import auto_ignore as ai, printer_commands, settings_service

ERR = 0x05008062          # 83918946: strong light on the build plate
PAUSED = {"gcode_state": "PAUSE", "err": ERR, "stage": "identifying_build_plate_type", "job_id": "777"}


@pytest.fixture(autouse=True)
def _clean():
    ai._tries.clear()
    ai._skips.clear()
    yield
    ai._tries.clear()
    ai._skips.clear()


class _St:
    def __init__(self, pid="b3", state="paused", online=True, kind="bambu"):
        self.d = {"id": pid, "label": "P3", "kind": kind, "state": state, "online": online}

    def to_dict(self):
        return dict(self.d)


class _Store:
    def __init__(self, *sts):
        self.sts = sts

    def get_all(self):
        return list(self.sts)


def _run(store, ctx, now, codes=("0500_8062",), result=None):
    sent, audited = [], []
    ai.check_once(store, codes, lambda pid: ctx, lambda pid, err: sent.append((pid, err)) or
                  (result or {"status": "confirmed"}), audited.append, now=now)
    return sent, audited


def test_code_of():
    assert ai.code_of(ERR) == "0500_8062" and ai.code_of(0x0C00_8001) == "0C00_8001"


def test_payload_is_studios_ignore_button():
    assert printer_commands.build_error_ignore(ERR, "777") == {
        "print": {"sequence_id": "0", "command": "ignore", "err": "83918946", "param": "reserve", "job_id": "777"}}


def test_ignores_once_then_retries_after_two_minutes_at_most_three_times():
    store = _Store(_St())
    sent, audited = _run(store, PAUSED, 1000.0)
    assert sent == [("b3", ERR)] and audited == ["auto_ignore:b3:0500_8062:confirmed"]
    assert _run(store, PAUSED, 1000.0 + 60)[0] == []                       # too soon
    assert _run(store, PAUSED, 1000.0 + ai.RETRY_S)[0] == [("b3", ERR)]    # still paused: again
    assert _run(store, PAUSED, 1000.0 + 2 * ai.RETRY_S)[0] == [("b3", ERR)]
    assert _run(store, PAUSED, 1000.0 + 9 * ai.RETRY_S)[0] == []           # 3 tries per job, then hands off
    # the next job pauses on the same error: a fresh try
    assert _run(store, dict(PAUSED, job_id="778"), 5000.0)[0] == [("b3", ERR)]


@pytest.mark.parametrize("ctx,st,codes", [
    (dict(PAUSED, err=0x0300_8001), _St(), ("0500_8062",)),     # another error: never
    (dict(PAUSED, gcode_state="RUNNING"), _St(), ("0500_8062",)),
    (dict(PAUSED, job_id=None), _St(), ("0500_8062",)),
    (PAUSED, _St(state="printing"), ("0500_8062",)),
    (PAUSED, _St(online=False), ("0500_8062",)),
    (PAUSED, _St(kind="creality"), ("0500_8062",)),
    (PAUSED, _St(), ()),                                        # the owner emptied the list
    (None, _St(), ("0500_8062",)),                              # no live telemetry
])
def test_never_sends_outside_the_listed_pause(ctx, st, codes):
    assert _run(_Store(st), ctx, 1000.0, codes=codes)[0] == []


def test_a_listed_pause_that_is_not_ignored_is_audited_once_with_the_reason():
    store = _Store(_St())
    for ctx, why in ((dict(PAUSED, job_id=None), "no_job_id"), (dict(PAUSED, gcode_state="RUNNING"), "state_RUNNING")):
        ai._skips.clear()
        sent, audited = _run(store, ctx, 1000.0)
        assert sent == [] and audited == ["auto_ignore:b3:0500_8062:skip:" + why]
        assert _run(store, ctx, 1015.0) == ([], [])                     # once per pause and reason
    # another error, or no telemetry: nothing to explain
    assert _run(store, dict(PAUSED, err=0x0300_8001), 1000.0) == ([], [])
    assert _run(store, None, 1000.0) == ([], [])
    # all tries used: said once
    ai._skips.clear()
    for t in (0, ai.RETRY_S, 2 * ai.RETRY_S):
        _run(store, PAUSED, 1000.0 + t)
    assert _run(store, PAUSED, 1000.0 + 3 * ai.RETRY_S)[1] == ["auto_ignore:b3:0500_8062:skip:tries_used"]


def test_each_pause_gets_fresh_tries_even_with_job_id_zero():
    # network prints report job_id "0" (17.10): tries must not pool across pauses
    ctx = dict(PAUSED, job_id="0")
    paused, printing = _Store(_St()), _Store(_St(state="printing"))
    for t in (0, ai.RETRY_S, 2 * ai.RETRY_S):
        assert _run(paused, ctx, 1000.0 + t)[0] == [("b3", ERR)]
    assert _run(paused, ctx, 1000.0 + 3 * ai.RETRY_S)[0] == []          # this pause: 3 tries used
    _run(printing, ctx, 2000.0)                                          # it resumed
    assert _run(paused, ctx, 3000.0)[0] == [("b3", ERR)]                 # the next pause: tried again
    _run(_Store(_St(online=False)), ctx, 4000.0)                         # offline is no proof the pause ended
    assert ai._tries


def test_a_plate_that_re_pauses_right_away_still_stops_at_three_tries():
    # ignore -> prints a few seconds -> the marker is still unreadable -> paused again
    ctx = dict(PAUSED, job_id="0")
    paused, printing = _Store(_St()), _Store(_St(state="printing"))
    sent = []
    for i in range(6):
        t0 = 1000.0 + i * ai.RETRY_S
        sent += _run(paused, ctx, t0)[0]
        _run(printing, ctx, t0 + 5)
    assert len(sent) == ai.MAX_TRIES


def test_ignore_print_error_checks_the_live_pause(monkeypatch):
    sent = []
    monkeypatch.setattr(printer_commands, "_send_local", lambda pid, p: sent.append(p) or
                        {"success": True, "replied": True})
    for ctx, detail in ((None, printer_commands.MSG_NO_TELEMETRY),
                        (dict(PAUSED, gcode_state="RUNNING"), printer_commands.MSG_NOT_PAUSED_ON),
                        (dict(PAUSED, err=1), printer_commands.MSG_NOT_PAUSED_ON),
                        (dict(PAUSED, job_id=None), printer_commands.MSG_NO_JOB_ID)):
        monkeypatch.setattr(printer_commands, "live_print_error", lambda pid, c=ctx: c)
        assert printer_commands.ignore_print_error("b3", ERR) == {
            "success": False, "status": "refused", "detail": detail, "replied": False}
    monkeypatch.setattr(printer_commands, "live_print_error", lambda pid: PAUSED)
    assert printer_commands.ignore_print_error("b3", ERR)["status"] == "confirmed"
    assert sent == [printer_commands.build_error_ignore(ERR, "777")]


def test_ignore_print_error_refuses_a_laser_or_cutter_job(monkeypatch):
    # the checks of the notification's «ignore» button (Studio's check_resume_condition)
    sent = []
    monkeypatch.setattr(printer_commands, "_send_local", lambda pid, p: sent.append(p) or
                        {"success": True, "replied": True})
    monkeypatch.setattr(printer_commands, "live_print_error", lambda pid: dict(PAUSED, job_attr=0x22))
    assert printer_commands.ignore_print_error("b3", ERR)["detail"] == printer_commands.MSG_LASER_JOB
    monkeypatch.setattr(printer_commands, "live_print_error", lambda pid: dict(PAUSED, job_id="0", job_attr=18))
    assert printer_commands.ignore_print_error("b3", ERR)["status"] == "confirmed"
    assert sent == [printer_commands.build_error_ignore(ERR, "0")]


# --- view(): the thread's state for the dashboard ----------------------------------------

def _view(now, pid="b3", err=ERR):
    return ai.view(pid, err, now=now)


def test_view_before_any_try():
    assert _view(1000.0) == {"enabled": True, "tries": 0, "max": ai.MAX_TRIES, "next_in_s": None, "skip": None}


def test_view_counts_the_tries_and_the_next_one():
    store = _Store(_St())
    ctx = dict(PAUSED, job_id="0")
    _run(store, ctx, 1000.0)
    assert _view(1030.0) == {"enabled": True, "tries": 1, "max": 3, "next_in_s": ai.RETRY_S - 30, "skip": None}
    assert _view(1000.0 + ai.RETRY_S + 5)["next_in_s"] == 0                 # due: the next pass presses
    _run(store, ctx, 1000.0 + ai.RETRY_S)
    _run(store, ctx, 1000.0 + 2 * ai.RETRY_S)
    _run(store, ctx, 1000.0 + 3 * ai.RETRY_S)                               # tries used: noted as a skip
    v = _view(1000.0 + 3 * ai.RETRY_S)
    assert (v["tries"], v["next_in_s"], v["skip"]) == (3, None, "tries_used")
    # another printer, another code: their own state
    assert _view(1000.0, pid="b4")["tries"] == 0 and _view(1000.0, err=0x0C008009)["tries"] == 0


def test_view_shows_why_it_does_not_press():
    _run(_Store(_St()), dict(PAUSED, job_id=None), 1000.0)
    assert _view(1010.0) == {"enabled": True, "tries": 0, "max": 3, "next_in_s": None, "skip": "no_job_id"}


def test_view_follows_the_current_pause():
    # job_id "0" pools nothing: after FORGET_S out of the pause the next one starts at 0 ...
    ctx = dict(PAUSED, job_id="0")
    paused, printing = _Store(_St()), _Store(_St(state="printing"))
    _run(paused, ctx, 1000.0)
    _run(printing, ctx, 1000.0 + ai.FORGET_S)
    assert _view(1000.0 + ai.FORGET_S)["tries"] == 0
    # ... while a plate that re-pauses within seconds keeps counting
    _run(paused, ctx, 5000.0)
    _run(printing, ctx, 5005.0)
    _run(paused, ctx, 5000.0 + ai.RETRY_S)
    assert _view(5000.0 + ai.RETRY_S)["tries"] == 2


def test_skip_notes_are_written_under_the_lock():
    # view() copies _skips from the API threads: check_once must not write it unlocked
    import threading
    done = threading.Event()
    with ai._lock:
        t = threading.Thread(target=lambda: (_run(_Store(_St()), dict(PAUSED, job_id=None), 1000.0), done.set()))
        t.start()
        assert not done.wait(0.2) and ai._skips == {}
    t.join(5)
    assert done.is_set() and list(ai._skips) == [("b3", "None", ERR, "no_job_id")]


def test_raw_report_keeps_the_job_id():
    r = RawReport()
    r.update({"command": "push_status", "job_id": 777})
    assert r.snapshot()["job_id"] == "777"
    r.update({"command": "push_status", "job_id": ""})        # an empty one does not erase it
    assert r.snapshot()["job_id"] == "777"


def test_setting_default_and_validation():
    # worn plate markers, plates move between printers: both «marker not detected» codes, every Bambu
    assert settings_service.DEFAULTS["auto_ignore_print_errors"] == ["0500_8062", "0C00_8009"]
    assert ai.DEFAULT_CODES == settings_service.DEFAULTS["auto_ignore_print_errors"]
    admin._validate_settings({"auto_ignore_print_errors": ["0500_8062", "0300_8001"]})
    admin._validate_settings({"auto_ignore_print_errors": []})
    for bad in ("0500_8062", ["0500-8062"], ["0500_806"], [5], ["0500_8062"] * 33):
        with pytest.raises(HTTPException):
            admin._validate_settings({"auto_ignore_print_errors": bad})


# --- maintenance mode (spec_maintenance B21): never pressed, said once per pause ------------

def _run_maint(store, ctx, now, maint=lambda pid: True, codes=("0500_8062",)):
    sent, audited = [], []
    ai.check_once(store, codes, lambda pid: ctx, lambda pid, err: sent.append((pid, err)) or {"status": "confirmed"},
                  audited.append, now=now, maint=maint)
    return sent, audited


def test_a_printer_in_maintenance_is_never_pressed_and_said_once_per_pause():
    paused, printing = _Store(_St()), _Store(_St(state="printing"))
    assert _run_maint(paused, PAUSED, 1000.0) == ([], ["auto_ignore:b3:0500_8062:skip:maintenance"])
    assert _run_maint(paused, PAUSED, 1000.0 + ai.RETRY_S) == ([], [])        # one row per pause
    assert ai._tries == {}                                                     # no try spent
    assert _view(1000.0 + ai.RETRY_S)["skip"] == "maintenance" and _view(1000.0 + ai.RETRY_S)["tries"] == 0
    # out of the pause (forget) and a new one: one row again
    _run_maint(printing, PAUSED, 2000.0)
    assert _run_maint(paused, PAUSED, 2100.0) == ([], ["auto_ignore:b3:0500_8062:skip:maintenance"])
    # another code, no telemetry: nothing to say
    ai._skips.clear()
    assert _run_maint(paused, dict(PAUSED, err=0x0300_8001), 3000.0) == ([], [])
    assert _run_maint(paused, None, 3000.0) == ([], [])
    # the mode is off: the same pause gets its usual first try, and view() stops saying "maintenance"
    _run_maint(paused, PAUSED, 4000.0)
    sent, audited = _run_maint(paused, PAUSED, 4015.0, maint=lambda pid: False)
    assert sent == [("b3", ERR)] and audited == ["auto_ignore:b3:0500_8062:confirmed"]
    assert (_view(4015.0)["tries"], _view(4015.0)["skip"]) == (1, None)


def test_maintenance_is_per_printer_and_a_failing_lookup_counts_as_no():
    store = _Store(_St(pid="b3"), _St(pid="b4"))
    sent, audited = _run_maint(store, PAUSED, 1000.0, maint=lambda pid: pid == "b3")
    assert sent == [("b4", ERR)]
    assert sorted(audited) == ["auto_ignore:b3:0500_8062:skip:maintenance", "auto_ignore:b4:0500_8062:confirmed"]

    def broken(pid):
        raise RuntimeError("registry down")

    ai._tries.clear()
    assert _run_maint(_Store(_St()), PAUSED, 2000.0, maint=broken)[0] == [("b3", ERR)]


def test_the_thread_asks_the_registry(monkeypatch):
    import inspect

    from app.services import printer_registry

    monkeypatch.setattr(printer_registry, "in_maintenance", lambda pid, *a, **k: pid == "b3")
    sent, audited = [], []
    ai.check_once(_Store(_St()), ("0500_8062",), lambda pid: PAUSED, lambda pid, err: sent.append(pid) or {},
                  audited.append, now=1000.0)                                  # maint=None: the registry's
    assert sent == [] and audited == ["auto_ignore:b3:0500_8062:skip:maintenance"]
    assert "maint=printer_registry.in_maintenance" in inspect.getsource(ai._loop)
