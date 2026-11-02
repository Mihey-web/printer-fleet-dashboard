"""Collect every Cyrillic string the panel can show (i18n coverage, docs/dev/I18N_REPORT.md).

The panel is translated on the client, keyed by the Russian string (app/web/i18n.js).
This script finds the Russian strings by parsing the sources, not by eye:

* app/web/app.js — every string literal and every static chunk of a template
  literal (comments and regex literals skipped);
* app/web/i18n.js — the same (the switcher's label);
* app/web/pwa/manifest.webmanifest — the names (served translated, app/api/i18n.py);
* app/web/index.html, app/web/login.html — text nodes and the attributes the
  translator handles (title, placeholder, aria-label, alt, data-tip, content of
  <title>);
* the server modules whose texts reach the panel (SERVER_FILES) — every string
  constant except docstrings and arguments of logger.* / logging.* calls.

A literal is cut into *pieces*: HTML tags are removed, the attribute values are
pieces of their own, entities and JS escapes are decoded, every piece is trimmed.
A piece with Cyrillic is what the dictionary (or a pattern, or an exclusion)
has to cover.

    python scripts/i18n_extract.py             # every piece, one per line (JSON)
    python scripts/i18n_extract.py --missing [lang]   # pieces app/web/i18n/<lang>.js (en) does not cover
"""
import ast
import html
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "app" / "web"

CYR = re.compile(r"[А-Яа-яЁё]")
ATTRS = ("title", "placeholder", "aria-label", "alt", "data-tip", "data-l", "content", "label")

# Server modules whose strings reach the panel's screen (API errors, stage and state
# labels, notification texts and hints, journal reasons, command answers) or the bot.
SERVER_FILES = [
    "app/api/main.py",
    "app/api/admin.py",
    "app/api/auth.py",
    "app/services/normalizer.py",
    "app/services/printer_commands.py",
    "app/services/hms_catalog.py",
    "app/services/notifications.py",
    "app/services/notification_actions.py",
    "app/services/notification_mutes.py",
    "app/services/fleet_commands.py",
    "app/services/ams_drying.py",
    "app/services/ams_autodry.py",
    "app/services/ams_autodry_engine.py",
    "app/services/ams_store.py",
    "app/services/auto_ignore.py",
    "app/services/job_journal.py",
    "app/services/job_queries.py",
    "app/services/settings_service.py",
    "app/services/printer_registry.py",
    "app/services/bambu_extras.py",
    "app/services/audit_service.py",
    # the Telegram bot's texts (its language: settings «telegram_lang», app/services/i18n.py)
    "main.py",
    "app/services/telegram_bot_async.py",
]


# ---- JavaScript ---------------------------------------------------------------------------

_REGEX_PREV = set("(,=:[!&|?{};+-*%<>~^") | {""}
_REGEX_WORDS = {"return", "typeof", "case", "do", "else", "in", "of", "new", "delete", "void", "throw"}
_JS_ESC = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}


def _js_unescape(s):
    out, i = [], 0
    while i < len(s):
        ch = s[i]
        if ch != "\\" or i + 1 >= len(s):
            out.append(ch)
            i += 1
            continue
        nx = s[i + 1]
        if nx == "u":
            if s[i + 2:i + 3] == "{":
                j = s.index("}", i)
                out.append(chr(int(s[i + 3:j], 16)))
                i = j + 1
            else:
                out.append(chr(int(s[i + 2:i + 6], 16)))
                i += 6
            continue
        if nx == "x":
            out.append(chr(int(s[i + 2:i + 4], 16)))
            i += 4
            continue
        if nx == "\n":
            i += 2
            continue
        out.append(_JS_ESC.get(nx, nx))
        i += 2
    return "".join(out)


