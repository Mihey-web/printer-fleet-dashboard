import logging
import socket
import threading

from pybambu import BambuClient
from pybambu import models as _pb_models

logger = logging.getLogger(__name__)

# pybambu parses only the 5-level humidity index out of the AMS payload and
# discards humidity_raw — the real percentage that AMS 2 Pro units report
# (verified live on P2S: {"humidity": "2", "humidity_raw": "24"}). Wrap the
# library's parser to stash it onto each AMSInstance so the normalizer can
# expose a human-readable percent. Any payload surprise must never break
# telemetry parsing, hence the broad guard.
_orig_ams_print_update = _pb_models.AMSList.print_update


def _ams_print_update_keep_raw(self, data):
    changed = _orig_ams_print_update(self, data)
    try:
        for ams in (data.get("ams") or {}).get("ams", []):
            index = int(ams.get("id", -1))
            if not (0 <= index < len(self.data) and self.data[index] is not None):
                continue
            raw = ams.get("humidity_raw")
            if raw not in (None, ""):
                self.data[index].humidity_raw = int(float(raw))
            # dry_time > 0 while the AMS drying cycle is running
            dry = ams.get("dry_time")
            if dry not in (None, ""):
                self.data[index].dry_time = int(float(dry))
    except (TypeError, ValueError, AttributeError):
        pass
    return changed


_pb_models.AMSList.print_update = _ams_print_update_keep_raw

# pybambu читает температуру камеры только из плоского chamber_temper — так шлют
# X1/X1C. P2S (новая прошивка) чамбер-сенсор отдаёт вложенно:
# print.device.ctc.info.temp (ctc = chamber temperature controller, °C, verified
# live on P2S: 43). Без фолбэка камера у P2S не показывается, хотя сенсор есть.
# Значения вроде device.bed.info.temp — бит-паковка, поэтому берём только ctc и
# в разумном диапазоне.
_orig_temp_print_update = _pb_models.Temperature.print_update


def _temp_print_update_ctc(self, data):
    changed = _orig_temp_print_update(self, data)
    try:
        if not data.get("chamber_temper"):
            ctc = (((data.get("device") or {}).get("ctc") or {}).get("info") or {}).get("temp")
            if isinstance(ctc, (int, float)) and 0 < ctc < 200 and self.chamber_temp != round(ctc):
                self.chamber_temp = round(ctc)
                changed = True
    except (TypeError, ValueError, AttributeError):
        pass
    return changed


_pb_models.Temperature.print_update = _temp_print_update_ctc

# pybambu разбирает report-топик только ради телеметрии и молча выбрасывает
# ответы принтера на команды (блоки с result/reason). Tee сырого payload в
# printer_commands даёт локальному каналу подтверждения — на них построено
# определение, принимает ли прошивка print-класс (Developer Mode и fw 01.07).
_orig_client_on_message = BambuClient.on_message


def _client_on_message_tee(self, client, userdata, message):
    _orig_client_on_message(self, client, userdata, message)
    try:
        from app.services import printer_commands
        printer_commands.note_local_reply(getattr(self, "_serial", None), message.payload)
    except Exception:
        pass


BambuClient.on_message = _client_on_message_tee


class BambuCollector:
    def __init__(self, label: str, host: str, access_code: str, serial: str, device_type: str = "X1C"):
        self.label = label
        self.host = host
        self.client = BambuClient(
            device_type=device_type,
            serial=serial,
            host=host,
            local_mqtt=True,
            region="",
            email="",
            username="",
            auth_token="",
            access_code=access_code,
        )
        self.connected = False
        self._connect_lock = threading.Lock()

    def connect(self):
        # Идемпотентно: connect() зовут и warm-up на старте, и fetch()/send_command
        # из своих потоков. Каждый вызов BambuClient.connect() создаёт НОВЫЙ
        # mqtt.Client + listener-поток (старый продолжает жить и реконнектиться),
        # поэтому вторую сессию к принтеру поднимать нельзя.
        with self._connect_lock:
            if self.connected:
                return
            self.client.connect(lambda *_args, **_kwargs: None)
            self.connected = True

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
            # Don't flip connected=False here. pybambu's listener thread owns
            # reconnection of the existing mqtt.Client; a fresh connect() would
            # spawn a DUPLICATE client + listener alongside the still-live one
            # (see connect()'s note). The publish error is surfaced to the caller;
            # real liveness is judged by info.online in fetch().
            return {"success": False, "detail": str(e)}

    def fetch(self):
        try:
            device = self.client.get_device()
        except Exception:
            device = None

        # pybambu создаёт Device прямо в конструкторе клиента, поэтому
        # get_device() никогда не возвращает None — после выключения принтера
        # он вечно отдаёт последнюю закешированную телеметрию. Живость
        # определяет info.online: его сбрасывают paho on_disconnect при
        # разрыве MQTT и 30-секундный watchdog при тишине в эфире. Стейл
        # нельзя отдавать наверх — иначе выключенный принтер навсегда
        # остаётся в последнем статусе (или в «простое» после рестарта Pi).
        if device is not None and getattr(getattr(device, "info", None), "online", False):
            return device

        if self.connected:
            # MQTT-сессия уже поднималась: mqtt_listen_thread в pybambu сам
            # бесконечно переподключается (reconnect_delay 1s). Повторный
            # connect() породил бы дубль клиента и потока на каждый опрос.
            raise ConnectionError("MQTT telemetry lost (printer powered off or unreachable)")

        if not self.is_reachable():
            raise ConnectionError(f"TCP unreachable at {self.host}:8883/8884")

        try:
            self.connect()
        except Exception as e:
            self.connected = False
            raise ConnectionError(f"MQTT connect failed: {e}")

        # connect() асинхронный — первая телеметрия придёт чуть позже. Этот
        # цикл честно считаем неудачным: grace-период в poll_loop закроет
        # разрыв, а следующий опрос уже увидит info.online=True.
        raise ConnectionError("MQTT connecting — telemetry not received yet")
