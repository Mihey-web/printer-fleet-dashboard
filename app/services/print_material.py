"""Slicer-planned grams. Unknown stays None; never extrapolate from progress."""
import io
import ftplib
import hashlib
import json
import logging
import math
import re
import threading
import tempfile
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from xml.etree import ElementTree

from app.services import bambu_models, project_summary

READ_WORKERS = 2      # job reads at once across the farm (grams, preview, card): seconds each
SUPPORT_WORKERS = 1   # supports' counts at once, their own queue: the whole gcode, minutes each
_POOL = ThreadPoolExecutor(max_workers=READ_WORKERS, thread_name_prefix="print-material")
_SUPPORT_POOL = ThreadPoolExecutor(max_workers=SUPPORT_WORKERS, thread_name_prefix="print-supports")
MAX_ATTEMPTS = 4
logger = logging.getLogger(__name__)
_LIMIT = 2 * 1024 * 1024


def grams(value):
    if isinstance(value, bool):
        return None
    try:
        n = float(value)
        return round(n, 3) if math.isfinite(n) and 0 < n <= 100000 else None
    except (TypeError, ValueError):
        return None


# The Ender-5 Max's listing (retGcodeFileInfo, "dir:name:size:layer:mtime:mm:thumb;...") has the
# filament's length, not its weight: grams = length x 1.75 mm filament's section x the material's
# density. The densities are Creality Print's own (filamentWeight / consumables on the K1C, 17.11:
# PETG 1.27); the material is the one Creality Print writes in the file's name ("Part_PETG_2h5m.gcode"),
# else PETG, 171 of the K1C's 188 files on 17.11.
FILAMENT_SECTION_MM2 = math.pi * 0.875 ** 2
DENSITY_G_CM3 = {"PLA": 1.24, "PETG": 1.27, "ABS": 1.04, "ASA": 1.07, "TPU": 1.21, "PC": 1.20}
DEFAULT_DENSITY = DENSITY_G_CM3["PETG"]
_NAME_MATERIAL = re.compile(r"_(%s)(?:[-+][A-Z0-9]*)*_" % "|".join(DENSITY_G_CM3), re.I)   # PETG-CF, PLA+


def length_grams(length_mm, name=""):
    try:
        length = float(length_mm)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(length) or length <= 0:
        return None
    material = _NAME_MATERIAL.search(str(name or ""))
    density = DENSITY_G_CM3[material.group(1).upper()] if material else DEFAULT_DENSITY
    return grams(round(length * FILAMENT_SECTION_MM2 / 1000 * density, 1))


def _listing_v1_grams(listing, path):
    if not isinstance(listing, dict):
        return None
    for record in str(listing.get("fileInfo") or "").split(";"):
        parts = record.split(":")
        if len(parts) < 7:
            continue
        name = ":".join(parts[1:-5])      # dir first, the five numbers and the thumbnail last
        if name and parts[0].rstrip("/") + "/" + name == path:
            return length_grams(parts[-2], name)
    return None


def creality_grams(report):
    path = report.get("printFileName")
    if not isinstance(path, str) or not path:
        return None
    if "retGcodeFileInfo" in report:
        return _listing_v1_grams(report.get("retGcodeFileInfo"), path)
    for item in report.get("retGcodeFileInfo2", []) or []:
        if not isinstance(item, dict) or item.get("path") != path:
            continue
        values = str(item.get("filamentWeight", "")).split(",")
        try:
            weights = [float(v) for v in values]
            if any(not math.isfinite(v) or v < 0 for v in weights):
                return None
            return grams(sum(weights))
        except ValueError:
            return None
    return None


def slice_grams(xml, plate_number=None):
    if len(xml) > _LIMIT or b"<!DOCTYPE" in xml.upper() or b"<!ENTITY" in xml.upper():
        raise ValueError("invalid slice metadata")
    plates = ElementTree.fromstring(xml).findall("plate")
    chosen = []
    for plate in plates:
        metadata = {m.get("key"): m.get("value") for m in plate.findall("metadata")}
        if plate_number is not None and metadata.get("index") == str(plate_number):
            chosen = [metadata]
            break
        if plate_number is None:
            chosen.append(metadata)
    if len(chosen) != 1:
        return None  # Never use the first plate of an ambiguous multi-plate file.
    return grams(chosen[0].get("weight"))


