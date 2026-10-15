"""BambuCollector over the vendored pybambu: lifecycle, threads, sockets, logging.

paho's Client is replaced by bambu_testkit.FakePaho (fake_paho fixture), so
the library's real connect() / MqttThread / watchdog code runs without a
socket. tests/test_bambu_real_paho.py covers the same assumptions with the
real paho client against a local stub broker.
"""
import inspect
import logging
import os
import re
import ssl
import tempfile
import threading
import time
from types import SimpleNamespace

import paho.mqtt.client as _paho_client
import pytest

import bambu_testkit as kit

# Captured at import, before any fixture swaps paho's Client for FakePaho.
_REAL_PAHO_CLIENT = _paho_client.Client

HOST, CODE, SERIAL = "192.0.2.10", "12345678", "00M00A000000000"
PUSH = {"print": {"command": "push_status", "msg": 0, "gcode_state": "IDLE",
                  "nozzle_temper": 25, "wifi_signal": "-40dBm"}}


def _connected(make_collector, fake_paho, **kwargs):
    """connect() and wait until the lib's listener has called paho connect()."""
    collector = make_collector(**kwargs)
    collector.connect()
    assert kit.wait_until(lambda: fake_paho.instances and fake_paho.instances[-1].connect_calls, timeout=5)
    return collector, fake_paho.instances[-1]


def _bridge_done(collector) -> bool:
    return collector._bridge is not None and not collector._bridge.is_alive()


# --- construction --------------------------------------------------------------

def test_ctor_is_side_effect_free(no_network, make_collector):
    before = set(threading.enumerate())
    collector = make_collector()
    assert set(threading.enumerate()) - before == set()
    device = collector.client.get_device()
    assert device is not None
    assert device.info.online is False
    assert not os.path.exists(collector.client.cache_path)


def test_ctor_tolerates_null_registry_fields(no_network, make_collector):
    # printers.serial / access_code / model are nullable; the vendored ctor would
    # raise on serial=None, and build_collectors() failing is fatal for the app.
    before = set(threading.enumerate())
    collector = make_collector("L", "192.0.2.1", None, None, None)
    assert collector.client.get_device() is not None
    assert collector.client._serial == ""
    assert collector.client._access_code == ""
    assert collector.client.get_device().info.device_type == "UNKNOWN"
    assert set(threading.enumerate()) - before == set()


def test_build_collectors_survives_null_serial(monkeypatch, bambu_threads_clean):
    import main
    from app.collectors.bambu_collector import BambuCollector
    from app.services import printer_registry

    rows = [{"id": "bambu-1", "kind": "bambu", "label": "B1", "host": "192.0.2.1",
             "port": None, "model": None, "access_code": None, "serial": None}]
    monkeypatch.setattr(printer_registry, "load_for_startup", lambda cfg: rows)
    items = main.build_collectors()
    try:
        assert len(items) == 1
        _pid, _kind, _label, collector, device_type = items[0]
        assert isinstance(collector, BambuCollector)
        assert device_type == "X1C"
    finally:
        main._close_bambu_collectors(items)


def test_client_config(make_collector):
    client = make_collector().client
    settings = client.settings
    assert settings["local_mqtt"] is True
    assert settings["enable_camera"] is False
    assert settings["force_ip"] is True
    assert settings["user_language"] == "ru"
    assert settings["timelapse_cache_count"] == 0
    assert settings["auth_token"] == ""
    assert settings["disable_ssl_verify"] is True
    assert client.user_language == "ru"
    assert client.ftp_enabled is False
    assert client.camera_enabled is False
    assert client.probe_remote_media_sources() is False
    assert os.path.dirname(client.cache_path) == tempfile.gettempdir()


def test_cache_path_is_unique_unpredictable_and_never_created(fake_paho, make_collector):
    a = make_collector()
    b = make_collector(serial="22E00A000000000")
    assert a.client.cache_path != b.client.cache_path
    for collector in (a, b):
        assert re.search(r"[0-9a-f]{32}", os.path.basename(collector.client.cache_path))
        collector.connect()
    assert kit.wait_until(lambda: len(fake_paho.instances) == 2
                          and all(p.connect_calls for p in fake_paho.instances), timeout=5)
    assert not os.path.exists(a.client.cache_path)
    assert not os.path.exists(b.client.cache_path)


