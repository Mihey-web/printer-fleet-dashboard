"""Fleet-wide (bulk) commands: eligibility, concurrency, deadline, family locks.

No wall-clock assertions: concurrency is proven with a Barrier, deadlines with
small patched values and Events the test releases itself.
"""
import threading

import pytest

from app.domain.models import PrinterKind, PrinterState, PrinterStatus
from app.services import fleet_commands, printer_commands
from app.services.normalizer import normalize_ws_dict

CONFIRMED = {"success": True, "status": "confirmed", "detail": "подтверждено принтером", "replied": True}


class _Store:
    def __init__(self, statuses):
        self._s = list(statuses)

    def get_all(self):
        return list(self._s)


def _bambu(pid, state=PrinterState.PRINTING, online=True, grace=False, label=None):
    return PrinterStatus(id=pid, label=label or pid.upper(), kind=PrinterKind.BAMBU, online=online,
                         state=state if online else PrinterState.OFFLINE, grace_period_active=grace,
                         device_type="P2S", light_on=True)


def _klipper(pid, webhooks="ready", pin="caselight", state="printing"):
    data = {"state": state, "progress": 40, "remaining_time": 600, "debug": {"webhooks_state": webhooks},
            "light_pin": pin}
    return normalize_ws_dict(pid, pid, PrinterKind.KLIPPER, data)


@pytest.fixture(autouse=True)
def _verdicts(monkeypatch):
    from app.services import settings_service

    monkeypatch.setattr(settings_service, "get", lambda key, **k: ["c-dark"] if key == "printer_light_disabled" else {})
    printer_commands._capability.clear()
    for pid in ("b1", "b2", "b3", "b4", "b5", "b6", "b7", "b8", "b-grace", "b-off", "b-fin", "b-idle"):
        printer_commands._capability[pid] = {"print_cmds": True, "via": "fun", "ts": 0}
    printer_commands._capability["b-blocked"] = {"print_cmds": False, "via": "local", "ts": 0}
    yield
    printer_commands._capability.clear()


def _row(d, action="pause"):
    caps, note = printer_commands.caps_for(d)
    return fleet_commands.skip_reason(action, d, caps, note)


@pytest.mark.parametrize("status,action,why", [
    (_bambu("b1"), "pause", None),
    (_bambu("b1"), "light_on", None),                                    # light also while printing
    (_bambu("b1"), "resume", "принтер не на паузе"),
    (_bambu("b-fin", PrinterState.FINISHED), "pause", "печать завершена"),
    (_bambu("b-idle", PrinterState.IDLE), "pause", "принтер не печатает"),
    (_bambu("b-idle", PrinterState.ERROR), "pause", "принтер в ошибке — см. карточку"),
    (_bambu("b1", PrinterState.PAUSED), "resume", None),
    (_bambu("b-off", online=False), "pause", "офлайн"),
    (_bambu("b-off", online=False), "light_off", "офлайн"),
    (_bambu("b-grace", grace=True), "pause", "нет связи — данные устарели"),
    (_bambu("b-grace", grace=True), "light_on", "нет связи — данные устарели"),
    (_bambu("b-blocked"), "pause", "прошивка блокирует (нет Developer Mode)"),
    (_bambu("b-blocked"), "light_on", None),
    (_bambu("b-new"), "pause", "доступ к управлению ещё проверяется"),
    (PrinterStatus(id="m1", label="M", kind=PrinterKind.MKS, online=False, state=PrinterState.OFFLINE),
     "pause", "офлайн · управление не подключено"),
    (PrinterStatus(id="m1", label="M", kind=PrinterKind.MKS, online=True, state=PrinterState.PRINTING),
     "pause", "управление не подключено"),
    (PrinterStatus(id="c1", label="C", kind=PrinterKind.CREALITY, online=True, state=PrinterState.PRINTING),
     "light_on", None),                                                  # Creality: the light by default (27.10)
    (PrinterStatus(id="c-dark", label="C", kind=PrinterKind.CREALITY, online=True, state=PrinterState.PRINTING),
     "light_on", "нет управляемой подсветки"),                           # listed in printer_light_disabled
    (_klipper("k1"), "pause", None),
    (_klipper("k1", pin=""), "light_on", "нет управляемой подсветки"),
    (_klipper("k1", webhooks="shutdown"), "pause", "Klipper не готов"),
    (_klipper("k1", webhooks="shutdown"), "light_on", "Klipper не готов"),        # why, as for pause and /command
    (PrinterStatus(id="m1", label="M", kind=PrinterKind.MKS, online=True, state=PrinterState.PRINTING),
     "light_off", "управление не подключено"),
])
def test_skip_reason(status, action, why):
    assert _row(status.to_dict(), action) == why


