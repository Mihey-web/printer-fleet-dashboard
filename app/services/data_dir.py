"""Where the panel keeps its databases and data folders: one setting, FORGE_DATA_DIR.

The folder is FORGE_DATA_DIR from the environment (or `.env`), else DATA_DIR in
config.py, else the project root (the folder with main.py), where everything has
always lived. A relative value is taken from the project root. The folder holds the
three databases (printer_history.db, users.db, ams_history.db) and data/ (job
previews and cards, the shared file pool, Bambu's HMS tables), laid out as in the
project root.

Read once, at import: main.py loads `.env` before any module imports this one. The
modules keep their own constants (state_store.DB_PATH, auth.USERS_DB,
job_previews.DIRECTORY, ...), set from the values here, because tests and other
modules point those at other files.
"""
import os

# The project root, computed exactly as every module computed it before this setting:
# with FORGE_DATA_DIR unset each path below is byte for byte the old one.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))


def resolve() -> str:
    """The data folder: env FORGE_DATA_DIR, else config.DATA_DIR, else the project root."""
    raw = os.environ.get("FORGE_DATA_DIR")
    if not (raw or "").strip():
        try:
            import config
            raw = getattr(config, "DATA_DIR", None)
        except ImportError:
            raw = None
    raw = str(raw).strip() if raw is not None else ""
    if not raw:
        return PROJECT_ROOT
    return os.path.join(PROJECT_ROOT, os.path.expanduser(raw))


DATA_DIR = resolve()


def path(*parts: str) -> str:
    """A file or folder inside the data folder."""
    return os.path.join(DATA_DIR, *parts)


HISTORY_DB = path("printer_history.db")   # fleet, settings, state history, print journal
USERS_DB = path("users.db")               # accounts, sessions, audit log
AMS_DB = path("ams_history.db")           # AMS humidity / temperature history
JOB_PREVIEWS_DIR = path("data", "job_previews")
FILE_POOL_DIR = path("data", "file_pool")
HMS_DIR = path("data", "hms")
