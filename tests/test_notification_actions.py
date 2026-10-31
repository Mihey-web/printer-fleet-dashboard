"""Notification actions (app/services/notification_actions.py, spec §5).

The registry and actions_for(); run() with its live checks against a stubbed
live status (and once end to end through an offline vendored client); the
fleet reset's reset_skip_reason / reset_one. Nothing reaches a printer: sends
are stubs. Bambu's action table is a tiny synthetic one.
"""
import json
from types import SimpleNamespace

import pytest

from app.domain.models import PrinterKind, PrinterState, PrinterStatus
from app.services import auto_ignore, hms_catalog, notification_actions as na, printer_commands as pc, settings_service

P = "Q7X"
CTX = {"err": 0x05008062, "job_id": "0", "subtask_id": ""}


@pytest.fixture(autouse=True)
def table(monkeypatch):
    monkeypatch.setattr(hms_catalog, "_index", {})
    monkeypatch.setattr(settings_service, "_cache", None)

    def load(act):
        monkeypatch.setattr(hms_catalog, "_index", {P: {"act": act, "err": {}, "hms": {}, "ver": ("1", "2")}})
    return load


@pytest.mark.parametrize("key,payload", [
    ("resume", {"print": {"sequence_id": "0", "command": "resume", "err": "83918946", "param": "reserve",
                          "job_id": "0"}}),
    ("ignore", {"print": {"sequence_id": "0", "command": "ignore", "err": "83918946", "param": "reserve",
                          "job_id": "0"}}),
    ("stop", {"print": {"sequence_id": "0", "command": "stop", "err": "83918946", "param": "reserve", "job_id": "0"}}),
    ("ams_done", {"print": {"sequence_id": "0", "command": "ams_control", "param": "done"}}),
    ("ams_retry", {"print": {"sequence_id": "0", "command": "ams_control", "param": "resume"}}),
    ("ams_abort", {"print": {"sequence_id": "0", "command": "ams_control", "param": "abort"}}),
    ("clean", {"print": {"sequence_id": "0", "command": "clean_print_error", "subtask_id": "",
                         "print_error": 83918946}}),
    ("idle_ignore", {"print": {"sequence_id": "0", "command": "idle_ignore", "err": "83918946", "type": 0}}),
    ("buzzer_off", {"print": {"sequence_id": "0", "command": "buzzer_ctrl", "mode": 0}}),
    ("dry_stop", {"print": {"sequence_id": "0", "command": "auto_stop_ams_dry"}}),
    ("refresh_nozzle", {"print": {"sequence_id": "0", "command": "refresh_nozzle"}}),
    ("purifier_off", {"print": {"sequence_id": "0", "command": "close_air_filt"}}),
    ("close_dialog", {"system": {"sequence_id": "0", "command": "uiop", "name": "print_error", "action": "close",
                                 "source": 1, "type": "dialog", "err": "05008062"}}),
])
def test_payloads(key, payload):
    # err: a decimal string in the hms commands, an int in clean_print_error, hex only in uiop.
    assert na.ACTIONS[key].builder(CTX) == payload


def test_ignore_is_the_existing_builder():
    from app.services.printer_commands import build_error_ignore
    assert na.ACTIONS["ignore"].builder(CTX) == build_error_ignore(0x05008062, "0")


def test_klipper_actions_are_moonraker_methods():
    assert (na.ACTIONS["fw_restart"].rpc, na.ACTIONS["klippy_restart"].rpc) == ("printer.firmware_restart",
                                                                               "printer.restart")
    assert na.ACTIONS["fw_restart"].confirm and na.ACTIONS["fw_restart"].danger
    assert na.actions_for("klipper", "webhooks: shutdown", None, "klipper") == ([], False)


def test_klipper_buttons_by_host_state():
    assert [(a["key"], a["label"], a["style"], a["yes"]) for a in na.klipper_actions("Shutdown")] == [
        ("fw_restart", "Перезапуск прошивки", "danger", "Перезапустить")]
    assert [(a["key"], a["style"]) for a in na.klipper_actions("error")] == [("klippy_restart", "primary"),
                                                                           ("fw_restart", "danger")]
    for state in ("ready", "startup", "disconnected", None):
        assert na.klipper_actions(state) == []


