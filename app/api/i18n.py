"""The panel's languages on the server side (app/web/i18n.js, docs/dev/I18N_REPORT.md).

* GET /i18n.js — public, like /theme.js: the runtime (app/web/i18n.js) and every
  dictionary of app/web/i18n/*.js in one script, so the login page and the app
  both have the language before their first paint, and a new language is one
  more file in that folder. Only UI texts are in it, nothing about the farm.
* GET /manifest.<lang>.webmanifest — public, like /manifest.webmanifest: the PWA
  manifest with its names translated by that language's dictionary.
* GET / POST /api/i18n/texts?lang=en — behind the session (the /api gate): the
  fleet's live texts the server takes from Bambu's tables (HMS and print-error
  descriptions, Russian on the card) in the original English of those tables
  (the vendored ha-bambulab copy, app/vendor/pybambu/hms_error_text), keyed by
  the Russian text the panel shows. POST {"texts": [...]} also looks up the
  strings the client met and could not translate (the journal's reasons): found
  back by their Russian text in the same tables. The client asks when it meets
  Cyrillic it cannot translate.

The paths are fixed at import (one per dictionary file found), each served by its
own route, so the client's path never reaches the filesystem.
"""
import hashlib
import json
import logging
import os
import re
from typing import Any, Dict, Optional

from fastapi import FastAPI, Query, Request, Response

logger = logging.getLogger(__name__)

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WEB = os.path.join(ROOT, "app", "web")
RUNTIME = os.path.join(WEB, "i18n.js")
DICT_DIR = os.path.join(WEB, "i18n")
MANIFEST = os.path.join(WEB, "pwa", "manifest.webmanifest")

LANG_RE = re.compile(r"^[a-z]{2,3}$")
_STRINGS_RE = re.compile(r"/\*strings:begin\*/(.*?)/\*strings:end\*/", re.S)


def languages():
    """The dictionaries in app/web/i18n: ['en', ...] (file name = language code)."""
    try:
        names = sorted(os.listdir(DICT_DIR))
    except FileNotFoundError:
        return []
    return [n[:-3] for n in names if n.endswith(".js") and LANG_RE.match(n[:-3])]


LANGS = languages()
BUNDLE_PATH = "/i18n.js"
MANIFEST_PATHS = {"/manifest.%s.webmanifest" % lang: lang for lang in LANGS}
PUBLIC_PATHS = (BUNDLE_PATH,) + tuple(MANIFEST_PATHS)


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def bundle():
    """The runtime, every dictionary, then the start: one script."""
    parts = [_read(RUNTIME)]
    for lang in languages():
        parts.append("/* i18n/%s.js */\n" % lang + _read(os.path.join(DICT_DIR, lang + ".js")))
    parts.append("I18N.start();\n")
    return "\n".join(parts)


def strings(lang):
    """{ru: translation} of one dictionary: its "strings" block is plain JSON between markers."""
    if lang not in languages():
        return {}
    m = _STRINGS_RE.search(_read(os.path.join(DICT_DIR, lang + ".js")))
    if not m:
        return {}
    try:
        data = json.loads(m.group(1))
    except ValueError:
        logger.warning("[i18n] %s.js: the strings block is not JSON", lang)
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}


def manifest(lang):
    """The PWA manifest in one language: names, description and shortcuts looked up exactly."""
    doc = json.loads(_read(MANIFEST))
    tr = strings(lang)

    def t(s):
        return tr.get(s, s) if isinstance(s, str) else s

    for key in ("name", "short_name", "description"):
        if key in doc:
            doc[key] = t(doc[key])
    for sc in doc.get("shortcuts") or []:
        for key in ("name", "short_name", "description"):
            if key in sc:
                sc[key] = t(sc[key])
    doc["lang"] = lang
    return doc


def _cached(request, body, media_type):
    tag = '"%s"' % hashlib.sha256(body).hexdigest()[:32]
    headers = {"Cache-Control": "no-cache", "ETag": tag}
    if request.headers.get("if-none-match") == tag:
        return Response(status_code=304, headers=headers)
    return Response(content=body, media_type=media_type, headers=headers)


# ---- the fleet's live texts ---------------------------------------------------------------

def _english(src, code, model):
    from app.vendor.pybambu.utils import get_HMS_error_text, get_print_error_text
    code = str(code or "").strip()
    if not code:
        return None
    try:
        if src == "print_error":
            text = get_print_error_text(code, model or "", "en")
        elif src == "hms":
            text = get_HMS_error_text(code, model or "", "en")
        else:
            return None
    except Exception:
        logger.debug("[i18n] no English text for %s %s", src, code, exc_info=True)
        return None
    text = (text or "").strip()
    return text if text and text != "unknown" else None


