import logging

from mks_wifi_client import MksWifiClient

logger = logging.getLogger(__name__)


class MksWifiCollector:
    def __init__(self, label: str, host: str, port: int = 8080):
        self.label = label
        self.client = MksWifiClient(host, label, port=port)

    def fetch(self):
        try:
            data = self.client.get_data()
            if not data:
                raise ConnectionError("HTTP request succeeded but no telemetry data received")
            return data
        except ConnectionError:
            raise
        except Exception as e:
            raise ConnectionError(f"MKS HTTP fetch failed: {e}")

    def send_command(self, command: str, param: str = None) -> dict:
        # MKS WiFi (Marlin-based) accepts raw G-code over TCP.
        #   M25 = pause SD print, M24 = resume SD print, M524 = abort SD print.
        gcode_map = {
            "pause": "M25",
            "resume": "M24",
            "stop": "M524",
        }
        try:
            if command == "gcode_line":
                if not param:
                    return {"success": False, "detail": "No gcode provided"}
                line = param
            elif command in gcode_map:
                line = gcode_map[command]
            else:
                return {"success": False, "detail": f"Unsupported command: {command}"}

            self.client.send_gcode(line)
            return {"success": True, "detail": "Command sent"}
        except Exception as e:
            return {"success": False, "detail": str(e)}
