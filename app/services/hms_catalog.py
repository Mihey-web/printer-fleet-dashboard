"""Bambu's own HMS tables: the dialog buttons of every error code and its Russian text.

Bambu Studio builds its error dialog from two tables it downloads per printer
model: GetActionImage.php (the button ids of every code) and query.php (the
texts). This daemon thread does the same once a day (notifications spec,
step 3). The texts come twice: in Russian (the panel's cards) and in Bambu's
English original (the English panel and bot, for the codes the vendored
English table does not have; app/api/i18n.py). Only the 3-character model prefix of a serial ever leaves the farm
(d=22E), never a serial, and no cookies or credentials are sent.

The tables are Bambu's data: they live in data/hms/ (gitignored, not in the DB
backups) and are never vendored into the repo. Without them the callers fall
back to the vendored ha-bambulab texts and a generic button rule.
"""
import json
import logging
import os
import re
import threading
import time
import urllib.parse
import urllib.request
import zlib
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

ACTION_URL = "https://e.bambulab.com/hms/GetActionImage.php"
TEXT_URL = "https://e.bambulab.com/query.php"
LANG = "ru"
EN = "en"                       # the second text table: Bambu's English original
USER_AGENT = "Forge-HMS/1"
TIMEOUT_S = 15                  # each socket operation
DOWNLOAD_S = 60                 # the whole download: a trickling answer never stalls the refresh loop
CHUNK = 64 * 1024
MAX_BYTES = 8 * 1024 * 1024     # the ru text table is ~1 MB unpacked (18.10.2026)
# Per record: a real one lists a handful of button ids and a text of a few
# sentences; a broken or tampered table must not cost minutes to parse or
# swell every /api/printers record.
MAX_IDS = 32
MAX_ID_LIST = 64                # longer: the record's buttons are dropped
MAX_INTRO = 1000
START_DELAY_S = 30              # let the poll loop start first; the cache is loaded at once
LOOP_S = 3600                   # registry re-read + staleness check; a failed refresh retries on the next pass
REFRESH_S = 24 * 3600
NOT_MODIFIED = 201              # {"result":201,"ver":0}: the v=<ver> we sent is current

CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                         "data", "hms")
META_FILE = "meta.json"
FILES = {"action": "action_%s.json", "text": "text_" + LANG + "_%s.json", "text_en": "text_" + EN + "_%s.json"}
# kind -> (index keys, version slot, query.php language)
_KINDS = {"action": (("act",), 0, None), "text": (("err", "hms"), 1, LANG), "text_en": (("err_en", "hms_en"), 2, EN)}

PREFIX_RE = re.compile(r"^[0-9A-Z]{3}$")
_VER_RE = re.compile(r"^[0-9A-Za-z._-]{1,32}$")
_HEX = frozenset("0123456789ABCDEF")

# Model -> serial prefix, for printers registered without a usable serial.
MODEL_PREFIX = {"X1C": "00M", "X1": "00W", "X1E": "03W", "P1P": "01S", "P1S": "01P", "A1M": "030",
                "A1MINI": "030", "A1": "039", "P2S": "22E", "H2S": "093", "H2D": "094", "H2C": "31B",
                "X2D": "20P"}

# Studio answers these AMS print errors with its own «Повторить» (10002) and
# «Подтвердить» (10004) buttons instead of the table's.
FIXED_ACTIONS = {c.replace("_", ""): (10002, 10004) for c in (
    "0701_8004", "0701_8005", "0701_8007", "0701_8012", "0702_8012", "0703_8012", "07FF_8012", "07FF_8013")}


class BadResponse(ValueError):
    """The server answered, but not with a table we can use."""


# Readers never lock: every update builds a new dict and swaps the reference.
_prefix_by_pid: Dict[str, str] = {}
# P -> {"act", "err", "hms", "err_en", "hms_en", "ver": (action_ver, text_ver, text_en_ver)}
_index: Dict[str, Dict[str, Any]] = {}
_meta: Optional[Dict[str, Dict[str, Any]]] = None   # P -> {action_ver, text_ver, text_en_ver, checked_at, error}
_EMPTY = {"act": None, "err": None, "hms": None, "err_en": None, "hms_en": None, "ver": (None, None, None)}
_VER_KEYS = ("action_ver", "text_ver", "text_en_ver")


# ---- prefixes -------------------------------------------------------------

def model_prefix(model: Any) -> Optional[str]:
    return MODEL_PREFIX.get(str(model or "").upper().replace(" ", "").replace("-", ""))