@pytest.mark.parametrize("action,ids,detail", [
    ("stop", ["b1"], "Групповое действие: light_on, light_off, pause, resume или reset_errors"),
    ("cooldown", ["b1"], "Групповое действие: light_on, light_off, pause, resume или reset_errors"),
    ("pause", [], "Не выбран ни один принтер"),
    ("pause", None, "Не выбран ни один принтер"),
    ("pause", ["p%d" % i for i in range(65)], "За раз — не больше 64 принтеров"),
])
def test_run_bulk_validation(action, ids, detail):
    with pytest.raises(fleet_commands.BulkError) as e:
        fleet_commands.run_bulk(action, ids, store=_Store([]), send=lambda *a: pytest.fail("sent"))
    assert str(e.value) == detail


def test_results_in_request_order_deduplicated_with_labels():
    store = _Store([_bambu("b1"), _bambu("b2"), _bambu("b3", PrinterState.IDLE)])
    sent = []
    out = fleet_commands.run_bulk("pause", ["b3", "b1", "b2", "b1"], store=store, labels={"b1": "P1"},
                                  send=lambda pid, action, params: sent.append((pid, action, params)) or CONFIRMED)
    assert [r["id"] for r in out["results"]] == ["b3", "b1", "b2"]
    assert out["results"][0] == {"id": "b3", "label": "B3", "ok": False, "status": "skipped",
                                 "detail": "принтер не печатает"}
    assert out["results"][1] == {"id": "b1", "label": "P1", "ok": True, "status": "confirmed",
                                 "detail": "подтверждено принтером"}
    assert sorted(sent) == [("b1", "pause", {}), ("b2", "pause", {})]
    assert out["summary"] == {"confirmed": 2, "sent": 0, "rejected": 0, "skipped": 1, "dry_run": 0}
    assert (out["action"], out["dry_run"]) == ("pause", False) and len(out["batch"]) == 8


def test_all_sends_run_at_the_same_time():
    # one worker per printer: the Barrier only opens if all 8 sends run concurrently
    ids = ["b%d" % i for i in range(1, 9)]
    barrier = threading.Barrier(8, timeout=5)

    def send(pid, action, params):
        barrier.wait()
        return CONFIRMED

    out = fleet_commands.run_bulk("pause", ids, store=_Store([_bambu(i) for i in ids]), send=send)
    assert [r["status"] for r in out["results"]] == ["confirmed"] * 8
    assert [r["id"] for r in out["results"]] == ids


def test_send_still_running_at_the_deadline_holds_the_family_lock(monkeypatch):
    monkeypatch.setattr(fleet_commands, "BULK_DEADLINE", 0.2)
    release = threading.Event()

    def send(pid, action, params):
        release.wait(10)
        return CONFIRMED

    store = _Store([_bambu("b1")])
    out = fleet_commands.run_bulk("light_on", ["b1"], store=store, send=send)
    assert out["results"] == [{"id": "b1", "label": "B1", "ok": True, "status": "sent",
                               "detail": "ответа не дождались — итог покажет статус принтера"}]
    with pytest.raises(fleet_commands.BulkBusy) as e:          # the light family is still busy ...
        fleet_commands.run_bulk("light_off", ["b1"], store=store, send=send)
    assert str(e.value) == "Групповая команда уже выполняется — дождитесь окончания"
    release.set()
    lock = fleet_commands._busy["light"]
    assert lock.acquire(timeout=5)                             # ... until the send in flight is done
    lock.release()


