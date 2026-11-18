"""Print files stored on the printers, and starting one of them from the panel.

Bambu: the printer's file channel (port 6000: X1 / P2 / H2 internal storage) or FTPS
(P1 / A1: the SD card). A start is the `project_file` command as ha-bambulab builds it
(pybambu utils.build_print_project_file_command): plate gcode, URL, AMS mapping, flags.
Creality: the WS listing (`reqGcodeFile` → retGcodeFileInfo2 on K1C / K2, the older
retGcodeFileInfo string on the Ender-5 Max) and `opGcodeFile: "printprt:<path>"`,
exactly what the printers' own web page sends. A Creality with Klipper's own API
(Moonraker :7125, the K2 Plus) lists its gcodes root there too — the source of
download, upload and delete (app/clients/moonraker.py); the WS listing adds its
thumbnails, times and grams.

A start may only name a file from a listing this process made in the last
LIST_TTL_S: never a path a request makes up.
"""
import copy
import io
import logging
import math
import re
import threading
import time
import urllib.parse
import urllib.request
import zipfile
from typing import Any, Dict, List, Optional
from xml.etree import ElementTree

from app.clients import moonraker
from app.services import bambu_models, project_summary

logger = logging.getLogger(__name__)

LIST_TTL_S = 300
LIST_WAIT_S = 12            # Creality: the listing arrives on the WS stream
META_LIMIT = 2 * 1024 * 1024
ARCHIVE_BUDGET = 8 * 1024 * 1024
SEQUENTIAL_MAX = 128 * 1024 * 1024
BLOCK = 65536
PLATE_MAX = 64

# Printers whose firmware takes a print URL relative to the SD card (ha-bambulab LEGACY_SDCARD_PRINTERS).
LEGACY_SDCARD = ("X1", "X1C", "X1E", "P1P", "P1S", "A1", "A1MINI")
EXTERNAL_SPOOL = 254
HT_SLOT_BASE, HT_UNIT_BASE = 16, 128

START_GCODE_STATES = {"IDLE", "FINISH", "FAILED"}

MSG_NOT_SUPPORTED = "Файлы на принтере есть только у Bambu и Creality"
MSG_OFFLINE = "Принтер не на связи"
MSG_NO_LISTING = "Принтер не прислал список файлов — повторите через минуту"
MSG_LIST_FAILED = "Не удалось получить список файлов с принтера"
MSG_STORAGE_CLOSED = ("Принтер не открывает свою память по сети (отвечает «хранилище недоступно») — "
                      "его файлы видны только на экране принтера")
MSG_STICK_UNREAD = "Не удалось прочитать список файлов на флешке принтера — повторите через минуту"
MSG_UNKNOWN_FILE = "Этого файла нет в свежем списке — откройте список файлов заново"
MSG_UNREADABLE = "Принтер не отдаёт содержимое этого файла — запустите его с экрана принтера"
MSG_BUSY = "Принтер занят: запуск только когда он свободен (не печатает, не на паузе)"
MSG_BAD_PLATE = "Такого стола в файле нет"
MSG_BAD_MAPPING = "Укажите слот для каждого пластика файла"
MSG_BAD_SLOT = "Слот %s не найден в AMS принтера"
MSG_EMPTY_SLOT = "Слот %s пуст"
MSG_NO_PRINT_CMDS = "Прошивка принтера не принимает команды печати (нет Developer Mode)"
MSG_CREALITY_BUSY = "Дождитесь окончания печати или подготовки принтера"
MSG_NOT_SLICED = "В файле нет нарезанных столов — сначала нарежьте его в слайсере"
MSG_NO_DELETE = "Удалить файл с принтера можно только через Klipper API принтера (есть у K2 Plus)"
MSG_DELETE_BUSY = "Удалять файлы можно, только когда принтер свободен (не печатает, не на паузе)"
MSG_DELETE_FAILED = "Принтер не удалил файл — повторите или удалите с экрана принтера"
MSG_DELETED = "Файл удалён с принтера"
META_WAIT_S = 5             # Klipper API printers: how long the WS listing may add its thumbnails

# The printers' factory sample files, by exact name (one list for the server: the per-printer
# list, the pool and the sync). Not by folder: /userdata/model/bbl holds the owner's files too,
# and the firmware's samples cannot be deleted over the network (cmdtype 3 → result 2).
_SAMPLE_BAMBU = {n.lower() + ".gcode.3mf" for n in (
    "3DBenchy", "Phone Stand", "Film Sticking Fixture", "Film Sticking Fixture by AMS", "Film Sticking Fixture_ams",
    "Outerdiameter Gauge", "Bed scraper", "Bambulab Badge", "Scraper Grip", "Scraper Grip by AMS", "Scraper Grip_ams",
    "Honeycomb Organizer", "Screw Gauge", "Vase", "3Dino Puzzle", "Burr Puzzle", "Squishy Turtle",
    "Spartan SDCard Holder", "Spool Holder", "Bambulab_Spool part 1", "Bambulab_Spool part 2",
    "BambulabSpool_part 1", "BambulabSpool_part 2", "mini tray", "Camera Hiding Shell")}
