import logging
import socket
import threading
import time
from types import SimpleNamespace

import pytest

import bambu_testkit as kit


@pytest.fixture(autouse=True)
def _reset_vendor_log_rate_limits():
    # `import main` installs rate-limit filters on the vendored lib's and paho's
    # loggers (process-global). Start every test with a clean window so a test
    # that expects a lib ERROR does not depend on test order.
    for name in ("app.vendor.pybambu", "paho.mqtt.client"):
        for flt in logging.getLogger(name).filters:
            reset = getattr(flt, "reset", None)
            if callable(reset):
                reset()
    yield


@pytest.fixture
def fake_paho(monkeypatch):
    """Route the vendored lib's paho.mqtt.client.Client to bambu_testkit.FakePaho."""
    from app.vendor.pybambu import bambu_client as vendor_client

    kit.FakePaho.reset()
    monkeypatch.setattr(vendor_client.mqtt, "Client", kit.FakePaho)
    yield kit.FakePaho
    kit.FakePaho.reset()


@pytest.fixture
def fake_clock(monkeypatch):
    """Drive bambu_collector's backoff / rate-limit clock by hand."""
    import app.collectors.bambu_collector as bc

    clock = kit.FakeClock()
    monkeypatch.setattr(bc, "_monotonic", clock)
    return clock


_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


@pytest.fixture
def no_network(monkeypatch):
    """Record (and refuse) every outbound connection; fail the test if any happened.

    Raising inside the lib's background threads would just be logged and
    retried, so attempts are collected and asserted at teardown instead.
    Loopback socket.connect stays allowed: asyncio's self-pipe on Windows uses
    it. Plain TCP to 127.0.0.1 via create_connection (what paho uses) is
    opt-in: ``no_network.allow("127.0.0.1")``.
    """
    attempts = []
    allowed = set()
    real_create = socket.create_connection
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def _host(address):
        return address[0] if isinstance(address, tuple) else address

    def create_connection(address, *args, **kwargs):
        if _host(address) in allowed:
            return real_create(address, *args, **kwargs)
        attempts.append(("create_connection", address))
        raise OSError("network access is disabled in this test")

    def connect(self, address):
        host = _host(address)
        if host in _LOOPBACK or host in allowed:
            return real_connect(self, address)
        attempts.append(("connect", address))
        raise OSError("network access is disabled in this test")

    def connect_ex(self, address):
        host = _host(address)
        if host in _LOOPBACK or host in allowed:
            return real_connect_ex(self, address)
        attempts.append(("connect_ex", address))
        return 111  # ECONNREFUSED

    monkeypatch.setattr(socket, "create_connection", create_connection)
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    guard = SimpleNamespace(attempts=attempts, allow=allowed.add)
    yield guard
    assert attempts == [], f"unexpected network connections: {attempts}"


@pytest.fixture
def bambu_threads_clean():
    """Fail if a lib thread (Mqtt/Watchdog/Chamber/Media) or a connect bridge
    started during the test is still alive shortly after it."""
    before = set(threading.enumerate())
    yield
    deadline = time.monotonic() + 6.0
    leftover = []
    while time.monotonic() < deadline:
        leftover = [t.name for t in kit.lib_threads() if t not in before]
        if not leftover:
            break
        time.sleep(0.05)
    assert leftover == [], f"threads left running: {leftover}"


@pytest.fixture
def make_collector(bambu_threads_clean):
    """Factory for BambuCollector; every collector built is close()d afterwards,
    and then no lib thread may be left (bambu_threads_clean tears down later).
    List fake_paho / no_network before this fixture so they outlive it."""
    from app.collectors.bambu_collector import BambuCollector

    made = []

    def factory(label="L", host="192.0.2.10", access_code="12345678",
                serial="00M00A000000000", device_type="X1C", **kwargs):
        collector = BambuCollector(label, host, access_code, serial, device_type, **kwargs)
        made.append(collector)
        return collector

    yield factory
    for collector in made:
        collector.close()


@pytest.fixture
def offline_collector(make_collector):
    """A collector whose client accepts reports without any MQTT session."""

    def factory(model, host="192.0.2.10"):
        collector = make_collector("fx", host, "00000000", kit.FAKE_SERIALS[model], model)
        collector.client.client = kit.StubPaho()
        return collector

    return factory
