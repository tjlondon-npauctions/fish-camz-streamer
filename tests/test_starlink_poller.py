"""The poller must degrade, never break: boats are wired differently, and it
shares a container with the heartbeat."""

import json
from pathlib import Path
import logging

import pytest

from app.starlink.client import StarlinkError
from app.starlink.poller import RETRY_SECONDS, StarlinkPoller, for_heartbeat
from test_starlink_summary import TUNA


DIAGNOSTICS = {"dishGetDiagnostics": {"hardwareSelfTest": "PASSED", "disablementCode": "OKAY",
                                      "overageRateLimited": False,
                                      "location": {"enabled": False}}}


class ScriptedClient:
    """Stands in for StarlinkClient: per-request result or StarlinkError."""

    def __init__(self, status=TUNA, location=None, diagnostics=DIAGNOSTICS, **others):
        self.results = {"getStatus": status, "getLocation": location, "getDiagnostics": diagnostics, **others}
        self.sent = []

    @property
    def status(self):
        return self.results["getStatus"]

    def handle(self, request):
        name = next(iter(request))
        self.sent.append(name)
        result = self.results.get(name)
        if isinstance(result, StarlinkError):
            raise result
        if result is None:
            raise StarlinkError("unsupported", f"no {name}")
        return result

    def close(self):
        pass


def poller(tmp_path, client):
    return StarlinkPoller(state_dir=str(tmp_path), poll_interval=15, client=client)


def state(tmp_path):
    return json.loads((tmp_path / "starlink.json").read_text())


def test_healthy_dish(tmp_path):
    p = poller(tmp_path, ScriptedClient(location=StarlinkError("not_permitted", "Requests are not enabled")))
    assert p.poll_once() == 15
    s = state(tmp_path)
    assert s["status"] == "ok" and s["reachable"] is True
    assert s["summary"]["hardware"] == "rev4_prod2"
    assert s["raw"]["deviceInfo"]["id"]  # full status kept locally for the dashboard
    assert s["location_access"] == "not_permitted"


@pytest.mark.parametrize("kind", ["unreachable", "not_permitted", "unsupported", "unavailable", "error"])
def test_failures_become_states_with_backoff(tmp_path, kind):
    p = poller(tmp_path, ScriptedClient(status=StarlinkError(kind, "boom")))
    assert p.poll_once() == RETRY_SECONDS[kind]
    s = state(tmp_path)
    assert s["status"] == kind and s["detail"] == "boom"


def test_no_dish_logs_once_not_every_poll(tmp_path, caplog):
    p = poller(tmp_path, ScriptedClient(status=StarlinkError("unreachable", "no answer")))
    with caplog.at_level(logging.INFO, logger="app.starlink.poller"):
        for _ in range(5):
            p.poll_once()
    assert len([r for r in caplog.records if "unreachable" in r.getMessage()]) == 1


def test_location_coordinates_are_never_stored(tmp_path):
    loc = {"getLocation": {"lla": {"lat": 32.894321, "lon": -117.124556, "alt": 95.4}, "source": "GPS"}}
    p = poller(tmp_path, ScriptedClient(location=loc))
    p.poll_once()
    text = (tmp_path / "starlink.json").read_text()
    assert state(tmp_path)["location_access"] == "enabled"
    assert "32.89" not in text and "117.12" not in text


def test_location_probed_rarely(tmp_path):
    client = ScriptedClient(location=StarlinkError("not_permitted", "no"))
    p = poller(tmp_path, client)
    for _ in range(10):
        p.poll_once()
    assert client.sent.count("getLocation") == 1


def test_run_loop_survives_unexpected_errors(tmp_path):
    class Exploding(ScriptedClient):
        def handle(self, request):
            raise RuntimeError("unexpected")

    p = poller(tmp_path, Exploding())
    p._stop.wait = lambda delay: p._stop.set()  # one iteration
    p._run()  # must not raise