def test_button_ids_map_to_actions_and_the_rest_are_dropped():
    assert na.ACTION_BY_ID == {2: "resume", 3: "resume", 4: "resume", 12: "resume", 28: "resume", 27: "ignore",
                               25: "ignore", 5: "stop", 7: "ams_done", 10001: "ams_done", 8: "ams_retry",
                               9: "ams_retry", 34: "ams_retry", 10002: "ams_retry", 51: "ams_abort", 11: "clean",
                               10004: "clean", 23: "idle_ignore", 29: "buzzer_off", 35: "dry_stop",
                               24: "refresh_nozzle", 54: "purifier_off"}


def test_confirmations_and_guards():
    for key in ("stop", "ams_abort", "fw_restart", "klippy_restart"):
        assert na.ACTIONS[key].confirm, key
    assert "job_attr" in na.ACTIONS["resume"].guards and "job_attr" not in na.ACTIONS["stop"].guards
    assert na.ACTIONS["close_dialog"].enabled is False and na.ENABLE_UIOP is False


def test_labels_by_id_dedup_and_styles(table):
    table({"07008005": {"dev": [9, 6, 8, 9]}, "03008007": {"def": [5, 2, 39]}, "0500C011": {"dev": [11, 1, 13, 56]}})
    actions, blocking = na.actions_for("print_error", "0700_8005", P)
    assert [(a["key"], a["label"], a["style"], a["bambu_id"]) for a in actions] == [
        ("ams_retry", "Готово — продолжить", "primary", 9)]
    assert blocking is False
    actions, blocking = na.actions_for("print_error", "0300_8007", P)
    assert [(a["key"], a["style"]) for a in actions] == [("stop", "danger"), ("resume", "primary")]
    assert blocking is True
    actions, _ = na.actions_for("print_error", "0500_C011", P)
    assert [(a["key"], a["label"]) for a in actions] == [("clean", "ОК")]


def test_studio_fixed_codes_get_retry_and_ok_without_a_table():
    actions, _ = na.actions_for("print_error", "0701_8004", None)
    assert [(a["key"], a["label"], a["bambu_id"]) for a in actions] == [
        ("ams_retry", "Повторить", 10002), ("clean", "ОК", 10004)]


def test_hms_codes_get_only_the_live_actions(table):
    table({"0500040000030057": {"dev": [54, 2, 11, 29]}})
    actions, _ = na.actions_for("hms", "0500_0400_0003_0057", P)
    assert [a["key"] for a in actions] == ["purifier_off", "buzzer_off"]


def test_the_cancel_echo_closes_the_dialog_whatever_the_table(table):
    table({"0300400C": {"dev": [5]}})
    for code in ("0300_400C", "0500_400E"):
        assert na.actions_for("print_error", code, P) == ([{
            "key": "clean", "label": "Закрыть диалог", "style": "primary", "confirm": None, "yes": None,
            "bambu_id": None}], False)


def test_fallback_without_a_table():
    assert [a["key"] for a in na.actions_for("print_error", "0C00_8009", P)[0]] == ["ignore"]
    assert na.actions_for("print_error", "0700_8011", P) == ([], False)
    assert na.table_known(P, "0700_8011") is False
    assert na.actions_for("print_error", "not hex", P) == ([], False)


# --- run(): live checks, confirmation, in-flight mark, verdicts ---------------------------

ERR = 0x05008062
PE_KEY = "pe:0500_8062"
PAUSED_CTX = {"gcode_state": "PAUSE", "err": ERR, "stage": None, "job_id": "0", "subtask_id": "", "job_attr": 18}
CONFIRMED = {"success": True, "status": "confirmed", "detail": "подтверждено принтером", "replied": True}


def REFUSED(detail):
    return {"success": False, "status": "refused", "detail": detail, "replied": False}


def _acts(*keys):
    return [{"key": k, "label": k, "style": "", "confirm": None, "yes": None, "bambu_id": None} for k in keys]


def _rec(key=PE_KEY, src="print_error", code="0500_8062", severity="pause", actions=("ignore", "resume", "stop"),
         blocking=False):
    return {"key": key, "src": src, "code": code, "severity": severity, "actions": _acts(*actions),
            "blocking": blocking}


def _status(*records, kind=PrinterKind.BAMBU, state=PrinterState.PAUSED, pid="b3"):
    return PrinterStatus(id=pid, label="P3", kind=kind, online=True, state=state, notifications=list(records))