_BLOCK = 65536
_SKIP = 2 * _BLOCK   # read through a short forward gap instead of opening a new range


class RemoteZip(io.RawIOBase):
    """Seekable bounded reader: only ZIP directory and slice metadata, no model/video.

    open_range(start) returns a stream positioned at `start` (read(n) gives exactly n
    bytes, close() ends it). Contiguous blocks are read from the one open stream; a new
    range is opened only on a jump, so a read costs a few ranges instead of one per block.
    """
    def __init__(self, open_range, size):
        super().__init__()
        self.open_range, self.size = open_range, size
        self.pos = self.total = 0
        self.budget = _LIMIT
        self.streaming = False
        self.cache = {}
        self.stream = None
        self.stream_pos = None

    def allow(self, extra):
        """Read `extra` more bytes for one long member (the gcode), front to back: blocks
        already passed are dropped instead of cached."""
        self.budget += extra
        self.streaming = True

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

    def _drop_stream(self):
        stream, self.stream, self.stream_pos = self.stream, None, None
        if stream is not None:
            stream.close()

    def close(self):
        try:
            self._drop_stream()
        finally:
            super().close()

    def _fetch(self, start):
        """Cache the block at `start`, reading on from the open range when it is near."""
        if self.stream is None or not (0 <= start - self.stream_pos <= _SKIP):
            self._drop_stream()
            self.stream, self.stream_pos = self.open_range(start), start
        while start not in self.cache:
            at = self.stream_pos
            length = min(_BLOCK, self.size - at)
            if self.total + length > self.budget:
                raise ValueError("metadata budget exceeded")
            block = self.stream.read(length)
            if len(block) != length:
                raise EOFError("short metadata range")
            self.stream_pos += length
            self.total += length
            self.cache.setdefault(at, block)
        if self.stream_pos >= self.size:
            self._drop_stream()

    def read(self, n=-1):
        n = self.size - self.pos if n is None or n < 0 else min(n, self.size - self.pos)
        if n <= 0:
            return b""
        if n > _LIMIT:
            raise ValueError("metadata read too large")
        output = bytearray()
        while n:
            start = self.pos // _BLOCK * _BLOCK
            if start not in self.cache:
                self._fetch(start)
            block = self.cache[start]
            take = min(n, len(block) - (self.pos - start))
            output.extend(block[self.pos - start:self.pos - start + take])
            self.pos += take
            n -= take
            if self.streaming and self.pos - start == len(block):
                del self.cache[start]
        return bytes(output)


class FtpRanges:
    """One FTPS login for the whole job read (size probe, ranges, P1 fallback)."""
    def __init__(self, factory, path, ftp=None):
        self.factory, self.path, self.ftp = factory, path, ftp

    def connection(self):
        if self.ftp is None:
            self.ftp = self.factory()
            self.ftp.voidcmd("TYPE I")
        return self.ftp

    def open(self, start):
        return _FtpStream(self, self.connection().transfercmd("RETR " + self.path, rest=start))

    def finish_transfer(self, complete):
        try:
            self.ftp.voidresp()   # 226 after a full transfer, 426/451 after an early close
        except (ftplib.error_temp, ftplib.error_perm):
            if complete:
                self.close()
        except Exception:
            self.close()   # unknown state: the next range logs in again

    def close(self):
        ftp, self.ftp = self.ftp, None
        if ftp is not None:
            try:
                ftp.close()
            except Exception:
                pass


class _FtpStream:
    def __init__(self, owner, sock):
        self.owner, self.sock, self.done = owner, sock, False

    def read(self, length):
        block = bytearray()
        while len(block) < length:
            data = self.sock.recv(length - len(block))
            if not data:
                self.done = True
                break
            block.extend(data)
        return bytes(block)

    def close(self):
        sock, self.sock = self.sock, None
        if sock is None:
            return
        try:
            sock.close()
        finally:
            self.owner.finish_transfer(self.done)


class Tcp6000Ranges:
    """Ranges of one file over the printer's file channel (port 6000).

    The printer streams from `offset` to the end of the file, so a range is read from
    one session for as long as the reads stay contiguous; a jump closes it. The first
    range reuses the session that listed the files. Every data reply must continue at
    exactly the offset already received.
    """
    def __init__(self, make_source, remote_file, source=None):
        self.make_source, self.remote_file, self.idle = make_source, remote_file, source

    def open(self, start):
        source, self.idle = self.idle, None
        if source is None:
            source = self.make_source()
        try:
            request = source._download_request(self.remote_file)
            request["offset"] = start
            seq = source._send_ctrl_request(4, request)
        except Exception:
            source.close()
            raise
        return _Tcp6000Stream(source, seq, start)

    def close(self):
        source, self.idle = self.idle, None
        if source is not None:
            source.close()


