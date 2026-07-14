import logging

from creality_client import CrealityK1Client

logger = logging.getLogger(__name__)


class CrealityCollector:
    def __init__(self, label: str, host: str):
        self.label = label
        self.client = CrealityK1Client(host, label)

    def fetch(self):
        try:
            if not self.client.connected:
                self.client.connect()
            if not self.client.connected:
                raise ConnectionError("WebSocket not connected")
            data = self.client.get_data()
            if not data:
                raise ConnectionError("WebSocket connected but no telemetry data received")
            return data
        except ConnectionError:
            raise
        except Exception as e:
            raise ConnectionError(f"WebSocket fetch failed: {e}")

    def send_command(self, command: str, param: str = None) -> dict:
        return self.client.send_command(command, param)