@pytest.fixture
def env(monkeypatch):
    """run() against a stubbed live status; records what would be sent."""
    st = SimpleNamespace(kind="bambu", status=_status(_rec()), ctx=dict(PAUSED_CTX), sent=[], rpc=[], refresh=[],
                         reply={"success": True, "replied": True, "detail": "подтверждено принтером"}, clock=[1000.0])
    monkeypatch.setattr("app.services.printer_registry.get_printer",
                        lambda pid: None if pid == "nope" else {"id": pid, "kind": st.kind, "model": "P2S"})
    monkeypatch.setattr(na, "_live", lambda _pc, pid, prn: None if st.status is None else (st.status, st.ctx))
    monkeypatch.setattr(pc, "_send_local", lambda pid, payload: st.sent.append(payload) or dict(st.reply))
    monkeypatch.setattr(pc, "_note_reply", lambda *a: None)
    monkeypatch.setattr(pc, "send_klipper_rpc", lambda pid, action: st.rpc.append(action) or dict(st.reply))
    monkeypatch.setattr(pc, "_start_fast_refresh",
                        lambda pid, prn, expect=None, max_s=None: st.refresh.append((pid, expect, max_s)))
    monkeypatch.setattr(pc, "_monotonic", lambda: st.clock[0])
    na._marks.clear()
    yield st
    na._marks.clear()


def test_ignore_sends_studios_button_and_follows_the_printer_up(env):
    assert na.run("b3", PE_KEY, "ignore") == CONFIRMED
    assert env.sent == [pc.build_error_ignore(ERR, "0")]          # job_id "0" is a real job id (network prints)
    [(pid, expect, max_s)] = env.refresh
    assert (pid, max_s) == ("b3", na.REFRESH_S)
    assert expect(_status(state=PrinterState.PRINTING)) and not expect(_status())


@pytest.mark.parametrize("action,payload", [
    ("resume", {"print": {"sequence_id": "0", "command": "resume", "err": "83918946", "param": "reserve",
                          "job_id": "0"}}),
    ("stop", {"print": {"sequence_id": "0", "command": "stop", "err": "83918946", "param": "reserve",
                        "job_id": "0"}}),
])
def test_resume_and_stop_payloads(env, action, payload):
    assert na.run("b3", PE_KEY, action, confirmed=True)["status"] == "confirmed"
    assert env.sent == [payload]


def test_clean_closes_the_cancel_echo_in_any_state(env):
    env.status = _status(_rec("pe:0300_400C", code="0300_400C", severity="common", actions=("clean",)),
                         state=PrinterState.CANCELLED)
    env.ctx = dict(PAUSED_CTX, gcode_state="FAILED", err=0x0300400C, subtask_id=None)
    assert na.run("b3", "pe:0300_400C", "clean")["status"] == "confirmed"
    # subtask_id as a string ("" when the report has none), print_error as an int
    assert env.sent == [{"print": {"sequence_id": "0", "command": "clean_print_error", "subtask_id": "",
                                   "print_error": 0x0300400C}}]
    expect = env.refresh[0][1]
    assert expect(_status()) and not expect(env.status)            # waits for the record to go


@pytest.mark.parametrize("ctx,action,detail", [
    (dict(PAUSED_CTX, err=0x07008011), "ignore", pc.MSG_NOT_PAUSED_ON),      # another error now
    (dict(PAUSED_CTX, err=0x07008011), "stop", pc.MSG_NOT_PAUSED_ON),
    (dict(PAUSED_CTX, gcode_state="RUNNING"), "ignore", pc.MSG_NOT_PAUSED),
    (dict(PAUSED_CTX, gcode_state="RUNNING"), "resume", pc.MSG_NOT_PAUSED),
    (dict(PAUSED_CTX, job_attr=0x20), "ignore", pc.MSG_LASER_JOB),           # laser / cutter job
    (dict(PAUSED_CTX, job_attr=0x22), "resume", pc.MSG_LASER_JOB),
    (dict(PAUSED_CTX, job_id=None), "resume", pc.MSG_NO_JOB_ID),
    (dict(PAUSED_CTX, job_id=""), "ignore", pc.MSG_NO_JOB_ID),
    (dict(PAUSED_CTX, job_id=None), "stop", pc.MSG_NO_JOB_ID),
])
def test_live_checks_refuse_and_send_nothing(env, ctx, action, detail):
    env.ctx = ctx
    assert na.run("b3", PE_KEY, action, confirmed=True) == REFUSED(detail)
    assert env.sent == [] and env.refresh == [] and na._marks == {}