def js_strings(src):
    """[(line, text)] of every string literal and template chunk of a JS source."""
    out = []
    i, n = 0, len(src)
    prev = ""            # the last significant token (for the regex / division decision)

    def line_of(pos):
        return src.count("\n", 0, pos) + 1

    def read_template(i):
        # src[i] is the char after the opening backtick; returns the index after the closing one
        start = i
        buf = []
        while i < n:
            ch = src[i]
            if ch == "\\":
                buf.append(src[i:i + 2])
                i += 2
                continue
            if ch == "`":
                out.append((line_of(start), _js_unescape("".join(buf))))
                return i + 1
            if ch == "$" and src[i + 1:i + 2] == "{":
                out.append((line_of(start), _js_unescape("".join(buf))))
                buf = []
                i = read_code(i + 2, "}")
                start = i
                continue
            buf.append(ch)
            i += 1
        raise ValueError("unterminated template")

    def read_code(i, until=None):
        nonlocal prev
        depth = 0
        while i < n:
            ch = src[i]
            if ch in " \t\r\n":
                i += 1
                continue
            if src.startswith("//", i):
                j = src.find("\n", i)
                i = n if j < 0 else j
                continue
            if src.startswith("/*", i):
                i = src.index("*/", i + 2) + 2
                continue
            if ch in "'\"":
                j, buf = i + 1, []
                while src[j] != ch:
                    if src[j] == "\\":
                        buf.append(src[j:j + 2])
                        j += 2
                    else:
                        buf.append(src[j])
                        j += 1
                out.append((line_of(i), _js_unescape("".join(buf))))
                i = j + 1
                prev = "str"
                continue
            if ch == "`":
                i = read_template(i + 1)
                prev = "str"
                continue
            if ch == "/" and (prev in _REGEX_PREV or prev in _REGEX_WORDS):
                j, cls = i + 1, False
                while True:
                    c = src[j]
                    if c == "\\":
                        j += 2
                        continue
                    if c == "[":
                        cls = True
                    elif c == "]":
                        cls = False
                    elif c == "/" and not cls:
                        break
                    elif c == "\n":
                        raise ValueError("bad regex at line %d" % line_of(i))
                    j += 1
                j += 1
                while j < n and (src[j].isalnum()):
                    j += 1
                i = j
                prev = "re"
                continue
            if until and ch == "{":
                depth += 1
            if until and ch == "}":
                if depth == 0:
                    prev = "}"
                    return i + 1
                depth -= 1
            m = re.match(r"[A-Za-z_$][\w$]*|\d[\w.]*", src[i:i + 64])
            if m:
                prev = m.group(0) if m.group(0) in _REGEX_WORDS else "id"
                i += len(m.group(0))
                continue
            prev = ")" if ch in ")]" else ch
            i += 1
        return i

    read_code(0)
    return out


# ---- pieces -------------------------------------------------------------------------------

_ATTR_RE = re.compile(r"\b(?:%s)=(\"([^\"]*)\"|'([^']*)')" % "|".join(re.escape(a) for a in ATTRS))
_TAG_RE = re.compile(r"<[^<>]*>")


def pieces(text):
    """The visible parts of one literal: attribute values and the text between tags, trimmed,
    inner whitespace collapsed (the translator looks strings up the same way)."""
    found = []
    attr_names = "|".join(re.escape(a) for a in ATTRS)
    for m in _ATTR_RE.finditer(text):
        found.append(m.group(2) if m.group(2) is not None else m.group(3))
    rest = _ATTR_RE.sub(" ", text)
    # an attribute opened at the end of the literal: '… data-tip="Загрузить: ' + name + '">'
    m = re.search(r"\b(?:%s)=[\"']([^\"']*)$" % attr_names, rest)
    if m:
        found.append(m.group(1))
        rest = rest[:m.start()]
    # the literal starts inside an attribute value: '— открыть список" aria-label="…'
    m = re.match(r"^([^\"<>]*)[\"'](?=\s*(?:/?>|[\w-]+=|$))", rest)
    if m:
        found.append(m.group(1))
        rest = rest[m.end():]
    rest = _TAG_RE.sub("\n", rest)
    # a tag cut by the concatenation: its head at the end, its tail at the start
    rest = re.sub(r"<[a-zA-Z/][^<>]*$", "\n", rest)
    m = re.match(r"^[^<>]*>", rest)
    if m and not CYR.search(m.group(0)):
        rest = rest[m.end():]
    found.extend(rest.split("\n"))
    out = []
    for p in found:
        p = norm(html.unescape(p))
        if CYR.search(p):
            out.append(p)
    return out


def norm(s):
    """The lookup form of a string: no-break spaces as spaces, inner runs of whitespace as one, trimmed."""
    return re.sub(r"\s+", " ", s.replace("\u00a0", " ")).strip()


# ---- HTML ---------------------------------------------------------------------------------