_SAMPLE_CREALITY = re.compile(r"(Crealitylogo_PLA_.*|3DBenchy.*|3DBench_PLA_.*)\.gcode", re.I)


def is_sample(name: Any) -> bool:
    name = str(name or "")
    return name.lower() in _SAMPLE_BAMBU or bool(_SAMPLE_CREALITY.fullmatch(name))


def hide_samples() -> bool:
    from app.services import settings_service
    return settings_service.peek("hide_sample_files") is not False


_store = None
_lock = threading.Lock()
_listings: Dict[str, Dict[str, Any]] = {}       # printer_id -> {"at", "files": {path: entry}, "order": [...]}
_details: Dict[tuple, Dict[str, Any]] = {}      # (printer_id, path, size, mtime) -> detail
_io_locks: Dict[str, threading.Lock] = {}


def set_store(store) -> None:
    global _store
    _store = store


def _get_store():
    if _store is not None:
        return _store
    from app.services import printer_commands
    return printer_commands.get_store()


def _io_lock(printer_id: str) -> threading.Lock:
    with _lock:
        return _io_locks.setdefault(printer_id, threading.Lock())


def _collector(printer_id: str):
    store = _get_store()
    return store.get_collector(printer_id) if store is not None else None


# --- parsing ---------------------------------------------------------------------------

def _int(value) -> Optional[int]:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return int(n) if math.isfinite(n) else None


def _entry(path, name, size, mtime, storage="", **extra) -> Dict[str, Any]:
    out = {"path": path, "name": name, "title": project_summary.short_name(name) or name,
           "size": size if isinstance(size, int) and size >= 0 else None,
           "mtime": mtime if isinstance(mtime, int) and mtime > 0 else None, "storage": storage}
    out.update({k: v for k, v in extra.items() if v not in (None, "", [], {})})
    return out


def bambu_entries(files) -> List[Dict[str, Any]]:
    """pybambu RemoteMediaFile list → entries: sliced projects only, newest first, demos last."""
    out, seen = [], set()
    for f in files:
        name = str(getattr(f, "basename", "") or "")
        path = str(getattr(f, "path", "") or "")
        if not name.lower().endswith(".3mf") or not path or path in seen or any(c in path for c in "\r\n\x00"):
            continue
        seen.add(path)   # P2S / X1C list internal storage once per storage name (emmc, udisk)
        modified = getattr(f, "modified", None)
        demo = is_sample(name)          # the firmware's own sample models
        out.append(_entry(path, name, int(getattr(f, "size", 0) or 0),
                          int(modified.timestamp()) if modified else None,
                          str(getattr(f, "storage", "") or ""), demo=True if demo else None))
    out.sort(key=lambda e: (bool(e.get("demo")), -(e["mtime"] or 0)))
    return out


def creality_entries(listing) -> List[Dict[str, Any]]:
    """retGcodeFileInfo2 (list of dicts) or retGcodeFileInfo ({"fileInfo": "dir:name:size:layer:mtime:mm:thumb;..."})."""
    out = []
    if isinstance(listing, list):
        for item in listing:
            if not isinstance(item, dict):
                continue
            path, name = str(item.get("path") or ""), str(item.get("name") or "")
            if not path.startswith("/") or not name or any(c in path for c in "\r\n\x00"):
                continue
            summary = project_summary.creality(item)
            # /downloads/humbnail/<name> serves `thumbnail` (live 16.11, K1C / K2); `preview`'s original/ is 404
            thumb = str(item.get("thumbnail") or "").rsplit("/", 1)[-1]
            out.append(_entry(path, name, _int(item.get("file_size")), _int(item.get("create_time")),
                              demo=True if is_sample(name) else None, time_s=summary.get("time_s"), grams=summary.get("grams"),
                              filaments=summary.get("filaments"),
                              thumb=("humbnail/" + thumb) if thumb.lower().endswith(".png") else None))
    elif isinstance(listing, dict):
        for record in str(listing.get("fileInfo") or "").split(";"):
            parts = record.split(":")
            if len(parts) < 7 or not parts[0].startswith("/") or not parts[1]:
                continue
            name = parts[1]
            path = parts[0].rstrip("/") + "/" + name
            if any(c in path for c in "\r\n\x00"):
                continue
            thumb = parts[6].rsplit("/", 1)[-1]
            out.append(_entry(path, name, _int(parts[2]), _int(parts[4]), demo=True if is_sample(name) else None,
                              thumb=("humbnail/" + thumb) if thumb else None))
    out.sort(key=lambda e: (bool(e.get("demo")), -(e["mtime"] or 0)))
    return out


