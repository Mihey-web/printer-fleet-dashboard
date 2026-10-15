"""Команды принтерам: точные payload'ы, локальный канал с ответами, зонд капабилити."""
import json
import threading
import time

import pytest

from app.services import printer_commands


def REFUSED(detail):
    """The server did not send (kind, caps, params, state, planning)."""
    return {"success": False, "status": "refused", "detail": detail, "replied": False}


def REJECTED(detail, replied=True):
    """The printer (replied) or the transport said no."""
    return {"success": False, "status": "rejected", "detail": detail, "replied": replied}


CONFIRMED = {"success": True, "status": "confirmed", "detail": "подтверждено принтером", "replied": True}
SENT = {"success": True, "status": "sent", "detail": "отправлено (без подтверждения)", "replied": False}


def test_note_capability_transient_silence_keeps_prior_verdict(monkeypatch):
    # A silent probe (None) must not wipe a known verdict, or the control buttons
    # vanish until the next probe (~10 min).
    from app.services import settings_service
    monkeypatch.setattr(settings_service, "set_many", lambda d, **k: None)
    printer_commands._capability.clear()
    printer_commands._note_capability("p1", True, "local")
    assert printer_commands.get_capability("p1") is True
    printer_commands._note_capability("p1", None, None)      # transient silence
    assert printer_commands.get_capability("p1") is True     # kept
    printer_commands._note_capability("p1", False, "local")  # real verdict updates
    assert printer_commands.get_capability("p1") is False
    printer_commands._capability.clear()


@pytest.fixture
def cmd_env(monkeypatch):
    monkeypatch.setattr(printer_commands, "REPLY_TIMEOUT", 0.05)
    # изолируем персист capability от реальной printer_history.db
    from app.services import settings_service
    _saved: dict = {}
    monkeypatch.setattr(settings_service, "set_many", lambda d, **k: _saved.update(d))
    monkeypatch.setattr(settings_service, "get", lambda key, **k: _saved.get(key, {}))
    printer_commands._capability.clear()
    printer_commands._ams_inflight.clear()
    printer_commands._eject_marks.clear()
    getattr(printer_commands, "_dry_started", {}).clear()
    # no live-refresh threads after a successful command (tested on its own)
    monkeypatch.setattr(printer_commands, "_start_fast_refresh", lambda *a, **k: None, raising=False)
    yield
    printer_commands._capability.clear()
    printer_commands._ams_inflight.clear()
    printer_commands._eject_marks.clear()
    getattr(printer_commands, "_dry_started", {}).clear()
    getattr(printer_commands, "_fast_refresh_until", {}).clear()
    printer_commands.set_store(None)


def _bambu_registry(monkeypatch, model=None):
    monkeypatch.setattr("app.services.printer_registry.get_printer",
                        lambda pid: {"id": pid, "kind": "bambu", "serial": "SN1",
                                     "model": model})


class _Store:
    """Стор с одним фейковым коллектором; publish задаётся снаружи."""

    def __init__(self, publish):
        class _Collector:
            pass
        self._c = _Collector()
        self._c.client = type("C", (), {"publish": staticmethod(publish)})()

    def get_collector(self, pid):
        return self._c


def _replying(result, reason=""):
    """publish, который синхронно отвечает на свой же sequence_id."""
    def publish(payload):
        cls = "print" if "print" in payload else "system"
        reply = json.dumps({cls: {"sequence_id": payload[cls]["sequence_id"],
                                  "result": result, "reason": reason}}).encode()
        printer_commands.note_local_reply("SN1", reply)
        return True
    return publish


def test_build_light_payloads():
    on = printer_commands.build_light(True)
    assert on == {"system": {"sequence_id": "0", "command": "ledctrl",
                             "led_node": "chamber_light", "led_mode": "on",
                             "led_on_time": 500, "led_off_time": 500,
                             "loop_times": 0, "interval_time": 0}}
    assert printer_commands.build_light(False)["system"]["led_mode"] == "off"


def test_build_print_actions():
    for action in ("pause", "resume", "stop"):
        assert printer_commands.build_print_action(action) == {
            "print": {"sequence_id": "0", "command": action, "param": ""}}


def test_build_speed():
    assert printer_commands.build_speed(3) == {
        "print": {"sequence_id": "0", "command": "print_speed", "param": "3"}}


def test_build_gcode_joins_lines_with_newlines():
    p = printer_commands.build_gcode(["M104 S200", "M140 S60"])
    assert p["print"]["command"] == "gcode_line"
    assert p["print"]["param"] == "M104 S200\nM140 S60\n"


def test_build_fans_maps_percent_to_255_and_skips_none():
    p = printer_commands.build_fans(part=100, aux=None, chamber=50)
    assert p["print"]["param"] == "M106 P1 S255\nM106 P3 S128\n"


def test_build_preheat_and_cooldown():
    assert printer_commands.build_preheat(220, 65)["print"]["param"] == "M104 S220\nM140 S65\n"
    cd = printer_commands.build_cooldown()["print"]["param"]
    assert "M104 S0" in cd and "M140 S0" in cd and "M106 P1 S0" in cd


def test_build_eject_is_relative_z_down_without_homing():
    p = printer_commands.build_eject()["print"]["param"]
    assert "G91" in p and "G1 Z30" in p and "G90" in p
    assert "G28" not in p  # без хоуминга — не таранит деталь


def test_speed_validates_range(cmd_env, monkeypatch):
    _bambu_registry(monkeypatch)
    r = printer_commands.send("p1", "speed", {"level": 9})
    assert not r["success"] and "1-4" in r["detail"]


def test_fans_requires_at_least_one(cmd_env, monkeypatch):
    _bambu_registry(monkeypatch)
    r = printer_commands.send("p1", "fans", {})
    assert not r["success"] and "вентилятор" in r["detail"]


def test_preheat_requires_a_target(cmd_env, monkeypatch):
    _bambu_registry(monkeypatch)
    r = printer_commands.send("p1", "preheat", {})
    assert not r["success"] and "температура" in r["detail"].lower()


def test_speed_confirmed_and_marks_capability(cmd_env, monkeypatch):
    _bambu_registry(monkeypatch)
    printer_commands.set_store(_Store(_replying("success")))
    r = printer_commands.send("p1", "speed", {"level": 2})
    assert r["success"] and printer_commands.get_capability("p1") is True


def test_send_rejects_unknown_printer(cmd_env, monkeypatch):
    monkeypatch.setattr("app.services.printer_registry.get_printer", lambda pid: None)
    r = printer_commands.send("p1", "light_on")
    assert not r["success"]


def test_send_unknown_action(cmd_env, monkeypatch):
    _bambu_registry(monkeypatch)
    r = printer_commands.send("p1", "explode")
    assert not r["success"] and "Неизвестная команда" in r["detail"]


def test_send_no_collector(cmd_env, monkeypatch):
    _bambu_registry(monkeypatch)
    printer_commands.set_store(None)
    r = printer_commands.send("p1", "light_on")
    assert not r["success"] and "коллектор" in r["detail"]


def test_send_confirmed_by_printer(cmd_env, monkeypatch):
    _bambu_registry(monkeypatch)
    printer_commands.set_store(_Store(_replying("success")))
    r = printer_commands.send("p1", "pause")
    assert r == CONFIRMED
    assert printer_commands.get_capability("p1") is True


def test_send_without_reply_still_sent(cmd_env, monkeypatch):
    _bambu_registry(monkeypatch)
    published = []
    printer_commands.set_store(_Store(lambda payload: published.append(payload) or True))
    r = printer_commands.send("p1", "light_on")
    assert r == SENT
    assert published[0]["system"]["command"] == "ledctrl"
    # свет — system-класс, капабилити print-класса не трогает
    assert printer_commands.get_capability("p1") is None


def test_verify_failed_marks_blocked(cmd_env, monkeypatch):
    _bambu_registry(monkeypatch)
    printer_commands.set_store(_Store(_replying("failed", "mqtt message verify failed")))
    r = printer_commands.send("p1", "pause")
    assert r == REJECTED("🔒 прошивка блокирует сторонние команды")
    assert printer_commands.get_capability("p1") is False


def test_probe_blocked_then_capable(cmd_env, monkeypatch):
    _bambu_registry(monkeypatch)
    printer_commands.set_store(_Store(_replying("failed", "mqtt message verify failed")))
    assert printer_commands.probe("p1") is False
    # принтер перевели в Developer Mode — следующий зонд видит ответ без verify failed
    printer_commands.set_store(_Store(_replying("success")))
    assert printer_commands.probe("p1") is True
    assert printer_commands.get_capability("p1") is True


def test_probe_silence_is_unknown(cmd_env, monkeypatch):
    _bambu_registry(monkeypatch)
    printer_commands.set_store(_Store(lambda payload: True))
    assert printer_commands.probe("p1") is None
    assert printer_commands.get_capability("p1") is None


def test_capability_persists_and_reloads(monkeypatch):
    """Вердикт зонда переживает рестарт: bool сохраняется, None не затирает."""
    from app.services import settings_service
    saved: dict = {}
    monkeypatch.setattr(settings_service, "set_many", lambda d, **k: saved.update(d))
    monkeypatch.setattr(settings_service, "get", lambda key, **k: saved.get(key, {}))
    printer_commands._capability.clear()

    printer_commands._note_capability("bambu-9", False, "local")   # 2S — блок
    printer_commands._note_capability("bambu-6", True, "local")    # C3 — можно
    printer_commands._note_capability("bambu-7", None, "local")    # молчит — не персистим
    assert saved["printer_capability"] == {"bambu-9": False, "bambu-6": True}

    # эмулируем рестарт: память пуста, но БД помнит
    printer_commands._capability.clear()
    assert printer_commands.get_capability("bambu-9") is None
    printer_commands.load_persisted_capabilities()
    assert printer_commands.get_capability("bambu-9") is False
    assert printer_commands.get_capability("bambu-6") is True
    assert printer_commands.get_capability("bambu-7") is None
    printer_commands._capability.clear()


class _CrealityStore:
    """Стор с Creality-коллектором: пишет вызовы send_command в calls."""

    def __init__(self, reply=None, boom=None):
        self.calls = []
        outer = self

        class _Collector:
            def send_command(self, command, param=None):
                outer.calls.append((command, param))
                if boom is not None:
                    raise boom
                return reply if reply is not None else {"success": True, "detail": "Command sent"}

        self._c = _Collector()

    def get_collector(self, pid):
        return self._c


def _creality_registry(monkeypatch):
    monkeypatch.setattr("app.services.printer_registry.get_printer",
                        lambda pid: {"id": pid, "kind": "creality"})


def test_send_creality_pause_goes_to_collector(cmd_env, monkeypatch):
    _creality_registry(monkeypatch)
    store = _CrealityStore()
    printer_commands.set_store(store)
    r = printer_commands.send("p1", "pause")
    assert r["success"]
    assert store.calls == [("pause", None)]


def test_send_creality_resume_and_stop(cmd_env, monkeypatch):
    _creality_registry(monkeypatch)
    store = _CrealityStore()
    printer_commands.set_store(store)
    assert printer_commands.send("p1", "resume")["success"]
    assert printer_commands.send("p1", "stop")["success"]
    assert store.calls == [("resume", None), ("stop", None)]


def test_send_creality_rejects_bambu_only_action(cmd_env, monkeypatch):
    # Канал Creality ("set" на WS :9999) умеет только управление печатью и
    # свет — скорость/AMS/остывание туда не пробросить, отклоняем до отправки.
    _creality_registry(monkeypatch)
    store = _CrealityStore()
    printer_commands.set_store(store)
    for action in ("speed", "ams_load", "cooldown"):
        r = printer_commands.send("p1", action, {"level": 2, "slot": 0})
        assert not r["success"] and "Creality" in r["detail"]
    assert store.calls == []


def test_send_creality_client_failure_surfaces_detail(cmd_env, monkeypatch):
    _creality_registry(monkeypatch)
    printer_commands.set_store(_CrealityStore(reply={"success": False, "detail": "Not connected"}))
    r = printer_commands.send("p1", "pause")
    assert not r["success"] and r["detail"] == "Not connected"


