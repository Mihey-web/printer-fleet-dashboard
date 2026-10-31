"""Printer notifications: PrinterStatus.notifications (notifications spec §2).

One record per thing the printer reports as wrong or worth knowing, built from
the same inputs as last_error (which stays as it is: golden tests, Telegram,
history), each with its level, Russian text, our hint, a wiki link, the time it
was first seen and the buttons that answer it (notification_actions).

Pure functions, no I/O: Bambu's tables are read from memory (hms_catalog), the
vendored lib's texts from its cached lookups. Klipper (klippy's host state, a
print error) and Creality (err.errcode / key) are built from the clients' data;
for them last_error is the line of their errors (error_line).
"""
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

from app.collectors.bambu_raw import CANCEL_CODES
from app.services import bambu_extras, hms_catalog, notification_actions

MAX_ITEMS = 20
REASON_MAX = 300

# Ranks for sorting and the worst level (fatal > pause > serious > common = info).
RANK = {"fatal": 4, "pause": 3, "serious": 2, "common": 1, "info": 1}
# HMS level = code >> 16; other values (0, >= 5) are not shown.
HMS_SEVERITY = {1: "fatal", 2: "serious", 3: "common", 4: "info"}
# print_error level = (low 16 bits & 0xF000) >> 12; below 0x4000 = a status, not an error.
PE_MIN_LOW = 0x4000
PE_SEVERITY = {0x4: "fatal", 0x8: "pause", 0xC: "common"}
# A code without a text anywhere is shown at these levels only (as "unknown");
# Studio hides an empty intro, and so does the dashboard for the lesser levels.
SILENT_SHOWN = ("fatal", "serious")
# The lesser HMS levels: routine notes the lists fold away ("routine", not
# "maintenance": that name is the printer's maintenance mode, spec_maintenance).
ROUTINE = ("common", "info")
ATTENTION_STATES = ("paused", "error", "cancelled")

# Bambu's wiki per model; codes the lib has no own page for link the model's HMS page.
WIKI_BASE = "https://wiki.bambulab.com/en/%s/troubleshooting/hmscode/%s"
WIKI_HOME = "/hms/home"
WIKI_PATH = {"X1C": "x1", "X1": "x1", "X1E": "x1", "P1S": "p1", "P1P": "p1", "P2S": "p2s", "H2S": "h2d",
             "H2D": "h2d", "A1": "a1", "A1M": "a1-mini", "A1MINI": "a1-mini"}
WIKI_DEFAULT = "x1"
# Bambu's page of a print error. No d=: Studio puts the serial there.
PE_WIKI = "https://e.bambulab.com/index.php?e=%s&s=device_error&lang=ru"

# Our own hints (texts of the approved mockup).
CANCEL_HINT = ("Это не сбой: задание отменили. «Закрыть диалог» уберёт сообщение с экрана принтера, "
               "карточка вернётся в «Простой».")
LUBE_HINT = "Обслуживание: гайд по смазке — по ссылке «Подробнее». Команды нет."
NO_COMMAND_HINT = "Команды для этого кода нет: устраните причину, принтер снимет уведомление сам."
NO_TABLE_HINT = "Таблица действий Bambu ещё не загружена — решите на экране принтера."
UNKNOWN_HINT = "Bambu не публикует описание этого кода — поищите его по ссылке «Подробнее»."
KLIPPER_SHUTDOWN_HINT = "Проверьте USB-кабель и питание платы, затем перезапустите прошивку (FIRMWARE_RESTART)."
KLIPPER_DISCONNECTED_HINT = "Перезапустите службу klipper на хосте."
CREALITY_HINT = ("Подтвердить ошибку из «Кузницы» нельзя: у Creality по WebSocket нет такой команды. "
                 "Сбросьте на экране принтера, расшифровку смотрите в таблице кодов Creality.")
_FEED_HINT = "Сначала вставьте пруток в тот же слот, потом «Продолжить»."
_CUT_TIP_HINT = "Обрежьте кончик прутка ровно и вставьте заново, потом «Готово — продолжить» (AMS повторит подачу)."
HINTS = {
    "0300_8007": "Продолжайте, только если модель держится на столе: проверьте по камере или на месте.",
    "0700_8011": _FEED_HINT,
    "07FF_8011": _FEED_HINT,
    "0700_8005": _CUT_TIP_HINT,
    "0700_8004": _CUT_TIP_HINT,
    "0300_2E00_0003_0001": ("Перекалибровать на экране принтера — в таблице Bambu это мастер на самом принтере, "
                            "из «Кузницы» команды нет."),
}
HINT_PREFIXES = (("0501_0400_", LUBE_HINT),)   # rod / rail lubrication and cleaning


