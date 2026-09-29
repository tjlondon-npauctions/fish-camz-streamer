"""The poller must degrade, never break: boats are wired differently, and it
shares a container with the heartbeat."""

import json
import logging

import pytest

from app.starlink.client import StarlinkError
from app.starlink.poller import RETRY_SECONDS, StarlinkPoller, for_heartbeat
from test_starlink_summary import TUNA


class ScriptedClient:
    """Stands in for StarlinkClient: returns or raises per request name."""

    def __init__(self, status=TUNA, location=None):
        self.status = status
        self.location = location
        self.sent = []

    def handle(self, request):
        name = next(iter(request))
        self.sent.append(name)
        result = self.status if name == "getStatus" else self.location
        if isinstance(result, StarlinkError):
            raise result
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
