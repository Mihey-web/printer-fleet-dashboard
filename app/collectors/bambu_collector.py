import asyncio
import functools
import json
import logging
import os
import socket
import ssl
import tempfile
import threading
import time
import uuid
from pathlib import Path

from app.vendor.pybambu import BambuClient
from app.vendor.pybambu import bambu_client as _vendor_bambu_client
from app.vendor.pybambu.const import LOGGER as _PYBAMBU_LOGGER

logger = logging.getLogger(__name__)

# pybambu 1.0.1 pinged every 5 s, so a powered-off printer was noticed in 5-10 s.
MQTT_KEEPALIVE = 5
# paho's reconnect back-off (s): the wait doubles from 1 s up to the cap and
# restarts after a successful login. Upstream caps it at 30 s, so a printer back
# from an outage could stay offline for 30 s more (1.0.1 retried every second);
# 5 s while the printer is off, unreachable or still booting, upstream's 30 s
# while it refuses the login.
RECONNECT_MAX_DELAY = 5
REFUSED_RECONNECT_MAX_DELAY = 30
# CONNACK codes of a refused login: 4 = bad user name or password, 5 = not
# authorised (wrong access code, LAN / Developer mode off). The others (1-3:
# protocol version, client id, server unavailable — a printer still booting)
# are retried like an outage.
LOGIN_REFUSED_CONNACK = (4, 5)
SESSION_REFUSED_CONNACK = {1: "protocol version", 2: "client id", 3: "server unavailable"}
# A refused login lasts until a login succeeds, or until this many attempts in
# a row end some other way (no answer, connection closed before its CONNACK,
# CONNACK 1-3): the printer has been switched off or rebooted. A single one is a
# flaky link and must not restart paho's wait at 1 s (see _set_reconnect_cap()).
# At the 30 s cap the second one comes up to ~1 min after a switch-off.
REFUSED_STREAK_END_AFTER = 2
# Delays (s) before BambuClient.connect() is re-run after it raised, or after the
# lib's listener thread ended on its own. Capped low: a failure here is local
# (TLS context, files) and retrying is cheap.
REBUILD_BACKOFF = (30, 60, 120)
_monotonic = time.monotonic  # indirection so tests can drive the backoff/rate-limit clock


def _noop() -> None:
    return None


def tls_verify_enabled(spec, model) -> bool:
    """FORGE_BAMBU_TLS_VERIFY: "*" = every Bambu printer, or a comma list of models."""
    items = {s.strip().upper() for s in (spec or "").split(",") if s.strip()}
    return "*" in items or (model or "").strip().upper() in items