class _Tcp6000Stream:
    def __init__(self, source, seq, start):
        self.source, self.seq, self.offset = source, seq, start
        self.pending, self.ended = b"", False

    def read(self, length):
        from app.vendor.pybambu.media_sources import RESULT_CONTINUE, RESULT_OK
        data = bytearray(self.pending[:length])
        self.pending = self.pending[length:]
        while len(data) < length and not self.ended:
            reply, chunk = self.source._read_matching_reply(4, self.seq, timeout=15)
            result = reply.get("result")
            if result not in (RESULT_CONTINUE, RESULT_OK):
                raise ValueError("file range refused: %s" % result)
            info = reply.get("reply") or {}
            self.ended = result == RESULT_OK
            if info.get("mem_dl_param_size") or not chunk:
                continue
            if int(info.get("offset", -1)) != self.offset:
                raise ValueError("file range offset mismatch")
            if "size" in info and int(info["size"]) != len(chunk):
                raise ValueError("file range size mismatch")
            self.offset += len(chunk)
            need = length - len(data)
            data.extend(chunk[:need])
            self.pending = chunk[need:]
        return bytes(data)

    def close(self):
        self.source.close()


def read_current_file(client, member="Metadata/slice_info.config"):
    """Studio's get_project_file RAM reader; reads only current slice metadata.

    H2/P2 firmware may deny internal archive downloads while serving this API.
    See BambuStudio PrinterFileSystem.cpp: GetPickImage / DownloadRamFile(16).
    """
    from app.vendor.pybambu.media_sources import Tcp6000MediaSource, MAGIC_CTRL, _wrap_ctrl_json
    class FileClient:
        ftp_enabled = True
        def __getattr__(self, key):
            return getattr(client, key)
    source = Tcp6000MediaSource(FileClient())
    try:
        source._connect()
        seq = source._next_cmd_seq()
        param = json.dumps({"sequence_id": seq, "version": 1, "peer_host": "studio",
                            "command": "get_project_file",
                            "file_rel_path": member}).encode()
        request = {"path": "mem:/16", "offset": 0, "mem_dl_param_size": len(param)}
        source._send_frame(MAGIC_CTRL, _wrap_ctrl_json(
            {"cmdtype": 4, "sequence": seq, "req": request}) + b"\n\n" + param)
        data = bytearray()
        deadline = time.monotonic() + 30
        for _ in range(128):
            if time.monotonic() >= deadline:
                raise TimeoutError("slice metadata timeout")
            reply, chunk = source._read_matching_reply(4, seq, timeout=8)
            if reply.get("result") not in (0, 1):
                raise ValueError("current slice unavailable")
            info = reply.get("reply") or {}
            total = int(info.get("total", 0))
            if total <= 0 or total > _LIMIT:
                raise ValueError("invalid slice metadata size")
            if info.get("mem_dl_param_size"):
                if json.loads(chunk).get("result") != 0:
                    raise ValueError("current slice unavailable")
                continue
            if int(info.get("offset", -1)) != len(data) or int(info.get("size", -1)) != len(chunk):
                raise ValueError("invalid slice metadata offset")
            data.extend(chunk)
            if len(data) > total or len(data) > _LIMIT:
                raise ValueError("slice metadata too large")
            if reply["result"] == 0:
                if len(data) != total or hashlib.md5(data).hexdigest() != info.get("file_md5"):
                    raise ValueError("slice metadata checksum mismatch")
                return bytes(data)
        raise ValueError("incomplete slice metadata")
    finally:
        source.close()


def read_current_slice(client):
    return read_current_file(client)


def preview_plate(xml, plate):
    if plate is not None:
        return plate
    plates = ElementTree.fromstring(xml).findall("plate")
    if len(plates) == 1:
        for item in plates[0].findall("metadata"):
            if item.get("key") == "index" and str(item.get("value", "")).isdigit():
                return int(item.get("value"))
    return None


