"""Behavioural tests for the WebSocket printer clients (Klipper / Creality).

Focus on the thread-safety hardening: get_data() must hand out a private copy
(so the normalizer can't mutate live telemetry), request ids must advance
atomically, and parsing must populate the expected fields. No sockets used.
"""
import threading
import time

import pytest

from klipper_client import KlipperClient
from creality_client import CrealityK1Client


def test_klipper_parse_and_get_data_copy():
    k = KlipperClient("host", 7125, "(13) Varan")
    k.parse_status({
        "print_stats": {"state": "printing", "print_duration": 120,
                        "info": {"current_layer": 5, "total_layer": 100}},
        "display_status": {"progress": 0.5},
        "extruder": {"temperature": 210.4, "target": 210},
    })
    snap = k.get_data()
    assert snap["state"] == "printing"
    assert snap["progress"] == 50
    assert snap["nozzle_temp"] == 210
    assert snap["current_layer"] == 5

    # Mutating the returned dict must not touch internal state.
    snap["state"] = "tampered"
    assert k.get_data()["state"] == "printing"


def test_klipper_next_id_is_atomic_and_monotonic():
    k = KlipperClient("host", 7125, "L")
    ids = []

    def grab():
        for _ in range(200):
            ids.append(k._next_id())

    threads = [threading.Thread(target=grab) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # No id handed out twice despite 4 concurrent threads.
    assert len(ids) == len(set(ids)) == 800


def test_creality_offline_returns_empty_then_parses_when_connected():
    c = CrealityK1Client("host", "(11) K1C")
    c.on_message(None, '{"printStatus": "printing", "nozzleTemp": 210}')
    # Not connected yet -> treated as offline.
    assert c.get_data() == {}
    c.connected = True
    data = c.get_data()
    assert data["state"] == "printing"
    assert data["nozzle_temp"] == 210


def test_creality_parses_material_status_top_level():
    # materialStatus is the only runout signal K1 firmware gives (state stays
    # "printing"), so it must land in the top-level data, not just debug.
    c = CrealityK1Client("host", "(11) K1C")
    c.on_message(None, '{"printStatus": "printing", "materialStatus": 1}')
    c.connected = True
    data = c.get_data()
    assert data["material_status"] == 1
    c.on_message(None, '{"materialStatus": 0}')
    assert c.get_data()["material_status"] == 0


def test_creality_get_data_returns_copy():
    c = CrealityK1Client("host", "L")
    c.on_message(None, '{"printStatus": "printing"}')
    c.connected = True
    snap = c.get_data()
    snap["state"] = "tampered"
    assert c.get_data()["state"] == "printing"


def test_klipper_get_data_stale_forces_offline():
    # A hung socket (no fresh message for >30s) must surface as offline instead
    # of replaying the last telemetry forever.
    k = KlipperClient("host", 7125, "L")
    k.parse_status({"print_stats": {"state": "printing"}})
    k.connected = True
    k.last_message_ts = time.time() - 31
    assert k.get_data() == {}
    assert k.connected is False


def test_klipper_get_data_fresh_is_served():
    k = KlipperClient("host", 7125, "L")
    k.parse_status({"print_stats": {"state": "printing"}})
    k.connected = True
    k.last_message_ts = time.time()
    assert k.get_data()["state"] == "printing"


def _bambu_collector_with_device(online: bool):
    from app.collectors.bambu_collector import BambuCollector

    c = BambuCollector("L", "host", "code", "serial")

    class _Info:
        pass

    class _Device:
        pass

    device = _Device()
    device.info = _Info()
    device.info.online = online

    class _Client:
        connected = online

        def get_device(self):
            return device

    c.client = _Client()
    return c, device


def test_bambu_fetch_stale_device_raises():
    # pybambu creates the Device object in the client constructor, so
    # get_device() never returns None — after the printer powers off it keeps
    # handing out the last cached telemetry with info.online=False. fetch()
    # must surface that as a connection error (grace -> offline in poll_loop)
    # instead of replaying the stale status forever.
    c, _ = _bambu_collector_with_device(online=False)
    c.connected = True
    reconnects = []
    c.connect = lambda: reconnects.append(1)

    with pytest.raises(ConnectionError):
        c.fetch()
    # paho's listener thread auto-reconnects on its own; fetch() must not
    # spawn a duplicate MQTT client/thread every poll cycle.
    assert reconnects == []


def test_bambu_fetch_live_device_returned():
    c, device = _bambu_collector_with_device(online=True)
    c.connected = True
    assert c.fetch() is device


# --- Liveness must come from real telemetry, not keepalive frames -------------

import json


def test_klipper_liveness_only_from_status_messages():
    # Moonraker broadcasts server-process notifications (notify_proc_stat_update,
    # ~1/s) to every socket even after klippy has shut down. Those must NOT count
    # as liveness — otherwise a dead print's last state ("printing") never goes
    # stale and is served forever. Only an actual printer-status message counts.
    k = KlipperClient("host", 7125, "L")
    k.last_message_ts = 0.0
    k.on_message(None, json.dumps({
        "jsonrpc": "2.0", "method": "notify_proc_stat_update",
        "params": [{"cpu_temp": 55}],
    }))
    assert k.last_message_ts == 0.0  # keepalive/proc-stat did not refresh liveness

    k.on_message(None, json.dumps({
        "jsonrpc": "2.0", "method": "notify_status_update",
        "params": [{"print_stats": {"state": "printing"}}],
    }))
    assert k.last_message_ts > 0.0  # real status update did refresh it


def test_creality_heartbeat_does_not_refresh_liveness():
    # The K1 sends heart_beat keepalives independent of telemetry. A heartbeat
    # alone must not keep the 30s staleness guard alive, or a printer whose real
    # telemetry has frozen keeps showing its last state.
    c = CrealityK1Client("host", "L")
    c.last_message_ts = 0.0

    class _WS:
        def send(self, _msg):
            pass

    c.on_message(_WS(), json.dumps({"ModeCode": "heart_beat"}))
    assert c.last_message_ts == 0.0  # heartbeat is a keepalive, not telemetry

    c.on_message(_WS(), json.dumps({"nozzleTemp": 25}))
    assert c.last_message_ts > 0.0  # real telemetry refreshed it


def test_klipper_connect_skips_when_already_connected(monkeypatch):
    # connect() must be idempotent under a lock: called while already connected it
    # must not build a second socket. Two reconnect drivers (poll fetch() and the
    # on_close reconnect loop) otherwise race and spawn duplicate run_forever
    # threads / leaked sockets.
    built = []
    monkeypatch.setattr("websocket.WebSocketApp", lambda *a, **k: built.append(1))
    k = KlipperClient("host", 7125, "L")
    assert hasattr(k, "_connect_lock")
    k.connected = True
    k.connect()
    assert built == []


def test_creality_connect_skips_when_already_connected(monkeypatch):
    built = []
    monkeypatch.setattr("websocket.WebSocketApp", lambda *a, **k: built.append(1))
    c = CrealityK1Client("host", "L")
    assert hasattr(c, "_connect_lock")
    c.connected = True
    c.connect()
    assert built == []


def test_klipper_get_data_empty_before_any_telemetry():
    # self.data is pre-seeded with defaults; before any real status message is
    # parsed, get_data() must report "no telemetry" (empty) so a connected-but-
    # silent Moonraker isn't shown online with a bogus 'unknown' state.
    k = KlipperClient("host", 7125, "L")
    k.connected = True
    assert k.get_data() == {}
    k.parse_status({"print_stats": {"state": "printing"}})
    assert k.get_data()["state"] == "printing"


def test_bambu_send_command_publish_error_keeps_connected():
    # A publish exception must NOT clear self.connected: pybambu's listener thread
    # owns reconnection, and a fresh connect() would spawn a duplicate mqtt client
    # + listener alongside the live one.
    c, _ = _bambu_collector_with_device(online=True)
    c.connected = True

    class _Client:
        connected = True

        def publish(self, payload):
            raise RuntimeError("boom")

    c.client = _Client()
    r = c.send_command("gcode_line", "G28")
    assert r["success"] is False
    assert c.connected is True