def test_queued_sends_are_cancelled_at_the_deadline(monkeypatch):
    monkeypatch.setattr(fleet_commands, "BULK_MAX_WORKERS", 1)
    monkeypatch.setattr(fleet_commands, "BULK_DEADLINE", 0.5)
    release, calls = threading.Event(), []

    def send(pid, action, params):
        calls.append(pid)
        release.wait(10)
        return CONFIRMED

    out = fleet_commands.run_bulk("pause", ["b1", "b2", "b3"], store=_Store([_bambu(i) for i in ("b1", "b2", "b3")]),
                                  send=send)
    assert [(r["status"], r["detail"]) for r in out["results"]] == [
        ("sent", "ответа не дождались — итог покажет статус принтера"),
        ("skipped", "не отправлено — истёк срок ожидания"),
        ("skipped", "не отправлено — истёк срок ожидания")]
    release.set()
    lock = fleet_commands._busy["print"]
    assert lock.acquire(timeout=5)
    lock.release()
    assert calls == ["b1"]                                     # never sent later


def test_refusals_and_errors_of_send():
    replies = {
        "b1": {"success": False, "status": "refused", "detail": "Уже на паузе", "replied": False},
        "b2": {"success": False, "status": "rejected", "detail": "🔒 прошивка блокирует сторонние команды",
               "replied": True},
        "b3": {"success": True, "status": "sent", "detail": "отправлено (без подтверждения)", "replied": False},
    }

    def send(pid, action, params):
        if pid == "b4":
            raise RuntimeError("socket closed")
        return replies[pid]

    ids = ["b1", "b2", "b3", "b4"]
    out = fleet_commands.run_bulk("pause", ids, store=_Store([_bambu(i) for i in ids]), send=send)
    assert [(r["status"], r["ok"], r["detail"]) for r in out["results"]] == [
        ("skipped", False, "уже на паузе"),
        ("rejected", False, "🔒 прошивка блокирует сторонние команды"),
        ("sent", True, "отправлено (без подтверждения)"),
        ("rejected", False, "socket closed")]
    assert out["summary"] == {"confirmed": 0, "sent": 1, "rejected": 2, "skipped": 1, "dry_run": 0}


def test_unknown_printer_and_dry_run():
    store = _Store([_bambu("b1"), _bambu("b2", PrinterState.IDLE)])
    out = fleet_commands.run_bulk("pause", ["nope", "b1", "b2"], store=store, dry_run=True,
                                  send=lambda *a: pytest.fail("dry run must not send"))
    assert [(r["status"], r["ok"], r["detail"]) for r in out["results"]] == [
        ("skipped", False, "принтер не найден"), ("dry_run", True, "будет отправлено"),
        ("skipped", False, "принтер не печатает")]
    assert out["dry_run"] is True
    assert out["summary"] == {"confirmed": 0, "sent": 0, "rejected": 0, "skipped": 2, "dry_run": 1}


def test_one_lock_per_action_family():
    store = _Store([_bambu("b1")])
    light = fleet_commands._busy["light"]
    assert light.acquire(blocking=False)
    try:
        with pytest.raises(fleet_commands.BulkBusy):
            fleet_commands.run_bulk("light_on", ["b1"], store=store, send=lambda *a: CONFIRMED)
        # an urgent pause-all is never blocked by a light-all
        assert fleet_commands.run_bulk("pause", ["b1"], store=store, send=lambda *a: CONFIRMED)["summary"]["confirmed"] == 1
    finally:
        light.release()
    for family in ("light", "print"):
        assert fleet_commands._busy[family].acquire(blocking=False)
        fleet_commands._busy[family].release()

    class _Broken:
        def get_all(self):
            raise RuntimeError("store down")

    with pytest.raises(RuntimeError):
        fleet_commands.run_bulk("resume", ["b1"], store=_Broken(), send=lambda *a: CONFIRMED)
    assert fleet_commands._busy["print"].acquire(blocking=False)
    fleet_commands._busy["print"].release()