def collect_preview(reader, xml, plate, on_preview):
    if on_preview is None:
        return
    try:
        number = preview_plate(xml, plate)
        if number is None:
            return
        on_preview(reader("Metadata/plate_%s.png" % number))
    except Exception:
        pass  # A missing/invalid thumbnail must not suppress the filament estimate.


def metadata_document(data):
    if len(data) > _LIMIT or b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise ValueError("invalid project metadata")
    if data.lstrip().startswith(b"{"):
        return json.loads(data)
    def node(element):
        result = dict(element.attrib)
        if element.text and element.text.strip():
            result["text"] = element.text.strip()
        for child in element:
            result.setdefault(child.tag, []).append(node(child))
        return result
    root = ElementTree.fromstring(data)
    return {root.tag: node(root)}


def collect_project(reader, xml, plate, on_project):
    """Hand on_project the human summary of the job (project_summary.bambu).

    H2/P2/X1 firmware serves only slice_info/model_settings from RAM (err_no -2 for
    project_settings.config), so print settings appear only where the archive is readable.
    """
    if on_project is None:
        return None
    try:
        files = {"slice_info.config": metadata_document(xml)}
        try:
            files["project_settings.config"] = metadata_document(reader("Metadata/project_settings.config"))
        except Exception:
            pass
        number = preview_plate(xml, plate)
        summary = project_summary.bambu({"plate": number, "files": files})
        if summary.get("parts") and number is not None:
            try:   # the plate map: each part filled with its id as a colour (Bambu Studio's pick image)
                from app.services import job_previews
                summary["pick"] = job_previews.save(reader("Metadata/pick_%s.png" % number))
            except Exception as exc:
                logger.debug("plate map unavailable: %s", type(exc).__name__)
        if summary:
            on_project(summary)
            return summary
    except Exception:
        pass  # Project details are optional; the filament estimate must not depend on them.
    return None


# ── Support weight. Bambu Studio writes only the job's total grams (slice_info, the gcode
# header); its per-feature split stays in the slicer's window. The gcode marks every feature
# («; FEATURE: Support», «Support interface», «Support transition»), so the supports' share is
# the E of their extrusion moves, weighed by the header's filament diameter and density.
GCODE_LIMIT = 1024 * 1024 * 1024      # decompressed gcode bound
SUPPORT_TIME_S = 300                   # the whole count (download included) or nothing
_CHUNK = 1024 * 1024                   # one regex pass holds the GIL for about this much text
_LINE_MAX = 1024 * 1024                # no gcode line is that long
_RX_MARK = re.compile(rb"^(?:(?:; FEATURE: |;TYPE:)([^\r\n]*)|T(\d+))\r?$", re.M)   # Bambu / Orca, a tool change
_RX_EXTRUDE = re.compile(rb"^G[123] [^\n;]*?[XY][^\n;]*?E(\d*\.?\d+)", re.M)   # E-: retracts, wipes


def _header_list(text, key):
    match = re.search(rb"^; " + key + rb": *([\d.,]+)\r?$", text, re.M)
    if not match:
        return []
    values = [float(v) for v in match[1].split(b",") if v]
    return values if values and all(math.isfinite(v) and 0 < v < 10 for v in values) else []


def support_grams(stream, deadline, chunk=_CHUNK):
    """Grams of filament the gcode extrudes as supports; None when it cannot tell."""
    densities, diameters = [], []
    feature, tool, carry, read = b"", 0, b"", 0
    lengths = {}
    while True:
        if time.monotonic() > deadline:
            return None
        block = stream.read(chunk)
        read += len(block)
        if read > GCODE_LIMIT or len(carry) > _LINE_MAX:
            return None
        text = carry + block
        if block:   # whole lines only; the rest waits for the next block
            cut = text.rfind(b"\n") + 1
            text, carry = text[:cut], text[cut:]
        if not (densities and diameters):   # the header: the first lines of the file
            densities = densities or _header_list(text, rb"filament_density")
            diameters = diameters or _header_list(text, rb"filament_diameter")
            if not (densities and diameters) and (read > 256 * 1024 or not block):
                return None
        parts = _RX_MARK.split(text)
        for i in range(0, len(parts), 3):
            if feature.startswith(b"Support"):
                moves = _RX_EXTRUDE.findall(parts[i])
                if moves:
                    lengths[tool] = lengths.get(tool, 0.0) + sum(map(float, moves))
            if i + 1 < len(parts):
                if parts[i + 1] is not None:
                    feature = parts[i + 1].strip()
                elif densities and int(parts[i + 2]) < len(densities):   # T255 / T1000: machine moves
                    tool = int(parts[i + 2])
        if not block:
            break
    total = 0.0
    for t, mm in lengths.items():
        diameter = diameters[t] if t < len(diameters) else diameters[0]
        total += mm * math.pi * (diameter / 2) ** 2 * densities[t] / 1000
    return round(total, 1) if math.isfinite(total) else None