def test_connect_never_prunes_cached_files(fake_paho, make_collector, tmp_path):
    # Upstream prunes the media cache on every connect: timelapses down to
    # timelapse_cache_count (0 = all), 3MFs down to one, stale *.part files.
    # The app has no cache; if the path ever exists it must not delete anything.
    files = [tmp_path / "timelapse" / "a.mp4", tmp_path / "timelapse" / "a.jpg",
             tmp_path / "timelapse" / "b.mp4.part",
             tmp_path / "prints" / "old.3mf", tmp_path / "prints" / "new.3mf"]
    for f in files:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"x")
    old = time.time() - 3 * 24 * 3600
    for f in (files[2], files[3]):
        os.utime(f, (old, old))
    collector = make_collector()
    collector.client._cache_path = str(tmp_path)
    collector.connect()
    assert kit.wait_until(lambda: fake_paho.instances and fake_paho.instances[-1].connect_calls, timeout=5)
    assert [f.name for f in files if not f.exists()] == []


def test_mock_serial_prefix_has_no_special_meaning(fake_paho, make_collector):
    # Upstream treats "MOCK-<x>" serials as demo printers and reads
    # mock_data/<serial>.json; the app has neither the data nor the feature.
    collector = make_collector(serial="MOCK-X1C")
    assert collector.client._mock is False
    collector.connect()
    assert kit.wait_until(lambda: fake_paho.instances and fake_paho.instances[-1].connect_calls, timeout=5)
    assert isinstance(collector.client.client, kit.FakePaho)


# --- connect ---------------------------------------------------------------------

def test_connect_returns_immediately_and_spawns_one_listener(fake_paho, make_collector):
    fake_paho.tls_delay = 1.0          # a slow BambuClient.connect() coroutine
    collector = make_collector()
    t0 = time.monotonic()
    collector.connect()
    assert time.monotonic() - t0 < 0.5
    t0 = time.monotonic()
    with pytest.raises(ConnectionError):
        collector.fetch()              # poll workers never wait for the bridge
    assert time.monotonic() - t0 < 0.5

    assert kit.wait_until(lambda: fake_paho.instances and fake_paho.instances[-1].connect_calls, timeout=5)
    paho = fake_paho.instances[-1]
    assert paho.connect_calls == [(HOST, 8883, 30)]
    assert paho.reconnect_delay == (1, 30)
    assert paho.auth == ("bblp", CODE)
    assert paho.tls_insecure is True
    assert paho.tls_ctx.verify_mode == ssl.CERT_NONE
    collector.connect()
    time.sleep(0.2)
    assert len(fake_paho.instances) == 1


def test_on_connect_restores_keepalive_and_requests_info(fake_paho, make_collector):
    _collector, paho = _connected(make_collector, fake_paho)
    paho.fire_connect(0)
    assert paho._keepalive == 5
    assert paho.subscribed == [f"device/{SERIAL}/report"]
    assert paho.published_commands() == ["get_version", "pushall"]
    assert {topic for topic, _payload in paho.published} == {f"device/{SERIAL}/request"}


def test_paho_keepalive_internals_unchanged():
    # on_connect sets paho's private _keepalive to 5 s (1.0.1 parity). That only
    # works while paho reads the attribute at these places (paho-mqtt 2.1.0).
    src = {name: inspect.getsource(getattr(_REAL_PAHO_CLIENT, name))
           for name in ("_check_keepalive", "loop_misc", "reconnect", "_ssl_wrap_socket")}
    assert "self._keepalive" in src["_check_keepalive"]
    assert "self._keepalive" in src["loop_misc"]
    assert "_send_connect(self._keepalive)" in src["reconnect"]
    assert "settimeout(self._keepalive)" in src["_ssl_wrap_socket"]


def test_listener_start_is_logged_with_thread_id(fake_paho, make_collector, caplog):
    caplog.set_level(logging.INFO, logger="app.collectors.bambu_collector")
    collector, _paho = _connected(make_collector, fake_paho, label="P7")
    assert kit.wait_until(lambda: "MQTT listener started" in caplog.text, timeout=5)
    native_id = collector.client._mqtt.native_id
    assert f"[P7] MQTT listener started (thread *-Mqtt-{native_id})" in caplog.text


# --- publish / tee ---------------------------------------------------------------

def test_publish_before_connect_returns_false(make_collector, monkeypatch):
    from app.services import printer_commands

    collector = make_collector()
    assert collector.client.publish({"print": {"command": "x"}}) is False

    class _Store:
        def get_collector(self, pid):
            return collector if pid == "bambu-1" else None

    monkeypatch.setattr(printer_commands, "_store", _Store())
    reply = printer_commands._send_local("bambu-1", {"print": {"command": "gcode_line", "param": "M400\n"}})
    assert reply == {"success": False, "detail": "publish failed", "replied": False}


class _RecordingPaho:
    def __init__(self, rc=0):
        self.rc, self.published = rc, []

    def publish(self, topic, payload=None, *args, **kwargs):
        self.published.append((topic, payload))
        return SimpleNamespace(rc=self.rc)


