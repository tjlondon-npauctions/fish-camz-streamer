"""Background thread that uploads HLS segments to Bunny CDN.

The hard problem here is a vessel whose uplink cannot carry its own video.
When that happens the uploader must degrade towards *current but gappy* rather
than *complete but hours late*, so the scheduling and playlist decisions live
as pure functions in :mod:`app.streaming.playlist` and this class is the I/O
around them.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Optional

from app.streaming.playlist import (
    PublishState,
    PublishedEntry,
    UploadCandidate,
    advance_published,
    parse_playlist,
    plan_uploads,
    render_playlist,
)

logger = logging.getLogger(__name__)

STATE_VERSION = 1
STATE_FILENAME = "uploader_state.json"
#: How many segment timestamps the hot state file carries (~2h at 6s).
RECENT_TIMESTAMP_LIMIT = 1200
#: Republish the playlist at least this often even when unchanged, so a lost
#: PUT or a CDN cache miss cannot strand viewers on a stale playlist.
PLAYLIST_REFRESH_INTERVAL = 60.0
#: The index can yield to live segments, but never for longer than this.
INDEX_MAX_DEFERRAL = 600.0


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
        segment_duration: float = 6.0,
        published_playlist_size: int = 10,
        max_disk_bytes: int = 2147483648,
        min_free_bytes: int = 1073741824,
        live_batch: int = 2,
        live_catch_up: int = 6,
        live_deadline: float = 30.0,
        backfill_min_interval: float = 120.0,
        backfill_suspend_backlog: float = 900.0,
        index_upload_interval: float = 180.0,
        state_persist_interval: float = 30.0,
        max_publish_age: float = 600.0,
    ):
        self._segment_dir = Path(segment_dir)
        self._storage_zone = storage_zone
        self._api_key = api_key
        self._stream_path = stream_path.strip("/")
        self._state_file = Path(state_dir) / "uploader.json"
        self._persist_file = self._segment_dir / STATE_FILENAME

        # Build base URL
        if region:
            self._base_url = f"https://{region}.storage.bunnycdn.com/{storage_zone}"
        else:
            self._base_url = f"https://storage.bunnycdn.com/{storage_zone}"

        self._buffer_segments = buffer_segments
        # Hard caps on what may sit on disk. The count cap alone is not a disk
        # guarantee — 1000 segments is 75MB at 75KB each but 3.3GB at 3.3MB —
        # so a byte budget runs alongside it and whichever binds first wins.
        self._max_unsent_segments = max_unsent_segments
        self._max_disk_bytes = max_disk_bytes
        self._min_free_bytes = min_free_bytes
        self._max_timestamp_history = 15000  # cap to prevent unbounded memory growth
        self._force_dropped_count = 0
        self._force_dropped_bytes = 0

        self._segment_duration = segment_duration
        self._published_playlist_size = published_playlist_size
        self._live_batch = live_batch
        self._live_catch_up = live_catch_up
        self._live_deadline = live_deadline
        self._backfill_min_interval = backfill_min_interval
        self._backfill_suspend_backlog = backfill_suspend_backlog
        self._index_upload_interval = index_upload_interval
        self._state_persist_interval = state_persist_interval
        self._max_publish_age = max_publish_age

        self._session = None
        self._requests = None

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # State
        self._uploaded_segments: set[str] = set()
        # Full history of segment timestamps — includes segments no longer on disk
        # but still on CDN. This is the source of truth for DVR time lookups, and
        # membership here is also what "confirmed on the CDN" means when deciding
        # what may appear in the published playlist.
        self._segment_timestamps: dict[str, float] = {}
        self._publish_state = PublishState()
        self._last_index_upload: float = time.time()
        self._last_index_hash: str = ""
        self._last_playlist_text: str = ""
        self._last_playlist_upload: float = 0
        self._last_backfill_at: float = 0
        self._last_persist: float = 0
        self._throughput_bps: float = 0.0
        self._backlog_seconds: float = 0.0
        self._disk_bytes: int = 0
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
        # Rehydrate before the thread runs. Without this, every FFmpeg restart
        # starts from an empty uploaded-set and re-sends the entire on-disk
        # buffer oldest-first — which is what made hours-old footage play live.
        self._load_state()
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        logger.info("HLS uploader started (zone: %s, path: %s)", self._storage_zone, self._stream_path)

    def stop(self) -> None:
        """Stop the upload thread."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10)
        self._persist_state(force=True)
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
        entries = self._publish_state.entries
        return {
            "running": self._thread is not None and self._thread.is_alive(),
            "upload_count": self._upload_count,
            "error_count": self._error_count,
            "last_error": self._last_error,
            "last_upload_time": self._last_upload_time,
            "segments_tracked": len(self._segment_timestamps),
            "force_dropped_count": self._force_dropped_count,
            "force_dropped_bytes": self._force_dropped_bytes,
            "backlog_seconds": round(self._backlog_seconds, 1),
            "uplink_kbps_ewma": round(self._throughput_bps * 8.0 / 1000.0, 1),
            "disk_bytes": self._disk_bytes,
            "published_entries": len(entries),
            "media_sequence": entries[0].seq if entries else 0,
        }

    # ── Persistence ───────────────────────────────────────────────────────

    def _load_state(self) -> None:
        """Rehydrate upload state from disk.

        Never raises: a missing or corrupt file degrades to a cold start, which
        is exactly the old behaviour. Two sources are merged — ``segments.json``
        (already written for the DVR index, so a Pi upgrading from an older
        build has one) and the hot state file's recent tail.
        """
        timestamps: dict[str, float] = {}

        index_path = self._segment_dir / "segments.json"
        try:
            data = json.loads(index_path.read_text())
            segments = data.get("segments")
            if isinstance(segments, dict):
                for name, ts in segments.items():
                    try:
                        timestamps[str(name)] = float(ts)
                    except (TypeError, ValueError):
                        continue
        except (OSError, ValueError, AttributeError):
            pass

        state: dict = {}
        try:
            loaded = json.loads(self._persist_file.read_text())
            if isinstance(loaded, dict) and loaded.get("version") == STATE_VERSION:
                state = loaded
        except (OSError, ValueError):
            pass

        recent = state.get("recent_timestamps")
        if isinstance(recent, dict):
            for name, ts in recent.items():
                try:
                    timestamps[str(name)] = float(ts)
                except (TypeError, ValueError):
                    continue

        self._segment_timestamps = timestamps

        on_disk = {p.name for p in self._segment_dir.glob("*.ts")}
        uploaded = state.get("uploaded")
        if isinstance(uploaded, list):
            self._uploaded_segments = {str(n) for n in uploaded} & on_disk
        else:
            # No hot state (first run of this version). Anything we have a
            # timestamp for is already on the CDN, so trust the index.
            self._uploaded_segments = set(timestamps) & on_disk

        self._publish_state = self._load_publish_state(state)

        if timestamps:
            logger.info(
                "Uploader state restored: %d known segments, %d already uploaded, "
                "%d published entries",
                len(timestamps),
                len(self._uploaded_segments),
                len(self._publish_state.entries),
            )

    def _load_publish_state(self, state: dict) -> PublishState:
        """Rebuild the published window, dropping anything now too stale."""
        raw = state.get("published")
        try:
            next_seq = int(state.get("next_seq", 0))
            disc_seq = int(state.get("disc_seq", 0))
        except (TypeError, ValueError):
            return PublishState()

        entries: list[PublishedEntry] = []
        now = time.time()
        if isinstance(raw, list):
            for item in raw:
                if not isinstance(item, dict):
                    continue
                try:
                    start = float(item["start_epoch"])
                    if now - start > self._max_publish_age:
                        continue
                    entries.append(
                        PublishedEntry(
                            name=str(item["name"]),
                            duration=float(item["duration"]),
                            start_epoch=start,
                            discontinuity=bool(item.get("discontinuity", False)),
                            seq=int(item["seq"]),
                        )
                    )
                except (KeyError, TypeError, ValueError):
                    continue

        # next_seq survives even when the window is dropped, so the media
        # sequence never runs backwards across a restart.
        return PublishState(entries=tuple(entries), next_seq=next_seq, disc_seq=disc_seq)

    def _persist_state(self, force: bool = False) -> None:
        """Write the hot state file, throttled to protect the SD card."""
        now = time.time()
        if not force and now - self._last_persist < self._state_persist_interval:
            return
        self._last_persist = now

        recent = sorted(self._segment_timestamps.items(), key=lambda kv: kv[1])
        recent = recent[-RECENT_TIMESTAMP_LIMIT:]

        payload = {
            "version": STATE_VERSION,
            "written_at": now,
            "uploaded": sorted(self._uploaded_segments),
            "recent_timestamps": dict(recent),
            "next_seq": self._publish_state.next_seq,
            "disc_seq": self._publish_state.disc_seq,
            "published": [
                {
                    "name": e.name,
                    "duration": e.duration,
                    "start_epoch": e.start_epoch,
                    "discontinuity": e.discontinuity,
                    "seq": e.seq,
                }
                for e in self._publish_state.entries
            ],
        }

        tmp = self._persist_file.with_suffix(".tmp")
        try:
            self._persist_file.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(payload))
            os.replace(tmp, self._persist_file)
        except OSError as e:
            logger.debug("Could not persist uploader state: %s", e)

    # ── Main loop ─────────────────────────────────────────────────────────

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
            self._persist_state()
            self._stop_event.wait(backoff)

    def _scan_segments(self) -> list[tuple[Path, int]]:
        """One pass over the segment directory: (path, size), oldest first.

        Sizes come from the same scandir call rather than a stat() per file —
        on an SD card polled every 1.5s that difference is worth having.
        """
        found: list[tuple[Path, int]] = []
        try:
            with os.scandir(self._segment_dir) as it:
                for entry in it:
                    if not entry.name.endswith(".ts"):
                        continue
                    try:
                        found.append((Path(entry.path), entry.stat().st_size))
                    except OSError:
                        continue
        except OSError:
            return []
        found.sort(key=lambda pair: pair[0].name)
        return found

    def _sync_once(self) -> None:
        """Upload segments, publish the playlist, and keep the disk bounded.

        Scans the whole directory rather than just the current playlist, so
        segments written during an outage are still uploaded when connectivity
        returns — but the live window is always served first, because on a link
        that cannot keep up, oldest-first means the live edge is never reached.
        """
        now = time.time()
        entries = self._scan_segments()
        on_disk = {path.name for path, _ in entries}
        self._disk_bytes = sum(size for _, size in entries)

        # A missing playlist (the gap while FFmpeg restarts) means "publish
        # nothing new", not "do nothing" — backfill and cleanup still matter.
        playlist_path = self._segment_dir / "live.m3u8"
        try:
            source = parse_playlist(playlist_path.read_text())
        except OSError:
            source = []

        plan = plan_uploads(
            live_names=[e.name for e in source],
            on_disk=[UploadCandidate(path.name, size) for path, size in entries],
            confirmed=self._segment_timestamps,
            throughput_bps=self._throughput_bps,
            now=now,
            last_backfill_at=self._last_backfill_at,
            segment_duration=self._segment_duration,
            live_batch=self._live_batch,
            live_catch_up=self._live_catch_up,
            live_deadline=self._live_deadline,
            backfill_min_interval=self._backfill_min_interval,
            backfill_suspend_backlog=self._backfill_suspend_backlog,
        )
        self._backlog_seconds = plan.backlog_seconds

        starts = {e.name: e.program_date_time for e in source}
        sizes = {path.name: size for path, size in entries}

        live_sent = 0
        for candidate in plan.live:
            if self._upload_segment(candidate.name, sizes, starts):
                live_sent += 1
        for candidate in plan.backfill:
            if self._upload_segment(candidate.name, sizes, starts):
                self._last_backfill_at = now

        self._publish(source, now)
        # Yield the link to live video only while live uploads are actually
        # landing. If they are all failing we are not competing for bandwidth,
        # and the index would otherwise be starved until the hard override.
        self._maybe_upload_index(now, live_pending=live_sent > 0)

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

        self._cleanup_disk(entries)

    def _upload_segment(self, name: str, sizes: dict, starts: dict) -> bool:
        """Upload one .ts and record when it actually started."""
        path = self._segment_dir / name
        if not self._upload_file(path, name, "video/mp2t"):
            return False

        self._uploaded_segments.add(name)
        start = starts.get(name)
        if start is None:
            # No PROGRAM-DATE-TIME to go on. mtime is when FFmpeg *finished*
            # the segment, so step back one duration to approximate its start.
            try:
                start = path.stat().st_mtime - self._segment_duration
            except OSError:
                start = time.time() - self._segment_duration
        self._segment_timestamps[name] = start
        return True

    # ── Publishing ────────────────────────────────────────────────────────

    def _publish(self, source, now: float) -> None:
        """Rewrite and upload live.m3u8 from confirmed-uploaded segments only.

        The playlist FFmpeg writes names segments the moment they exist on
        disk, so publishing it verbatim advertises files that may still be
        queued — every one of them a 404 for the player. Rewriting from the
        confirmed set is what makes the published playlist always resolvable.

        Never write over FFmpeg's own live.m3u8: with -hls_flags append_list it
        reads that file back on restart to continue numbering.
        """
        if not source:
            return

        self._publish_state = advance_published(
            self._publish_state,
            source,
            confirmed_times=self._segment_timestamps,
            window=self._published_playlist_size,
            segment_duration=self._segment_duration,
            max_publish_age=self._max_publish_age,
            now=now,
        )

        text = render_playlist(self._publish_state, target_duration=self._segment_duration)
        if not text:
            return

        unchanged = text == self._last_playlist_text
        if unchanged and now - self._last_playlist_upload < PLAYLIST_REFRESH_INTERVAL:
            return

        if self._upload_bytes(text.encode("utf-8"), "live.m3u8",
                              "application/vnd.apple.mpegurl"):
            self._last_playlist_text = text
            self._last_playlist_upload = now

    def _maybe_upload_index(self, now: float, live_pending: bool) -> None:
        """Upload the DVR index, but not at the expense of live video.

        The index is the full timestamp map — hundreds of KB — and its only
        consumer reads it every five minutes. Sending it every 30s while live
        segments queue behind it was consuming a large share of a slow uplink.
        """
        if not self._segment_timestamps:
            return

        overdue = now - self._last_index_upload > INDEX_MAX_DEFERRAL
        if live_pending and not overdue:
            return
        if now - self._last_index_upload < self._index_upload_interval and not overdue:
            return

        self._upload_segment_index()
        self._last_index_upload = now

    def _upload_segment_index(self) -> None:
        """Upload a JSON index mapping segment names to timestamps.

        Contains ALL known segment timestamps — including segments that
        have been cleaned from local disk but still exist on Bunny CDN.
        This is what the DVR API uses for time-range lookups, and it must stay
        a superset of everything ever uploaded: the cloud-side sync is
        append-only, so anything missing here is invisible to the DVR forever.
        """
        index_path = self._segment_dir / "segments.json"
        segments = {
            name: ts
            for name, ts in sorted(self._segment_timestamps.items())
        }

        digest = hashlib.sha256(
            json.dumps(segments, sort_keys=True).encode("utf-8")
        ).hexdigest()
        if digest == self._last_index_hash:
            return  # nothing new to say

        try:
            index = {
                "segments": segments,
                "segment_duration": self._segment_duration,
                "updated_at": time.time(),
            }
            payload = json.dumps(index)
            # The local copy doubles as the restart seed read by _load_state().
            with open(index_path, "w") as f:
                f.write(payload)

            if self._upload_bytes(payload.encode("utf-8"), "segments.json",
                                  "application/json"):
                self._last_index_hash = digest
        except OSError as e:
            logger.warning("Failed to write segment index: %s", e)

    # ── Disk ──────────────────────────────────────────────────────────────

    def _cleanup_disk(self, entries: list) -> None:
        """Two-pass disk cleanup.

        Pass 1 (preferred): delete already-uploaded segments beyond
        ``buffer_segments`` or the byte budget. Timestamps are preserved in
        ``_segment_timestamps`` for DVR lookups.

        Pass 2 (safety): if the directory still exceeds ``max_unsent_segments``
        or ``max_disk_bytes``, drop the oldest unconditionally — even if they
        haven't been uploaded yet. This protects the Pi's disk when Bunny is
        unreachable for an extended period (Starlink outage, Bunny rate-limit,
        etc). Without this, segments accumulate forever and eventually fill the
        disk, taking the whole streamer down.
        """
        total_bytes = sum(size for _, size in entries)
        over_budget = self._over_budget(len(entries), total_bytes)

        if len(entries) <= self._buffer_segments and not over_budget:
            return

        # ─── Pass 1: soft eviction of uploaded segments only ───
        remaining: list[tuple[Path, int]] = []
        deleted = 0
        count = len(entries)
        for path, size in entries:
            evict = count > self._buffer_segments or self._over_budget(count, total_bytes)
            if evict and path.name in self._segment_timestamps:
                try:
                    path.unlink()
                    self._uploaded_segments.discard(path.name)
                    deleted += 1
                    count -= 1
                    total_bytes -= size
                    continue
                except OSError:
                    pass
            remaining.append((path, size))

        if deleted > 0:
            logger.debug(
                "Cleaned up %d uploaded segments from disk (%d remaining)",
                deleted,
                len(remaining),
            )

        # ─── Pass 2: hard eviction when uploads have stalled ───
        # Triggered when the directory still exceeds a safety cap. We drop
        # oldest first, regardless of upload status, and emit a WARNING so the
        # operator can diagnose. This is the OUTAGE PROTECTION path.
        published = {e.name for e in self._publish_state.entries}
        hard_deleted = 0
        hard_bytes = 0
        by_count = count > self._max_unsent_segments
        for path, size in list(remaining):
            if not self._over_budget(count, total_bytes) and count <= self._max_unsent_segments:
                break
            # Never pull a segment out from under the published playlist.
            if path.name in published:
                continue
            try:
                path.unlink()
                self._uploaded_segments.discard(path.name)
                self._segment_timestamps.pop(path.name, None)
                remaining.remove((path, size))
                hard_deleted += 1
                hard_bytes += size
                count -= 1
                total_bytes -= size
            except OSError:
                pass

        if hard_deleted > 0:
            self._force_dropped_count += hard_deleted
            self._force_dropped_bytes += hard_bytes
            logger.warning(
                "Force-dropped %d unsent segments (%.1f MB, trigger: %s) — Bunny "
                "upload stalled? (%d still on disk, %d total force-dropped this session)",
                hard_deleted,
                hard_bytes / 1e6,
                "count" if by_count else "disk budget",
                len(remaining),
                self._force_dropped_count,
            )

    def _over_budget(self, count: int, total_bytes: int) -> bool:
        """True when the segment directory is using more disk than allowed."""
        if self._max_disk_bytes and total_bytes > self._max_disk_bytes:
            return True
        if self._min_free_bytes:
            try:
                free = shutil.disk_usage(str(self._segment_dir)).free
            except OSError:
                return False
            if free < self._min_free_bytes:
                return True
        return False

    # ── Transport ─────────────────────────────────────────────────────────

    def _upload_file(self, local_path: Path, remote_name: str, content_type: str) -> bool:
        """Upload a file to Bunny Storage, streaming it from disk.

        NOTE: requests' ``timeout`` is a per-socket-operation timeout, not a
        deadline for the whole transfer. A 3MB PUT over a slow link can take
        well over a minute without tripping it, which is correct — do not
        "fix" this by adding a total-duration timeout, or slow-but-healthy
        uploads will be killed mid-flight.
        """
        url = f"{self._base_url}/{self._stream_path}/{remote_name}"
        started = time.monotonic()
        try:
            size = local_path.stat().st_size
        except OSError:
            size = 0
        try:
            with open(local_path, "rb") as f:
                resp = self._session.put(
                    url,
                    data=f,
                    headers={"Content-Type": content_type},
                    timeout=15,
                )
            return self._finish_upload(resp, remote_name, size, started)
        except self._requests.RequestException as e:
            self._last_error = f"Upload {remote_name}: {e}"
            self._error_count += 1
            logger.warning("Upload failed for %s: %s", remote_name, e)
            return False

    def _upload_bytes(self, payload: bytes, remote_name: str, content_type: str) -> bool:
        """Upload an in-memory payload (playlist, index) without touching disk."""
        url = f"{self._base_url}/{self._stream_path}/{remote_name}"
        started = time.monotonic()
        try:
            resp = self._session.put(
                url,
                data=payload,
                headers={"Content-Type": content_type},
                timeout=15,
            )
            return self._finish_upload(resp, remote_name, len(payload), started)
        except self._requests.RequestException as e:
            self._last_error = f"Upload {remote_name}: {e}"
            self._error_count += 1
            logger.warning("Upload failed for %s: %s", remote_name, e)
            return False

    def _finish_upload(self, resp, remote_name: str, size: int, started: float) -> bool:
        if resp.status_code in (200, 201):
            self._upload_count += 1
            self._last_upload_time = time.time()
            self._record_throughput(size, time.monotonic() - started)
            return True
        self._last_error = f"Upload {remote_name}: HTTP {resp.status_code}"
        self._error_count += 1
        logger.warning("Upload failed for %s: HTTP %d", remote_name, resp.status_code)
        return False

    def _record_throughput(self, size: int, elapsed: float) -> None:
        """Feed a completed upload into the uplink estimate.

        Used to spot segments too large to reach the CDN while they are still
        live-relevant. Seeded at zero so the estimate is treated as unknown
        until there is a real sample.
        """
        if size <= 0 or elapsed <= 0:
            return
        sample = size / elapsed
        if self._throughput_bps <= 0:
            self._throughput_bps = sample
        else:
            self._throughput_bps = 0.3 * sample + 0.7 * self._throughput_bps

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
