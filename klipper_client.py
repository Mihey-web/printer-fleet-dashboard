import json
import math
import logging
import re
import websocket
import threading
import time
import warnings

logger = logging.getLogger(__name__)

# Seconds an acknowledged command (run_action) waits for Moonraker's reply.
REPLY_TIMEOUT = 8.0
# A pending request without a reply is dropped after this long (the next
# registration prunes it); also the age after which a discovery counts as lost.
_PENDING_MAX_AGE = 60.0
# Pin names that are safe to interpolate into G-code (SET_PIN PIN=...).
_PIN_NAME = re.compile(r"^[A-Za-z0-9_]+$")
_RPC_METHODS = {"pause": "printer.print.pause", "resume": "printer.print.resume",
                "stop": "printer.print.cancel"}


# Words of a pin name that make it a light, by preference (pick_light_pin).
_LIGHT_WORDS = {"light": 1, "lights": 1, "caselight": 1, "led": 2, "leds": 2, "lamp": 3, "lamps": 3}
_CAMEL_WORD = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+")


def _name_words(name):
    """Lower-case words of a pin name, split on '_', digits and camelCase:
    'chamber_light2' -> chamber, light; 'LEDStrip' -> led, strip."""
    return [w.lower() for part in re.split(r"[_\d]+", name) for w in _CAMEL_WORD.findall(part)]


def pick_light_pin(objects):
    """The output_pin that drives a light: caselight / case_light first, then
    a name with the word 'light', then 'led', then 'lamp' (ties: by name).
    Whole words only — 'motors_enabled', 'hotend_cooled' or 'flight_mode' is
    no light. Names outside [A-Za-z0-9_] are ignored; None when there is no
    such pin."""
    best = None
    for o in objects or ():
        if not isinstance(o, str) or not o.startswith("output_pin "):
            continue
        name = o.split(None, 1)[1].strip()
        if not _PIN_NAME.match(name):
            continue
        if name.lower() in ("caselight", "case_light"):
            rank = 0
        else:
            ranks = [_LIGHT_WORDS[w] for w in _name_words(name) if w in _LIGHT_WORDS]
            if not ranks:
                continue
            rank = min(ranks)
        if best is None or (rank, name) < best:
            best = (rank, name)
    return best[1] if best else None