def test_send_creality_exception_does_not_escape(cmd_env, monkeypatch):
    _creality_registry(monkeypatch)
    printer_commands.set_store(_CrealityStore(boom=RuntimeError("socket dead")))
    r = printer_commands.send("p1", "pause")
    assert not r["success"] and "socket dead" in r["detail"]


def test_send_creality_no_collector(cmd_env, monkeypatch):
    _creality_registry(monkeypatch)
    printer_commands.set_store(None)
    r = printer_commands.send("p1", "pause")
    assert not r["success"] and "коллектор" in r["detail"]


def test_send_rejects_unsupported_kind(cmd_env, monkeypatch):
    # MKS: канал команд не подключён — отказ, а не тихий успех (Klipper — ниже).
    monkeypatch.setattr("app.services.printer_registry.get_printer",
                        lambda pid: {"id": pid, "kind": "mks"})
    assert printer_commands.send("p1", "pause") == REFUSED(printer_commands.MSG_KIND)


def test_capability_for_by_kind(cmd_env, monkeypatch):
    from app.services import settings_service
    monkeypatch.setattr(settings_service, "set_many", lambda d, **k: None)
    # Creality: подписи нет, зондировать нечего — канал жив, пока принтер онлайн.
    assert printer_commands.capability_for("c1", "creality", True) is True
    assert printer_commands.capability_for("c1", "creality", False) is False
    # Bambu: только вердикт зонда.
    assert printer_commands.capability_for("b1", "bambu", True) is None
    printer_commands._note_capability("b1", True, "local")
    assert printer_commands.capability_for("b1", "bambu", True) is True
    # Остальные типы команд не поддерживают.
    assert printer_commands.capability_for("k1", "klipper", True) is None


# --- Камера H2S: M141 --------------------------------------------------------

def test_build_chamber_sets_m141():
    assert printer_commands.build_chamber(55)["print"]["param"] == "M141 S55\n"
    assert printer_commands.build_chamber(0)["print"]["param"] == "M141 S0\n"


def test_chamber_action_requires_temp_in_range():
    with pytest.raises(ValueError):
        printer_commands._build_payload("chamber", {})
    with pytest.raises(ValueError):
        printer_commands._build_payload("chamber", {"temp": printer_commands.CHAMBER_MAX + 1})
    with pytest.raises(ValueError):
        printer_commands._build_payload("chamber", {"temp": -1})
    p = printer_commands._build_payload("chamber", {"temp": printer_commands.CHAMBER_MAX})
    assert p["print"]["param"] == "M141 S65\n"


def test_has_chamber_heater_only_h2_series():
    assert printer_commands.has_chamber_heater("H2S")
    assert printer_commands.has_chamber_heater("h2d")
    assert not printer_commands.has_chamber_heater("P2S")
    assert not printer_commands.has_chamber_heater("X1C")
    assert not printer_commands.has_chamber_heater(None)


def test_send_chamber_rejected_without_heater(cmd_env, monkeypatch):
    _bambu_registry(monkeypatch, model="P2S")
    printer_commands.set_store(_Store(_replying("success")))
    r = printer_commands.send("p1", "chamber", {"temp": 50})
    assert not r["success"] and "H2" in r["detail"]


def test_send_chamber_on_h2s_publishes_m141(cmd_env, monkeypatch):
    _bambu_registry(monkeypatch, model="H2S")
    published = []
    printer_commands.set_store(_Store(lambda payload: published.append(payload) or True))
    r = printer_commands.send("p1", "chamber", {"temp": 60})
    assert r["success"]
    assert published[0]["print"]["command"] == "gcode_line"
    assert published[0]["print"]["param"] == "M141 S60\n"


# --- AMS: load / unload (ams_change_filament) ----------------------------------

import bambu_testkit as kit


def _tray(i, ttype, lo, hi, color="FF0000FF"):
    return {"id": str(i), "tray_type": ttype, "tray_color": color, "tray_info_idx": "GFG99",
            "nozzle_temp_min": str(lo), "nozzle_temp_max": str(hi), "remain": 80, "state": 11}


def _empty(i):
    return {"id": str(i), "state": 0}


def _unit(uid, trays, info="10002003", **extra):
    unit = {"id": str(uid), "humidity": "3", "humidity_raw": "20", "temp": "25.0", "info": info,
            "dry_time": 0, "dry_sf_reason": [], "tray": trays}
    unit.update(extra)
    return unit


# AMS 1 (id 0, slots 1-4), AMS 2 (id 1, slots 5-8), AMS HT 1 (id 128, slot 9).
UNITS = [
    _unit(0, [_tray(0, "PETG", 230, 270), _tray(1, "PLA", 190, 230), _empty(2), _empty(3)]),
    _unit(1, [_tray(0, "PA-CF", 260, 300), _tray(1, "PLA", 190, 230), _tray(2, "ABS", 240, 280), _empty(3)]),
    _unit(128, [_tray(0, "PETG", 220, 270)], info="10002004"),
]


def _report(snow=0x0000, loaded=True, gcode="IDLE", state=1, star=0xFFFF, ams_status=0, units=UNITS, **extra):
    """A new-protocol (P2S / H2S) report: snow = (ams << 8) | slot in the extruder."""
    p = {"command": "push_status", "msg": 0, "gcode_state": gcode, "fun2": "3973", "ams_status": ams_status,
         "device": {"extruder": {"state": state, "info": [{"id": 0, "snow": snow, "spre": 0xFFFF, "star": star,
                                                          "stat": ams_status, "info": 0b1000 | (0b10 if loaded else 0),
                                                          "temp": 25}]}}}
    if units is not None:
        p["ams"] = {"ams": units}
    p.update(extra)
    return p


class _LiveStore:
    """A store whose collector is an offline vendored client fed with reports;
    publish() records the payload and answers with `reply` (None = silence)."""

    def __init__(self, collector, reply="success"):
        self.collector = collector
        self.published = []
        self.upserted = []

        def publish(payload):
            self.published.append(payload)
            if reply is not None:
                cls = "print" if "print" in payload else "system"
                printer_commands.note_local_reply("SN1", json.dumps({cls: {
                    "sequence_id": payload[cls]["sequence_id"], "result": reply, "reason": ""}}).encode())
            return True

        collector.client.publish = publish

    def get_collector(self, pid):
        return self.collector

    def upsert(self, status):
        self.upserted.append(status)

    def feed(self, *reports):
        for r in reports:
            kit.deliver(self.collector, {"print": r})

    def get_all(self):
        from app.services.normalizer import normalize_bambu
        return [normalize_bambu("p1", "x", self.collector.client.get_device(), device_type="P2S")]


@pytest.fixture
def live(cmd_env, monkeypatch, offline_collector):
    def make(*reports, model="P2S", version="01.02.00.00", reply="success"):
        _bambu_registry(monkeypatch, model)
        collector = offline_collector(model)
        if version:
            kit.deliver(collector, {"info": {"command": "get_version", "sequence_id": "0", "module": [
                {"name": "ota", "sw_ver": version, "hw_ver": "OTA", "sn": kit.FAKE_SERIALS[model]}]}})
        store = _LiveStore(collector, reply)
        store.feed(*reports)
        printer_commands.set_store(store)
        return store
    return make


def _sent(store):
    return [p["print"] for p in store.published]


def _change(**kw):
    p = {"command": "ams_change_filament", "sequence_id": "0"}
    p.update(kw)
    return {"print": p}


@pytest.mark.parametrize("ams_id,slot,target", [(0, 0, 0), (1, 2, 6), (3, 3, 15), (128, 0, 128)])
def test_build_ams_load_addresses_the_tray_like_bambu_studio(ams_id, slot, target):
    assert printer_commands.build_ams_load(ams_id, slot, 250, 240) == _change(
        ams_id=ams_id, slot_id=slot, target=target, curr_temp=250, tar_temp=240)


def test_build_ams_unload_marks_slot_255():
    assert printer_commands.build_ams_unload(255, 220) == _change(
        ams_id=255, slot_id=255, target=255, curr_temp=220, tar_temp=220)
    assert printer_commands.build_ams_unload(1, 210)["print"]["ams_id"] == 1


NO_TELEMETRY = "Нет свежей телеметрии принтера — повторите через несколько секунд"


@pytest.mark.parametrize("action", ["ams_load", "ams_unload"])
def test_ams_command_without_live_telemetry_is_refused(cmd_env, monkeypatch, action):
    _bambu_registry(monkeypatch, "P2S")
    published = []

    class _Collector:
        client = type("C", (), {"publish": staticmethod(lambda p: published.append(p) or True)})()

        def live_device(self):
            return None

    class _Store:
        def __init__(self, collector):
            self.collector = collector

        def get_collector(self, pid):
            return self.collector

    printer_commands.set_store(_Store(_Collector()))
    assert printer_commands.send("p1", action, {"ams": 0, "slot": 1}) == REFUSED(NO_TELEMETRY)
    # a collector without live_device(), or no store at all, fails closed as well
    printer_commands.set_store(_Store(object()))
    assert printer_commands.send("p1", action, {"ams": 0, "slot": 1})["detail"] == NO_TELEMETRY
    printer_commands.set_store(None)
    assert printer_commands.send("p1", action, {"ams": 0, "slot": 1})["detail"] == NO_TELEMETRY
    assert published == []


def test_offline_printer_is_refused(live):
    store = live(_report())
    store.collector.client.get_device().info.set_online(False)
    assert printer_commands.send("p1", "ams_unload")["detail"] == NO_TELEMETRY
    assert store.published == []


PRINTING = "Принтер занят печатью — загрузка и выгрузка филамента доступны только в простое"


@pytest.mark.parametrize("gcode", ["RUNNING", "PREPARE", "SLICING", "PAUSE", "INIT"])
@pytest.mark.parametrize("action", ["ams_load", "ams_unload"])
def test_load_unload_only_while_the_live_state_is_idle(live, gcode, action):
    store = live(_report(snow=0x0000, gcode=gcode))
    r = printer_commands.send("p1", action, {"ams": 1, "slot": 2})
    assert r == REFUSED(PRINTING)
    assert store.published == []


STATE_UNKNOWN = "Состояние принтера ещё неизвестно — повторите через несколько секунд"


@pytest.mark.parametrize("gcode", [None, "", "WAT"])
@pytest.mark.parametrize("action", ["ams_load", "ams_unload"])
def test_load_unload_wait_for_a_known_state(live, gcode, action):
    # Right after the connection the lib's gcode_state is "unknown" (it also
    # turns "" and any value it does not know into "unknown"): the printer is
    # not known to print, so do not say it does — but refuse all the same.
    report = _report(snow=0x0000, gcode=gcode)
    if gcode is None:
        del report["gcode_state"]          # never reported: the lib keeps "unknown"
    store = live(report)
    r = printer_commands.send("p1", action, {"ams": 1, "slot": 2})
    assert r == REFUSED(STATE_UNKNOWN)
    assert store.published == []


@pytest.mark.parametrize("gcode", ["IDLE", "FINISH", "FAILED"])
def test_load_allowed_when_idle_finished_or_failed(live, gcode):
    store = live(_report(snow=0x0000, gcode=gcode))
    assert printer_commands.send("p1", "ams_load", {"ams": 1, "slot": 2})["success"]
    assert len(store.published) == 1


AMS_BUSY = "AMS занят (калибровка, чтение RFID или самопроверка) — дождитесь окончания"


@pytest.mark.parametrize("report,model,version,detail", [
    (_report(state=2), "P2S", "01.02.00.00",
     "Принтеры с двумя экструдерами пока не поддерживаются — загрузка и выгрузка с экрана принтера"),
    (_report(), "P2S", None, "Версия прошивки принтера ещё не получена — повторите через несколько секунд"),
    (_report(), "X1C", "01.05.00.00", "Прошивка принтера не поддерживает смену филамента командой"),
    (_report(state=1 | 1 << 19, star=0x0102, ams_status=0x0103), "P2S", "01.02.00.00",
     "Идёт смена филамента — дождитесь окончания"),
    (_report(ams_status=0x0200), "P2S", "01.02.00.00", AMS_BUSY),    # RFID being read
    (_report(ams_status=0x0700), "P2S", "01.02.00.00", AMS_BUSY),    # cold pull
])
@pytest.mark.parametrize("action", ["ams_load", "ams_unload"])
def test_filament_change_gates(live, report, model, version, detail, action):
    store = live(report, model=model, version=version)
    assert printer_commands.send("p1", action, {"ams": 1, "slot": 2}) == REFUSED(detail)
    assert store.published == []


