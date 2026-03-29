from app.camera.probe import StreamInfo
from app.streaming.ffmpeg_builder import build_command


def _base_config(**overrides):
    config = {
        "camera": {
            "rtsp_url": "rtsp://192.168.1.100:554/stream1",
            "username": "",
            "password": "",
            "transport": "tcp",
        },
        "cloudflare": {
            "rtmps_url": "rtmps://live.cloudflare.com:443/live/",
            "stream_key": "test-key-123",
        },
        "encoding": {
            "mode": "auto",
            "video_bitrate": "2500k",
            "max_video_bitrate": "3000k",
            "buffer_size": "5000k",
            "resolution": "source",
            "framerate": "source",
            "keyframe_interval": 2,
            "audio_bitrate": "128k",
            "preset": "veryfast",
        },
    }
    for section, values in overrides.items():
        config.setdefault(section, {}).update(values)
    return config


def _h264_aac_probe():
    return StreamInfo(
        video_codec="h264", audio_codec="aac",
        width=1920, height=1080, framerate=30.0,
        is_h264=True, is_aac=True, can_copy=True,
    )


def _h265_probe():
    return StreamInfo(
        video_codec="hevc", audio_codec="aac",
        width=1920, height=1080, framerate=25.0,
        is_h264=False, is_aac=True, can_copy=False,
    )


def _h264_no_audio_probe():
    return StreamInfo(
        video_codec="h264", audio_codec="",
        width=1280, height=720, framerate=15.0,
        is_h264=True, is_aac=False, can_copy=True,
    )


class TestAutoMode:
    def test_copies_when_h264_aac(self):
        cmd = build_command(_base_config(), _h264_aac_probe())
        assert "-c" in cmd
        assert cmd[cmd.index("-c") + 1] == "copy"
        assert "libx264" not in cmd

    def test_transcodes_when_h265(self):
        cmd = build_command(_base_config(), _h265_probe())
        assert "libx264" in cmd
        assert "-c:a" in cmd

    def test_transcodes_when_no_probe(self):
        cmd = build_command(_base_config(), None)
        assert "libx264" in cmd


class TestCopyMode:
    def test_forced_copy(self):
        config = _base_config(encoding={"mode": "copy"})
        cmd = build_command(config, _h265_probe())  # Even with H.265
        assert "-c" in cmd or "-c:v" in cmd

    def test_copy_no_audio(self):
        config = _base_config(encoding={"mode": "copy"})
        cmd = build_command(config, _h264_no_audio_probe())
        assert "-an" in cmd


class TestTranscodeMode:
    def test_forced_transcode(self):
        config = _base_config(encoding={"mode": "transcode"})
        cmd = build_command(config, _h264_aac_probe())
        assert "libx264" in cmd

    def test_custom_bitrate(self):
        config = _base_config(encoding={"mode": "transcode", "video_bitrate": "4000k"})
        cmd = build_command(config, _h264_aac_probe())
        idx = cmd.index("-b:v")
        assert cmd[idx + 1] == "4000k"

    def test_resolution_override(self):
        config = _base_config(encoding={"mode": "transcode", "resolution": "1280x720"})
        cmd = build_command(config, _h264_aac_probe())
        assert "-vf" in cmd
        assert "scale=1280:720" in cmd[cmd.index("-vf") + 1]

    def test_framerate_override(self):
        config = _base_config(encoding={"mode": "transcode", "framerate": 15})
        cmd = build_command(config, _h264_aac_probe())
        idx = cmd.index("-r")
        assert cmd[idx + 1] == "15"

    def test_keyframe_interval(self):
        config = _base_config(encoding={"mode": "transcode", "keyframe_interval": 2})
        probe = _h264_aac_probe()  # 30fps
        cmd = build_command(config, probe)
        idx = cmd.index("-g")
        assert cmd[idx + 1] == "60"  # 30fps * 2s = 60 frames

    def test_copies_aac_audio_in_transcode(self):
        """If video needs transcode but audio is already AAC, copy audio."""
        config = _base_config(encoding={"mode": "transcode"})
        probe = _h265_probe()  # H.265 + AAC
        cmd = build_command(config, probe)
        assert "libx264" in cmd
        idx = cmd.index("-c:a")
        assert cmd[idx + 1] == "copy"

    def test_no_audio_stream(self):
        config = _base_config(encoding={"mode": "transcode"})
        cmd = build_command(config, _h264_no_audio_probe())
        assert "-an" in cmd

    def test_auto_downscale_high_res(self):
        """5MP+ sources should auto-downscale to 720p when transcoding."""
        config = _base_config(encoding={"mode": "transcode", "resolution": "source"})
        probe = StreamInfo(
            video_codec="hevc", width=2880, height=1620, framerate=25.0,
            is_h264=False, is_aac=False, can_copy=False,
        )
        cmd = build_command(config, probe)
        assert "-vf" in cmd
        assert "scale=1280:720" in cmd[cmd.index("-vf") + 1]

    def test_no_downscale_1080p(self):
        """1080p should not be auto-downscaled."""
        config = _base_config(encoding={"mode": "transcode", "resolution": "source"})
        cmd = build_command(config, _h265_probe())  # 1920x1080
        assert "-vf" not in cmd


class TestInputOutput:
    def test_rtsp_transport(self):
        cmd = build_command(_base_config(), _h264_aac_probe())
        idx = cmd.index("-rtsp_transport")
        assert cmd[idx + 1] == "tcp"

    def test_rtsp_no_reconnect_flags(self):
        """RTSP inputs must NOT have -reconnect flags (they only work with HTTP/RTMP)."""
        cmd = build_command(_base_config(), _h264_aac_probe())
        assert "-reconnect" not in cmd
        assert "-reconnect_streamed" not in cmd
        assert "-stimeout" in cmd

    def test_http_has_reconnect_flags(self):
        """HTTP inputs should have reconnect flags."""
        config = _base_config(camera={"rtsp_url": "http://192.168.1.100/stream"})
        cmd = build_command(config, _h264_aac_probe())
        assert "-reconnect" in cmd
        assert "-rtsp_transport" not in cmd

    def test_rtmps_output(self):
        cmd = build_command(_base_config(), _h264_aac_probe())
        assert cmd[-1] == "rtmps://live.cloudflare.com:443/live/test-key-123"
        assert cmd[-2] == "flv"
        assert cmd[-3] == "-f"

    def test_credentials_embedded(self):
        config = _base_config(camera={
            "rtsp_url": "rtsp://192.168.1.100:554/stream",
            "username": "admin",
            "password": "pass123",
        })
        cmd = build_command(config, _h264_aac_probe())
        idx = cmd.index("-i")
        assert cmd[idx + 1] == "rtsp://admin:pass123@192.168.1.100:554/stream"

    def test_no_duplicate_credentials(self):
        """Don't double-embed credentials if already in URL."""
        config = _base_config(camera={
            "rtsp_url": "rtsp://admin:pass@192.168.1.100:554/stream",
            "username": "admin",
            "password": "pass",
        })
        cmd = build_command(config, _h264_aac_probe())
        idx = cmd.index("-i")
        assert cmd[idx + 1].count("@") == 1
