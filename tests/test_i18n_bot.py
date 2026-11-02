"""The Telegram bot in English (app/services/i18n.py, docs/dev/I18N_PHASE2_REPORT.md).

* the translator: the panel's own dictionary (app/web/i18n/en.js), exact lookups, the
  known pieces of a glued text, a name or an unknown text left alone;
* durations 5ч30м / 5h30m and dates by the language;
* the printer's error: Bambu's texts as the English original of their code;
* the notification templates: the admin's text as written, a default in the bot's language;
* the setting telegram_lang: Russian by default, validated, the admin page's choice;
* the poll loop's notifications and the bot's own first message.
The summary's snapshot is in tests/test_i18n.py (with the coverage of main.py and the bot).
"""
import asyncio
import time
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import main
from app.api import i18n as i18n_api
from app.domain.models import PrinterKind, PrinterState, PrinterStatus
from app.services import i18n as bot_i18n
from app.services import settings_service
from app.vendor.pybambu.utils import get_HMS_error_text, get_print_error_text

HMS = "0500_0100_0003_0004"


# --- the translator --------------------------------------------------------------------------

def test_the_dictionary_is_the_panels_own():
    assert bot_i18n.languages() == ("ru", "en")
    assert bot_i18n.t("офлайн", "en") == "offline" == i18n_api.strings("en")["офлайн"]
    assert bot_i18n.t("  на обслуживании ", "en") == "  in maintenance "      # edge spaces kept
    assert bot_i18n.t("офлайн", "ru") == "офлайн"
    assert bot_i18n.t("Принтер у окна", "en") == "Принтер у окна"             # not in it: as it is
    assert bot_i18n.t(None, "en") is None and bot_i18n.t("", "en") == ""


def test_pieces_inside_a_glued_text_and_names_left_alone():
    assert bot_i18n.tr("Ошибка печати 0300_400C", "en") == "Print error 0300_400C"
    assert bot_i18n.tr("Klipper остановлен (shutdown): Lost communication with MCU", "en") == (
        "Klipper stopped (shutdown): Lost communication with MCU")
    # a name, or a text the dictionary does not know: more than 30 % would stay Russian
    for s in ("Принтер у окна", "Сопло засорилось на втором слое"):
        assert bot_i18n.tr(s, "en") == s
    assert bot_i18n.tr("plain English", "en") == "plain English"
    assert bot_i18n.tr("Ошибка печати 0300_400C", "ru") == "Ошибка печати 0300_400C"


def test_the_dictionary_is_reread_when_its_file_changes(monkeypatch, tmp_path):
    d = tmp_path / "i18n"
    d.mkdir()
    f = d / "en.js"
    f.write_text('I18N.register("en", {strings: /*strings:begin*/{"офлайн": "away"}/*strings:end*/});',
                 encoding="utf-8")
    monkeypatch.setattr(i18n_api, "DICT_DIR", str(d))
    monkeypatch.setattr(bot_i18n, "_dicts", {})
    assert bot_i18n.t("офлайн", "en") == "away"
    f.write_text('I18N.register("en", {strings: /*strings:begin*/{"офлайн": "gone"}/*strings:end*/});',
                 encoding="utf-8")
    st = f.stat()
    import os
    os.utime(f, (st.st_atime, st.st_mtime + 5))
    assert bot_i18n.t("офлайн", "en") == "gone"


def test_durations_and_dates_by_the_language():
    assert [bot_i18n.duration(s, "en") for s in (19800, 7200, 300, 59, 0, None, -5, "x")] == [
        "5h30m", "2h", "5m", "0m", None, None, None, None]
    assert [bot_i18n.duration(s, "ru") for s in (19800, 7200, 300)] == ["5ч30м", "2ч", "5м"]
    assert main._fmt_time(19800) == "5ч30м" and main._fmt_time(19800, "en") == "5h30m"
    ts = time.mktime((2026, 11, 1, 17, 3, 0, 0, 0, -1))
    assert bot_i18n.date(ts, "ru", now=ts) == "01.11 17:03"
    assert bot_i18n.date(ts, "en", now=ts) == "01/11 17:03"
    assert bot_i18n.date(ts, "en", with_time=False, now=ts + 400 * 86400) == "01/11/2026"
    assert bot_i18n.date(None, "en") == ""