@pytest.mark.parametrize("params,detail", [
    ({"slot": 3}, "Укажите блок AMS и слот (обновите страницу)"),          # an old client
    ({"ams": "1", "slot": 2}, "Укажите блок AMS и слот (обновите страницу)"),
    ({"ams": 255, "slot": 0}, "Загрузка с внешней катушки — только с экрана принтера"),
    ({"ams": 254, "slot": 0}, "Загрузка с внешней катушки — только с экрана принтера"),
    ({"ams": 2, "slot": 0}, "AMS 3 не найден"),
    ({"ams": 129, "slot": 0}, "AMS HT 2 не найден"),
    ({"ams": 1, "slot": 4}, "AMS 2: нет такого слота"),
    ({"ams": 128, "slot": 1}, "AMS HT 1: нет такого слота"),
    ({"ams": 1}, "AMS 2: нет такого слота"),
    ({"ams": 0, "slot": 2}, "Слот 3 пуст"),
    ({"ams": 1, "slot": 3}, "Слот 8 пуст"),
    ({"ams": 0, "slot": 0}, "Этот филамент уже загружен"),
])
def test_load_validation(live, params, detail):
    store = live(_report(snow=0x0000))
    assert printer_commands.send("p1", "ams_load", params) == REFUSED(detail)
    assert store.published == []


def test_load_of_the_slot_in_snow_allowed_when_the_extruder_is_empty(live):
    # snow still points at AMS 1 slot 1, but info bit 1 = 0: nothing in the extruder.
    store = live(_report(snow=0x0000, loaded=False))
    assert printer_commands.send("p1", "ams_load", {"ams": 0, "slot": 0})["success"]
    sent = _sent(store)
    assert sent == [dict(_change(ams_id=0, slot_id=0, target=0, curr_temp=250, tar_temp=250)["print"],
                         sequence_id=sent[0]["sequence_id"])]


@pytest.mark.parametrize("snow,params,want", [
    # target ABS 240-280 -> 260; the loaded PETG (AMS 1 slot 1) 230-270 -> 250
    (0x0000, {"ams": 1, "slot": 2}, dict(ams_id=1, slot_id=2, target=6, curr_temp=250, tar_temp=260)),
    # AMS HT: target = its unit id; PETG 220-270 -> 245
    (0x0000, {"ams": 128, "slot": 0}, dict(ams_id=128, slot_id=0, target=128, curr_temp=250, tar_temp=245)),
    # loaded PA-CF (AMS 2 slot 1) 260-300 -> 280
    (0x0100, {"ams": 0, "slot": 1}, dict(ams_id=0, slot_id=1, target=1, curr_temp=280, tar_temp=210)),
    # loaded from the external spool: its range is unknown -> 220
    (0xFF00, {"ams": 0, "slot": 0}, dict(ams_id=0, slot_id=0, target=0, curr_temp=220, tar_temp=250)),
    # filament in the extruder, but no slot in snow (tray_now 255): unknown origin -> 220
    (0xFFFF, {"ams": 1, "slot": 2}, dict(ams_id=1, slot_id=2, target=6, curr_temp=220, tar_temp=260)),
])
def test_load_payload_end_to_end(live, snow, params, want):
    store = live(_report(snow=snow))
    r = printer_commands.send("p1", "ams_load", params)
    assert r == CONFIRMED
    sent = _sent(store)
    assert sent == [dict(_change(**want)["print"], sequence_id=sent[0]["sequence_id"])]


@pytest.mark.parametrize("snow,want", [
    (0x0101, dict(ams_id=1, curr_temp=210, tar_temp=210)),      # tray_now 5: AMS 2 slot 2 (PLA 190-230)
    (0x8000, dict(ams_id=128, curr_temp=245, tar_temp=245)),    # tray_now 8: AMS HT 1
    (0xFF00, dict(ams_id=255, curr_temp=220, tar_temp=220)),    # external spool
    (0x0003, dict(ams_id=255, curr_temp=220, tar_temp=220)),    # stale index on an empty slot: external
])
def test_unload_payload_end_to_end(live, snow, want):
    store = live(_report(snow=snow))
    assert printer_commands.send("p1", "ams_unload")["success"]
    sent = _sent(store)
    assert sent == [dict(_change(slot_id=255, target=255, **want)["print"], sequence_id=sent[0]["sequence_id"])]


@pytest.mark.parametrize("report,model,detail", [
    (_report(snow=0x0000, loaded=False), "P2S", "Филамент не загружен в экструдер — выгружать нечего"),
    (_report(snow=0xFFFF, loaded=True), "P2S", "Нет данных о загруженном филаменте"),
    ({"gcode_state": "IDLE", "ams": {"tray_now": "255"}}, "P1S", "Филамент не загружен — выгружать нечего"),
])
def test_unload_validation(live, report, model, detail):
    store = live(report, model=model, version="01.09.00.00" if model == "P1S" else "01.02.00.00")
    assert printer_commands.send("p1", "ams_unload") == REFUSED(detail)
    assert store.published == []


@pytest.mark.parametrize("model,report,version", [
    # X1C without an AMS, new protocol: the extruder holds the external spool
    ("X1C", _report(snow=0xFF00, units=None), "01.11.02.00"),
    # P1S without an AMS, legacy protocol: tray_now 254 = external spool
    ("P1S", {"gcode_state": "FINISH", "ams": {"tray_now": "254"}}, "01.09.00.00"),
])
def test_external_spool_unload_without_an_ams(live, model, report, version):
    store = live(report, model=model, version=version)
    assert printer_commands.send("p1", "ams_unload")["success"]
    sent = _sent(store)
    assert sent == [dict(_change(ams_id=255, slot_id=255, target=255, curr_temp=220, tar_temp=220)["print"],
                         sequence_id=sent[0]["sequence_id"])]


def _vir_slot(ttype, lo, hi, sid="255"):
    """The external spool as P2S / H2S / X1C report it (print.vir_slot)."""
    return [dict(_tray(0, ttype, lo, hi), id=sid)]


@pytest.mark.parametrize("model,report,version,temp", [
    # the extruder holds the external spool: ABS 240-280 -> 260 (ha-bambulab's middle)
    ("P2S", _report(snow=0xFF00, vir_slot=_vir_slot("ABS", 240, 280)), "01.02.00.00", 260),
    # stale AMS index on an empty slot while feeding from the external spool
    ("P2S", _report(snow=0x0003, vir_slot=_vir_slot("ABS", 240, 280)), "01.02.00.00", 260),
    # X1C without an AMS; P1S (legacy vt_tray, tray_now 254)
    ("X1C", _report(snow=0xFF00, units=None, vir_slot=_vir_slot("PC", 260, 300)), "01.11.02.00", 280),
    ("P1S", {"gcode_state": "FINISH", "ams": {"tray_now": "254"},
             "vt_tray": dict(_tray(0, "ASA", 240, 270), id="254")}, "01.09.00.00", 255),
    # no profile range on the spool (0 / 0): 220 as before
    ("P2S", _report(snow=0xFF00, vir_slot=_vir_slot("", 0, 0)), "01.02.00.00", 220),
    # clamped to the hotend's range
    ("P2S", _report(snow=0xFF00, vir_slot=_vir_slot("PPS-CF", 380, 420)), "01.02.00.00", 300),
])
def test_external_spool_unload_uses_its_profile(live, model, report, version, temp):
    store = live(report, model=model, version=version)
    assert printer_commands.send("p1", "ams_unload")["success"]
    sent = _sent(store)
    assert sent == [dict(_change(ams_id=255, slot_id=255, target=255, curr_temp=temp, tar_temp=temp)["print"],
                         sequence_id=sent[0]["sequence_id"])]


def test_load_while_the_external_spool_is_in_uses_its_profile_to_cut(live):
    # PC 260-300 in the extruder from the external spool -> cut at 280; PLA slot 190-230 -> 210
    store = live(_report(snow=0xFF00, vir_slot=_vir_slot("PC", 260, 300)))
    assert printer_commands.send("p1", "ams_load", {"ams": 0, "slot": 1})["success"]
    sent = _sent(store)
    assert sent == [dict(_change(ams_id=0, slot_id=1, target=1, curr_temp=280, tar_temp=210)["print"],
                         sequence_id=sent[0]["sequence_id"])]


def test_load_temperatures_are_clamped_per_model(live):
    hot = [_unit(0, [_tray(0, "PPS-CF", 380, 420), _tray(1, "PLA", 100, 120), _empty(2), _empty(3)])]
    store = live(_report(snow=0x0001, units=hot))
    assert printer_commands.send("p1", "ams_load", {"ams": 0, "slot": 0})["success"]
    assert (_sent(store)[0]["curr_temp"], _sent(store)[0]["tar_temp"]) == (170, 300)
    store = live(_report(snow=0x0001, units=hot), model="H2S")
    assert printer_commands.send("p2", "ams_load", {"ams": 0, "slot": 0})["success"]   # p1 is in flight
    assert _sent(store)[0]["tar_temp"] == 350          # H2 series nozzle goes to 350 °C


def test_second_change_is_refused_until_the_first_one_ran(live, monkeypatch):
    clock = kit.FakeClock()
    monkeypatch.setattr(printer_commands, "_monotonic", clock)
    store = live(_report(snow=0x0000))
    assert printer_commands.send("p1", "ams_unload")["success"]
    # a double click: the printer has not reported the change yet
    r = printer_commands.send("p1", "ams_load", {"ams": 1, "slot": 2})
    assert r == REFUSED("Команда смены филамента уже отправлена — дождитесь окончания")
    assert len(store.published) == 1
    # the printer ran the change (busy, then done): the guard is lifted
    store.feed(_report(snow=0x0000, state=1 | 1 << 19, star=0x00FF, ams_status=0x0104),
               _report(snow=0xFFFF, loaded=False))
    assert printer_commands.send("p1", "ams_load", {"ams": 1, "slot": 2})["success"]
    assert len(store.published) == 2
    # no telemetry about the change at all: the guard lapses after 20 s
    assert not printer_commands.send("p1", "ams_load", {"ams": 0, "slot": 1})["success"]
    clock.advance(20)
    assert printer_commands.send("p1", "ams_load", {"ams": 0, "slot": 1})["success"]
    assert len(store.published) == 3


def test_rejected_change_does_not_block_a_retry(live):
    store = live(_report(snow=0x0000), reply="fail")
    r = printer_commands.send("p1", "ams_unload")
    assert r == REJECTED("принтер отклонил: fail")
    printer_commands.send("p1", "ams_unload")
    assert len(store.published) == 2


def _answer_with(store, **fields):
    """The store's printer answers every command with these reply fields."""
    def publish(payload):
        store.published.append(payload)
        printer_commands.note_local_reply("SN1", json.dumps({"print": dict(
            {"sequence_id": payload["print"]["sequence_id"]}, **fields)}).encode())
        return True
    store.collector.client.publish = publish


@pytest.mark.parametrize("reply,detail", [
    ({"errno": -2, "soft_temp": 45}, "Камера слишком горячая — филамент может размягчиться. "
                                     "Дождитесь, пока она остынет ниже 45 °C: откройте дверцу или включите вентиляторы"),
    ({"errno": -2}, "Камера горячее 45 °C (сейчас или по заданной цели) — PLA, PETG и TPU загружать нельзя, "
                    "чтобы не забить экструдер"),
    ({"errno": -4, "soft_temp": 50}, "AMS слишком горячий — филамент может размягчиться. "
                                     "Дождитесь, пока он остынет ниже 50 °C"),
    ({"errno": -4}, "AMS слишком горячий — филамент может размягчиться. Дождитесь, пока он остынет"),
    ({"errno": -1, "soft_temp": 45}, "принтер отклонил: fail"),
    ({"errno": "-2"}, "принтер отклонил: fail"),
    ({}, "принтер отклонил: fail"),
])
def test_refused_change_explains_a_hot_chamber_or_ams(live, reply, detail):
    store = live(_report(snow=0x0000))
    _answer_with(store, command="ams_change_filament", result="fail", **reply)
    assert printer_commands.send("p1", "ams_unload") == REJECTED(detail)
    printer_commands._ams_inflight.clear()
    assert printer_commands.send("p1", "ams_load", {"ams": 1, "slot": 2}) == REJECTED(detail)