def plates_from_slice_info(xml: bytes) -> List[Dict[str, Any]]:
    """Each sliced plate of a Bambu 3MF: index, time, grams, filaments (file order, 1-based id), parts' title."""
    if len(xml) > META_LIMIT or b"<!DOCTYPE" in xml.upper() or b"<!ENTITY" in xml.upper():
        raise ValueError("invalid slice metadata")
    plates = []
    for plate in ElementTree.fromstring(xml).findall("plate"):
        meta = {m.get("key"): m.get("value") for m in plate.findall("metadata")}
        index = _int(meta.get("index"))
        if index is None or not 1 <= index <= PLATE_MAX:
            continue
        filaments = []
        for f in plate.findall("filament"):
            fid = _int(f.get("id"))
            if fid is None or not 1 <= fid <= 64:
                continue
            color = str(f.get("color") or "")
            filaments.append({k: v for k, v in {
                "id": fid, "type": f.get("type") or None,
                "color": color[1:7].upper() if re.fullmatch(r"#[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?", color) else None,
                "grams": project_summary._number(f.get("used_g")),
                "tray_info_idx": f.get("tray_info_idx") or None}.items() if v is not None})
        names = []
        for o in plate.findall("object"):
            if str(o.get("skipped")).lower() != "true":
                n = project_summary.short_name(o.get("name")) or "Без названия"
                if n not in names:
                    names.append(n)
        plates.append({k: v for k, v in {
            "index": index, "time_s": _int(meta.get("prediction")) or None,
            "grams": project_summary._number(meta.get("weight")),
            "filaments": sorted(filaments, key=lambda f: f["id"]),
            "title": project_summary._title([{"name": n} for n in names]),
            "nozzle_mm": project_summary._number(str(meta.get("nozzle_diameters") or "").split(",")[0]),
        }.items() if v not in (None, "")})
    return sorted(plates, key=lambda p: p["index"])


# --- Bambu file channel ----------------------------------------------------------------

def _file_client(client):
    class FileClient:          # enables only the file channel, never the lib's own media downloads
        ftp_enabled = True
        def __getattr__(self, key):
            return getattr(client, key)
    return FileClient()


def _bambu_model(printer_id: str, prn: Dict[str, Any]) -> str:
    from app.services import printer_commands
    return str(printer_commands.bambu_model(printer_id, prn) or prn.get("model") or "").upper()


def _uses_ftps(model: str) -> bool:
    return bambu_models.has(model, "files_over_ftps")      # no port-6000 file channel


STORAGE_UNAVAILABLE = 17     # Bambu Studio PrinterFileSystem.h: the printer keeps that storage closed


def _usb_over_ftps(model: str) -> bool:
    """P2 / H2 / X2: FTPS shows only a USB stick (the internal storage is the file channel's)."""
    return bambu_models.has(model, "usb_over_ftps")


def _storage_closed(proxy) -> bool:
    """True when the file channel answers STORAGE_UNAVAILABLE for the internal storage
    (live 16.11: every H2S, fw 01.02 / 01.03, for model and timelapse alike; P2S lists)."""
    from app.vendor.pybambu.media_sources import Tcp6000MediaSource
    source = Tcp6000MediaSource(proxy)
    try:
        source._connect()
        seq = source._send_ctrl_request(1, {"type": "model", "api_version": 2, "notify": "DETAIL", "storage": "emmc"})
        reply, _ = source._read_matching_reply(1, seq, timeout=10)
        return int(reply.get("result", -1)) == STORAGE_UNAVAILABLE
    except Exception:
        return False
    finally:
        source.close()


class StickUnread(LookupError):
    """P2 / H2: the USB stick's listing failed; `files` = what the internal storage listed."""

    def __init__(self, files: List[Dict[str, Any]]):
        super().__init__(MSG_STICK_UNREAD)
        self.files = files


def list_bambu(client, model: str) -> List[Dict[str, Any]]:
    from app.vendor.pybambu.media_sources import Ftps990MediaSource, Tcp6000MediaSource
    proxy = _file_client(client)
    if _uses_ftps(model):
        source = Ftps990MediaSource(proxy)
        try:
            files = source.list_files("model", [".3mf"], ["/"])
        finally:
            source.close()
        return [dict(e, storage="sdcard") for e in bambu_entries(files)]
    source = Tcp6000MediaSource(proxy)
    try:
        entries = bambu_entries(source.list_files("model", [".3mf"]))
    finally:
        source.close()
    if _usb_over_ftps(model):
        usb, failed = [], False
        try:
            ftps = Ftps990MediaSource(proxy)
            try:
                usb = [dict(e, storage="udisk", via="ftps") for e in bambu_entries(ftps.list_files("model", [".3mf"], ["/"]))]
            finally:
                ftps.close()
        except Exception as exc:
            # the FTPS login itself failed (the library swallows a failed LIST): the stick is unknown
            logger.warning("USB listing over FTPS failed: %s", type(exc).__name__)
            failed = True
        on_usb = {(e["name"], e["size"]) for e in usb}
        entries = usb + [e for e in entries if (e["name"], e["size"]) not in on_usb]
        entries.sort(key=lambda e: (bool(e.get("demo")), -(e["mtime"] or 0)))
        if failed:
            raise StickUnread(entries)
        if not entries and _storage_closed(proxy):
            raise LookupError(MSG_STORAGE_CLOSED)
    return entries


