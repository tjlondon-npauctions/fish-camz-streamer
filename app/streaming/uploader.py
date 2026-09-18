"""Background thread that uploads HLS segments to Bunny CDN."""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class HLSUploader:
    """Watches a local HLS directory and uploads segments to Bunny Storage."""

    def __init__(
        self,
        segment_dir: str,
        storage_zone: str,
        api_key: str,
        region: str = "",
        stream_path: str = "live",
        state_dir: str = "/run/rpie",
        buffer_segments: int = 150,
        max_unsent_segments: int = 1000,
    ):
        self._segment_dir = Path(segment_dir)
        self._storage_zone = storage_zone
        self._api_key = api_key
        self._stream_path = stream_path.strip("/")
        self._state_file = Path(state_dir) / "uploader.json"

        # Build base URL
        if region:
            self._base_url = f"https://{region}.storage.bunnycdn.com/{storage_zone}"
        else:
            self._base_url = f"https://storage.bunnycdn.com/{storage_zone}"

        self._buffer_segments = buffer_segments
        # Hard cap on total segments on disk (uploaded + pending). When Bunny
        # is unreachable for long periods, segments accumulate because the
        # normal cleanup only evicts already-uploaded ones. This cap kicks in
        # to drop oldest segments unconditionally, preventing the disk from
        # filling up during extended outages (Starlink down for hours, etc).
        # 1000 segments × 6s = ~100 minutes of footage at typical bitrates.
        self._max_unsent_segments = max_unsent_segments
        self._max_timestamp_history = 15000  # cap to prevent unbounded memory growth
        self._force_dropped_count = 0

        self._session = None
        self._requests = None

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # State
        self._uploaded_segments: set[str] = set()
        # Full history of segment timestamps — includes segments no longer on disk
        # but still on CDN. This is the source of truth for DVR time lookups.
        self._segment_timestamps: dict[str, float] = {}
        self._last_index_upload: float = 0
        self._upload_count = 0
        self._error_count = 0
        self._last_error = ""
        self._last_upload_time = 0.0

    def start(self) -> None:
        """Start the background upload thread."""
        import requests as _requests
        self._session = _requests.Session()
        self._session.headers["AccessKey"] = self._api_key
        self._requests = _requests

        self._segment_dir.mkdir(parents=True, exist_ok=True)
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        logger.info("HLS uploader started (zone: %s, path: %s)", self._storage_zone, self._stream_path)

    def stop(self) -> None:
        """Stop the upload thread."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10)
        if self._session:
            self._session.close()
        logger.info("HLS uploader stopped")

    def cleanup(self) -> None:
        """Delete live.m3u8 from CDN so stale playlists aren't served.

        Called on clean stream stop. For unclean shutdowns (power loss),
        the player uses heartbeat freshness to detect offline state.
        """
        import requests as _requests

        playlist_url = f"{self._base_url}/{self._stream_path}/live.m3u8"
        try:
            resp = _requests.delete(
                playlist_url,
                headers={"AccessKey": self._api_key},
                timeout=10,
            )
            if resp.status_code in (200, 204, 404):
                logger.info("Deleted playlist from CDN: %s", playlist_url)
            else:
                logger.warning("CDN playlist delete HTTP %d: %s", resp.status_code, resp.text[:200])
        except Exception as e:
            logger.warning("Failed to delete playlist from CDN: %s", e)

    def get_status(self) -> dict:
        return {
            "running": self._thread is not None and self._thread.is_alive(),
            "upload_count": self._upload_count,
            "error_count": self._error_count,
            "last_error": self._last_error,
            "last_upload_time": self._last_upload_time,
            "segments_tracked": len(self._uploaded_segments),
            "force_dropped_count": self._force_dropped_count,
        }

    def _run(self) -> None:
        """Main upload loop: watch for new segments and upload them."""
        backoff = 1.5
        while not self._stop_event.is_set():
            try:
                self._sync_once()
                backoff = 1.5  # reset on success
            except Exception as e:
                self._last_error = str(e)
                self._error_count += 1
                logger.error("Uploader error: %s", e)
                backoff = min(backoff * 2, 30)  # exponential backoff, max 30s

            self._write_state()
            self._stop_event.wait(backoff)

    def _sync_once(self) -> None:
        """Upload new segments, playlist, and periodically the DVR index.

        Scans the full directory (not just the current playlist) so that
        segments written during a network outage are uploaded when
        connectivity returns.
        """
        playlist = self._segment_dir / "live.m3u8"
        if not playlist.exists():
            return

        # Glob once — reused for upload, cleanup, and pruning
        all_ts_paths = sorted(self._segment_dir.glob("*.ts"), key=lambda p: p.name)
        on_disk = {p.name for p in all_ts_paths}

        # Upload new segments, oldest first
        uploaded_this_round = 0
        for path in all_ts_paths:
            if path.name in self._uploaded_segments:
                continue

            if self._upload_file(path, path.name, "video/mp2t"):
                self._uploaded_segments.add(path.name)
                # Use file mtime — reflects when FFmpeg wrote it
                try:
                    self._segment_timestamps[path.name] = path.stat().st_mtime
                except OSError:
                    self._segment_timestamps[path.name] = time.time()
                uploaded_this_round += 1

        if uploaded_this_round > 0:
            logger.debug("Uploaded %d segments (%d total tracked)",
                         uploaded_this_round, len(self._uploaded_segments))

        # Upload playlist (after segments exist on CDN)
        self._upload_file(playlist, "live.m3u8", "application/vnd.apple.mpegurl")

        # Upload segment index for DVR lookups (every ~30 seconds)
        if self._segment_timestamps:
            now = time.time()
            if now - self._last_index_upload > 30:
                self._upload_segment_index()
                self._last_index_upload = now

        # Prune _uploaded_segments for files no longer on disk
        # (so we don't skip re-uploads if a file reappears with same name)
        stale_tracked = self._uploaded_segments - on_disk
        if stale_tracked:
            self._uploaded_segments -= stale_tracked

        # Cap _segment_timestamps to prevent unbounded memory growth.
        # Prune oldest 20% when limit is exceeded, keeping newest entries
        # for DVR lookups.
        if len(self._segment_timestamps) > self._max_timestamp_history:
            sorted_entries = sorted(self._segment_timestamps.items(), key=lambda x: x[1])
            prune_count = len(sorted_entries) // 5  # remove oldest 20%
            for name, _ in sorted_entries[:prune_count]:
                del self._segment_timestamps[name]
            logger.debug("Pruned %d old segment timestamps (%d remaining)",
                         prune_count, len(self._segment_timestamps))

        # Disk cleanup — keep at most buffer_segments files on disk
        self._cleanup_disk(all_ts_paths)

    def _upload_segment_index(self) -> None:
        """Upload a JSON index mapping segment names to timestamps.

        Contains ALL known segment timestamps — including segments that
        have been cleaned from local disk but still exist on Bunny CDN.
        This is what the DVR API uses for time-range lookups.
        """
        index_path = self._segment_dir / "segments.json"
        try:
            index = {
                "segments": {
                    name: ts
                    for name, ts in sorted(self._segment_timestamps.items())
                },
                "segment_duration": 6,
                "updated_at": time.time(),
            }
            with open(index_path, "w") as f:
                json.dump(index, f)

            self._upload_file(index_path, "segments.json", "application/json")
        except OSError as e:
            logger.warning("Failed to write segment index: %s", e)

    def _cleanup_disk(self, all_ts_paths: list[Path]) -> None:
        """Two-pass disk cleanup.

        Pass 1 (preferred): delete already-uploaded segments beyond
        ``buffer_segments``. Timestamps are preserved in
        ``_segment_timestamps`` for DVR lookups.

        Pass 2 (safety): if total segments still exceed
        ``max_unsent_segments`` after pass 1, drop the oldest unconditionally
        — even if they haven't been uploaded yet. This protects the Pi's disk
        when Bunny is unreachable for an extended period (Starlink outage,
        Bunny rate-limit, etc.). Without this, segments accumulate forever and
        eventually fill the disk, taking the whole streamer down.
        """
        if len(all_ts_paths) <= self._buffer_segments:
            return

        # ─── Pass 1: soft eviction of uploaded segments only ───
        to_delete = len(all_ts_paths) - self._buffer_segments
        deleted = 0
        remaining: list[Path] = []
        for path in all_ts_paths:
            if deleted < to_delete and path.name in self._segment_timestamps:
                try:
                    path.unlink()
                    self._uploaded_segments.discard(path.name)
                    deleted += 1
                    continue
                except OSError:
                    pass
            remaining.append(path)

        if deleted > 0:
            logger.debug(
                "Cleaned up %d uploaded segments from disk (%d remaining)",
                deleted,
                len(remaining),
            )

        # ─── Pass 2: hard eviction when uploads have stalled ───
        # Triggered when total segments-on-disk exceeds the safety cap. We
        # drop oldest first, regardless of upload status, and emit a WARNING
        # so the operator can diagnose. This is the OUTAGE PROTECTION path.
        if len(remaining) > self._max_unsent_segments:
            excess = len(remaining) - self._max_unsent_segments
            hard_deleted = 0
            for path in remaining[:excess]:
                try:
                    path.unlink()
                    self._uploaded_segments.discard(path.name)
                    self._segment_timestamps.pop(path.name, None)
                    hard_deleted += 1
                except OSError:
                    pass
            if hard_deleted > 0:
                self._force_dropped_count += hard_deleted
                logger.warning(
                    "Force-dropped %d unsent segments — Bunny upload stalled? "
                    "(%d still on disk, %d total force-dropped this session)",
                    hard_deleted,
                    len(remaining) - hard_deleted,
                    self._force_dropped_count,
                )

    def _upload_file(self, local_path: Path, remote_name: str, content_type: str) -> bool:
        """Upload a file to Bunny Storage."""
        url = f"{self._base_url}/{self._stream_path}/{remote_name}"
        try:
            with open(local_path, "rb") as f:
                resp = self._session.put(
                    url,
                    data=f,
                    headers={"Content-Type": content_type},
                    timeout=15,
                )
            if resp.status_code in (200, 201):
                self._upload_count += 1
                self._last_upload_time = time.time()
                return True
            else:
                self._last_error = f"Upload {remote_name}: HTTP {resp.status_code}"
                self._error_count += 1
                logger.warning("Upload failed for %s: HTTP %d", remote_name, resp.status_code)
                return False
        except self._requests.RequestException as e:
            self._last_error = f"Upload {remote_name}: {e}"
            self._error_count += 1
            logger.warning("Upload failed for %s: %s", remote_name, e)
            return False

    def _delete_remote(self, remote_name: str) -> None:
        """Delete an old segment from Bunny Storage."""
        url = f"{self._base_url}/{self._stream_path}/{remote_name}"
        try:
            self._session.delete(url, timeout=10)
        except self._requests.RequestException:
            pass

    def _write_state(self) -> None:
        """Write uploader state to tmpfs for the web UI."""
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self._state_file, "w") as f:
                json.dump(self.get_status(), f)
        except OSError:
            pass
