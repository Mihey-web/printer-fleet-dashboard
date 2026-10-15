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
    # The (vendored) pybambu creates the Device object in the client constructor, so
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
    # A publish exception must NOT clear self.connected: the vendored pybambu's listener thread
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


def test_creality_numeric_state_5_is_paused():
    # Ender 5 Max сообщает паузу кодом 5 (проверено вживую на E5). Раньше он
    # проваливался в дефолт 'idle', normalizer промоутил idle обратно в
    # 'printing' — и пауза была не видна на карточке вообще.
    c = CrealityK1Client("host", "E5")
    c.on_message(None, '{"state": 5, "printLeftTime": 16352}')
    c.connected = True
    assert c.get_data()["state"] == "paused"


def test_creality_numeric_state_known_codes():
    c = CrealityK1Client("host", "E5")
    c.connected = True
    for code, want in ((0, "idle"), (1, "printing"), (2, "paused"), (3, "finish")):
        c.on_message(None, '{"state": %d}' % code)
        assert c.get_data()["state"] == want


def test_creality_unknown_numeric_state_is_not_idle():
    # Неизвестный код не должен маскироваться под 'idle' — иначе следующая
    # незнакомая пауза снова станет невидимой.
    c = CrealityK1Client("host", "E5")
    c.on_message(None, '{"state": 42}')
    c.connected = True
    assert c.get_data()["state"] == "unknown"


# --- Klipper: light pin discovery, acknowledged commands (run_action) -------------

from klipper_client import pick_light_pin


@pytest.mark.parametrize("objects,want", [
    (["output_pin caselight", "output_pin beeper"], "caselight"),
    (["output_pin led_strip", "output_pin chamber_light"], "chamber_light"),   # "light" before "led"
    (["output_pin Case_Light", "output_pin chamber_light"], "Case_Light"),     # caselight / case_light first
    (["output_pin top_lamp", "output_pin led_bar"], "led_bar"),
    (["output_pin top_lamp"], "top_lamp"),
    (["output_pin light;M112", "output_pin bad name light"], None),           # unsafe for G-code: ignored
    (["output_pin beeper", "fan", "heater_bed", "led caselight"], None),       # only output_pin
    ([], None),
    (None, None),
    # whole words only: "enabled", "disabled", "cooled", "controlled", "sled" hold "led",
    # "flight", "highlight" hold "light" — none of them is a light (SET_PIN would switch it)
    (["output_pin motors_enabled"], None),
    (["output_pin stepper_disabled"], None),
    (["output_pin hotend_cooled"], None),
    (["output_pin fan_speed_controlled"], None),
    (["output_pin sled_lock"], None),
    (["output_pin flight_mode"], None),
    (["output_pin highlight_unused"], None),
    (["output_pin motors_enabled", "output_pin top_lamp"], "top_lamp"),
    (["output_pin led_strip"], "led_strip"),
    (["output_pin chamber_light"], "chamber_light"),
    (["output_pin light_2", "output_pin leds"], "light_2"),                    # digits split words; plurals
    (["output_pin LEDStrip", "output_pin top_lamps"], "LEDStrip"),             # camelCase
    (["output_pin ChamberLight", "output_pin LEDStrip"], "ChamberLight"),
    (["output_pin CaseLight", "output_pin ChamberLight"], "CaseLight"),
    (["output_pin my_caselight", "output_pin led_bar"], "my_caselight"),
])
def test_pick_light_pin(objects, want):
    assert pick_light_pin(objects) == want


class _FakeWs:
    """Records sent requests; `answer(req)` may return a reply delivered at once."""

    def __init__(self, client, answer=None):
        self.client, self.answer, self.sent = client, answer, []

    def send(self, raw):
        req = json.loads(raw)
        self.sent.append(req)
        reply = self.answer(req) if self.answer else None
        if reply is not None:
            self.client.on_message(None, json.dumps(dict(reply, jsonrpc="2.0", id=req["id"])))


def _ready_klipper(answer=None, pin=None):
    k = KlipperClient("host", 7125, "Ghost 6")
    k.ws = _FakeWs(k, answer)
    k.connected = True
    if pin is not None:
        k.light_pin = pin
    return k


@pytest.mark.parametrize("action,method", [("pause", "printer.print.pause"), ("resume", "printer.print.resume"),
                                           ("stop", "printer.print.cancel")])
def test_klipper_run_action_confirmed(action, method):
    k = _ready_klipper(lambda req: {"result": "ok"})
    assert k.run_action(action, timeout=1) == {"success": True, "replied": True, "detail": "подтверждено принтером"}
    assert [r["method"] for r in k.ws.sent] == [method]
    assert k._pending == {}


