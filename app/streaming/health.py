"""Parses FFmpeg progress output into structured health metrics.

Handles two FFmpeg output formats:
1. -progress pipe:2 — key=value pairs, one per line (preferred)
2. Single-line -stats format with \r carriage returns (fallback)
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Individual field patterns for single-line stats format
FRAME_RE = re.compile(r"frame=\s*(\d+)")
FPS_RE = re.compile(r"fps=\s*([\d.]+)")
BITRATE_RE = re.compile(r"bitrate=\s*([\d.]+)kbits/s")
SPEED_RE = re.compile(r"speed=\s*([\d.]+)x")
TIME_RE = re.compile(r"(?:out_time|time)=\s*(\d+):(\d+):([\d.]+)")


@dataclass
class HealthSnapshot:
    timestamp: float = 0.0
    frame_count: int = 0
    fps: float = 0.0
    bitrate_kbps: float = 0.0
    speed: float = 0.0
    elapsed_seconds: float = 0.0
    is_stalled: bool = False
    is_slow: bool = False


class HealthMonitor:
    """Monitors FFmpeg stream health by parsing stderr/progress output."""

    def __init__(self, stall_timeout: int = 30, slow_grace_period: int = 15):
        self.stall_timeout = stall_timeout
        self.slow_grace_period = slow_grace_period
        self._last_frame_count = 0
        self._last_frame_time = time.time()
        self._start_time = time.time()
        self._latest = HealthSnapshot()
        # Accumulator for -progress key=value blocks
        self._pending = {}

    def parse_line(self, line: str) -> None:
        """Parse a single line of FFmpeg output.

        Handles:
        - Key=value lines from -progress pipe:2
        - Single-line stats with \r carriage returns
        """
        # Split on \r in case of carriage return separated updates
        for segment in line.split("\r"):
            segment = segment.strip()
            if not segment:
                continue

            # Distinguish between -progress key=value lines and single-line stats.
            # Key=value lines have exactly one "=" with no spaces (e.g. "frame=100")
            # Stats lines have multiple "=" with spaces (e.g. "frame= 1500 fps=30.0 ...")
            eq_count = segment.count("=")
            if eq_count == 1 and " " not in segment.strip():
                self._parse_progress_kv(segment)
            elif eq_count >= 2:
                # Multiple fields — single-line stats format
                self._parse_stats_line(segment)
            else:
                # Single key=value (could be from -progress)
                self._parse_progress_kv(segment)

    def _parse_progress_kv(self, line: str) -> None:
        """Parse a key=value line from -progress output."""
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()

        if not key or not value:
            return

        self._pending[key] = value

        # "progress=continue" or "progress=end" marks the end of a block
        if key == "progress":
            self._flush_progress_block()

    def _flush_progress_block(self) -> None:
        """Process accumulated key=value pairs into a health snapshot."""
        p = self._pending
        self._pending = {}

        frame_str = p.get("frame", "")
        if not frame_str:
            return

        now = time.time()

        try:
            frame_count = int(frame_str)
        except ValueError:
            return

        fps = self._parse_float(p.get("fps", ""))
        speed = self._parse_speed(p.get("speed", ""))
        bitrate = self._parse_bitrate(p.get("bitrate", ""))
        elapsed = self._parse_time(p.get("out_time", p.get("out_time_ms", "")))

        self._update_snapshot(now, frame_count, fps, bitrate, speed, elapsed)

    def _parse_stats_line(self, text: str) -> None:
        """Parse a single-line stats format."""
        frame_match = FRAME_RE.search(text)
        if not frame_match:
            return

        now = time.time()
        frame_count = int(frame_match.group(1))

        fps_match = FPS_RE.search(text)
        fps = float(fps_match.group(1)) if fps_match else self._latest.fps

        bitrate_match = BITRATE_RE.search(text)
        bitrate = float(bitrate_match.group(1)) if bitrate_match else self._latest.bitrate_kbps

        speed_match = SPEED_RE.search(text)
        speed = float(speed_match.group(1)) if speed_match else self._latest.speed

        elapsed = self._latest.elapsed_seconds
        time_match = TIME_RE.search(text)
        if time_match:
            h, m, s = time_match.groups()
            elapsed = int(h) * 3600 + int(m) * 60 + float(s)

        self._update_snapshot(now, frame_count, fps, bitrate, speed, elapsed)

    def _update_snapshot(self, now, frame_count, fps, bitrate, speed, elapsed):
        """Update the health snapshot with new values, clamped to sane bounds."""
        if frame_count > self._last_frame_count:
            self._last_frame_count = frame_count
            self._last_frame_time = now

        is_stalled = (now - self._last_frame_time) > self.stall_timeout
        in_grace = (now - self._start_time) < self.slow_grace_period
        is_slow = 0 < speed < 0.9 and not in_grace

        if is_stalled:
            logger.warning("Stream stalled: no new frames for %.0fs", now - self._last_frame_time)
        if is_slow:
            logger.warning("Stream slow: speed=%.2fx (encoding can't keep up)", speed)

        # Clamp to sane bounds — FFmpeg can occasionally report wild values
        fps = min(fps, 120) if fps > 0 else self._latest.fps
        bitrate = min(bitrate, 50000) if bitrate > 0 else self._latest.bitrate_kbps
        speed = min(speed, 5.0) if speed > 0 else self._latest.speed

        self._latest = HealthSnapshot(
            timestamp=now,
            frame_count=frame_count,
            fps=fps,
            bitrate_kbps=bitrate,
            speed=speed,
            elapsed_seconds=elapsed if elapsed else self._latest.elapsed_seconds,
            is_stalled=is_stalled,
            is_slow=is_slow,
        )

    @staticmethod
    def _parse_float(s: str) -> float:
        try:
            return float(s)
        except (ValueError, TypeError):
            return 0.0

    @staticmethod
    def _parse_speed(s: str) -> float:
        """Parse speed like '1.05x' or 'N/A'."""
        if not s or s == "N/A":
            return 0.0
        s = s.rstrip("x").strip()
        try:
            return float(s)
        except (ValueError, TypeError):
            return 0.0

    @staticmethod
    def _parse_bitrate(s: str) -> float:
        """Parse bitrate like '2048.5kbits/s' or 'N/A'."""
        if not s or s == "N/A":
            return 0.0
        match = re.search(r"([\d.]+)kbits/s", s)
        if match:
            return float(match.group(1))
        return 0.0

    @staticmethod
    def _parse_time(s: str) -> float:
        """Parse time like '00:01:23.456789' or microseconds."""
        if not s:
            return 0.0
        match = re.match(r"(\d+):(\d+):([\d.]+)", s)
        if match:
            h, m, sec = match.groups()
            return int(h) * 3600 + int(m) * 60 + float(sec)
        # Try microseconds format (out_time_ms)
        try:
            return float(s) / 1000000.0
        except (ValueError, TypeError):
            return 0.0

    def get_snapshot(self) -> HealthSnapshot:
        """Return the latest health snapshot."""
        if self._latest.timestamp > 0:
            elapsed_since_update = time.time() - self._latest.timestamp
            if elapsed_since_update > self.stall_timeout:
                self._latest.is_stalled = True
        return self._latest

    def reset(self) -> None:
        """Reset state for a new stream session."""
        self._last_frame_count = 0
        self._last_frame_time = time.time()
        self._start_time = time.time()
        self._latest = HealthSnapshot()
        self._pending = {}
