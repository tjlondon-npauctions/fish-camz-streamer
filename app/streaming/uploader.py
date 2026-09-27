"""Background thread that uploads HLS segments to Bunny CDN."""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from pathlib import Path
from typing import Optional

from app.streaming import link_health
from app.streaming.backlog import playlist_durations

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
        catch_up_minutes: Optional[float] = 15,
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
        # Max time per cycle spent on backlog segments (i.e. not in the current
        # playlist), so the playlist is republished every ~10s while catching up.
        self._backlog_budget_seconds = 10.0
        # After an outage, only footage from the last N minutes is uploaded;
        # older unsent segments are deleted unsent. Hours of backlog on a
        # capped Starlink link cost data and compete with the live video.
        # None = upload everything, 0 = live only.
        self._catch_up_minutes = catch_up_minutes
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
        self._playlist_published_at = 0.0
        self._backlog_segments = 0
        self._backlog_behind_live = 0  # unsent and older than the live playlist
        self._backlog_bytes = 0
        self._backlog_oldest_mtime: Optional[float] = None
        self._backlog_skipped_count = 0
        # Every upload's outcome, for judging the link (see link_health.py)
        self._link_events: deque[link_health.LinkEvent] = deque(maxlen=2000)
        self._stream_kbps: Optional[float] = None

    def _seed_timestamps(self) -> None:
        """Load the DVR index a previous run left on disk into memory.

        :meth:`_upload_segment_index` republishes ``segments.json`` wholesale
        from ``_segment_timestamps``, so starting with an empty map would
        flatten the CDN's copy — and with it the DVR timeline for every
        segment older than this process. FFmpeg restarts several times a day,
        so that is routine, not an edge case.

        Entries are a starting point, not a source of truth: a segment still
        on disk has its timestamp re-derived from mtime when it uploads, which
        corrects anything stale here.
        """
        index_path = self._segment_dir / "segments.json"
        try:
            with open(index_path) as f:
                stored = json.load(f).get("segments", {})
        except (OSError, ValueError, AttributeError):
            return
        if not isinstance(stored, dict):
            return

        for name, ts in stored.items():
            if isinstance(name, str) and isinstance(ts, (int, float)):
                self._segment_timestamps[name] = float(ts)

        if self._segment_timestamps:
            logger.info(
                "Seeded %d segment timestamps from the previous DVR index",
                len(self._segment_timestamps),
            )

    def start(self) -> None:
        """Start the background upload thread."""
        import requests as _requests
        self._session = _requests.Session()
        self._session.headers["AccessKey"] = self._api_key
        self._requests = _requests

        self._segment_dir.mkdir(parents=True, exist_ok=True)
        self._seed_timestamps()
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
            # Epoch times on the Pi's clock; the web API turns them into ages
            "playlist_published_at": self._playlist_published_at,
            "backlog_segments": self._backlog_segments,
            "backlog_behind_live": self._backlog_behind_live,
            "backlog_bytes": self._backlog_bytes,
            "backlog_oldest_mtime": self._backlog_oldest_mtime,
            "backlog_skipped_count": self._backlog_skipped_count,
            "catch_up_minutes": self._catch_up_minutes,
            "link": link_health.assess_link(self._link_events, time.time(), self._stream_kbps),
        }

    def _run(self) -> None:
        """Main upload loop: watch for new segments and upload them."""
        backoff = 1.5
        while not self._stop_event.is_set():
            try:
                if self._sync_once():
                    backoff = 1.5  # reset on success
                else:
                    # An upload failed: the link is down or struggling. Back
                    # off rather than retry every ~1.5s — instant failures
                    # (DNS, refused) otherwise spin, and every one logs a
                    # warning into Docker logs on the Pi's SD card. Capped
                    # at 10s so recovery is noticed quickly.
                    backoff = min(backoff * 2, 10)
            except Exception as e:
                self._last_error = str(e)
                self._error_count += 1
                logger.error("Uploader error: %s", e)
                backoff = min(backoff * 2, 30)  # exponential backoff, max 30s

            self._write_state()
            self._stop_event.wait(backoff)

    def _sync_once(self) -> bool:
        """Upload new segments, playlist, and periodically the DVR index.

        Scans the full directory (not just the current playlist) so that
        segments written during a network outage are uploaded when
        connectivity returns. Returns False if any upload failed.
        """
        playlist = self._segment_dir / "live.m3u8"
        if not playlist.exists():
            return True

        # Glob once — reused for upload, cleanup, and pruning
        all_ts_paths = sorted(self._segment_dir.glob("*.ts"), key=lambda p: p.name)
        live_durations = playlist_durations(playlist)
        live_names = set(live_durations)
        self._stream_kbps = self._measure_stream_kbps(live_durations) or self._stream_kbps
        if self._catch_up_minutes is not None:
            all_ts_paths = self._skip_stale_backlog(all_ts_paths, live_names)
        on_disk = {p.name for p in all_ts_paths}

        # Live edge first: the segments the playlist references, then the
        # playlist itself. Only after that do we spend time on the backlog.
        # Previously every pending segment was uploaded before the playlist,
        # so after a long outage (hours of segments buffered on disk) the
        # CDN playlist stayed frozen for the whole catch-up and viewers saw
        # a black player — and every restart began the pass again.
        live_paths = [p for p in all_ts_paths if p.name in live_names]
        live_ok = all(
            self._upload_segment(p)
            for p in live_paths
            if not self._is_uploaded(p.name)
        )

        # Upload playlist (after its segments exist on CDN). If one of them
        # failed, leave the previous playlist up rather than publish a 404.
        playlist_ok = live_ok and self._upload_file(
            playlist, "live.m3u8", "application/vnd.apple.mpegurl"
        )
        if playlist_ok:
            self._playlist_published_at = time.time()

        # Backlog, oldest first, within a time budget so the next cycle can
        # refresh the playlist. Anything not reached is picked up next cycle.
        # Skipped entirely if the live edge just failed, and stopped at the
        # first failure: one failed upload says enough about the link.
        deadline = time.monotonic() + self._backlog_budget_seconds
        uploaded_this_round = 0
        backlog_ok = True
        for path in all_ts_paths if playlist_ok else []:
            if self._is_uploaded(path.name) or path.name in live_names:
                continue
            if time.monotonic() >= deadline:
                break
            if self._upload_segment(path):
                uploaded_this_round += 1
            elif path.exists():  # a vanished file isn't a link failure
                backlog_ok = False
                break

        if uploaded_this_round > 0:
            logger.debug("Uploaded %d backlog segments (%d total tracked)",
                         uploaded_this_round, len(self._uploaded_segments))

        self._measure_backlog()

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
        return playlist_ok and backlog_ok

    def _is_uploaded(self, name: str) -> bool:
        """Uploaded by this run, or by an earlier one.

        ``_segment_timestamps`` only gains an entry after a successful upload,
        and is seeded from the previous run's index — so after a restart the
        segments still buffered on disk aren't sent to Bunny a second time
        (previously ~15 min of video re-uploaded on every restart).
        """
        return name in self._uploaded_segments or name in self._segment_timestamps

    def _skip_stale_backlog(self, paths: list[Path], live_names: set[str]) -> list[Path]:
        """Delete unsent segments older than ``catch_up_minutes``.

        Returns the paths still on disk.
        """
        cutoff = time.time() - self._catch_up_minutes * 60
        kept: list[Path] = []
        skipped = 0
        for path in paths:
            if self._is_uploaded(path.name) or path.name in live_names:
                kept.append(path)
                continue
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    skipped += 1
                    continue
            except OSError:
                continue  # already gone
            kept.append(path)

        if skipped:
            self._backlog_skipped_count += skipped
            logger.warning(
                "Skipped %d unsent segments older than %s min (hls.catch_up_minutes)",
                skipped,
                self._catch_up_minutes,
            )
        return kept

    def _measure_stream_kbps(self, durations: dict[str, float]) -> Optional[float]:
        """Bitrate the stream needs, from the live segments' sizes and lengths."""
        total_bytes = 0
        total_seconds = 0.0
        for name, seconds in durations.items():
            try:
                total_bytes += (self._segment_dir / name).stat().st_size
            except OSError:
                continue
            total_seconds += seconds
        if total_seconds <= 0:
            return None
        return total_bytes * 8 / total_seconds / 1000

    def _measure_backlog(self) -> None:
        """Record how many segments are waiting to upload, for the dashboard.

        Re-globs rather than reusing the cycle's list: on a slow link a cycle
        can take longer than a segment, and the segments FFmpeg wrote
        meanwhile are exactly the lag we want to show.
        """
        paths = sorted(self._segment_dir.glob("*.ts"), key=lambda p: p.name)
        live = playlist_durations(self._segment_dir / "live.m3u8")
        # The newest file is still being written unless the playlist lists it
        if paths and paths[-1].name not in live:
            paths = paths[:-1]
        count = 0
        behind_live = 0
        size = 0
        oldest: Optional[float] = None
        for path in paths:
            if self._is_uploaded(path.name):
                continue
            try:
                st = path.stat()
            except OSError:
                continue
            count += 1
            if path.name not in live:
                behind_live += 1
            size += st.st_size
            if oldest is None or st.st_mtime < oldest:
                oldest = st.st_mtime
        self._backlog_segments = count
        self._backlog_behind_live = behind_live
        self._backlog_bytes = size
        self._backlog_oldest_mtime = oldest

    def _upload_segment(self, path: Path) -> bool:
        """Upload one segment and record it for the DVR index."""
        # A segment can vanish between the glob and the upload (disk cleanup,
        # or someone clearing a backlog by hand) — skip it, don't abort the cycle.
        try:
            mtime = path.stat().st_mtime  # reflects when FFmpeg wrote it
        except OSError:
            return False
        if not self._upload_file(path, path.name, "video/mp2t"):
            return False
        self._uploaded_segments.add(path.name)
        self._segment_timestamps[path.name] = mtime
        return True

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
        started = time.monotonic()
        try:
            size = local_path.stat().st_size
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
                self._record_link(link_health.OK, size, time.monotonic() - started)
                return True
            else:
                self._last_error = f"Upload {remote_name}: HTTP {resp.status_code}"
                self._error_count += 1
                self._record_link(link_health.classify_http(resp.status_code))
                logger.warning("Upload failed for %s: HTTP %d", remote_name, resp.status_code)
                return False
        except self._requests.RequestException as e:
            self._last_error = f"Upload {remote_name}: {e}"
            self._error_count += 1
            self._record_link(link_health.classify_exception(e))
            logger.warning("Upload failed for %s: %s", remote_name, e)
            return False

    def _record_link(self, kind: str, size: int = 0, seconds: float = 0.0) -> None:
        self._link_events.append(link_health.LinkEvent(time.time(), kind, size, seconds))

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
