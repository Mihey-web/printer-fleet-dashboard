import json
import math
import logging
import websocket
import threading
import time

logger = logging.getLogger(__name__)


class CrealityK1Client:
    """Simple WebSocket client for Creality K1/K1 Max"""

    def __init__(self, host, label):
        self.host = host
        self.label = label
        self.ws = None
        self.connected = False
        self.data = {}
        self.last_message_ts = 0
        self.thread = None
        self._lock = threading.Lock()  # guards self.data (WS thread vs reader thread)
        self._stop_reconnect = threading.Event()
        self._reconnect_thread = None
        self._reconnect_delay = 5  # начальная задержка 5 секунд
        self._max_reconnect_delay = 60  # максимум 60 секунд
        self._poll_thread = None
        # Serializes connect(): both fetch() (poll thread) and the on_close/on_error
        # reconnect loop can call it. Without this the two drivers race and spawn
        # duplicate run_forever threads / leaked sockets. Mirrors the Bambu collector.
        self._connect_lock = threading.Lock()

    def connect(self):
        with self._connect_lock:
            # Idempotent: if a concurrent caller already brought the socket up,
            # don't tear it down and rebuild — that would orphan the live thread.
            if self.connected:
                return self.connected
            try:
                # Close any previous socket before replacing it, otherwise the old
                # run_forever thread keeps its TCP connection open and leaks on every
                # reconnect.
                if self.ws is not None:
                    try:
                        self.ws.close()
                    except Exception:
                        pass
                    # close() is non-blocking — join the old run_forever thread so its
                    # TCP socket is released before opening a new one, preventing FD
                    # leaks across repeated reconnects on a flapping network.
                    old_thread = self.thread
                    if old_thread is not None and old_thread.is_alive():
                        old_thread.join(timeout=5)
                ws_url = f"ws://{self.host}:9999"
                self.ws = websocket.WebSocketApp(
                    ws_url,
                    on_message=self.on_message,
                    on_error=self.on_error,
                    on_close=self.on_close,
                    on_open=self.on_open
                )
                self.thread = threading.Thread(target=self.ws.run_forever, kwargs={'ping_interval': 15, 'ping_timeout': 10}, daemon=True)
                self.thread.start()
                time.sleep(2)
                return self.connected
            except Exception as e:
                logger.error("[%s] Ошибка подключения WebSocket: %s", self.label, e)
                return False

    def on_open(self, ws):
        logger.info("[%s] WebSocket подключен", self.label)
        self.connected = True
        self._start_polling()

    def on_message(self, ws, message):
        try:
            if message == 'ok':
                return

            data = json.loads(message)

            if isinstance(data, dict) and data.get('ModeCode') == 'heart_beat':
                try:
                    ws.send('ok')
                except Exception:
                    logger.debug("[%s] heartbeat ack send failed", self.label)
                return

            # Liveness is refreshed only by real telemetry, not the heart_beat
            # keepalive handled above — otherwise a printer whose telemetry has
            # frozen but still pings keeps the 30s staleness guard alive and shows
            # its last state forever.
            self.last_message_ts = time.time()

            with self._lock:
                if not self.data:
                    self.data = {
                        'state': 'idle',
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
                        'filename': None,
                        'message': None,
                        'filament_used_mm': None,
                        'material_status': None,
                        'fan_speed_pct': None,
                        'feedrate_pct': None,
                        'stage': None,
                        'firmware_version': None,
                    }

                if 'printStatus' in data:
                    self.data['state'] = data['printStatus']
                elif 'state' in data:
                    st = data['state']
                    if isinstance(st, int):
                        self.data['state'] = {0: 'idle', 1: 'printing', 2: 'paused', 3: 'finish'}.get(st, 'idle')
                    else:
                        self.data['state'] = str(st).lower()

                if 'nozzleTemp' in data:
                    self.data['nozzle_temp'] = int(float(data['nozzleTemp']))

                if 'bedTemp0' in data:
                    self.data['bed_temp'] = int(float(data['bedTemp0']))

                if 'boxTemp' in data:
                    self.data['chamber_temp'] = int(float(data['boxTemp']))
                if 'targetNozzleTemp' in data:
                    self.data['target_nozzle_temp'] = int(float(data['targetNozzleTemp']))
                if 'targetBedTemp0' in data:
                    self.data['target_bed_temp'] = int(float(data['targetBedTemp0']))
                if 'usedMaterialLength' in data:
                    self.data['filament_used_mm'] = float(data['usedMaterialLength'])
                if 'materialStatus' in data:
                    # 0 = filament present; non-zero = runout. The firmware keeps
                    # state="printing" during a runout, so this is the only signal.
                    try:
                        self.data['material_status'] = int(data['materialStatus'])
                    except (TypeError, ValueError):
                        pass
                if 'modelFanPct' in data:
                    self.data['fan_speed_pct'] = int(data['modelFanPct'])
                if 'curFeedratePct' in data:
                    self.data['feedrate_pct'] = int(data['curFeedratePct'])
                if 'deviceState' in data and data['deviceState'] == 7:
                    self.data['stage'] = 'homing'
                if 'modelVersion' in data:
                    self.data['firmware_version'] = str(data['modelVersion'])

                if 'layer' in data:
                    self.data['current_layer'] = int(data['layer'])
                elif 'CurrentLayer' in data:
                    self.data['current_layer'] = int(data['CurrentLayer'])
                elif 'currentLayer' in data:
                    self.data['current_layer'] = int(data['currentLayer'])
                elif 'curLayer' in data:
                    self.data['current_layer'] = int(data['curLayer'])

                if 'TotalLayer' in data:
                    self.data['total_layers'] = int(data['TotalLayer'])

                if 'printLeftTime' in data:
                    self.data['remaining_time'] = int(data['printLeftTime'])

                if 'printJobTime' in data:
                    self.data['print_time'] = int(data['printJobTime'])

                for key in ('printFileName', 'fileName', 'filename', 'jobName', 'taskName', 'modelName', 'printName'):
                    value = data.get(key)
                    if value:
                        self.data['filename'] = str(value)
                        break

                for key in ('message', 'printInfo', 'taskName'):
                    value = data.get(key)
                    if value and not self.data.get('message'):
                        self.data['message'] = str(value)
                        break

                debug = self.data.setdefault('debug', {})
                debug['last_keys'] = sorted(str(key) for key in data.keys())[:80]
                debug['job_related_fields'] = {
                    str(key): str(value)
                    for key, value in data.items()
                    if any(token in str(key).lower() for token in ('file', 'name', 'job', 'task', 'print', 'model')) and value not in (None, '')
                }
                debug['raw_message_excerpt'] = {
                    str(key): str(value)
                    for key, value in data.items()
                    if value not in (None, '')
                }
                if 'boxsInfo' in data:
                    debug['cfs_summary'] = data['boxsInfo']
                if 'materialStatus' in data:
                    debug['material_status'] = data['materialStatus']
                if 'curPosition' in data:
                    debug['position'] = str(data['curPosition'])
                if 'lightSw' in data:
                    debug['light_on'] = bool(data['lightSw'])
                if 'err' in data:
                    debug['err'] = data['err']
                if 'model' in data:
                    debug['model'] = str(data['model'])
                if 'caseFanPct' in data:
                    debug['case_fan_pct'] = int(data['caseFanPct'])
                if 'auxiliaryFanPct' in data:
                    debug['aux_fan_pct'] = int(data['auxiliaryFanPct'])

                total_time = self.data['print_time'] + self.data['remaining_time']
                if total_time > 0:
                    self.data['progress'] = int((self.data['print_time'] / total_time) * 100)

                if self.data['total_layers'] > 0 and self.data['current_layer'] > 0:
                    self.data['progress'] = int((self.data['current_layer'] / self.data['total_layers']) * 100)

                # Only force "printing" while there is time remaining. After a job
                # finishes the printer keeps a non-zero print_time but printLeftTime
                # drops to 0 — forcing "printing" there would mask the completion.
                if self.data['print_time'] > 0 and self.data.get('remaining_time', 0) > 0:
                    if self.data['state'] == 'unknown':
                        self.data['state'] = 'printing'

        except Exception as e:
            logger.warning("[%s] Ошибка парсинга: %s", self.label, e, exc_info=True)

    def on_error(self, ws, error):
        logger.warning("[%s] WebSocket ошибка: %s", self.label, error)
        self.connected = False
        self._poll_thread = None
        if not self._stop_reconnect.is_set():
            self._start_reconnect()

    def on_close(self, ws, close_status_code, close_msg):
        logger.info("[%s] WebSocket закрыт", self.label)
        self.connected = False
        self._poll_thread = None
        # Запускаем переподключение
        if not self._stop_reconnect.is_set():
            self._start_reconnect()

    def get_data(self):
        """Return a snapshot copy of current printer data; empty dict means offline."""
        with self._lock:
            # Check connected inside the lock so we don't race on_close flipping it
            # between an outside check and the data read (which would serve stale data).
            if not self.connected:
                return {}
            if not self.data:
                return {}
            if self.last_message_ts and time.time() - self.last_message_ts > 30:
                self.connected = False
                logger.warning("[%s] Нет данных > 30с, принудительный дисконнект", self.label)
                return {}
            return dict(self.data)

    def _send_get(self, params):
        try:
            if self.ws and self.connected:
                self.ws.send(json.dumps({'method': 'get', 'params': params}))
        except Exception as e:
            logger.warning("[%s] _send_get failed: %s", self.label, e)

    def send_command(self, command, param=None):
        """Send a print-control command via the Creality port-9999 'set' method.

        Supported: pause, resume, stop, gcode_line.
        """
        if not self.connected or not self.ws:
            return {"success": False, "detail": "Not connected"}

        param_map = {
            "pause": {"pause": 1},
            "resume": {"pause": 0},
            "stop": {"stop": 1},
        }

        try:
            if command == "gcode_line":
                if not param:
                    return {"success": False, "detail": "No gcode provided"}
                params = {"gcodeCmd": param}
            elif command in param_map:
                params = param_map[command]
            else:
                return {"success": False, "detail": f"Unsupported command: {command}"}

            self.ws.send(json.dumps({"method": "set", "params": params}))
            return {"success": True, "detail": "Command sent"}
        except Exception as e:
            return {"success": False, "detail": str(e)}

    def _start_polling(self):
        if self._poll_thread and self._poll_thread.is_alive():
            return
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._poll_thread.start()

    def _poll_loop(self):
        next_para = 0.0
        next_objects = 0.0
        next_cfs = 0.0
        while self.connected and not self._stop_reconnect.is_set():
            now = time.time()
            if now >= next_para:
                self._send_get({'ReqPrinterPara': 1})
                next_para = now + 5
            if now >= next_objects:
                self._send_get({'reqPrintObjects': 1})
                next_objects = now + 2
            if now >= next_cfs:
                self._send_get({'boxsInfo': 1})
                next_cfs = now + 300
            time.sleep(0.5)

    def _start_reconnect(self):
        """Запускает поток переподключения"""
        if self._reconnect_thread and self._reconnect_thread.is_alive():
            return
        self._reconnect_thread = threading.Thread(target=self._reconnect_loop, daemon=True)
        self._reconnect_thread.start()

    def _reconnect_loop(self):
        """Цикл переподключения с экспоненциальной задержкой"""
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

            # Экспоненциальная задержка
            delay = min(delay * 2, self._max_reconnect_delay)

    def disconnect(self):
        """Close WebSocket connection"""
        self._stop_reconnect.set()
        if self.ws:
            self.ws.close()


def translate_k1_state(state):
    """Translate K1 state to Russian"""
    states = {
        'printing': 'ПЕЧАТЬ',
        'paused': 'ПАУЗА',
        'complete': 'ГОТОВО',
        'standby': 'ОЖИДАНИЕ',
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


