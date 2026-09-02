"""Команды принтерам: точные payload'ы, локальный канал с ответами, зонд капабилити."""
import json

import pytest

from app.services import printer_commands


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
    yield
    printer_commands._capability.clear()
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


def test_build_dry_stop_probe_payload():
    # build_dry_stop остаётся как безвредная нагрузка зонда капабилити
    stop = printer_commands.build_dry_stop()
    assert stop["print"]["mode"] == 0
    assert stop["print"]["duration"] == 0
    assert stop["print"]["temp"] == 0
    assert stop["print"]["cooling_temp"] == 40


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


def test_build_ams_load_unload():
    assert printer_commands.build_ams_load(2)["print"]["target"] == 2
    assert printer_commands.build_ams_unload()["print"]["target"] == 255


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


def test_skip_objects_validates_list(cmd_env, monkeypatch):
    _bambu_registry(monkeypatch)
    assert not printer_commands.send("p1", "skip_objects", {"obj_list": []})["success"]
    assert not printer_commands.send("p1", "skip_objects", {"obj_list": ["x"]})["success"]


def test_ams_load_validates_slot(cmd_env, monkeypatch):
    _bambu_registry(monkeypatch)
    r = printer_commands.send("p1", "ams_load", {"slot": 99})
    assert not r["success"] and "0-15" in r["detail"]


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
    assert r["success"] and "подтверждено" in r["detail"]
    assert printer_commands.get_capability("p1") is True


def test_send_without_reply_still_sent(cmd_env, monkeypatch):
    _bambu_registry(monkeypatch)
    published = []
    printer_commands.set_store(_Store(lambda payload: published.append(payload) or True))
    r = printer_commands.send("p1", "light_on")
    assert r["success"] and "без подтверждения" in r["detail"]
    assert published[0]["system"]["command"] == "ledctrl"
    # свет — system-класс, капабилити print-класса не трогает
    assert printer_commands.get_capability("p1") is None


def test_verify_failed_marks_blocked(cmd_env, monkeypatch):
    _bambu_registry(monkeypatch)
    printer_commands.set_store(_Store(_replying("failed", "mqtt message verify failed")))
    r = printer_commands.send("p1", "pause")
    assert not r["success"] and "блокирует" in r["detail"]
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
    # Канал Creality ("set" на WS :9999) умеет только управление печатью —
    # свет/скорость/AMS туда не пробросить, отклоняем до отправки.
    _creality_registry(monkeypatch)
    store = _CrealityStore()
    printer_commands.set_store(store)
    for action in ("light_on", "speed", "ams_load", "cooldown"):
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
    # klipper/mks канал команд ещё не реализован — отказ, а не тихий успех.
    for kind in ("klipper", "mks"):
        monkeypatch.setattr("app.services.printer_registry.get_printer",
                            lambda pid, k=kind: {"id": pid, "kind": k})
        r = printer_commands.send("p1", "pause")
        assert not r["success"]


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
