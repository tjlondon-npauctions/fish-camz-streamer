"""Network connectivity monitor for detecting outages and measuring latency."""

from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)


class NetworkMonitor:
    """Background thread that checks network connectivity periodically."""

    def __init__(
        self,
        check_host: str = "1.1.1.1",
        check_interval: int = 30,
        outage_threshold: int = 60,
        state_dir: str = "/run/rpie",
    ):
        self.check_host = check_host
        self.check_interval = check_interval
        self.outage_threshold = outage_threshold
        self._state_file = Path(state_dir) / "network.json"

        self._stop_event = threading.Event()
        self._thread = None

        # State
        self._connected = True
        self._latency_ms = 0.0
        self._last_check = 0.0
        self._outage_start = None
        self._on_recovery_callback = None

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def in_extended_outage(self) -> bool:
        if self._outage_start is None:
            return False
        return (time.time() - self._outage_start) > self.outage_threshold

    def on_recovery(self, callback) -> None:
        """Register a callback to be called when network recovers from outage."""
        self._on_recovery_callback = callback

    def start(self) -> None:
        """Start the background monitoring thread."""
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        logger.info("Network monitor started (checking %s every %ds)", self.check_host, self.check_interval)

    def stop(self) -> None:
        """Stop the monitoring thread."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)

    def check_now(self) -> bool:
        """Run an immediate connectivity check."""
        return self._do_check()

    def get_status(self) -> dict:
        return {
            "connected": self._connected,
            "latency_ms": round(self._latency_ms, 1),
            "last_check": self._last_check,
            "in_extended_outage": self.in_extended_outage,
        }

    def _run(self) -> None:
        """Main monitoring loop."""
        while not self._stop_event.is_set():
            self._do_check()
            self._write_state()
            self._stop_event.wait(self.check_interval)

    def _do_check(self) -> bool:
        """Ping the check host and update state."""
        self._last_check = time.time()
        was_connected = self._connected

        try:
            result = subprocess.run(
                ["ping", "-c", "1", "-W", "5", self.check_host],
                capture_output=True,
                text=True,
                timeout=10,
            )

            if result.returncode == 0:
                self._connected = True
                self._latency_ms = self._parse_ping_latency(result.stdout)

                if not was_connected:
                    was_extended = self.in_extended_outage
                    logger.info("Network recovered (latency: %.1fms)", self._latency_ms)
                    self._outage_start = None
                    if was_extended and self._on_recovery_callback:
                        logger.info("Extended outage ended, restarting stream")
                        self._on_recovery_callback()
            else:
                self._mark_disconnected()

        except (subprocess.TimeoutExpired, OSError):
            self._mark_disconnected()

        return self._connected

    def _mark_disconnected(self) -> None:
        if self._connected:
            logger.warning("Network connectivity lost to %s", self.check_host)
            self._outage_start = time.time()
        self._connected = False
        self._latency_ms = 0.0

    def _parse_ping_latency(self, output: str) -> float:
        """Extract RTT from ping output."""
        for line in output.splitlines():
            if "time=" in line:
                try:
                    time_part = line.split("time=")[1]
                    return float(time_part.split()[0])
                except (IndexError, ValueError):
                    pass
        return 0.0

    def _write_state(self) -> None:
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self._state_file, "w") as f:
                json.dump(self.get_status(), f)
        except OSError as e:
            logger.debug("Could not write network state: %s", e)