class _Ranges(io.RawIOBase):
    """Seekable reader over a range callback, 64 KB blocks, a total byte budget."""

    def __init__(self, size: int, read_range):
        self.size, self.read_range = size, read_range
        self.pos = self.total = 0
        self.cache: Dict[int, bytes] = {}

    def seekable(self):
        return True

    def readable(self):
        return True

    def seek(self, offset, whence=0):
        self.pos = offset + (self.pos if whence == 1 else self.size if whence == 2 else 0)
        if self.pos < 0:
            raise ValueError("negative offset")
        return self.pos

    def tell(self):
        return self.pos

    def readinto(self, buffer):
        data = self.read(len(buffer))
        buffer[:len(data)] = data
        return len(data)

    def read(self, n=-1):
        n = self.size - self.pos if n is None or n < 0 else min(n, self.size - self.pos)
        if n <= 0:
            return b""
        out = bytearray()
        while n:
            start = self.pos // BLOCK * BLOCK
            if start not in self.cache:
                length = min(BLOCK, self.size - start)
                if self.total + length > ARCHIVE_BUDGET:
                    raise ValueError("archive read budget exceeded")
                block = self.read_range(start, length)
                if len(block) != length:
                    raise EOFError("short range")
                self.cache[start] = block
                self.total += length
            block = self.cache[start]
            take = min(n, len(block) - (self.pos - start))
            out.extend(block[self.pos - start:self.pos - start + take])
            self.pos += take
            n -= take
        return bytes(out)


def _tcp6000_archive(proxy, path: str, name: str, size: int) -> bytes:
    """The whole archive over the file channel (X1C: the SD card as /mnt/sdcard/; P2S refuses: result 2)."""
    import os
    import tempfile
    from app.vendor.pybambu.media_sources import RemoteMediaFile, Tcp6000MediaSource
    if size > SEQUENTIAL_MAX:
        raise ValueError("archive too large")
    remote = RemoteMediaFile(name=name, path=path, size=size, media_type="model", source="tcp6000")
    fd, temporary = tempfile.mkstemp(prefix=".forge-file-")
    os.close(fd)
    source = Tcp6000MediaSource(proxy)
    try:
        source.download_file(remote, temporary)
        if os.path.getsize(temporary) != size:
            raise EOFError("short archive")
        with open(temporary, "rb") as stream:
            return stream.read()
    finally:
        source.close()
        os.unlink(temporary)


def _ftp_range(client, path: str):
    def read_range(start, length):
        ftp = client.ftp_connection()
        try:
            ftp.voidcmd("TYPE I")
            with ftp.transfercmd("RETR " + path, rest=start) as sock:
                block = bytearray()
                while len(block) < length:
                    data = sock.recv(length - len(block))
                    if not data:
                        break
                    block.extend(data)
            return bytes(block)
        finally:
            try:
                ftp.close()
            except Exception:
                pass
    return read_range


def _ftp_sequential(client, path: str, size: int) -> bytes:
    """P1 firmware refuses REST: the whole archive once, bounded (print_material.read_sequential_archive)."""
    if size > SEQUENTIAL_MAX:
        raise ValueError("archive too large")
    ftp = client.ftp_connection()
    body = bytearray()
    deadline = time.monotonic() + 300
    try:
        def receive(block):
            if len(body) + len(block) > size or time.monotonic() > deadline:
                raise ValueError("archive read limit exceeded")
            body.extend(block)
        ftp.retrbinary("RETR " + path, receive, blocksize=BLOCK)
    finally:
        try:
            ftp.close()
        except Exception:
            pass
    if len(body) != size:
        raise EOFError("short archive")
    return bytes(body)


def _read_member(archive: zipfile.ZipFile, name: str) -> bytes:
    info = archive.getinfo(name)
    if info.file_size > META_LIMIT or info.compress_size > META_LIMIT:
        raise ValueError("member too large")
    return archive.read(info)


def archive_detail(archive: zipfile.ZipFile) -> Dict[str, Any]:
    from app.services import job_previews
    plates = plates_from_slice_info(_read_member(archive, "Metadata/slice_info.config"))
    names = set(archive.namelist())
    # only a plate with its G-code starts; a project saved unsliced has none (plan_bambu: MSG_NOT_SLICED)
    plates = [p for p in plates if "Metadata/plate_%d.gcode" % p["index"] in names]
    for plate in plates:
        try:
            plate["thumb"] = job_previews.save(_read_member(archive, "Metadata/plate_%d.png" % plate["index"]))
        except Exception:
            pass
    return {"plates": plates}


