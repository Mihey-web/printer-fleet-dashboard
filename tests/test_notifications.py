"""Printer notifications (app/services/notifications.py, notifications spec step 4).

Bambu's runtime tables are replaced by tiny synthetic ones (not Bambu's data);
lib texts come from the vendored ha-bambulab tables. The replay tests run the
real fixtures through the vendored lib and RawReport, like the golden tests.
"""
import dataclasses
import logging
from types import SimpleNamespace

import pytest

import bambu_testkit as kit
from app.domain.models import PrinterKind, PrinterState, PrinterStatus
from app.services import hms_catalog, normalizer, notification_actions, notifications as nf, settings_service
from app.services.normalizer import normalize_bambu
from app.vendor.pybambu.utils import get_print_error_text

P = "Q7X"   # synthetic serial prefix


@pytest.fixture(autouse=True)
def catalog(monkeypatch):
    """No runtime tables unless a test loads its own; the settings cache stays untouched."""
    monkeypatch.setattr(hms_catalog, "_index", {})
    monkeypatch.setattr(hms_catalog, "_prefix_by_pid", {})
    monkeypatch.setattr(settings_service, "_cache", None)   # auto-ignore codes = the defaults

    def load(act=None, err=None, hms=None):
        monkeypatch.setattr(hms_catalog, "_index", {P: {"act": act or {}, "err": err or {}, "hms": hms or {},
                                                        "ver": ("1", "2")}})
    return load


def _hms_obj(*entries, raw_list=None):
    """The lib's HMSList shape: errors {i-Code, i-Error, i-Wiki, i-Severity} (+ its raw list)."""
    errors = {"Count": len(entries)}
    for i, (code, text, *wiki) in enumerate(entries, 1):
        errors.update({f"{i}-Code": "HMS_" + code, f"{i}-Error": text, f"{i}-Wiki": wiki[0] if wiki else "",
                       f"{i}-Severity": "?"})
    obj = SimpleNamespace(errors=errors, error_count=len(entries))
    if raw_list is not None:
        obj._hms_list = raw_list
    return obj


def _keys(items):
    return [n["key"] for n in items]


def _lib_status(make_collector, model, block):
    """One report through the vendored lib and RawReport (as the parity test feeds them)."""
    collector = make_collector("probe", "192.0.2.1", "00000000", "PROBE0000000000", model)
    device = collector.client.get_device()
    device.print_update(data=dict({"msg": 0}, **block))
    device.info.set_online(True)
    return normalize_bambu("probe-1", "probe", device, device_type=model)


# ---- HMS ------------------------------------------------------------------

@pytest.mark.parametrize("level,severity", [(1, "fatal"), (2, "serious"), (3, "common"), (4, "info")])
def test_hms_level_is_the_high_half_of_code(level, severity):
    code = "0C00_0100_%04X_0004" % level
    [n] = nf.build_bambu(_hms_obj((code, "текст")), {}, 0, "X1C")
    assert (n["key"], n["src"], n["code"], n["severity"], n["text"]) == ("hms:" + code, "hms", code, severity, "текст")
    assert n["unknown"] is False and n["blocking"] is False
    assert n["routine"] is (severity in ("common", "info"))


def test_hms_other_levels_and_empty_codes_are_not_shown():
    raw = {"hms": [{"attr": 0x0C000100, "code": 0x00050001}, {"attr": 0x0C000100, "code": 0x00000001},
                   {"attr": 0, "code": 0x00010001}, {"attr": 0x0C000100, "code": 0}, {"attr": "x", "code": 1}]}
    assert nf.build_bambu(_hms_obj(), raw, 0, "X1C") == []
    assert nf.build_bambu(_hms_obj(("0001_0000_0000_0000", "уровень 0")), {}, 0, "X1C") == []


