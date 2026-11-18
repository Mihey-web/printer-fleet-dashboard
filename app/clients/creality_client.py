import asyncio
import logging
import math
from app.services.creality_features import feeder
from app.clients import moonraker
from app.services import creality_parts, job_previews, klipper_project, project_summary
from app.services.print_material import creality_grams
from app.vendor.ha_creality_ws.ws_client import KClient
import threading
import time
import urllib.request

logger = logging.getLogger(__name__)

PREVIEW_DELAY_S = 15      # the printer renders current_print_image.png after the job starts
PREVIEW_RETRY_S = 60
PREVIEW_ATTEMPTS = 4
FILE_INFO_RETRY_S = 120   # the listing carries every file on the printer: ask once per job
FILE_INFO_ATTEMPTS = 3    # the listing asked for / the K2's G-code settings read, per job
CFS_CONFIG_EVERY_S = 30   # the CFS's boxConfig is asked for at most this often
STALE_S = 30              # get_data(): telemetry older than this is stale
PREVIEW_TIMEOUT_S = 8     # the job's thumbnail over HTTP
SEND_TIMEOUT_S = 5        # a WebSocket frame handed to the event loop

# Числовой `state` из WS-телеметрии Creality. K1-прошивки отдают паузу как 2,
# Ender 5 Max — как 5 (проверено вживую: pause -> 5, resume -> 0/1).
# Неизвестный код НЕЛЬЗЯ схлопывать в 'idle': normalizer промоутит idle обратно
# в 'printing' по любому признаку активности, и реальная пауза становится
# невидимой на карточке. 'unknown' проходит ту же промоцию, но паузу не прячет.
# 4 = stopped: a K1C (fw 1.3.5.22) holds it for minutes after a cancelled print
# (ha_creality_ws k1c_cancel_during_self_test fixture, derive_print_state); 7 / 9
# (finishing a cancel, starting a print) stay 'unknown' = busy.
STATE_CODES = {0: 'idle', 1: 'printing', 2: 'paused', 3: 'finish', 4: 'cancelled', 5: 'paused'}


# Telemetry values arrive as numbers or text; KClient hands on its cumulative state, so a value
# that cannot be read rides on every later frame: it is skipped (None), never the whole frame.
def _number(value):
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return n if math.isfinite(n) else None


def _rounded(value):
    """Temperatures and fan percent, rounded as the web rounds Bambu's (half up)."""
    n = _number(value)
    return None if n is None else int(math.floor(n + 0.5))


def _whole(value):
    """Counters (layers, seconds, percent done): the whole part."""
    n = _number(value)
    return None if n is None else int(n)


def _firmware(model_version):
    """The version out of modelVersion ("printer hw ver:;printer sw ver:;DWIN hw ver:F004;DWIN sw
    ver:V1.2.0.24;"): the printer's own, else the screen's (K1C, Ender-5 Max: only that one);
    None when it names none. Another shape is kept as it is."""
    text = str(model_version or "").strip()
    if ":" not in text:
        return text or None
    parts = {}
    for seg in text.split(";"):
        key, _, value = seg.partition(":")
        parts[key.strip().lower()] = value.strip()
    version = parts.get("printer sw ver") or parts.get("dwin sw ver")
    return version[1:] if version and version[:1] in "Vv" and version[1:2].isdigit() else version or None


