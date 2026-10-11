"""The real paho client against a tiny local MQTT broker (bambu_testkit.StubBroker).

The riskiest assumptions of the adapter are otherwise checked only against
FakePaho, which encodes them: that setting paho's private _keepalive in
on_connect really makes paho ping every 5 s, that a refused CONNACK keeps
the same listener retrying instead of ending it, that the reconnect back-off
bounds the wait after an outage or a reboot (and keeps doubling through
refusals), and that paho reports a failed reconnect through on_connect_fail,
so a refused login ends once the printer is off. TLS is skipped (plain TCP to
127.0.0.1); everything else is the production code path. ~80 s in total.
"""
import time

import pytest

import bambu_testkit as kit


@pytest.fixture
def broker_factory():
    brokers = []

    def make(connack_codes=(0,), port=0):
        broker = kit.StubBroker(connack_codes, port=port)
        brokers.append(broker)
        return broker

    yield make
    for broker in brokers:
        broker.close()


def _collector(make_collector, broker):
    collector = make_collector("RP", "127.0.0.1", "12345678", kit.FAKE_SERIALS["P2S"], "P2S")
    collector.client.setup_tls = lambda: None    # plain TCP to the stub broker
    collector.client._port = broker.port
    return collector


def _online(collector) -> bool:
    return collector.client.get_device().info.online


def test_keepalive_is_restored_to_5s(no_network, broker_factory, make_collector):
    no_network.allow("127.0.0.1")
    broker = broker_factory((0,))
    collector = _collector(make_collector, broker)
    collector.connect()
    assert kit.wait_until(lambda: _online(collector), timeout=10)
    assert broker.connects[0][1] == 30           # the lib's first CONNECT announces 30 s
    assert collector.client.client._keepalive == 5
    assert kit.wait_until(lambda: broker.pings, timeout=9)
    # With keepalive 30 the first PINGREQ would come ~30 s after the last packet.
    assert broker.pings[0] - broker.connacks[0][0] < 7.0
    assert collector.fetch() is collector.client.get_device()


@pytest.mark.parametrize("refused", [5, 3])  # 5 = not authorised, 3 = server unavailable
def test_refused_connack_is_retried_by_the_same_listener(refused, no_network, broker_factory, make_collector):
    no_network.allow("127.0.0.1")
    broker = broker_factory((refused, 0))
    collector = _collector(make_collector, broker)
    collector.connect()
    assert kit.wait_until(lambda: collector.client._mqtt is not None, timeout=5)
    listener = collector.client._mqtt
    assert kit.wait_until(lambda: _online(collector), timeout=10)
    assert [code for _t, code in broker.connacks] == [refused, 0]
    assert collector.client._mqtt is listener and listener.is_alive()
    assert collector.fetch() is collector.client.get_device()


def test_back_online_within_seconds_after_an_outage(no_network, broker_factory, make_collector):
    # During an outage paho doubles the wait between attempts up to max_delay.
    # With upstream's 1 -> 30 s the attempts come ~1, 3, 7, 15 and 31 s after
    # the drop, so a printer back at 16.5 s would wait until ~31 s. With
    # 1 -> 5 s they come ~1, 3, 7, 12 and 17 s after it.
    no_network.allow("127.0.0.1")
    broker = broker_factory((0,))
    collector = _collector(make_collector, broker)
    collector.connect()
    assert kit.wait_until(lambda: _online(collector), timeout=10)

    broker.go_down()
    down_at = time.monotonic()
    assert kit.wait_until(lambda: not _online(collector), timeout=5)
    time.sleep(max(0.0, down_at + 16.5 - time.monotonic()))
    broker.go_up()
    up_at = time.monotonic()
    assert len(broker.dropped) >= 3                  # paho kept retrying meanwhile
    assert kit.wait_until(lambda: _online(collector), timeout=20)
    assert time.monotonic() - up_at < 7.0
    assert collector.fetch() is collector.client.get_device()