def test_silent_hms_shows_only_fatal_and_serious_as_unknown():
    raw = {"hms": [{"attr": 0x05000600, "code": 0x00020070}, {"attr": 0x05000200, "code": 0x00030009},
                   {"attr": 0x05000200, "code": 0x00010009}]}
    items = nf.build_bambu(_hms_obj(), raw, 0, "P2S")
    assert [(n["code"], n["severity"], n["text"], n["unknown"]) for n in items] == [
        ("0500_0200_0001_0009", "fatal", None, True), ("0500_0600_0002_0070", "serious", None, True)]
    assert all(n["hint"] == nf.UNKNOWN_HINT for n in items)
    # The lib's "unknown" (no such code in its tables) is no text either.
    [n] = nf.build_bambu(_hms_obj(("0FFF_0000_0002_0001", "unknown")), {}, 0, "X1C")
    assert n["unknown"] is True and n["text"] is None


@pytest.mark.parametrize("scenario,model,want", [
    # The lib drops codes with an empty text; the serious one comes back as unknown.
    ([{"attr": 0x05000600, "code": 0x00020070}], "P2S", [("0500_0600_0002_0070", "serious", True)]),
    # The real P2S order: that code, then a common one with text.
    ([{"attr": 0x05000600, "code": 0x00020070}, {"attr": 0x05000100, "code": 0x00030007}], "P2S",
     [("0500_0600_0002_0070", "serious", True), ("0500_0100_0003_0007", "common", False)]),
    # A common code with an empty text stays hidden (Studio hides an empty intro).
    ([{"attr": 0x05000200, "code": 0x00030009}], "X1C", []),
])
def test_silent_codes_through_the_vendored_lib(make_collector, scenario, model, want):
    status = _lib_status(make_collector, model, {"gcode_state": "IDLE", "hms": scenario})
    assert [(n["code"], n["severity"], n["unknown"]) for n in status.notifications] == want
    assert all(isinstance(n["since"], float) for n in status.notifications)   # RawReport's first-seen time


def test_one_code_reported_twice_is_one_record():
    # Live on P1 / P3 / P4 (18.10): the same HMS twice in hms[] (and in last_error).
    entry = {"attr": 0x05000600, "code": 0x00020070}
    obj = _hms_obj(raw_list=[entry, dict(entry)])
    assert _keys(nf.build_bambu(obj, {}, 0, "P2S")) == ["hms:0500_0600_0002_0070"]
    raw = {"hms": [dict(entry, first_seen=10.0), dict(entry, first_seen=10.0)]}
    assert _keys(nf.build_bambu(_hms_obj(), raw, 0, "P2S")) == ["hms:0500_0600_0002_0070"]


def test_raw_order_first_then_what_only_the_lib_lists():
    raw = {"hms": [{"attr": 0x0C000300, "code": 0x00030007, "first_seen": 5.0}]}
    obj = _hms_obj(("0C00_0300_0003_0007", "дефект"), ("0300_1100_0002_0001", "резонанс"))
    items = nf.build_bambu(obj, raw, 0, "X1C")
    assert [(n["code"], n["since"]) for n in items] == [("0300_1100_0002_0001", None), ("0C00_0300_0003_0007", 5.0)]
    assert items[1]["text"] == "дефект"     # the lib's text for the raw code


def test_every_code_of_last_error_has_a_record_in_the_fixture_replays(offline_collector):
    sev = {1: "fatal", 2: "serious", 3: "common", 4: "info"}
    for model in ("X1C", "P2S", "H2S", "P1S"):
        collector = offline_collector(model)
        for line in kit.load_fixture(model):
            kit.deliver(collector, line["msg"])
            status = normalize_bambu("fx", model, collector.fetch(), device_type=model)
            in_line = [part.split(":")[0][len("HMS_"):] for part in (status.last_error or "").split(" | ")
                       if part.startswith("HMS_")]
            records = {n["code"]: n for n in status.notifications}
            assert set(in_line) <= set(records), (model, in_line)
            for code in in_line:
                assert records[code]["severity"] == sev[int(code.split("_")[2], 16)]
                assert isinstance(records[code]["since"], float)
            assert len(records) == len(status.notifications)


# ---- print_error -------------------------------------------------------------

@pytest.mark.parametrize("value,severity", [(0x03004000, "fatal"), (0x07008011, "pause"), (0x0500C010, "common"),
                                            (0x0500A001, "common")])