class KlipperClient:
    """Simple WebSocket client for Klipper via Moonraker"""

    def __init__(self, host, port, label):
        self.host = host
        self.port = port
        self.label = label
        self.ws = None
        self.connected = False
        self.data = {
            'state': 'unknown',
            'progress': 0,
            'remaining_time': 0,
            'print_time': 0,
            'nozzle_temp': 0,
            'bed_temp': 0,
            'chamber_temp': None,
            'target_nozzle_temp': None,
            'target_bed_temp': None,
            'current_layer': 0,
            'total_layers': 0,
            'filament_used_mm': None,
            'fan_speed_pct': None,
            'feedrate_pct': None,
            'stage': None,
        }
        self.thread = None
        self.request_id = 1
        # Timestamp of the last successfully-parsed message. Used by get_data() to
        # detect a hung socket (TCP stalled with no FIN, so on_close never fires)
        # and force a disconnect instead of serving stale telemetry as fresh.
        self.last_message_ts = 0.0
        # False until the first real printer-status message is parsed. self.data is
        # pre-seeded with a full default dict (state='unknown', …), so without this
        # flag get_data() would serve those defaults for a Moonraker that connected
        # but never sent telemetry — showing a dead/empty host as online 'unknown'.
        self._got_telemetry = False
        # Guards self.data (WS thread vs reader thread) and request_id (WS thread
        # subscribe vs API thread run_action).
        self._lock = threading.Lock()
        self._stop_reconnect = threading.Event()
        self._reconnect_thread = None
        self._reconnect_delay = 5
        self._max_reconnect_delay = 60
        # Serializes connect(): both fetch() (poll thread) and the on_close/on_error
        # reconnect loop can call it. Without this the two drivers race and spawn
        # duplicate run_forever threads / leaked sockets. Mirrors the Bambu collector.
        self._connect_lock = threading.Lock()
        # Light: the output_pin found by discovery (None = not looked up yet,
        # "" = looked up, none found) and its configured scale (SET_PIN VALUE).
        self.light_pin = None
        self.light_scale = 1.0
        # JSON-RPC requests awaiting their reply: id -> {"event", "result",
        # "error", "on_result", "ts"}; guarded by self._lock.
        self._pending = {}
        # Set by parse_status when klippy becomes ready; on_message then runs
        # the discovery outside the lock. _discover_ts: start of the discovery
        # in flight (0 = none), so a second one is not sent meanwhile.
        self._discover_needed = False
        self._discover_ts = 0.0

    def _next_id(self):
        with self._lock:
            rid = self.request_id
            self.request_id += 1
            return rid

    def connect(self):
        with self._connect_lock:
            # Idempotent: if a concurrent caller already brought the socket up,
            # don't tear it down and rebuild — that would orphan the live thread.
            if self.connected:
                return self.connected
            try:
                # Close any previous socket before replacing it so the old run_forever
                # thread's TCP connection doesn't leak on every reconnect.
                if self.ws is not None:
                    try:
                        self.ws.close()
                    except Exception:
                        pass
                    # close() only signals run_forever to stop; it does not block. Join
                    # the old thread so its TCP socket is actually released before we
                    # open a new one — otherwise FDs leak on every reconnect on a
                    # flapping network.
                    old_thread = self.thread
                    if old_thread is not None and old_thread.is_alive():
                        old_thread.join(timeout=5)
                ws_url = f"ws://{self.host}:{self.port}/websocket"
                self.ws = websocket.WebSocketApp(
                    ws_url,
                    on_message=self.on_message,
                    on_error=self.on_error,
                    on_close=self.on_close,
                    on_open=self.on_open
                )
                # ping_interval/timeout let the library detect a dead peer that stopped
                # responding without sending a FIN, instead of blocking in recv forever.
                self.thread = threading.Thread(
                    target=self.ws.run_forever,
                    kwargs={'ping_interval': 15, 'ping_timeout': 10},
                    daemon=True,
                )
                self.thread.start()
                time.sleep(2)
                return self.connected
            except Exception as e:
                logger.error("[%s] Ошибка подключения WebSocket: %s", self.label, e)
                return False

    def on_open(self, ws):
        logger.info("[%s] WebSocket подключен", self.label)
        self.connected = True
        # A discovery sent on the previous socket will never be answered.
        self._discover_ts = 0.0
        # Подписываемся на статус принтера
        self.subscribe_to_status()
        self._discover()

    def subscribe_to_status(self):
        # Subscribe to printer status updates via Moonraker WebSocket
        try:
            request = {
                "jsonrpc": "2.0",
                "method": "printer.objects.subscribe",
                "params": {
                    "objects": {
                     "print_stats": ["state", "print_duration", "filename", "total_duration", "filament_used", "info", "message"],
                     "display_status": ["progress", "message"],
                     "toolhead": ["position", "homed_axes", "stalls"],
                     "extruder": ["temperature", "target", "power", "pressure_advance"],
                     "heater_bed": ["temperature", "target", "power"],
                     "gcode_move": ["gcode_position", "speed_factor"],
                     "virtual_sdcard": ["progress", "file_path", "is_active", "file_size"],
                     "fan": ["speed"],
                     "webhooks": ["state", "state_message"],
                     "idle_timeout": ["state"],
                     "system_stats": ["sysload", "memavail", "cputime"],


                    }
                },
                "id": self._next_id()
            }
            pin = self.light_pin
            if pin:
                request["params"]["objects"]["output_pin " + pin] = ["value"]
            self.ws.send(json.dumps(request))

            query_request = {
                "jsonrpc": "2.0",
                "method": "printer.objects.query",
                "params": {
                    "objects": {
                        "print_stats": ["filename", "state"],
                        "virtual_sdcard": ["file_path", "is_active", "progress"]
                    }
                },
                "id": self._next_id()
            }
            self.ws.send(json.dumps(query_request))

        except Exception as e:
            logger.warning("[%s] Ошибка подписки: %s", self.label, e)

    def on_message(self, ws, message):
        try:
            data = json.loads(message)
            if self._route_reply(data):
                return

            got_status = False
            if data.get('result') and isinstance(data['result'], dict):
                result_status = data['result'].get('status')
                if isinstance(result_status, dict):
                    self.parse_status(result_status)
                    got_status = True

            if 'method' in data and data['method'] == 'notify_status_update':
                status = data['params'][0]
                self.parse_status(status)
                got_status = True

            # Only a real printer-status message counts as liveness. Moonraker also
            # broadcasts server-process notifications (notify_proc_stat_update ~1/s)
            # on this socket even after klippy has shut down; refreshing the
            # staleness timestamp on those would keep a dead print showing its last
            # state forever, defeating get_data()'s 30s guard.
            if got_status:
                self.last_message_ts = time.time()

            with self._lock:
                need, self._discover_needed = self._discover_needed, False
            if need:
                self._discover()   # outside the lock: _next_id takes it

        except Exception as e:
            logger.warning("[%s] Ошибка парсинга: %s", self.label, e, exc_info=True)

    # --- JSON-RPC requests with replies --------------------------------------

    def _route_reply(self, data):
        """A reply to one of our pending requests: hand it over (and run its
        callback outside the lock) instead of parsing it as status."""
        rid = data.get("id") if isinstance(data, dict) else None
        if rid is None:
            return False
        with self._lock:
            entry = self._pending.pop(rid, None)
        if entry is None:
            return False
        entry["result"], entry["error"] = data.get("result"), data.get("error")
        entry["event"].set()
        callback = entry.get("on_result")
        if callback is not None:
            try:
                callback(entry["result"], entry["error"])
            except Exception:
                logger.warning("[%s] reply handler failed", self.label, exc_info=True)
        return True

    def _register(self, on_result=None):
        """A new request id with its pending entry; drops entries that never got a reply."""
        rid = self._next_id()
        entry = {"event": threading.Event(), "result": None, "error": None,
                 "on_result": on_result, "ts": time.monotonic()}
        with self._lock:
            stale = time.monotonic() - _PENDING_MAX_AGE
            for key in [k for k, v in self._pending.items() if v["ts"] < stale]:
                del self._pending[key]
            self._pending[rid] = entry
        return rid, entry

    @staticmethod
    def _send_request(ws, rid, method, params):
        request = {"jsonrpc": "2.0", "method": method, "id": rid}
        if params is not None:
            request["params"] = params
        ws.send(json.dumps(request))

    def _rpc(self, method, params=None, timeout=REPLY_TIMEOUT):
        """Send a request and wait for its reply (API threads only, never the
        WS thread). Returns {"success", "replied", "detail"}."""
        ws = self.ws
        if not self.connected or ws is None:
            return {"success": False, "replied": False, "detail": "Нет связи с Moonraker"}
        rid, entry = self._register()
        try:
            self._send_request(ws, rid, method, params)
        except Exception as e:
            with self._lock:
                self._pending.pop(rid, None)
            return {"success": False, "replied": False, "detail": str(e)}
        got = entry["event"].wait(timeout)
        with self._lock:
            self._pending.pop(rid, None)
        if not got:
            return {"success": True, "replied": False, "detail": "отправлено (без подтверждения)"}
        error = entry["error"]
        if error is not None:
            message = error.get("message") if isinstance(error, dict) else None
            return {"success": False, "replied": True,
                    "detail": "Klipper отклонил: " + str(message or error)[:200]}
        return {"success": True, "replied": True, "detail": "подтверждено принтером"}

    def _rpc_async(self, method, params, on_result):
        """Send a request whose reply goes to on_result(result, error); never
        waits (safe from on_open / the WS thread). False when not sent."""
        ws = self.ws
        if ws is None:
            return False
        rid, _entry = self._register(on_result)
        try:
            self._send_request(ws, rid, method, params)
            return True
        except Exception as e:
            with self._lock:
                self._pending.pop(rid, None)
            logger.warning("[%s] %s не отправлен: %s", self.label, method, e)
            return False

    # --- light pin discovery ---------------------------------------------------

    def _discover(self):
        """Look the light pin up (printer.objects.list), unless a lookup is in flight."""
        now = time.monotonic()
        with self._lock:
            if self._discover_ts and now - self._discover_ts < _PENDING_MAX_AGE:
                return
            self._discover_ts = now
        if not self._rpc_async("printer.objects.list", None, self._on_objects):
            self._discover_ts = 0.0

    def _on_objects(self, result, error):
        self._discover_ts = 0.0
        if error is not None:
            # light_pin stays None: retried when klippy next becomes ready
            logger.warning("[%s] objects.list: %s", self.label, error)
            return
        objects = result.get("objects") if isinstance(result, dict) else None
        pin = pick_light_pin(objects) or ""
        with self._lock:
            self.light_pin = pin
            self.data["light_pin"] = pin
        if pin:
            logger.info("[%s] light pin: %s", self.label, pin)
            # Moonraker replaces a connection's subscription: the whole set again, with the pin
            self.subscribe_to_status()
            self._rpc_async("printer.objects.query", {"objects": {"configfile": ["settings"]}}, self._on_config)

    def _on_config(self, result, error):
        """The pin's scale (SET_PIN VALUE for "on"). Klipper lower-cases the
        section names of configfile.settings."""
        pin = self.light_pin
        if error is not None or not pin or not isinstance(result, dict):
            return
        settings = ((result.get("status") or {}).get("configfile") or {}).get("settings") or {}
        section = settings.get(("output_pin " + pin).lower()) or {}
        try:
            scale = float(section.get("scale", 1.0))
        except (TypeError, ValueError):
            scale = 1.0
        self.light_scale = scale if scale > 0 else 1.0

    def run_action(self, action, timeout=REPLY_TIMEOUT):
        """pause / resume / stop / light_on / light_off with Moonraker's reply
        as the verdict: {"success", "replied", "detail"}."""
        if action in _RPC_METHODS:
            return self._rpc(_RPC_METHODS[action], timeout=timeout)
        if action in ("light_on", "light_off"):
            pin = self.light_pin
            if not pin:
                return {"success": False, "replied": False,
                        "detail": "У принтера не найден пин подсветки (output_pin с light/led в имени)"}
            value = self.light_scale if action == "light_on" else 0
            r = self._rpc("printer.gcode.script",
                          {"script": "SET_PIN PIN=%s VALUE=%s" % (pin, "%g" % value)}, timeout=timeout)
            if r.get("success") and r.get("replied"):
                # Klipper ran SET_PIN; the pin's own push follows later (~250 ms,
                # more during a print). Unanswered or refused: left to that push.
                with self._lock:
                    self.data['light_on'] = action == "light_on"
            return r
        return {"success": False, "replied": False, "detail": "Неподдерживаемая команда: %s" % action}

    def parse_status(self, status):
        try:
            with self._lock:
                self._got_telemetry = True
                if 'print_stats' in status:
                    ps = status['print_stats']
                    if 'state' in ps:
                        self.data['state'] = ps['state']
                    if 'print_duration' in ps:
                        self.data['print_time'] = int(ps['print_duration'])
                    if 'filename' in ps and ps['filename']:
                        self.data['filename'] = ps['filename']
                    if 'message' in ps and ps['message']:
                        self.data['message'] = ps['message']
                    if 'total_duration' in ps:
                        self.data['total_duration'] = float(ps['total_duration'])
                    if 'filament_used' in ps:
                        self.data['filament_used_mm'] = float(ps['filament_used'])
                    info = ps.get('info')
                    if isinstance(info, dict):
                        current_layer = info.get('current_layer')
                        total_layer = info.get('total_layer')
                        if current_layer is not None:
                            self.data['current_layer'] = int(current_layer)
                        if total_layer is not None:
                            self.data['total_layers'] = int(total_layer)

                if 'display_status' in status:
                    ds = status['display_status']
                    if 'progress' in ds and ds['progress'] is not None:
                        self.data['progress'] = int(float(ds['progress']) * 100)
                    if 'message' in ds and ds['message']:
                        self.data['message'] = ds['message']

                if 'virtual_sdcard' in status:
                    vs = status['virtual_sdcard']
                    if 'progress' in vs and vs['progress'] is not None:
                        self.data['progress'] = int(float(vs['progress']) * 100)
                    if 'file_path' in vs and vs['file_path']:
                        self.data['filename'] = str(vs['file_path']).replace('\\', '/').split('/')[-1]
                    if 'file_size' in vs and vs['file_size'] is not None:
                        self.data.setdefault('debug', {})['file_size_bytes'] = int(vs['file_size'])

                debug = self.data.setdefault('debug', {})
                debug['status_sections'] = sorted(status.keys())
                debug['job_related_fields'] = {}
                for section_name, section_value in status.items():
                    if isinstance(section_value, dict):
                        for key, value in section_value.items():
                            key_str = str(key)
                            if any(token in key_str.lower() for token in ('file', 'name', 'job', 'task', 'print', 'message')) and value not in (None, ''):
                                debug['job_related_fields'][f"{section_name}.{key_str}"] = str(value)

                if 'extruder' in status:
                    ext = status['extruder']
                    if 'temperature' in ext:
                        self.data['nozzle_temp'] = int(float(ext['temperature']))
                    if 'target' in ext:
                        self.data['target_nozzle_temp'] = int(float(ext['target']))
                    if 'pressure_advance' in ext:
                        self.data.setdefault('debug', {})['pressure_advance'] = float(ext['pressure_advance'])


                if 'heater_bed' in status:
                    bed = status['heater_bed']
                    if 'temperature' in bed:
                        self.data['bed_temp'] = int(float(bed['temperature']))
                    if 'target' in bed:
                        self.data['target_bed_temp'] = int(float(bed['target']))

                if 'fan' in status:
                    fan = status['fan']
                    if 'speed' in fan and fan['speed'] is not None:
                        self.data['fan_speed_pct'] = int(float(fan['speed']) * 100)

                if 'toolhead' in status:
                    th = status['toolhead']
                    debug_th = self.data.setdefault('debug', {})
                    if 'stalls' in th:
                        debug_th['toolhead_stalls'] = int(th['stalls'])
                    if 'homed_axes' in th:
                        debug_th['homed_axes'] = str(th['homed_axes'])

                if 'gcode_move' in status:
                    gm = status['gcode_move']
                    if 'speed_factor' in gm and gm['speed_factor'] is not None:
                        self.data['feedrate_pct'] = int(float(gm['speed_factor']) * 100)

                if 'webhooks' in status:
                    wh = status['webhooks']
                    wh_state = wh.get('state')
                    debug_wh = self.data.setdefault('debug', {})
                    # klippy (re)started or its MCU came back: look the light pin up again
                    if str(wh_state).lower() == 'ready' and str(debug_wh.get('webhooks_state')).lower() != 'ready':
                        self._discover_needed = True
                    debug_wh['webhooks_state'] = wh_state
                    # webhooks.state_message is a host-health string, not a print stage.
                    # When the host is operational its state is "ready" and the message is
                    # the constant "Printer is ready" (true even mid-print) — surfacing that
                    # as a stage badge leaves a permanent banner on the card. Only expose the
                    # message for abnormal states (startup/shutdown/error), where it carries
                    # an actual diagnostic.
                    if wh.get('state_message') and str(wh_state).lower() != 'ready':
                        self.data['stage'] = str(wh['state_message'])

                light_pin = self.light_pin
                light = status.get('output_pin ' + light_pin) if light_pin else None
                if isinstance(light, dict) and light.get('value') is not None:
                    try:
                        self.data['light_on'] = float(light['value']) > 0
                    except (TypeError, ValueError):
                        pass

                if 'system_stats' in status:
                    ss = status['system_stats']
                    debug_ss = self.data.setdefault('debug', {})
                    if 'sysload' in ss:
                        debug_ss['sysload'] = float(ss['sysload'])
                    if 'memavail' in ss:
                        debug_ss['memavail'] = float(ss['memavail'])
                    if 'cputime' in ss:
                        debug_ss['cputime'] = float(ss['cputime'])

                # Moonraker doesn't expose remaining_time directly; estimate it from
                # print_duration and progress (constant-rate assumption). Falls back
                # to 0 outside of an active print or when the data isn't ready yet.
                try:
                    print_time = self.data.get('print_time', 0) or 0
                    progress_frac = self.data.get('progress', 0) / 100.0 if self.data.get('progress') else 0
                    state_lc = str(self.data.get('state') or '').lower()
                    if state_lc == 'printing' and print_time > 0 and 0 < progress_frac < 1:
                        estimated_total = print_time / progress_frac
                        remaining = int(estimated_total - print_time)
                        self.data['remaining_time'] = max(0, remaining)
                    elif state_lc in ('paused', 'printing'):
                        # Preserve previously computed value, don't zero it out.
                        pass
                    else:
                        self.data['remaining_time'] = 0
                except Exception:
                    logger.debug("[%s] remaining_time estimate failed", self.label)

        except Exception as e:
            logger.warning("[%s] Ошибка парсинга статуса: %s", self.label, e, exc_info=True)

    def on_error(self, ws, error):
        logger.warning("[%s] WebSocket ошибка: %s", self.label, error)
        # A transport error means the connection is no longer usable. Without
        # this, on_close is the only thing that flips connected=False, but the
        # library does not guarantee on_close fires after every on_error — so a
        # silent error would leave the collector serving stale data forever.
        self.connected = False
        if not self._stop_reconnect.is_set():
            self._start_reconnect()

    def on_close(self, ws, close_status_code, close_msg):
        logger.info("[%s] WebSocket закрыт", self.label)
        self.connected = False
        if not self._stop_reconnect.is_set():
            self._start_reconnect()

    def _start_reconnect(self):
        if self._reconnect_thread and self._reconnect_thread.is_alive():
            return
        self._reconnect_thread = threading.Thread(target=self._reconnect_loop, daemon=True)
        self._reconnect_thread.start()

    def _reconnect_loop(self):
        delay = self._reconnect_delay
        while not self._stop_reconnect.is_set() and not self.connected:
            logger.info("[%s] Попытка переподключения через %s секунд...", self.label, delay)
            time.sleep(delay)
            if self._stop_reconnect.is_set():
                break
            try:
                logger.info("[%s] Переподключение...", self.label)
                self.connect()
                if self.connected:
                    logger.info("[%s] Переподключение успешно", self.label)
                    return
            except Exception as e:
                logger.warning("[%s] Ошибка переподключения: %s", self.label, e)
            delay = min(delay * 2, self._max_reconnect_delay)

    def send_command(self, command, param=None):
        """Deprecated, no caller left: printer_commands sends Klipper actions
        through run_action (answered by Moonraker). Fire-and-forget print control
        via Moonraker JSON-RPC: pause, resume, stop (cancel), gcode_line. To be
        removed in a cleanup.
        """
        warnings.warn("KlipperClient.send_command is deprecated: use run_action", DeprecationWarning, stacklevel=2)
        if not self.connected or not self.ws:
            return {"success": False, "detail": "Not connected"}

        method_map = {
            "pause": "printer.print.pause",
            "resume": "printer.print.resume",
            "stop": "printer.print.cancel",
        }

        try:
            rid = self._next_id()
            if command == "gcode_line":
                if not param:
                    return {"success": False, "detail": "No gcode provided"}
                request = {
                    "jsonrpc": "2.0",
                    "method": "printer.gcode.script",
                    "params": {"script": param},
                    "id": rid,
                }
            elif command in method_map:
                request = {
                    "jsonrpc": "2.0",
                    "method": method_map[command],
                    "id": rid,
                }
            else:
                return {"success": False, "detail": f"Unsupported command: {command}"}

            self.ws.send(json.dumps(request))
            return {"success": True, "detail": "Command sent"}
        except Exception as e:
            return {"success": False, "detail": str(e)}

    def get_data(self):
        """Return a snapshot copy of current printer data.

        Returning a shallow copy keeps the live telemetry dict private — the
        normalizer writes a "debug" key back into whatever it receives, which
        would otherwise pollute this client's internal state across polls.

        Returns {} when the socket has gone stale (no parsed message in >30s),
        so a hung connection surfaces as offline instead of replaying the last
        telemetry forever. Mirrors the Creality client's guard.
        """
        with self._lock:
            # No real telemetry parsed yet — don't serve the pre-seeded defaults
            # as if the printer were reporting (Moonraker up, klippy silent).
            if not self._got_telemetry:
                return {}
            if self.last_message_ts and time.time() - self.last_message_ts > 30:
                self.connected = False
                logger.warning("[%s] Нет данных > 30с, принудительный дисконнект", self.label)
                return {}
            return dict(self.data)

    def disconnect(self):
        """Close WebSocket connection"""
        self._stop_reconnect.set()
        if self.ws:
            self.ws.close()


def translate_klipper_state(state):
    """Translate Klipper state to Russian"""
    states = {
        'standby': 'ОЖИДАНИЕ',
        'printing': 'ПЕЧАТЬ',
        'paused': 'ПАУЗА',
        'complete': 'ГОТОВО',
        'cancelled': 'ОТМЕНЕНО',
        'error': 'ОШИБКА',
        'unknown': 'НЕИЗВЕСТНО',
    }
    try:
        return states.get(state.lower(), state.upper())
    except Exception:
        return str(state)


def format_remaining_minutes(minutes):
    try:
        m = int(minutes or 0)
    except Exception:
        m = 0
    if m < 60:
        return f"{m} мин"
    h = m // 60
    r = m % 60
    if r == 0:
        return f"{h} ч"
    return f"{h} ч {r} мин"