def test_stop_skips_the_job_attr_and_pause_checks_as_studio(env):
    env.ctx = dict(PAUSED_CTX, job_attr=0x20, gcode_state="RUNNING")
    assert na.run("b3", PE_KEY, "stop", confirmed=True)["status"] == "confirmed"


def test_the_cancel_echo_already_closed(env):
    env.status = _status(_rec("pe:0300_400C", code="0300_400C", severity="common", actions=("clean",)))
    env.ctx = dict(PAUSED_CTX, err=0)
    assert na.run("b3", "pe:0300_400C", "clean") == REFUSED(na.MSG_DIALOG_CLOSED)


@pytest.mark.parametrize("action", ["stop", "ams_abort"])
def test_dangerous_actions_need_the_confirmation(env, action):
    env.status = _status(_rec(actions=("stop", "ams_abort")))
    assert na.run("b3", PE_KEY, action) == REFUSED(na.MSG_CONFIRM)
    assert na.run("b3", PE_KEY, action, confirmed="yes") == REFUSED(na.MSG_CONFIRM)   # only True confirms
    assert env.sent == []
    assert na.run("b3", PE_KEY, action, confirmed=True)["status"] == "confirmed"


@pytest.mark.parametrize("pid,key,action,detail", [
    ("b3", "pe:0700_8011", "ignore", na.MSG_GONE),                 # no such record live
    ("b3", PE_KEY, "ams_retry", na.MSG_NOT_LISTED),                # not one of its buttons
    ("b3", PE_KEY, "close_dialog", na.MSG_UNKNOWN_ACTION),         # uiop: off (ENABLE_UIOP)
    ("b3", PE_KEY, "skip_objects", na.MSG_UNKNOWN_ACTION),
    ("b3", PE_KEY, "fw_restart", pc.MSG_NOT_SUPPORTED),            # a Klipper action on a Bambu
    ("nope", PE_KEY, "ignore", pc.MSG_KIND),
])
def test_requests_that_do_not_fit_the_live_record(env, pid, key, action, detail):
    assert na.run(pid, key, action, confirmed=True) == REFUSED(detail)
    assert env.sent == [] and env.rpc == []


def test_no_live_telemetry(env):
    env.status = None
    assert na.run("b3", PE_KEY, "ignore") == REFUSED(pc.MSG_NO_TELEMETRY)


def test_a_print_error_command_on_an_hms_record_is_refused(env):
    env.status = _status(_rec("hms:0500_0400_0003_0057", src="hms", code="0500_0400_0003_0057",
                              severity="common", actions=("resume", "buzzer_off")))
    assert na.run("b3", "hms:0500_0400_0003_0057", "resume") == REFUSED(na.MSG_NOT_LISTED)
    # the live-only actions need nothing but the record: sent whatever print_error says
    env.ctx = dict(PAUSED_CTX, err=0)
    assert na.run("b3", "hms:0500_0400_0003_0057", "buzzer_off")["status"] == "confirmed"
    assert env.sent == [{"print": {"sequence_id": "0", "command": "buzzer_ctrl", "mode": 0}}]


def test_a_second_press_within_ten_seconds_is_refused(env):
    assert na.run("b3", PE_KEY, "ignore")["status"] == "confirmed"
    env.clock[0] += na.ACTION_GUARD_S - 1
    assert na.run("b3", PE_KEY, "resume") == REFUSED(na.MSG_INFLIGHT)
    assert na.run("b4", PE_KEY, "resume")["status"] == "confirmed"          # another printer is free
    env.clock[0] += 1
    assert na.run("b3", PE_KEY, "resume")["status"] == "confirmed"
    assert len(env.sent) == 3


def test_a_failed_send_gives_the_mark_back(env):
    env.reply = {"success": False, "replied": True, "detail": "принтер отклонил: x"}
    assert na.run("b3", PE_KEY, "ignore") == {"success": False, "status": "rejected",
                                              "detail": "принтер отклонил: x", "replied": True}
    assert na._marks == {} and env.refresh == []
    env.reply = {"success": True, "replied": False, "detail": "отправлено (без подтверждения)"}
    assert na.run("b3", PE_KEY, "ignore") == {"success": True, "status": "sent",
                                              "detail": "отправлено (без подтверждения)", "replied": False}
    assert len(env.refresh) == 1


def _klipper_status(*records):
    return _status(*records, kind=PrinterKind.KLIPPER, state=PrinterState.ERROR, pid="k1")