def test_print_error_level_from_the_low_half(value, severity):
    [n] = nf.build_bambu(_hms_obj(), {}, value, "P2S")
    code = "%04X_%04X" % (value >> 16, value & 0xFFFF)
    assert (n["key"], n["src"], n["code"], n["severity"]) == ("pe:" + code, "print_error", code, severity)
    assert n["routine"] is False
    assert n["wiki"] == "https://e.bambulab.com/index.php?e=%08X&s=device_error&lang=ru" % value
    assert "d=" not in n["wiki"]


@pytest.mark.parametrize("value", [0, None, -5, 0x03000001, 0x05003FFF])
def test_print_error_status_values_are_dropped(value):
    assert nf.build_bambu(_hms_obj(), {}, value, "P2S") == []


@pytest.mark.parametrize("value", [0x0300400C, 0x0500400E])
def test_cancel_echo_is_common_with_close_dialog(value):
    [n] = nf.build_bambu(_hms_obj(), {}, value, "P2S")
    assert n["severity"] == "common" and n["hint"] == nf.CANCEL_HINT and n["wiki"] is None
    assert [(a["key"], a["label"], a["style"]) for a in n["actions"]] == [("clean", "Закрыть диалог", "primary")]
    assert n["blocking"] is False


def test_print_error_text_from_the_lib_without_a_table():
    [n] = nf.build_bambu(_hms_obj(), {}, 0x03004000, "P2S")
    assert n["text"] == get_print_error_text("03004000", "P2S", "ru")
    assert n["unknown"] is False


def test_print_error_without_any_text_is_unknown():
    [n] = nf.build_bambu(_hms_obj(), {}, 0x0FFF8FFF, "P2S")
    assert n["text"] is None and n["unknown"] is True and n["severity"] == "pause"


def test_print_error_first_seen_only_for_the_same_code():
    raw = {"print_error": 0x07008011, "print_error_since": 123.5}
    assert nf.build_bambu(_hms_obj(), raw, 0x07008011, "H2S")[0]["since"] == 123.5
    assert nf.build_bambu(_hms_obj(), raw, 0x07008005, "H2S")[0]["since"] is None


# ---- runtime tables: texts and buttons -------------------------------------

def test_the_runtime_table_text_comes_first(catalog):
    catalog(err={"03004000": "Тест: ошибка"}, hms={"0C00010000010004": "Тест: лидар"})
    items = nf.build_bambu(_hms_obj(("0C00_0100_0001_0004", "текст библиотеки")), {}, 0x03004000, "X1C", P)
    assert [n["text"] for n in items] == ["Тест: ошибка", "Тест: лидар"]


def test_buttons_from_the_table_in_its_order(catalog):
    catalog(act={"05008062": {"dev": [28, 27, 6]}})
    [n] = nf.build_bambu(_hms_obj(), {}, 0x05008062, "P2S", P)
    assert [(a["key"], a["label"], a["style"], a["bambu_id"]) for a in n["actions"]] == [
        ("resume", "Устранено — продолжить", "primary", 28), ("ignore", "Игнорировать и продолжить", "", 27)]
    assert n["hint"] is None and n["blocking"] is False


def test_a_required_choice_is_blocking_and_stop_needs_confirmation(catalog):
    catalog(act={"03008007": {"dev": [5, 2, 39]}})
    [n] = nf.build_bambu(_hms_obj(), {}, 0x03008007, "P1S", P)
    stop, resume = n["actions"]
    assert (stop["key"], stop["style"], stop["confirm"], stop["yes"]) == (
        "stop", "danger", "Остановить печать? Задание будет потеряно.", "Стоп")
    assert (resume["key"], resume["style"], resume["confirm"]) == ("resume", "primary", None)
    assert n["blocking"] is True
    assert n["hint"] == nf.HINTS["0300_8007"]


def test_hms_buttons_skip_print_error_commands(catalog):
    catalog(act={"0300E50000020001": {"def": [4]}, "0500040000030057": {"def": [54]}})
    obj = _hms_obj(("0300_E500_0002_0001", "с кнопкой 4"), ("0500_0400_0003_0057", "очистка воздуха"))
    a, b = nf.build_bambu(obj, {}, 0, "P2S", P)
    assert a["actions"] == [] and a["hint"] == nf.NO_COMMAND_HINT
    assert [x["key"] for x in b["actions"]] == ["purifier_off"]


