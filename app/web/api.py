"""JSON API endpoints for the dashboard AJAX calls and stream control."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from flask import Blueprint, jsonify, request

from app.camera.discovery import (
    discover_cameras,
    detect_brand,
    get_channel_urls,
    get_common_rtsp_urls,
)
from app.camera.probe import probe_stream
from app.config import manager
from app.system.stats import get_system_stats

logger = logging.getLogger(__name__)

api = Blueprint("api", __name__, url_prefix="/api")

VERSION_FILE = Path(__file__).resolve().parent.parent.parent / "VERSION"


def _get_version() -> str:
    try:
        return VERSION_FILE.read_text().strip()
    except OSError:
        return "unknown"


@api.route("/version")
def version():
    """Current software version."""
    return jsonify({"version": _get_version()})


def _read_state_file(filename: str) -> dict:
    """Read a JSON state file from tmpfs."""
    config = manager.load()
    state_dir = manager.get(config, "system", "state_dir", "/run/rpie")
    state_file = Path(state_dir) / filename
    try:
        if state_file.exists():
            with open(state_file) as f:
                return json.load(f)
    except (json.JSONDecodeError, OSError):
        pass
    return {}


@api.route("/status")
def stream_status():
    """Current stream status (read from shared tmpfs)."""
    config = manager.load()
    state = _read_state_file("state.json")
    if not state:
        state = {
            "running": False,
            "uptime_seconds": 0,
            "restart_count": 0,
            "last_error": "",
            "fps": 0,
            "bitrate_kbps": 0,
            "speed": 0,
            "frame_count": 0,
            "is_stalled": False,
            "is_slow": False,
            "pid": None,
        }
    # Include config diagnostics
    state["config_output_mode"] = manager.get(config, "output", "mode", "rtmp")
    state["config_bunny_cdn_url"] = manager.get(config, "bunny", "cdn_url", "")
    state["config_bunny_storage_zone"] = manager.get(config, "bunny", "storage_zone", "")
    return jsonify(state)


@api.route("/system")
def system_stats():
    """System resource usage."""
    return jsonify(get_system_stats())


@api.route("/network")
def network_status():
    """Network connectivity status."""
    state = _read_state_file("network.json")
    if not state:
        return jsonify({
            "connected": True,
            "latency_ms": 0,
            "last_check": 0,
            "in_extended_outage": False,
        })
    return jsonify(state)


@api.route("/uploader")
def uploader_status():
    """HLS uploader status (read from shared tmpfs)."""
    state = _read_state_file("uploader.json")
    if not state:
        return jsonify({
            "running": False,
            "upload_count": 0,
            "error_count": 0,
            "last_error": "",
            "segments_tracked": 0,
        })
    return jsonify(state)


@api.route("/gps")
def gps_status():
    """GPS position and status."""
    state = _read_state_file("gps.json")
    if not state:
        return jsonify({
            "has_fix": False,
            "lat": None,
            "lng": None,
            "speed_knots": None,
            "heading": None,
            "error": "GPS not enabled",
        })
    return jsonify(state)


@api.route("/stream/<action>", methods=["POST"])
def stream_control(action):
    """Control the streamer container: start, stop, restart."""
    if action not in ("start", "stop", "restart"):
        return jsonify({"error": f"Unknown action: {action}"}), 400

    try:
        import docker
        client = docker.from_env()
        container = client.containers.get("rpie-streamer")

        if action == "start":
            container.start()
        elif action == "stop":
            container.stop(timeout=15)
        elif action == "restart":
            container.restart(timeout=15)

        return jsonify({"status": "ok", "action": action})

    except ImportError:
        # Docker SDK not available — try subprocess fallback
        import subprocess
        try:
            subprocess.run(
                ["docker", action, "rpie-streamer"],
                check=True,
                capture_output=True,
                timeout=30,
            )
            return jsonify({"status": "ok", "action": action})
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            return jsonify({"error": str(e)}), 500

    except Exception as e:
        logger.error("Stream control error: %s", e)
        return jsonify({"error": str(e)}), 500


@api.route("/cameras/scan")
def scan_cameras():
    """Scan for ONVIF cameras on the local network."""
    try:
        cameras = discover_cameras(timeout=5.0)
        return jsonify({"cameras": cameras})
    except Exception as e:
        logger.error("Camera scan error: %s", e)
        return jsonify({"cameras": [], "error": str(e)})


@api.route("/camera/probe")
def probe_camera():
    """Probe a camera's RTSP stream for codec info."""
    url = request.args.get("url", "")
    if not url:
        return jsonify({"error": "Missing 'url' parameter"}), 400

    try:
        info = probe_stream(url, timeout=10)
        return jsonify({
            "video_codec": info.video_codec,
            "audio_codec": info.audio_codec,
            "resolution": info.resolution,
            "framerate": info.framerate,
            "can_copy": info.can_copy,
        })
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 400


