"""A shared pool of print files per printer model, and copying the missing ones by a button.

A model's pool is every print file on the printers of that model plus the files an admin
uploaded to the panel (data/file_pool/<group>/). Same file = same name and size.
«Синхронизировать» first shows a plan (which files go to which printers, what is skipped
and why), then copies, one sync at a time, with progress per printer.

What the printers allow (live 16.11.2026):
* reading a file: X1C (SD card over the file channel, internal history maybe), P1S (FTPS);
  P2S refuses (result 2), H2S without a USB stick lists nothing, Creality has no download —
  except a Creality with Klipper's own API (Moonraker :7125, the K2 Plus): every file of its
  gcodes root (app/clients/moonraker.py).
* writing a file: Bambu X1 / P1 / A1 to the SD card over FTPS (as Bambu Studio does);
  Creality K1C / Ender-5 Max: POST /upload/<name> (the printer's own web page);
  K2 Plus: Moonraker's /server/files/upload into the gcodes root;
  P2S / H2S internal storage: no network upload.
Never deletes or overwrites a file on a printer; never copies across models.
"""
import hashlib
import http.client
import logging
import os
import re
import shutil
import tempfile
import threading
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.services import bambu_models, creality_models, data_dir, printer_files, project_summary

logger = logging.getLogger(__name__)


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    """A whole number: `name` in the environment, else config.<name without FORGE_>, else `default`
    (a value that is not a whole number >= minimum falls back to it, with a warning)."""
    raw = os.environ.get(name)
    if raw is None:
        try:
            import config
            raw = getattr(config, name[len("FORGE_"):] if name.startswith("FORGE_") else name, default)
        except ImportError:
            raw = default
    try:
        value = int(str(raw).strip())
    except ValueError:
        value = None
    if value is None or value < minimum:
        logger.warning("[pool] %s=%r is not a whole number >= %d, using %d", name, raw, minimum, default)
        return default
    return value


DIRECTORY = Path(data_dir.DATA_DIR).resolve() / "data" / "file_pool"
POOL_MAX_GB = _env_int("FORGE_FILE_POOL_MAX_GB", 8)                       # every group's uploads together
FILE_MAX_MB = _env_int("FORGE_FILE_POOL_FILE_MAX_MB", 512)                # one uploaded file
KEEP_FREE_MB = _env_int("FORGE_FILE_POOL_KEEP_FREE_MB", 1024, minimum=0)  # left free on the disk the databases share
MAX_FILE_BYTES = FILE_MAX_MB * 1024 * 1024
MAX_POOL_BYTES = POOL_MAX_GB * 1024 * 1024 * 1024
KEEP_FREE_BYTES = KEEP_FREE_MB * 1024 * 1024
STALE_UPLOAD_S = 3600       # an .upload-* this old and not in flight was left by a crash
PLAN_TTL_S = 600
CHUNK = 65536
CREALITY_WEB_PORT = 80              # the printer's own web page: POST /upload/<name> (K1C / Ender-5 Max)
CREALITY_UPLOAD_TIMEOUT_S = 60.0    # per socket operation, as moonraker.TRANSFER_TIMEOUT_S

_GROUP = re.compile(r"(bambu|creality)-[A-Za-z0-9_]{1,24}")
_BAD_NAME = re.compile(r'[\x00-\x1f/\\:*?"<>|]')