def bambu_detail(client, model: str, entry: Dict[str, Any]) -> Dict[str, Any]:
    path, size = entry["path"], entry.get("size") or 0
    if size <= 22:
        raise ValueError("empty archive")
    if _uses_ftps(model) or entry.get("via") == "ftps":
        try:
            with zipfile.ZipFile(_Ranges(size, _ftp_range(client, path))) as archive:
                return archive_detail(archive)
        except Exception as exc:
            logger.debug("ranged FTP read failed (%s), sequential", type(exc).__name__)
            with zipfile.ZipFile(io.BytesIO(_ftp_sequential(client, path, size))) as archive:
                return archive_detail(archive)
    body = _tcp6000_archive(_file_client(client), path, entry["name"], size)
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        return archive_detail(archive)


# --- listing and details: one entry point for both kinds ---------------------------------

def _printer(printer_id: str) -> Optional[Dict[str, Any]]:
    from app.services import printer_registry
    return printer_registry.get_printer(printer_id)


def _creality_listing(printer_id: str, wait: float = LIST_WAIT_S):
    collector = _collector(printer_id)
    client = getattr(collector, "client", None)
    if client is None or not getattr(client, "connected", False):
        raise LookupError(MSG_OFFLINE)
    since = time.monotonic()
    client.request_file_listing()
    deadline = since + wait
    while time.monotonic() < deadline:
        got = client.file_listing
        if got is not None and got[0] >= since:
            return got[1]
        time.sleep(0.2)
    raise LookupError(MSG_NO_LISTING)


def has_klipper_api(prn: Optional[Dict[str, Any]], wait: bool = True) -> bool:
    """A Creality whose Moonraker answers (wait=False: the cached answer only, never a probe)."""
    if not prn or prn.get("kind") != "creality" or not prn.get("host"):
        return False
    return moonraker.available(prn["host"]) if wait else moonraker.known(prn["host"]) is True


def _slashes(path: str) -> str:
    return re.sub(r"/+", "/", path)


def moonraker_entries(files: List[Dict[str, Any]], root: Optional[str]) -> List[Dict[str, Any]]:
    """moonraker.list_gcodes() → entries: .gcode only; path = the file on the printer (root + its
    path, what printprt takes), mr = its path inside the gcodes root (download / delete)."""
    if not root or not root.startswith("/"):
        return []
    out = []
    for f in files:
        rel = f.get("path") or ""
        name = rel.rsplit("/", 1)[-1]
        if not name.lower().endswith(".gcode") or name.startswith(".") or any(c in rel for c in "\r\n\x00"):
            continue
        out.append(_entry(_slashes(root.rstrip("/") + "/" + rel), name, _int(f.get("size")), _int(f.get("modified")),
                          mr=rel, demo=True if is_sample(name) else None))
    return out