def test_klipper_firmware_restart(env):
    env.kind = "klipper"
    env.status = _klipper_status(_rec("klipper:shutdown", src="klipper", code="webhooks: shutdown",
                                      severity="fatal", actions=("fw_restart",)))
    env.ctx = {}
    assert na.run("k1", "klipper:shutdown", "fw_restart") == REFUSED(na.MSG_CONFIRM)
    assert na.run("k1", "klipper:shutdown", "klippy_restart", confirmed=True) == REFUSED(na.MSG_NOT_LISTED)
    assert na.run("k1", "klipper:shutdown", "fw_restart", confirmed=True)["status"] == "confirmed"
    assert env.rpc == ["fw_restart"] and env.sent == []
    expect = env.refresh[0][1]
    assert expect(SimpleNamespace(ctl_ready=True)) and not expect(SimpleNamespace(ctl_ready=False))
    assert na.run("k1", PE_KEY, "ignore") == REFUSED(pc.MSG_NOT_SUPPORTED)    # Bambu actions: not here


def test_klipper_restarts_only_in_their_host_state(env):
    env.kind, env.ctx = "klipper", {}
    env.status = _klipper_status(_rec("klipper:error", src="klipper", code="webhooks: error", severity="fatal",
                                      actions=("klippy_restart", "fw_restart")),
                                 _rec("klipper:startup", src="klipper", code="webhooks: startup", severity="info",
                                      actions=("fw_restart", "klippy_restart")))
    assert na.run("k1", "klipper:error", "klippy_restart", confirmed=True)["status"] == "confirmed"
    env.clock[0] += na.ACTION_GUARD_S
    for action in ("fw_restart", "klippy_restart"):
        assert na.run("k1", "klipper:startup", action, confirmed=True) == REFUSED(na.MSG_KLIPPY_STATE)
    assert env.rpc == ["klippy_restart"]


def test_send_klipper_rpc_only_runs_the_restarts(monkeypatch):
    calls = []
    monkeypatch.setattr(pc, "_send_klipper", lambda pid, action: calls.append(action) or {"success": True})
    assert pc.send_klipper_rpc("k1", "pause") == {"success": False, "detail": pc.MSG_NOT_SUPPORTED, "replied": False}
    assert pc.send_klipper_rpc("k1", "fw_restart") == {"success": True}
    assert calls == ["fw_restart"]


# --- end to end: a paused P2S through the offline vendored client ------------------------------

class _LiveStore:
    def __init__(self, collector):
        self.collector, self.published = collector, []

        def publish(payload):
            self.published.append(payload)
            pc.note_local_reply("SN1", json.dumps({"print": {"sequence_id": payload["print"]["sequence_id"],
                                                             "result": "success", "reason": ""}}).encode())
            return True

        collector.client.publish = publish

    def get_collector(self, pid):
        return self.collector

    def feed(self, report):
        import bambu_testkit as kit
        kit.deliver(self.collector, {"print": dict({"command": "push_status", "msg": 0}, **report)})


@pytest.fixture
def live_p2s(monkeypatch, offline_collector, table):
    monkeypatch.setattr(pc, "REPLY_TIMEOUT", 0.5)
    monkeypatch.setattr(pc, "_start_fast_refresh", lambda *a, **k: None)
    monkeypatch.setattr(pc, "_note_capability", lambda *a, **k: None)
    monkeypatch.setattr("app.services.printer_registry.get_printer",
                        lambda pid: {"id": pid, "kind": "bambu", "model": "P2S"})
    monkeypatch.setattr(hms_catalog, "_prefix_by_pid", {})
    monkeypatch.setattr(hms_catalog, "_index", {"22E": {"act": {"05008062": {"dev": [27, 28]}}, "err": {}, "hms": {},
                                                        "ver": ("1", "2")}})
    na._marks.clear()
    store = _LiveStore(offline_collector("P2S"))
    pc.set_store(store)
    yield store
    pc.set_store(None)
    na._marks.clear()