def hint_for(code: str) -> Optional[str]:
    if code in HINTS:
        return HINTS[code]
    return next((hint for prefix, hint in HINT_PREFIXES if code.startswith(prefix)), None)


def _model_key(model: Any) -> str:
    return str(model or "").upper().replace(" ", "").replace("-", "")


def hms_wiki(code: str, model: Any, lib_wiki: Any = None) -> str:
    """The lib's wiki page of the code, unless it only has the HMS home page;
    then the code's page in the model's section of the wiki."""
    wiki = str(lib_wiki or "")
    if wiki and not wiki.rstrip("/").endswith(WIKI_HOME):
        return wiki
    return WIKI_BASE % (WIKI_PATH.get(_model_key(model), WIKI_DEFAULT), code)


def _hms_code(attr: int, code: int) -> str:
    return f"{attr >> 16:04X}_{attr & 0xFFFF:04X}_{code >> 16:04X}_{code & 0xFFFF:04X}"


def _lib_hms(hms_obj: Any) -> Dict[str, Tuple[Optional[str], Optional[str]]]:
    """The lib's hms.errors as {code: (text, wiki)}, in its order. The lib
    resolved the texts already: its lookup is not called again (a miss
    unpacks the whole table). "unknown" (no such code in its tables) = no text."""
    errors = getattr(hms_obj, "errors", None)
    if not isinstance(errors, dict):
        return {}

    def _idx(key: str) -> int:
        try:
            return int(key.split("-", 1)[0])
        except (ValueError, IndexError):
            return 0

    out: Dict[str, Tuple[Optional[str], Optional[str]]] = {}
    for key in sorted((k for k in errors if str(k).endswith("-Code")), key=_idx):
        code = str(errors.get(key) or "").strip().upper()
        if code.startswith("HMS_"):
            code = code[len("HMS_"):]
        if not code:
            continue
        prefix = str(key)[:-len("-Code")]
        text = str(errors.get(f"{prefix}-Error") or "").strip()
        out.setdefault(code, (text if text and text.lower() != "unknown" else None, errors.get(f"{prefix}-Wiki")))
    return out


def _parse_hms(code: str) -> Optional[Tuple[int, int]]:
    """"AAAA_BBBB_CCCC_DDDD" -> (attr, code)."""
    parts = code.split("_")
    if len(parts) != 4 or not all(len(p) == 4 for p in parts):
        return None
    try:
        a, b, c, d = (int(p, 16) for p in parts)
    except ValueError:
        return None
    return (a << 16) | b, (c << 16) | d


def _raw_hms(raw: Dict[str, Any], hms_obj: Any) -> List[Dict[str, Any]]:
    """The printer's hms[] in its order: RawReport's (with first-seen times),
    else the lib's raw list (test doubles, a device built outside the collector)."""
    items = raw.get("hms")
    if not isinstance(items, list):
        items = getattr(hms_obj, "_hms_list", None)
    return items if isinstance(items, list) else []


def _record(key: str, src: str, code: str, severity: str, text: Optional[str], hint: Optional[str],
            wiki: Optional[str], seen_at: Optional[float], actions: List[Dict[str, Any]],
            blocking: bool) -> Dict[str, Any]:
    # since: the first-seen time; the notification tracker replaces it with the
    # time it keeps across restarts.
    return {"key": key, "src": src, "code": code, "severity": severity, "text": text, "unknown": text is None,
            "hint": hint, "wiki": wiki, "since": seen_at, "seen_at": seen_at, "actions": actions,
            "blocking": blocking, "routine": src == "hms" and severity in ROUTINE}


def _pe_text(value: int, prefix: Optional[str], model: Any) -> Optional[str]:
    """Bambu's runtime table, else the vendored lib's text through last_error's
    own cached lookup (print_error_line: the same call, so no second miss)."""
    text = hms_catalog.text(prefix, value)
    if text:
        return text
    line = bambu_extras.print_error_line(value, str(model or ""))
    head = "Ошибка печати %s: " % notification_actions.pe_label(value)
    if not line.startswith(head):
        return None
    return line[len(head):].strip() or None