# --- the printer's error -----------------------------------------------------------------------

def _bambu_error():
    ru = get_HMS_error_text(HMS, "X1C", "ru")
    status = PrinterStatus(id="b1", label="X1C", kind=PrinterKind.BAMBU, online=True, state=PrinterState.ERROR,
                           device_type="X1C", notifications=[{"src": "hms", "code": HMS, "text": ru}],
                           last_error="HMS_%s: %s [fatal] | Ошибка печати 0300_400C" % (HMS, ru))
    return status, ru, get_HMS_error_text(HMS, "X1C", "en")


def test_bambus_texts_are_the_english_original_of_their_code():
    status, ru, en = _bambu_error()
    assert bot_i18n.printer_error(status.last_error, status, "en") == (
        "HMS_%s: %s [fatal] | Print error 0300_400C" % (HMS, en))
    assert bot_i18n.printer_error(status.last_error, status, "ru") == status.last_error
    # without the records (a text from elsewhere): the server's own words only, Bambu's stays
    assert bot_i18n.printer_error("Ошибка печати 0300_400C", None, "en") == "Print error 0300_400C"
    assert bot_i18n.printer_error("Lost communication with MCU", status, "en") == "Lost communication with MCU"
    pe_ru, pe_en = get_print_error_text("0300400C", "X1C", "ru"), get_print_error_text("0300400C", "X1C", "en")
    pe = {"id": "b1", "device_type": "X1C",
          "notifications": [{"src": "print_error", "code": "0300_400C", "text": pe_ru}]}
    assert bot_i18n.printer_error("Ошибка печати 0300_400C: " + pe_ru, pe, "en") == "Print error 0300_400C: " + pe_en


def test_the_error_notification_in_english_is_still_escaped():
    status, _ru, en = _bambu_error()
    msg = main._error_notification("🔴 Error: {label}", "X1C <1>", status.last_error, status, "en")
    assert msg == "🔴 Error: X1C &lt;1&gt;: HMS_%s: %s [fatal] | Print error 0300_400C" % (HMS, en.replace("'", "&#x27;"))
    assert main._error_notification("{label} упал", "P1", None) == "P1 упал"


# --- the templates -----------------------------------------------------------------------------

def test_default_templates_follow_the_bots_language():
    assert {k: bot_i18n.default_template(k, "en") for k in bot_i18n.TEMPLATE_KEYS} == {
        "telegram_finish_template": "✅ Print finished: {label}",
        "telegram_error_template": "\U0001f534 Error: {label}",
        "telegram_paused_template": "⚠️ Paused: {label}"}
    for k in bot_i18n.TEMPLATE_KEYS:
        assert bot_i18n.default_template(k, "ru") == settings_service.DEFAULTS[k]
        ru, en = settings_service.DEFAULTS[k], bot_i18n.default_template(k, "en")
        # a default (never edited, the Russian one saved by the form, or the English one): the bot's language
        for stored in (None, "", ru, en):
            assert bot_i18n.template({"telegram_lang": "en", k: stored}, k) == en
            assert bot_i18n.template({"telegram_lang": "ru", k: stored}, k) == ru
            assert bot_i18n.template({k: stored}, k) == ru                 # no language: Russian
        # the admin's own text: as written, in either language
        for own in ("Готово: {label}", "Done, {label}!"):
            assert bot_i18n.template({"telegram_lang": "en", k: own}, k) == own
            assert bot_i18n.template({"telegram_lang": "ru", k: own}, k) == own


# --- the setting ---------------------------------------------------------------------------------

def test_telegram_lang_defaults_to_russian_and_round_trips(tmp_path):
    db = str(tmp_path / "s.db")
    settings_service.init_db(db)
    try:
        assert settings_service.get("telegram_lang", db) == "ru"
        settings_service.set_many({"telegram_lang": "en"}, db)
        assert settings_service.get("telegram_lang", db) == "en"
    finally:
        settings_service.reset_cache()
    assert bot_i18n.bot_lang({}) == "ru" and bot_i18n.bot_lang({"telegram_lang": "xx"}) == "ru"
    assert bot_i18n.bot_lang({"telegram_lang": "en"}) == "en"


