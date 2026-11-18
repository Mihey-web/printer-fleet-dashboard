"""Moonraker, Klipper's own HTTP API, on a Creality printer (port 7125, no login).

Live 16.11.2026: the K2 Plus serves it (file_manager, history, job_queue, klippy
ready); the K1C and the Ender-5 Max do not, they stay on the WebSocket :9999 client
alone (app/clients/creality_client.py). So it is detected per printer, by its host:

* known(host) never waits: the cached answer (True / False), or None while the
  first probe runs in the background — the poll loop and the normalizer use it;
* available(host) waits for one probe at most PROBE_TIMEOUT_S — the API threads
  (the file list, a command) use it.
An answer is kept RECHECK_S, then checked again in the background (the old one
stays meanwhile).

Every request has a bounded timeout; errors are MoonrakerError (a short English
reason for the log, never shown as is).
"""
import http.client
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

PORT = 7125
PROBE_TIMEOUT_S = 3.0
HTTP_TIMEOUT_S = 8.0
TRANSFER_TIMEOUT_S = 60.0
RECHECK_S = 600.0
CHUNK = 65536
JSON_LIMIT = 4 * 1024 * 1024

_open = urllib.request.urlopen              # tests replace these two
_connection = http.client.HTTPConnection
_monotonic = time.monotonic

_lock = threading.Lock()
_cache: Dict[str, tuple] = {}               # host -> (monotonic of the answer, bool)
_probing: set = set()

_BAD_PATH = re.compile(r"[\x00-\x1f\\]")


class MoonrakerError(Exception):
    pass


def _base(host: str) -> str:
    return "http://%s:%d" % (host, PORT)


def check_path(rel: Any) -> str:
    """A path inside the gcodes root as Moonraker lists it ("a.gcode", "dir/b.gcode")."""
    if not isinstance(rel, str) or not rel or rel.startswith("/") or _BAD_PATH.search(rel) \
            or any(part in ("", ".", "..") for part in rel.split("/")):
        raise MoonrakerError("bad path")
    return rel


def request(host: str, method: str, path: str, query: Optional[Dict[str, Any]] = None,
            timeout: float = HTTP_TIMEOUT_S) -> Any:
    """One JSON request; Moonraker's "result" (MoonrakerError on any failure)."""
    url = _base(host) + path + ("?" + urllib.parse.urlencode(query, doseq=True) if query else "")
    req = urllib.request.Request(url, method=method, headers={"Accept": "application/json"})
    if method != "GET":
        req.data = b""
    try:
        with _open(req, timeout=timeout) as response:
            body = response.read(JSON_LIMIT + 1)
    except urllib.error.HTTPError as e:
        raise MoonrakerError("HTTP %s" % e.code)
    except Exception as e:
        raise MoonrakerError(type(e).__name__)
    if len(body) > JSON_LIMIT:
        raise MoonrakerError("reply too large")
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise MoonrakerError("not json")
    if not isinstance(data, dict) or "result" not in data:
        raise MoonrakerError("no result")
    return data["result"]


# --- detection ---------------------------------------------------------------------------

def probe(host: str) -> bool:
    """Whether Moonraker answers on the host (GET /server/info), cached."""
    try:
        info = request(host, "GET", "/server/info", timeout=PROBE_TIMEOUT_S)
        ok = isinstance(info, dict) and "klippy_state" in info
    except MoonrakerError:
        ok = False
    with _lock:
        _cache[host] = (_monotonic(), ok)
        _probing.discard(host)
    return ok


def _probe_later(host: str) -> None:
    with _lock:
        if host in _probing:
            return
        _probing.add(host)
    threading.Thread(target=probe, args=(host,), name="moonraker-probe", daemon=True).start()


def known(host: Optional[str]) -> Optional[bool]:
    """The cached answer, never waiting (a stale or missing one is re-probed in the background)."""
    if not host:
        return None
    with _lock:
        hit = _cache.get(host)
    if hit is None or _monotonic() - hit[0] > RECHECK_S:
        _probe_later(host)
    return hit[1] if hit is not None else None


def available(host: Optional[str]) -> bool:
    """The cached answer, or one probe now (at most PROBE_TIMEOUT_S)."""
    if not host:
        return False
    with _lock:
        hit = _cache.get(host)
    if hit is None:
        return probe(host)
    return bool(known(host))           # a stale answer stays meanwhile, re-probed in the background


def forget(host: Optional[str] = None) -> None:
    """Drop the cache (tests; a printer's host changed)."""
    with _lock:
        if host is None:
            _cache.clear()
        else:
            _cache.pop(host, None)


# --- files --------------------------------------------------------------------------------

def roots(host: str) -> Dict[str, str]:
    """{"gcodes": "/mnt/UDISK/printer_data/gcodes", ...}: each root's directory on the printer."""
    out = {}
    for r in request(host, "GET", "/server/files/roots") or []:
        if isinstance(r, dict) and isinstance(r.get("name"), str) and isinstance(r.get("path"), str):
            out[r["name"]] = r["path"]
    return out