class CrealityK1Client:
    """Bridge the synchronous farm poller to the standalone HA Creality client."""

    def __init__(self, host, label):
        self.host = host
        self.label = label
        self.data = {}
        self.last_message_ts = 0
        self._session = 0
        self._settings_read_at = 0
        self._material_read_at = 0
        self._material_path = None
        self._parts = creality_parts.Reader()   # reqPrintObjects: the job's parts, the skipped ones
        self._material_attempts = 0
        self._preview_path = None
        self._preview_due = 0
        self._preview_attempts = 0
        self._preview_busy = False
        self._pending_summary = None
        self._details_lock = threading.Lock()
        self._details_path = None   # the job whose G-code settings are read (Klipper's API)
        self._details = None
        self._details_retry = None  # (path, listing card) of a failed settings read: read again
        self.file_listing = None   # (monotonic, retGcodeFileInfo2 list | retGcodeFileInfo dict): printer_files
        self._lock = threading.Lock()
        self._connect_lock = threading.Lock()
        self._shutdown = None
        self._loop = None
        self._thread = None
        self._client = KClient(host, self._receive)

    @property
    def connected(self):
        return self._client.is_connected

    def connect(self):
        with self._connect_lock:
            if self._thread is None or not self._thread.is_alive():
                self._shutdown = asyncio.Event()
                self._loop = asyncio.new_event_loop()
                self._thread = threading.Thread(target=self._run, args=(self._loop,),
                                                name=f"Creality-{self.label}", daemon=True)
                self._thread.start()
        return self.connected

    def _run(self, loop):
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._client.start())
            loop.run_until_complete(self._shutdown.wait())
        finally:
            loop.run_until_complete(self._client.stop())
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()

    def disconnect(self):
        with self._connect_lock:
            if self._thread and self._thread.is_alive():
                self._loop.call_soon_threadsafe(self._shutdown.set)
                self._thread.join()

    async def _receive(self, data):
        self._parse_report(data)
        for key in ('retGcodeFileInfo2', 'retGcodeFileInfo'):
            if key in data:
                self.file_listing = (time.monotonic(), data[key])
        if self._pending_summary is not None:
            pending, self._pending_summary = self._pending_summary, None
            asyncio.get_running_loop().run_in_executor(None, self._save_summary, *pending)
        path = data.get('printFileName')
        now = time.monotonic()
        if path and path != self._material_path:
            self._material_path, self._material_attempts = path, 0
            self._preview_path, self._preview_attempts = path, 0
            self._preview_due = now + PREVIEW_DELAY_S
            self._details_retry = None
        retry = self._details_retry
        if retry is not None and retry[0] != self._material_path:
            retry = self._details_retry = None      # another job's
        if (self._material_path and self._material_attempts < FILE_INFO_ATTEMPTS
                and (self.data.get('project_key') is None or retry is not None)
                and now - self._material_read_at > FILE_INFO_RETRY_S):
            self._material_read_at = now
            self._material_attempts += 1
            if self.data.get('project_key') is None:
                await self._client.request_gcode_file_info()
            else:   # the listing's card is shown, its G-code settings failed: read them again
                self._details_retry = None
                asyncio.get_running_loop().run_in_executor(None, self._save_summary, *retry)
        if (self._preview_path and self.data.get('preview_key') is None and not self._preview_busy
                and self._preview_attempts < PREVIEW_ATTEMPTS and now >= self._preview_due
                and self.data.get('state') in ('printing', 'paused', 'unknown')):
            self._preview_busy = True
            self._preview_attempts += 1
            asyncio.get_running_loop().run_in_executor(None, self._read_preview, self._preview_path)
        if data.get('cfsConnect') == 1 and time.monotonic() - self._settings_read_at > CFS_CONFIG_EVERY_S:
            self._settings_read_at = time.monotonic()
            await self._client._send_json({"method": "get", "params": {"boxConfig": 1}})

    def _read_preview(self, path):
        """The slicer thumbnail of the running job (same URL Home Assistant's image entity uses)."""
        key = None
        try:
            url = 'http://%s/downloads/original/current_print_image.png' % self.host
            with urllib.request.urlopen(url, timeout=PREVIEW_TIMEOUT_S) as response:
                key = job_previews.save(response.read(job_previews.MAX_BYTES + 1))
        except Exception as e:
            logger.debug("[%s] превью задания недоступно: %s", self.label, type(e).__name__)
        with self._lock:
            self._preview_busy = False
            self._preview_due = time.monotonic() + PREVIEW_RETRY_S
            if key and self._preview_path == path and self.data.get('material_job_path') == path:
                self.data['preview_key'] = key

    def _save_summary(self, path, summary):
        """The listing's card at once; with Klipper's API (the K2) the card from the G-code's own
        settings next (app/services/klipper_project.py), read once per job (again after a failed
        read, _receive). A card with the settings is never replaced by the listing's."""
        if moonraker.known(self.host) is not True:
            self._store_summary(path, summary)
            return
        if self._details_path != path:      # a new job (unlocked peek): the listing's card meanwhile
            self._store_summary(path, summary)
        with self._details_lock:
            if self._details_path != path:
                self._details_path = path            # _read_details clears it after a failed read
                self._details = self._read_details(path, summary)
            details = self._details
        self._store_summary(path, project_summary.with_gcode(summary, **details) if details else summary)

    def _read_details(self, path, summary):
        try:
            found = klipper_project.read_settings(self.host, path)
        except Exception as e:      # read again later (_receive), from this listing's card
            logger.debug("[%s] настройки задания из файла не прочитаны: %s", self.label, e)
            self._details_path = None
            self._details_retry = (path, summary)
            return None
        self._details_retry = None      # read (another listing may have come meanwhile)
        if found is None:
            return None
        rel, settings, head = found
        details = {"settings": settings, "head": head}
        if klipper_project.has_supports(settings):    # the settings now, the supports' grams after the whole file
            self._store_summary(path, project_summary.with_gcode(summary, **details))
            try:
                details["support_grams"] = klipper_project.support_grams(self.host, rel)
            except Exception as e:  # the settings stay on the card, only the supports' grams are missing
                logger.debug("[%s] граммы поддержек не посчитаны: %s", self.label, e)
        return details

    def _store_summary(self, path, summary):
        try:
            key = job_previews.save_project(summary)
        except (OSError, ValueError) as e:
            logger.debug("[%s] карточка проекта не сохранена: %s", self.label, type(e).__name__)
            return
        with self._lock:
            if self.data.get('material_job_path') == path:
                self.data['project_key'] = key

    def _put(self, key, value):
        """A parsed value; one that could not be read (None) keeps the last good one."""
        if value is not None:
            self.data[key] = value

    def _file_info(self, data):
        path = data.get('printFileName') or self._material_path
        for item in data.get('retGcodeFileInfo2') or []:
            if isinstance(item, dict) and item.get('path') == path:
                return item
        return None

    def _parse_report(self, data):
        try:
            with self._lock:
                if self._session != self._client.reconnect_count:
                    self.data = {}
                    self._session = self._client.reconnect_count
                    self._material_path = None   # a fresh state: the job's card and preview are read again
                self.last_message_ts = time.monotonic()
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
                        self.data['state'] = STATE_CODES.get(st, 'unknown')
                    else:
                        self.data['state'] = str(st).lower()

                if 'nozzleTemp' in data:
                    self._put('nozzle_temp', _rounded(data['nozzleTemp']))

                if 'bedTemp0' in data:
                    self._put('bed_temp', _rounded(data['bedTemp0']))

                if 'boxTemp' in data:
                    self._put('chamber_temp', _rounded(data['boxTemp']))
                if 'targetNozzleTemp' in data:
                    self._put('target_nozzle_temp', _rounded(data['targetNozzleTemp']))
                if 'targetBedTemp0' in data:
                    self._put('target_bed_temp', _rounded(data['targetBedTemp0']))
                if 'usedMaterialLength' in data:
                    self._put('filament_used_mm', _number(data['usedMaterialLength']))
                if 'materialStatus' in data:
                    # 0 = filament present; non-zero = runout. The firmware keeps
                    # state="printing" during a runout, so this is the only signal.
                    try:
                        self.data['material_status'] = int(data['materialStatus'])
                    except (TypeError, ValueError):
                        pass
                if 'modelFanPct' in data:
                    self._put('fan_speed_pct', _rounded(data['modelFanPct']))
                if 'curFeedratePct' in data:
                    self._put('feedrate_pct', _rounded(data['curFeedratePct']))
                if 'deviceState' in data:
                    self.data['stage'] = 'homing' if data['deviceState'] == 7 else None
                if 'modelVersion' in data:
                    self.data['firmware_version'] = _firmware(data['modelVersion'])

                if 'layer' in data:
                    self._put('current_layer', _whole(data['layer']))
                elif 'CurrentLayer' in data:
                    self._put('current_layer', _whole(data['CurrentLayer']))
                elif 'currentLayer' in data:
                    self._put('current_layer', _whole(data['currentLayer']))
                elif 'curLayer' in data:
                    self._put('current_layer', _whole(data['curLayer']))

                if 'TotalLayer' in data:
                    self._put('total_layers', _whole(data['TotalLayer']))

                if 'printLeftTime' in data:
                    self._put('remaining_time', _whole(data['printLeftTime']))

                if 'printJobTime' in data:
                    self._put('print_time', _whole(data['printJobTime']))

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
                material_path = data.get('printFileName')
                if material_path and self.data.get('material_job_path') != material_path:
                    self.data['planned_filament_g'] = None
                    self.data['preview_key'] = self.data['project_key'] = None
                    self.data['material_job_path'] = material_path
                if 'retGcodeFileInfo' in data:     # the Ender-5 Max's listing: the filament's length only
                    grams = creality_grams({'printFileName': self.data.get('material_job_path'),
                                            'retGcodeFileInfo': data['retGcodeFileInfo']})
                    if grams is not None:
                        self.data['planned_filament_g'] = grams
                if 'retGcodeFileInfo2' in data:
                    grams = creality_grams(dict(data, printFileName=self.data.get('material_job_path')))
                    if grams is not None:
                        self.data['planned_filament_g'] = grams
                    item = self._file_info(dict(data, printFileName=self.data.get('material_job_path')))
                    summary = project_summary.creality(item) if item else None
                    if summary:   # written to disk by _receive, outside the lock and the WS loop
                        self._pending_summary = (self.data.get('material_job_path'), summary)
                self.data.update(self._parts.absorb(data))
                self.data['target_box_temp'] = data.get('targetBoxTemp')
                self.data['max_box_temp'] = data.get('maxBoxTemp')
                self.data['axes_homed'] = data.get('autohome')
                self.data['device_state'] = data.get('deviceState')
                self.data['feed_state'] = data.get('feedState')
                self.data['cfs'] = feeder(data.get('boxsInfo')) if data.get('cfsConnect') == 1 else None
                self.data['box_config'] = dict(data['boxConfig']) if isinstance(data.get('boxConfig'), dict) else None
                if 'boxsInfo' in data:
                    debug['cfs_summary'] = data['boxsInfo']
                if 'materialStatus' in data:
                    debug['material_status'] = data['materialStatus']
                if 'curPosition' in data:
                    debug['position'] = str(data['curPosition'])
                if 'lightSw' in data:
                    debug['light_on'] = bool(data['lightSw'])
                    try:
                        self.data['light_on'] = bool(int(data['lightSw']))
                    except (TypeError, ValueError):
                        pass
                if 'err' in data:
                    debug['err'] = data['err']
                    err = data['err']
                    # {"errcode": N, "key": K}: errcode 0 = no error (the notification's source)
                    if isinstance(err, dict) and 'errcode' in err:
                        try:
                            self.data['err_code'] = int(err['errcode'])
                            self.data['err_key'] = err.get('key')
                        except (TypeError, ValueError):
                            pass
                if 'model' in data:
                    debug['model'] = str(data['model'])
                for key, out in (('caseFanPct', 'case_fan_pct'), ('auxiliaryFanPct', 'aux_fan_pct')):
                    value = _rounded(data.get(key))
                    if value is not None:
                        debug[out] = self.data[out] = value
                # The firmware's own heater ceilings (K1C: 300 / 100): they cap
                # the temperature commands (printer_commands.creality_temp_limits).
                for key, out in (('maxNozzleTemp', 'max_nozzle_temp'), ('maxBedTemp', 'max_bed_temp')):
                    if key in data:
                        try:
                            self.data[out] = int(float(data[key]))
                        except (TypeError, ValueError):
                            pass

                # The printer's own percent (its screen's; HA's derive_print_state reads the same):
                # 0 on a new job's first frame, where the layers may still echo the last job's.
                printed = _whole(data.get('printProgress') if data.get('printProgress') is not None
                                 else data.get('dProgress'))
                total_time = self.data['print_time'] + self.data['remaining_time']
                if printed is not None:     # a K1C says 100 with time still left, then 99 (HA R81)
                    self.data['progress'] = max(0, min(99 if self.data['remaining_time'] > 0 else 100, printed))
                elif self.data['total_layers'] > 0 and self.data['current_layer'] > 0:
                    self.data['progress'] = int((self.data['current_layer'] / self.data['total_layers']) * 100)
                elif total_time > 0:
                    self.data['progress'] = int((self.data['print_time'] / total_time) * 100)

                # Only force "printing" while there is time remaining. After a job
                # finishes the printer keeps a non-zero print_time but printLeftTime
                # drops to 0 — forcing "printing" there would mask the completion.
                if self.data['print_time'] > 0 and self.data.get('remaining_time', 0) > 0:
                    if self.data['state'] == 'unknown':
                        self.data['state'] = 'printing'

        except Exception as e:
            logger.warning("[%s] Ошибка парсинга: %s", self.label, e, exc_info=True)

    def get_data(self):
        """Only serve telemetry received on the current, live session."""
        with self._lock:
            if (not self.connected or self._session != self._client.reconnect_count
                    or time.monotonic() - self.last_message_ts > STALE_S):
                return {}
            out = dict(self.data)
        # Klipper's own API next to the WS one (K2 Plus): True / False, None until probed
        out['klipper_api'] = moonraker.known(self.host)
        return out

    def _send(self, params):
        # A lost connection can leave delivery uncertain. Do not replay motion
        # or other physical commands on the next connection.
        message = {"method": "set", "params": params}
        future = asyncio.run_coroutine_threadsafe(self._client._send_json(message), self._loop)
        try:
            future.result(timeout=SEND_TIMEOUT_S)
        except TimeoutError:
            future.cancel()
            raise TimeoutError("Timed out sending command")

    def request_file_listing(self):
        """Ask for every G-code file on the printer; the reply lands in file_listing."""
        future = asyncio.run_coroutine_threadsafe(self._client.request_gcode_file_info(), self._loop)
        future.result(timeout=SEND_TIMEOUT_S)

    def _schedule_cfs_refresh(self):
        async def refresh():
            for delay in (1, 4, 10):
                await asyncio.sleep(delay)
                if not self.connected:
                    return
                try:
                    await self._client.request_boxs_info()
                    await self._client._send_json({"method": "get", "params": {"boxConfig": 1}})
                except Exception:
                    return
        asyncio.create_task(refresh())

    def send_command(self, command, param=None):
        """Send a command via the Creality port-9999 'set' method (fire and
        forget: the firmware sends no acknowledgement).

        Supported: pause, resume, stop, light_on, light_off, gcode_line, and
        "set" with param = a list of params dicts (printer_commands builds
        them), each sent as its own {"method": "set"} frame, in order.
        """
        if not self.connected:
            return {"success": False, "detail": "Not connected"}

        if command == "set":
            frames = param if isinstance(param, list) else None
            if not frames or any(not isinstance(f, dict) or not f for f in frames):
                return {"success": False, "detail": "No set params provided"}
            sent = 0
            try:
                for frame in frames:
                    self._send(frame)
                    sent += 1
                    if any(k in frame for k in ("modifyMaterial", "feedInOrOut", "refreshBox", "boxConfig")):
                        self._loop.call_soon_threadsafe(self._schedule_cfs_refresh)
                return {"success": True, "detail": "Command sent"}
            except Exception as e:
                if not sent:
                    return {"success": False, "detail": str(e)}
                # the frames before the error reached the printer: never «nothing sent»
                return {"success": True, "partial": True,
                        "detail": "отправлено частично: %d из %d (%s)" % (sent, len(frames), e)}

        param_map = {
            "pause": {"pause": 1},
            "resume": {"pause": 0},
            "stop": {"stop": 1},
            "light_on": {"lightSw": 1},
            "light_off": {"lightSw": 0},
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

            self._send(params)
            return {"success": True, "detail": "Command sent"}
        except Exception as e:
            return {"success": False, "detail": str(e)}