def html_pieces(src):
    """[(line, piece)] of an HTML file: text, the handled attributes; <script>/<style> skipped
    (login.html's inline script is read as JS)."""
    from html.parser import HTMLParser

    res = []

    class P(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.skip = 0

        def handle_starttag(self, tag, attrs):
            if tag in ("script", "style"):
                self.skip += 1
            for k, v in attrs:
                if k in ATTRS and v and CYR.search(v):
                    res.append((self.getpos()[0], norm(v)))

        def handle_endtag(self, tag):
            if tag in ("script", "style"):
                self.skip -= 1

        def handle_data(self, data):
            if self.skip:
                return
            for part in data.split("\n"):
                part = part.replace(" ", " ").strip()
                if CYR.search(part):
                    res.append((self.getpos()[0], part))

    P().feed(re.sub(r"<!--.*?-->", "", src, flags=re.S))
    for m in re.finditer(r"<script>(.*?)</script>", src, re.S):
        base = src.count("\n", 0, m.start())
        for line, s in js_strings(m.group(1)):
            for p in pieces(s):
                res.append((base + line, p))
    return res


# ---- Python -------------------------------------------------------------------------------

def py_strings(src):
    """[(line, text)] of the string constants of a module, without docstrings and log calls."""
    tree = ast.parse(src)
    skip = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                skip.add(id(body[0].value))
        if isinstance(node, ast.Call):
            f = node.func
            name = ""
            if isinstance(f, ast.Attribute):
                base = f.value
                name = (base.id if isinstance(base, ast.Name) else getattr(base, "attr", "")) + "." + f.attr
            if name.split(".")[0] in ("logger", "logging", "log", "_log", "LOG") or name.endswith(".debug"):
                for sub in ast.walk(node):
                    skip.add(id(sub))
    out = []
    for node in ast.walk(tree):
        if id(node) in skip:
            continue
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.append((node.lineno, node.value))
    return out


def _py_format_parts(s):
    # "%s", "%d", "{label}" placeholders split the text (the client sees the filled-in string)
    return re.split(r"%(?:\([^)]*\))?[-+ #0]*\d*(?:\.\d+)?[sdifrxX]|\{[^{}]*\}", s)


# ---- all ----------------------------------------------------------------------------------

def collect():
    """[{file, line, text}] — every Cyrillic piece, in source order (duplicates kept)."""
    rows = []
    for name in ("app.js", "i18n.js"):
        for line, s in js_strings((WEB / name).read_text(encoding="utf-8")):
            for p in pieces(s):
                rows.append({"file": "app/web/" + name, "line": line, "text": p})
    for name in ("index.html", "login.html"):
        for line, p in html_pieces((WEB / name).read_text(encoding="utf-8")):
            rows.append({"file": "app/web/" + name, "line": line, "text": p})
    man = json.loads((WEB / "pwa" / "manifest.webmanifest").read_text(encoding="utf-8"))
    for obj in [man] + list(man.get("shortcuts") or []):
        for key in ("name", "short_name", "description"):
            if isinstance(obj.get(key), str) and CYR.search(obj[key]):
                rows.append({"file": "app/web/pwa/manifest.webmanifest", "line": 0, "text": norm(obj[key])})
    for rel in SERVER_FILES:
        for line, s in py_strings((ROOT / rel).read_text(encoding="utf-8")):
            for part in _py_format_parts(s):
                part = part.replace("%%", "%")
                for p in pieces(part):
                    rows.append({"file": rel, "line": line, "text": p})
    return rows


def load_dictionary(lang="en"):
    """{"dict": {ru: translation}, "patterns": [[source, flags, replacement]], "exclude": {ru: reason}}
    of app/web/i18n/<lang>.js, read by node (the file is plain JS)."""
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        raise RuntimeError("node is not installed")
    js = """
var W = {}; W.window = W;
var I18N = { register: function (lang, d) { W.d = d; } };
%s
var d = W.d;
console.log(JSON.stringify({
  dict: d.strings,
  patterns: (d.patterns || []).map(function (p) { return [p[0].source, p[0].flags, typeof p[1] === "string" ? p[1] : null]; }),
  exclude: d.untranslated || {}
}));
""" % (WEB / "i18n" / (lang + ".js")).read_text(encoding="utf-8")
    out = subprocess.run([node, "-"], input=js, capture_output=True, text=True, encoding="utf-8", timeout=60)
    if out.returncode:
        raise RuntimeError(out.stderr)
    return json.loads(out.stdout)


def uncovered(rows=None, dic=None):
    """The pieces neither the dictionary, a pattern nor the exclusions cover."""
    rows = collect() if rows is None else rows
    dic = load_dictionary() if dic is None else dic
    pats = [re.compile(_py_regex(src), _py_flags(fl)) for src, fl, _ in dic["patterns"]]
    miss = []
    for r in rows:
        t = r["text"]
        if t in dic["dict"] or t in dic["exclude"]:
            continue
        if any(p.search(t) for p in pats):
            continue
        miss.append(r)
    return miss


def _py_regex(src):
    # the patterns are written in the common subset of JS and Python regexes; JS \d etc. agree
    return src.replace("(?<", "(?P<") if "(?<" in src and "(?<=" not in src and "(?<!" not in src else src


def _py_flags(flags):
    f = 0
    if "i" in flags:
        f |= re.I
    if "u" in flags:
        f |= re.U
    return f


if __name__ == "__main__":
    if "--missing" in sys.argv:
        seen = set()
        rest = [a for a in sys.argv[1:] if not a.startswith("--")]
        for r in uncovered(dic=load_dictionary(rest[0] if rest else "en")):
            if r["text"] not in seen:
                seen.add(r["text"])
                print(json.dumps(r, ensure_ascii=False))
    else:
        rows = collect()
        uniq = {}
        for r in rows:
            uniq.setdefault(r["text"], r)
        for r in uniq.values():
            print(json.dumps(r, ensure_ascii=False))
        print("pieces: %d, unique: %d" % (len(rows), len(uniq)), file=sys.stderr)
