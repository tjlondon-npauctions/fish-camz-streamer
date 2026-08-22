"""Background heartbeat to the Fishcamz backend."""

from __future__ import annotations

import hashlib
import json
import logging
import socket
import threading
import time
from pathlib import Path
from typing import Optional

from app.config import manager
from app.system.stats import get_system_stats
from app.updater import Updater, get_status as get_update_status

logger = logging.getLogger(__name__)

VERSION_FILE = Path(__file__).resolve().parent.parent / "VERSION"
MACHINE_ID_FILE = Path("/etc/machine-id")


def _get_version() -> str:
    try:
        return VERSION_FILE.read_text().strip()
    except OSError:
        return "unknown"


_DEVICE_ID_CACHE: Optional[str] = None


def _get_device_id() -> str:
    """Return a stable, hashed device identifier derived from /etc/machine-id.

    The raw machine-id is hashed (SHA-256, first 16 hex chars) so it isn't
    sent in the clear. Cached after first call. Returns an empty string if the
    file is unreadable so the backend can fall back to registrationToken.
    """
    global _DEVICE_ID_CACHE
    if _DEVICE_ID_CACHE is not None:
        return _DEVICE_ID_CACHE
    try:
        raw = MACHINE_ID_FILE.read_text().strip()
        if raw:
            _DEVICE_ID_CACHE = hashlib.sha256(raw.encode("ascii")).hexdigest()[:16]
            return _DEVICE_ID_CACHE
    except OSError:
        pass
    _DEVICE_ID_CACHE = ""
    return ""


def build_payload(config: dict) -> dict:
    """Assemble the heartbeat body from config plus the state files on tmpfs.

    Shared with the settings page's "Test connection" button so the test
    exercises the real endpoint with the real payload — a test that posts a
    stand-in body can pass while the live heartbeat fails, or mark a healthy
    vessel offline by omitting stream_health.
    """
    state_dir = manager.get(config, "system", "state_dir", "/run/rpie")
    update_state = get_update_status()
    return {
        "vessel_name": config.get("vessel", {}).get("name", ""),
        "device_id": _get_device_id(),
        # Hostname is the operator-set Pi name (e.g. "whale-pi.local")
        # — handy for the admin UI when no friendly name is set yet.
        "device_name": socket.gethostname(),
        "version": _get_version(),
        "update_status": update_state.get("status", "idle"),
        "update_error": update_state.get("error", ""),
        "stream_health": _read_state_file(state_dir, "state.json"),
        "system_stats": get_system_stats(),
        "uploader": _read_state_file(state_dir, "uploader.json"),
        "network": _read_state_file(state_dir, "network.json"),
        "gps": _read_state_file(state_dir, "gps.json"),
        "bunny_stream_path": config.get("bunny", {}).get("stream_path", "live"),
        "bunny_cdn_url": config.get("bunny", {}).get("cdn_url", ""),
        "output_mode": config.get("output", {}).get("mode", "rtmp"),
        "tunnel_url": config.get("remote_access", {}).get("tunnel_url", ""),
        "timestamp": time.time(),
    }


def _read_state_file(state_dir: str, filename: str) -> dict:
    """Read a JSON state file from tmpfs."""
    state_file = Path(state_dir) / filename
    try:
        if state_file.exists():
            with open(state_file) as f:
                return json.load(f)
    except (json.JSONDecodeError, OSError):
        pass
    return {}