def serial_prefix(serial: Any) -> Optional[str]:
    p = str(serial or "").strip().upper()[:3]
    return p if PREFIX_RE.match(p) else None


def collect_prefixes(rows: Iterable[Dict[str, Any]]) -> Dict[str, str]:
    """{pid: prefix} for the Bambu printers of the registry (serial first, then the model)."""
    out = {}
    for r in rows or []:
        if r.get("kind") != "bambu" or not r.get("id"):
            continue
        p = serial_prefix(r.get("serial")) or model_prefix(r.get("model"))
        if p:
            out[str(r["id"])] = p
    return out


def prefix_for(pid: str, model: Any = None) -> Optional[str]:
    return _prefix_by_pid.get(pid) or model_prefix(model)


# ---- lookups --------------------------------------------------------------

def _norm(ecode: Any) -> Optional[str]:
    """0x07008011 / "0700_8011" / "HMS_0700_2500_0002_0001" -> upper hex, 8 or 16 digits."""
    if isinstance(ecode, bool):
        return None
    if isinstance(ecode, int):
        s = "%08X" % (ecode & 0xFFFFFFFF)
    else:
        s = str(ecode or "").strip().upper()
        if s.startswith("HMS_"):
            s = s[4:]
        s = s.replace("_", "")
    return s if len(s) in (8, 16) and set(s) <= _HEX else None


def text(prefix: Optional[str], ecode: Any, lang: str = LANG) -> Optional[str]:
    """Bambu's Russian (lang="en": English) text of a print_error (8 hex) or HMS
    (16 hex) code. None: no table, no such code, or an empty intro (Studio hides those)."""
    code = _norm(ecode)
    e = _index.get(prefix or "")
    if code is None or not e:
        return None
    section = ("err" if len(code) == 8 else "hms") + ("_en" if lang == EN else "")
    return (e.get(section) or {}).get(code) or None


def action_ids(prefix: Optional[str], ecode: Any) -> Tuple[List[int], bool]:
    """Bambu's button ids of a code -> (ids, from_table).

    The record of this model wins over "default"; the two are never merged
    (Studio merges them). from_table is False only when the model's table is
    not loaded: the caller then uses its own fallback. A code the loaded table
    does not list gets ([], True)."""
    code = _norm(ecode)
    if code is None:
        return [], False
    if code in FIXED_ACTIONS:
        return list(FIXED_ACTIONS[code]), True
    act = (_index.get(prefix or "") or {}).get("act")
    if act is None:
        return [], False
    rec = act.get(code) or {}
    return list(rec["dev"] if "dev" in rec else rec.get("def", ())), True


def status() -> Dict[str, Dict[str, Any]]:
    """Per prefix: versions, last check, table sizes, last error (admin page)."""
    idx, meta, pids = _index, _meta or {}, _prefix_by_pid
    out = {}
    for p in sorted(set(pids.values()) | set(idx)):
        e, m = idx.get(p) or _EMPTY, meta.get(p) or {}
        out[p] = {"action_ver": e["ver"][0], "text_ver": e["ver"][1], "text_en_ver": e["ver"][2],
                  "checked_at": m.get("checked_at"),
                  "n_actions": len(e["act"] or {}), "n_err": len(e["err"] or {}), "n_hms": len(e["hms"] or {}),
                  "error": m.get("error"), "printers": sum(1 for v in pids.values() if v == p)}
    return out


# ---- parsing --------------------------------------------------------------

def _result(doc: Any) -> Optional[int]:
    try:
        return int(doc.get("result"))
    except (AttributeError, TypeError, ValueError):
        return None


def _ver(doc: Dict[str, Any]) -> Optional[str]:
    v = doc.get("ver")
    v = str(v) if isinstance(v, (str, int)) and not isinstance(v, bool) else ""
    return v if _VER_RE.match(v) and v != "0" else None


def _ids(raw: Any) -> List[int]:
    if not isinstance(raw, list) or len(raw) > MAX_ID_LIST:
        return []
    out, seen = [], set()
    for a in raw:
        if isinstance(a, str) and a.strip().isdecimal() and len(a) < 12:
            a = int(a)
        if isinstance(a, int) and not isinstance(a, bool) and a not in seen:
            seen.add(a)
            out.append(a)
            if len(out) >= MAX_IDS:
                break
    return out