def plate_metadata(xml, number):
    for item in ElementTree.fromstring(xml).findall("plate"):
        meta = {m.get("key"): m.get("value") for m in item.findall("metadata")}
        if meta.get("index") == str(number):
            return meta
    return {}


def supports_used(xml, plate):
    """The plate's number when the slicer put supports on it, else None."""
    number = preview_plate(xml, plate)
    return number if number is not None and plate_metadata(xml, number).get("support_used") == "true" else None


def collect_supports(archive, xml, plate, summary, on_project, allow=None):
    """Hand on_project the summary again with the supports' grams, for a job that has supports."""
    if not summary or on_project is None:
        return
    try:
        number = supports_used(xml, plate)
        if number is None:
            return
        member = archive.getinfo("Metadata/plate_%d.gcode" % number)
        if member.file_size > GCODE_LIMIT:
            return
        if allow is not None:
            allow(member.compress_size + _BLOCK)   # a remote archive streams the gcode once
        with archive.open(member) as stream:
            value = support_grams(stream, time.monotonic() + SUPPORT_TIME_S)
        if value is not None:
            on_project(dict(summary, support_grams=value))
    except Exception as exc:
        logger.debug("support weight unavailable: %s", type(exc).__name__)


def archive_grams(archive, plate, on_preview=None, on_project=None, allow=None, on_supports=None):
    """The job's grams; on_preview / on_project get its preview and card. The supports' grams
    (the whole gcode, minutes) are counted here, or with on_supports only announced: the job
    has supports, their count is left to the caller (MaterialReader: their own queue)."""
    member = archive.getinfo("Metadata/slice_info.config")
    if member.file_size > _LIMIT or member.compress_size > _LIMIT:
        raise ValueError("slice metadata too large")
    xml = archive.read(member)
    value = slice_grams(xml, plate)
    def image_reader(name):
        entry = archive.getinfo(name)
        if entry.file_size > _LIMIT or entry.compress_size > _LIMIT:
            raise ValueError("preview too large")
        return archive.read(entry)
    collect_preview(image_reader, xml, plate, on_preview)
    summary = collect_project(image_reader, xml, plate, on_project)
    if on_supports is None:
        collect_supports(archive, xml, plate, summary, on_project, allow)
    elif summary and on_project is not None and supports_used(xml, plate) is not None:
        on_supports()
    return value


def read_sequential_archive(factory, path, size, plate, on_preview=None, on_project=None, on_supports=None):
    """P1 firmware rejects FTP REST; spool the exact job archive once, bounded."""
    if size > 128 * 1024 * 1024:
        raise ValueError("job archive too large for sequential read")
    ftp = factory()
    deadline = time.monotonic() + 300
    try:
        with tempfile.SpooledTemporaryFile(max_size=_LIMIT) as stream:
            def receive(block):
                if stream.tell() + len(block) > size or time.monotonic() > deadline:
                    raise ValueError("job archive read limit exceeded")
                stream.write(block)
            ftp.retrbinary("RETR " + path, receive, blocksize=65536)
            if stream.tell() != size:
                raise EOFError("short job archive")
            stream.seek(0)
            with zipfile.ZipFile(stream) as archive:
                return archive_grams(archive, plate, on_preview, on_project, on_supports=on_supports)
    finally:
        ftp.close()


def _plate(gcode):
    match = re.search(r"plate_(\d+)\.gcode$", gcode or "", re.I)
    return int(match[1]) if match else None