def test_errno_of_other_commands_is_not_read_as_a_hot_chamber(live):
    store = live(_report(units=_dry_units(**DRYING)))
    _answer_with(store, command="ams_filament_drying", result="fail", errno=-2, soft_temp=45)
    assert printer_commands.send("p1", "dry_stop", {"ams": 0}) == REJECTED("принтер отклонил: fail")


INFLIGHT = "Команда смены филамента уже отправлена — дождитесь окончания"


def test_change_racing_one_that_awaits_its_reply_is_refused(live, monkeypatch):
    # Two tabs click at once: the second request arrives while the first one
    # waits for the printer's reply (up to REPLY_TIMEOUT, outside any lock).
    # The guard is taken before the publish, so only one command goes out.
    store = live(_report(snow=0x0000))
    release, waiting = threading.Event(), threading.Event()
    published = []

    def send_local(pid, payload):
        published.append(payload["print"]["command"])
        waiting.set()
        release.wait(5)
        return {"success": True, "detail": "подтверждено принтером", "replied": True}

    monkeypatch.setattr(printer_commands, "_send_local", send_local)
    results = {}
    first = threading.Thread(target=lambda: results.update(first=printer_commands.send("p1", "ams_unload")))
    first.start()
    assert waiting.wait(5)
    second = threading.Thread(target=lambda: results.update(
        second=printer_commands.send("p1", "ams_load", {"ams": 1, "slot": 2})))
    second.start()
    second.join(1)
    release.set()
    first.join(5)
    second.join(5)
    assert results == {"first": CONFIRMED, "second": REFUSED(INFLIGHT)}
    assert published == ["ams_change_filament"]
    assert store.published == []


@pytest.mark.parametrize("outcome", [
    {"success": False, "detail": "publish failed", "replied": False},
    RuntimeError("socket closed"),
])
def test_failed_send_releases_the_guard(live, monkeypatch, outcome):
    live(_report(snow=0x0000))
    calls = []

    def send_local(pid, payload):
        calls.append(payload)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(printer_commands, "_send_local", send_local)
    if isinstance(outcome, Exception):
        with pytest.raises(RuntimeError):
            printer_commands.send("p1", "ams_unload")
    else:
        assert printer_commands.send("p1", "ams_unload") == REJECTED("publish failed", replied=False)
    assert printer_commands._ams_inflight == {}
    monkeypatch.setattr(printer_commands, "_send_local",
                        lambda pid, payload: {"success": True, "detail": "ok", "replied": True})
    assert printer_commands.send("p1", "ams_unload")["success"]


# --- AMS drying (ams_filament_drying) -------------------------------------------

from app.vendor.pybambu.commands import AMS_FILAMENT_DRYING_TEMPLATE


def _drying_template(**kw):
    p = json.loads(json.dumps(AMS_FILAMENT_DRYING_TEMPLATE))
    p["print"].update(kw)
    return p


def test_build_dry_stop_is_bambu_studios_stop():
    # Verified live on a P2S AMS 2 Pro: success, dry_time 633 -> 0 within a second.
    assert printer_commands.build_dry_stop(1) == {"print": {
        "sequence_id": "0", "command": "ams_filament_drying", "ams_id": 1, "temp": 0, "cooling_temp": 0,
        "duration": 0, "humidity": 0, "mode": 0, "rotate_tray": False, "filament": "",
        "close_power_conflict": False}}
    assert printer_commands.build_dry_stop() == _drying_template(ams_id=0)   # the probe's payload


def test_build_dry_start():
    assert printer_commands.build_dry_start(0, 55, 12, "PETG") == _drying_template(
        ams_id=0, mode=1, temp=55, duration=12, filament="PETG", cooling_temp=45)
    # Below 45 °C the printer ignores the start (ha-bambulab; 45 verified live).
    assert printer_commands.DRY_COOLING_TEMP >= 45


def _dry_units(info="10002003", sf=(), dry_time=0, setting=None, trays=None):
    unit = _unit(0, trays or [_tray(0, "PETG", 230, 270), _tray(1, "PLA", 190, 230), _empty(2), _empty(3)],
                 info=info, dry_time=dry_time, dry_sf_reason=list(sf))
    if setting:
        unit["dry_setting"] = dict(zip(("dry_temperature", "dry_duration", "dry_filament"), setting))
    return [unit, _unit(1, [_tray(0, "ABS", 240, 280), _empty(1), _empty(2), _empty(3)], info="10002001"),
            _unit(128, [_tray(0, "PA-CF", 260, 300)], info="10002004")]


DRYING = dict(info="10942023", sf=(6,), dry_time=633, setting=(55, 12, "PETG"))


def test_dry_stop_and_start_publish_studios_payloads(live):
    store = live(_report(units=_dry_units(**DRYING), gcode="RUNNING"))   # drying runs while printing too
    assert printer_commands.send("p1", "dry_stop", {"ams": 0}) == CONFIRMED
    store.feed(_report(units=_dry_units(info="10142003"), gcode="RUNNING"))
    r = printer_commands.send("p1", "dry_start", {"ams": 128, "temp": 85, "hours": 12, "filament": "pa"})
    assert r["success"]
    sent = store.published
    assert sent[0] == _drying_template(ams_id=0, sequence_id=sent[0]["print"]["sequence_id"])
    assert sent[1] == _drying_template(ams_id=128, mode=1, temp=85, duration=12, filament="PA", cooling_temp=45,
                                       sequence_id=sent[1]["print"]["sequence_id"])


@pytest.mark.parametrize("units,params,detail", [
    (_dry_units(), {"temp": 50}, "Укажите блок AMS (обновите страницу)"),
    (_dry_units(), {"ams": 2}, "AMS 3 не найден"),
    (_dry_units(), {"ams": 1}, "AMS 2 не умеет сушить (нужен AMS 2 Pro или AMS HT)"),
    (_dry_units(**DRYING), {"ams": 0}, "Сушка уже идёт — сначала остановите её"),
    (_dry_units(info="10002043"), {"ams": 0}, "Сушка останавливается — подождите"),
    (_dry_units(sf=(3,)), {"ams": 0},
     "Сушка сейчас недоступна: В выходе AMS остался филамент: при нагреве он может застрять — сначала выгрузите его"),
    (_dry_units(sf=(1, 2)), {"ams": 0},
     "Сушка сейчас недоступна: AMS занят (калибровка, чтение RFID или загрузка/выгрузка) — подождите"),
])
def test_dry_start_refusals(live, units, params, detail):
    store = live(_report(units=units))
    body = dict({"temp": 45, "hours": 12, "filament": "PLA"}, **params)
    assert printer_commands.send("p1", "dry_start", body) == REFUSED(detail)
    assert store.published == []


@pytest.mark.parametrize("trays,feeding,ams_id,temp,ok,detail", [
    # AMS 2 Pro with PETG: 45-65 °C idle
    ([_tray(0, "PETG", 230, 270)], False, 0, 65, True, None),
    ([_tray(0, "PETG", 230, 270)], False, 0, 66, False, "Температура сушки: целое 45–65 °C"),
    ([_tray(0, "PETG", 230, 270)], False, 0, 44, False, "Температура сушки: целое 45–65 °C"),
    ([_tray(0, "PETG", 230, 270)], False, 0, "55", False, "Температура сушки: целое 45–65 °C"),
    # a PLA spool caps the AMS at its heat-distortion temperature
    ([_tray(0, "PETG", 230, 270), _tray(1, "PLA", 190, 230)], False, 0, 50, False,
     "Температура сушки: целое 45–45 °C, в AMS есть PLA"),
    # printing from this AMS: the print caps (PETG 55 °C)
    ([_tray(0, "PETG", 230, 270)], True, 0, 60, False,
     "Температура сушки: целое 45–55 °C, идёт печать из этого AMS (PETG)"),
    ([_tray(0, "PETG", 230, 270)], True, 0, 55, True, None),
    # AMS HT: up to 85 °C
    (None, False, 128, 85, True, None),
    (None, False, 128, 86, False, "Температура сушки: целое 45–85 °C"),
])
def test_dry_start_temperature_limits(live, trays, feeding, ams_id, temp, ok, detail):
    trays = trays and trays + [_empty(i) for i in range(len(trays), 4)]
    store = live(_report(units=_dry_units(trays=trays), gcode="RUNNING" if feeding else "IDLE", snow=0x0000))
    r = printer_commands.send("p1", "dry_start", {"ams": ams_id, "temp": temp, "hours": 12, "filament": "PETG"})
    assert r["success"] is ok
    if not ok:
        assert r["detail"] == detail
        assert store.published == []


@pytest.mark.parametrize("params,detail", [
    ({"hours": 0}, "Длительность сушки: целое 1–24 ч"),
    ({"hours": 25}, "Длительность сушки: целое 1–24 ч"),
    ({"hours": None}, "Длительность сушки: целое 1–24 ч"),
    ({"filament": "PETG HF"}, "Материал: PLA, PETG, ABS, ASA, PA, PC, TPU, PVA или PPS"),
    ({"filament": None}, "Материал: PLA, PETG, ABS, ASA, PA, PC, TPU, PVA или PPS"),
])
def test_dry_start_hours_and_material(live, params, detail):
    store = live(_report(units=_dry_units()))
    body = {k: v for k, v in dict({"ams": 0, "temp": 45, "hours": 12, "filament": "PLA"}, **params).items()
            if v is not None}
    assert printer_commands.send("p1", "dry_start", body) == REFUSED(detail)
    assert store.published == []


@pytest.mark.parametrize("fun2", ["1", "10"])                    # bit 5 = 0 (bit 4 alone set in "10")
def test_dry_start_needs_remote_drying_support(live, fun2):
    store = live(_report(units=_dry_units(), fun2=fun2))
    assert store.get_all()[0].ams_dry_ok is False
    r = printer_commands.send("p1", "dry_start", {"ams": 0, "temp": 45, "hours": 12, "filament": "PLA"})
    assert r == REFUSED("Прошивка принтера не сообщает о поддержке удалённой сушки")
    # stopping stays possible (idempotent, like the probe)
    assert printer_commands.send("p1", "dry_stop", {"ams": 0})["success"]
    assert len(store.published) == 1


def test_dry_start_with_fun2_bit_5_alone(live):
    store = live(_report(units=_dry_units(), fun2="20"))
    assert store.get_all()[0].ams_dry_ok is True
    assert printer_commands.send("p1", "dry_start", {"ams": 0, "temp": 45, "hours": 12, "filament": "PLA"})["success"]
    assert len(store.published) == 1


def test_dry_commands_without_live_telemetry(live):
    store = live(_report(units=_dry_units(**DRYING)))
    store.collector.client.get_device().info.set_online(False)
    for action in ("dry_stop", "dry_start"):
        assert printer_commands.send("p1", action, {"ams": 0})["detail"] == NO_TELEMETRY
    assert store.published == []


def test_probe_publishes_studios_stop(cmd_env, monkeypatch):
    _bambu_registry(monkeypatch)
    published = []
    printer_commands.set_store(_Store(lambda payload: published.append(payload) or True))
    printer_commands.probe("p1")
    assert published == [_drying_template(ams_id=0, sequence_id=published[0]["print"]["sequence_id"])]


@pytest.mark.parametrize("units,probed", [
    (_dry_units(), True),
    (_dry_units(**DRYING), False),
    # the second unit dries (the probe's stop goes to unit 0, but stay away anyway)
    ([_dry_units()[0], dict(_dry_units(**DRYING)[0], id="1")], False),
])
def test_probe_cycle_skips_printers_with_a_drying_unit(live, monkeypatch, units, probed):
    store = live(_report(units=units, home_flag=0))           # a full report (P1: home_flag, no fun)
    calls = []
    monkeypatch.setattr(printer_commands, "probe", lambda pid: calls.append(pid))
    printer_commands._probe_cycle(store)
    assert calls == (["p1"] if probed else [])


