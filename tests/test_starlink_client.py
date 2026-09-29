"""Reflection client against a fake dish — the path a real dish takes."""

import time

import pytest

grpc = pytest.importorskip("grpc")
pytest.importorskip("grpc_reflection")

from fake_dish import FakeDish  # noqa: E402
from app.starlink.client import StarlinkClient, StarlinkError  # noqa: E402

STATUS = {
    "deviceInfo": {"hardwareVersion": "rev4_prod2", "softwareVersion": "2026.09.16"},
    "popPingLatencyMs": 24.0,
    "ulBandwidthRestrictedReason": "NO_LIMIT",
}


def test_get_status_with_grpcurl_names():
    with FakeDish(STATUS) as dish:
        r = StarlinkClient(dish.address, timeout=2).handle({"getStatus": {}})
    assert r["apiVersion"] == "43"
    assert r["dishGetStatus"]["deviceInfo"]["hardwareVersion"] == "rev4_prod2"
    assert r["dishGetStatus"]["ulBandwidthRestrictedReason"] == "NO_LIMIT"


def test_location_refused_is_not_permitted():
    with FakeDish(STATUS) as dish:
        with pytest.raises(StarlinkError) as e:
            StarlinkClient(dish.address, timeout=2).handle({"getLocation": {"source": "GPS"}})
    assert e.value.kind == "not_permitted"


def test_location_when_enabled():
    with FakeDish(STATUS, location={"lla": {"lat": 32.89, "lon": -117.12, "alt": 95.4}, "source": "GPS"}) as dish:
        r = StarlinkClient(dish.address, timeout=2).handle({"getLocation": {"source": "GPS"}})
    assert r["getLocation"]["lla"]["lat"] == 32.89


def test_write_requests_are_refused_before_sending():
    with FakeDish(STATUS) as dish:
        client = StarlinkClient(dish.address, timeout=2)
        for bad in ({"reboot": {}}, {"dishStow": {}}, {"getStatus": {}, "reboot": {}}, {}):
            with pytest.raises(ValueError):
                client.handle(bad)
        assert dish.calls == []


def test_request_this_firmware_lacks_is_unsupported():
    with FakeDish(STATUS) as dish:
        with pytest.raises(StarlinkError) as e:
            StarlinkClient(dish.address, timeout=2).handle({"getHistory": {}})
    assert e.value.kind == "unsupported"


def test_no_dish_is_unreachable_and_quick():
    started = time.monotonic()
    with pytest.raises(StarlinkError) as e:
        StarlinkClient("127.0.0.1:1", timeout=1).handle({"getStatus": {}})
    assert e.value.kind == "unreachable"
    assert time.monotonic() - started < 3


def test_no_reflection_is_unsupported():
    with FakeDish(STATUS, reflection_enabled=False) as dish:
        with pytest.raises(StarlinkError) as e:
            StarlinkClient(dish.address, timeout=2).handle({"getStatus": {}})
    assert e.value.kind == "unsupported"


def test_recovers_after_dish_reboot():
    """Same address, new server: the client drops the dead channel and
    re-learns the schema (a reboot can bring new firmware)."""
    with FakeDish(STATUS) as dish:
        client = StarlinkClient(dish.address, timeout=1)
        client.handle({"getStatus": {}})
        port = dish.port
    with pytest.raises(StarlinkError) as e:
        client.handle({"getStatus": {}})
    assert e.value.kind == "unreachable"
    with FakeDish(dict(STATUS, popPingLatencyMs=30.0), port=port):
        assert client.handle({"getStatus": {}})["dishGetStatus"]["popPingLatencyMs"] == 30.0