@functools.lru_cache(maxsize=1)
def _bambu_ca_context() -> ssl.SSLContext:
    """Verify printer certificates against Bambu's CAs only (app/vendor/pybambu/certs).

    Upstream's create_local_ssl_context() starts from ssl.create_default_context(),
    so with the hostname check off it also accepts any publicly trusted
    certificate. Residual risk: any certificate signed by Bambu's CAs is accepted
    for any printer (the name can't be pinned, see below).
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    # Printers are addressed by IP and their certificate names the serial, so the
    # name can't be checked; paho also derives tls_insecure from this flag.
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT   # as upstream: Bambu CAs lack the key-usage extension
    ctx.maximum_version = ssl.TLSVersion.TLSv1_2  # as upstream: P2S fw 01.02 hangs on a TLS 1.3 ClientHello
    certs = Path(_vendor_bambu_client.__file__).with_name("certs")
    for cert in sorted(certs.glob("*.cert")):
        ctx.load_verify_locations(cafile=str(cert))
    return ctx


def _client_config(host, access_code, serial, device_type, tls_verify=False) -> dict:
    return {
        # Registry columns serial/access_code/model are nullable; the lib calls
        # str methods on them, and a failing BambuCollector() kills build_collectors().
        "host": host or "",
        "access_code": access_code or "",
        "serial": serial or "",
        "device_type": device_type or "unknown",   # lib upper-cases; get_version auto-detects later
        "local_mqtt": True,                         # LAN broker host:8883, user bblp
        "auth_token": "", "username": "", "email": "", "region": "",  # every cloud path is a no-op
        "enable_camera": False,      # P1/A1 JPEG stream on :6000: unused, heavy, collides with OBS bridge
        "force_ip": True,            # keep `host`; don't adopt the print.net.info IP (bridged printers)
        "user_language": "ru",       # HMS / print_error text language (lib default is 'pt')
        "timelapse_cache_count": 0,  # never download timelapses
        # The lib prunes this media cache on every connect (deletes old videos /
        # 3MFs / *.part). Nothing is ever cached here (FTP is off); a unique,
        # unguessable path that is never created keeps a shared /tmp out of reach.
        "file_cache_path": os.path.join(tempfile.gettempdir(), f"forge-bambu-cache-{uuid.uuid4().hex}"),
        # Default: pybambu 1.0.1 parity (encrypted, certificate not checked);
        # opt-in verification per model via FORGE_BAMBU_TLS_VERIFY.
        "disable_ssl_verify": not tls_verify,
    }


def _tee(serial, payload) -> None:
    try:
        from app.services import printer_commands
        printer_commands.note_local_reply(serial, payload)
    except Exception:
        pass


class _ForgeBambuClient(BambuClient):
    """Vendored BambuClient plus what the dashboard needs on top of it."""

    def __init__(self, config: dict):
        super().__init__(config)
        # There is no config key for FTP; every FTP / TCP-6000 gate (model and
        # timelapse download, the media probe in connect()) reads ftp_enabled.
        self._enable_ftp = False
        # "MOCK-<x>" serials are upstream demo printers fed from mock_data/<serial>.json;
        # neither the data nor the feature exists here.
        self._mock = False
        # connect() prunes the media cache; there is none (see _client_config).
        print_job = self._device.print_job
        print_job.prune_print_history_files = _noop
        print_job.prune_timelapse_files = _noop
        # Set while the printer refuses the MQTT login (CONNACK 4 / 5); see on_disconnect().
        self._login_refused = False
        self._unanswered = 0            # attempts in a row that ended otherwise, during a refused login
        self._session_refused = None    # CONNACK 1-3 that ended the last attempt, else None
        self._connack_code = None       # code of the last refused CONNACK (on_connect)
        self._reconnect_cap = None      # (paho client, max delay) last set; see _set_reconnect_cap()

    @property
    def local_tls_context(self):
        if self._disable_ssl_verify:
            return super().local_tls_context
        return _bambu_ca_context()

    async def connect(self, callback):
        # Every call builds a new paho client and listener, whose first connects
        # fail inside the lib's own retry loop without any callback: a refusal
        # seen by the previous client must not stick.
        self._login_refused = False
        self._unanswered = 0
        self._session_refused = None
        await super().connect(callback)
        client = self.client
        if client is not None:
            # paho reports a failed reconnect (TCP refused or timed out, TLS
            # error) through on_connect_fail only. Set after the listener has
            # started, which is early enough: paho reconnects only after a
            # back-off wait of at least 1 s.
            client.on_connect_fail = self.on_connect_fail

    def _set_reconnect_cap(self, client_, max_delay: int) -> None:
        # Every reconnect_delay_set() call restarts paho's wait at 1 s, so set
        # the cap only when it changes (a new paho client starts with the lib's
        # 30 s): within a streak of failed attempts the wait keeps doubling.
        if self._reconnect_cap != (client_, max_delay):
            self._reconnect_cap = (client_, max_delay)
            client_.reconnect_delay_set(min_delay=1, max_delay=max_delay)

    def _end_refused_login(self, client_) -> None:
        """No refused-login state; paho retries with the outage back-off (1 -> 5 s)."""
        self._login_refused = False
        self._unanswered = 0
        self._set_reconnect_cap(client_, RECONNECT_MAX_DELAY)

    def _attempt_failed(self, client_, session_refused=None) -> None:
        """An attempt ended without a refused login: no answer, closed before its
        CONNACK, or CONNACK 1-3 (session_refused). Within a refused login only
        REFUSED_STREAK_END_AFTER of them in a row end it: a single one keeps the
        30 s cap and the streak's single ERROR + event."""
        self._session_refused = session_refused
        if self._login_refused:
            self._unanswered += 1
            if self._unanswered < REFUSED_STREAK_END_AFTER:
                return
        self._end_refused_login(client_)

    def on_connect(self, client_, userdata, flags, result_code, properties=None):
        # The lib hard-codes keepalive=30 (a dead link is noticed after 30-60 s).
        # paho reads _keepalive on every loop tick and in reconnect(); its public
        # `keepalive` setter refuses an open connection, so set the attribute
        # (paho-mqtt 2.1.0; tests/test_bambu_adapter.py guards the internals).
        if hasattr(client_, "_keepalive"):
            client_._keepalive = MQTT_KEEPALIVE
        if result_code != 0:
            # A refused CONNACK: paho closes the connection and calls
            # on_disconnect(5) next, which does the bookkeeping by this code.
            # Upstream would mark the client connected and subscribe / request a
            # push first.
            self._connack_code = result_code
            return
        self._session_refused = None
        self._end_refused_login(client_)
        super().on_connect(client_, userdata, flags, result_code, properties)

    def on_connect_fail(self, client_, userdata):
        # A reconnect failed before any CONNACK (TCP refused or timed out, TLS
        # error): the printer is off or unreachable, at least for now.
        self._attempt_failed(client_)

    def on_disconnect(self, client_, userdata, result_code):
        if result_code != 5:
            # The link dropped, or a connection closed before its CONNACK.
            self._attempt_failed(client_)
            super().on_disconnect(client_, userdata, result_code)
            return
        # paho reports every refused CONNACK as rc=5 (its code went to
        # on_connect just before). Upstream then calls client.disconnect(), which
        # ends loop_forever and the listener thread for good: keep paho's own
        # reconnect loop running instead, like 1.0.1 did.
        if self._connack_code in LOGIN_REFUSED_CONNACK:
            # Wrong access code, or LAN / Developer mode off: usually fixed on the
            # printer with the same code. Back-off 1 -> 30 s; the rest is the
            # lib's bookkeeping, one ERROR + event per refusal streak.
            self._set_reconnect_cap(client_, REFUSED_RECONNECT_MAX_DELAY)
            self._unanswered = 0
            self._session_refused = None
            if not self._login_refused:
                _PYBAMBU_LOGGER.error(
                    "On Disconnect: the printer refused the MQTT login (CONNACK %s): check the "
                    "access code and LAN / Developer mode. Retrying.", self._connack_code)
                self.callback("event_printer_access_denied")
            self._login_refused = True
        else:
            # Server unavailable (a printer still booting) and the like: nothing
            # is wrong with the login, so retry as after an outage (within a
            # refused login: see _attempt_failed()).
            _PYBAMBU_LOGGER.debug("On Disconnect: the MQTT broker turned the login away (CONNACK %s)",
                                  self._connack_code)
            self._attempt_failed(client_, self._connack_code)
        self._last_error_code = result_code
        self._on_disconnect()

    def on_message(self, client, userdata, message):
        # The lib parses reports only for telemetry and drops command replies
        # (result/reason). Tee every raw payload to printer_commands afterwards,
        # even when parsing failed.
        try:
            super().on_message(client, userdata, message)
        finally:
            _tee(getattr(self, "_serial", None), message.payload)

    def publish(self, msg) -> bool:
        # Upstream's body with the paho client read once: close() and the
        # rebuild bridge set self.client to None / a new client on another
        # thread at any moment. No client (before connect(), after close())
        # means "not sent" instead of AttributeError.
        client = self.client
        if client is None:
            return False
        topic = f"device/{self._serial}/request"
        if client.publish(topic, json.dumps(msg)).rc == 0:
            _PYBAMBU_LOGGER.debug("Sent %s to topic %s", msg, topic)
            return True
        _PYBAMBU_LOGGER.error("Failed to send message to topic %s", topic)
        return False


