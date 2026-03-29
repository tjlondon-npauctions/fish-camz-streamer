"""Parses FFmpeg stderr progress output into structured health metrics."""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# FFmpeg progress line pattern:
# frame= 1234 fps=30.0 q=28.0 size= 12345kB time=00:01:23.45 bitrate=1234.5kbits/s speed=1.0x
PROGRESS_RE = re.compile(
    r"frame=\s*(\d+)\s+"
    r"fps=\s*([\d.]+)\s+"
    r".*?"
    r"bitrate=\s*([\d.]+)kbits/s\s+"
    r".*?"
    r"speed=\s*([\d.]+)x"
)

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
        """Parse a single line of FFmpeg stderr output."""
        match = PROGRESS_RE.search(line)
        if not match:
            return

        now = time.time()
        frame_count = int(match.group(1))
        fps = float(match.group(2))
        bitrate = float(match.group(3))
        speed = float(match.group(4))

        # Parse elapsed time
        elapsed = 0.0
        time_match = TIME_RE.search(line)
        if time_match:
            h, m, s = time_match.groups()
            elapsed = int(h) * 3600 + int(m) * 60 + float(s)

        # Check for stall
        if frame_count > self._last_frame_count:
            self._last_frame_count = frame_count
            self._last_frame_time = now

        is_stalled = (now - self._last_frame_time) > self.stall_timeout
        is_slow = speed < 0.9 and speed > 0

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
