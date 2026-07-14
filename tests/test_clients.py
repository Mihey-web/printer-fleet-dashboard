"""Behavioural tests for the WebSocket printer clients (Klipper / Creality).

Focus on the thread-safety hardening: get_data() must hand out a private copy
(so the normalizer can't mutate live telemetry), request ids must advance
atomically, and parsing must populate the expected fields. No sockets used.
"""
import threading
import time

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