class _RateLimitFilter(logging.Filter):
    """Let one record per (call site, thread) through every `window` seconds.

    The vendored lib logs some failures at ERROR on every MQTT message (a payload
    that breaks parsing) or every second (listener connect errors such as a TLS
    failure), and paho logs through a single call site. Keying on the thread
    keeps one printer's failure from hiding another's: the lib's threads are
    named "<model>-Mqtt-<native id>", and the collector logs which label owns
    which id. The thread name is prefixed to the message (the app's log format
    has no thread field), and the first occurrence is always shown.
    """

    def __init__(self, window: float = 600.0):
        super().__init__()
        self._window = window
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self._last = {}
            self._suppressed = {}
            self._pruned_at = None

    def _prune(self, now: float) -> None:
        # Thread names change whenever a listener is rebuilt: forget keys that
        # stayed quiet for two windows so the maps can't grow without bound.
        if self._pruned_at is not None and now - self._pruned_at < self._window:
            return
        self._pruned_at = now
        for key in [k for k, last in self._last.items() if now - last >= 2 * self._window]:
            del self._last[key]
            self._suppressed.pop(key, None)

    def filter(self, record: logging.LogRecord) -> bool:
        thread = record.threadName or "?"
        key = (record.name, record.pathname, record.lineno, thread)
        now = _monotonic()
        with self._lock:
            self._prune(now)
            last = self._last.get(key)
            if last is not None and now - last < self._window:
                self._suppressed[key] = self._suppressed.get(key, 0) + 1
                return False
            self._last[key] = now
            repeats = self._suppressed.pop(key, 0)
        prefix = f"[{thread}] "
        suffix = f" [+{repeats} similar suppressed]" if repeats else ""
        if record.args:  # the message is %-formatted later: keep a '%' in a label literal
            prefix, suffix = prefix.replace("%", "%%"), suffix.replace("%", "%%")
        record.msg = f"{prefix}{record.msg}{suffix}"
        return True


