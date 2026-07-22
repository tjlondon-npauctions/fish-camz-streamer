"""Tests for camera probe retry + copy-mode fallback.

A transient probe failure on restart (e.g. camera mid-reboot) must not
permanently downgrade a copyable H.264 source to transcode mode.
"""

from unittest import mock

from app.camera.probe import StreamInfo
from app.streaming.engine import StreamEngine, _stream_info_from_config


def _config(**cam):
    c = {
        "camera": {
            "rtsp_url": "rtsp://cam/live",
            "username": "",
            "password": "",
            "transport": "tcp",
            "probe_retries": 3,
            "probe_retry_delay": 0,  # no real sleeping in tests
        },
        "system": {"state_dir": "/tmp"},
        "stream": {"restart_delay": 5},
    }
    c["camera"].update(cam)
    return c


def test_probe_success_caches_result():
    eng = StreamEngine(_config())
    info = StreamInfo(video_codec="h264", can_copy=True, width=1920, height=1080, framerate=15)
    with mock.patch("app.streaming.engine.probe_stream", return_value=info) as ps:
        got = eng._probe_camera()
    assert got is info
    assert eng._last_probe is info
    ps.assert_called_once()


def test_probe_retries_until_success():
    eng = StreamEngine(_config())
    info = StreamInfo(video_codec="h264", can_copy=True)
    with mock.patch(
        "app.streaming.engine.probe_stream",
        side_effect=[RuntimeError("down"), RuntimeError("down"), info],
    ) as ps:
        got = eng._probe_camera()
    assert got is info
    assert ps.call_count == 3


def test_probe_failure_reuses_in_memory_probe():
    eng = StreamEngine(_config())
    cached = StreamInfo(video_codec="h264", can_copy=True)
    eng._last_probe = cached  # a prior successful probe this session
    with mock.patch("app.streaming.engine.probe_stream", side_effect=RuntimeError("down")):
        got = eng._probe_camera()
    assert got is cached  # stayed in copy mode instead of transcoding


def test_probe_failure_reuses_config_last_probe():
    cfg = _config(last_probe={
        "url": "rtsp://cam/live",
        "video_codec": "h264",
        "resolution": "1920x1080",
        "framerate": 15,
        "can_copy": True,
    })
    eng = StreamEngine(cfg)
    with mock.patch("app.streaming.engine.probe_stream", side_effect=RuntimeError("down")):
        got = eng._probe_camera()
    assert got is not None and got.can_copy and got.is_h264 and got.width == 1920


def test_probe_failure_no_fallback_returns_none():
    eng = StreamEngine(_config())
    with mock.patch("app.streaming.engine.probe_stream", side_effect=RuntimeError("down")):
        got = eng._probe_camera()
    assert got is None  # genuinely unknown source → transcode fallback


def test_config_fallback_requires_matching_url_and_copy():
    # URL mismatch → no reuse
    assert _stream_info_from_config(
        {"rtsp_url": "rtsp://a", "last_probe": {"url": "rtsp://b", "can_copy": True}}
    ) is None
    # not copyable → no reuse
    assert _stream_info_from_config(
        {"rtsp_url": "rtsp://a", "last_probe": {"url": "rtsp://a", "can_copy": False}}
    ) is None
    # valid → reconstructed StreamInfo
    ok = _stream_info_from_config({
        "rtsp_url": "rtsp://a",
        "last_probe": {"url": "rtsp://a", "can_copy": True, "video_codec": "h264",
                       "resolution": "1280x720", "framerate": 30},
    })
    assert ok is not None and ok.can_copy and ok.width == 1280 and ok.height == 720
