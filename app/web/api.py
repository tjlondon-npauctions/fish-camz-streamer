"""JSON API endpoints for the dashboard AJAX calls and stream control."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from flask import Blueprint, jsonify, request

from app.camera.discovery import discover_cameras, get_common_rtsp_urls
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
    state = _read_state_file("state.json")
    if not state:
        return jsonify({
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
        })
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