def test_admin_validates_and_shows_the_bots_language():
    from app.api import admin
    admin._validate_settings({"telegram_lang": "en"})
    admin._validate_settings({"telegram_lang": "ru"})
    for bad in ("de", "EN", "", None, 1, ["en"]):
        with pytest.raises(HTTPException) as e:
            admin._validate_settings({"telegram_lang": bad})
        assert e.value.status_code == 400
    values = dict(settings_service.DEFAULTS, telegram_lang="en", telegram_error_template="Беда: {label}")
    out = admin._public_settings(values)
    # the form shows the default templates as the bot sends them; the admin's own text as written
    assert out["telegram_lang"] == "en"
    assert out["telegram_finish_template"] == "✅ Print finished: {label}"
    assert out["telegram_paused_template"] == "⚠️ Paused: {label}"
    assert out["telegram_error_template"] == "Беда: {label}"
    assert admin._public_settings(dict(settings_service.DEFAULTS)) == {
        k: v for k, v in settings_service.DEFAULTS.items() if k not in admin.ENGINE_KEYS}


def test_the_admin_page_offers_the_choice():
    from pathlib import Path
    web = Path(__file__).resolve().parents[1] / "app" / "web"
    html = (web / "index.html").read_text(encoding="utf-8")
    js = (web / "app.js").read_text(encoding="utf-8")
    tg = html[html.index('id="tgSection"'):html.index('id="proxySection"')]
    assert '<select id="tgLang">' in tg and 'value="ru"' in tg and 'value="en"' in tg
    assert "srvEl('tgLang').value = values.telegram_lang || 'ru';" in js
    assert "saveServerSettings({ telegram_lang: this.value }, 'tgSavedFlash', 'tgError')" in js


# --- the poll loop and the bot ---------------------------------------------------------------------

def test_notifications_in_the_bots_language(monkeypatch):
    sent = []

    class _Tg:
        def notify(self, text):
            sent.append(text)
            return True

    class _Store:
        def upsert(self, status):
            pass

        def get_one(self, pid):
            return None

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
    monkeypatch.setattr(main, "_in_maintenance", lambda pid: False)
    cfg = dict(settings_service.DEFAULTS, telegram_lang="en", telegram_notify_on_finish=True,
               telegram_notify_on_paused=True, telegram_notify_on_error=True,
               telegram_paused_template="Пауза у {label}")                     # the admin's own: as written
    monkeypatch.setattr(main.settings_service, "get_all", lambda: cfg)
    collectors = [(pid, PrinterKind.MKS, "MKS " + pid, _Collector(state), "mks")
                  for pid, state in (("fin", "finish"), ("pause", "paused"), ("err", "error"))]
    with pytest.raises(_OneCycle):
        main.poll_loop(_Store(), collectors, _Tg(), prev_states={pid: PrinterState.PRINTING for pid, *_r in collectors})
    assert "✅ Print finished: MKS fin" in sent
    assert "Пауза у MKS pause" in sent
    err = [m for m in sent if "MKS err" in m]
    assert len(err) == 1 and err[0].startswith("\U0001f534 Error: MKS err")


def test_the_bots_first_message_without_a_summary(monkeypatch):
    from app.services import telegram_bot_async as tba

    class _Bot:
        def __init__(self):
            self.sent = []

        async def send_message(self, chat_id, text, parse_mode=None):
            self.sent.append(text)
            return SimpleNamespace(message_id=1)

    async def _quiet(self, bot):
        return None

    monkeypatch.setattr(tba.TelegramReporter, "_periodic_update", _quiet)
    for lang, want in (("ru", "(нет данных)"), ("en", "(no data)")):
        monkeypatch.setattr(settings_service, "peek", lambda key, _l=lang: _l if key == "telegram_lang" else None)
        rep = tba.TelegramReporter("t", allowed_chat_id=1)
        rep._available = True
        rep.set_status_getter(lambda: "")
        bot = _Bot()
        assert asyncio.run(rep._establish_chat(bot, 1)) is True
        assert bot.sent == [want] and rep._last_text == want