def parse_actions(doc: Any, prefix: str) -> Dict[str, Dict[str, List[int]]]:
    """GetActionImage.php body -> {ecode: {"dev": ids, "def": ids}}."""
    if _result(doc) != 0 or not isinstance(doc.get("data"), list):
        raise BadResponse("action table: unexpected structure")
    act: Dict[str, Dict[str, List[int]]] = {}
    for item in doc["data"]:
        if not isinstance(item, dict):
            continue
        code, dev = _norm(item.get("ecode")), str(item.get("device") or "").strip()
        slot = "dev" if dev.upper() == prefix else "def" if dev.lower() == "default" else None
        if code is not None and slot is not None:
            act.setdefault(code, {}).setdefault(slot, _ids(item.get("actions")))   # first record wins
    return act


def parse_texts(doc: Any, lang: str = LANG) -> Tuple[Dict[str, str], Dict[str, str]]:
    """query.php body -> ({ecode8: intro}, {ecode16: intro}); an empty intro stays ""."""
    data = doc.get("data") if _result(doc) == 0 else None
    if not isinstance(data, dict):
        raise BadResponse("text table: unexpected structure")
    out = []
    for section, size in (("device_error", 8), ("device_hms", 16)):
        sec = data.get(section)
        rows = sec.get(lang) if isinstance(sec, dict) else None
        if not isinstance(rows, list):
            raise BadResponse("text table: no %s/%s" % (section, lang))
        table: Dict[str, str] = {}
        for item in rows:
            code = _norm(item.get("ecode")) if isinstance(item, dict) else None
            if code is not None and len(code) == size:
                intro = item.get("intro")
                table.setdefault(code, intro.strip()[:MAX_INTRO] if isinstance(intro, str) else "")
        out.append(table)
    return out[0], out[1]


def _apply(entry: Dict[str, Any], kind: str, doc: Dict[str, Any], prefix: str) -> Dict[str, Any]:
    """A copy of the index entry with this kind's table replaced (raises BadResponse)."""
    entry = dict(entry)
    keys, slot, lang = _KINDS[kind]
    if kind == "action":
        entry["act"] = parse_actions(doc, prefix)
    else:
        entry[keys[0]], entry[keys[1]] = parse_texts(doc, lang)
    ver = list(entry["ver"])
    ver[slot] = _ver(doc)
    entry["ver"] = tuple(ver)
    return entry


def _loaded(entry: Dict[str, Any], kind: str) -> bool:
    return entry[_KINDS[kind][0][0]] is not None


# ---- disk -----------------------------------------------------------------

def _path(name: str) -> str:
    return os.path.join(CACHE_DIR, name)


def _write_atomic(name: str, data: bytes) -> None:
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = _path(name)
    tmp = path + ".tmp"
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _read_doc(name: str) -> Any:
    with open(_path(name), "rb") as f:
        return json.loads(f.read().decode("utf-8-sig"))


def _load_meta() -> Dict[str, Dict[str, Any]]:
    global _meta
    if _meta is None:
        try:
            doc = _read_doc(META_FILE)
            _meta = {k: dict(v) for k, v in doc.items() if isinstance(v, dict)} if isinstance(doc, dict) else {}
        except FileNotFoundError:
            _meta = {}
        except Exception as e:
            logger.warning("[hms-catalog] meta.json unreadable (%s), starting over", type(e).__name__)
            _meta = {}
    return _meta


def _save_meta(meta: Dict[str, Dict[str, Any]]) -> None:
    global _meta
    _write_atomic(META_FILE, json.dumps(meta, ensure_ascii=False, indent=1, sort_keys=True).encode("utf-8"))
    _meta = meta


def _load_prefix(prefix: str) -> None:
    """Cached tables of one prefix -> memory. A missing or broken file stays unloaded
    and is downloaded on the next refresh."""
    entry = dict(_index.get(prefix) or _EMPTY)
    for kind, name in FILES.items():
        try:
            entry = _apply(entry, kind, _read_doc(name % prefix), prefix)
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.warning("[hms-catalog] %s: cached %s table unreadable (%s), will download again",
                           prefix, kind, type(e).__name__)
    _index_set(prefix, entry)


def _index_set(prefix: str, entry: Dict[str, Any]) -> None:
    global _index
    _index = {**_index, prefix: entry}


# ---- network --------------------------------------------------------------

def _url(kind: str, prefix: str, ver: Optional[str]) -> str:
    """Nothing but the model prefix, the language and the cached version goes out."""
    if not PREFIX_RE.match(prefix or ""):
        raise ValueError("bad model prefix")
    lang = _KINDS[kind][2]
    q = [("lang", lang)] if lang else []
    q.append(("d", prefix))
    if ver:
        q.append(("v", ver))
    return (ACTION_URL if kind == "action" else TEXT_URL) + "?" + urllib.parse.urlencode(q)


def _monotonic() -> float:
    return time.monotonic()


