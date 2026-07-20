"""Tests for cloud-driven stream start/stop via the heartbeat response.

Covers container_control.set_stream validation and HeartbeatSender's
_apply_stream_command gating + idempotency (the dock-aware auto-stream path).
"""

from unittest import mock

from app.streaming import container_control
from app.heartbeat import HeartbeatSender


def test_set_stream_rejects_unknown_action():
    result = container_control.set_stream("explode")
    assert result == {"ok": False, "error": "Unknown action: explode"}


def _sender():
    # Backend URL empty so start() is a no-op; we call the method directly.
    return HeartbeatSender({"backend": {}})


def test_apply_stream_command_ignored_when_remote_control_disabled():
    sender = _sender()
    with mock.patch("app.config.manager.load", return_value={
        "stream": {"allow_remote_control": False},
    }), mock.patch("app.streaming.container_control.set_stream") as set_stream, \
            mock.patch("app.heartbeat._read_state_file", return_value={"running": False}):
        sender._apply_stream_command("start", "/run/rpie")
        set_stream.assert_not_called()


def test_apply_stream_command_noop_when_already_running():
    sender = _sender()
    with mock.patch("app.config.manager.load", return_value={
        "stream": {"allow_remote_control": True},
    }), mock.patch("app.streaming.container_control.set_stream") as set_stream, \
            mock.patch("app.heartbeat._read_state_file", return_value={"running": True}):
        sender._apply_stream_command("start", "/run/rpie")
        set_stream.assert_not_called()  # idempotent — already in desired state


def test_apply_stream_command_starts_when_stopped():
    sender = _sender()
    with mock.patch("app.config.manager.load", return_value={
        "stream": {"allow_remote_control": True},
    }), mock.patch("app.streaming.container_control.set_stream",
                   return_value={"ok": True, "action": "start"}) as set_stream, \
            mock.patch("app.heartbeat._read_state_file", return_value={"running": False}):
        sender._apply_stream_command("start", "/run/rpie")
        set_stream.assert_called_once_with("start")


def test_apply_stream_command_stops_when_running():
    sender = _sender()
    with mock.patch("app.config.manager.load", return_value={
        "stream": {"allow_remote_control": True},
    }), mock.patch("app.streaming.container_control.set_stream",
                   return_value={"ok": True, "action": "stop"}) as set_stream, \
            mock.patch("app.heartbeat._read_state_file", return_value={"running": True}):
        sender._apply_stream_command("stop", "/run/rpie")
        set_stream.assert_called_once_with("stop")


def test_apply_stream_command_ignores_invalid_command():
    sender = _sender()
    with mock.patch("app.streaming.container_control.set_stream") as set_stream:
        sender._apply_stream_command("restart", "/run/rpie")  # only start/stop honoured
        set_stream.assert_not_called()
