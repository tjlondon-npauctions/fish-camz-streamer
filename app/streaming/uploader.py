"""Background thread that uploads HLS segments to Bunny CDN."""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Optional

import requests

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

        self._session = requests.Session()
        self._session.headers["AccessKey"] = api_key

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # State
        self._uploaded_segments: set[str] = set()
        self._upload_count = 0
        self._error_count = 0
        self._last_error = ""
        self._last_upload_time = 0.0

    def start(self) -> None:
        """Start the background upload thread."""
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
        self._session.close()
        logger.info("HLS uploader stopped")

    def get_status(self) -> dict:
        return {
            "running": self._thread is not None and self._thread.is_alive(),
            "upload_count": self._upload_count,
            "error_count": self._error_count,
            "last_error": self._last_error,
            "last_upload_time": self._last_upload_time,
            "segments_tracked": len(self._uploaded_segments),
        }

    def _run(self) -> None:
        """Main upload loop: watch for new segments and upload them."""
        while not self._stop_event.is_set():
            try:
                self._sync_once()
            except Exception as e:
                self._last_error = str(e)
                self._error_count += 1
                logger.error("Uploader error: %s", e)

            self._write_state()
            self._stop_event.wait(1.5)

    def _sync_once(self) -> None:
        """Check for new segments and upload them."""
        playlist = self._segment_dir / "live.m3u8"
        if not playlist.exists():
            return

        # Parse playlist to find current segments
        current_segments = set()
        try:
            content = playlist.read_text()
            for line in content.splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    current_segments.add(line)
        except OSError:
            return

        # Upload new segments
        for segment_name in sorted(current_segments):
            if segment_name in self._uploaded_segments:
                continue

            segment_path = self._segment_dir / segment_name
            if not segment_path.exists():
                continue

            if self._upload_file(segment_path, segment_name, "video/mp2t"):
                self._uploaded_segments.add(segment_name)

        # Upload playlist (after segments so they exist on CDN first)
        self._upload_file(playlist, "live.m3u8", "application/vnd.apple.mpegurl")

        # Clean up old segments from CDN
        stale = self._uploaded_segments - current_segments
        for segment_name in stale:
            self._delete_remote(segment_name)
            self._uploaded_segments.discard(segment_name)

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
        except requests.RequestException as e:
            self._last_error = f"Upload {remote_name}: {e}"
            self._error_count += 1
            logger.warning("Upload failed for %s: %s", remote_name, e)
            return False

    def _delete_remote(self, remote_name: str) -> None:
        """Delete an old segment from Bunny Storage."""
        url = f"{self._base_url}/{self._stream_path}/{remote_name}"
        try:
            self._session.delete(url, timeout=10)
        except requests.RequestException:
            pass  # Best effort cleanup

    def _write_state(self) -> None:
        """Write uploader state to tmpfs for the web UI."""
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self._state_file, "w") as f:
                json.dump(self.get_status(), f)
        except OSError:
            pass