def read_bambu(client, name, gcode, on_preview=None, on_project=None, on_value=None, on_supports=None):
    """The job's grams (on_value: as soon as known), preview and card; with on_supports the
    supports' count is only announced (archive_grams), read_supports does it."""
    plate = _plate(gcode)
    model = str(getattr(client, "_device_type", "")).upper()
    ram_error = None
    if bambu_models.has(model, "current_job_over_file_channel"):
        value = None
        try:
            xml = read_current_slice(client)
            value = slice_grams(xml, plate)
            collect_preview(lambda member: read_current_file(client, member), xml, plate, on_preview)
            collect_project(lambda member: read_current_file(client, member), xml, plate, on_project)
        except Exception as exc:
            ram_error = exc  # Older firmware can still expose the job over FTP.
        if value is not None:
            if on_project is not None:
                if on_value is not None:
                    on_value(value)   # the grams now; the archive below may take minutes
                more_details(client, name, gcode, plate, value, on_project, on_supports)
            return value
    return read_archive(client, name, gcode, plate, on_preview, on_project, ram_error, on_supports)


def more_details(client, name, gcode, plate, value, on_project, on_supports=None):
    """The memory serves no print settings (err_no -2 for project_settings and the gcode).
    The job's archive on the SD card / USB stick has both: the full summary replaces the short
    one when it is the same job (the same grams); without storage the short one stays."""
    held, supports = [], []
    try:
        if read_archive(client, name, gcode, plate, None, held.append,
                        on_supports=None if on_supports is None else lambda: supports.append(True)) == value and held:
            on_project(held[-1])
            if supports:
                on_supports()
    except Exception as exc:
        logger.debug("job archive unavailable: %s", type(exc).__name__)


def read_supports(client, name, gcode, value, on_project):
    """The card with the supports' grams of a job whose read announced them (on_supports): its
    archive read once more, the count included; handed on only for the same job (the same
    grams) when the count came through."""
    held = []
    if read_archive(client, name, gcode, _plate(gcode), None, held.append) == value and held \
            and held[-1].get("support_grams") is not None:
        on_project(held[-1])


def read_archive(client, name, gcode, plate, on_preview=None, on_project=None, ram_error=None, on_supports=None):
    """The job's 3MF from the printer's storage: FTP (SD card, USB stick), else port 6000."""
    factory = client.ftp_connection
    candidates = []
    for value in (gcode, name):
        if not value or any(c in value for c in ("\r", "\n", "\x00")):
            continue
        base = value.replace("\\", "/").rsplit("/", 1)[-1]
        if base.startswith("plate_") and base.endswith(".gcode"):
            continue
        names = [base] if base.endswith(".3mf") else [base + ".3mf", base + ".gcode.3mf"]
        for filename in names:
            for prefix in ("/cache/", "/"):
                path = prefix + filename
                if path not in candidates:
                    candidates.append(path)
    if not candidates:
        if ram_error is not None:
            raise ram_error  # a failed read is retried; "no estimate" is not
        return None
    ftp = factory()
    path = size = None
    try:
        ftp.voidcmd("TYPE I")
        for candidate in candidates:
            try:
                candidate_size = ftp.size(candidate)
                if candidate_size and candidate_size > 22:
                    path, size = candidate, candidate_size
                    break
            except Exception:
                continue
    except BaseException:
        ftp.close()
        raise
    if path is not None:
        ranges = FtpRanges(factory, path, ftp)   # the probe's login serves the whole read
        try:
            return read_ranges(ranges.open, size, plate, on_preview, on_project, on_supports)
        except ftplib.error_perm as exc:
            if str(exc)[:3] not in ("500", "501", "502", "504"):
                raise
            # P1 refuses REST; the same login spools the archive once instead.
            return read_sequential_archive(ranges.connection, path, size, plate, on_preview, on_project, on_supports)
        finally:
            ranges.close()
    ftp.close()
    # Modern internal storage uses the library's file channel, independently
    # of its camera. A proxy enables only this reader, never lib media downloads.
    from app.vendor.pybambu.media_sources import Tcp6000MediaSource
    class FileClient:
        ftp_enabled = True
        def __getattr__(self, key):
            return getattr(client, key)
    proxy = FileClient()
    source = Tcp6000MediaSource(proxy)
    try:
        files = source.list_files("model", [".3mf"])
        names = {p.rsplit("/", 1)[-1] for p in candidates}
        matches = [f for f in files if f.basename in names and f.size > 22]
        if not matches:
            if ram_error is not None:
                raise ram_error
            return None
        selected = max(matches, key=lambda f: f.sort_time)
        # The listing session is idle again and serves the first range.
        ranges, source = Tcp6000Ranges(lambda: Tcp6000MediaSource(proxy), selected, source), None
    finally:
        if source is not None:
            source.close()
    try:
        return read_ranges(ranges.open, selected.size, plate, on_preview, on_project, on_supports)
    finally:
        ranges.close()