def _klipper_listing(printer_id: str, prn: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The gcodes root from Moonraker, each file with the WS listing's picture, time and grams
    when that listing comes within META_WAIT_S; files only the WS lists stay (no mr)."""
    try:
        root = moonraker.roots(prn["host"]).get("gcodes")
        files = moonraker_entries(moonraker.list_gcodes(prn["host"]), root)
    except moonraker.MoonrakerError as exc:
        logger.info("[files] %s klipper listing failed: %s", printer_id, exc)
        raise LookupError(MSG_LIST_FAILED)
    try:
        ws = creality_entries(_creality_listing(printer_id, META_WAIT_S))
    except LookupError:
        ws = []
    by_path = {_slashes(e["path"]): e for e in ws}
    for e in files:
        extra = by_path.pop(e["path"], None)
        if extra is None:          # another spelling of the folder: the one WS file of that name and size
            same = [k for k, w in by_path.items() if w["name"] == e["name"] and w.get("size") in (None, e["size"])]
            extra = by_path.pop(same[0]) if len(same) == 1 else None
        if extra is not None:
            mr = e["mr"]
            e.update({k: v for k, v in extra.items() if k not in ("size", "mtime")})
            e["mr"] = mr
    files += list(by_path.values())
    files.sort(key=lambda e: (bool(e.get("demo")), -(e["mtime"] or 0)))
    return files


def list_files(printer_id: str) -> Dict[str, Any]:
    """{"kind", "files": [entry...], "klipper"} (+ "warning": a part not listed, the rest shown);
    LookupError(ru message) when nothing can be listed."""
    prn = _printer(printer_id)
    kind = (prn or {}).get("kind")
    if kind not in ("bambu", "creality"):
        raise LookupError(MSG_NOT_SUPPORTED)
    klipper = has_klipper_api(prn)
    warning = None
    with _io_lock(printer_id):
        files = None
        if klipper:
            try:
                files = _klipper_listing(printer_id, prn)
            except LookupError:
                klipper = False            # the WS listing still works: nothing to copy or delete
        if files is not None:
            pass
        elif kind == "creality":
            files = creality_entries(_creality_listing(printer_id))
        else:
            client = getattr(_collector(printer_id), "client", None)
            if client is None or not getattr(client, "connected", False):
                raise LookupError(MSG_OFFLINE)
            try:
                files = list_bambu(client, _bambu_model(printer_id, prn))
            except StickUnread as e:
                if not e.files:
                    raise
                files, warning = e.files, str(e)    # the internal storage's files still show
            except LookupError:
                raise
            except Exception as exc:
                logger.info("[files] %s listing failed: %s", printer_id, type(exc).__name__)
                raise LookupError(MSG_LIST_FAILED)
    with _lock:   # samples stay startable when listed: hiding is the panel's view, not a rule
        _listings[printer_id] = {"at": time.monotonic(), "files": {f["path"]: f for f in files}}
    hidden = sum(1 for f in files if f.get("demo")) if hide_samples() else 0
    if hidden:
        files = [f for f in files if not f.get("demo")]
    out = {"kind": kind, "files": files, "hidden_samples": hidden, "klipper": klipper}
    if warning:
        out["warning"] = warning
    return out


def listed(printer_id: str, path: Any) -> Optional[Dict[str, Any]]:
    """The entry of `path` from this printer's listing of the last LIST_TTL_S, else None."""
    if not isinstance(path, str):
        return None
    with _lock:
        listing = _listings.get(printer_id)
        if listing is None or time.monotonic() - listing["at"] > LIST_TTL_S:
            return None
        return listing["files"].get(path)


def file_detail(printer_id: str, path: str) -> Dict[str, Any]:
    """Bambu: the file's plates (read from the 3MF, cached); Creality: the listing entry itself."""
    entry = listed(printer_id, path)
    if entry is None:
        raise LookupError(MSG_UNKNOWN_FILE)
    prn = _printer(printer_id) or {}
    if prn.get("kind") != "bambu":
        return {"entry": entry}
    key = (printer_id, path, entry.get("size"), entry.get("mtime"))
    with _lock:
        cached = _details.get(key)
    if cached is not None:
        return dict(copy.deepcopy(cached), entry=entry)   # the caller adds its own keys per request
    client = getattr(_collector(printer_id), "client", None)
    if client is None:
        raise LookupError(MSG_OFFLINE)
    with _io_lock(printer_id):
        try:
            detail = bambu_detail(client, _bambu_model(printer_id, prn), entry)
        except Exception as exc:
            logger.info("[files] %s read failed: %s", printer_id, type(exc).__name__)
            raise LookupError(MSG_UNREADABLE)
    with _lock:
        _details[key] = detail
        while len(_details) > 400:
            _details.pop(next(iter(_details)))
    return dict(copy.deepcopy(detail), entry=entry)


def delete_file(printer_id: str, path: Any) -> Dict[str, Any]:
    """Delete one listed file through Moonraker (printer_commands.send's contract); only a file
    the listing got from Moonraker, only while the printer stands."""
    from app.services import printer_commands as pc
    prn = _printer(printer_id)
    entry = listed(printer_id, path)
    if entry is None:
        return pc._refused(MSG_UNKNOWN_FILE)
    if not entry.get("mr") or not has_klipper_api(prn):
        return pc._refused(MSG_NO_DELETE)
    if creality_start_refusal(pc._ws_live(printer_id, prn)):
        return pc._refused(MSG_DELETE_BUSY)
    with _io_lock(printer_id):
        try:
            moonraker.delete(prn["host"], entry["mr"])
        except moonraker.MoonrakerError as exc:
            logger.info("[files] %s delete failed: %s", printer_id, exc)
            return {"success": False, "status": "rejected", "detail": MSG_DELETE_FAILED, "replied": False}
    with _lock:
        listing = _listings.get(printer_id)
        if listing is not None:
            listing["files"].pop(entry["path"], None)
    return {"success": True, "status": "confirmed", "detail": MSG_DELETED, "replied": True}


def creality_thumb(printer_id: str, path: str) -> bytes:
    """The listing's own thumbnail, from the printer's web server (its file page's URLs)."""
    entry = listed(printer_id, path)
    prn = _printer(printer_id) or {}
    if entry is None or prn.get("kind") != "creality" or not entry.get("thumb"):
        raise LookupError(MSG_UNKNOWN_FILE)
    folder, _, name = entry["thumb"].partition("/")
    urls = ["http://%s/downloads/%s/%s" % (prn["host"], folder, urllib.parse.quote(name))]
    if folder == "humbnail":
        urls.append("http://%s/downloads/defData/%s" % (prn["host"], urllib.parse.quote(name)))
    for url in urls:
        try:
            with urllib.request.urlopen(url, timeout=6) as response:
                data = response.read(META_LIMIT + 1)
            if 0 < len(data) <= META_LIMIT:
                return data
        except Exception:
            continue
    raise LookupError("Картинки нет")


# --- AMS mapping ------------------------------------------------------------------------

def ams_slots(units: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The printer's AMS slots in the project_file numbering: unit*4+tray (AMS), 16+k (AMS HT 128+k)."""
    out = []
    for unit in units or []:
        index = unit.get("index")
        if not isinstance(index, int):
            continue
        slots = unit.get("slots") or []
        for tray, slot in enumerate(slots):
            if 0 <= index < 4 and tray < 4:
                flat, name = index * 4 + tray, "%d%s" % (index + 1, "ABCD"[tray])
            elif HT_UNIT_BASE <= index < HT_UNIT_BASE + 8 and tray == 0:
                flat, name = HT_SLOT_BASE + index - HT_UNIT_BASE, "HT%d" % (index - HT_UNIT_BASE + 1)
            else:
                continue
            out.append({"slot": flat, "ams_id": index, "slot_id": tray, "name": name,
                        "type": slot.get("type"), "color": slot.get("color"), "empty": bool(slot.get("empty"))})
    return out


def _color_distance(a: Optional[str], b: Optional[str]) -> float:
    try:
        x = [int(a[i:i + 2], 16) for i in (0, 2, 4)]
        y = [int(b[i:i + 2], 16) for i in (0, 2, 4)]
    except (TypeError, ValueError, IndexError):
        return 1e9
    return sum((p - q) ** 2 for p, q in zip(x, y)) ** 0.5


def default_mapping(filaments: List[Dict[str, Any]], slots: List[Dict[str, Any]]) -> List[int]:
    """Per filament: the loaded slot of the same material with the nearest colour, else the external spool."""
    loaded = [s for s in slots if not s["empty"] and s.get("type")]
    out = []
    for f in filaments:
        same = [s for s in loaded if str(s["type"]).upper() == str(f.get("type") or "").upper()]
        if not same:
            out.append(EXTERNAL_SPOOL)
            continue
        out.append(min(same, key=lambda s: _color_distance(s.get("color"), f.get("color")))["slot"])
    return out


# --- start ------------------------------------------------------------------------------

def build_project_file(model: str, entry: Dict[str, Any], plate: int, filaments: List[Dict[str, Any]],
                       mapping: List[int], slots: List[Dict[str, Any]], bed_leveling: bool,
                       timelapse: bool) -> Dict[str, Any]:
    """The project_file payload (ha-bambulab build_print_project_file_command + Studio's ams_mapping2).

    `mapping[i]` is the slot of the plate's i-th filament; ams_mapping is indexed by the
    project's filament id (Studio: -1 / ams 255 slot 255 for a filament this plate does not use).
    """
    from app.vendor.pybambu.commands import PRINT_PROJECT_FILE_TEMPLATE
    command = copy.deepcopy(PRINT_PROJECT_FILE_TEMPLATE)
    p = command["print"]
    path, storage = entry["path"], entry.get("storage") or ""
    if path.startswith("/mnt/sdcard/"):
        path = path[len("/mnt/sdcard"):]                         # X1C file channel: the SD card's own mount
    if path.startswith("/userdata/") or storage in ("emmc", "internal"):
        url = "file://" + path                                   # internal storage: the printer's own path
    elif model in LEGACY_SDCARD:
        url = "file:///sdcard/" + path.lstrip("/")
    else:
        url = "ftp:///" + path.lstrip("/")
    use_ams = any(m != EXTERNAL_SPOOL for m in mapping)
    by_slot = {s["slot"]: s for s in slots}
    width = max([f["id"] for f in filaments] or [1])
    flat, explicit = [-1] * width, [{"ams_id": 255, "slot_id": 255} for _ in range(width)]
    for f, m in zip(filaments, mapping):
        flat[f["id"] - 1] = m
        explicit[f["id"] - 1] = ({"ams_id": 255, "slot_id": 0} if m == EXTERNAL_SPOOL else
                                 {"ams_id": by_slot[m]["ams_id"], "slot_id": by_slot[m]["slot_id"]})
    p.update({
        "sequence_id": "0", "param": "Metadata/plate_%d.gcode" % plate, "url": url,
        "timelapse": bool(timelapse),
        "bed_leveling": bool(bed_leveling), "auto_bed_leveling": 1 if bed_leveling else 0,
        "flow_cali": False, "extrude_cali_flag": 0, "vibration_cali": False, "layer_inspect": False,
        "use_ams": use_ams, "subtask_name": project_summary.short_name(entry["name"]) or entry["name"],
    })
    if use_ams:
        p["ams_mapping"], p["ams_mapping2"] = flat, explicit
    else:
        p["ams_mapping"] = [EXTERNAL_SPOOL if m != -1 else -1 for m in flat]   # the template's default role
    return command


def _bool(value, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def plan_bambu(printer_id: str, prn: Dict[str, Any], params: Dict[str, Any], view) -> Dict[str, Any]:
    """Validate a Bambu start (no I/O but the cached detail) and build its payload; ValueError(ru)."""
    entry = listed(printer_id, params.get("path"))
    if entry is None:
        raise ValueError(MSG_UNKNOWN_FILE)
    key = (printer_id, entry["path"], entry.get("size"), entry.get("mtime"))
    with _lock:
        detail = _details.get(key)
    if not detail or not detail.get("plates"):
        raise ValueError(MSG_NOT_SLICED if detail else MSG_UNREADABLE)
    plate_no = params.get("plate")
    plate = next((p for p in detail["plates"] if type(plate_no) is int and p["index"] == plate_no), None)
    if plate is None:
        raise ValueError(MSG_BAD_PLATE)
    filaments = plate.get("filaments") or []
    if not filaments:
        raise ValueError(MSG_UNREADABLE)
    model = _bambu_model(printer_id, prn)
    if bambu_models.has(model, "dual_nozzle"):
        from app.services.printer_commands import MSG_DUAL_NOZZLE
        raise ValueError(MSG_DUAL_NOZZLE)
    mapping = params.get("mapping")
    slots = ams_slots(view.units) if view is not None else []
    if not isinstance(mapping, list) or len(mapping) != len(filaments) or any(type(m) is not int for m in mapping):
        raise ValueError(MSG_BAD_MAPPING)
    by_slot = {s["slot"]: s for s in slots}
    for m in mapping:
        if m == EXTERNAL_SPOOL:
            continue
        if m not in by_slot:
            raise ValueError(MSG_BAD_SLOT % m)
        if by_slot[m]["empty"]:
            raise ValueError(MSG_EMPTY_SLOT % by_slot[m]["name"])
    return build_project_file(model, entry, plate["index"], filaments, mapping, slots,
                              _bool(params.get("bed_leveling"), True), _bool(params.get("timelapse"), False))


def bambu_start_refusal(view) -> Optional[str]:
    from app.services import printer_commands as pc
    if view is None:
        return pc.MSG_NO_TELEMETRY
    if view.gcode_state not in START_GCODE_STATES:
        return MSG_BUSY
    if view.status.filament_change is not None or view.raw.get("busy"):
        return pc.MSG_CHANGE_RUNNING
    return None


def creality_start_refusal(live) -> Optional[str]:
    from app.services import printer_commands as pc
    from app.domain.models import PrinterState
    if live is None:
        return pc.MSG_NO_TELEMETRY
    st, data = live
    if st.state not in (PrinterState.IDLE, PrinterState.FINISHED, PrinterState.CANCELLED):
        return MSG_BUSY
    if data.get("device_state") not in (0, None) or data.get("feed_state", 0) not in (0, None):
        return MSG_CREALITY_BUSY
    return None


def start(printer_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """Start a listed file: printer_commands.send's contract ({"success", "status", "detail", "replied"})."""
    from app.services import printer_commands as pc
    params = params or {}
    prn = _printer(printer_id)
    kind = (prn or {}).get("kind")
    if kind not in ("bambu", "creality"):
        return pc._refused(MSG_NOT_SUPPORTED)
    if kind == "creality":
        entry = listed(printer_id, params.get("path"))
        if entry is None:
            return pc._refused(MSG_UNKNOWN_FILE)
        refusal = creality_start_refusal(pc._ws_live(printer_id, prn))
        if refusal:
            return pc._refused(refusal)
        r = pc._send_creality(printer_id, "print_file", [{"opGcodeFile": "printprt:" + entry["path"]}])
    else:
        if pc.get_capability(printer_id) is False:
            return pc._refused(MSG_NO_PRINT_CMDS)
        view = pc._live_view(printer_id, prn)
        try:
            payload = plan_bambu(printer_id, prn, params, view)   # parameters first
        except ValueError as e:
            return pc._refused(str(e))
        refusal = bambu_start_refusal(view)
        if refusal:
            return pc._refused(refusal)
        if pc._inflight_blocks(printer_id, view.raw):
            return pc._refused(pc.MSG_INFLIGHT)
        # the guard runs under the publish lock: the state is read again right before the publish
        def changed():
            again = pc._live_view(printer_id, prn)
            return bambu_start_refusal(again) is not None
        r = pc._send_local(printer_id, payload, guard=changed)
        if r.get("skipped"):
            return pc._refused(MSG_BUSY)
        pc._note_reply(printer_id, payload, r)
    out = pc._outcome(r)
    if out["success"]:
        pc._start_fast_refresh(printer_id, prn, None, pc.FAST_REFRESH_CMD_MAX)
    return out