class HeartbeatSender:
    """Sends periodic heartbeat to the Fishcamz backend."""

    def __init__(self, config: dict):
        self.config = config
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_error = ""
        self._send_count = 0
        self._error_count = 0
        self._updater = Updater(current_version=_get_version())

    def start(self) -> None:
        """Start the heartbeat background thread."""
        backend_cfg = self.config.get("backend", {})
        url = backend_cfg.get("url", "")
        if not url:
            logger.info("Backend URL not configured — heartbeat disabled")
            return

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        logger.info("Heartbeat started (target: %s)", url)

    def stop(self) -> None:
        """Stop the heartbeat thread."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10)
        logger.info("Heartbeat stopped")

    def _apply_tunnel_token(self, token: str) -> None:
        """Persist a new Cloudflare Tunnel token from the backend and (re)start the tunnel.

        The backend delivers a token in the heartbeat response when an admin stages
        one in vessels_private/{vesselId}.pendingTunnelToken. Idempotent: if the
        token is already the current one, nothing happens.
        """
        config = manager.load()
        current = manager.get(config, "remote_access", "tunnel_token", "")
        if current == token:
            return

        manager.set_value(config, "remote_access", "tunnel_token", token)
        manager.set_value(config, "remote_access", "enabled", True)
        manager.save(config)
        logger.info("Applied new tunnel token from backend (len=%d)", len(token))

        try:
            from app.web.routes import _start_tunnel
            _start_tunnel(token)
        except Exception as e:
            logger.warning("Could not (re)start tunnel after token update: %s", e)

    def _apply_stream_command(self, command: str, state_dir: str) -> None:
        """Act on a start/stop command the backend returns in the heartbeat.

        The cloud drives dock-aware auto-streaming: when a vessel leaves its
        dock the backend returns ``stream_command: "start"`` (and ``"stop"`` on
        return). Gated by ``stream.allow_remote_control`` so an operator can veto
        it locally, and idempotent against the current streamer state.
        """
        if command not in ("start", "stop"):
            return

        config = manager.load()
        if not manager.get(config, "stream", "allow_remote_control", True):
            logger.info("Ignoring stream_command=%s — remote control disabled", command)
            return

        # Idempotency guard: no-op if the streamer is already in the desired state.
        state = _read_state_file(state_dir, "state.json")
        running = bool(state.get("running"))
        if running == (command == "start"):
            return

        from app.streaming import container_control

        result = container_control.set_stream(command)
        if result.get("ok"):
            logger.info("Applied stream_command=%s from backend", command)
        else:
            logger.warning("stream_command=%s failed: %s", command, result.get("error"))

    def _run(self) -> None:
        """Main heartbeat loop."""
        import requests

        interval = 60
        while not self._stop_event.is_set():
            try:
                # Re-read config from disk each tick so settings changes take
                # effect without restarting the service.
                config = manager.load()
                backend_cfg = config.get("backend", {})
                url = backend_cfg.get("url", "").rstrip("/")
                api_key = backend_cfg.get("vessel_api_key", "")
                interval = backend_cfg.get("heartbeat_interval", 60)
                state_dir = manager.get(config, "system", "state_dir", "/run/rpie")

                endpoint = f"{url}/api/vessels/heartbeat"

                payload = build_payload(config)

                headers = {"Content-Type": "application/json"}
                if api_key:
                    headers["X-Vessel-Key"] = api_key

                resp = requests.post(endpoint, json=payload, headers=headers, timeout=15)

                if resp.status_code in (200, 201):
                    self._send_count += 1
                    logger.debug("Heartbeat sent (#%d)", self._send_count)
                    try:
                        data = resp.json()
                    except ValueError:
                        data = None
                    if isinstance(data, dict):
                        new_token = data.get("tunnel_token")
                        if isinstance(new_token, str) and new_token:
                            self._apply_tunnel_token(new_token)

                        # Phase B — controlled update via target_version.
                        # If the cloud has set one, hand off to the updater.
                        # The updater is idempotent and no-ops when target == current.
                        target_version = data.get("target_version")
                        registry = data.get("registry")
                        if isinstance(target_version, str) and target_version:
                            try:
                                self._updater.maybe_update(target_version, registry)
                            except Exception as e:
                                logger.warning("Updater dispatch error: %s", e)

                        # Dock-aware auto-streaming — start/stop on cloud command.
                        stream_command = data.get("stream_command")
                        if isinstance(stream_command, str) and stream_command:
                            try:
                                self._apply_stream_command(stream_command, state_dir)
                            except Exception as e:
                                logger.warning("Stream command dispatch error: %s", e)
                else:
                    self._last_error = f"HTTP {resp.status_code}"
                    self._error_count += 1
                    logger.warning("Heartbeat failed: HTTP %d", resp.status_code)

            except Exception as e:
                self._last_error = str(e)
                self._error_count += 1
                logger.warning("Heartbeat error: %s", e)

            self._stop_event.wait(interval)
