"""FFmpeg subprocess lifecycle manager with auto-restart and health monitoring."""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

from app.camera.probe import StreamInfo, probe_stream
from app.config import manager
from app.streaming.ffmpeg_builder import build_command, _build_rtsp_url
from app.streaming.health import HealthMonitor, HealthSnapshot
from app.streaming.playlist import parse_playlist
from app.streaming.uploader import HLSUploader

logger = logging.getLogger(__name__)

# Patterns to redact from log output
_REDACT_PATTERNS = [
    re.compile(r"(rtsp://[^:]+:)[^@]+(@)"),         # rtsp://user:PASSWORD@
    re.compile(r"(rtmps?://[^\s]+/live/)\S+"),       # rtmps://.../live/STREAMKEY
]


def _redact(text: str) -> str:
    """Redact credentials and stream keys from text for safe logging."""
    text = _REDACT_PATTERNS[0].sub(r"\1***\2", text)
    text = _REDACT_PATTERNS[1].sub(r"\1***", text)
    return text


def _stream_info_from_config(cam: dict) -> Optional[StreamInfo]:
    """Reconstruct a StreamInfo from the persisted camera.last_probe summary.

    Only returns a value when the cached summary was captured for the current
    RTSP URL and reported a copyable (H.264) source. Used as a fallback so a
    transient probe failure doesn't downgrade a known-copyable camera to
    transcode mode.
    """
    lp = cam.get("last_probe") or {}
    if not lp or lp.get("url") != cam.get("rtsp_url") or not lp.get("can_copy"):
        return None

    width = height = 0
    res = str(lp.get("resolution", ""))
    if "x" in res:
        try:
            width, height = (int(part) for part in res.split("x", 1))
        except ValueError:
            width = height = 0
    codec = str(lp.get("video_codec", ""))
    try:
        framerate = float(lp.get("framerate", 0) or 0)
    except (TypeError, ValueError):
        framerate = 0.0

    return StreamInfo(
        video_codec=codec,
        framerate=framerate,
        width=width,
        height=height,
        is_h264=(codec.lower() == "h264"),
        can_copy=True,
    )