def test_probe_waits_for_a_full_report_after_connecting(live, monkeypatch):
    # 13.10.2026: after each Pi brownout restart the probe's stop hit AMS 0 before the
    # printer's first full report — the live view had no drying and no fun yet — and
    # killed six real drying runs. No full report (neither fun nor home_flag): no probe.
    store = live(_report(units=_dry_units()))
    calls = []
    monkeypatch.setattr(printer_commands, "probe", lambda pid: calls.append(pid))
    printer_commands._probe_cycle(store)
    assert calls == []


def test_probe_loop_waits_three_minutes_after_start():
    import inspect
    src = inspect.getsource(printer_commands._probe_loop)
    assert "time.sleep(180)" in src


# --- the probe never cuts a drying start short --------------------------------------

DRY_PLA = {"ams": 0, "temp": 45, "hours": 1, "filament": "PLA"}


def test_dry_start_arms_the_quiet_window_before_publishing(live):
    store = live(_report(units=_dry_units()))
    seen = []
    real = store.collector.client.publish

    def publish(payload):
        seen.append(printer_commands._dry_quiet("p1"))
        return real(payload)

    store.collector.client.publish = publish
    assert printer_commands.send("p1", "dry_start", DRY_PLA)["success"]
    assert seen == [True]


def test_failed_dry_start_publish_keeps_the_window(live):
    store = live(_report(units=_dry_units()))

    def publish(payload):
        raise OSError("broken pipe")

    store.collector.client.publish = publish
    assert printer_commands.send("p1", "dry_start", DRY_PLA)["status"] == "rejected"
    assert printer_commands._dry_quiet("p1") is True


def test_refused_dry_start_does_not_arm(live):
    live(_report(units=_dry_units(**DRYING)))
    assert printer_commands.send("p1", "dry_start", DRY_PLA)["status"] == "refused"
    assert printer_commands._dry_quiet("p1") is False


def test_probe_is_skipped_within_the_quiet_window(live, monkeypatch):
    store = live(_report(units=_dry_units()))
    assert printer_commands.send("p1", "dry_start", DRY_PLA)["success"]
    store.published.clear()
    noted = []
    monkeypatch.setattr(printer_commands, "_note_capability", lambda *a: noted.append(a))
    assert printer_commands.probe("p1") is None
    assert store.published == [] and noted == []          # a skipped probe teaches nothing
    calls = []
    monkeypatch.setattr(printer_commands, "probe", lambda pid: calls.append(pid))
    printer_commands._probe_cycle(store)
    assert calls == []


def test_probe_cannot_interleave_with_a_dry_start(live, monkeypatch):
    # The start arms the window before its publish takes the publish lock; the
    # probe checks it under that lock right before its own publish. The live
    # view still shows the unit idle while the start waits for its reply.
    store = live(_report(units=_dry_units()), reply=None)
    monkeypatch.setattr(printer_commands, "REPLY_TIMEOUT", 0.5)
    t = threading.Thread(target=printer_commands.send, args=("p1", "dry_start", DRY_PLA))
    t.start()
    deadline = time.monotonic() + 5
    while not store.published and time.monotonic() < deadline:
        time.sleep(0.01)
    assert store.published, "the start was not published"
    assert printer_commands.probe("p1") is None
    t.join(5)
    assert [p["print"]["mode"] for p in store.published] == [1]


