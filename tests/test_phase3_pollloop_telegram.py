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
