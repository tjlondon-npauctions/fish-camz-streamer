"""GPS reader using gpsd for position, speed, and heading data."""

from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class GpsReader:
    """Reads GPS data from a USB GPS dongle via gpsd's JSON interface.

    Uses gpspipe (part of gpsd-clients) to get JSON output from gpsd,
    avoiding the need for the gpsd Python bindings which can be
    difficult to install in Docker containers.
    """

    def __init__(
        self,
        gpsd_host: str = "localhost",
        gpsd_port: int = 2947,
        poll_interval: int = 5,
        state_dir: str = "/run/rpie",
    ):
        self._host = gpsd_host
        self._port = gpsd_port
        self._poll_interval = poll_interval
        self._state_file = Path(state_dir) / "gps.json"

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

        # Current position
        self._lat: Optional[float] = None
        self._lng: Optional[float] = None
        self._speed_knots: Optional[float] = None
        self._heading: Optional[float] = None
        self._altitude: Optional[float] = None
        self._fix_time: Optional[str] = None
        self._has_fix = False
        self._last_update: float = 0
        self._error = ""

    def start(self) -> None:
        """Start the background GPS reading thread."""
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        logger.info("GPS reader started (gpsd at %s:%d)", self._host, self._port)

    def stop(self) -> None:
        """Stop the GPS reading thread."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10)
        logger.info("GPS reader stopped")

    def get_position(self) -> Optional[dict]:
        """Return current GPS position, or None if no fix."""
        with self._lock:
            if not self._has_fix:
                return None
            return {
                "lat": self._lat,
                "lng": self._lng,
                "speed_knots": self._speed_knots,
                "heading": self._heading,
                "altitude": self._altitude,
                "fix_time": self._fix_time,
                "age_seconds": time.time() - self._last_update if self._last_update else None,
            }

    def get_status(self) -> dict:
        """Return GPS status for the state file and API."""
        with self._lock:
            return {
                "has_fix": self._has_fix,
                "lat": self._lat,
                "lng": self._lng,
                "speed_knots": self._speed_knots,
                "heading": self._heading,
                "altitude": self._altitude,
                "fix_time": self._fix_time,
                "last_update": self._last_update,
                "error": self._error,
            }

    def _run(self) -> None:
        """Main GPS reading loop."""
        while not self._stop_event.is_set():
            try:
                self._poll_gpsd()
            except Exception as e:
                with self._lock:
                    self._error = str(e)
                logger.warning("GPS read error: %s", e)

            self._write_state()
            self._stop_event.wait(self._poll_interval)

    def _poll_gpsd(self) -> None:
        """Poll gpsd for a single position fix using gpspipe."""
        try:
            # Use gpspipe to get one JSON TPV (Time-Position-Velocity) report
            # -w = JSON output, -n 5 = read 5 sentences (enough to get a TPV)
            result = subprocess.run(
                ["gpspipe", "-w", "-n", "10", f"{self._host}:{self._port}"],
                capture_output=True,
                text=True,
                timeout=10,
            )

            if result.returncode != 0:
                # gpspipe might not be available — try direct socket
                self._poll_gpsd_socket()
                return

            # Parse JSON lines looking for TPV (position) reports
            for line in result.stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    if data.get("class") == "TPV":
                        self._update_from_tpv(data)
                        return
                except json.JSONDecodeError:
                    continue

        except FileNotFoundError:
            # gpspipe not installed — try direct socket approach
            self._poll_gpsd_socket()
        except subprocess.TimeoutExpired:
            with self._lock:
                self._error = "gpsd timeout"

    def _poll_gpsd_socket(self) -> None:
        """Fallback: connect directly to gpsd socket for JSON data."""
        import socket

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5)
        try:
            sock.connect((self._host, self._port))
            # Send WATCH command to start JSON streaming
            sock.send(b'?WATCH={"enable":true,"json":true}\n')

            # Read responses looking for TPV
            buffer = b""
            deadline = time.time() + 5
            while time.time() < deadline:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buffer += chunk
                for line in buffer.split(b"\n"):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        if data.get("class") == "TPV":
                            self._update_from_tpv(data)
                            return
                    except json.JSONDecodeError:
                        continue
                # Keep only the last incomplete line in buffer
                buffer = buffer.rsplit(b"\n", 1)[-1]
        except (socket.error, OSError) as e:
            with self._lock:
                self._error = f"gpsd connection failed: {e}"
        finally:
            sock.close()

    def _update_from_tpv(self, tpv: dict) -> None:
        """Update position from a gpsd TPV report."""
        with self._lock:
            mode = tpv.get("mode", 0)
            # mode: 0=unknown, 1=no fix, 2=2D fix, 3=3D fix
            if mode >= 2 and "lat" in tpv and "lon" in tpv:
                self._lat = tpv["lat"]
                self._lng = tpv["lon"]
                try:
                    self._speed_knots = float(tpv["speed"]) * 1.94384 if "speed" in tpv else None
                except (TypeError, ValueError):
                    self._speed_knots = None
                self._heading = tpv.get("track")
                self._altitude = tpv.get("altHAE") or tpv.get("alt")
                self._fix_time = tpv.get("time")
                self._has_fix = True
                self._last_update = time.time()
                self._error = ""
                logger.debug("GPS fix: %.6f, %.6f (%.1f kn, heading %.0f)",
                             self._lat, self._lng,
                             self._speed_knots or 0, self._heading or 0)
            else:
                self._has_fix = False
                self._error = "No GPS fix"

    def _write_state(self) -> None:
        """Write GPS state to tmpfs for the web UI."""
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self._state_file, "w") as f:
                json.dump(self.get_status(), f)
        except OSError:
            pass