def _client_swapped_on_every_read(values):
    """A _ForgeBambuClient whose `client` returns the next of `values` on each
    read: close() and the rebuild bridge replace it on another thread at any
    time (disconnect() sets None, connect() a new paho client)."""
    from app.collectors.bambu_collector import _ForgeBambuClient, _client_config

    script = list(values)

    class _Swapped(_ForgeBambuClient):
        @property
        def client(self):
            return script.pop(0) if script else None

        @client.setter
        def client(self, value):
            pass

    return _Swapped(_client_config(HOST, CODE, SERIAL, "X1C"))


def test_publish_reads_the_paho_client_once():
    new = _RecordingPaho()
    client = _client_swapped_on_every_read([new, None, new])
    assert client.publish({"print": {"command": "pause"}}) is True
    assert new.published == [(f"device/{SERIAL}/request", '{"print": {"command": "pause"}}')]


def test_publish_through_a_torn_down_paho_client_returns_false():
    old = _RecordingPaho(rc=4)   # paho after disconnect(): MQTT_ERR_NO_CONN
    client = _client_swapped_on_every_read([old, None])
    assert client.publish({"print": {"command": "pause"}}) is False
    assert len(old.published) == 1


def test_tee_receives_every_raw_message(fake_paho, make_collector, monkeypatch):
    from app.services import printer_commands

    teed = []
    monkeypatch.setattr(printer_commands, "note_local_reply", lambda serial, payload: teed.append((serial, payload)))
    _collector, paho = _connected(make_collector, fake_paho)
    paho.fire_connect(0)
    sent = [paho.fire_message(msg) for msg in (
        PUSH,
        {"print": {"command": "push_status", "msg": 1, "mc_percent": 5}},
        b"{not json",
        {"event": {"event": "client.connected"}},
        {"print": {"command": "gcode_line", "sequence_id": "50001", "result": "success"}},
    )]
    assert teed == [(SERIAL, payload) for payload in sent]


def test_device_feeds_raw_report_then_the_lib(offline_collector, monkeypatch):
    # Every report reaches RawReport (fields the lib drops) and then the lib itself,
    # both from paho's on_message and from a direct device.print_update(data=...).
    from app.collectors.bambu_raw import RawReport

    collector = offline_collector("P2S")
    device = collector.client.get_device()
    kit.deliver(collector, {"print": {"command": "push_status", "msg": 0, "gcode_state": "IDLE", "fun": "20000000"}})
    assert device.forge_raw.snapshot()["fun"] == 0x20000000
    assert device.print_job.gcode_state == "IDLE"
    device.print_update(data={"fun2": "3973", "gcode_state": "RUNNING"})
    assert device.forge_raw.snapshot()["fun2"] == 0x3973
    assert device.print_job.gcode_state == "RUNNING"

    def boom(self, data):
        raise RuntimeError("raw report broke")

    monkeypatch.setattr(RawReport, "update", boom)
    kit.deliver(collector, {"print": {"command": "push_status", "msg": 0, "gcode_state": "FINISH"}})
    assert device.print_job.gcode_state == "FINISH"     # the lib still parsed the report


