import os
import tempfile
from pathlib import Path
from unittest import mock

import yaml

from app.config import manager


def _make_config(**overrides):
    """Build a minimal valid config with optional overrides."""
    config = {
        "camera": {"rtsp_url": "rtsp://192.168.1.100:554/stream1", "transport": "tcp"},
        "cloudflare": {
            "rtmps_url": "rtmps://live.cloudflare.com:443/live/",
            "stream_key": "abc123",
        },
        "encoding": {"mode": "auto", "video_bitrate": "2500k"},
        "web": {"port": 8080, "username": "admin", "password_hash": "hashed"},
    }
    for section, values in overrides.items():
        config.setdefault(section, {}).update(values)
    return config


class TestDeepMerge:
    def test_adds_missing_keys(self):
        base = {"a": 1, "b": {"c": 2, "d": 3}}
        override = {"b": {"c": 99}}
        result = manager._deep_merge(base, override)
        assert result == {"a": 1, "b": {"c": 99, "d": 3}}

    def test_does_not_remove_user_keys(self):
        base = {"a": 1}
        override = {"a": 1, "b": 2}
        result = manager._deep_merge(base, override)
        assert result == {"a": 1, "b": 2}


class TestValidate:
    def test_valid_config(self):
        assert manager.validate(_make_config()) == []

    def test_invalid_rtsp_url(self):
        config = _make_config(camera={"rtsp_url": "http://bad"})
        errors = manager.validate(config)
        assert any("rtsp://" in e for e in errors)

    def test_invalid_encoding_mode(self):
        config = _make_config(encoding={"mode": "invalid"})
        errors = manager.validate(config)
        assert any("mode" in e for e in errors)

    def test_invalid_bitrate(self):
        config = _make_config(encoding={"video_bitrate": "2500"})
        errors = manager.validate(config)
        assert any("bitrate" in e for e in errors)

    def test_invalid_port(self):
        config = _make_config(web={"port": 99999})
        errors = manager.validate(config)
        assert any("port" in e for e in errors)

    def test_empty_rtsp_url_is_ok(self):
        config = _make_config(camera={"rtsp_url": ""})
        errors = manager.validate(config)
        assert not any("rtsp" in e.lower() for e in errors)


class TestGetSet:
    def test_get_existing(self):
        config = _make_config()
        assert manager.get(config, "camera", "transport") == "tcp"

    def test_get_missing_returns_default(self):
        assert manager.get({}, "camera", "rtsp_url", "fallback") == "fallback"

    def test_set_value(self):
        config = _make_config()
        manager.set_value(config, "camera", "rtsp_url", "rtsp://new")
        assert config["camera"]["rtsp_url"] == "rtsp://new"

    def test_set_value_creates_section(self):
        config = {}
        manager.set_value(config, "new_section", "key", "value")
        assert config["new_section"]["key"] == "value"


class TestSetupChecks:
    def test_setup_complete(self):
        config = _make_config()
        assert manager.is_setup_complete(config) is True

    def test_setup_incomplete(self):
        config = _make_config(web={"password_hash": ""})
        assert manager.is_setup_complete(config) is False

    def test_streaming_ready(self):
        config = _make_config()
        assert manager.is_streaming_ready(config) is True

    def test_streaming_not_ready_no_rtsp(self):
        config = _make_config(camera={"rtsp_url": ""})
        assert manager.is_streaming_ready(config) is False

    def test_streaming_not_ready_no_key(self):
        config = _make_config(cloudflare={"stream_key": ""})
        assert manager.is_streaming_ready(config) is False

    def test_streaming_ready_hls_mode(self):
        config = _make_config(
            output={"mode": "hls"},
            bunny={"storage_zone": "test-zone", "api_key": "test-key"},
        )
        assert manager.is_streaming_ready(config) is True

    def test_streaming_not_ready_hls_no_zone(self):
        config = _make_config(
            output={"mode": "hls"},
            bunny={"storage_zone": "", "api_key": "test-key"},
        )
        assert manager.is_streaming_ready(config) is False


class TestLoadSave:
    def test_save_and_load_roundtrip(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        data_dir = tmp_path

        with mock.patch.object(manager, "CONFIG_FILE", config_file), \
             mock.patch.object(manager, "DATA_DIR", data_dir), \
             mock.patch.object(manager, "DEFAULT_CONFIG", config_file):
            config = _make_config()
            manager.save(config)

            with open(config_file) as f:
                loaded = yaml.safe_load(f)

            assert loaded["camera"]["rtsp_url"] == "rtsp://192.168.1.100:554/stream1"
            assert loaded["cloudflare"]["stream_key"] == "abc123"

    def test_save_sets_permissions(self, tmp_path):
        config_file = tmp_path / "config.yaml"

        with mock.patch.object(manager, "CONFIG_FILE", config_file), \
             mock.patch.object(manager, "DATA_DIR", tmp_path):
            manager.save({"test": True})
            mode = oct(os.stat(config_file).st_mode)[-3:]
            assert mode == "600"