def test_without_a_table_auto_ignore_codes_get_ignore_the_rest_nothing():
    # Defaults of auto_ignore_print_errors (the settings cache is not loaded here).
    [n] = nf.build_bambu(_hms_obj(), {}, 0x05008062, "P2S", P)
    assert [(a["key"], a["bambu_id"]) for a in n["actions"]] == [("ignore", 27)] and n["hint"] is None
    [n] = nf.build_bambu(_hms_obj(), {}, 0x07008011, "H2S", P)
    assert n["actions"] == [] and n["hint"] == nf.HINTS["0700_8011"]
    [n] = nf.build_bambu(_hms_obj(), {}, 0x05008099, "H2S", P)
    assert n["actions"] == [] and n["hint"] == nf.NO_TABLE_HINT


def test_a_loaded_table_without_the_code_gives_no_fallback(catalog):
    catalog(act={})
    [n] = nf.build_bambu(_hms_obj(), {}, 0x05008062, "P2S", P)
    assert n["actions"] == [] and n["hint"] is None


# ---- hints and wiki -----------------------------------------------------------

def test_hints_by_code_and_by_prefix():
    obj = _hms_obj(("0501_0400_0003_0002", "смазка"), ("0300_2E00_0003_0001", "калибровка"),
                   ("0C00_0100_0001_0004", "лидар"), ("0300_1100_0002_0001", "резонанс"))
    hints = {n["code"]: n["hint"] for n in nf.build_bambu(obj, {}, 0, "H2S")}
    assert hints == {"0501_0400_0003_0002": nf.LUBE_HINT, "0300_2E00_0003_0001": nf.HINTS["0300_2E00_0003_0001"],
                     "0C00_0100_0001_0004": nf.NO_COMMAND_HINT, "0300_1100_0002_0001": nf.NO_COMMAND_HINT}
    for code in ("0700_8005", "0700_8004", "0700_8011", "07FF_8011"):
        assert nf.hint_for(code)
    assert nf.hint_for("0700_8099") is None


@pytest.mark.parametrize("model,path", [("P2S", "p2s"), ("H2S", "h2d"), ("H2D", "h2d"), ("X1C", "x1"), ("X1E", "x1"),
                                        ("P1S", "p1"), ("P1P", "p1"), ("A1", "a1"), ("A1MINI", "a1-mini"),
                                        ("A1 mini", "a1-mini"), (None, "x1"), ("H2C", "x1")])
def test_hms_wiki_by_model(model, path):
    code = "0500_0600_0002_0070"
    want = "https://wiki.bambulab.com/en/%s/troubleshooting/hmscode/%s" % (path, code)
    assert nf.hms_wiki(code, model) == want
    assert nf.hms_wiki(code, model, "https://wiki.bambulab.com/en/hms/home") == want


def test_hms_wiki_keeps_the_libs_own_page():
    own = "https://wiki.bambulab.com/en/h2/troubleshooting/hmscode/0700_2000_0002_0025"
    [n] = nf.build_bambu(_hms_obj(("0700_2100_0002_0025", "сопротивление", own)), {}, 0, "H2S")
    assert n["wiki"] == own


# ---- sorting and the cap -----------------------------------------------------

def test_worst_first_and_the_printers_order_within_a_level():
    obj = _hms_obj(("0C00_0300_0003_0007", "c1"), ("0300_1100_0002_0001", "s1"), ("0500_0100_0004_0001", "i1"),
                   ("0C00_0100_0001_0004", "f1"), ("0500_0100_0003_0004", "c2"))
    items = nf.build_bambu(obj, {}, 0x07008011, "H2S")
    assert [(n["severity"], n["code"]) for n in items] == [
        ("fatal", "0C00_0100_0001_0004"), ("pause", "0700_8011"), ("serious", "0300_1100_0002_0001"),
        ("common", "0C00_0300_0003_0007"), ("info", "0500_0100_0004_0001"), ("common", "0500_0100_0003_0004")]


def test_at_most_twenty_records_the_least_severe_go():
    items = [{"key": str(i), "severity": "common"} for i in range(25)] + [{"key": "f", "severity": "fatal"}]
    out = nf.sort_and_cap(items)
    assert len(out) == nf.MAX_ITEMS == 20
    assert out[0]["key"] == "f" and [n["key"] for n in out[1:]] == [str(i) for i in range(19)]