def _print_error(value: Optional[int], raw: Dict[str, Any], model: Any, prefix: Optional[str]) -> Optional[Dict[str, Any]]:
    if not value or value <= 0:
        return None
    value &= 0xFFFFFFFF
    cancel = value in CANCEL_CODES
    low = value & 0xFFFF
    if not cancel and low < PE_MIN_LOW:
        return None
    code = notification_actions.pe_label(value)
    severity = "common" if cancel else PE_SEVERITY.get((low & 0xF000) >> 12, "common")
    actions, blocking = notification_actions.actions_for("print_error", code, prefix, "bambu")
    if cancel:
        hint = CANCEL_HINT
    else:
        hint = hint_for(code)
        if hint is None and not actions and not notification_actions.table_known(prefix, code):
            hint = NO_TABLE_HINT
    seen_at = raw.get("print_error_since") if raw.get("print_error") == value else None
    return _record("pe:" + code, "print_error", code, severity, _pe_text(value, prefix, model), hint,
                   None if cancel else PE_WIKI % code.replace("_", ""), seen_at, actions, blocking)


def _hms(code: str, attr_code: Tuple[int, int], seen_at: Any, lib: Tuple[Optional[str], Optional[str]],
         model: Any, prefix: Optional[str]) -> Optional[Dict[str, Any]]:
    attr, value = attr_code
    if attr <= 0 or value <= 0:
        return None
    severity = HMS_SEVERITY.get(value >> 16)
    if severity is None:
        return None
    text = hms_catalog.text(prefix, code) or lib[0]
    if text is None and severity not in SILENT_SHOWN:
        return None
    actions, blocking = notification_actions.actions_for("hms", code, prefix, "bambu")
    hint = hint_for(code)
    if hint is None and text is None:
        hint = UNKNOWN_HINT
    elif hint is None and not actions and severity in SILENT_SHOWN:
        hint = NO_COMMAND_HINT
    seen = seen_at if isinstance(seen_at, (int, float)) and not isinstance(seen_at, bool) else None
    return _record("hms:" + code, "hms", code, severity, text, hint, hms_wiki(code, model, lib[1]), seen,
                   actions, blocking)


def build_bambu(hms_obj: Any, raw: Optional[Dict[str, Any]], print_error: Optional[int], model: Any = None,
                prefix: Optional[str] = None, gcode_state: Any = None) -> List[Dict[str, Any]]:
    """A Bambu printer's notifications, worst first (sort_and_cap).

    print_error: the lib's value (the one last_error shows). HMS codes in the
    printer's order: RawReport's hms[] (first-seen times), then whatever the
    lib's hms.errors lists beyond it (the MQTT thread swaps the two one after
    the other), so every code of last_error has a record. gcode_state is
    accepted for the callers' symmetry; no rule depends on it yet.
    """
    raw = raw or {}
    lib = _lib_hms(hms_obj)
    out: List[Dict[str, Any]] = []
    seen = set()

    def add(n: Optional[Dict[str, Any]]) -> None:
        if n is not None and n["key"] not in seen:
            seen.add(n["key"])
            out.append(n)

    add(_print_error(print_error, raw, model, prefix))
    listed = set()
    for item in _raw_hms(raw, hms_obj):
        try:
            attr, value = int(item["attr"]), int(item["code"])
        except (KeyError, TypeError, ValueError):
            continue
        if attr <= 0 or value <= 0:
            continue
        code = _hms_code(attr, value)
        listed.add(code)
        add(_hms(code, (attr, value), item.get("first_seen"), lib.get(code, (None, None)), model, prefix))
    for code, entry in lib.items():
        parsed = _parse_hms(code)
        if code not in listed and parsed is not None:
            add(_hms(code, parsed, None, entry, model, prefix))
    return sort_and_cap(out)


# --- Klipper (Moonraker) ---------------------------------------------------------

KLIPPER_DOWN = ("shutdown", "error", "disconnected")   # the host is up, klippy is not: ERROR, online
MSG_KLIPPY_DISCONNECTED = "Klippy отключился от Moonraker"
KLIPPER_TEXT_MAX = 300


def klipper_host_state(data: Dict[str, Any]) -> Optional[str]:
    """klippy's state (lower case): the client's host_state (webhooks.state,
    server.info, notify_klippy_*), else debug.webhooks_state; None = unknown."""
    state = data.get("host_state")
    if state is None:
        debug = data.get("debug")
        state = debug.get("webhooks_state") if isinstance(debug, dict) else None
    if state is None:
        return None
    return str(state).strip().lower() or None


def _first_line(text: Any) -> Optional[str]:
    """Klipper's messages run over several lines ("Lost communication with MCU
    'mcu'\\nOnce the underlying issue is corrected, ..."): the first one is the
    reason, the rest is the same advice every time."""
    line = next((s.strip() for s in str(text or "").splitlines() if s.strip()), "")
    return line[:KLIPPER_TEXT_MAX] or None


