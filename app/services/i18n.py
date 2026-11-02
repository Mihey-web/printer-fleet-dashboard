"""The server's own texts in another language: the Telegram bot, the CSV exports
(docs/dev/I18N_PHASE2_REPORT.md).

One dictionary for the panel and the server: app/web/i18n/<lang>.js, read through
app.api.i18n.strings(lang). The key is the Russian string exactly as the code
writes it, so the sources stay Russian and a text is looked up, never rewritten.
What the user wrote (printer and job names, the admin's own Telegram templates)
is never translated; Bambu's HMS and print-error texts are not translated either:
the English original of the same code comes from Bambu's tables
(app.api.i18n.live_texts).

    t("офлайн", "en")              -> "offline"   (exact, as the panel's dictionary)
    tr("Ошибка печати 0300_400C")  -> "Print error 0300_400C"   (known pieces inside)
    duration(19800, "en")          -> "5h30m"
"""
import logging
import os
import re
import time
from threading import Lock
from typing import Any, Dict, Iterable, Optional

logger = logging.getLogger(__name__)

SOURCE = "ru"
# Under this share of Cyrillic letters left after the pieces a line counts as
# translated (app/web/i18n.js: a half-translated line is worse than the original).
MAX_LEFT = 0.3
_CYR = re.compile(r"[А-Яа-яЁё]")
_LETTER = re.compile(r"[^\W\d_]")

_lock = Lock()
_dicts: Dict[str, Any] = {}       # lang -> (mtime, {ru: text}, pieces regex or None)


def languages():
    """("ru", "en", ...): the source and every dictionary of app/web/i18n."""
    from app.api import i18n as i18n_api
    return (SOURCE,) + tuple(lang for lang in i18n_api.languages() if lang != SOURCE)


def bot_lang(values: Optional[Dict[str, Any]] = None) -> str:
    """The Telegram bot's language (settings «telegram_lang»), the source when unknown."""
    if values is not None:
        lang = values.get("telegram_lang")
    else:
        from app.services import settings_service
        lang = settings_service.peek("telegram_lang")
    return lang if isinstance(lang, str) and lang in languages() else SOURCE


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace(" ", " ")).strip()


def _dictionary(lang: str):
    """(strings, pieces) of one language, re-read when its file changes."""
    from app.api import i18n as i18n_api
    path = os.path.join(i18n_api.DICT_DIR, lang + ".js")
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}, None
    with _lock:
        hit = _dicts.get(lang)
        if hit and hit[0] == mtime:
            return hit[1], hit[2]
    strings = i18n_api.strings(lang)
    keys = sorted((k for k in strings if _CYR.search(k)), key=len, reverse=True)
    # the longest known piece first, only on word boundaries (a short one, «с» or «на»,
    # never inside a word)
    pieces = re.compile(r"(?<!\w)(?:%s)(?!\w)" % "|".join(re.escape(k) for k in keys)) if keys else None
    with _lock:
        _dicts[lang] = (mtime, strings, pieces)
    return strings, pieces


def t(s: Any, lang: Optional[str] = None) -> Any:
    """The exact translation of a Russian string (edge whitespace kept), else the string itself."""
    lang = bot_lang() if lang is None else lang
    if lang == SOURCE or not isinstance(s, str) or not s.strip():
        return s
    strings, _ = _dictionary(lang)
    text = strings.get(_norm(s))
    if text is None:
        return s
    return s[:len(s) - len(s.lstrip())] + text + s[len(s.rstrip()):]


def tr(s: Any, lang: Optional[str] = None) -> Any:
    """t(), else the known pieces inside the string (texts glued from parts, as
    «Ошибка печати 0300_400C: …»). The string comes back unchanged when more than
    MAX_LEFT of its letters would stay Russian: a name or a text the dictionary
    does not know."""
    lang = bot_lang() if lang is None else lang
    if lang == SOURCE or not isinstance(s, str) or not _CYR.search(s):
        return s
    exact = t(s, lang)
    if exact is not s:
        return exact
    strings, pieces = _dictionary(lang)
    if pieces is None:
        return s
    out = pieces.sub(lambda m: strings[m.group(0)], s)
    letters = len(_LETTER.findall(out))
    return out if letters and len(_CYR.findall(out)) <= MAX_LEFT * letters else s


def duration(seconds: Any, lang: Optional[str] = None) -> Optional[str]:
    """5ч30м / 5h30m; None for nothing or a negative time."""
    try:
        seconds = int(seconds or 0)
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    h, m = divmod(seconds // 60, 60)
    hs, ms = t("ч", lang), t("м", lang)
    if h and m:
        return f"{h}{hs}{m}{ms}"
    if h:
        return f"{h}{hs}"
    return f"{m}{ms}"


def date(ts: Any, lang: Optional[str] = None, with_time: bool = True, now: Optional[float] = None) -> str:
    """01.11 17:03 (Russian) / 01/11 17:03 (English, as the panel's en-GB dates);
    the year is added when it is not this year. "" for no time. (The bot's texts
    carry no date today: this is the one place for them.)"""
    lang = bot_lang() if lang is None else lang
    try:
        when = time.localtime(float(ts))
    except (TypeError, ValueError, OverflowError, OSError):
        return ""
    sep = "." if lang == SOURCE else "/"
    out = "%02d%s%02d" % (when.tm_mday, sep, when.tm_mon)
    if when.tm_year != time.localtime(time.time() if now is None else now).tm_year:
        out += "%s%d" % (sep, when.tm_year)
    if with_time:
        out += " %02d:%02d" % (when.tm_hour, when.tm_min)
    return out


def printer_error(text: Any, status: Any = None, lang: Optional[str] = None) -> Any:
    """last_error in a language: Bambu's texts as the English original of their code
    (the printer's notifications carry the codes), the server's own words by the
    dictionary, the rest (the printer's own messages) as it is."""
    lang = bot_lang() if lang is None else lang
    if lang == SOURCE or not isinstance(text, str) or not _CYR.search(text):
        return text
    bambu: Dict[str, str] = {}
    if status is not None:
        from app.api import i18n as i18n_api
        row = status if isinstance(status, dict) else {
            "id": getattr(status, "id", None), "device_type": getattr(status, "device_type", None),
            "notifications": getattr(status, "notifications", None)}
        try:
            bambu = i18n_api.live_texts([row], lang)
        except Exception:
            logger.debug("[i18n] Bambu's English texts unavailable", exc_info=True)
    parts = []
    for part in text.split(" | "):
        for ru in sorted(bambu, key=len, reverse=True):
            if ru in part:
                part = part.replace(ru, bambu[ru])
        parts.append(tr(part, lang))
    return " | ".join(parts)


# ---- the Telegram templates ------------------------------------------------

TEMPLATE_KEYS = ("telegram_finish_template", "telegram_error_template", "telegram_paused_template")


def default_template(key: str, lang: Optional[str] = None) -> str:
    """The default of a notification template in a language: its text around {label}
    looked up in the dictionary (the Russian default when there is no translation)."""
    from app.services import settings_service
    lang = bot_lang() if lang is None else lang
    return "{label}".join(t(part, lang) for part in settings_service.DEFAULTS[key].split("{label}"))


def template(values: Dict[str, Any], key: str) -> str:
    """The template the bot sends: the admin's own text as it is; a default (the
    Russian one, or its translation saved from the form) in the bot's language."""
    value = values.get(key)
    if not isinstance(value, str) or not value.strip() or value in _defaults(key):
        return default_template(key, bot_lang(values))
    return value


def _defaults(key: str) -> Iterable[str]:
    return {default_template(key, lang) for lang in languages()}