def test_klipper_run_action_rejected_silent_or_disconnected():
    k = _ready_klipper(lambda req: {"error": {"code": 400, "message": "Print not paused <x>"}})
    r = k.run_action("resume", timeout=1)
    assert r == {"success": False, "replied": True, "detail": "Klipper отклонил: Print not paused <x>"}
    k = _ready_klipper()                                    # Moonraker never answers
    assert k.run_action("pause", timeout=0.05) == {"success": True, "replied": False,
                                                   "detail": "отправлено (без подтверждения)"}
    assert k._pending == {}
    k.connected = False
    assert k.run_action("pause", timeout=0.05) == {"success": False, "replied": False,
                                                   "detail": "Нет связи с Moonraker"}

    class _Broken:
        def send(self, raw):
            raise OSError("socket closed")

    k = _ready_klipper()
    k.ws = _Broken()
    assert k.run_action("stop", timeout=0.05) == {"success": False, "replied": False, "detail": "socket closed"}
    assert k._pending == {}


def test_klipper_send_command_is_deprecated():
    # printer_commands sends Klipper actions through run_action; the old fire-and-forget pair
    # (client + collector) has no caller left and warns until a cleanup removes it
    from app.collectors.klipper_collector import KlipperCollector

    k = KlipperClient("host", 7125, "L")
    with pytest.warns(DeprecationWarning, match="run_action"):
        assert k.send_command("pause") == {"success": False, "detail": "Not connected"}
    with pytest.warns(DeprecationWarning, match="run_action"):
        KlipperCollector("L", "host", 7125).send_command("pause")


def test_klipper_light_through_set_pin():
    k = _ready_klipper(lambda req: {"result": "ok"}, pin="caselight")
    assert k.run_action("light_on", timeout=1)["replied"] is True
    assert k.run_action("light_off", timeout=1)["success"] is True
    assert [r["params"]["script"] for r in k.ws.sent] == ["SET_PIN PIN=caselight VALUE=1",
                                                          "SET_PIN PIN=caselight VALUE=0"]
    assert [r["method"] for r in k.ws.sent] == ["printer.gcode.script"] * 2
    k = _ready_klipper(lambda req: {"result": "ok"}, pin="")
    r = k.run_action("light_on", timeout=1)
    assert r["success"] is False and "пин подсветки" in r["detail"] and k.ws.sent == []
    assert _ready_klipper().run_action("explode")["success"] is False


def test_klipper_confirmed_light_is_in_the_data_at_once():
    # Klipper pushes the pin's value on its own schedule (~250 ms, later during a print): a
    # confirmed light is in get_data() from the reply on, so the store refresh that follows
    # it in printer_commands already carries the new state.
    k = _ready_klipper(lambda req: {"result": "ok"}, pin="caselight")
    k.parse_status({"print_stats": {"state": "standby"}, "output_pin caselight": {"value": 0}})
    assert k.get_data()["light_on"] is False
    assert k.run_action("light_on", timeout=1)["replied"] is True
    assert k.get_data()["light_on"] is True
    assert k.run_action("light_off", timeout=1)["replied"] is True
    assert k.get_data()["light_on"] is False
    # unanswered or refused: it may not have switched — the data waits for Klipper's own push
    k.ws.answer = None
    assert k.run_action("light_on", timeout=0.05)["replied"] is False
    assert k.get_data()["light_on"] is False
    k.ws.answer = lambda req: {"error": {"message": "Unknown pin"}}
    assert k.run_action("light_on", timeout=1)["success"] is False
    assert k.get_data()["light_on"] is False
    k.ws.answer = lambda req: {"result": "ok"}
    assert k.run_action("pause", timeout=1)["replied"] is True          # only a light touches it
    assert k.get_data()["light_on"] is False


def test_klipper_reply_is_routed_not_parsed_as_status():
    k = _ready_klipper(lambda req: {"result": {"status": {"print_stats": {"state": "printing"}}}})
    assert k.run_action("pause", timeout=1)["replied"] is True
    assert k.get_data() == {}                    # the reply was not taken for telemetry
    assert k.last_message_ts == 0.0


_OBJECTS = {"objects": ["webhooks", "print_stats", "output_pin beeper", "output_pin CaseLight", "fan"]}