MSG_BAD_GROUP = "Нет такой группы принтеров"
MSG_BAD_NAME = "Недопустимое имя файла"
MSG_BAD_TYPE_BAMBU = "Для Bambu нужен нарезанный файл .gcode.3mf"
MSG_BAD_TYPE_CREALITY = "Для Creality нужен файл .gcode"
MSG_TOO_BIG = "Файл больше %d МБ" % FILE_MAX_MB
MSG_POOL_FULL = "В общем хранилище нет места (%d ГБ) — удалите ненужные файлы" % POOL_MAX_GB
MSG_DISK_FULL = "На диске панели мало свободного места — удалите ненужные файлы"
MSG_EXISTS = "Файл с таким именем уже есть в хранилище"
MSG_NO_FILE = "Такого файла нет в хранилище"
MSG_PLAN_STALE = "План устарел — откройте его заново"
MSG_PLAN_OTHER_GROUP = "План составлен для другой группы принтеров — откройте его заново"
MSG_SYNC_RUNNING = "Синхронизация уже идёт — дождитесь окончания"
WHY_OFFLINE = "не на связи"
WHY_BUSY = "занят (печатает или готовится)"
WHY_NO_STICK = "нужна флешка в принтере — во внутреннюю память файлы по сети не пишутся"
WHY_NO_CARD = "нет SD-карты в принтере"
WHY_INTERNAL = "лежит только во внутренней памяти — прошивка её не отдаёт; нужна копия на флешке или в панели"
WHY_CREALITY = "Creality не отдаёт файлы по сети; загрузите файл в панель"
WHY_NOT_LISTED = "не прислал список файлов"
WHY_NO_UPLOAD = "эта модель не принимает файлы по сети"
WHY_NO_SOURCE = "нет принтера, с которого его можно прочитать"
WHY_NAME_TAKEN = "на принтере уже есть другой файл с таким именем"
WHY_SYNC_BROKEN = "синхронизация прервалась из-за ошибки панели — запустите её снова"

_lock = threading.Lock()
_plans: Dict[str, Dict[str, Any]] = {}
_job: Dict[str, Any] = {}
_room_lock = threading.Lock()
_writing: Dict[str, int] = {}     # uploads in flight: temporary path -> bytes taken so far
_stored = 0                       # the pool's bytes on disk (not in flight), counted at the last upload's start


# --- groups -------------------------------------------------------------------------------

def group_of(prn: Dict[str, Any]) -> Optional[str]:
    kind, model = prn.get("kind"), re.sub(r"[^A-Za-z0-9_]", "", str(prn.get("model") or ""))
    if kind not in ("bambu", "creality") or not model:
        return None
    return "%s-%s" % (kind, model.upper() if kind == "bambu" else model.lower())


def group_title(group: str) -> str:
    kind, _, model = group.partition("-")
    if kind == "bambu":
        return "Bambu " + model
    return "Creality " + (creality_models.display_name(model) or model)     # as on the printer's card


def _model(prn: Dict[str, Any]) -> str:
    return str(prn.get("model") or "").upper()


def can_upload(prn: Dict[str, Any]) -> bool:
    """The model can take files at all: Creality, Bambu on its SD card / USB stick (see storage_refusal)."""
    return prn.get("kind") in ("creality", "bambu")


def can_read(prn: Dict[str, Any]) -> bool:
    """Every file of this printer can be read: X1 / P1 / A1, a Creality with Klipper's API (the cached
    answer: never a probe here); P2 / H2 only from a stick (entry_readable)."""
    if prn.get("kind") == "creality":
        return printer_files.has_klipper_api(prn, wait=False)
    return prn.get("kind") == "bambu" and bambu_models.has(_model(prn), "reads_every_file")


def entry_readable(prn: Dict[str, Any], entry: Dict[str, Any]) -> bool:
    """X1C: SD card and internal history over the file channel; P1: FTPS; P2 / H2: only the USB
    stick over FTPS — their internal storage refuses downloads (result 2), Creality gives out only
    what its Moonraker listed (mr)."""
    if prn.get("kind") == "creality":
        return bool(entry.get("mr"))
    return can_read(prn) or (prn.get("kind") == "bambu" and entry.get("via") == "ftps")


