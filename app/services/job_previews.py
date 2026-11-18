"""Immutable, locally stored slicer thumbnails shared by live cards and history."""
import hashlib
import io
import os
from pathlib import Path
import re
import tempfile

from app.services import data_dir

DIRECTORY = Path(data_dir.DATA_DIR).resolve() / "data" / "job_previews"
MAX_BYTES = 2 * 1024 * 1024


def valid_key(key):
    return isinstance(key, str) and re.fullmatch(r"[0-9a-f]{64}", key) is not None


def save(data):
    from PIL import Image
    if not data or len(data) > MAX_BYTES:
        raise ValueError("invalid preview size")
    with Image.open(io.BytesIO(data)) as source:
        if source.format not in ("PNG", "JPEG", "WEBP") or source.width * source.height > 16_000_000:
            raise ValueError("invalid preview image")
        picture = source.convert("RGBA")
        bounds = picture.getchannel("A").getbbox()
        if bounds is None:
            raise ValueError("empty preview")
        picture = picture.crop(bounds)
        picture.thumbnail((640, 640))
        buffer = io.BytesIO()
        picture.save(buffer, format="PNG", optimize=True)
    body = buffer.getvalue()
    key = hashlib.sha256(body).hexdigest()
    DIRECTORY.mkdir(parents=True, exist_ok=True)
    target = DIRECTORY / (key + ".png")
    if not target.exists():
        fd, temporary = tempfile.mkstemp(dir=DIRECTORY, prefix=".preview-")
        try:
            with os.fdopen(fd, "wb") as out:
                out.write(body)
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return key


def save_project(summary):
    """Store a project_summary dict once, under the hash of its content."""
    import json
    body = json.dumps(summary, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")
    if len(body) > 256 * 1024:
        raise ValueError("project summary too large")
    key = hashlib.sha256(body).hexdigest()
    DIRECTORY.mkdir(parents=True, exist_ok=True)
    if (DIRECTORY / (key + ".json")).exists():
        return key
    fd, temporary = tempfile.mkstemp(dir=DIRECTORY, prefix=".project-")
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(body)
        os.replace(temporary, DIRECTORY / (key + ".json"))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return key


ORPHAN_AGE_S = 2 * 86400   # a live job gets its key in print_jobs within minutes of the file
TEMP_PREFIXES = (".preview-", ".project-")   # save() / save_project() temporaries a crash left behind

# project key -> the stored keys its card refers to («pick», the plate map). A card
# never changes (its key is the hash of its content), so each is read once.
_card_refs = {}


def card_refs(key):
    """The stored previews a project card refers to: its top-level values that are keys."""
    refs = _card_refs.get(key)
    if refs is None:
        import json
        try:
            card = json.loads((DIRECTORY / (key + ".json")).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return frozenset()      # not stored (yet), or unreadable: asked again next time
        values = card.values() if isinstance(card, dict) else ()
        refs = _card_refs[key] = frozenset(v for v in values if valid_key(v))
    return refs


def prune(referenced, now):
    """Delete stored previews/projects no print job refers to (older than ORPHAN_AGE_S).

    A kept project card keeps what it refers to (the plate map of «Пропустить
    деталь»); a temporary file of a write that never finished goes too.
    """
    removed = 0
    if not DIRECTORY.is_dir():
        return removed
    paths = list(DIRECTORY.iterdir())
    names = {path.name for path in paths}
    keep = set(referenced)
    for key in list(keep):
        if valid_key(key) and key + ".json" in names:
            keep.update(card_refs(key))
    for path in paths:
        key, _, extension = path.name.partition(".")
        temporary = path.name.startswith(TEMP_PREFIXES)
        if not temporary and (extension not in ("png", "json") or not valid_key(key) or key in keep):
            continue
        try:
            if now - path.stat().st_mtime > ORPHAN_AGE_S and path.is_file():
                path.unlink()
                removed += 1
                _card_refs.pop(key, None)
        except OSError:
            pass
    return removed