def test_back_online_within_seconds_after_a_reboot(no_network, broker_factory, make_collector):
    # A rebooting printer accepts TCP before its broker takes logins and
    # answers CONNACK 3 (server unavailable) meanwhile. Treated as a refused
    # login, the first refusal restarted the wait at 1 s with a 30 s cap: the
    # attempts came ~1, 3, 7, 15 and 31 s after it, so a broker taking logins
    # ~16 s in waited ~15 s more. As an outage they stay ~1, 3, 7, 12, 17, 22 s
    # after the drop.
    no_network.allow("127.0.0.1")
    broker = broker_factory((0,))
    collector = _collector(make_collector, broker)
    collector.connect()
    assert kit.wait_until(lambda: _online(collector), timeout=10)

    broker.go_down()
    down_at = time.monotonic()
    assert kit.wait_until(lambda: not _online(collector), timeout=5)
    time.sleep(max(0.0, down_at + 2.0 - time.monotonic()))
    broker.connack_codes = [3]                       # booting: TCP accepted, logins turned away
    broker.go_up()
    time.sleep(max(0.0, down_at + 19.5 - time.monotonic()))
    assert {code for _t, code in broker.connacks[1:]} == {3}
    # The wait keeps doubling to the 5 s cap through the refusals (attempts
    # ~3, 7, 12, 17 s after the drop) instead of restarting at 1 s on each.
    refused_at = [t for t, _code in broker.connacks[1:]]
    assert 3 <= len(refused_at) <= 5, [round(t - down_at, 2) for t in refused_at]
    assert min(b - a for a, b in zip(refused_at, refused_at[1:])) > 3.0
    # Powered on and answering, just not taking logins yet.
    with pytest.raises(ConnectionError, match=r"turned the session away \(CONNACK 3"):
        collector.fetch()
    broker.connack_codes = [0]                       # booted
    up_at = time.monotonic()
    assert kit.wait_until(lambda: _online(collector), timeout=20)
    assert time.monotonic() - up_at < 7.0
    assert collector.fetch() is collector.client.get_device()


def _fetch_error(collector) -> str:
    try:
        collector.fetch()
    except ConnectionError as e:
        return str(e)
    return "online"


def test_refused_login_is_not_reported_once_the_printer_is_off(no_network, broker_factory, make_collector):
    # The printer refuses the login, then is switched off: from then on every
    # reconnect fails at TCP, which paho reports through on_connect_fail only
    # (neither on_connect nor on_disconnect runs). One such failure is not
    # enough (a flaky link), the second in a row ends the refused login: here
    # ~3 and ~7 s after the switch-off on Windows, ~1 and ~3 s on Linux.
    no_network.allow("127.0.0.1")
    broker = broker_factory((5,))
    collector = _collector(make_collector, broker)
    collector.connect()
    assert kit.wait_until(lambda: "login refused" in _fetch_error(collector), timeout=10)

    broker.close()                                   # switched off: connections refused
    assert kit.wait_until(lambda: "powered off or unreachable" in _fetch_error(collector), timeout=15)
    assert [code for _t, code in broker.connacks] == [5]
    assert collector.client._mqtt.is_alive()         # paho keeps retrying


def test_back_online_after_refused_then_off_then_on(no_network, broker_factory, make_collector):
    # Refused login, printer switched off, switched back on ~24.5 s later with
    # the login fixed. Once the refused login has ended the outage back-off
    # (1 -> 5 s) applies again: the attempts come ~8, 12, 18, 25 s after the
    # switch-off on Windows (a closed port takes ~2 s to refuse there). Left at
    # 1 -> 30 s they would come ~11, 21, 39 s after it.
    no_network.allow("127.0.0.1")
    broker = broker_factory((5,))
    collector = _collector(make_collector, broker)
    collector.connect()
    assert kit.wait_until(lambda: "login refused" in _fetch_error(collector), timeout=10)
    broker.close()
    off_at = time.monotonic()
    time.sleep(max(0.0, off_at + 24.5 - time.monotonic()))
    broker_factory((0,), port=broker.port)           # switched on, access code fixed
    up_at = time.monotonic()
    assert kit.wait_until(lambda: _online(collector), timeout=25)
    assert time.monotonic() - up_at < 9.0
    assert collector.fetch() is collector.client.get_device()