def list_gcodes(host: str) -> List[Dict[str, Any]]:
    """Every file of the gcodes root, subfolders included: [{"path", "size", "modified"}]."""
    out = []
    for f in request(host, "GET", "/server/files/list", {"root": "gcodes"}) or []:
        if not isinstance(f, dict):
            continue
        try:
            rel = check_path(f.get("path"))
        except MoonrakerError:
            continue
        out.append({"path": rel, "size": f.get("size"), "modified": f.get("modified")})
    return out


def _file_url(host: str, rel: str) -> str:
    return _base(host) + "/server/files/gcodes/" + urllib.parse.quote(check_path(rel))


def open_file(host: str, rel: str, byte_range: Optional[str] = None):
    """The file as a response to read (and close): whole, or byte_range ("bytes=-4096")."""
    req = urllib.request.Request(_file_url(host, rel), method="GET",
                                 headers={"Range": byte_range} if byte_range else {})
    try:
        return _open(req, timeout=TRANSFER_TIMEOUT_S)
    except urllib.error.HTTPError as e:
        raise MoonrakerError("HTTP %s" % e.code)
    except Exception as e:
        raise MoonrakerError(type(e).__name__)


def download(host: str, rel: str, local: str, size: Optional[int] = None,
             progress: Optional[Callable[[int], None]] = None) -> None:
    """The file into `local`; never longer than its listed size."""
    req = urllib.request.Request(_file_url(host, rel), method="GET")
    try:
        with _open(req, timeout=TRANSFER_TIMEOUT_S) as response, open(local, "wb") as out:
            while True:
                block = response.read(CHUNK)
                if not block:
                    break
                if size is not None and out.tell() + len(block) > size:
                    raise MoonrakerError("longer than listed")
                out.write(block)
                if progress:
                    progress(len(block))
    except MoonrakerError:
        raise
    except urllib.error.HTTPError as e:
        raise MoonrakerError("HTTP %s" % e.code)
    except Exception as e:
        raise MoonrakerError(type(e).__name__)
    if size is not None and os.path.getsize(local) != size:
        raise MoonrakerError("short download")


def upload(host: str, name: str, local: str, progress: Optional[Callable[[int], None]] = None) -> None:
    """POST /server/files/upload (multipart «file», root gcodes) into the gcodes root."""
    name = check_path(name)
    if "/" in name:
        raise MoonrakerError("bad name")
    boundary = "forge" + uuid.uuid4().hex
    head = ('--%s\r\nContent-Disposition: form-data; name="root"\r\n\r\ngcodes\r\n'
            '--%s\r\nContent-Disposition: form-data; name="file"; filename="%s"\r\n'
            'Content-Type: application/octet-stream\r\n\r\n'
            % (boundary, boundary, name.replace('"', ""))).encode("utf-8")
    tail = ("\r\n--%s--\r\n" % boundary).encode()
    size = os.path.getsize(local)
    conn = _connection(host, PORT, timeout=TRANSFER_TIMEOUT_S)
    try:
        conn.putrequest("POST", "/server/files/upload")
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
                if progress:
                    progress(len(block))
        conn.send(tail)
        response = conn.getresponse()
        status = response.status
        response.read(4096)
    except Exception as e:
        raise MoonrakerError(type(e).__name__)
    finally:
        conn.close()
    if status not in (200, 201):
        raise MoonrakerError("upload refused: HTTP %s" % status)


def delete(host: str, rel: str) -> None:
    """DELETE /server/files/gcodes/<path>: one file."""
    request(host, "DELETE", "/server/files/gcodes/" + urllib.parse.quote(check_path(rel)))


# --- printer state, G-code --------------------------------------------------------------------

def query(host: str, objects: List[str]) -> Dict[str, Any]:
    """/printer/objects/query of these objects (all their fields): {name: fields}."""
    result = request(host, "GET", "/printer/objects/query", {o: "" for o in objects})
    status = result.get("status") if isinstance(result, dict) else None
    return status if isinstance(status, dict) else {}


def gcode_store(host: str, count: int = 30) -> List[Dict[str, Any]]:
    """The console's last lines: [{"message", "time", "type"}], oldest first."""
    result = request(host, "GET", "/server/gcode_store", {"count": int(count)})
    lines = result.get("gcode_store") if isinstance(result, dict) else None
    return [x for x in lines or [] if isinstance(x, dict) and isinstance(x.get("message"), str)]


def run_script(host: str, script: str, timeout: float) -> Any:
    """POST /printer/gcode/script: Moonraker answers when the script has run (or failed)."""
    return request(host, "POST", "/printer/gcode/script", {"script": script}, timeout=timeout)


def firmware_restart(host: str) -> Any:
    return request(host, "POST", "/printer/firmware_restart")