def test_end_to_end_ignore_on_a_live_pause(live_p2s):
    live_p2s.feed({"gcode_state": "PAUSE", "print_error": ERR, "job_id": "0", "subtask_id": "", "job_attr": 18})
    st = pc.live_status("p1")
    [n] = [n for n in st.notifications if n["src"] == "print_error"]
    assert (n["key"], [(a["key"], a["label"]) for a in n["actions"]]) == (
        PE_KEY, [("ignore", "Игнорировать и продолжить"), ("resume", "Устранено — продолжить")])
    assert na.run("p1", PE_KEY, "ignore")["status"] == "confirmed"
    [sent] = live_p2s.published
    assert dict(sent["print"], sequence_id="0") == pc.build_error_ignore(ERR, "0")["print"]
    assert sent["print"]["sequence_id"] != "0"                     # _send_local's own sequence id
    # the printer resumed: the record is gone, nothing more is sent
    live_p2s.feed({"gcode_state": "RUNNING", "print_error": 0})
    assert na.run("p1", PE_KEY, "resume") == REFUSED(na.MSG_GONE)
    assert len(live_p2s.published) == 1


def test_end_to_end_laser_job_is_refused(live_p2s):
    live_p2s.feed({"gcode_state": "PAUSE", "print_error": ERR, "job_id": "0", "job_attr": 0x22})
    assert na.run("p1", PE_KEY, "resume") == REFUSED(pc.MSG_LASER_JOB)
    assert live_p2s.published == []


# --- the fleet reset: reset_skip_reason / reset_one ---------------------------------------------

CANCEL_REC = _rec("pe:0300_400C", code="0300_400C", severity="common", actions=("clean",))


@pytest.fixture
def fleet(monkeypatch):
    monkeypatch.setattr(settings_service, "get", lambda key, **k: [] if key == "printer_light_disabled" else {})
    monkeypatch.setattr(settings_service, "_cache", {"auto_ignore_print_errors": ["0500_8062"]})
    pc._capability.clear()
    for pid in ("b1", "b2", "b3"):
        pc._capability[pid] = {"print_cmds": True, "via": "fun", "ts": 0}
    pc._capability["b-blocked"] = {"print_cmds": False, "via": "local", "ts": 0}
    auto_ignore._tries.clear()
    auto_ignore._skips.clear()
    yield
    pc._capability.clear()
    auto_ignore._tries.clear()
    auto_ignore._skips.clear()


def _why(status):
    return na.reset_skip_reason(status.to_dict())


def _b(*records, pid="b1", state=PrinterState.PAUSED, **kw):
    return PrinterStatus(id=pid, label=pid, kind=PrinterKind.BAMBU, online=kw.get("online", True), state=state,
                         grace_period_active=kw.get("grace", False), notifications=list(records))


@pytest.mark.parametrize("status,why", [
    (_b(CANCEL_REC, state=PrinterState.CANCELLED), None),
    (_b(CANCEL_REC, _rec("hms:0300_1100_0002_0001", src="hms", code="0300_1100_0002_0001", severity="serious",
                         actions=())), None),
    (_b(), "уведомлений нет"),
    (_b(CANCEL_REC, online=False, state=PrinterState.OFFLINE), "офлайн"),
    (_b(CANCEL_REC, grace=True), "нет связи — данные устарели"),
    (_b(CANCEL_REC, pid="b-blocked"), "прошивка блокирует (нет Developer Mode)"),
    (_b(CANCEL_REC, pid="b-new"), "доступ к управлению ещё проверяется"),
    (_b(_rec("pe:0700_8011", code="0700_8011", actions=("resume",))),
     "пауза-ошибка 0700_8011 — решается кнопками в уведомлении"),
    (_b(_rec("pe:0300_8007", code="0300_8007", actions=("stop", "resume"), blocking=True)),
     "0300_8007 после отключения питания — выбор обязателен: продолжить или остановить"),
    (_b(_rec("pe:0300_C001", code="0300_C001", severity="common", actions=("clean",), blocking=True)),
     "0300_C001 — выбор обязателен: решите кнопками в уведомлении"),
    (_b(_rec("pe:0300_4000", code="0300_4000", severity="fatal", actions=()), state=PrinterState.ERROR),
     "ошибка 0300_4000 — команды сброса нет, решите на экране принтера"),
    (_b(_rec("hms:0C00_0100_0001_0004", src="hms", code="0C00_0100_0001_0004", severity="fatal", actions=())),
     "HMS не сбрасываются командой — принтер снимет сам, когда причину устранят"),
    (_b(_rec("hms:0300_1100_0002_0001", src="hms", code="0300_1100_0002_0001", severity="serious", actions=())),
     "только HMS — сбрасывать нечего, принтер снимет их сам"),
])
def test_reset_skip_reason_for_bambu(fleet, status, why):
    assert _why(status) == why


