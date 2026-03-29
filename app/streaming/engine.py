"""FFmpeg subprocess lifecycle manager with auto-restart and health monitoring."""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

from app.camera.probe import StreamInfo, probe_stream
from app.config import manager
from app.streaming.ffmpeg_builder import build_command
from app.streaming.health import HealthMonitor, HealthSnapshot

logger = logging.getLogger(__name__)


class StreamEngine:
    """Manages an FFmpeg streaming subprocess with auto-restart."""

    def __init__(self, config: dict):
        self.config = config
        self._process: Optional[subprocess.Popen] = None
        self._health = HealthMonitor(
            stall_timeout=manager.get(config, "stream", "stall_timeout", 30)
        )
        self._stderr_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

        # State
        self._running = False
        self._start_time: Optional[float] = None
        self._restart_count = 0
        self._last_error = ""
        self._current_backoff = manager.get(config, "stream", "restart_delay", 5)
        self._stable_since: Optional[float] = None

        # State file location
        state_dir = manager.get(config, "system", "state_dir", "/run/rpie")
        self._state_file = Path(state_dir) / "state.json"

    def start(self) -> None:
        """Start the FFmpeg streaming process."""
        with self._lock:
            if self._running:
                logger.warning("Stream already running")
                return

            self._stop_event.clear()

            # Probe camera to determine codec strategy
            probe = self._probe_camera()

            # Build FFmpeg command
            cmd = build_command(self.config, probe)
            logger.info("Starting FFmpeg: %s", " ".join(cmd))

            try:
                self._process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1,
                )
            except FileNotFoundError:
                self._last_error = "FFmpeg not found. Is it installed?"
                logger.error(self._last_error)
                self._write_state()
                return

            self._running = True
            self._start_time = time.time()
            self._health.reset()

            # Start stderr reader thread
            self._stderr_thread = threading.Thread(
                target=self._read_stderr,
                daemon=True,
            )
            self._stderr_thread.start()

            logger.info("FFmpeg started (PID %d)", self._process.pid)
            self._write_state()

    def stop(self) -> None:
        """Stop the FFmpeg process gracefully."""
        with self._lock:
            self._stop_event.set()
            process = self._process
            if process is None or process.poll() is not None:
                self._running = False
                self._process = None
                self._write_state()
                return

        # Release lock before blocking on process termination
        logger.info("Stopping FFmpeg (PID %d)...", process.pid)
        process.send_signal(signal.SIGTERM)
        try:
            process.wait(timeout=10)
            logger.info("FFmpeg stopped gracefully")
        except subprocess.TimeoutExpired:
            logger.warning("FFmpeg didn't stop, sending SIGKILL")
            process.kill()
            process.wait(timeout=5)

        with self._lock:
            self._running = False
            self._process = None
            self._write_state()

    def restart(self) -> None:
        """Stop then start the stream."""
        self.stop()
        time.sleep(1)
        self.start()

    def is_running(self) -> bool:
        """Check if FFmpeg is currently running."""
        if self._process is None:
            return False
        return self._process.poll() is None

    def get_status(self) -> dict:
        """Return current stream status."""
        health = self._health.get_snapshot()
        uptime = time.time() - self._start_time if self._start_time and self._running else 0

        return {
            "running": self.is_running(),
            "uptime_seconds": round(uptime),
            "restart_count": self._restart_count,
            "last_error": self._last_error,
            "fps": health.fps,
            "bitrate_kbps": health.bitrate_kbps,
            "speed": health.speed,
            "frame_count": health.frame_count,
            "is_stalled": health.is_stalled,
            "is_slow": health.is_slow,
            "pid": self._process.pid if self._process else None,
        }

    def get_health(self) -> HealthSnapshot:
        """Return the latest health snapshot."""
        return self._health.get_snapshot()

    def run_with_auto_restart(self) -> None:
        """Main loop: run the stream with automatic restart on failure.

        This blocks until stop() is called from another thread.
        """
        base_delay = manager.get(self.config, "stream", "restart_delay", 5)
        max_delay = manager.get(self.config, "stream", "max_restart_delay", 120)
        stable_threshold = manager.get(self.config, "stream", "stable_threshold", 60)

        self._current_backoff = base_delay

        while not self._stop_event.is_set():
            self.start()

            if not self.is_running():
                # Failed to start — wait and retry
                logger.error("Failed to start stream, retrying in %ds", self._current_backoff)
                self._stop_event.wait(self._current_backoff)
                self._current_backoff = min(self._current_backoff * 2, max_delay)
                continue

            # Wait for process to exit
            stable_start = time.time()
            while not self._stop_event.is_set() and self.is_running():
                self._stop_event.wait(1)

                # Reset backoff if stable long enough
                if time.time() - stable_start > stable_threshold:
                    if self._current_backoff != base_delay:
                        logger.info("Stream stable for %ds, resetting backoff", stable_threshold)
                        self._current_backoff = base_delay

            if self._stop_event.is_set():
                break

            # Process exited unexpectedly
            exit_code = self._process.returncode if self._process else -1
            self._last_error = f"FFmpeg exited with code {exit_code}"
            self._restart_count += 1
            self._running = False

            logger.warning(
                "FFmpeg exited (code %d), restart #%d in %ds",
                exit_code, self._restart_count, self._current_backoff,
            )
            self._write_state()

            self._stop_event.wait(self._current_backoff)
            self._current_backoff = min(self._current_backoff * 2, max_delay)

    def reload_config(self, config: dict) -> None:
        """Update config (called when settings change)."""
        self.config = config

    def _probe_camera(self) -> Optional[StreamInfo]:
        """Probe the camera stream, returning None on failure."""
        rtsp_url = manager.get(self.config, "camera", "rtsp_url", "")
        if not rtsp_url:
            return None

        try:
            info = probe_stream(rtsp_url)
            logger.info(
                "Camera probe: %s %s @ %s %.0ffps (can_copy=%s)",
                info.video_codec, info.audio_codec,
                info.resolution, info.framerate, info.can_copy,
            )
            return info
        except RuntimeError as e:
            logger.warning("Camera probe failed: %s (will use transcode mode)", e)
            return None

    def _read_stderr(self) -> None:
        """Background thread: read FFmpeg stderr and feed to health monitor."""
        early_lines = []
        try:
            for line in self._process.stderr:
                if self._stop_event.is_set():
                    break
                line = line.strip()
                if not line:
                    continue

                self._health.parse_line(line)

                # Capture early output for crash diagnostics
                if len(early_lines) < 50:
                    early_lines.append(line)

                # Log warnings/errors from FFmpeg
                if any(lvl in line.lower() for lvl in ("error", "fatal", "invalid", "unknown")):
                    logger.error("FFmpeg: %s", line)
                    self._last_error = line
        except (ValueError, OSError):
            pass  # Process closed

        # If FFmpeg exited quickly, dump all captured output for debugging
        if early_lines and self._process and self._process.poll() is not None:
            exit_code = self._process.returncode
            if exit_code != 0:
                logger.error("FFmpeg exited with code %d. Output:", exit_code)
                for line in early_lines:
                    logger.error("  %s", line)

    def _write_state(self) -> None:
        """Write current state to tmpfs for the web UI to read."""
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            state = self.get_status()
            state["timestamp"] = time.time()
            with open(self._state_file, "w") as f:
                json.dump(state, f)
        except OSError as e:
            logger.warning("Could not write state file: %s", e)
