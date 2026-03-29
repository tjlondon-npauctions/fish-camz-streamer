"""Builds FFmpeg command-line arguments from config and probe results.

This is a pure function with no side effects — easy to test and debug.
"""

from __future__ import annotations

from typing import Optional

from app.camera.probe import StreamInfo


def build_command(config: dict, probe: Optional[StreamInfo] = None) -> list[str]:
    """Build a complete FFmpeg command as a list of arguments.

    Args:
        config: Application config dict
        probe: Optional StreamInfo from probing the camera. If None,
               falls back to transcode mode.

    Returns:
        List of strings suitable for subprocess.Popen()
    """
    cam = config.get("camera", {})
    cf = config.get("cloudflare", {})
    enc = config.get("encoding", {})

    rtsp_url = _build_rtsp_url(cam)
    rtmps_url = _build_rtmps_url(cf)

    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-progress", "pipe:1"]

    # Input options
    cmd += _input_args(cam, rtsp_url)

    # Encoding options (copy vs transcode)
    cmd += _encoding_args(enc, probe)

    # Output
    cmd += ["-f", "flv", rtmps_url]

    return cmd


def _build_rtsp_url(cam: dict) -> str:
    """Construct RTSP URL with embedded credentials if provided."""
    url = cam.get("rtsp_url", "")
    username = cam.get("username", "")
    password = cam.get("password", "")

    if username and "://" in url and "@" not in url:
        proto, rest = url.split("://", 1)
        url = f"{proto}://{username}:{password}@{rest}"

    return url


def _build_rtmps_url(cf: dict) -> str:
    """Construct the full RTMPS output URL."""
    rtmps_url = cf.get("rtmps_url", "rtmps://live.cloudflare.com:443/live/")
    stream_key = cf.get("stream_key", "")

    # Ensure URL ends with /
    if not rtmps_url.endswith("/"):
        rtmps_url += "/"

    return f"{rtmps_url}{stream_key}"


def _input_args(cam: dict, rtsp_url: str) -> list[str]:
    """Build input-side FFmpeg arguments."""
    transport = cam.get("transport", "tcp")

    args = []

    if rtsp_url.startswith("rtsp://"):
        # RTSP-specific options (reconnect flags are NOT valid for RTSP)
        args += ["-rtsp_transport", transport]
        # Timeout for RTSP connection (microseconds)
        args += ["-timeout", "10000000"]
    else:
        # HTTP/RTMP reconnect options (only valid for these protocols)
        args += ["-reconnect", "1"]
        args += ["-reconnect_streamed", "1"]
        args += ["-reconnect_delay_max", "30"]

    args += ["-i", rtsp_url]

    return args


def _encoding_args(enc: dict, probe: Optional[StreamInfo]) -> list[str]:
    """Build encoding arguments based on mode and probe results."""
    mode = enc.get("mode", "auto")

    if mode == "copy" or (mode == "auto" and probe and probe.can_copy):
        return _copy_args(probe)

    return _transcode_args(enc, probe)


def _copy_args(probe: Optional[StreamInfo]) -> list[str]:
    """Arguments for codec copy (passthrough)."""
    args = ["-c", "copy"]

    # If no audio stream detected, don't try to copy audio
    if probe and not probe.audio_codec:
        args = ["-c:v", "copy", "-an"]

    return args


def _transcode_args(enc: dict, probe: Optional[StreamInfo]) -> list[str]:
    """Arguments for software transcoding."""
    args = []

    # Video encoding
    args += [
        "-c:v", "libx264",
        "-preset", enc.get("preset", "veryfast"),
        "-b:v", enc.get("video_bitrate", "2500k"),
        "-maxrate", enc.get("max_video_bitrate", "3000k"),
        "-bufsize", enc.get("buffer_size", "5000k"),
    ]

    # Resolution scaling
    resolution = enc.get("resolution", "source")
    if resolution == "source" and probe and probe.width > 1920:
        # Auto-downscale high-res sources (e.g. 5MP) to 1280x720 when transcoding
        # to keep CPU usage manageable on a Raspberry Pi
        args += ["-vf", "scale=1280:720"]
    elif resolution != "source" and "x" in str(resolution):
        parts = str(resolution).split("x")
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            args += ["-vf", f"scale={parts[0]}:{parts[1]}"]

    # Framerate
    framerate = enc.get("framerate", "source")
    if framerate != "source":
        args += ["-r", str(framerate)]

    # Keyframe interval
    keyframe_secs = enc.get("keyframe_interval", 2)
    fps = probe.framerate if probe and probe.framerate else 30
    if framerate != "source":
        fps = float(framerate)
    gop = int(fps * keyframe_secs)
    args += ["-g", str(gop)]

    # Audio encoding
    if probe and not probe.audio_codec:
        args += ["-an"]
    elif probe and probe.is_aac:
        args += ["-c:a", "copy"]
    else:
        args += [
            "-c:a", "aac",
            "-b:a", enc.get("audio_bitrate", "128k"),
        ]

    return args