def test_reset_skip_reason_says_when_auto_ignore_is_on_it(fleet):
    status = _b(_rec(actions=("ignore", "resume")))
    held = "пауза-ошибка 0500_8062 — решается кнопками в уведомлении"
    tail = ", автопропуск уже пробует"
    assert _why(status) == held + tail                                      # a listed code: the thread is on it
    auto_ignore._tries[("b1", "0", ERR)] = (1, 1000.0)
    assert _why(status) == held + tail
    auto_ignore._tries[("b1", "0", ERR)] = (3, 1000.0)                      # all tries used
    assert _why(status) == held
    auto_ignore._tries.clear()
    auto_ignore._skips[("b1", "None", ERR, "no_job_id")] = 1000.0           # it cannot press
    assert _why(status) == held
    # /api/printers' own "auto" wins over a fresh look
    auto_ignore._skips.clear()
    d = status.to_dict()
    d["notifications"][0]["auto"] = {"enabled": True, "tries": 3, "max": 3, "next_in_s": None, "skip": None}
    assert na.reset_skip_reason(d) == held


def test_reset_skip_reason_for_the_other_kinds(fleet):
    from app.services.normalizer import normalize_ws_dict

    def klipper(webhooks, state="standby"):
        return normalize_ws_dict("k1", "k1", PrinterKind.KLIPPER, {"state": state, "debug": {"webhooks_state": webhooks}})

    assert _why(klipper("shutdown")) == "Klipper: перезапуск прошивки — только по одному, с подтверждением"
    assert _why(klipper("disconnected")) == "Klipper: команды сброса нет — устраните причину на принтере"
    assert _why(klipper("ready")) == "уведомлений нет"
    creality = PrinterStatus(id="c1", label="c", kind=PrinterKind.CREALITY, online=True, state=PrinterState.ERROR,
                             notifications=[_rec("creality:2/5", src="creality", code="errcode 2 · key 5",
                                                 severity="fatal", actions=())])
    assert _why(creality) == "Creality: команды сброса нет — только на экране принтера"
    mks = PrinterStatus(id="m1", label="m", kind=PrinterKind.MKS, online=True, state=PrinterState.PRINTING)
    assert _why(mks) == "управление не подключено"


def test_resettable_picks_only_a_closing_record():
    assert na.resettable([_rec(), CANCEL_REC]) is CANCEL_REC
    assert na.resettable([_rec(actions=("clean",))]) is None                   # a pause decides with its buttons
    assert na.resettable([dict(CANCEL_REC, blocking=True)]) is None
    assert na.resettable([_rec("hms:x", src="hms", severity="common", actions=("clean",))]) is None


def test_reset_one_closes_the_live_record(env):
    env.status = _status(_rec("hms:0300_1100_0002_0001", src="hms", code="0300_1100_0002_0001", severity="serious",
                              actions=()), CANCEL_REC, state=PrinterState.CANCELLED)
    env.ctx = dict(PAUSED_CTX, gcode_state="FAILED", err=0x0300400C)
    assert na.reset_one("b3", "reset_errors", {})["status"] == "confirmed"
    assert [p["print"]["command"] for p in env.sent] == ["clean_print_error"]
    env.clock[0] += na.ACTION_GUARD_S
    env.status = _status(_rec())                                    # only a pause: nothing to reset
    assert na.reset_one("b3") == REFUSED(na.RESET_GONE)
    env.kind = "klipper"
    assert na.reset_one("k1") == REFUSED(na.RESET_GONE)
    assert len(env.sent) == 1


def test_reset_one_sends_only_the_record_the_admin_confirmed(env):
    # the dialog showed the cancel echo; live it is a fatal error with «ОК» now: not sent
    fatal = _rec("pe:0300_4000", code="0300_4000", severity="fatal", actions=("clean",))
    env.status = _status(fatal, state=PrinterState.ERROR)
    env.ctx = dict(PAUSED_CTX, gcode_state="FAILED", err=0x03004000)
    assert na.reset_one("b3", "reset_errors", {"key": "pe:0300_400C"}) == REFUSED(na.RESET_CHANGED)
    assert env.sent == [] and na._marks == {}
    assert na.reset_one("b3", "reset_errors", {"key": "pe:0300_4000"})["status"] == "confirmed"
    assert [p["print"]["print_error"] for p in env.sent] == [0x03004000]