# ---- top_reason ---------------------------------------------------------------

def _status(state, notifs=(), stage=None):
    return PrinterStatus(id="p", label="P", kind=PrinterKind.BAMBU, online=True, state=state, stage=stage,
                         notifications=list(notifs))


def test_top_reason():
    pause = {"src": "print_error", "code": "0500_8062", "severity": "pause", "text": "свет"}
    serious = {"src": "hms", "code": "0500_0600_0002_0070", "severity": "serious", "text": None}
    fatal = {"src": "hms", "code": "0C00_0100_0001_0004", "severity": "fatal", "text": "лидар"}
    echo = {"src": "print_error", "code": "0300_400C", "severity": "common", "text": "Отменено"}
    assert nf.top_reason(_status(PrinterState.PAUSED, [serious, pause])) == "0500_8062: свет"
    assert nf.top_reason(_status(PrinterState.ERROR, [pause, fatal])) == "0C00_0100_0001_0004: лидар"
    assert nf.top_reason(_status(PrinterState.ERROR, [serious])) is None
    assert nf.top_reason(_status(PrinterState.CANCELLED, [echo])) == "0300_400C: Отменено"
    assert nf.top_reason(_status(PrinterState.PAUSED, [serious], stage="paused_user")) == "stage:paused_user"
    assert nf.top_reason(_status(PrinterState.ERROR, [], stage="paused_user")) is None
    assert nf.top_reason(_status(PrinterState.PRINTING, [pause])) is None
    no_text = dict(pause, text=None)
    assert nf.top_reason(_status(PrinterState.PAUSED, [no_text])) == "0500_8062"
    long = dict(pause, text="я" * 400)
    assert len(nf.top_reason(_status(PrinterState.PAUSED, [long]))) == nf.REASON_MAX
    assert nf.top_reason({"state": "paused", "notifications": [pause]}) == "0500_8062: свет"


# ---- normalizer wiring --------------------------------------------------------

def test_normalize_bambu_carries_the_records_and_keeps_last_error():
    hms = _hms_obj(("0C00_0100_0001_0004", "Тестовый лидар"))
    result = SimpleNamespace(print_job=SimpleNamespace(gcode_state="PAUSE", print_error=0x07008011), hms=hms)
    s = normalize_bambu("bambu-1", "P1", result, device_type="H2S")
    assert s.last_error.split(" | ") == [
        "HMS_0C00_0100_0001_0004: Тестовый лидар [?]",
        "Ошибка печати 0700_8011: " + get_print_error_text("07008011", "H2S", "ru")]
    assert _keys(s.notifications) == ["hms:0C00_0100_0001_0004", "pe:0700_8011"]
    assert s.to_dict()["notifications"] == s.notifications
    # Grace keeps the last good records (dataclasses.replace carries every field).
    assert dataclasses.replace(s, last_error="grace", grace_period_active=True).notifications == s.notifications


def test_offline_and_healthy_printers_have_no_records():
    assert normalize_bambu("b", "B", None).notifications == []
    healthy = SimpleNamespace(print_job=SimpleNamespace(gcode_state="RUNNING", print_error=0),
                              hms=SimpleNamespace(errors={"Count": 0}, error_count=0))
    assert normalize_bambu("b", "B", healthy).notifications == []


def test_the_model_prefix_of_the_printer_picks_its_table(catalog, monkeypatch):
    catalog(err={"07008011": "Тест: из таблицы"})
    monkeypatch.setattr(hms_catalog, "_prefix_by_pid", {"bambu-1": P})
    result = SimpleNamespace(print_job=SimpleNamespace(gcode_state="PAUSE", print_error=0x07008011))
    assert normalize_bambu("bambu-1", "P1", result, device_type="H2S").notifications[0]["text"] == "Тест: из таблицы"
    assert normalize_bambu("bambu-2", "P2", result, device_type="H2S").notifications[0]["text"] != "Тест: из таблицы"