# --- reset_errors: «Сбросить ошибки» (notification_actions.reset_skip_reason / reset_one) --------

def _rec(key, code, severity, actions, src="print_error", blocking=False):
    return {"key": key, "src": src, "code": code, "severity": severity, "blocking": blocking,
            "actions": [{"key": a, "label": a, "style": "", "confirm": None, "yes": None, "bambu_id": None}
                        for a in actions]}


CANCEL = _rec("pe:0300_400C", "0300_400C", "common", ("clean",))
PAUSE_8011 = _rec("pe:0700_8011", "0700_8011", "pause", ("resume",))
BLOCKING = _rec("pe:0300_8007", "0300_8007", "pause", ("stop", "resume"), blocking=True)
HMS = _rec("hms:0300_1100_0002_0001", "0300_1100_0002_0001", "serious", (), src="hms")


def _with(status, *records):
    status.notifications = list(records)
    return status


@pytest.mark.parametrize("status,why", [
    (_with(_bambu("b1", PrinterState.CANCELLED), CANCEL, HMS), None),
    (_with(_bambu("b1", PrinterState.PAUSED), PAUSE_8011), "пауза-ошибка 0700_8011 — решается кнопками в уведомлении"),
    (_with(_bambu("b1", PrinterState.PAUSED), BLOCKING),
     "0300_8007 после отключения питания — выбор обязателен: продолжить или остановить"),
    (_with(_bambu("b1"), HMS), "только HMS — сбрасывать нечего, принтер снимет их сам"),
    (_bambu("b1", PrinterState.IDLE), "уведомлений нет"),
    (_with(_bambu("b-off", online=False), CANCEL), "офлайн"),
    (_with(_bambu("b-grace", grace=True), CANCEL), "нет связи — данные устарели"),
    (_with(_bambu("b-blocked", PrinterState.CANCELLED), CANCEL), "прошивка блокирует (нет Developer Mode)"),
    (_klipper("k1", webhooks="shutdown"), "Klipper: перезапуск прошивки — только по одному, с подтверждением"),
    (PrinterStatus(id="m1", label="M", kind=PrinterKind.MKS, online=True, state=PrinterState.PRINTING),
     "управление не подключено"),
])
def test_reset_errors_skip_reason(status, why):
    assert _row(status.to_dict(), "reset_errors") == why


def _reset_store():
    return _Store([_with(_bambu("b1", PrinterState.CANCELLED), CANCEL),
                   _with(_bambu("b2", PrinterState.PAUSED), PAUSE_8011),
                   _bambu("b3", PrinterState.IDLE)])


def test_reset_errors_dry_run_lists_who_and_why():
    out = fleet_commands.run_bulk("reset_errors", ["b1", "b2", "b3"], store=_reset_store(), dry_run=True,
                                  send=lambda *a: pytest.fail("dry run must not send"))
    assert [(r["id"], r["status"], r["detail"]) for r in out["results"]] == [
        ("b1", "dry_run", "будет отправлено"),
        ("b2", "skipped", "пауза-ошибка 0700_8011 — решается кнопками в уведомлении"),
        ("b3", "skipped", "уведомлений нет")]
    assert out["summary"]["dry_run"] == 1 and out["dry_run"] is True


def test_reset_errors_sends_through_reset_one(monkeypatch):
    from app.services import notification_actions
    sent = []
    monkeypatch.setattr(notification_actions, "reset_one",
                        lambda pid, action, params: sent.append((pid, action, params)) or CONFIRMED)
    out = fleet_commands.run_bulk("reset_errors", ["b1", "b2", "b3"], store=_reset_store())
    assert sent == [("b1", "reset_errors", {})]                     # the pause and the empty one: never
    assert [r["status"] for r in out["results"]] == ["confirmed", "skipped", "skipped"]
    # its refusal (the record went meanwhile) comes back as skipped, lower case
    monkeypatch.setattr(notification_actions, "reset_one", lambda pid, action, params: {
        "success": False, "status": "refused", "detail": "Сбрасывать нечего", "replied": False})
    out = fleet_commands.run_bulk("reset_errors", ["b1"], store=_reset_store())
    assert out["results"][0]["status"] == "skipped" and out["results"][0]["detail"] == "сбрасывать нечего"


