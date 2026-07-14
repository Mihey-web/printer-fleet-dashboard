import logging

from klipper_client import KlipperClient

logger = logging.getLogger(__name__)


class KlipperCollector:
    def __init__(self, label: str, host: str, port: int):
        self.label = label
        # Connect lazily in fetch() (like CrealityCollector) rather than in the
        # constructor, so build_collectors() at startup doesn't block on a slow
        # WebSocket handshake and delay the HTTP port bind.
        self.client = KlipperClient(host, port, label)

    def fetch(self):
        if not self.client.connected:
            self.client.connect()
        if not self.client.connected:
            raise ConnectionError("Moonraker WebSocket not connected")
        data = self.client.get_data()
        if not data:
            raise ConnectionError("Moonraker WebSocket connected but no telemetry data received")
        return data

    def send_command(self, command: str, param: str = None) -> dict:
        return self.client.send_command(command, param)