def live_texts(rows, lang):
    """{russian text: original English} for the notifications of the given printer records."""
    out: Dict[str, str] = {}
    if lang != "en":
        return out
    for d in rows:
        model = d.get("device_type") or d.get("model")
        for n in d.get("notifications") or []:
            ru = n.get("text")
            if not isinstance(ru, str) or not ru.strip() or ru in out:
                continue
            en = _english(n.get("src"), n.get("code"), model)
            if en:
                out[ru] = en
    return out


# Bambu's texts the panel shows outside the live notifications (the print journal's
# pause and error reasons, the statistics' frequent reasons): found back by their
# Russian text in the same tables the server took them from.
MAX_ASK = 200
MAX_ASK_LEN = 2000
_reverse: Optional[Dict[str, Any]] = None
_CODE_HEAD = re.compile(r"^(?:HMS_)?[0-9A-Fa-f]{4}(?:_[0-9A-Fa-f]{4}){1,3}\s*[:—-]\s*")


def _key(s):
    return " ".join(str(s).split())


def _reverse_index():
    """{normalized Russian text: (src, code, model)} of the vendored tables (built once)."""
    global _reverse
    if _reverse is None:
        from app.vendor.pybambu.utils import _load_error_data
        idx: Dict[str, Any] = {}
        try:
            data = _load_error_data("ru")
        except Exception:
            logger.debug("[i18n] vendored ru table unreadable", exc_info=True)
            data = {}
        for section, src in (("device_error", "print_error"), ("device_hms", "hms")):
            for code, msgs in (data.get(section) or {}).items():
                if not isinstance(msgs, dict):
                    continue
                for msg, models in msgs.items():
                    if isinstance(msg, str) and msg.strip():
                        idx.setdefault(_key(msg), (src, code, (models or [""])[0]))
        _reverse = idx
    return _reverse


def _catalog_lookup(text):
    """The same in Bambu's runtime tables (app/services/hms_catalog, Russian only)."""
    try:
        from app.services import hms_catalog
        for _prefix, entry in list(hms_catalog._index.items()):
            for section, src in (("err", "print_error"), ("hms", "hms")):
                for code, ru in (entry.get(section) or {}).items():
                    if ru and _key(ru) == text:
                        return src, code, ""
    except Exception:
        logger.debug("[i18n] catalog lookup failed", exc_info=True)
    return None


def asked_texts(texts, lang):
    """{russian text: original English} for strings the panel could not translate."""
    out: Dict[str, str] = {}
    if lang != "en" or not isinstance(texts, list):
        return out
    idx = _reverse_index()
    for t in texts[:MAX_ASK]:
        if not isinstance(t, str) or not t.strip() or len(t) > MAX_ASK_LEN:
            continue
        t = _key(t)
        cands = [t, _CODE_HEAD.sub("", t)]
        for sep in (": ", " — "):
            if sep in t:
                cands.append(t.split(sep, 1)[1])
        for c in dict.fromkeys(cands):
            hit = idx.get(c) or (_catalog_lookup(c) if len(c) > 12 else None)
            if hit:
                en = _english(hit[0], hit[1], hit[2])
                if en:
                    out[c] = en
                break
    return out


def register(app: FastAPI, store: Any) -> None:
    """The routes above (create_app calls it)."""

    def bundle_endpoint(request: Request):
        return _cached(request, bundle().encode("utf-8"), "text/javascript")

    app.add_api_route(BUNDLE_PATH, bundle_endpoint, methods=["GET"], include_in_schema=False)

    for path, lang in MANIFEST_PATHS.items():
        def manifest_endpoint(request: Request, _lang: str = lang):
            body = json.dumps(manifest(_lang), ensure_ascii=False, indent=1).encode("utf-8")
            return _cached(request, body, "application/manifest+json")

        app.add_api_route(path, manifest_endpoint, methods=["GET"], include_in_schema=False)

    def fleet_rows():
        rows = []
        for s in store.get_all():
            try:
                rows.append(s.to_dict())
            except Exception:
                continue
        return rows

    @app.get("/api/i18n/texts")
    def i18n_texts(lang: str = Query("en", max_length=8)):
        if lang not in LANGS:
            return {}
        return live_texts(fleet_rows(), lang)

    @app.post("/api/i18n/texts")
    async def i18n_texts_asked(request: Request, lang: str = Query("en", max_length=8)):
        if lang not in LANGS:
            return {}
        try:
            body = await request.json()
        except Exception:
            body = {}
        texts = body.get("texts") if isinstance(body, dict) else None
        out = live_texts(fleet_rows(), lang)
        out.update(asked_texts(texts, lang))
        return out