def _http_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json",
                                               "Accept-Encoding": "gzip"})
    deadline = _monotonic() + DOWNLOAD_S
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        gz = "gzip" in str(resp.headers.get("Content-Encoding") or "").lower()
        # read1: whatever has arrived (one socket read), so the deadline is checked
        # between them; read() would wait for a whole chunk of a trickle
        read = getattr(resp, "read1", None) or resp.read
        parts, size = [], 0
        while size <= MAX_BYTES:
            if _monotonic() > deadline:
                raise BadResponse("timeout")
            chunk = read(min(CHUNK, MAX_BYTES + 1 - size))
            if not chunk:
                break
            parts.append(chunk)
            size += len(chunk)
        body = b"".join(parts)
    if len(body) > MAX_BYTES:
        raise BadResponse("response too large")
    if gz:
        d = zlib.decompressobj(16 + zlib.MAX_WBITS)
        body = d.decompress(body, MAX_BYTES + 1)
        if len(body) > MAX_BYTES or d.unconsumed_tail:
            raise BadResponse("response too large")
    return body


def _refresh_prefix(prefix: str, now: float) -> None:
    """Both tables of one prefix: 201 keeps the cache, a new table replaces it,
    an error keeps it and leaves checked_at alone (retried on the next pass)."""
    entry = _index.get(prefix) or _EMPTY
    errors = []
    for kind in FILES:
        have = entry["ver"][_KINDS[kind][1]] if _loaded(entry, kind) else None
        try:
            body = _http_get(_url(kind, prefix, have))
            doc = json.loads(body.decode("utf-8-sig"))
            if have and _result(doc) == NOT_MODIFIED:
                continue
            new = _apply(entry, kind, doc, prefix)
            _write_atomic(FILES[kind] % prefix, body)
            entry = new
            _index_set(prefix, entry)
            n = sum(len(entry[k]) for k in _KINDS[kind][0])
            logger.info("[hms-catalog] %s: %s table ver %s (%d codes)", prefix, kind,
                        entry["ver"][_KINDS[kind][1]], n)
        except Exception as e:
            errors.append(type(e).__name__)
            logger.warning("[hms-catalog] %s: %s table not refreshed: %s: %s", prefix, kind, type(e).__name__,
                           str(e)[:200])
    meta = {k: dict(v) for k, v in _load_meta().items()}
    m = meta.setdefault(prefix, {})
    m.update(zip(_VER_KEYS, entry["ver"]))
    if errors:
        m["error"] = ",".join(dict.fromkeys(errors))
    else:
        m["checked_at"], m["error"] = now, None
    try:
        _save_meta(meta)
    except Exception as e:
        logger.warning("[hms-catalog] meta.json not written: %s", type(e).__name__)


def _stale(prefix: str, now: float) -> bool:
    entry = _index.get(prefix) or _EMPTY
    if not all(_loaded(entry, k) for k in FILES):
        return True
    try:
        age = now - float((_load_meta().get(prefix) or {}).get("checked_at") or 0)
    except (TypeError, ValueError):
        return True
    return not 0 <= age < REFRESH_S     # a clock that went back (fake-hwclock) re-checks too


def _update_prefixes(rows: Optional[Iterable[Dict[str, Any]]] = None) -> List[str]:
    global _prefix_by_pid
    if rows is None:
        from app.services import printer_registry
        rows = printer_registry.list_printers(include_deleted=False)
    _prefix_by_pid = collect_prefixes(rows)
    return sorted(set(_prefix_by_pid.values()))


def load_cached(rows: Optional[Iterable[Dict[str, Any]]] = None) -> None:
    """Prefixes from the registry and the cached tables, no network (startup)."""
    _load_meta()
    for p in _update_prefixes(rows):
        _load_prefix(p)


def refresh_once(now: Optional[float] = None, rows: Optional[Iterable[Dict[str, Any]]] = None) -> None:
    now = time.time() if now is None else now
    _load_meta()
    for p in _update_prefixes(rows):
        if p not in _index:
            _load_prefix(p)             # a printer added since the last pass
        if _stale(p, now):
            _refresh_prefix(p, now)


def _loop() -> None:
    try:
        load_cached()
    except Exception:
        logger.error("[hms-catalog] loading the cache failed", exc_info=True)
    time.sleep(START_DELAY_S)
    while True:
        try:
            refresh_once()
        except Exception:
            logger.error("[hms-catalog] refresh failed", exc_info=True)
        time.sleep(LOOP_S)


def start() -> None:
    threading.Thread(target=_loop, daemon=True, name="hms-catalog").start()
