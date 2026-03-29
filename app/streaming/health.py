"""Parses FFmpeg stderr progress output into structured health metrics."""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Individual field patterns — more robust than one monolithic regex.
# FFmpeg progress lines look like:
#   frame= 1234 fps=30.0 q=28.0 size= 12345kB time=00:01:23.45 bitrate=1234.5kbits/s speed=1.0x
# But formatting varies by version, and \r carriage returns are common.
FRAME_RE = re.compile(r"frame=\s*(\d+)")
FPS_RE = re.compile(r"fps=\s*([\d.]+)")
BITRATE_RE = re.compile(r"bitrate=\s*([\d.]+)kbits/s")
SPEED_RE = re.compile(r"speed=\s*([\d.]+)x")
TIME_RE = re.compile(r"time=(\d+):(\d+):([\d.]+)")


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
    """Monitors FFmpeg stream health by parsing stderr output."""

    def __init__(self, stall_timeout: int = 30):
        self.stall_timeout = stall_timeout
        self._last_frame_count = 0
        self._last_frame_time = time.time()
        self._latest = HealthSnapshot()

    def parse_line(self, line: str) -> None:
        """Parse a single line of FFmpeg stderr output.

        Handles both single-line progress format and carriage-return
        overwritten lines. Extracts whatever fields are present.
        """
        # Split on \r in case multiple progress updates are in one line
        for segment in line.split("\r"):
            segment = segment.strip()
            if not segment:
                continue
            self._parse_segment(segment)

    def _parse_segment(self, text: str) -> None:
        """Parse a single progress segment."""
        frame_match = FRAME_RE.search(text)
        if not frame_match:
            return  # Not a progress line

        now = time.time()
        frame_count = int(frame_match.group(1))

        fps_match = FPS_RE.search(text)
        fps = float(fps_match.group(1)) if fps_match else self._latest.fps

        bitrate_match = BITRATE_RE.search(text)
        bitrate = float(bitrate_match.group(1)) if bitrate_match else self._latest.bitrate_kbps

        speed_match = SPEED_RE.search(text)
        speed = float(speed_match.group(1)) if speed_match else self._latest.speed

        # Parse elapsed time
        elapsed = self._latest.elapsed_seconds
        time_match = TIME_RE.search(text)
        if time_match:
            h, m, s = time_match.groups()
            elapsed = int(h) * 3600 + int(m) * 60 + float(s)

        # Check for stall
        if frame_count > self._last_frame_count:
            self._last_frame_count = frame_count
            self._last_frame_time = now

        is_stalled = (now - self._last_frame_time) > self.stall_timeout
        is_slow = 0 < speed < 0.9

        if is_stalled:
            logger.warning("Stream stalled: no new frames for %.0fs", now - self._last_frame_time)
        if is_slow:
            logger.warning("Stream slow: speed=%.2fx (encoding can't keep up)", speed)

        self._latest = HealthSnapshot(
            timestamp=now,
            frame_count=frame_count,
            fps=fps,
            bitrate_kbps=bitrate,
            speed=speed,
            elapsed_seconds=elapsed,
            is_stalled=is_stalled,
            is_slow=is_slow,
        )

    def get_snapshot(self) -> HealthSnapshot:
        """Return the latest health snapshot."""
        # Check for stall even if no new lines have been parsed
        if self._latest.timestamp > 0:
            elapsed_since_update = time.time() - self._latest.timestamp
            if elapsed_since_update > self.stall_timeout:
                self._latest.is_stalled = True
        return self._latest

    def reset(self) -> None:
        """Reset state for a new stream session."""
        self._last_frame_count = 0
        self._last_frame_time = time.time()
        self._latest = HealthSnapshot()
