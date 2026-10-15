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
    yield
    ai._tries.clear()


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