@api.route("/camera/common-urls")
def common_urls():
    """Get common RTSP URL patterns to try for a given IP."""
    ip = request.args.get("ip", "")
    username = request.args.get("username", "")
    password = request.args.get("password", "")
    if not ip:
        return jsonify({"error": "Missing 'ip' parameter"}), 400

    urls = get_common_rtsp_urls(ip, username, password)
    return jsonify({"urls": urls})


@api.route("/camera/detect-channels", methods=["POST"])
def detect_channels():
    """Probe an NVR/camera to find active channels and their stream info.

    Accepts JSON body: {ip, username?, password?, hardware?, name?, scopes?}
    Returns: {brand, channels: [{channel, quality, url, video_codec, resolution, ...}]}
    """
    data = request.get_json(silent=True) or {}
    ip = data.get("ip", "").strip()
    if not ip:
        return jsonify({"error": "Missing 'ip' field"}), 400

    username = data.get("username", "")
    password = data.get("password", "")
    hardware = data.get("hardware", "")
    name = data.get("name", "")
    scopes = data.get("scopes", "")

    brand = detect_brand(hardware, name, scopes)
    candidates = get_channel_urls(ip, brand, username, password)

    active_channels = []
    detected_brand = brand

    for candidate in candidates:
        try:
            info = probe_stream(candidate["url"], timeout=5)
            active_channels.append({
                "channel": candidate["channel"],
                "quality": candidate["quality"],
                "url": candidate["url"],
                "brand": candidate["brand"],
                "video_codec": info.video_codec,
                "audio_codec": info.audio_codec,
                "resolution": info.resolution,
                "framerate": info.framerate,
                "can_copy": info.can_copy,
            })
            # If we were guessing brands, lock in the one that worked
            if not detected_brand:
                detected_brand = candidate["brand"]
        except RuntimeError:
            continue

    # If we detected a brand from probing and didn't know it before,
    # now probe all channels for that brand
    if detected_brand and not brand and active_channels:
        remaining = get_channel_urls(ip, detected_brand, username, password)
        seen_urls = {ch["url"] for ch in active_channels}
        for candidate in remaining:
            if candidate["url"] in seen_urls:
                continue
            try:
                info = probe_stream(candidate["url"], timeout=5)
                active_channels.append({
                    "channel": candidate["channel"],
                    "quality": candidate["quality"],
                    "url": candidate["url"],
                    "brand": candidate["brand"],
                    "video_codec": info.video_codec,
                    "audio_codec": info.audio_codec,
                    "resolution": info.resolution,
                    "framerate": info.framerate,
                    "can_copy": info.can_copy,
                })
            except RuntimeError:
                continue

    # Sort: main streams first, then by channel number
    active_channels.sort(key=lambda c: (c["channel"], c["quality"] != "main"))

    return jsonify({
        "brand": detected_brand,
        "channels": active_channels,
    })


def _redact_log_lines(lines):
    """Remove credentials and stream keys from log output."""
    patterns = [
        (re.compile(r"(rtsp://[^:]+:)[^@]+(@)"), r"\1***\2"),
        (re.compile(r"(rtmps?://[^\s]+/live/)\S+"), r"\1***"),
    ]
    result = []
    for line in lines:
        for pattern, replacement in patterns:
            line = pattern.sub(replacement, line)
        result.append(line)
    return result


@api.route("/backend/test", methods=["POST"])
def test_backend():
    """Test connectivity to the Fishcamz backend."""
    import requests as http_requests

    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip().rstrip("/")

    if not url:
        return jsonify({"ok": False, "error": "No URL provided"})

    try:
        resp = http_requests.get(f"{url}/api/vessels", timeout=10)
        if resp.status_code == 200:
            return jsonify({"ok": True})
        else:
            return jsonify({"ok": False, "error": f"HTTP {resp.status_code}"})
    except http_requests.ConnectionError:
        return jsonify({"ok": False, "error": "Could not connect. Check the URL and network."})
    except http_requests.Timeout:
        return jsonify({"ok": False, "error": "Connection timed out."})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@api.route("/logs")
def get_logs():
    """Get recent log lines from the streamer container."""
    lines = int(request.args.get("lines", 100))
    lines = min(lines, 500)

    try:
        import docker
        client = docker.from_env()
        container = client.containers.get("rpie-streamer")
        log_output = container.logs(tail=lines, timestamps=True).decode("utf-8", errors="replace")
        return jsonify({"logs": _redact_log_lines(log_output.splitlines())})
    except Exception:
        # Fallback: try reading from Docker CLI
        import subprocess
        try:
            result = subprocess.run(
                ["docker", "logs", "--tail", str(lines), "--timestamps", "rpie-streamer"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            output = result.stdout + result.stderr
            return jsonify({"logs": _redact_log_lines(output.splitlines())})
        except Exception as e:
            return jsonify({"logs": [], "error": str(e)})