def _plain(key: str, src: str, code: str, severity: str, text: str, hint: Optional[str] = None,
           actions: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    return _record(key, src, code, severity, text, hint, None, None, actions or [], False)


def build_klipper(data: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """A Klipper printer's notifications from the client's data: klippy's host
    state (shutdown / error / disconnected: fatal, startup: info) with its
    message, and a print error (print_stats.state "error" with its message;
    a print error that only repeats the host's message is left out)."""
    data = data or {}
    host = klipper_host_state(data)
    message = _first_line(data.get("host_message"))
    out: List[Dict[str, Any]] = []
    if host == "shutdown":
        text = "Klipper остановлен (shutdown)" + (": " + message if message else "")
        out.append(_plain("klipper:shutdown", "klipper", "webhooks: shutdown", "fatal", text, KLIPPER_SHUTDOWN_HINT,
                          notification_actions.klipper_actions(host)))
    elif host == "error":
        text = "Ошибка Klipper" + (": " + message if message else "")
        out.append(_plain("klipper:error", "klipper", "webhooks: error", "fatal", text,
                          actions=notification_actions.klipper_actions(host)))
    elif host == "disconnected":
        out.append(_plain("klipper:disconnected", "klipper", "klippy: disconnected", "fatal",
                          message or MSG_KLIPPY_DISCONNECTED, KLIPPER_DISCONNECTED_HINT))
    elif host == "startup":
        out.append(_plain("klipper:startup", "klipper", "webhooks: startup", "info", "Klipper запускается"))
    if str(data.get("state") or "").strip().lower() == "error":
        print_message = _first_line(data.get("print_message"))
        if host not in KLIPPER_DOWN or (print_message and print_message != message):
            out.append(_plain("klipper:print_error", "klipper", "print_stats: error", "fatal",
                              print_message or "Ошибка печати Klipper"))
    return sort_and_cap(out)


# --- Creality --------------------------------------------------------------------

def build_creality(data: Optional[Dict[str, Any]], state: Any) -> List[Dict[str, Any]]:
    """A Creality printer's error (WS :9999 err = {errcode, key}; 0 = none).
    Its level follows the print: still printing = serious, paused = pause,
    standing (idle / finished / unknown / error) = fatal, and the normalizer
    shows that printer as ERROR. Not live-verified: no err != 0 seen yet."""
    data = data or {}
    code = data.get("err_code")
    if not isinstance(code, int) or isinstance(code, bool) or code == 0:
        return []
    key = data.get("err_key")
    key_s = re.sub(r"[^0-9A-Za-z_-]", "", str(key))[:32] if key is not None else ""
    state = str(getattr(state, "value", state) or "").lower()
    severity = {"printing": "serious", "paused": "pause"}.get(state, "fatal")
    if key is None:
        label, text = "errcode %d" % code, "Ошибка Creality %d" % code
    else:
        label, text = "errcode %d · key %s" % (code, key), "Ошибка Creality %d/%s" % (code, key)
    return [_plain("creality:%d%s" % (code, "/" + key_s if key_s else ""), "creality", label, severity, text,
                   CREALITY_HINT)]


# Levels that make last_error of a WebSocket printer (Bambu keeps its own line).
ERROR_LINE = ("fatal", "pause", "serious")


def error_line(items: Iterable[Dict[str, Any]]) -> Optional[str]:
    """last_error of a Klipper / Creality status: the texts of its errors."""
    parts = [str(n.get("text") or n.get("code")) for n in items or [] if n.get("severity") in ERROR_LINE]
    return " | ".join(parts) or None


def sort_and_cap(items: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Worst level first, the printer's order within a level; at most MAX_ITEMS
    (the least severe go)."""
    ranked = sorted(items, key=lambda n: -RANK.get(n.get("severity"), 0))
    return ranked[:MAX_ITEMS]


def _field(status: Any, name: str) -> Any:
    return status.get(name) if isinstance(status, dict) else getattr(status, name, None)


def top_reason(status: Any) -> Optional[str]:
    """Why a paused / errored / cancelled printer stands, for the event log:
    "<code>: <text>" of the worst record that is not a lesser HMS, else
    "stage:<slug>" of a pause; None otherwise."""
    state = _field(status, "state")
    state = getattr(state, "value", state)
    if state not in ATTENTION_STATES:
        return None
    best = None
    for n in _field(status, "notifications") or []:
        if n.get("src") == "hms" and n.get("severity") not in ("fatal", "pause"):
            continue
        if best is None or RANK.get(n.get("severity"), 0) > RANK.get(best.get("severity"), 0):
            best = n
    if best is not None:
        text = best.get("text")
        reason = "%s: %s" % (best.get("code"), text) if text else str(best.get("code"))
        return reason[:REASON_MAX]
    stage = _field(status, "stage")
    if state == "paused" and stage:
        return ("stage:%s" % stage)[:REASON_MAX]
    return None