def test_live_device_only_while_telemetry_is_live(no_network, offline_collector, monkeypatch):
    # Commands checked against live telemetry: the Device while info.online,
    # else None. Unlike fetch() it never connects or schedules a rebuild.
    collector = offline_collector("P2S")
    monkeypatch.setattr(collector, "connect", lambda: pytest.fail("live_device() must not connect"))
    assert collector.live_device() is None                 # nothing received yet
    kit.deliver(collector, PUSH)
    assert collector.live_device() is collector.client.get_device()
    collector.client.get_device().info.set_online(False)   # link drop / watchdog
    assert collector.live_device() is None
    monkeypatch.setattr(collector.client, "get_device", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    assert collector.live_device() is None


# --- liveness, refused logins, rebuilds ------------------------------------------

def test_link_drop_marks_offline_without_rebuild(fake_paho, make_collector):
    collector, paho = _connected(make_collector, fake_paho)
    paho.fire_connect(0)
    paho.fire_message(PUSH)
    assert collector.fetch() is collector.client.get_device()

    paho.fire_disconnect(16)           # keepalive timeout / TCP error
    assert collector.client.get_device().info.online is False
    with pytest.raises(ConnectionError, match="powered off or unreachable"):
        collector.fetch()
    assert collector._listener_dead() is False
    assert collector.client._mqtt.is_alive()
    assert len(fake_paho.instances) == 1


def test_refused_login_keeps_listener_retrying(fake_paho, make_collector, fake_clock):
    # paho reports every refused CONNACK (bad credentials, not authorised,
    # server unavailable) as rc=5. Upstream then stops the listener for good;
    # here paho's own loop keeps retrying (backoff 1 -> 30 s), as 1.0.1 did.
    events = []
    collector = make_collector()
    collector._on_event = events.append
    collector.connect()
    assert kit.wait_until(lambda: fake_paho.instances and fake_paho.instances[-1].connect_calls, timeout=5)
    paho = fake_paho.instances[-1]
    paho.fire_connect(0)
    paho.fire_message(PUSH)
    collector.fetch()

    for _attempt in range(2):          # two refused reconnects, as paho delivers them
        paho.fire_connect(5)
        paho.fire_disconnect(5)
    assert not paho.stopped.is_set()   # loop_forever was not ended
    assert collector.client._mqtt.is_alive()
    assert collector._listener_dead() is False
    assert events.count("event_printer_access_denied") == 1
    with pytest.raises(ConnectionError, match="login refused"):
        collector.fetch()
    fake_clock.advance(3600)
    with pytest.raises(ConnectionError, match="login refused"):
        collector.fetch()
    assert len(fake_paho.instances) == 1   # no client re-creation

    paho.fire_connect(0)               # code / LAN mode fixed on the printer
    paho.fire_message(PUSH)
    assert collector.fetch() is collector.client.get_device()


def test_refused_connack_does_not_run_the_libs_on_connect(fake_paho, make_collector):
    # paho calls on_connect for a refused CONNACK too, then on_disconnect(5).
    # Upstream would mark the client connected and subscribe / request a push
    # on a connection that is being closed, on every refused retry.
    collector, paho = _connected(make_collector, fake_paho)
    paho.fire_connect(5)
    assert collector.client.connected is False
    assert paho.subscribed == [] and paho.published == []
    assert collector.send_command("pause")["detail"].startswith("MQTT not connected")
    paho.fire_disconnect(5)
    assert collector.client.connected is False and collector.client._login_refused is True

    paho.fire_connect(0)
    assert collector.client.connected is True
    assert paho.published_commands() == ["get_version", "pushall"]


@pytest.mark.parametrize("code", [4, 5])  # bad user name or password, not authorised
def test_reconnect_backoff_short_after_login_long_while_refused(code, fake_paho, make_collector):
    # paho doubles the wait between reconnects up to max_delay and restarts it
    # on every reconnect_delay_set() call. Upstream's 1 -> 30 s left a printer
    # that came back after an outage offline for up to 30 s more (1.0.1: 1 s).
    collector, paho = _connected(make_collector, fake_paho)
    assert paho.reconnect_delay == (1, 30)           # set by the lib's connect()
    paho.fire_connect(0)
    assert paho.reconnect_delay == (1, 5)
    paho.fire_disconnect(16)                        # link lost: retries 1, 2, 4, 5, 5 ... s
    assert paho.reconnect_delay == (1, 5)

    calls = len(paho.reconnect_delay_calls)
    for _attempt in range(3):                       # refused logins: back to 1 -> 30 s,
        paho.fire_connect(code)                     # set once, so the wait keeps doubling
        paho.fire_disconnect(5)
    assert paho.reconnect_delay_calls[calls:] == [(1, 30)]
    assert collector.client._login_refused is True

    paho.fire_connect(0)
    assert paho.reconnect_delay == (1, 5)


def test_server_unavailable_connack_is_retried_like_an_outage(fake_paho, make_collector):
    # CONNACK 3 (server unavailable): a booting printer accepts TCP before its
    # broker takes logins. Nothing is wrong with the access code, so it must not
    # restart paho's wait at 1 s with the refused-login cap of 30 s: a printer
    # rebooted that way came back up to ~15 s late.
    events = []
    collector = make_collector()
    collector._on_event = events.append
    collector.connect()
    assert kit.wait_until(lambda: _bridge_done(collector) and fake_paho.instances[-1].connect_calls, timeout=5)
    paho = fake_paho.instances[-1]
    assert paho.reconnect_delay_calls == [(1, 30)]  # set by the lib's connect()

    for _attempt in range(3):                       # the app starts while the printer boots
        paho.fire_connect(3)
        paho.fire_disconnect(5)
    assert paho.reconnect_delay_calls[1:] == [(1, 5)]   # set once: the wait keeps doubling
    assert collector.client._login_refused is False
    with pytest.raises(ConnectionError, match=r"turned the session away \(CONNACK 3: server unavailable\)"):
        collector.fetch()
    paho.fire_connect_fail()                        # then no answer at all
    with pytest.raises(ConnectionError, match="powered off or unreachable"):
        collector.fetch()

    paho.fire_connect(0)
    paho.fire_message(PUSH)
    assert collector.fetch() is collector.client.get_device()
    paho.fire_disconnect(16)                        # rebooted: link lost, then refusals
    for _attempt in range(3):
        paho.fire_connect(3)
        paho.fire_disconnect(5)
    assert paho.reconnect_delay_calls[1:] == [(1, 5)]   # 1, 2, 4, 5, 5 ... s from the drop
    assert collector.client._login_refused is False
    with pytest.raises(ConnectionError, match=r"turned the session away \(CONNACK 3"):
        collector.fetch()
    assert events.count("event_printer_access_denied") == 0
    assert not paho.stopped.is_set() and collector.client._mqtt.is_alive()


@pytest.mark.parametrize("stop_answering", ["connect_fail", "closed_before_connack", "mixed"])
def test_refused_login_ends_when_the_printer_stops_answering(stop_answering, fake_paho, make_collector):
    # A printer that refused the login and is then switched off: paho reports
    # a failed reconnect (TCP refused or timed out) through on_connect_fail
    # only, and a connection closed before its CONNACK as on_disconnect(rc != 5).
    # One of them can be a flaky link; the second in a row ends the
    # refused-login state and its 1 -> 30 s back-off.
    collector, paho = _connected(make_collector, fake_paho)
    assert kit.wait_until(lambda: _bridge_done(collector), timeout=5)
    paho.fire_connect(0)
    paho.fire_message(PUSH)
    fail = {"connect_fail": [paho.fire_connect_fail] * 2,
            "closed_before_connack": [lambda: paho.fire_disconnect(7)] * 2,
            "mixed": [paho.fire_connect_fail, lambda: paho.fire_disconnect(7)]}[stop_answering]
    for _streak in range(2):
        paho.fire_connect(5)
        paho.fire_disconnect(5)
        assert paho.reconnect_delay == (1, 30)
        calls = len(paho.reconnect_delay_calls)
        fail[0]()
        assert collector.client._login_refused is True
        assert len(paho.reconnect_delay_calls) == calls   # the 30 s cap stays, the wait keeps doubling
        with pytest.raises(ConnectionError, match="login refused"):
            collector.fetch()
        fail[1]()
        assert collector.client._login_refused is False
        assert paho.reconnect_delay_calls[calls:] == [(1, 5)]
        with pytest.raises(ConnectionError, match="powered off or unreachable"):
            collector.fetch()

    calls = len(paho.reconnect_delay_calls)
    paho.fire_connect_fail()                        # still off: the wait keeps doubling
    paho.fire_disconnect(7)
    assert len(paho.reconnect_delay_calls) == calls
    assert collector.client._mqtt.is_alive()


def test_refused_login_ends_after_a_reboot_that_turns_sessions_away(fake_paho, make_collector):
    # CONNACK 1-3 are no login refusal either: a printer rebooted mid-streak
    # (CONNACK 3 while booting) ends it like one that stopped answering, and
    # a refusal after that starts a new streak with its own ERROR + event.
    events = []
    collector = make_collector()
    collector._on_event = events.append
    collector.connect()
    assert kit.wait_until(lambda: _bridge_done(collector) and fake_paho.instances[-1].connect_calls, timeout=5)
    paho = fake_paho.instances[-1]
    paho.fire_connect(5)
    paho.fire_disconnect(5)
    paho.fire_connect(3)
    paho.fire_disconnect(5)
    with pytest.raises(ConnectionError, match="login refused"):
        collector.fetch()
    paho.fire_connect(3)
    paho.fire_disconnect(5)
    assert paho.reconnect_delay == (1, 5)
    with pytest.raises(ConnectionError, match=r"turned the session away \(CONNACK 3"):
        collector.fetch()
    paho.fire_connect(5)                            # booted, access code still wrong
    paho.fire_disconnect(5)
    assert paho.reconnect_delay == (1, 30)
    assert events.count("event_printer_access_denied") == 2


def test_isolated_failed_reconnects_do_not_end_a_refused_login(fake_paho, make_collector):
    # Wrong access code on a printer with a flaky link: every other attempt
    # fails at TCP (paho: on_connect_fail) or is closed before its CONNACK, the
    # rest get CONNACK 5. Ending the refusal streak on each of those restarted
    # paho's wait at 1 s before every attempt (1.0.1's 1 s retries) and logged
    # an ERROR + access_denied event per refusal instead of per streak.
    events = []
    collector = make_collector()
    collector._on_event = events.append
    collector.connect()
    assert kit.wait_until(lambda: _bridge_done(collector) and fake_paho.instances[-1].connect_calls, timeout=5)
    paho = fake_paho.instances[-1]
    calls = len(paho.reconnect_delay_calls)
    for fail in (paho.fire_connect_fail, lambda: paho.fire_disconnect(7)) * 3:
        paho.fire_connect(5)
        paho.fire_disconnect(5)
        fail()
        assert collector.client._login_refused is True
        with pytest.raises(ConnectionError, match="login refused"):
            collector.fetch()
    assert paho.reconnect_delay_calls[calls:] == [(1, 30)]   # set once: the wait keeps doubling
    assert events.count("event_printer_access_denied") == 1


@pytest.mark.parametrize("code", [1, 2, 3])  # protocol version, client id, server unavailable
def test_persistent_session_refusal_is_not_reported_as_powered_off(code, fake_paho, make_collector):
    # The printer answers MQTT (TCP + TLS + CONNACK) but keeps turning the
    # session away: it is demonstrably powered on and reachable.
    collector, paho = _connected(make_collector, fake_paho)
    for _attempt in range(10):
        paho.fire_connect(code)
        paho.fire_disconnect(5)
    with pytest.raises(ConnectionError, match=rf"turned the session away \(CONNACK {code}\b") as ei:
        collector.fetch()
    assert "powered off" not in str(ei.value)


def test_rebuilt_listener_does_not_inherit_a_refused_login(fake_paho, make_collector, fake_clock):
    # A new paho client's first connects fail inside the lib's own retry loop,
    # which calls no callback: a refusal seen by the previous client must not
    # stick while the printer is off.
    collector, paho = _connected(make_collector, fake_paho)
    paho.fire_connect(5)
    paho.fire_disconnect(5)
    paho.stopped.set()                              # loop_forever() returns
    assert kit.wait_until(lambda: not collector.client._mqtt.is_alive(), timeout=5)
    with pytest.raises(ConnectionError, match="listener stopped"):
        collector.fetch()
    fake_clock.advance(31)
    with pytest.raises(ConnectionError):
        collector.fetch()                           # spawns the rebuild bridge
    assert kit.wait_until(lambda: len(fake_paho.instances) == 2 and _bridge_done(collector)
                          and fake_paho.instances[-1].connect_calls, timeout=5)
    with pytest.raises(ConnectionError, match="powered off or unreachable"):
        collector.fetch()


def test_bridge_failure_is_retried_with_capped_backoff(fake_paho, make_collector, fake_clock):
    fake_paho.tls_error = ssl.SSLError("bad TLS context")
    collector = make_collector()
    collector.connect()
    assert kit.wait_until(lambda: _bridge_done(collector), timeout=5)
    assert isinstance(collector._bridge_error, ssl.SSLError)

    delays = []
    for _attempt in range(5):
        instances = len(fake_paho.instances)
        with pytest.raises(ConnectionError, match="reconnect scheduled"):
            collector.fetch()
        delay = collector._rebuild_due - fake_clock()
        delays.append(delay)
        fake_clock.advance(delay - 1)
        with pytest.raises(ConnectionError):
            collector.fetch()
        assert len(fake_paho.instances) == instances     # not due yet
        fake_clock.advance(2)
        with pytest.raises(ConnectionError):
            collector.fetch()                            # spawns the rebuild bridge
        assert kit.wait_until(lambda: len(fake_paho.instances) == instances + 1
                              and _bridge_done(collector), timeout=5)
    assert delays == [30, 60, 120, 120, 120]

    fake_paho.tls_error = None
    with pytest.raises(ConnectionError):
        collector.fetch()
    fake_clock.advance(121)
    with pytest.raises(ConnectionError):
        collector.fetch()
    assert kit.wait_until(lambda: fake_paho.instances[-1].connect_calls and _bridge_done(collector), timeout=5)
    paho = fake_paho.instances[-1]
    paho.fire_connect(0)
    paho.fire_message(PUSH)
    assert collector.fetch() is collector.client.get_device()
    assert collector._rebuild_attempt == 0 and collector._rebuild_due is None
    assert [p.stopped.is_set() for p in fake_paho.instances[:-1]] == [True] * (len(fake_paho.instances) - 1)


def test_listener_that_ends_on_its_own_is_rebuilt_with_backoff(fake_paho, make_collector, fake_clock):
    # The lib's MqttThread exits once loop_forever() returns, with no connect
    # error recorded. Nothing brings telemetry back then without a new connect().
    collector, paho = _connected(make_collector, fake_paho)
    for delay in (30, 60):
        paho.fire_connect(0)
        paho.stopped.set()                         # loop_forever() returns
        assert kit.wait_until(lambda: not collector.client._mqtt.is_alive(), timeout=5)
        assert collector._bridge_error is None
        instances = len(fake_paho.instances)
        with pytest.raises(ConnectionError, match="listener stopped"):
            collector.fetch()
        assert collector._rebuild_due - fake_clock() == delay
        fake_clock.advance(delay - 1)
        with pytest.raises(ConnectionError):
            collector.fetch()
        assert len(fake_paho.instances) == instances     # not due yet
        fake_clock.advance(2)
        with pytest.raises(ConnectionError):
            collector.fetch()                            # spawns the rebuild bridge
        assert kit.wait_until(lambda: len(fake_paho.instances) == instances + 1 and _bridge_done(collector)
                              and fake_paho.instances[-1].connect_calls, timeout=5)
        assert collector.client._mqtt.is_alive()
        paho = fake_paho.instances[-1]

    paho.fire_connect(0)
    paho.fire_message(PUSH)
    assert collector.fetch() is collector.client.get_device()
    assert collector._rebuild_attempt == 0 and collector._rebuild_due is None


def test_fetch_never_blocks_while_rebuilding(fake_paho, make_collector, fake_clock):
    fake_paho.tls_error = ssl.SSLError("bad TLS context")
    collector = make_collector(label="P9")
    collector.connect()
    assert kit.wait_until(lambda: _bridge_done(collector), timeout=5)
    with pytest.raises(ConnectionError):
        collector.fetch()              # schedules the rebuild in 30 s
    fake_paho.tls_error = None
    fake_paho.tls_delay = 1.0
    fake_clock.advance(31)
    for _ in range(2):
        t0 = time.monotonic()
        with pytest.raises(ConnectionError):
            collector.fetch()          # 1st spawns the rebuild, 2nd finds it running
        assert time.monotonic() - t0 < 0.5
        assert collector._bridge.is_alive()
        assert collector._bridge.name == "bambu-connect-P9"
    assert kit.wait_until(lambda: _bridge_done(collector), timeout=5)


def test_close_joins_threads_and_stops_reconnects(fake_paho, make_collector):
    collector, paho = _connected(make_collector, fake_paho)
    paho.fire_connect(0)
    paho.fire_message(PUSH)
    listener, watchdog = collector.client._mqtt, collector.client._watchdog
    assert "-Mqtt-" in listener.name and listener.is_alive()
    assert "-Watchdog-" in watchdog.name and watchdog.is_alive()

    collector.close()
    assert not listener.is_alive() and not watchdog.is_alive()
    assert collector.client.client is None
    with pytest.raises(ConnectionError, match="collector closed"):
        collector.fetch()
    collector.connect()
    time.sleep(0.1)
    assert len(fake_paho.instances) == 1


def test_no_camera_or_media_threads_on_p1s_job_lifecycle(fake_paho, no_network, make_collector):
    # P1S has the CAMERA_IMAGE feature: upstream would start a JPEG stream on
    # TCP 6000 and FTP/media downloads around a job. Neither may run here.
    collector, paho = _connected(make_collector, fake_paho, serial=kit.FAKE_SERIALS["P1S"], device_type="P1S")
    paho.fire_connect(0)
    for line in kit.load_fixture("P1S"):
        paho.fire_message(line["msg"])
    for state, extra in (("IDLE", {}), ("PREPARE", {"gcode_file_prepare_percent": "0"}),
                         ("PREPARE", {"gcode_file_prepare_percent": "99"}), ("RUNNING", {}),
                         ("FINISH", {}), ("FAILED", {"print_error": 50348044})):
        paho.fire_message({"print": {"command": "push_status", "msg": 1, "gcode_state": state, **extra}})
    time.sleep(0.2)
    names = [t.name for t in threading.enumerate()]
    assert [n for n in names if "-Chamber-" in n or "-Media-" in n] == []
    assert collector.client.get_device().print_job.gcode_state == "FAILED"


# --- logging ---------------------------------------------------------------------

def test_quiet_vendor_logging_after_import_main():
    import main  # noqa: F401  (calls quiet_vendor_logging at import)
    from app.collectors.bambu_collector import _RateLimitFilter, quiet_vendor_logging
    from app.vendor.pybambu.const import LOGGER

    assert LOGGER.name == "app.vendor.pybambu"
    for name in ("app.vendor.pybambu", "paho.mqtt.client"):
        lg = logging.getLogger(name)
        assert lg.level == logging.ERROR
        assert sum(isinstance(f, _RateLimitFilter) for f in lg.filters) == 1
    quiet_vendor_logging()
    assert sum(isinstance(f, _RateLimitFilter) for f in logging.getLogger("app.vendor.pybambu").filters) == 1


def _record(thread, lineno=42, msg="A listener loop thread exception occurred: %s", args=("boom",)):
    rec = logging.LogRecord("app.vendor.pybambu", logging.ERROR, "/x/bambu_client.py", lineno, msg, args, None)
    rec.threadName = thread
    return rec


def test_rate_limit_is_per_thread_and_call_site(fake_clock):
    from app.collectors.bambu_collector import _RateLimitFilter

    flt = _RateLimitFilter(window=600)
    a, b = _record("X1C-Mqtt-11"), _record("P2S-Mqtt-22")
    assert flt.filter(a) and flt.filter(b)          # two printers, same call site: both shown
    assert a.getMessage() == "[X1C-Mqtt-11] A listener loop thread exception occurred: boom"
    assert b.getMessage() == "[P2S-Mqtt-22] A listener loop thread exception occurred: boom"
    assert not flt.filter(_record("X1C-Mqtt-11"))   # same thread + site inside the window
    assert flt.filter(_record("X1C-Mqtt-11", lineno=43))
    fake_clock.advance(601)
    again = _record("X1C-Mqtt-11")
    assert flt.filter(again)
    assert again.getMessage().endswith("occurred: boom [+1 similar suppressed]")


def test_rate_limit_forgets_stale_keys_resets_and_escapes_percent(fake_clock):
    from app.collectors.bambu_collector import _RateLimitFilter

    flt = _RateLimitFilter(window=600)
    for i in range(50):
        flt.filter(_record(f"X1C-Mqtt-{i}"))
    fake_clock.advance(1201)                         # keys idle for two windows are dropped:
    odd = _record("bambu-connect-P1 100%")           # thread names change on every rebuild
    assert flt.filter(odd)                           # (a user label inside the thread name)
    assert odd.getMessage() == "[bambu-connect-P1 100%] A listener loop thread exception occurred: boom"
    assert len(flt._last) == 1
    assert not flt.filter(_record("bambu-connect-P1 100%"))
    flt.reset()
    assert flt.filter(_record("bambu-connect-P1 100%"))


def test_main_closes_only_bambu_collectors():
    import main
    from app.domain.models import PrinterKind

    closed = []

    class _Collector:
        def __init__(self, name, fail=False):
            self.name, self.fail = name, fail

        def close(self):
            closed.append(self.name)
            if self.fail:
                raise RuntimeError("boom")

    items = [("b1", PrinterKind.BAMBU, "B1", _Collector("b1", fail=True), "X1C"),
             ("c1", PrinterKind.CREALITY, "C1", _Collector("c1"), "k1max"),
             ("b2", PrinterKind.BAMBU, "B2", _Collector("b2"), "P2S")]
    main._close_bambu_collectors(items)
    assert closed == ["b1", "b2"]


# --- TLS: opt-in certificate verification (FORGE_BAMBU_TLS_VERIFY) ----------------

@pytest.mark.parametrize("spec,model,expected", [
    ("", "X1C", False),
    (None, "X1C", False),
    ("*", "X1C", True),
    ("*", None, True),
    ("X1C,P2S", "x1c", True),
    (" x1c , p2s ", "P2S", True),
    ("X1C", "P1S", False),
    ("X1C", None, False),
    (",", "X1C", False),
])
def test_tls_verify_enabled(spec, model, expected):
    from app.collectors.bambu_collector import tls_verify_enabled

    assert tls_verify_enabled(spec, model) is expected


def _pem_blocks() -> int:
    from pathlib import Path
    from app.vendor.pybambu import bambu_client as vendor_client

    certs = Path(vendor_client.__file__).with_name("certs")
    return sum(p.read_text(encoding="ascii").count("BEGIN CERTIFICATE") for p in certs.glob("*.cert"))


def test_tls_verify_uses_bambu_cas_only(fake_paho, make_collector):
    _collector, paho = _connected(make_collector, fake_paho, tls_verify=True)
    ctx = paho.tls_ctx
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is False       # printers are addressed by IP
    assert ctx.maximum_version == ssl.TLSVersion.TLSv1_2
    # Bambu's bundled CAs and nothing else: upstream's context also trusts every
    # public CA, i.e. any publicly trusted certificate for any name.
    assert len(ctx.get_ca_certs()) == _pem_blocks() == 8
    assert paho.tls_insecure is None         # tls_insecure_set() is not called


def test_tls_verify_off_matches_pybambu_1_0_1(fake_paho, make_collector):
    _collector, paho = _connected(make_collector, fake_paho)
    assert paho.tls_ctx.verify_mode == ssl.CERT_NONE
    assert paho.tls_insecure is True


def test_build_collectors_passes_tls_flag_per_model(monkeypatch, bambu_threads_clean):
    import config
    import main
    from app.services import printer_registry

    rows = [{"id": "bambu-1", "kind": "bambu", "label": "B1", "host": "192.0.2.1", "port": None,
             "model": "X1C", "access_code": "12345678", "serial": "00M00A000000000"},
            {"id": "bambu-2", "kind": "bambu", "label": "B2", "host": "192.0.2.2", "port": None,
             "model": "P2S", "access_code": "12345678", "serial": "22E00A000000000"}]
    monkeypatch.setattr(printer_registry, "load_for_startup", lambda cfg: rows)
    monkeypatch.setattr(config, "BAMBU_TLS_VERIFY", "x1c", raising=False)
    items = main.build_collectors()
    try:
        verify = {label: not collector.client._disable_ssl_verify for _p, _k, label, collector, _d in items}
        assert verify == {"B1": True, "B2": False}
    finally:
        main._close_bambu_collectors(items)
