"""QA 17.11 follow-ups made while merging the six fix branches (findings that needed two parts' files)."""
import asyncio

import pytest

from app.services import telegram_bot_async as tba


@pytest.mark.parametrize("stored", [None, "5", 0, -3, True])
def test_live_summary_survives_a_broken_stored_interval(stored, monkeypatch):
    # F6: a null interval saved before the settings check made sleep(None) raise and end the task
    rep = tba.TelegramReporter("123:abc", update_interval=stored)
    slept = []

    async def fake_sleep(s):
        slept.append(s)
        rep._stop_event.set()

    monkeypatch.setattr(tba.asyncio, "sleep", fake_sleep)
    asyncio.run(rep._periodic_update(bot=None))
    assert slept == [2]


def test_live_summary_keeps_a_good_interval(monkeypatch):
    rep = tba.TelegramReporter("123:abc", update_interval=7)
    slept = []

    async def fake_sleep(s):
        slept.append(s)
        rep._stop_event.set()

    monkeypatch.setattr(tba.asyncio, "sleep", fake_sleep)
    asyncio.run(rep._periodic_update(bot=None))
    assert slept == [7]


def test_klipper_shutdown_says_the_reason_in_plain_words():
    # interface 14: «webhooks: shutdown … Timer too close» alone said nothing to a person
    from app.services import notifications as nf
    n = nf.build_klipper({"host_state": "shutdown", "host_message": "MCU 'mcu' shutdown: Timer too close"})[0]
    assert n["text"] == "Klipper остановлен (shutdown): MCU 'mcu' shutdown: Timer too close"   # the raw reason stays
    assert n["hint"].startswith("Плата не успела выполнить команду вовремя")
    lost = nf.build_klipper({"host_state": "shutdown", "host_message": "Lost communication with MCU 'mcu'"})[0]
    assert lost["hint"].startswith("Потеряна связь с платой")
    other = nf.build_klipper({"host_state": "shutdown", "host_message": "Something new"})[0]
    assert other["hint"] == nf.KLIPPER_SHUTDOWN_HINT