def storage_refusal(prn: Dict[str, Any]) -> Optional[str]:
    """Why a Bambu cannot take a file now: no SD card / USB stick (home_flag), else None.
    Live 16.11: without a stick a P2S / H2S takes FTP logins but STOR → 553; with one, STOR works."""
    if prn.get("kind") != "bambu":
        return None
    collector = printer_files._collector(prn["id"])
    live = getattr(collector, "live_device", None)
    device = live() if callable(live) else None
    if device is None:
        return WHY_OFFLINE
    try:
        state = device.home_flag.sdcard_status
    except Exception:
        state = None
    if state == "normal":
        return None
    return WHY_NO_STICK if printer_files._usb_over_ftps(_model(prn)) else WHY_NO_CARD


def _printers() -> List[Dict[str, Any]]:
    from app.services import printer_registry
    return [p for p in printer_registry.list_printers(include_deleted=False) if group_of(p)]


def groups() -> List[Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for p in _printers():
        g = group_of(p)
        entry = out.setdefault(g, {"key": g, "title": group_title(g), "printers": [],
                                   "kind": p["kind"], "pi_files": len(pi_files(g))})
        entry["printers"].append({"id": p["id"], "label": p.get("label") or p["id"],
                                  "upload": can_upload(p), "read": can_read(p)})
    for g in out.values():
        g["printers"].sort(key=lambda x: x["label"])
        g["syncable"] = any(x["upload"] for x in g["printers"])
    return sorted(out.values(), key=lambda g: g["title"])


def _members(group: str) -> List[Dict[str, Any]]:
    if not _GROUP.fullmatch(group or ""):
        raise LookupError(MSG_BAD_GROUP)
    members = [p for p in _printers() if group_of(p) == group]
    if not members:
        raise LookupError(MSG_BAD_GROUP)
    return sorted(members, key=lambda p: p.get("label") or p["id"])


# --- the panel's own storage (browser uploads) ----------------------------------------------

def _dir(group: str) -> Path:
    return DIRECTORY / group


def check_name(group: str, name: Any) -> str:
    if not isinstance(name, str) or not name.strip() or len(name) > 200 or _BAD_NAME.search(name) \
            or name.startswith(".") or name != name.strip():
        raise ValueError(MSG_BAD_NAME)
    lower = name.lower()
    if group.startswith("bambu-") and not lower.endswith(".gcode.3mf"):
        raise ValueError(MSG_BAD_TYPE_BAMBU)
    if group.startswith("creality-") and not lower.endswith(".gcode"):
        raise ValueError(MSG_BAD_TYPE_CREALITY)
    return name


def pi_files(group: str) -> List[Dict[str, Any]]:
    folder = _dir(group)
    if not folder.is_dir():
        return []
    out = []
    for path in folder.iterdir():
        if path.is_file() and not path.name.startswith("."):
            st = path.stat()
            out.append({"name": path.name, "size": st.st_size, "mtime": int(st.st_mtime)})
    return sorted(out, key=lambda f: -f["mtime"])


def _pool_bytes() -> int:
    """The pool's files on disk (caller holds _room_lock). An upload in flight counts in _writing
    instead (its temporary file, by name); one a crash left STALE_UPLOAD_S ago is removed."""
    if not DIRECTORY.is_dir():
        return 0
    flying = {os.path.basename(t) for t in _writing}
    total = 0
    for p in DIRECTORY.glob("*/*"):
        if p.name in flying:
            continue
        try:
            if not p.is_file():
                continue
            st = p.stat()
            if p.name.startswith(".upload-") and time.time() - st.st_mtime > STALE_UPLOAD_S:
                p.unlink()
                continue
        except OSError:
            continue
        total += st.st_size
    return total


def _check_room(size: int, n: int) -> None:
    """n more bytes of an upload `size` long (caller holds _room_lock): the file's limit, the pool's —
    every upload in flight counts, so parallel ones cannot pass it together — and the disk's reserve."""
    if size + n > MAX_FILE_BYTES:
        raise ValueError(MSG_TOO_BIG)
    if _stored + sum(_writing.values()) + n > MAX_POOL_BYTES:
        raise ValueError(MSG_POOL_FULL)
    if shutil.disk_usage(DIRECTORY).free - n < KEEP_FREE_BYTES:
        raise ValueError(MSG_DISK_FULL)


def _begin_upload(group: str, name: str):
    """(target, temporary path, open file): every check that needs no bytes, before any is read."""
    global _stored
    _members(group)
    check_name(group, name)
    folder = _dir(group)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / name
    if target.exists():
        raise ValueError(MSG_EXISTS)
    with _room_lock:
        _stored = _pool_bytes()
        _check_room(0, 1)                  # not even one byte fits: say so before reading the body
        fd, temporary = tempfile.mkstemp(dir=folder, prefix=".upload-")
        _writing[temporary] = 0
    return target, temporary, os.fdopen(fd, "wb")


def _take(temporary: str, n: int) -> None:
    """Count n bytes of this upload before they are written."""
    with _room_lock:
        _check_room(_writing[temporary], n)
        _writing[temporary] += n


def _finish_upload(target: Path, temporary: str, size: int) -> Dict[str, Any]:
    global _stored
    if size == 0:
        raise ValueError(MSG_BAD_NAME)
    with _room_lock:
        if target.exists():
            raise ValueError(MSG_EXISTS)
        os.replace(temporary, target)
        _stored += _writing.pop(temporary, size)
    return {"name": target.name, "size": size}


def _end_upload(temporary: str) -> None:
    """Finished or not: the temporary file goes and stops counting."""
    with _room_lock:
        _writing.pop(temporary, None)
    if os.path.exists(temporary):
        os.unlink(temporary)


def store_upload(group: str, name: str, chunks) -> Dict[str, Any]:
    """Write an uploaded file into the group's storage (bounded); `chunks` = iterable of bytes."""
    target, temporary, out = _begin_upload(group, name)
    size = 0
    try:
        with out:
            for chunk in chunks:
                _take(temporary, len(chunk))
                size += len(chunk)
                out.write(chunk)
        return _finish_upload(target, temporary, size)
    finally:
        _end_upload(temporary)


async def store_upload_async(group: str, name: str, stream) -> Dict[str, Any]:
    """The same from Starlette's request.stream(); the checks and disk writes run in a worker thread."""
    import asyncio
    loop = asyncio.get_running_loop()
    target, temporary, out = await loop.run_in_executor(None, _begin_upload, group, name)

    def write(chunk: bytes) -> None:
        _take(temporary, len(chunk))
        out.write(chunk)
    size = 0
    try:
        try:
            async for chunk in stream:
                if chunk:
                    await loop.run_in_executor(None, write, chunk)
                    size += len(chunk)
        finally:
            out.close()
        return _finish_upload(target, temporary, size)
    finally:
        _end_upload(temporary)


def delete_pi_file(group: str, name: str) -> None:
    _members(group)
    if not isinstance(name, str) or _BAD_NAME.search(name) or name.startswith("."):
        raise ValueError(MSG_BAD_NAME)
    path = _dir(group) / name
    if not path.is_file():
        raise LookupError(MSG_NO_FILE)
    path.unlink()


# --- the pool -------------------------------------------------------------------------------

def file_id(name: str, size: Optional[int]) -> str:
    return hashlib.sha1(("%s\x00%s" % (name, size)).encode("utf-8")).hexdigest()[:16]


def _listing(prn: Dict[str, Any]):
    """(entries, why-not) of one printer: live listing, demos left out."""
    try:
        listing = printer_files.list_files(prn["id"])
    except LookupError as e:
        if str(e) == printer_files.MSG_STORAGE_CLOSED and storage_refusal(prn) is None:
            return [], None     # an H2S with an empty stick: its memory is closed, the stick listed nothing
        known = (printer_files.MSG_OFFLINE, printer_files.MSG_STORAGE_CLOSED)
        return None, str(e) if str(e) in known else WHY_NOT_LISTED
    if listing.get("warning"):
        return None, WHY_NOT_LISTED     # the stick was not listed: a file of the same name may be on it
    return listing["files"], None    # factory samples are in only when the panel shows them (hide_sample_files off)


def build_pool(group: str) -> Dict[str, Any]:
    """Every file of the group: where it is, whether it can be read from somewhere."""
    members = _members(group)
    files: Dict[str, Dict[str, Any]] = {}
    printers = []
    for prn in members:
        entries, why = _listing(prn)
        printers.append({"id": prn["id"], "label": prn.get("label") or prn["id"], "listed": entries is not None,
                         "why": why, "upload": can_upload(prn), "read": can_read(prn)})
        for e in entries or []:
            key = file_id(e["name"], e.get("size"))
            f = files.setdefault(key, {"id": key, "name": e["name"], "title": e.get("title") or e["name"],
                                       "size": e.get("size"), "mtime": e.get("mtime"), "on": [], "sources": []})
            if prn["id"] not in f["on"]:
                f["on"].append(prn["id"])
            if entry_readable(prn, e) and not any(x.get("printer") == prn["id"] for x in f["sources"]):
                src = {"printer": prn["id"], "path": e["path"], "name": e["name"], "via": e.get("via")}
                if e.get("mr"):
                    src["mr"] = e["mr"]             # Klipper API: its path in the gcodes root
                f["sources"].append(src)
            elif not entry_readable(prn, e):
                f["why"] = WHY_CREALITY if prn["kind"] == "creality" else WHY_INTERNAL
            f["mtime"] = max(f["mtime"] or 0, e.get("mtime") or 0) or None
            if e.get("demo"):
                f["sample"] = True
    for pi in pi_files(group):
        key = file_id(pi["name"], pi["size"])
        f = files.setdefault(key, {"id": key, "name": pi["name"], "title": project_summary.short_name(pi["name"]) or pi["name"],
                                   "size": pi["size"], "mtime": pi["mtime"], "on": [], "sources": []})
        f["pi"] = True
        f["sources"].insert(0, {"pi": True, "name": pi["name"]})
    for f in files.values():
        if f["sources"]:
            f.pop("why", None)
    out = sorted(files.values(), key=lambda f: -(f["mtime"] or 0))
    return {"group": group, "title": group_title(group), "printers": printers, "files": out}


def _target_refusal(prn: Dict[str, Any]) -> Optional[str]:
    from app.services import printer_commands as pc
    if prn["kind"] == "bambu":
        view = pc._live_view(prn["id"], prn)
        if view is None:
            return WHY_OFFLINE
        return WHY_BUSY if printer_files.bambu_start_refusal(view) else None
    live = pc._ws_live(prn["id"], prn)
    if live is None:
        return WHY_OFFLINE
    return WHY_BUSY if printer_files.creality_start_refusal(live) else None


def make_plan(group: str, only: Optional[List[str]] = None) -> Dict[str, Any]:
    """`only`: the pool ids the admin ticked (None = every file of the pool)."""
    pool = build_pool(group)
    members = {p["id"]: p for p in _members(group)}
    names_on: Dict[str, Dict[str, Optional[int]]] = {p["id"]: {} for p in pool["printers"]}
    for f in pool["files"]:          # every file a printer holds, ticked or not: never overwritten
        for pid in f["on"]:
            names_on[pid][f["name"]] = f["size"]
    if only is not None:
        wanted = set(only)
        pool["files"] = [f for f in pool["files"] if f["id"] in wanted]
    targets, skipped = [], []
    for p in pool["printers"]:
        prn = members[p["id"]]
        if not p["upload"]:
            skipped.append({"printer": p["id"], "label": p["label"], "why": WHY_NO_UPLOAD})
            continue
        if not p["listed"]:
            why = p["why"] or WHY_NOT_LISTED
            if why == printer_files.MSG_STORAGE_CLOSED:
                why = WHY_NO_STICK           # an H2S without a stick: the stick is the way in
            skipped.append({"printer": p["id"], "label": p["label"], "why": why})
            continue
        why = _target_refusal(prn) or storage_refusal(prn)
        if why:
            skipped.append({"printer": p["id"], "label": p["label"], "why": why})
            continue
        # a Bambu's SD card / USB stick (FAT32, exFAT) does not tell Part from part; a Creality's Linux does
        fold = str.lower if prn["kind"] == "bambu" else str
        todo, conflicts = [], []
        taken = {fold(n): size for n, size in names_on[p["id"]].items()}
        for f in pool["files"]:
            if p["id"] in f["on"] or not f["sources"] or f.get("sample"):   # never a factory sample
                continue
            if fold(f["name"]) in taken:
                conflicts.append(f["name"])          # never overwrite a file of that name, nor send two
                continue
            taken[fold(f["name"])] = f["size"]
            todo.append(f["id"])
        if todo or conflicts:
            targets.append({"printer": p["id"], "label": p["label"], "files": todo, "conflicts": conflicts,
                            "bytes": sum(next(x["size"] or 0 for x in pool["files"] if x["id"] == i) for i in todo)})
    unavailable = [{"name": f["name"], "why": f.get("why") or WHY_NO_SOURCE} for f in pool["files"]
                   if not f["sources"] and not f.get("sample") and len(f["on"]) < len(pool["printers"])]
    plan = {"id": uuid.uuid4().hex, "group": group, "title": pool["title"], "at": time.time(),
            "files": {f["id"]: f for f in pool["files"]}, "targets": [t for t in targets if t["files"] or t["conflicts"]],
            "skipped": skipped, "unavailable": unavailable}
    plan["total_bytes"] = sum(t["bytes"] for t in plan["targets"])
    plan["copies"] = sum(len(t["files"]) for t in plan["targets"])
    with _lock:
        for key in [k for k, v in _plans.items() if time.time() - v["at"] > PLAN_TTL_S]:
            del _plans[key]
        _plans[plan["id"]] = plan
    return public_plan(plan)


def public_plan(plan: Dict[str, Any]) -> Dict[str, Any]:
    files = plan["files"]
    return {"id": plan["id"], "group": plan["group"], "title": plan["title"], "total_bytes": plan["total_bytes"],
            "copies": plan["copies"], "skipped": plan["skipped"], "unavailable": plan["unavailable"],
            "targets": [{"printer": t["printer"], "label": t["label"], "bytes": t["bytes"], "conflicts": t["conflicts"],
                         "files": [{"name": files[i]["name"], "title": files[i]["title"], "size": files[i]["size"]}
                                   for i in t["files"]]} for t in plan["targets"]],
            "free_space": None}   # neither Bambu FTP nor Creality report it


# --- copying --------------------------------------------------------------------------------

def _fetch(group: str, f: Dict[str, Any], workdir: str) -> str:
    """A local copy of the pool file: the panel's own, else from the first printer that gives it."""
    for src in f["sources"]:
        if src.get("pi"):
            path = _dir(group) / src["name"]
            if path.is_file():
                return str(path)
            continue
        prn = _printer(src["printer"])
        client = getattr(printer_files._collector(src["printer"]), "client", None)
        if prn is None or (client is None and prn.get("kind") != "creality"):
            continue
        if prn.get("kind") == "creality" and not src.get("mr"):
            continue
        local = os.path.join(workdir, "src-" + f["id"])
        try:
            model = str(prn.get("model") or "").upper()
            if prn.get("kind") == "creality":
                from app.clients import moonraker
                moonraker.download(prn["host"], src["mr"], local, f.get("size"))
            elif (printer_files._uses_ftps(model) or src.get("via") == "ftps"
                    or not src["path"].startswith(("/mnt/", "/userdata/"))):
                _ftp_get(client, src["path"], local, f.get("size"))
            else:
                from app.vendor.pybambu.media_sources import RemoteMediaFile, Tcp6000MediaSource
                source = Tcp6000MediaSource(printer_files._file_client(client))
                try:
                    source.download_file(RemoteMediaFile(name=src["name"], path=src["path"], size=f.get("size") or 0,
                                                         media_type="model", source="tcp6000"), local)
                finally:
                    source.close()
            if f.get("size") is not None and os.path.getsize(local) != f["size"]:
                raise EOFError("short download")
            return local
        except Exception as exc:
            logger.info("[pool] read from %s failed: %s", src["printer"], type(exc).__name__)
            if os.path.exists(local):
                os.unlink(local)
    raise LookupError(WHY_NO_SOURCE)


def _ftp_get(client, path: str, local: str, size: Optional[int]) -> None:
    ftp = client.ftp_connection()
    try:
        with open(local, "wb") as out:
            def receive(block):
                if size is not None and out.tell() + len(block) > size:
                    raise ValueError("longer than listed")
                out.write(block)
            ftp.retrbinary("RETR " + path, receive, blocksize=CHUNK)
    finally:
        try:
            ftp.close()
        except Exception:
            pass


def _upload_bambu(prn: Dict[str, Any], name: str, local: str, progress) -> None:
    """FTPS STOR to the SD card's root, as Bambu Studio sends a job."""
    client = getattr(printer_files._collector(prn["id"]), "client", None)
    if client is None:
        raise LookupError(WHY_OFFLINE)
    ftp = client.ftp_connection()
    try:
        with open(local, "rb") as stream:
            ftp.storbinary("STOR /" + name, stream, blocksize=CHUNK, callback=lambda block: progress(len(block)))
    finally:
        try:
            ftp.close()
        except Exception:
            pass


def _upload_creality(prn: Dict[str, Any], name: str, local: str, progress) -> None:
    """POST /upload/<name>, multipart field «file» — the printer's own web page (K1C / Ender-5 Max)."""
    boundary = "forge" + uuid.uuid4().hex
    head = ('--%s\r\nContent-Disposition: form-data; name="file"; filename="%s"\r\n'
            'Content-Type: application/octet-stream\r\n\r\n' % (boundary, name.replace('"', ""))).encode("utf-8")
    tail = ("\r\n--%s--\r\n" % boundary).encode()
    size = os.path.getsize(local)
    conn = http.client.HTTPConnection(prn["host"], CREALITY_WEB_PORT, timeout=CREALITY_UPLOAD_TIMEOUT_S)
    try:
        conn.putrequest("POST", "/upload/" + urllib.parse.quote(name))
        conn.putheader("Content-Type", "multipart/form-data; boundary=" + boundary)
        conn.putheader("Content-Length", str(len(head) + size + len(tail)))
        conn.endheaders()
        conn.send(head)
        with open(local, "rb") as stream:
            while True:
                block = stream.read(CHUNK)
                if not block:
                    break
                conn.send(block)
                progress(len(block))
        conn.send(tail)
        response = conn.getresponse()
        body = response.read(4096)
    finally:
        conn.close()
    import json
    try:
        ok = response.status == 200 and json.loads(body or b"{}").get("code") == 200
    except ValueError:
        ok = False
    if not ok:
        raise ValueError("upload refused: HTTP %s" % response.status)


def _printer(printer_id: str) -> Optional[Dict[str, Any]]:
    from app.services import printer_registry
    return printer_registry.get_printer(printer_id)


def status() -> Dict[str, Any]:
    with _lock:
        if not _job:
            return {"running": False}
        return {k: (v if k != "printers" else [dict(p) for p in v]) for k, v in _job.items() if k != "plan"}


def start_sync(plan_id: str, group: Optional[str] = None) -> Dict[str, Any]:
    """`group`: the one the request names — a plan of another group is refused, not run."""
    with _lock:
        plan = _plans.get(plan_id) if isinstance(plan_id, str) else None
        if plan is None or time.time() - plan["at"] > PLAN_TTL_S:
            raise ValueError(MSG_PLAN_STALE)
        if group is not None and plan["group"] != group:
            raise ValueError(MSG_PLAN_OTHER_GROUP)
        if _job.get("running"):
            raise ValueError(MSG_SYNC_RUNNING)
        _job.clear()
        _job.update({"running": True, "group": plan["group"], "title": plan["title"], "started": time.time(),
                     "finished": None, "plan": plan,
                     "printers": [{"printer": t["printer"], "label": t["label"], "state": "waiting",
                                   "files_total": len(t["files"]), "files_done": 0, "bytes_total": t["bytes"],
                                   "bytes_done": 0, "current": None, "errors": []} for t in plan["targets"] if t["files"]]})
        del _plans[plan_id]
    threading.Thread(target=_run, args=(plan,), name="file-pool-sync", daemon=True).start()
    return status()


def _row(printer_id: str) -> Dict[str, Any]:
    return next(r for r in _job["printers"] if r["printer"] == printer_id)


def _set(printer_id: str, **values) -> None:
    with _lock:
        _row(printer_id).update(values)


def _run(plan: Dict[str, Any]) -> None:
    group = plan["group"]
    order: Dict[str, List[str]] = {}
    for t in plan["targets"]:
        for fid in t["files"]:
            order.setdefault(fid, []).append(t["printer"])
    workdir = tempfile.mkdtemp(prefix=".sync-", dir=str(DIRECTORY) if DIRECTORY.is_dir() else None)
    try:
        for fid, targets in order.items():
            f = plan["files"][fid]
            live = []
            for pid in targets:
                prn = _printer(pid)
                with _lock:
                    gone = _row(pid)["state"] == "skipped"
                why = None if gone or prn is None else (_target_refusal(prn) or storage_refusal(prn))
                if gone:
                    continue
                if prn is None or why:
                    _set(pid, state="skipped", current=None, why=why or WHY_OFFLINE)
                    continue
                live.append(prn)
            if not live:
                continue
            try:
                local = _fetch(group, f, workdir)
            except LookupError as e:
                for prn in live:
                    with _lock:
                        _row(prn["id"])["errors"].append({"name": f["name"], "why": str(e)})
                continue
            try:
                for prn in live:
                    _copy_one(prn, f, local)
            finally:
                if local.startswith(workdir) and os.path.exists(local):
                    os.unlink(local)
        with _lock:
            for r in _job["printers"]:
                if r["state"] in ("waiting", "copying"):
                    r["state"] = "failed" if r["errors"] and not r["files_done"] else "done"
                    r["current"] = None
    except Exception:
        logger.error("[pool] sync failed", exc_info=True)
        with _lock:                 # the rest is not copied: every printer still in line says so
            for r in _job.get("printers") or []:
                if r["state"] in ("waiting", "copying"):
                    r.update(state="failed", current=None, why=WHY_SYNC_BROKEN)
    finally:
        with _lock:
            _job["running"] = False
            _job["finished"] = time.time()
        try:
            os.rmdir(workdir)
        except OSError:
            pass


def _copy_one(prn: Dict[str, Any], f: Dict[str, Any], local: str) -> None:
    pid = prn["id"]
    _set(pid, state="copying", current=f["name"])
    started = None
    with _lock:
        started = _row(pid)["bytes_done"]

    def progress(n):
        with _lock:
            _row(pid)["bytes_done"] += n
    try:
        if prn["kind"] == "bambu":
            _upload_bambu(prn, f["name"], local, progress)
        elif printer_files.has_klipper_api(prn):
            from app.clients import moonraker
            moonraker.upload(prn["host"], f["name"], local, progress)
        else:
            _upload_creality(prn, f["name"], local, progress)
        with _lock:
            row = _row(pid)
            row["files_done"] += 1
            row["bytes_done"] = started + (f.get("size") or 0)
    except Exception as exc:
        logger.warning("[pool] copy to %s failed: %s", pid, type(exc).__name__)
        why = str(exc) if isinstance(exc, LookupError) else "принтер не принял файл"   # LookupError: our own words
        with _lock:
            row = _row(pid)
            row["bytes_done"] = started + (f.get("size") or 0)   # the bar moves on; the error says what failed
            row["errors"].append({"name": f["name"], "why": why})
