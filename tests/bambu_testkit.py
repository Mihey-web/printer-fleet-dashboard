"""Shared helpers for the Bambu tests (vendored pybambu + BambuCollector).

A plain module: pytest puts tests/ on sys.path (rootdir-relative "prepend"
import mode), so test files import it as ``import bambu_testkit as kit``.
"""
import json
import os
import socket
import struct
import threading
import time
from pathlib import Path
from types import SimpleNamespace

FIXTURES = Path(__file__).parent / "fixtures" / "bambu"
# The fake serials the sanitized fixtures carry (see fixtures/bambu/README.md).
FAKE_SERIALS = {
    "X1C": "00M00A000000000",
    "P2S": "22E00A000000000",
    "H2S": "09300A000000000",
    "P1S": "01P00A000000000",
}
LIB_THREAD_MARKERS = ("-Mqtt-", "-Watchdog-", "-Chamber-", "-Media-", "bambu-connect-")


def load_fixture(model: str) -> list:
    # Text + splitlines(): the Windows working tree may check the file out with CRLF.
    text = (FIXTURES / f"{model}.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def as_payload(msg) -> bytes:
    return bytes(msg) if isinstance(msg, (bytes, bytearray)) else json.dumps(msg).encode()


def deliver(collector, msg) -> bytes:
    """Hand one report to the client exactly as paho would; returns the raw bytes."""
    payload = as_payload(msg)
    topic = f"device/{collector.client._serial}/report"
    collector.client.on_message(None, None, SimpleNamespace(payload=payload, topic=topic))
    return payload


def merge(dst: dict, src: dict) -> dict:
    """Recursive update where lists are replaced: the lib keeps a missing key's
    previous value, which matters for the P1S delta pushes."""
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            merge(dst[k], v)
        else:
            dst[k] = v
    return dst


def lib_threads() -> list:
    return [t for t in threading.enumerate()
            if t.is_alive() and any(m in t.name for m in LIB_THREAD_MARKERS)]


def wait_until(pred, timeout: float = 3.0, interval: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return bool(pred())


class FakeClock:
    """Stands in for bambu_collector._monotonic."""

    def __init__(self, start: float = 1000.0):
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class StubPaho:
    """A connected paho client that drops everything: the lib ignores reports
    while client.client is None, so offline replays plug this in."""

    def publish(self, topic, payload=None, *args, **kwargs):
        return SimpleNamespace(rc=0)

    def subscribe(self, topic, *args, **kwargs):
        return (0, 1)

    def disconnect(self, *args, **kwargs):
        return 0

    def loop_stop(self, *args, **kwargs):
        return 0


class FakePaho:
    """paho.mqtt.client.Client stand-in that records what the lib does with it.

    loop_forever() blocks until disconnect(), like paho's. Tests drive the
    callbacks with fire_connect / fire_message / fire_disconnect.
    """

    instances: list = []
    tls_delay = 0.0     # seconds tls_set_context() sleeps (a slow connect bridge)
    tls_error = None    # exception tls_set_context() raises (a failing bridge)

    @classmethod
    def reset(cls) -> None:
        cls.instances = []
        cls.tls_delay = 0.0
        cls.tls_error = None

    def __init__(self, client_id=None, protocol=None, clean_session=None, **kwargs):
        self.client_id = client_id
        self.on_connect = self.on_disconnect = self.on_message = self.on_connect_fail = None
        self._keepalive = 60
        self.connect_calls = []
        self.published = []
        self.subscribed = []
        self.connected = False
        self.tls_ctx = None
        self.tls_insecure = None
        self.reconnect_delay = None
        self.reconnect_delay_calls = []
        self.auth = None
        self.stopped = threading.Event()
        FakePaho.instances.append(self)

    # --- paho API used by the lib ---
    def enable_logger(self, *args, **kwargs):
        pass

    def reconnect_delay_set(self, min_delay=1, max_delay=120):
        # Real paho also restarts the back-off at min_delay on every call.
        self.reconnect_delay = (min_delay, max_delay)
        self.reconnect_delay_calls.append(self.reconnect_delay)

    def tls_set_context(self, context=None):
        if FakePaho.tls_delay:
            time.sleep(FakePaho.tls_delay)
        if FakePaho.tls_error is not None:
            raise FakePaho.tls_error
        self.tls_ctx = context

    def tls_insecure_set(self, value):
        self.tls_insecure = value

    def username_pw_set(self, username, password=None):
        self.auth = (username, password)

    def connect(self, host, port=1883, keepalive=60, **kwargs):
        self.connect_calls.append((host, port, keepalive))
        self._keepalive = keepalive

    def loop_forever(self, timeout=1.0, retry_first_connection=False):
        self.stopped.wait()

    def disconnect(self, *args, **kwargs):
        was_connected = self.connected
        self.connected = False
        self.stopped.set()
        if was_connected and self.on_disconnect:
            self.on_disconnect(self, None, 0)   # paho reports a clean disconnect
        return 0

    def loop_stop(self, *args, **kwargs):
        return 0

    def subscribe(self, topic, *args, **kwargs):
        self.subscribed.append(topic)
        return (0, 1)

    def publish(self, topic, payload=None, *args, **kwargs):
        self.published.append((topic, payload))
        return SimpleNamespace(rc=0 if self.connected else 4)

    # --- test drivers ---
    def fire_connect(self, rc: int = 0) -> None:
        self.connected = rc == 0
        self.on_connect(self, None, {}, rc)

    def fire_message(self, msg) -> bytes:
        payload = as_payload(msg)
        self.on_message(self, None, SimpleNamespace(payload=payload, topic="device/x/report"))
        return payload

    def fire_disconnect(self, rc: int) -> None:
        self.connected = False
        self.on_disconnect(self, None, rc)

    def fire_connect_fail(self) -> None:
        # A reconnect that failed before any CONNACK (TCP refused or timed out):
        # paho calls on_connect_fail only, neither on_connect nor on_disconnect.
        self.connected = False
        if self.on_connect_fail:
            self.on_connect_fail(self, None)

    def published_commands(self) -> list:
        out = []
        for _topic, payload in self.published:
            body = json.loads(payload)
            for block in body.values():
                out.append(block.get("command"))
        return out


# --- a tiny MQTT 3.1.1 broker for the real-paho tests --------------------------

def _encode_remaining_length(n: int) -> bytes:
    out = bytearray()
    while True:
        byte, n = n % 128, n // 128
        out.append(byte | (0x80 if n else 0))
        if not n:
            return bytes(out)


class StubBroker:
    """Plain-TCP MQTT 3.1.1 broker on 127.0.0.1, just enough for paho.

    CONNECT -> CONNACK with the next code from ``connack_codes`` (the last one
    repeats; a refused connection is closed, as real brokers do). SUBSCRIBE ->
    SUBACK plus one report PUBLISH on the subscribed topic. PINGREQ ->
    PINGRESP, with arrival times recorded. Client PUBLISHes are ignored.

    go_down() simulates an outage: open connections are dropped and every new
    one is closed right after accept, until go_up(). Each attempt then fails
    at once on every OS (a closed port takes ~2 s to refuse on Windows), so
    the client's retry times are just its reconnect back-off.
    """

    def __init__(self, connack_codes=(0,), report=None, port=0):
        # port: 0 = any free one; a former broker's port = that printer switched back on.
        self.connack_codes = list(connack_codes)
        self.report = report if report is not None else {
            "print": {"command": "push_status", "msg": 0, "gcode_state": "IDLE", "wifi_signal": "-40dBm"}}
        self.connects = []        # (monotonic time, keepalive announced in CONNECT)
        self.connacks = []        # (monotonic time, code sent)
        self.pings = []           # monotonic time of each PINGREQ
        self.dropped = []         # monotonic time of each connection refused while down
        self._down = False
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if port and os.name != "nt":
            # Rebind despite the closed connections' TIME_WAIT (Windows allows it as is;
            # its SO_REUSEADDR means something else).
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", port))
        self._sock.listen(8)
        self.port = self._sock.getsockname()[1]
        self._closed = threading.Event()
        self._conns = []
        self._thread = threading.Thread(target=self._accept_loop, name="stub-broker", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._closed.set()
        try:
            self._sock.close()
        except OSError:
            pass
        self._drop_connections()

    def go_down(self) -> None:
        self._down = True
        self._drop_connections()

    def go_up(self) -> None:
        self._down = False

    def _drop_connections(self) -> None:
        for c in list(self._conns):
            try:
                c.close()
            except OSError:
                pass

    def _accept_loop(self) -> None:
        while not self._closed.is_set():
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            if self._down:
                self.dropped.append(time.monotonic())
                conn.close()
                continue
            self._conns.append(conn)
            threading.Thread(target=self._serve, args=(conn,), name="stub-broker-conn", daemon=True).start()

    @staticmethod
    def _read_exact(conn, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = conn.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("closed")
            buf += chunk
        return buf

    def _read_packet(self, conn):
        first = self._read_exact(conn, 1)[0]
        mult, length = 1, 0
        while True:
            byte = self._read_exact(conn, 1)[0]
            length += (byte & 0x7F) * mult
            mult *= 128
            if not byte & 0x80:
                break
        return first >> 4, self._read_exact(conn, length) if length else b""

    def _serve(self, conn) -> None:
        try:
            while not self._closed.is_set():
                kind, body = self._read_packet(conn)
                if kind == 1:  # CONNECT
                    name_len = struct.unpack("!H", body[:2])[0]
                    keepalive = struct.unpack("!H", body[2 + name_len + 2:2 + name_len + 4])[0]
                    self.connects.append((time.monotonic(), keepalive))
                    code = self.connack_codes.pop(0) if len(self.connack_codes) > 1 else self.connack_codes[0]
                    conn.sendall(b"\x20\x02\x00" + bytes([code]))
                    self.connacks.append((time.monotonic(), code))
                    if code != 0:
                        return
                elif kind == 8:  # SUBSCRIBE
                    packet_id = body[:2]
                    topic_len = struct.unpack("!H", body[2:4])[0]
                    topic = body[4:4 + topic_len]
                    conn.sendall(b"\x90\x03" + packet_id + b"\x00")
                    payload = json.dumps(self.report).encode()
                    var = struct.pack("!H", len(topic)) + topic + payload
                    conn.sendall(b"\x30" + _encode_remaining_length(len(var)) + var)
                elif kind == 12:  # PINGREQ
                    self.pings.append(time.monotonic())
                    conn.sendall(b"\xd0\x00")
                elif kind == 14:  # DISCONNECT
                    return
        except (OSError, ConnectionError):
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass
