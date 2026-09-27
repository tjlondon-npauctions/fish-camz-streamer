"""Helpers for the local HLS segment backlog, shared by the uploader
(streamer container) and the web UI (web container) — both see the same
segment directory through the ./data bind mount."""

from __future__ import annotations

import json
import time
from pathlib import Path

# FFmpeg writes a segment under its final name and only adds it to the
# playlist once it's complete, so the segment being written is in neither.
# Anything this fresh is left alone — deleting it would leave the playlist
# pointing at a file that never uploads.
IN_PROGRESS_GRACE_SECONDS = 60


def playlist_durations(playlist: Path) -> dict[str, float]:
    """Segment filename → duration (from #EXTINF) for a local playlist."""
    try:
        lines = playlist.read_text().splitlines()
    except OSError:
        return {}
    durations: dict[str, float] = {}
    pending = 0.0
    for line in lines:
        line = line.strip()
        if line.startswith("#EXTINF:"):
            try:
                pending = float(line[len("#EXTINF:"):].split(",")[0])
            except ValueError:
                pending = 0.0
        elif line and not line.startswith("#"):
            durations[Path(line).name] = pending
            pending = 0.0
    return durations


def playlist_segments(playlist: Path) -> set[str]:
    """Segment filenames referenced by a local playlist."""
    return set(playlist_durations(playlist))


def uploaded_segments(segment_dir: Path) -> set[str]:
    """Names in the uploader's local DVR index — i.e. confirmed on Bunny."""
    try:
        with open(segment_dir / "segments.json") as f:
            segments = json.load(f).get("segments", {})
    except (OSError, ValueError, AttributeError):
        return set()
    return set(segments) if isinstance(segments, dict) else set()


def skip_backlog(segment_dir: Path) -> int:
    """Delete unsent segments behind the live edge so uploads resume at live.

    Keeps what the playlist references, anything still being written, and
    anything already uploaded (the uploader's disk cleanup handles those), so
    the count returned is the footage that will never reach the DVR.
    """
    keep = playlist_segments(segment_dir / "live.m3u8") | uploaded_segments(segment_dir)
    cutoff = time.time() - IN_PROGRESS_GRACE_SECONDS
    deleted = 0
    for path in segment_dir.glob("*.ts"):
        if path.name in keep:
            continue
        try:
            if path.stat().st_mtime >= cutoff:
                continue
            path.unlink()
            deleted += 1
        except OSError:
            pass  # already gone (the uploader's cleanup got there first)
    return deleted
