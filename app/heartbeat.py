"""Background heartbeat to the Fishcamz backend."""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Optional

from app.config import manager
from app.system.stats import get_system_stats

logger = logging.getLogger(__name__)

VERSION_FILE = Path(__file__).resolve().parent.parent / "VERSION"


def _get_version() -> str:
    try:
        return VERSION_FILE.read_text().strip()
    except OSError:
        return "unknown"


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

                payload = {
                    "vessel_name": config.get("vessel", {}).get("name", ""),
                    "version": _get_version(),
                    "stream_health": _read_state_file(state_dir, "state.json"),
                    "system_stats": get_system_stats(),
                    "uploader": _read_state_file(state_dir, "uploader.json"),
                    "network": _read_state_file(state_dir, "network.json"),
                    "gps": _read_state_file(state_dir, "gps.json"),
                    "bunny_stream_path": config.get("bunny", {}).get("stream_path", "live"),
                    "bunny_cdn_url": config.get("bunny", {}).get("cdn_url", ""),
                    "output_mode": config.get("output", {}).get("mode", "rtmp"),
                    "timestamp": time.time(),
                }

                headers = {"Content-Type": "application/json"}
                if api_key:
                    headers["X-Vessel-Key"] = api_key

                resp = requests.post(endpoint, json=payload, headers=headers, timeout=15)

                if resp.status_code in (200, 201):
                    self._send_count += 1
                    logger.debug("Heartbeat sent (#%d)", self._send_count)
                else:
                    self._last_error = f"HTTP {resp.status_code}"
                    self._error_count += 1
                    logger.warning("Heartbeat failed: HTTP %d", resp.status_code)

            except Exception as e:
                self._last_error = str(e)
                self._error_count += 1
                logger.warning("Heartbeat error: %s", e)

            self._stop_event.wait(interval)