def test_a_failing_build_costs_the_records_not_the_status(monkeypatch, caplog):
    def boom(*a, **k):
        raise RuntimeError("synthetic")
    monkeypatch.setattr(nf, "build_bambu", boom)
    monkeypatch.setattr(normalizer, "_notif_failed", set())
    result = SimpleNamespace(print_job=SimpleNamespace(gcode_state="PAUSE", print_error=0x07008011))
    with caplog.at_level(logging.ERROR, logger="app.services.normalizer"):
        s1 = normalize_bambu("bambu-1", "P1", result)
        s2 = normalize_bambu("bambu-1", "P1", result)
    assert s1.notifications == s2.notifications == [] and s1.last_error.startswith("Ошибка печати 0700_8011")
    assert len([r for r in caplog.records if "notifications not built" in r.getMessage()]) == 1


def test_debug_shows_raw_reports_view_while_printing(make_collector):
    s = _lib_status(make_collector, "P2S", {"gcode_state": "RUNNING", "print_error": 0, "mc_percent": 5,
                                            "hms": [{"attr": 0x05000600, "code": 0x00020070, "ts_unix": "1"}]})
    assert s.state == PrinterState.PRINTING
    assert s.debug["print_error_raw"] == 0
    assert [(e["attr"], e["code"]) for e in s.debug["hms_raw"]] == [(0x05000600, 0x00020070)]


# ---- settings peek --------------------------------------------------------------

def test_settings_peek_never_opens_the_db(monkeypatch):
    monkeypatch.setattr(settings_service, "_cache", None)
    monkeypatch.setattr(settings_service, "_connect", lambda *a, **k: pytest.fail("DB opened"))
    assert settings_service.peek("auto_ignore_print_errors") == settings_service.DEFAULTS["auto_ignore_print_errors"]
    monkeypatch.setattr(settings_service, "_cache", {"auto_ignore_print_errors": ["0700_8011"]})
    assert settings_service.peek("auto_ignore_print_errors") == ["0700_8011"]
    assert notification_actions.actions_for("print_error", "0700_8011", None)[0][0]["key"] == "ignore"
    with pytest.raises(KeyError):
        settings_service.peek("no_such_key")


# ---- Klipper and Creality (spec step 6) ---------------------------------------

_SHUTDOWN_MSG = ("Lost communication with MCU 'mcu'\nOnce the underlying issue is corrected, use the\n"
                 "\"FIRMWARE_RESTART\" command to reset the firmware, reload the\nconfig, and restart the host software.\n"
                 "Printer is shutdown\n")


def _brief(items):
    return [(n["key"], n["src"], n["code"], n["severity"], n["text"]) for n in items]


def test_klipper_shutdown_is_fatal_with_the_first_line_and_a_firmware_restart():
    items = nf.build_klipper({"host_state": "shutdown", "host_message": _SHUTDOWN_MSG, "state": "printing"})
    assert _brief(items) == [("klipper:shutdown", "klipper", "webhooks: shutdown", "fatal",
                              "Klipper остановлен (shutdown): Lost communication with MCU 'mcu'")]
    n = items[0]
    assert n["hint"] == nf.KLIPPER_SHUTDOWN_HINT and n["wiki"] is None and n["unknown"] is False
    assert [(a["key"], a["style"], a["confirm"] is not None) for a in n["actions"]] == [("fw_restart", "danger", True)]
    assert (n["blocking"], n["routine"], n["since"]) == (False, False, None)
    # no message yet (notify_klippy_shutdown comes before printer.info answers)
    assert nf.build_klipper({"host_state": "Shutdown"})[0]["text"] == "Klipper остановлен (shutdown)"


def test_klipper_error_disconnected_and_startup():
    err = nf.build_klipper({"host_state": "error", "host_message": "Option 'x' in section 'y' must be specified"})
    assert _brief(err) == [("klipper:error", "klipper", "webhooks: error", "fatal",
                            "Ошибка Klipper: Option 'x' in section 'y' must be specified")]
    assert [(a["key"], a["style"]) for a in err[0]["actions"]] == [("klippy_restart", "primary"),
                                                                   ("fw_restart", "danger")]
    gone = nf.build_klipper({"host_state": "disconnected"})
    assert _brief(gone) == [("klipper:disconnected", "klipper", "klippy: disconnected", "fatal",
                             "Klippy отключился от Moonraker")]
    assert gone[0]["actions"] == [] and gone[0]["hint"] == nf.KLIPPER_DISCONNECTED_HINT
    start = nf.build_klipper({"host_state": "startup", "host_message": "Printer is not ready"})
    assert _brief(start) == [("klipper:startup", "klipper", "webhooks: startup", "info", "Klipper запускается")]
    assert start[0]["routine"] is False                       # only lesser HMS fold away
    assert nf.build_klipper({"host_state": "ready", "host_message": None, "state": "printing"}) == []
    assert nf.build_klipper({}) == [] and nf.build_klipper(None) == []