def quiet_vendor_logging(window: float = 600.0) -> None:
    """ERROR-only, rate-limited output from the vendored lib and paho (idempotent)."""
    for lg in (_PYBAMBU_LOGGER, logging.getLogger("paho.mqtt.client")):
        lg.setLevel(logging.ERROR)
        if not any(isinstance(f, _RateLimitFilter) for f in lg.filters):
            lg.addFilter(_RateLimitFilter(window))


class BambuCollector:
    def __init__(self, label: str, host: str, access_code: str, serial: str, device_type: str = "X1C",
                 tls_verify: bool = False):
        self.label = label
        self.host = host
        self.client = _ForgeBambuClient(_client_config(host, access_code, serial, device_type, tls_verify))
        self.connected = False          # connect() requested; from then on the lib's listener owns (re)connection
        self._connect_lock = threading.Lock()
        self._bridge = None             # thread running BambuClient.connect() (first start or rebuild)
        self._bridge_error = None       # exception from the last bridge run
        self._rebuild_attempt = 0
        self._rebuild_due = None        # _monotonic() deadline for the next rebuild
        self._closed = False

    def _on_event(self, event: str) -> None:
        # Lib events arrive on the paho thread (~1/s); liveness is read from
        # device.info.online by fetch() instead.
        pass

    def _start_bridge(self, rebuild: bool) -> None:   # caller holds _connect_lock
        bridge = threading.Thread(target=self._bridge_run, args=(rebuild,), daemon=True,
                                  name=f"bambu-connect-{self.label}")
        self._bridge = bridge
        bridge.start()

    def _bridge_run(self, rebuild: bool) -> None:
        # BambuClient.connect() is a coroutine (TLS setup, cache prune and media
        # probe in an executor, then it starts the MQTT listener thread and
        # returns). Run it on a private loop in this short-lived thread, so no
        # poll worker ever waits for it.
        try:
            if rebuild:
                self.client.disconnect()   # joins the old listener + watchdog, client.client = None
            asyncio.run(self.client.connect(self._on_event))
        except Exception as e:
            self._bridge_error = e
            logger.warning("[%s] MQTT connect failed: %s", self.label, e)
            return
        self._bridge_error = None
        if self._closed:
            # close() gave up waiting for this bridge: don't leave a listener behind.
            self.client.disconnect()
            return
        listener = getattr(self.client, "_mqtt", None)
        # Lib threads log as "<model>-Mqtt-<native id>": map the id to the label.
        logger.info("[%s] MQTT listener started (thread *-Mqtt-%s)", self.label,
                    getattr(listener, "native_id", "?"))

    def connect(self):
        # Idempotent: warm-up, fetch() and send_command() may all call it. Every
        # BambuClient.connect() creates a NEW paho client + listener thread, so a
        # second session must never be started while one exists.
        with self._connect_lock:
            if self.connected or self._closed:
                return
            self._start_bridge(rebuild=False)
            self.connected = True

    def close(self) -> None:
        """Stop the listener and watchdog; idempotent. Used on shutdown and by tests."""
        with self._connect_lock:
            self._closed = True
            bridge = self._bridge
        if bridge is not None:
            bridge.join(timeout=10)
        try:
            self.client.disconnect()   # paho on_disconnect(0) -> online False; joins the lib threads
        except Exception:
            logger.debug("[%s] MQTT disconnect failed", self.label, exc_info=True)

    def is_reachable(self) -> bool:
        for port in (8883, 8884):
            try:
                with socket.create_connection((self.host, port), timeout=1.5):
                    return True
            except OSError:
                continue
        return False

    def send_command(self, command: str, param: str = None) -> dict:
        if not self.connected:
            try:
                self.connect()
            except Exception as e:
                return {"success": False, "detail": f"Not connected: {e}"}

        if not getattr(self.client, "connected", False):
            return {"success": False, "detail": "MQTT not connected (printer offline or not in LAN mode)"}

        if command == "gcode_line" and param is not None and not param.endswith("\n"):
            param = param + "\n"

        payload = {"print": {"sequence_id": "0", "command": command}}
        if param is not None:
            payload["print"]["param"] = param

        try:
            ok = self.client.publish(payload)
            return {"success": bool(ok), "detail": "Command sent" if ok else "Publish failed"}
        except Exception as e:
            # Don't flip connected=False here. The lib's listener thread owns
            # reconnection of the existing mqtt.Client; a fresh connect() would
            # spawn a DUPLICATE client + listener alongside the still-live one
            # (see connect()'s note). The publish error is surfaced to the caller;
            # real liveness is judged by info.online in fetch().
            return {"success": False, "detail": str(e)}

    def _listener_dead(self) -> bool:
        """True when nothing will bring telemetry back without a new connect():
        the last BambuClient.connect() raised, or the lib's listener thread ended."""
        listener = getattr(self.client, "_mqtt", None)
        if listener is not None and listener.is_alive():
            return False
        bridge = self._bridge
        if bridge is not None and bridge.is_alive():
            return False                   # start / rebuild in progress
        return self._bridge_error is not None or listener is not None

    def _schedule_rebuild(self) -> None:
        now = _monotonic()
        with self._connect_lock:
            if self._closed or (self._bridge is not None and self._bridge.is_alive()):
                return
            if self._rebuild_due is None:
                delay = REBUILD_BACKOFF[min(self._rebuild_attempt, len(REBUILD_BACKOFF) - 1)]
                self._rebuild_due = now + delay
                logger.warning("[%s] MQTT listener is down (%s); reconnecting in %d s", self.label,
                               "connect failed" if self._bridge_error is not None else "listener exited",
                               delay)
                return
            if now < self._rebuild_due:
                return
            self._rebuild_attempt += 1
            self._rebuild_due = None
            self._bridge_error = None
            self._start_bridge(rebuild=True)

    def fetch(self):
        if self._closed:
            raise ConnectionError("collector closed")
        try:
            device = self.client.get_device()
        except Exception:
            device = None

        # The lib creates the Device in the client constructor, so get_device()
        # never returns None: after the printer powers off it keeps handing out
        # the last cached telemetry. Liveness is device.info.online — cleared by
        # paho's on_disconnect (keepalive 5 s: a dead link is noticed in ~5-10 s)
        # and by the lib's 60 s no-data watchdog. Stale data must not go up,
        # otherwise a powered-off printer stays in its last status forever.
        if device is not None and getattr(getattr(device, "info", None), "online", False):
            self._rebuild_attempt = 0
            self._rebuild_due = None
            return device

        if self.connected:
            if self._listener_dead():
                self._schedule_rebuild()
                why = "MQTT connect failed" if self._bridge_error is not None else "MQTT listener stopped"
                raise ConnectionError(f"MQTT telemetry lost ({why}; reconnect scheduled)")
            if getattr(self.client, "_login_refused", False):
                raise ConnectionError(
                    "MQTT login refused by the printer (access code or LAN / Developer mode); retrying")
            code = getattr(self.client, "_session_refused", None)
            if code is not None:
                # Powered on and answering, e.g. still booting (CONNACK 3).
                raise ConnectionError(
                    f"MQTT broker on the printer answered but turned the session away "
                    f"(CONNACK {code}: {SESSION_REFUSED_CONNACK.get(code, 'refused')}); retrying")
            # The listener (paho loop_forever) reconnects by itself; a second
            # connect() would create a duplicate client + thread on every poll.
            raise ConnectionError("MQTT telemetry lost (printer powered off or unreachable)")

        if not self.is_reachable():
            raise ConnectionError(f"TCP unreachable at {self.host}:8883/8884")

        try:
            self.connect()
        except Exception as e:
            self.connected = False
            raise ConnectionError(f"MQTT connect failed: {e}")

        # connect() is asynchronous: the first telemetry arrives a bit later. This
        # cycle counts as failed; poll_loop's grace period covers the gap and the
        # next poll sees info.online=True.
        raise ConnectionError("MQTT connecting — telemetry not received yet")
