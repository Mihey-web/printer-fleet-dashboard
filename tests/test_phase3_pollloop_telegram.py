"""Phase-3 regressions: poll-loop grace handling, HTML-safe status text, and
Telegram notify() delivery semantics.
"""
import threading

import main
from app.domain.models import PrinterStatus, PrinterKind, PrinterState
from telegram_bot_async import TelegramReporter


class _Store:
    def __init__(self, items):
        self._items = items

    def get_all(self):
        return self._items


def _status(**kw):
    base = dict(id="p1", label="P1", kind=PrinterKind.BAMBU, online=True,
                state=PrinterState.IDLE)
    base.update(kw)
    return PrinterStatus(**base)


# --- #151: format_status_text must HTML-escape dynamic content ----------------

def test_format_status_text_escapes_label_and_error():
    # A '<' in a label/error would break Telegram's HTML parse (400) and freeze
    # the status message. The dynamic parts must be escaped; the <b> markup stays.
    s = _status(label="Prusa <MK4> & Co", state=PrinterState.ERROR,
                last_error="fault <x> & <y>")
    text = main.format_status_text(_Store([s]))
    assert "&lt;MK4&gt;" in text
    assert "&amp;" in text
    assert "<MK4>" not in text        # raw angle brackets escaped
    assert "<b>" in text              # intentional markup preserved


def test_format_status_text_escapes_printing_job_name():
    s = _status(state=PrinterState.PRINTING, progress_pct=42.0,
                job_name="part<v2>.gcode")
    text = main.format_status_text(_Store([s]))
    assert "part&lt;v2&gt;.gcode" in text
    assert "part<v2>" not in text


def test_effective_prev_collapses_offline_to_last_active():
    # Core of the "notify after a transient offline" fix (#251).
    P, O, F, I = (PrinterState.PRINTING, PrinterState.OFFLINE,
                  PrinterState.FINISHED, PrinterState.IDLE)
    # PRINTING -> brief offline -> now: treated as if prev was PRINTING.
    assert main._effective_prev_state(O, P) == P
    # Never observed active (startup/reconnect of old job): stays OFFLINE -> quiet.
    assert main._effective_prev_state(O, None) == O
    # Last active was idle, not printing: an offline->finished won't false-notify.
    assert main._effective_prev_state(O, I) == I
    # Non-offline prev passes straight through.
    assert main._effective_prev_state(P, None) == P
    assert main._effective_prev_state(F, P) == F


def test_format_status_text_uses_fresh_display_label(monkeypatch):
    # The bot status must show the CURRENT registry label (via _display_label),
    # not the name baked into the collector at startup — otherwise a rename only
    # shows after a restart.
    s = _status(id="p9", label="OldName", state=PrinterState.PRINTING,
                progress_pct=10.0)
    monkeypatch.setattr(main, "_display_label",
                        lambda pid, fb: "NewName" if pid == "p9" else fb)
    text = main.format_status_text(_Store([s]))
    assert "NewName" in text
    assert "OldName" not in text


def test_error_notification_escapes_printer_error_text():
    # Telegram messages go out with parse_mode=HTML. Bambu's Russian HMS texts
    # contain '>' («Настройки > Калибровка»), which would 400 the whole send.
    # The template's own markup stays as is.
    msg = main._error_notification("❌ <b>{label}</b> — ошибка", "P1",
                                   "HMS_0C00_0300_0003_000B: нажмите «Настройки > Калибровка» [common]")
    assert msg == ("❌ <b>P1</b> — ошибка: "
                   "HMS_0C00_0300_0003_000B: нажмите «Настройки &gt; Калибровка» [common]")
    assert main._error_notification("{label} упал", "P1", None) == "P1 упал"
    # Labels are free-form (Admin → Printers): escaped too.
    assert (main._error_notification("<b>{label}</b> ошибка", "X1C <test> & Co", "a > b")
            == "<b>X1C &lt;test&gt; &amp; Co</b> ошибка: a &gt; b")


