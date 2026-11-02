"""A tiny `.env` reader, so `cp .env.example .env` works without python-dotenv.

main.py calls load() before anything reads the environment. Only keys that are
NOT already in the environment are set: systemd `Environment=`, Docker Compose
`environment:` or a shell `export` always win over the file.

Format: one `KEY=VALUE` per line; an optional `export ` prefix; blank lines and
lines starting with `#` are skipped; a value may be in '…' or "…" quotes (taken
literally, no escapes); an unquoted value ends at ` #` (an inline comment).
"""
import os
import re
from typing import Dict, List, Optional

_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def parse(text: str) -> Dict[str, str]:
    """`.env` text → {key: value}. A malformed line is skipped; a later duplicate wins."""
    out: Dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not _KEY.fullmatch(key):
            continue
        value = value.strip()
        if value[:1] in ("'", '"'):
            end = value.find(value[0], 1)
            value = value[1:end] if end > 0 else value[1:]
        else:
            m = re.search(r"\s#", value)
            if m:
                value = value[:m.start()].rstrip()
        out[key] = value
    return out


def load(path: str, environ: Optional[dict] = None) -> List[str]:
    """Set the keys from `path` that are missing in `environ` (os.environ).

    Returns the names that were set (for one log line; values are never logged).
    A missing or unreadable file sets nothing.
    """
    env = os.environ if environ is None else environ
    try:
        # utf-8-sig: Notepad on Windows saves with a BOM
        with open(path, encoding="utf-8-sig") as f:
            text = f.read()
    except (OSError, UnicodeDecodeError):
        return []
    added = []
    for key, value in parse(text).items():
        if key not in env:
            env[key] = value
            added.append(key)
    return added