def test_reset_errors_carries_the_confirmed_record_per_printer():
    sent = []

    def send(pid, action, params):
        sent.append((pid, action, params))
        return CONFIRMED

    fleet_commands.run_bulk("reset_errors", ["b1"], store=_reset_store(), send=send,
                            keys={"b1": "pe:0300_400C", "b9": "pe:x"})
    assert sent == [("b1", "reset_errors", {"key": "pe:0300_400C"})]
    # another action never takes them
    sent.clear()
    fleet_commands.run_bulk("pause", ["b1"], store=_Store([_bambu("b1")]), send=send, keys={"b1": "pe:x"})
    assert sent == [("b1", "pause", {})]


# --- maintenance mode (spec_maintenance B19 / B20): left out of every bulk action ------------

MAINT = {"since": 1000.0, "note": None, "by": "admin"}


@pytest.mark.parametrize("action", fleet_commands.BULK_ACTIONS)
@pytest.mark.parametrize("status", [
    _bambu("b-off", online=False),                       # one of the five long-offline printers
    _bambu("b1"), _bambu("b1", PrinterState.PAUSED), _bambu("b-grace", grace=True),
    _with(_bambu("b1", PrinterState.CANCELLED), CANCEL),
    PrinterStatus(id="m1", label="M", kind=PrinterKind.MKS, online=False, state=PrinterState.OFFLINE),
    _klipper("k1", webhooks="shutdown"),
])
def test_skip_reason_of_a_printer_in_maintenance_comes_first(status, action):
    d = status.to_dict()
    assert _row(dict(d, maintenance=MAINT), action) == "на обслуживании"
    assert _row(dict(d, maintenance=None), action) == _row(d, action)           # None: as before


@pytest.mark.parametrize("dry_run", [True, False])
def test_run_bulk_skips_the_printers_in_maintenance(dry_run):
    store = _Store([_bambu("b1"), _bambu("b2"), _bambu("b-off", online=False)])
    sent = []
    out = fleet_commands.run_bulk("pause", ["b1", "b2", "b-off"], store=store, dry_run=dry_run,
                                  maintenance={"b2": MAINT, "b-off": MAINT},
                                  send=lambda pid, action, params: sent.append(pid) or CONFIRMED)
    assert [(r["id"], r["status"], r["detail"]) for r in out["results"]] == [
        ("b1", "dry_run", "будет отправлено") if dry_run else ("b1", "confirmed", "подтверждено принтером"),
        ("b2", "skipped", "на обслуживании"), ("b-off", "skipped", "на обслуживании")]
    assert sent == ([] if dry_run else ["b1"])
    assert out["summary"]["skipped"] == 2


def test_run_bulk_reads_the_maintenance_map_itself(monkeypatch):
    from app.services import printer_registry

    monkeypatch.setattr(printer_registry, "maintenance_map", lambda *a, **k: {"b1": MAINT})
    out = fleet_commands.run_bulk("light_on", ["b1", "b2"], store=_Store([_bambu("b1"), _bambu("b2")]),
                                  dry_run=True, send=lambda *a: pytest.fail("dry run must not send"))
    assert [r["detail"] for r in out["results"]] == ["на обслуживании", "будет отправлено"]

    def broken(*a, **k):
        raise RuntimeError("registry down")

    monkeypatch.setattr(printer_registry, "maintenance_map", broken)       # never breaks the bulk command
    out = fleet_commands.run_bulk("light_on", ["b1"], store=_Store([_bambu("b1")]), dry_run=True,
                                  send=lambda *a: pytest.fail("dry run must not send"))
    assert out["results"][0]["status"] == "dry_run"