def test_poll_loop_notifications_escape_printer_label(monkeypatch):
    # Finish / pause / error notifications all go out with parse_mode=HTML; a
    # raw "<test>" in the label makes Telegram reject the message.
    import time
    from types import SimpleNamespace

    import pytest

    sent = []

    class _Tg:
        def notify(self, text):
            sent.append(text)
            return True

    class _Store:
        def __init__(self):
            self.items = {}

        def upsert(self, status):
            self.items[status.id] = status

        def get_one(self, pid):
            return self.items.get(pid)

        def record_snapshot(self, status):
            pass

    class _Collector:
        def __init__(self, state):
            self.state = state

        def fetch(self):
            return {"state": self.state}

    class _OneCycle(Exception):
        pass

    def _sleep(_seconds):
        raise _OneCycle

    monkeypatch.setattr(main, "time", SimpleNamespace(time=time.time, monotonic=time.monotonic, sleep=_sleep))
    monkeypatch.setattr(main, "_display_label", lambda pid, fallback: fallback)
    monkeypatch.setattr(main.settings_service, "get_all", lambda: {
        "telegram_notify_on_finish": True, "telegram_notify_on_paused": True,
        "telegram_notify_on_error": True, "telegram_finish_template": "✅ {label}",
        "telegram_paused_template": "⏸ <b>{label}</b>", "telegram_error_template": "🔴 {label}"})
    label = "X1C <test> & Co"
    collectors = [(pid, PrinterKind.MKS, label, _Collector(state), "mks")
                  for pid, state in (("p-fin", "finish"), ("p-pause", "paused"), ("p-err", "error"))]
    with pytest.raises(_OneCycle):
        main.poll_loop(_Store(), collectors, _Tg(),
                       prev_states={pid: PrinterState.PRINTING for pid, *_rest in collectors})
    assert sorted(sent) == sorted(["✅ X1C &lt;test&gt; &amp; Co",
                                   "⏸ <b>X1C &lt;test&gt; &amp; Co</b>",
                                   "🔴 X1C &lt;test&gt; &amp; Co"])


# --- #321: grace status must preserve ALL fields (ams/fans/light_on/fw) -------

def test_build_grace_status_preserves_all_fields():
    prev = _status(
        state=PrinterState.PRINTING, progress_pct=55.0,
        ams={"units": [{"id": 0}]}, fans={"part": 100},
        light_on=True, fw_update=True,
        last_successful_fetch=123.0,
    )
    grace = main._build_grace_status(prev, reason="timeout", now=999.0,
                                     device_type="X1C")
    # The four fields the old field-by-field copy dropped:
    assert grace.ams == {"units": [{"id": 0}]}
    assert grace.fans == {"part": 100}
    assert grace.light_on is True
    assert grace.fw_update is True
    # Overridden markers:
    assert grace.grace_period_active is True
    assert grace.last_update_ts == 999.0
    assert grace.last_error == "timeout"
    # Last-good state preserved:
    assert grace.state == PrinterState.PRINTING
    assert grace.last_successful_fetch == 123.0


# --- #91 / #260: notify() returns delivery-dispatch bool, never blocks --------

def test_notify_false_when_no_chat():
    r = TelegramReporter(token="x")
    r._available = True
    r._chat_id = None
    calls = []
    r._send_now = lambda t: calls.append(t)
    assert r.notify("hi") is False
    assert calls == []


def test_notify_false_when_unavailable():
    r = TelegramReporter(token="x")
    r._available = False
    r._chat_id = 123
    assert r.notify("hi") is False


def test_notify_dispatches_on_background_thread_when_chat_known():
    r = TelegramReporter(token="x")
    r._available = True
    r._chat_id = 123
    done = threading.Event()
    r._send_now = lambda t: done.set()
    # Returns immediately (True = dispatched); the actual send runs off-thread so
    # a hung network call can't stall the poll loop that called notify().
    assert r.notify("hi") is True
    assert done.wait(2) is True


def test_poll_loop_notes_the_command_signature_of_bambu_printers(monkeypatch):
    # print.fun tells whether the printer takes unsigned print commands; the poll
    # loop hands it to printer_commands after every Bambu normalize.
    import time
    from types import SimpleNamespace

    import pytest
    from app.services import printer_commands

    class _OneCycle(Exception):
        pass

    def _sleep(_seconds):
        raise _OneCycle

    class _Store:
        def upsert(self, status):
            pass

        def get_one(self, pid):
            return None

        def record_snapshot(self, status):
            pass

    class _Collector:
        def fetch(self):
            return object()

    noted = []
    monkeypatch.setattr(main, "time", SimpleNamespace(time=time.time, monotonic=time.monotonic, sleep=_sleep))
    monkeypatch.setattr(main, "normalize_bambu",
                        lambda pid, label, result, device_type=None: _status(id=pid, signature_required=False))
    monkeypatch.setattr(printer_commands, "note_signature", lambda pid, required: noted.append((pid, required)))
    with pytest.raises(_OneCycle):
        main.poll_loop(_Store(), [("b1", PrinterKind.BAMBU, "B1", _Collector(), "P2S")])
    assert noted == [("b1", False)]