def test_heartbeat_is_small_and_has_no_raw(tmp_path):
    p = poller(tmp_path, ScriptedClient(location=StarlinkError("not_permitted", "no")))
    p.poll_once()
    hb = for_heartbeat(state(tmp_path))
    assert "raw" not in hb and hb["summary"]["state"] == "ok"
    assert len(json.dumps(hb)) < 1000
    assert for_heartbeat({}) == {}


def test_missing_grpc_degrades(tmp_path, monkeypatch):
    import builtins
    real_import = builtins.__import__

    def no_grpc(name, *args, **kwargs):
        if name == "grpc" or name.startswith("grpc."):
            raise ImportError("No module named 'grpc'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_grpc)
    from app.starlink.client import StarlinkClient
    p = StarlinkPoller(state_dir=str(tmp_path), client=StarlinkClient("127.0.0.1:1", timeout=1))
    assert p.poll_once() == RETRY_SECONDS["unavailable"]
    assert state(tmp_path)["status"] == "unavailable"


def test_heartbeat_payload_tolerates_missing_or_bad_file(tmp_path):
    from app.heartbeat import _starlink_summary
    assert _starlink_summary(str(tmp_path)) == {}
    (tmp_path / "starlink.json").write_text("{not json")
    assert _starlink_summary(str(tmp_path)) == {}


def test_api_requires_login(tmp_path, monkeypatch):
    from flask import Flask
    from app.web import api as api_mod

    app = Flask(__name__)
    app.secret_key = "test"
    app.register_blueprint(api_mod.api)
    monkeypatch.setattr(api_mod.manager, "load", lambda: {"system": {"state_dir": str(tmp_path)}})
    poller(tmp_path, ScriptedClient(location=StarlinkError("not_permitted", "no"))).poll_once()

    client = app.test_client()
    assert client.get("/api/starlink").status_code == 401
    with client.session_transaction() as s:
        s["authenticated"] = True
    body = client.get("/api/starlink").get_json()
    assert body["status"] == "ok" and body["updated_age_seconds"] < 5


ROUTER = json.loads((Path(__file__).parent / "fixtures" / "starlink_router_interfaces_vendetta.json").read_text())


def with_router(tmp_path, dish=None, router=None):
    return StarlinkPoller(state_dir=str(tmp_path), poll_interval=15,
                          client=dish or ScriptedClient(location=StarlinkError("not_permitted", "no")),
                          router_client=router, usage_path=str(tmp_path / "usage.json"))


class TestDiagnosticsInPoller:
    def test_overage_from_diagnostics_reaches_the_verdict(self, tmp_path):
        d = {"dishGetDiagnostics": {"overageRateLimited": True, "disablementCode": "OKAY"}}
        p = poller(tmp_path, ScriptedClient(diagnostics=d, location=StarlinkError("not_permitted", "no")))
        p.poll_once()
        assert state(tmp_path)["summary"]["state"] == "rate_limited"

    def test_dish_refusing_diagnostics_still_works(self, tmp_path):
        p = poller(tmp_path, ScriptedClient(diagnostics=StarlinkError("not_permitted", "no"),
                                            location=StarlinkError("not_permitted", "no")))
        p.poll_once()
        s = state(tmp_path)
        assert s["status"] == "ok" and s["diagnostics_status"] == "not_permitted"

    def test_raw_diagnostics_never_holds_coordinates(self, tmp_path):
        d = {"dishGetDiagnostics": {"location": {"enabled": True, "latitude": 32.894321, "longitude": -117.124556}}}
        p = poller(tmp_path, ScriptedClient(diagnostics=d, location=StarlinkError("not_permitted", "no")))
        p.poll_once()
        text = (tmp_path / "starlink.json").read_text()
        assert "32.894" not in text and "117.12" not in text
        assert state(tmp_path)["raw_diagnostics"]["location"] == {"enabled": True}


class TestRouterMeter:
    def test_meter_accumulates_across_polls(self, tmp_path, monkeypatch):
        import copy
        import app.starlink.poller as mod
        router = ScriptedClient(getNetworkInterfaces=ROUTER)
        p = with_router(tmp_path, router=router)
        clock = [1790640100.0]
        monkeypatch.setattr(mod.time, "time", lambda: clock[0])
        p.poll_once()  # baseline
        later = copy.deepcopy(ROUTER)
        wan = later["getNetworkInterfaces"]["networkInterfaces"][2]
        wan["txStats"]["bytes"] = str(int(wan["txStats"]["bytes"]) + 5_000_000)
        router.results["getNetworkInterfaces"] = later
        clock[0] += 61
        p.poll_once()
        usage = state(tmp_path)["usage"]
        assert usage["today"]["tx"] == 5_000_000 and usage["interface"] == "wan0"
        assert for_heartbeat(state(tmp_path))["usage"]["today"]["tx"] == 5_000_000

    def test_router_sampled_once_a_minute_not_every_poll(self, tmp_path):
        router = ScriptedClient(getNetworkInterfaces=ROUTER)
        p = with_router(tmp_path, router=router)
        for _ in range(4):
            p.poll_once()
        assert router.sent.count("getNetworkInterfaces") == 1

    def test_no_router_is_quiet_and_backs_off(self, tmp_path):
        router = ScriptedClient(getNetworkInterfaces=StarlinkError("unreachable", "no router"))
        p = with_router(tmp_path, router=router)
        p.poll_once()
        assert state(tmp_path)["usage_status"] == "unreachable"
        assert state(tmp_path)["status"] == "ok"  # dish unaffected

    def test_router_works_even_when_dish_is_unreachable(self, tmp_path):
        router = ScriptedClient(getNetworkInterfaces=ROUTER)
        p = with_router(tmp_path, dish=ScriptedClient(status=StarlinkError("unreachable", "x")), router=router)
        p.poll_once()
        assert state(tmp_path)["usage_status"] == "ok"


class TestHistoryInPoller:
    def history(self, now):
        ring = [0.02] * 900
        return {"dishGetHistory": {"current": "5000", "popPingDropRate": ring, "popPingLatencyMs": [30.0] * 900,
                                   "eventLog": {"events": [
                                       {"reason": "EVENT_REASON_OUTAGE_NO_PINGS",
                                        "startTimestampNs": str(int((now - 120) * 1e9)), "durationNs": "800000000"},
                                       {"reason": "EVENT_REASON_OUTAGE_OBSTRUCTED",
                                        "startTimestampNs": str(int((now - 7200) * 1e9)), "durationNs": "42000000000"},
                                   ]}}}

    def test_drop_rate_and_outages(self, tmp_path):
        import time as _t
        now = _t.time()
        client = ScriptedClient(location=StarlinkError("not_permitted", "no"), getHistory=self.history(now))
        p = poller(tmp_path, client)
        p.poll_once()
        s = state(tmp_path)
        assert s["summary"]["pop_drop_rate"] == 0.02
        assert s["summary"]["pop_latency_1m_ms"] == 30.0
        assert [o["cause"] for o in s["outages"]] == ["OBSTRUCTED", "NO_PINGS"]
        hb = for_heartbeat(s)["outages"]
        assert {o["cause"] for o in hb} == {"OBSTRUCTED", "NO_PINGS"}

    def test_history_read_once_a_minute(self, tmp_path):
        import time as _t
        client = ScriptedClient(location=StarlinkError("not_permitted", "no"), getHistory=self.history(_t.time()))
        p = poller(tmp_path, client)
        for _ in range(4):
            p.poll_once()
        assert client.sent.count("getHistory") == 1

    def test_dish_without_history_still_polls(self, tmp_path):
        p = poller(tmp_path, ScriptedClient(location=StarlinkError("not_permitted", "no")))
        p.poll_once()
        s = state(tmp_path)
        assert s["status"] == "ok" and s["history_status"] == "unsupported"
        assert "outages" not in for_heartbeat(s)