def read_ranges(open_range, size, plate, on_preview=None, on_project=None, on_supports=None):
    remote = RemoteZip(open_range, size)
    try:
        with zipfile.ZipFile(remote) as archive:
            return archive_grams(archive, plate, on_preview, on_project, remote.allow, on_supports)
    finally:
        remote.close()


def job_key(device):
    pj = device.print_job
    raw = device.forge_raw.snapshot()
    return (raw.get("job_seq"), raw.get("job_id"), pj.subtask_name or "", pj.gcode_file or "")


class MaterialReader:
    def __init__(self):
        self.lock = threading.Lock()
        self.key = None
        self.value = None
        self.preview = None
        self.project = None
        self.title = None
        self.error = None
        self.due = 0
        self.future = None
        self.attempts = 0

    def poll(self, client):
        device = client.get_device()
        key = job_key(device)
        with self.lock:
            if key != self.key:
                self.key, self.value, self.attempts, self.error = key, None, 0, None
                self.preview = None
                self.project = None
                self.title = None
                self.due = time.monotonic() + 10
            device.forge_material = (key, self.value)
            device.forge_preview = (key, self.preview)
            device.forge_project = (key, self.project, self.title)
            device.forge_material_debug = {"attempts": self.attempts, "error": self.error,
                                           "pending": self.future is not None and not self.future.done()}
            if self.future is not None and not self.future.done():
                return
            if self.value is not None or self.attempts >= MAX_ATTEMPTS or time.monotonic() < self.due:
                return
            raw = device.forge_raw.snapshot()
            if raw.get("gcode_state") not in ("RUNNING", "PAUSE"):
                return
            self.attempts += 1
            self.due = time.monotonic() + 120
            self.future = _POOL.submit(self._read, client, key)

    def _read(self, client, key):
        def receive(data):
            from app.services import job_previews
            preview = job_previews.save(data)
            with self.lock:
                if self.key == key:
                    self.preview = preview
        def receive_project(summary):
            from app.services import job_previews
            project = job_previews.save_project(summary)
            with self.lock:
                if self.key == key:
                    self.project, self.title = project, summary.get("title")
        def receive_value(value):
            with self.lock:
                if self.key == key:
                    self.value = value
        error, supports = None, []
        try:
            value = read_bambu(client, key[2], key[3], on_preview=receive, on_project=receive_project,
                               on_value=receive_value, on_supports=lambda: supports.append(True))
        except Exception as exc:
            value, error = None, type(exc).__name__   # never the message: it may carry paths
            logger.debug("slice metadata read failed: %s", error)
        with self.lock:
            if self.key == key:
                self.value, self.error = value, error
                if value is None and error is None:
                    self.attempts = MAX_ATTEMPTS   # the job has no estimate: stop asking
        if supports and value is not None:
            # the grams and the card are out; the supports' count must not hold this pool's
            # threads (other printers' grams wait for them): their own queue
            _SUPPORT_POOL.submit(self._read_supports, client, key, value)

    def _read_supports(self, client, key, value):
        def receive_project(summary):
            from app.services import job_previews
            with self.lock:
                if self.key != key:
                    return
            project = job_previews.save_project(summary)
            with self.lock:
                if self.key == key:
                    self.project, self.title = project, summary.get("title")
        with self.lock:
            if self.key != key:      # another job by now (the queue may have been long)
                return
        try:
            read_supports(client, key[2], key[3], value, receive_project)
        except Exception as exc:
            logger.debug("support weight unavailable: %s", type(exc).__name__)


def bambu_grams(device):
    value = getattr(device, "forge_material", None)
    return grams(value[1]) if value and value[0] == job_key(device) else None


def bambu_preview(device):
    value = getattr(device, "forge_preview", None)
    return value[1] if value and value[0] == job_key(device) else None


def bambu_project(device):
    value = getattr(device, "forge_project", None)
    return value[1] if value and value[0] == job_key(device) else None


def bambu_title(device):
    """The parts' names, for jobs the firmware reports only as plate_1.gcode / project_file."""
    value = getattr(device, "forge_project", None)
    return value[2] if value and len(value) > 2 and value[0] == job_key(device) else None