def _discovering_klipper(objects=_OBJECTS, scale=255):
    def answer(req):
        if req["method"] == "printer.objects.list":
            return {"result": objects}
        if req["method"] == "printer.objects.query" and "configfile" in req["params"]["objects"]:
            # Klipper lower-cases the section names of configfile.settings
            return {"result": {"status": {"configfile": {"settings": {
                "output_pin caselight": {"pwm": False, "scale": scale}}}}}}
        return None
    return _ready_klipper(answer)


def test_klipper_discovers_the_light_pin_and_its_scale():
    k = _discovering_klipper()
    k.on_open(k.ws)
    methods = [r["method"] for r in k.ws.sent]
    assert methods[:3] == ["printer.objects.subscribe", "printer.objects.query", "printer.objects.list"]
    assert k.light_pin == "CaseLight" and k.light_scale == 255.0
    # Moonraker replaces a connection's subscription: the full set again, plus the pin
    subs = [r for r in k.ws.sent if r["method"] == "printer.objects.subscribe"]
    assert len(subs) == 2
    assert subs[-1]["params"]["objects"]["output_pin CaseLight"] == ["value"]
    assert "print_stats" in subs[-1]["params"]["objects"]
    k.parse_status({"print_stats": {"state": "standby"}})
    assert k.get_data()["light_pin"] == "CaseLight"
    k.ws.answer = lambda req: {"result": "ok"}
    k.run_action("light_on", timeout=1)
    assert k.ws.sent[-1]["params"]["script"] == "SET_PIN PIN=CaseLight VALUE=255"


def test_klipper_without_a_light_pin_or_with_an_error():
    k = _discovering_klipper(objects={"objects": ["webhooks", "output_pin beeper"]})
    k.on_open(k.ws)
    k.parse_status({"print_stats": {"state": "standby"}})
    assert k.light_pin == "" and k.get_data()["light_pin"] == ""
    assert [r["method"] for r in k.ws.sent].count("printer.objects.subscribe") == 1
    k = _ready_klipper(lambda req: {"error": {"message": "Klippy Host not connected"}}
                       if req["method"] == "printer.objects.list" else None)
    k.on_open(k.ws)
    k.parse_status({"print_stats": {"state": "standby"}})
    assert k.light_pin is None and "light_pin" not in k.get_data()


def test_klipper_rediscovers_when_klippy_becomes_ready():
    k = _ready_klipper()                       # the objects.list request stays unanswered
    k.on_message(None, json.dumps({"jsonrpc": "2.0", "method": "notify_status_update",
                                   "params": [{"webhooks": {"state": "shutdown", "state_message": "MCU"}}]}))
    assert [r["method"] for r in k.ws.sent] == []
    k.on_message(None, json.dumps({"jsonrpc": "2.0", "method": "notify_status_update",
                                   "params": [{"webhooks": {"state": "ready"}}]}))
    assert [r["method"] for r in k.ws.sent] == ["printer.objects.list"]
    # still ready (or the same discovery in flight): no second request
    k.on_message(None, json.dumps({"jsonrpc": "2.0", "method": "notify_status_update",
                                   "params": [{"webhooks": {"state": "ready"}}]}))
    assert [r["method"] for r in k.ws.sent] == ["printer.objects.list"]


def test_klipper_light_state_from_the_pin_value():
    k = _ready_klipper(pin="caselight")
    k.subscribe_to_status()
    assert k.ws.sent[0]["params"]["objects"]["output_pin caselight"] == ["value"]
    k.parse_status({"output_pin caselight": {"value": 0.5}})
    assert k.get_data()["light_on"] is True
    k.parse_status({"output_pin caselight": {"value": 0}})
    assert k.get_data()["light_on"] is False
    k.parse_status({"output_pin beeper": {"value": 1}})
    assert k.get_data()["light_on"] is False


# --- Creality: light state and the (not yet live-verified) light switch -----------

def test_creality_light_state_and_switch():
    c = CrealityK1Client("host", "K1")
    c.on_message(None, '{"lightSw": 1}')
    c.connected = True
    assert c.get_data()["light_on"] is True
    c.on_message(None, '{"lightSw": 0}')
    assert c.get_data()["light_on"] is False
    c.on_message(None, '{"lightSw": "x", "nozzleTemp": 20}')       # unparseable: last value kept
    assert c.get_data()["light_on"] is False

    sent = []
    c.ws = type("W", (), {"send": lambda self, raw: sent.append(json.loads(raw))})()
    assert c.send_command("light_off")["success"] is True
    assert c.send_command("light_on")["success"] is True
    assert sent == [{"method": "set", "params": {"lightSw": 0}}, {"method": "set", "params": {"lightSw": 1}}]