def test_probe_returns_after_the_quiet_window(live, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(printer_commands, "_monotonic", lambda: clock[0])
    store = live(_report(units=_dry_units(), home_flag=0))
    assert printer_commands.send("p1", "dry_start", DRY_PLA)["success"]
    calls = []
    monkeypatch.setattr(printer_commands, "probe", lambda pid: calls.append(pid))
    printer_commands._probe_cycle(store)
    clock[0] += printer_commands.PROBE_DRY_QUIET_S - 1
    printer_commands._probe_cycle(store)
    assert calls == []
    clock[0] += 1
    printer_commands._probe_cycle(store)
    assert calls == ["p1"]


# --- print_cmds from print.fun (no probe where the printer reports it) -----------

def test_note_signature_sets_the_capability_once(cmd_env, monkeypatch):
    from app.services import settings_service

    saved = []
    monkeypatch.setattr(settings_service, "set_many", lambda d, **k: saved.append(dict(d)))
    printer_commands.note_signature("p1", None)              # no fun (P1 series): nothing known
    assert printer_commands.get_capability("p1") is None
    for _ in range(3):
        printer_commands.note_signature("p1", False)         # no signature needed
    assert printer_commands.get_capability("p1") is True
    assert printer_commands._capability["p1"]["via"] == "fun"
    assert saved == [{"printer_capability": {"p1": True}}]   # persisted on the change only
    printer_commands.note_signature("p1", True)              # Developer Mode switched off
    assert printer_commands.get_capability("p1") is False


def test_verify_failed_after_a_passive_verdict_distrusts_fun(cmd_env, monkeypatch, caplog):
    import logging

    _bambu_registry(monkeypatch)
    printer_commands._passive_distrust.discard("p1")
    printer_commands.note_signature("p1", False)
    printer_commands.set_store(_Store(_replying("failed", "mqtt message verify failed")))
    with caplog.at_level(logging.WARNING, logger="app.services.printer_commands"):
        assert not printer_commands.send("p1", "pause")["success"]
        printer_commands.send("p1", "pause")
    assert printer_commands.get_capability("p1") is False
    assert "p1" in printer_commands._passive_distrust
    assert len([r for r in caplog.records if "print.fun" in r.getMessage()]) == 1
    printer_commands.note_signature("p1", False)             # fun is not trusted any more
    assert printer_commands.get_capability("p1") is False
    printer_commands._passive_distrust.discard("p1")


@pytest.mark.parametrize("fun,distrusted,probed", [
    ("20011830F9CFF", False, False),     # fun reported, no signature needed: no probe
    ("3EC1AFFF9CFF", False, False),      # fun reported, signature required: no probe either
    (None, False, True),                 # no fun (P1 series): probe
    ("20011830F9CFF", True, True),       # fun proved wrong once: the probe decides
])
def test_probe_cycle_only_where_fun_is_missing_or_distrusted(live, monkeypatch, fun, distrusted, probed):
    report = _report(units=_dry_units(), home_flag=0)
    if fun:
        report["fun"] = fun
    store = live(report)
    if distrusted:
        printer_commands._passive_distrust.add("p1")
    calls = []
    monkeypatch.setattr(printer_commands, "probe", lambda pid: calls.append(pid))
    try:
        printer_commands._probe_cycle(store)
    finally:
        printer_commands._passive_distrust.discard("p1")
    assert calls == (["p1"] if probed else [])


# --- live refresh after an AMS command ----------------------------------------------

# cmd_env replaces it with a no-op (no background threads in the other tests)
_REAL_START_FAST_REFRESH = getattr(printer_commands, "_start_fast_refresh", None)

def test_fast_refresh_once_upserts_the_live_status(live):
    store = live(_report(snow=0x0000, state=1 | 1 << 19, star=0x0102, ams_status=0x0103))
    prn = {"label": "B1", "model": "P2S", "kind": "bambu"}
    assert printer_commands._fast_refresh_once("p1", prn) is True          # a change runs
    assert store.upserted[-1].filament_change == {"action": "load", "step": "cut", "ams": 1, "slot": 2}
    store.feed(_report(snow=0x0102))
    assert printer_commands._fast_refresh_once("p1", prn) is False         # settled
    assert store.upserted[-1].filament_change is None
    store.feed(_report(units=_dry_units(info="10402013")))                 # drying: checking
    assert printer_commands._fast_refresh_once("p1", prn) is True
    store.collector.client.get_device().info.set_online(False)
    assert printer_commands._fast_refresh_once("p1", prn) is None          # no live telemetry: stop
    assert len(store.upserted) == 3


@pytest.mark.parametrize("action,params,ok", [
    ("dry_stop", {"ams": 0}, True),
    ("ams_unload", {}, True),
    ("ams_load", {"ams": 0, "slot": 2}, False),     # refused: empty slot
])
def test_successful_ams_commands_start_the_live_refresh(live, monkeypatch, action, params, ok):
    live(_report(snow=0x0000, units=_dry_units()))
    started = []
    monkeypatch.setattr(printer_commands, "_start_fast_refresh", lambda pid, prn, *a, **k: started.append(pid))
    assert printer_commands.send("p1", action, params)["success"] is ok
    assert started == (["p1"] if ok else [])


def test_fast_refresh_loop_runs_until_settled_or_three_minutes(live, monkeypatch):
    clock = kit.FakeClock()
    monkeypatch.setattr(printer_commands, "_monotonic", clock)
    store = live(_report(snow=0x0000, state=1 | 1 << 19, star=0x00FF, ams_status=0x0102))
    ticks = []
    finish_at = [4]

    def sleep(seconds):
        clock.advance(seconds)
        ticks.append(clock())
        if len(ticks) == finish_at[0]:      # the change finishes after ~8 s
            store.feed(_report(snow=0xFFFF, loaded=False))

    monkeypatch.setattr(printer_commands, "_sleep", sleep)
    prn = {"label": "B1", "model": "P2S", "kind": "bambu"}
    printer_commands._fast_refresh_until["p1"] = (clock() + 180, clock() + 10, None)
    printer_commands._fast_refresh_loop("p1", prn)
    # busy for 3 polls, then settled; it keeps going for the 10 s minimum
    assert len(store.upserted) == 5 and "p1" not in printer_commands._fast_refresh_until
    # a printer that never settles is refreshed for 3 minutes at most
    store.feed(_report(units=_dry_units(info="10402013")))
    ticks.clear()
    finish_at[0] = None
    printer_commands._fast_refresh_until["p1"] = (clock() + 180, clock() + 10, None)
    printer_commands._fast_refresh_loop("p1", prn)
    assert len(ticks) == 90 and "p1" not in printer_commands._fast_refresh_until


def test_start_fast_refresh_extends_a_running_refresh(cmd_env, monkeypatch):
    clock = kit.FakeClock()
    monkeypatch.setattr(printer_commands, "_monotonic", clock)
    threads = []
    monkeypatch.setattr(printer_commands.threading, "Thread",
                        lambda **kw: threads.append(kw) or type("T", (), {"start": lambda self: None})())
    printer_commands.set_store(type("S", (), {"upsert": lambda self, s: None})())
    _REAL_START_FAST_REFRESH("p1", {})
    clock.advance(5)
    _REAL_START_FAST_REFRESH("p1", {})      # the running loop picks up the new window
    assert len(threads) == 1
    assert printer_commands._fast_refresh_until["p1"] == (clock() + 180, clock() + 10, None)
    printer_commands._fast_refresh_until.clear()


# === card controls: lights, state rules, eject guard, Klipper, caps, live state =====

from types import SimpleNamespace

from app.domain.models import PrinterKind, PrinterState, PrinterStatus
from app.services.normalizer import normalize_bambu, normalize_ws_dict
from app.vendor.pybambu.commands import (CHAMBER_LIGHT_2_OFF, CHAMBER_LIGHT_2_ON, CHAMBER_LIGHT_OFF,
                                         CHAMBER_LIGHT_ON)

STATE_MSG = printer_commands.STATE_MSG
EJECT_WAIT = "Стол уже опускается — подождите"


def test_build_light_matches_the_libs_payloads():
    assert printer_commands.build_light(True) == CHAMBER_LIGHT_ON
    assert printer_commands.build_light(False) == CHAMBER_LIGHT_OFF
    assert printer_commands.build_light(True, "chamber_light2") == CHAMBER_LIGHT_2_ON
    assert printer_commands.build_light(False, "chamber_light2") == CHAMBER_LIGHT_2_OFF


@pytest.mark.parametrize("model,two", [("P2S", False), ("X1C", False), ("P1S", False), ("", False), (None, False),
                                       ("H2S", True), ("H2D", True), ("h2c", True), ("X2D", True)])
def test_has_second_light(model, two):
    assert printer_commands.has_second_light(model) is two


class _LampStore:
    """A store whose printer answers every command (no reply with reply=None;
    `replies` sets it per led_node, `reasons` the reply's reason); the publish
    of the led_node named by `fail` fails. Records the payloads."""

    def __init__(self, reply="success", fail=None, device=None, status=None, replies=None, reasons=None):
        self.published, self.status, self.upserted = [], status, []
        outer = self

        def publish(payload):
            outer.published.append(payload)
            node = payload.get("system", {}).get("led_node")
            if fail is not None and node == fail:
                return False
            answer = (replies or {}).get(node, reply)
            if answer is not None:
                cls = "print" if "print" in payload else "system"
                printer_commands.note_local_reply("SN1", json.dumps({cls: {
                    "sequence_id": payload[cls]["sequence_id"], "result": answer,
                    "reason": (reasons or {}).get(node, "")}}).encode())
            return True

        class _Collector:
            client = SimpleNamespace(publish=publish)

            def live_device(self):
                return device

        self._c = _Collector()

    def get_collector(self, pid):
        return self._c

    def get_one(self, pid):
        return self.status

    def upsert(self, status):
        self.upserted.append(status)


def _nodes(store):
    return [p["system"]["led_node"] for p in store.published]


def test_light_on_h2s_switches_both_lamps(cmd_env, monkeypatch):
    _bambu_registry(monkeypatch, "H2S")
    store = _LampStore()
    printer_commands.set_store(store)
    assert printer_commands.send("p1", "light_on") == CONFIRMED
    assert _nodes(store) == ["chamber_light2", "chamber_light"]        # lamp 2 first, lamp 1's reply decides
    assert [p["system"]["led_mode"] for p in store.published] == ["on", "on"]
    seqs = [p["system"]["sequence_id"] for p in store.published]
    assert len(set(seqs)) == 2 and "0" not in seqs


def test_light_lamp1_is_the_verdict_lamp2_a_note(cmd_env, monkeypatch):
    _bambu_registry(monkeypatch, "H2S")
    printer_commands.set_store(_LampStore(fail="chamber_light2"))
    r = printer_commands.send("p1", "light_off")
    assert (r["success"], r["status"], r["replied"]) == (True, "confirmed", True)
    assert r["detail"] == "подтверждено принтером (вторая лампа: publish failed)"
    printer_commands.set_store(_LampStore(fail="chamber_light"))
    assert printer_commands.send("p1", "light_off") == REJECTED("publish failed", replied=False)
    assert printer_commands._local_awaited == {}                       # lamp 2's waiter dropped either way


def test_light_on_one_lamp_models(cmd_env, monkeypatch):
    _bambu_registry(monkeypatch, "P2S")
    store = _LampStore()
    printer_commands.set_store(store)
    assert printer_commands.send("p1", "light_on") == CONFIRMED
    assert _nodes(store) == ["chamber_light"]


def _lights(l1="off", l2="unknown"):
    return SimpleNamespace(chamber_light=l1, chamber_light2=l2, chamber_light_override="",
                           chamber_light2_override="", work_light="unknown")


def _fake_device(model="H2S", lights=None, gcode="IDLE"):
    return SimpleNamespace(info=SimpleNamespace(device_type=model), lights=lights or _lights(),
                           print_job=SimpleNamespace(gcode_state=gcode))


def test_confirmed_light_is_written_into_the_libs_lights(cmd_env, monkeypatch):
    # The printer may not report lights_report again for minutes (delta pushes):
    # the confirmed mode goes into the lib like its own TurnChamberLight* do.
    _bambu_registry(monkeypatch, "")
    device = _fake_device("H2S")
    printer_commands.set_store(_LampStore(device=device))
    assert printer_commands.send("p1", "light_on") == CONFIRMED
    lights = device.lights
    assert (lights.chamber_light, lights.chamber_light2) == ("on", "on")
    assert (lights.chamber_light_override, lights.chamber_light2_override) == ("on", "on")
    assert normalize_bambu("p1", "H1", device, device_type="H2S").light_on is True
    # unanswered: it may not have switched anything — the lib's state is left alone
    device.lights = _lights()
    printer_commands.set_store(_LampStore(device=device, reply=None))
    assert printer_commands.send("p1", "light_on") == SENT
    assert (device.lights.chamber_light, device.lights.chamber_light_override) == ("off", "")
    assert printer_commands._local_awaited == {}                       # no waiter left behind


@pytest.mark.parametrize("lamp2,reason,note", [
    (None, "", "без ответа"),                                         # silent
    ("failed", "", "отклонила — failed"),                            # refused
    ("failed", "led busy", "отклонила — led busy"),                  # refused, with the printer's reason
])
def test_lamp2_mode_is_written_only_after_its_own_reply(cmd_env, monkeypatch, lamp2, reason, note):
    # Lamp 1 confirms, lamp 2 does not: only lamp 1's mode goes into the lib, so the real
    # Lights follows the printer's reports of lamp 2 (still on) and the card reads lit —
    # not «off» with an override that ignores every report until lamp 2 says off.
    from app.vendor.pybambu.models import Lights

    monkeypatch.setattr(printer_commands, "LAMP2_REPLY_GRACE", 0.05)
    _bambu_registry(monkeypatch, "H2S")
    both_on = {"lights_report": [{"node": "chamber_light", "mode": "on"}, {"node": "chamber_light2", "mode": "on"}]}
    lights = Lights(client=None)
    lights.print_update(both_on)
    device = _fake_device("H2S", lights=lights)
    printer_commands.set_store(_LampStore(device=device, replies={"chamber_light2": lamp2},
                                          reasons={"chamber_light2": reason}))
    # lamp 1's reply is the verdict; the answer says what lamp 2 did (the card shows it), so a
    # bulb that is still lit after a confirmed «off» does not go unexplained
    assert printer_commands.send("p1", "light_off") == dict(
        CONFIRMED, detail="подтверждено принтером (вторая лампа: %s)" % note)
    assert (lights.chamber_light, lights.chamber_light_override) == ("off", "off")
    assert (lights.chamber_light2, lights.chamber_light2_override) == ("on", "")
    for _ in range(5):
        lights.print_update({"lights_report": [{"node": "chamber_light", "mode": "off"},
                                               {"node": "chamber_light2", "mode": "on"}]})
    assert lights.is_chamber_light_on is True
    assert normalize_bambu("p1", "H1", device, device_type="H2S").light_on is True
    assert printer_commands._local_awaited == {}


def test_light_written_into_the_real_lib_lasts_until_an_off_report(live):
    store = live({"command": "push_status", "msg": 0, "gcode_state": "RUNNING",
                  "lights_report": [{"node": "chamber_light", "mode": "off"}]}, model="P1S", version="01.09.00.00")
    device = store.collector.client.get_device()
    assert normalize_bambu("p1", "x", device, device_type="P1S").light_on is False
    assert printer_commands.send("p1", "light_on") == CONFIRMED
    assert _nodes(store) == ["chamber_light"]
    for line in kit.load_fixture("P1S")[1:]:                 # delta pushes without lights_report
        kit.deliver(store.collector, line["msg"])
        assert normalize_bambu("p1", "x", device, device_type="P1S").light_on is True
    store.feed({"lights_report": [{"node": "chamber_light", "mode": "off"}]})
    assert normalize_bambu("p1", "x", device, device_type="P1S").light_on is False


def test_confirmed_light_is_in_the_store_before_the_reply(live):
    # /api/printers serves the store: the page's next 3 s poll must already carry the new
    # light — not only the refresh loop's first pass, 2 s after the reply — or it undoes the
    # card's own update of the bulb (and the fleet bar's «горит N из M»).
    store = live({"command": "push_status", "msg": 0, "gcode_state": "RUNNING",
                  "lights_report": [{"node": "chamber_light", "mode": "off"}]}, model="P1S", version="01.09.00.00")
    assert printer_commands.send("p1", "light_on") == CONFIRMED          # cmd_env: no refresh loop thread
    assert [s.light_on for s in store.upserted] == [True]
    assert printer_commands.send("p1", "light_off") == CONFIRMED
    assert [s.light_on for s in store.upserted] == [True, False]


def test_a_failed_store_refresh_does_not_turn_a_confirmed_light_into_an_error(live, monkeypatch):
    live({"lights_report": [{"node": "chamber_light", "mode": "off"}]}, model="P1S", version="01.09.00.00")

    def broken(*a, **k):
        raise RuntimeError("normalize failed")

    monkeypatch.setattr(printer_commands, "_fast_refresh_once", broken)
    assert printer_commands.send("p1", "light_on") == CONFIRMED


def test_confirmed_klipper_light_is_in_the_store_before_the_reply(cmd_env, monkeypatch):
    class _LampClient(_KlipperClient):
        """As KlipperClient.run_action: a confirmed SET_PIN is in the client's data at once."""

        def run_action(self, action, timeout=None):
            r = super().run_action(action, timeout)
            self.snapshot["light_on"] = action == "light_on"
            return r

    client = _klipper_env(monkeypatch, _kstatus(), _LampClient(snapshot={
        "state": "standby", "light_on": False, "light_pin": "caselight", "debug": {"webhooks_state": "ready"}}))
    assert printer_commands.send("k1", "light_on") == CONFIRMED
    assert client.actions == ["light_on"]
    assert [s.light_on for s in printer_commands.get_store().upserted] == [True]


@pytest.mark.parametrize("registry,device,stored,two", [
    ("", "H2S", None, True),          # the live device's own model
    ("P2S", "H2S", None, True),       # ... wins over the registry
    ("", None, "H2S", True),          # no live device: the stored status
    ("H2S", "unknown", None, True),   # the lib's placeholder: down the chain
    ("H2S", None, None, True),        # only the registry knows it
    ("", None, None, False),          # nothing known: lamp 1 only, no M141
    ("H2S", "P2S", None, False),
])
def test_one_model_source_for_lamp2_and_the_chamber_heater(cmd_env, monkeypatch, registry, device, stored, two):
    _bambu_registry(monkeypatch, registry)
    st = PrinterStatus(id="p1", label="x", kind=PrinterKind.BAMBU, online=True, state=PrinterState.IDLE,
                       device_type=stored) if stored else None
    store = _LampStore(device=_fake_device(device) if device else None, status=st)
    printer_commands.set_store(store)
    model = printer_commands.bambu_model("p1", {"model": registry})
    assert ("M141 S0" in printer_commands.build_cooldown(model)["print"]["param"]) is two
    assert printer_commands.send("p1", "light_on")["success"]
    assert ("chamber_light2" in _nodes(store)) is two


def test_cooldown_turns_off_the_h2_chamber_heater():
    lines = printer_commands.build_cooldown("H2S")["print"]["param"].splitlines()
    assert lines[:2] == ["M104 S0", "M140 S0"] and lines[-1] == "M141 S0"
    assert "M141" not in printer_commands.build_cooldown("P2S")["print"]["param"]
    assert "M141" not in printer_commands.build_cooldown()["print"]["param"]


@pytest.mark.parametrize("action,state,want", [
    ("pause", "printing", None), ("pause", "paused", STATE_MSG["pause_paused"]),
    ("pause", "finished", STATE_MSG["pause_finished"]), ("pause", "error", STATE_MSG["pause_error"]),
    ("pause", "idle", STATE_MSG["pause_other"]), ("pause", "unknown", STATE_MSG["unknown"]),
    ("pause", "offline", STATE_MSG["pause_other"]), ("pause", None, STATE_MSG["unknown"]),
    ("resume", "paused", None), ("resume", "printing", STATE_MSG["resume"]), ("resume", "idle", STATE_MSG["resume"]),
    ("resume", "finished", STATE_MSG["resume"]), ("resume", "error", STATE_MSG["resume"]),
    ("resume", "unknown", STATE_MSG["unknown"]), ("resume", None, STATE_MSG["unknown"]),
    ("resume", "offline", STATE_MSG["resume"]),
    ("stop", "printing", None), ("stop", "paused", None), ("stop", "idle", STATE_MSG["stop"]),
    ("stop", "finished", STATE_MSG["stop"]), ("stop", "error", STATE_MSG["stop"]),
    ("stop", "unknown", STATE_MSG["unknown"]), ("stop", None, STATE_MSG["unknown"]),
    ("stop", "offline", STATE_MSG["stop"]),
    ("cooldown", "printing", STATE_MSG["cooldown"]), ("cooldown", "paused", None), ("cooldown", "idle", None),
    ("cooldown", "finished", None), ("cooldown", "error", None), ("cooldown", "unknown", STATE_MSG["unknown"]),
    ("cooldown", "offline", STATE_MSG["unknown"]), ("cooldown", None, STATE_MSG["unknown"]),
    ("eject", "printing", STATE_MSG["eject"]), ("eject", "paused", STATE_MSG["eject"]), ("eject", "idle", None),
    ("eject", "finished", None), ("eject", "error", None), ("eject", "unknown", STATE_MSG["unknown"]),
    ("eject", "offline", STATE_MSG["unknown"]),
    ("light_on", "printing", None), ("speed", "idle", None), ("ams_load", "printing", None),
])
def test_state_refusal_by_normalized_state(action, state, want):
    assert printer_commands.state_refusal(action, state) == want


@pytest.mark.parametrize("action,gcode,want", [
    ("cooldown", "IDLE", None), ("cooldown", "FINISH", None), ("cooldown", "FAILED", None),
    ("cooldown", "PAUSE", None),
    ("cooldown", "RUNNING", STATE_MSG["cooldown"]), ("cooldown", "PREPARE", STATE_MSG["cooldown"]),
    ("cooldown", "SLICING", STATE_MSG["cooldown"]),
    ("cooldown", "", STATE_MSG["unknown"]), ("cooldown", "UNKNOWN", STATE_MSG["unknown"]),
    ("cooldown", "INIT", STATE_MSG["unknown"]),
    ("eject", "IDLE", None), ("eject", "FINISH", None), ("eject", "FAILED", None),
    ("eject", "PAUSE", STATE_MSG["eject"]), ("eject", "RUNNING", STATE_MSG["eject"]),
    ("eject", "PREPARE", STATE_MSG["eject"]), ("eject", "SLICING", STATE_MSG["eject"]),
    ("eject", "", STATE_MSG["unknown"]), ("eject", "UNKNOWN", STATE_MSG["unknown"]),
])
@pytest.mark.parametrize("state", ["idle", "printing"])
def test_state_refusal_raw_gcode_state_decides(action, gcode, want, state):
    # Bambu: the raw gcode_state decides (fail closed), whatever the normalized state says
    assert printer_commands.state_refusal(action, state, gcode) == want


@pytest.mark.parametrize("action", ["cooldown", "eject"])
@pytest.mark.parametrize("gcode,detail", [("RUNNING", None), ("PREPARE", None), ("", STATE_MSG["unknown"])])
def test_cooldown_and_eject_refused_unless_the_printer_stands(live, action, gcode, detail):
    store = live(_report(gcode=gcode))
    assert printer_commands.send("p1", action) == REFUSED(detail or STATE_MSG[action])
    assert store.published == []


@pytest.mark.parametrize("action", ["cooldown", "eject"])
@pytest.mark.parametrize("gcode", ["IDLE", "FINISH", "FAILED"])
def test_cooldown_and_eject_sent_when_the_printer_stands(live, action, gcode):
    store = live(_report(gcode=gcode))
    assert printer_commands.send("p1", action) == CONFIRMED
    assert [p["print"]["command"] for p in store.published] == ["gcode_line"]


def test_cooldown_while_paused_is_sent_eject_is_not(live):
    store = live(_report(gcode="PAUSE"), model="H2S")
    assert printer_commands.send("p1", "eject") == REFUSED(STATE_MSG["eject"])
    assert printer_commands.send("p1", "cooldown") == CONFIRMED
    assert len(store.published) == 1 and store.published[0]["print"]["param"].endswith("M141 S0\n")


@pytest.mark.parametrize("action", ["cooldown", "eject"])
def test_cooldown_and_eject_need_live_telemetry_and_no_filament_change(live, action):
    store = live(_report(state=1 | 1 << 19, star=0x0102, ams_status=0x0103))
    assert printer_commands.send("p1", action) == REFUSED(printer_commands.MSG_CHANGE_RUNNING)
    store.collector.client.get_device().info.set_online(False)
    assert printer_commands.send("p1", action) == REFUSED(NO_TELEMETRY)
    assert store.published == []


def test_second_eject_within_the_guard_is_refused(live, monkeypatch):
    clock = kit.FakeClock()
    monkeypatch.setattr(printer_commands, "_monotonic", clock)
    store = live(_report())
    assert printer_commands.send("p1", "eject") == CONFIRMED
    clock.advance(printer_commands.EJECT_GUARD_SECONDS - 1)
    assert printer_commands.send("p1", "eject") == REFUSED(EJECT_WAIT)
    assert len(store.published) == 1
    clock.advance(1)
    assert printer_commands.send("p1", "eject") == CONFIRMED
    assert len(store.published) == 2
    # cooldown is idempotent: no guard
    assert printer_commands.send("p1", "cooldown") == CONFIRMED
    assert printer_commands.send("p1", "cooldown") == CONFIRMED
    assert len(store.published) == 4


def test_two_concurrent_ejects_publish_once(live):
    store = live(_report())
    barrier = threading.Barrier(2, timeout=5)
    results = []

    def go():
        barrier.wait()
        results.append(printer_commands.send("p1", "eject")["status"])

    threads = [threading.Thread(target=go) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert sorted(results) == ["confirmed", "refused"]
    assert len(store.published) == 1


def test_failed_eject_gives_the_guard_back(live):
    store = live(_report())
    publish = store.collector.client.publish
    store.collector.client.publish = lambda payload: False
    assert printer_commands.send("p1", "eject") == REJECTED("publish failed", replied=False)
    assert printer_commands._eject_marks == {}
    store.collector.client.publish = publish
    assert printer_commands.send("p1", "eject") == CONFIRMED
    assert len(store.published) == 1


@pytest.mark.parametrize("action,gcode,detail", [
    ("pause", "PAUSE", "Уже на паузе"),
    ("pause", "IDLE", "Принтер не печатает"),
    ("pause", "FINISH", "Печать завершена"),
    ("pause", "FAILED", "Принтер в ошибке — см. карточку"),
    ("resume", "RUNNING", "Принтер не на паузе"),
    ("resume", "IDLE", "Принтер не на паузе"),
    ("stop", "IDLE", "Печать не идёт — останавливать нечего"),
])
def test_print_control_refused_by_the_live_state(live, action, gcode, detail):
    store = live(_report(gcode=gcode))
    assert printer_commands.send("p1", action) == REFUSED(detail)
    assert store.published == []


@pytest.mark.parametrize("action,gcode", [("pause", "RUNNING"), ("pause", "PREPARE"), ("resume", "PAUSE"),
                                          ("stop", "RUNNING"), ("stop", "PAUSE")])
def test_print_control_sent_in_the_right_state(live, action, gcode):
    store = live(_report(gcode=gcode))
    assert printer_commands.send("p1", action) == CONFIRMED
    assert [p["print"]["command"] for p in store.published] == [action]


def test_print_control_without_live_state_is_left_to_the_printer(cmd_env, monkeypatch):
    _bambu_registry(monkeypatch)
    published = []
    printer_commands.set_store(_Store(lambda payload: published.append(payload) or True))
    assert printer_commands.send("p1", "resume") == SENT
    assert published[0]["print"]["command"] == "resume"


def test_object_skip_is_gone(cmd_env, monkeypatch):
    _bambu_registry(monkeypatch)
    published = []
    printer_commands.set_store(_Store(lambda payload: published.append(payload) or True))
    assert printer_commands.send("p1", "skip_objects", {"obj_list": [3]}) == REFUSED("Неизвестная команда: skip_objects")
    assert published == [] and not hasattr(printer_commands, "build_skip_objects")


# --- one result contract for every path of send() (shared with the auto-dry branch) ---

def _contract_ok(r):
    if set(r) != {"success", "status", "detail", "replied"} or not isinstance(r["detail"], str):
        return False
    if not isinstance(r["success"], bool) or not isinstance(r["replied"], bool):
        return False
    if r["status"] == "refused":
        return r["success"] is False and r["replied"] is False
    return r["status"] == (("confirmed" if r["replied"] else "sent") if r["success"] else "rejected")


class _WsStore:
    """A store with one WS collector (Creality / Klipper style) and its stored status."""

    def __init__(self, client=None, status=None, collector=None):
        self.client, self.status, self.upserted = client, status, []
        self._c = collector if collector is not None else SimpleNamespace(client=client)

    def get_collector(self, pid):
        return self._c

    def get_one(self, pid):
        return self.status

    def get_all(self):
        return [self.status] if self.status is not None else []

    def upsert(self, status):
        self.upserted.append(status)


class _KlipperClient:
    def __init__(self, reply=None, snapshot=None):
        self.actions, self.reply, self.snapshot = [], reply, snapshot or {}

    def run_action(self, action, timeout=None):
        self.actions.append(action)
        return dict(self.reply or {"success": True, "replied": True, "detail": "подтверждено принтером"})

    def get_data(self):
        return dict(self.snapshot)


def _klipper_env(monkeypatch, stored=None, client=None):
    monkeypatch.setattr("app.services.printer_registry.get_printer",
                        lambda pid: {"id": pid, "kind": "klipper", "label": "Ghost 6", "model": None})
    client = client or _KlipperClient()
    printer_commands.set_store(_WsStore(client, stored))
    return client


def _kstatus(state=PrinterState.IDLE, ready=True, light=True):
    return PrinterStatus(id="k1", label="Ghost 6", kind=PrinterKind.KLIPPER, online=True, state=state,
                         device_type="ghost6", ctl_ready=ready, light_ctl=light)


def test_every_path_of_send_keeps_the_contract(live, monkeypatch):
    results = {}
    store = live(_report(snow=0x0000))
    results["state refusal"] = printer_commands.send("p1", "pause")
    results["param error"] = printer_commands.send("p1", "speed", {"level": 9})
    results["ams plan refusal"] = printer_commands.send("p1", "ams_load", {"ams": 0, "slot": 2})
    results["confirmed"] = printer_commands.send("p1", "eject")
    results["eject guard"] = printer_commands.send("p1", "eject")
    store.collector.client.publish = lambda payload: False
    results["transport failure"] = printer_commands.send("p1", "light_on")
    store = live(_report(snow=0x0000), reply=None)
    results["sent"] = printer_commands.send("p2", "cooldown")
    store = live(_report(snow=0x0000), reply="fail")
    results["printer said no"] = printer_commands.send("p3", "cooldown")
    printer_commands.set_store(_Store(_replying("failed", "mqtt message verify failed")))
    results["verify failed"] = printer_commands.send("p4", "speed", {"level": 2})
    monkeypatch.setattr("app.services.printer_registry.get_printer", lambda pid: None)
    results["kind refusal"] = printer_commands.send("p1", "pause")
    _creality_registry(monkeypatch)
    printer_commands.set_store(_CrealityStore())
    results["cap refusal"] = printer_commands.send("c1", "light_on")
    results["creality"] = printer_commands.send("c1", "pause")
    _klipper_env(monkeypatch, _kstatus(PrinterState.PRINTING))
    results["klipper"] = printer_commands.send("k1", "pause")
    _klipper_env(monkeypatch, _kstatus(PrinterState.PAUSED), _KlipperClient(reply={
        "success": False, "replied": True, "detail": "Klipper отклонил: x"}))
    results["klipper rejected"] = printer_commands.send("k1", "resume")
    assert {k: r for k, r in results.items() if not _contract_ok(r)} == {}
    assert {k: r["status"] for k, r in results.items()} == {
        "state refusal": "refused", "param error": "refused", "ams plan refusal": "refused",
        "confirmed": "confirmed", "eject guard": "refused", "transport failure": "rejected",
        "sent": "sent", "printer said no": "rejected", "verify failed": "rejected", "kind refusal": "refused",
        "cap refusal": "refused", "creality": "sent", "klipper": "confirmed", "klipper rejected": "rejected"}
    assert results["verify failed"]["replied"] is True and results["transport failure"]["replied"] is False


# --- live_status: one public reader for every kind -----------------------------------

def test_live_status_for_every_kind(live, monkeypatch):
    live(_report(gcode="RUNNING"))
    st = printer_commands.live_status("p1")              # prn resolved from the registry
    assert (st.kind, st.state, st.online) == (PrinterKind.BAMBU, PrinterState.PRINTING, True)
    snap = {"state": "paused", "progress": 40, "remaining_time": 600, "debug": {"webhooks_state": "ready"}}
    client = _klipper_env(monkeypatch, _kstatus(), _KlipperClient(snapshot=snap))
    st = printer_commands.live_status("k1")
    assert (st.kind, st.state, st.device_type, st.ctl_ready) == (PrinterKind.KLIPPER, PrinterState.PAUSED,
                                                                 "ghost6", True)
    client.snapshot = {}                                   # empty snapshot = no live telemetry
    assert printer_commands.live_status("k1") is None
    assert printer_commands.live_status("m1", {"kind": "mks"}) is None
    printer_commands.set_store(None)
    assert printer_commands.live_status("k1") is None


# --- capabilities (caps_for), from real normalizer output -------------------------------

ALL_FALSE = dict.fromkeys(printer_commands.CAP_KEYS, False)


def _klipper_row(webhooks="ready", state="standby", pin=None, **extra):
    data = {"state": state, "progress": 0, "remaining_time": 0, "debug": {"webhooks_state": webhooks}}
    data.update(extra)
    if pin is not None:
        data["light_pin"] = pin
    return normalize_ws_dict("klipper-1", "Ghost 6", PrinterKind.KLIPPER, data).to_dict()


@pytest.mark.parametrize("state,extra", [("standby", {}), ("paused", {"progress": 40, "remaining_time": 600}),
                                         ("printing", {"progress": 40, "remaining_time": 600})])
@pytest.mark.parametrize("pin,light", [("caselight", True), ("", False), (None, None)])
def test_caps_klipper_ready_in_any_state(state, extra, pin, light):
    d = _klipper_row(state=state, pin=pin, **extra)
    assert d["state"] == {"standby": "idle"}.get(state, state)
    caps, note = printer_commands.caps_for(d)
    assert caps == dict(ALL_FALSE, pause=True, resume=True, stop=True, light=light)
    assert note is None


def test_caps_klipper_not_ready_or_offline():
    caps, note = printer_commands.caps_for(_klipper_row(webhooks="shutdown", pin="caselight"))
    assert (caps, note) == (ALL_FALSE, "Klipper не готов")
    offline = normalize_ws_dict("klipper-1", "Ghost 6", PrinterKind.KLIPPER, {}).to_dict()
    caps, note = printer_commands.caps_for(offline)
    assert (caps, note) == (dict(ALL_FALSE, pause=None, resume=None, stop=None, light=None), None)
    caps, note = printer_commands.caps_for(dict(_klipper_row(pin="caselight"), online=False))
    assert caps["pause"] is None and caps["light"] is None


@pytest.mark.parametrize("verdict,on,note", [(True, True, None),
                                             (False, False, "прошивка блокирует (нет Developer Mode)"),
                                             (None, None, "доступ к управлению ещё проверяется")])
def test_caps_bambu(cmd_env, verdict, on, note):
    if verdict is not None:
        printer_commands._note_capability("b1", verdict, "local")
    for online in (True, False):
        caps, n = printer_commands.caps_for({"id": "b1", "kind": "bambu", "online": online})
        assert caps == dict({k: on for k in printer_commands.CAP_KEYS}, light=True)
        assert n == note


def test_caps_creality_and_mks(cmd_env, monkeypatch):
    from app.services import settings_service

    row = {"id": "creality-1", "kind": "creality", "online": True}
    assert printer_commands.caps_for(row) == (dict(ALL_FALSE, pause=True, resume=True, stop=True, light=None), None)
    monkeypatch.setattr(settings_service, "get",
                        lambda key, **k: ["creality-1"] if key == "printer_light_enabled" else {})
    assert printer_commands.caps_for(row)[0]["light"] is True
    monkeypatch.setattr(settings_service, "get", lambda key, **k: "creality-1")     # broken: not a list
    assert printer_commands.caps_for(row)[0]["light"] is None
    assert printer_commands.caps_for({"id": "mks-1", "kind": "mks", "online": True}) == (
        ALL_FALSE, "управление не подключено")


def test_creality_light_only_when_enabled(cmd_env, monkeypatch):
    from app.services import settings_service

    _creality_registry(monkeypatch)
    store = _CrealityStore()
    printer_commands.set_store(store)
    assert printer_commands.send("p1", "light_on") == REFUSED("Управление светом на этом принтере ещё не проверено")
    assert store.calls == []
    monkeypatch.setattr(settings_service, "get", lambda key, **k: ["p1"] if key == "printer_light_enabled" else {})
    assert printer_commands.send("p1", "light_on") == SENT
    assert store.calls == [("light_on", None)]


# --- Klipper: pause / resume / cancel and light through run_action ---------------------

def test_klipper_pause_goes_to_run_action(cmd_env, monkeypatch):
    client = _klipper_env(monkeypatch, _kstatus(PrinterState.PRINTING))
    assert printer_commands.send("k1", "pause") == CONFIRMED
    assert client.actions == ["pause"]


def test_klipper_without_a_collector(cmd_env, monkeypatch):
    _klipper_env(monkeypatch, _kstatus())
    printer_commands.set_store(None)
    assert printer_commands.send("k1", "pause") == REJECTED("коллектор принтера не найден", replied=False)


@pytest.mark.parametrize("light,refused", [(False, True), (None, False), (True, False)])
def test_klipper_light_refused_only_without_a_pin(cmd_env, monkeypatch, light, refused):
    client = _klipper_env(monkeypatch, _kstatus(light=light))
    r = printer_commands.send("k1", "light_on")
    if refused:
        assert r == REFUSED("У принтера нет управляемой подсветки") and client.actions == []
    else:
        assert r == CONFIRMED and client.actions == ["light_on"]     # unknown pin: the client answers


def test_klipper_not_ready_is_refused(cmd_env, monkeypatch):
    client = _klipper_env(monkeypatch, _kstatus(PrinterState.PRINTING, ready=False))
    assert printer_commands.send("k1", "pause") == REFUSED("Klipper не готов")
    assert printer_commands.send("k1", "light_on") == REFUSED("Klipper не готов")
    assert client.actions == []


@pytest.mark.parametrize("state,action", [(PrinterState.IDLE, "pause"), (PrinterState.PAUSED, "resume"),
                                          (PrinterState.PAUSED, "stop")])
def test_klipper_idle_or_paused_stays_controllable(cmd_env, monkeypatch, state, action):
    # e.g. resume of a print paused from the dashboard (no debug while paused)
    client = _klipper_env(monkeypatch, _kstatus(state))
    assert printer_commands.send("k1", action) == CONFIRMED
    assert client.actions == [action]


def test_klipper_without_a_stored_status_is_not_refused(cmd_env, monkeypatch):
    client = _klipper_env(monkeypatch, None)
    assert printer_commands.send("k1", "pause") == CONFIRMED and client.actions == ["pause"]


def test_klipper_live_state_refusal(cmd_env, monkeypatch):
    snap = {"state": "standby", "progress": 0, "remaining_time": 0, "debug": {"webhooks_state": "ready"}}
    client = _klipper_env(monkeypatch, _kstatus(), _KlipperClient(snapshot=snap))
    assert printer_commands.send("k1", "pause") == REFUSED("Принтер не печатает")
    assert client.actions == []


def test_klipper_rejects_bambu_only_actions(cmd_env, monkeypatch):
    client = _klipper_env(monkeypatch, _kstatus())
    for action in ("cooldown", "eject", "speed", "ams_load"):
        assert printer_commands.send("k1", action) == REFUSED("Klipper не принимает команду «%s»" % action)
    assert client.actions == []


# --- fast refresh after every command (expected state, any kind) ------------------------

def test_fast_refresh_follows_the_expected_state(live, monkeypatch):
    clock = kit.FakeClock()
    monkeypatch.setattr(printer_commands, "_monotonic", clock)
    store = live(_report(gcode="RUNNING"))
    ticks, pause_at = [], [3]

    def sleep(seconds):
        clock.advance(seconds)
        ticks.append(clock())
        if len(ticks) == pause_at[0]:
            store.feed(_report(gcode="PAUSE"))          # the pause lands after ~6 s

    monkeypatch.setattr(printer_commands, "_sleep", sleep)
    prn = {"label": "B1", "model": "P2S", "kind": "bambu"}
    pause = printer_commands.EXPECT["pause"]
    printer_commands._fast_refresh_until["p1"] = (clock() + 30, clock() + 10, pause)
    printer_commands._fast_refresh_loop("p1", prn)
    # refreshed until paused, then on to the 10 s minimum
    assert [s.state for s in store.upserted] == [PrinterState.PRINTING] * 2 + [PrinterState.PAUSED] * 3
    assert "p1" not in printer_commands._fast_refresh_until
    # never paused: stops at the 30 s ceiling
    store.feed(_report(gcode="RUNNING"))
    ticks.clear()
    pause_at[0] = None
    printer_commands._fast_refresh_until["p1"] = (clock() + 30, clock() + 10, pause)
    printer_commands._fast_refresh_loop("p1", prn)
    assert len(ticks) == 15 and "p1" not in printer_commands._fast_refresh_until


def test_fast_refresh_of_a_ws_printer(cmd_env, monkeypatch):
    snap = {"state": "paused", "progress": 40, "remaining_time": 600, "debug": {"webhooks_state": "ready"}}
    client = _klipper_env(monkeypatch, _kstatus(), _KlipperClient(snapshot=snap))
    store = printer_commands.get_store()
    prn = {"id": "k1", "kind": "klipper", "label": "Ghost 6"}
    assert printer_commands._fast_refresh_once("k1", prn, printer_commands.EXPECT["pause"]) is False
    assert (store.upserted[-1].state, store.upserted[-1].ctl_ready) == (PrinterState.PAUSED, True)
    assert printer_commands._fast_refresh_once("k1", prn, printer_commands.EXPECT["resume"]) is True
    client.snapshot = {}
    assert printer_commands._fast_refresh_once("k1", prn) is None       # nothing upserted
    assert len(store.upserted) == 2


@pytest.mark.parametrize("reply,started", [("success", True), (None, True), ("fail", False)])
def test_fast_refresh_starts_after_a_sent_command_only(live, monkeypatch, reply, started):
    live(_report(gcode="RUNNING"), reply=reply)
    calls = []
    monkeypatch.setattr(printer_commands, "_start_fast_refresh", lambda pid, prn, *a, **k: calls.append((pid, a)))
    printer_commands.send("p1", "pause")
    printer_commands.send("p1", "resume")               # refused: not paused
    want = [("p1", (printer_commands.EXPECT["pause"], printer_commands.FAST_REFRESH_CMD_MAX))]
    assert calls == (want if started else [])


def test_running_refresh_is_widened_not_duplicated(cmd_env, monkeypatch):
    clock = kit.FakeClock()
    monkeypatch.setattr(printer_commands, "_monotonic", clock)
    threads = []
    monkeypatch.setattr(printer_commands.threading, "Thread",
                        lambda **kw: threads.append(kw) or type("T", (), {"start": lambda self: None})())
    printer_commands.set_store(type("S", (), {"upsert": lambda self, s: None})())
    t0 = clock()
    _REAL_START_FAST_REFRESH("p1", {})                        # AMS: 3 minutes, settling rules
    clock.advance(5)
    pause = printer_commands.EXPECT["pause"]
    _REAL_START_FAST_REFRESH("p1", {}, pause, printer_commands.FAST_REFRESH_CMD_MAX)
    assert len(threads) == 1
    assert printer_commands._fast_refresh_until["p1"] == (t0 + 180, clock() + 10, pause)
    clock.advance(1)
    _REAL_START_FAST_REFRESH("p1", {})                        # no expectation: keeps the last one
    assert printer_commands._fast_refresh_until["p1"] == (clock() + 180, clock() + 10, pause)
    printer_commands._fast_refresh_until.clear()


def test_confirmed_light_refresh_stops_at_keep_until(live, monkeypatch):
    clock = kit.FakeClock()
    monkeypatch.setattr(printer_commands, "_monotonic", clock)
    store = live(_report())
    sleeps = []
    monkeypatch.setattr(printer_commands, "_sleep", lambda s: (clock.advance(s), sleeps.append(s)))
    assert printer_commands.send("p1", "light_on") == CONFIRMED       # the lib already says "on"
    printer_commands._fast_refresh_until["p1"] = (clock() + 30, clock() + 10, printer_commands.EXPECT["light_on"])
    printer_commands._fast_refresh_loop("p1", {"label": "B1", "model": "P2S", "kind": "bambu"})
    assert len(sleeps) == 5 and store.upserted[-1].light_on is True