def test_klipper_print_error_and_its_echo_of_the_host_message():
    items = nf.build_klipper({"host_state": "ready", "state": "error",
                              "print_message": "Move out of range: 250.000 0.000 0.300 [0.000]"})
    assert _brief(items) == [("klipper:print_error", "klipper", "print_stats: error", "fatal",
                              "Move out of range: 250.000 0.000 0.300 [0.000]")]
    assert items[0]["actions"] == []
    assert nf.build_klipper({"state": "error", "print_message": ""})[0]["text"] == "Ошибка печати Klipper"
    # klippy down: the print error that only repeats the host's reason is left out, another one stays
    down = {"host_state": "shutdown", "host_message": _SHUTDOWN_MSG, "state": "error"}
    assert _keys(nf.build_klipper(dict(down, print_message="Lost communication with MCU 'mcu'"))) == [
        "klipper:shutdown"]
    assert _keys(nf.build_klipper(down)) == ["klipper:shutdown"]
    assert _keys(nf.build_klipper(dict(down, print_message="Heater extruder not heating at expected rate"))) == [
        "klipper:shutdown", "klipper:print_error"]


def test_klipper_host_state_falls_back_to_the_webhooks_debug_field():
    assert nf.klipper_host_state({"debug": {"webhooks_state": "Shutdown"}}) == "shutdown"
    assert nf.klipper_host_state({"host_state": "ready", "debug": {"webhooks_state": "shutdown"}}) == "ready"
    assert nf.klipper_host_state({"debug": {}}) is None and nf.klipper_host_state({}) is None
    assert _keys(nf.build_klipper({"debug": {"webhooks_state": "shutdown"}})) == ["klipper:shutdown"]


@pytest.mark.parametrize("state,severity", [(PrinterState.PRINTING, "serious"), (PrinterState.PAUSED, "pause"),
                                            (PrinterState.IDLE, "fatal"), (PrinterState.FINISHED, "fatal"),
                                            (PrinterState.UNKNOWN, "fatal"), ("error", "fatal")])
def test_creality_error_level_follows_the_print(state, severity):
    items = nf.build_creality({"err_code": 2, "err_key": 5}, state)
    assert _brief(items) == [("creality:2/5", "creality", "errcode 2 · key 5", severity, "Ошибка Creality 2/5")]
    assert (items[0]["hint"], items[0]["actions"], items[0]["wiki"]) == (nf.CREALITY_HINT, [], None)


def test_creality_without_an_error_or_with_an_odd_key():
    for data in ({"err_code": 0, "err_key": 0}, {}, {"err_code": None}, {"err_code": "2"}, {"err_code": True}):
        assert nf.build_creality(data, PrinterState.IDLE) == []
    assert nf.build_creality(None, PrinterState.IDLE) == []
    odd = nf.build_creality({"err_code": 2, "err_key": "a b;<c>"}, PrinterState.IDLE)[0]
    assert odd["key"] == "creality:2/abc" and odd["code"] == "errcode 2 · key a b;<c>"
    bare = nf.build_creality({"err_code": 7}, PrinterState.IDLE)[0]
    assert (bare["key"], bare["code"], bare["text"]) == ("creality:7", "errcode 7", "Ошибка Creality 7")


def test_error_line_joins_the_errors_only():
    items = [{"severity": "fatal", "text": "A"}, {"severity": "info", "text": "B"},
             {"severity": "pause", "text": None, "code": "C"}, {"severity": "serious", "text": "D"}]
    assert nf.error_line(items) == "A | C | D"
    assert nf.error_line([{"severity": "info", "text": "B"}]) is None and nf.error_line([]) is None