class StreamEngine:
    """Manages an FFmpeg streaming subprocess with auto-restart."""

    def __init__(self, config: dict):
        self.config = config
        self._process: Optional[subprocess.Popen] = None
        self._health = HealthMonitor(
            stall_timeout=manager.get(config, "stream", "stall_timeout", 30)
        )
        self._stdout_thread: Optional[threading.Thread] = None
        self._stderr_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

        # HLS uploader (started when output mode includes HLS)
        self._uploader: Optional[HLSUploader] = None

        # State
        self._running = False
        self._start_time: Optional[float] = None
        self._restart_count = 0
        self._last_error = ""
        # Last successful camera probe this session — reused as a fallback so a
        # transient probe failure on restart doesn't downgrade copy → transcode.
        self._last_probe: Optional[StreamInfo] = None
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

            # Ensure HLS segment directory exists and start uploader if needed
            output_mode = self.config.get("output", {}).get("mode", "rtmp")
            if output_mode in ("hls", "both"):
                hls_dir = self.config.get("hls", {}).get("segment_dir", "/run/rpie/hls")
                Path(hls_dir).mkdir(parents=True, exist_ok=True)

                # Tear down any leftover uploader from a previous start() —
                # run_with_auto_restart() calls start() after each FFmpeg
                # crash without calling stop(), so the prior uploader thread
                # would otherwise leak and keep PUTting every segment to Bunny.
                if self._uploader is not None:
                    logger.info("Stopping leftover HLS uploader before restart")
                    self._uploader.stop()
                    self._uploader = None

                # Start HLS uploader if Bunny CDN is configured
                bunny_cfg = self.config.get("bunny", {})
                if bunny_cfg.get("storage_zone") and bunny_cfg.get("api_key"):
                    state_dir = manager.get(self.config, "system", "state_dir", "/run/rpie")
                    hls_cfg = self.config.get("hls", {})
                    self._uploader = HLSUploader(
                        segment_dir=hls_dir,
                        storage_zone=bunny_cfg["storage_zone"],
                        api_key=bunny_cfg["api_key"],
                        region=bunny_cfg.get("region", ""),
                        stream_path=bunny_cfg.get("stream_path", "live"),
                        state_dir=state_dir,
                        buffer_segments=hls_cfg.get("buffer_segments", 150),
                        max_unsent_segments=hls_cfg.get("max_unsent_segments", 1000),
                        segment_duration=hls_cfg.get("segment_duration", 6),
                        published_playlist_size=hls_cfg.get("published_playlist_size", 10),
                        max_disk_bytes=hls_cfg.get("max_disk_bytes", 2147483648),
                        min_free_bytes=hls_cfg.get("min_free_bytes", 1073741824),
                        live_batch=hls_cfg.get("live_batch", 2),
                        live_catch_up=hls_cfg.get("live_catch_up", 6),
                        live_deadline=hls_cfg.get("live_deadline", 30),
                        backfill_min_interval=hls_cfg.get("backfill_min_interval", 120),
                        backfill_suspend_backlog=hls_cfg.get("backfill_suspend_backlog", 900),
                        index_upload_interval=hls_cfg.get("index_upload_interval", 180),
                        state_persist_interval=hls_cfg.get("state_persist_interval", 30),
                        max_publish_age=hls_cfg.get("max_publish_age", 600),
                    )
                    self._uploader.start()
                else:
                    logger.warning("HLS mode enabled but Bunny CDN not configured — segments will be local only")

            # Generate a session ID for unique segment filenames
            # This prevents CDN collisions when the stream restarts
            session_id = str(int(time.time()) % 100000)
            self.config.setdefault("hls", {})["session_id"] = session_id

            # Probe camera to determine codec strategy
            probe = self._probe_camera()

            # Build FFmpeg command
            cmd = build_command(self.config, probe)
            logger.info("Starting FFmpeg: %s", _redact(" ".join(cmd)))

            try:
                self._process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
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

            # Read progress from stdout (-progress pipe:1)
            self._stdout_thread = threading.Thread(
                target=self._read_progress,
                daemon=True,
            )
            self._stdout_thread.start()

            # Read errors from stderr
            self._stderr_thread = threading.Thread(
                target=self._read_stderr,
                daemon=True,
            )
            self._stderr_thread.start()

            logger.info("FFmpeg started (PID %d)", self._process.pid)
            self._write_state()

    def stop(self) -> None:
        """Stop the FFmpeg process gracefully.

        Note: we deliberately do NOT delete the live.m3u8 from Bunny here.
        Every restart path (network recovery, settings change, controlled OTA
        update, container restart) goes through this method, and deleting the
        playlist creates a 10–30s 404 window during which viewers see "This
        camera is not currently streaming" — even though the stream is just
        being recreated under the same hostname. The cloud-side player
        already gates playback on heartbeat freshness (`isPlayable = isLive
        && fresh heartbeat`), so a Pi that's actually gone surfaces as
        offline within ~90s without us having to break the playlist.

        If we ever need a "fully decommission" action, call uploader.cleanup()
        explicitly from that admin path, not from generic stop().
        """
        if self._uploader:
            logger.info("Stopping HLS uploader...")
            self._uploader.stop()
            self._uploader = None

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

        # Clean up reader threads to prevent file descriptor leaks
        for thread in (self._stdout_thread, self._stderr_thread):
            if thread and thread.is_alive():
                thread.join(timeout=5)

        # Remove the finalized playlist (contains EXT-X-ENDLIST)
        # so the next session starts clean and the CDN doesn't serve
        # a stale VOD playlist to viewers
        hls_dir = self.config.get("hls", {}).get("segment_dir", "/run/rpie/hls")
        playlist_path = Path(hls_dir) / "live.m3u8"
        try:
            if playlist_path.exists():
                playlist_path.unlink()
                logger.info("Removed finalized playlist")
        except OSError:
            pass

        with self._lock:
            self._running = False
            self._process = None
            self._stdout_thread = None
            self._stderr_thread = None
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

        bitrate_kbps = health.bitrate_kbps
        output_mode = self.config.get("output", {}).get("mode", "rtmp")
        if output_mode in ("hls", "both") and bitrate_kbps <= 0:
            # FFmpeg reports bitrate=N/A for HLS muxer output, so estimate
            # from recent segment sizes and their EXTINF durations.
            bitrate_kbps = self._compute_hls_bitrate()

        return {
            "running": self.is_running(),
            "uptime_seconds": round(uptime),
            "restart_count": self._restart_count,
            "last_error": self._last_error,
            "fps": health.fps,
            "bitrate_kbps": bitrate_kbps,
            "speed": health.speed,
            "frame_count": health.frame_count,
            "is_stalled": health.is_stalled,
            "is_slow": health.is_slow,
            "pid": self._process.pid if self._process else None,
            "uploader": self._uploader.get_status() if self._uploader else None,
        }

    def _compute_hls_bitrate(self, sample_size: int = 5) -> float:
        """Estimate output bitrate from the most recent HLS segments.

        Reads the live playlist, pairs the last ``sample_size`` segment
        filenames with their EXTINF durations, and divides total bytes
        by total seconds. Returns 0.0 if the playlist isn't readable.
        """
        hls_dir = Path(self.config.get("hls", {}).get("segment_dir", "/run/rpie/hls"))
        playlist = hls_dir / "live.m3u8"

        try:
            lines = playlist.read_text().splitlines()
        except OSError:
            return 0.0

        segments = parse_playlist("\n".join(lines))

        # Drop the most recent entry — it may still be open for write.
        recent = segments[-(sample_size + 1):-1] if len(segments) > sample_size else segments[:-1]
        if not recent:
            return 0.0

        total_bytes = 0
        total_seconds = 0.0
        for entry in recent:
            try:
                total_bytes += (hls_dir / entry.name).stat().st_size
            except OSError:
                continue
            total_seconds += entry.duration

        if total_seconds <= 0:
            return 0.0
        return (total_bytes * 8.0) / (total_seconds * 1000.0)

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
        """Probe the camera, returning None only when we genuinely can't
        determine a copyable source.

        On a restart the camera may be briefly unreachable (e.g. a nightly
        reboot). A single failed probe would force a needless fall back to
        transcode mode — burning CPU to re-encode an H.264 source that's
        actually copyable, and staying that way until the next restart. So we
        retry the live probe a few times to let the camera come back, then fall
        back to the last successful probe (this session, or the persisted
        camera.last_probe from setup) to stay in copy mode.
        """
        cam = self.config.get("camera", {})
        rtsp_url = _build_rtsp_url(cam)
        if not rtsp_url:
            return None

        attempts = manager.get(self.config, "camera", "probe_retries", 3)
        delay = manager.get(self.config, "camera", "probe_retry_delay", 5)
        last_err: Optional[Exception] = None

        for attempt in range(1, attempts + 1):
            if self._stop_event.is_set():
                return None
            try:
                info = probe_stream(rtsp_url)
                logger.info(
                    "Camera probe: %s %s @ %s %.0ffps (can_copy=%s)",
                    info.video_codec, info.audio_codec,
                    info.resolution, info.framerate, info.can_copy,
                )
                self._last_probe = info
                return info
            except RuntimeError as e:
                last_err = e
                if attempt < attempts:
                    logger.warning(
                        "Camera probe failed (attempt %d/%d): %s — retrying in %ds",
                        attempt, attempts, e, delay,
                    )
                    self._stop_event.wait(delay)

        # Retries exhausted — reuse the last known-good probe rather than
        # downgrading a source we know is copyable to transcode.
        fallback = self._last_probe or _stream_info_from_config(cam)
        if fallback is not None and fallback.can_copy:
            logger.warning(
                "Camera probe failed after %d attempts (%s) — reusing last "
                "successful probe to stay in copy mode",
                attempts, last_err,
            )
            return fallback

        logger.warning(
            "Camera probe failed after %d attempts: %s (will use transcode mode)",
            attempts, last_err,
        )
        return None

    def _read_progress(self) -> None:
        """Background thread: read FFmpeg -progress output from stdout."""
        # FFmpeg emits ~12 progress lines per second (one block per second).
        # Throttle state writes to 1 Hz so we don't recompute HLS bitrate
        # (which reads the playlist + stats segment files) on every line.
        last_write = 0.0
        try:
            for line in self._process.stdout:
                if self._stop_event.is_set():
                    break
                line = line.strip()
                if line:
                    self._health.parse_line(line)
                    now = time.monotonic()
                    if now - last_write >= 1.0:
                        self._write_state()
                        last_write = now
        except (ValueError, OSError):
            pass  # Process closed — expected on stop
        except Exception as e:
            logger.error("FFmpeg stdout reader crashed: %s", e)
            self._last_error = f"Health monitor crashed: {e}"

    def _read_stderr(self) -> None:
        """Background thread: read FFmpeg stderr for errors."""
        early_lines = []
        try:
            for line in self._process.stderr:
                if self._stop_event.is_set():
                    break
                line = line.strip()
                if not line:
                    continue

                # Capture early output for crash diagnostics
                if len(early_lines) < 50:
                    early_lines.append(line)

                # Log warnings/errors from FFmpeg
                if any(lvl in line.lower() for lvl in ("error", "fatal", "invalid", "unknown")):
                    logger.error("FFmpeg: %s", line)
                    self._last_error = line
        except (ValueError, OSError):
            pass  # Process closed — expected on stop
        except Exception as e:
            logger.error("FFmpeg stderr reader crashed: %s", e)

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
