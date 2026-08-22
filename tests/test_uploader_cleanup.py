"""Tests for disk retention: the count cap, the byte cap, and the free-space floor."""

import time
from unittest import mock

from app.streaming.playlist import PublishedEntry, PublishState
from app.streaming.uploader import HLSUploader

SMALL = 75_000      # a healthy 6s segment
HUGE = 3_300_000    # what a 4.4Mbps camera produces


def _uploader(tmp_path, **overrides):
    kwargs = dict(
        segment_dir=str(tmp_path),
        storage_zone="zone",
        api_key="key",
        state_dir=str(tmp_path / "run"),
        buffer_segments=10,
        max_unsent_segments=20,
        max_disk_bytes=0,     # off unless a test asks for it
        min_free_bytes=0,     # off unless a test asks for it
    )
    kwargs.update(overrides)
    return HLSUploader(**kwargs)


def _segments(tmp_path, count, size=SMALL, uploaded=False, up=None):
    entries = []
    for i in range(count):
        path = tmp_path / f"s1_{i:06d}.ts"
        path.write_bytes(b"x" * size)
        entries.append((path, size))
        if uploaded and up is not None:
            up._segment_timestamps[path.name] = time.time() - (count - i)
            up._uploaded_segments.add(path.name)
    return entries


class TestSoftEviction:
    def test_nothing_evicted_below_the_buffer(self, tmp_path):
        up = _uploader(tmp_path)
        entries = _segments(tmp_path, 5, uploaded=True, up=up)
        up._cleanup_disk(entries)
        assert all(path.exists() for path, _ in entries)

    def test_uploaded_segments_evicted_beyond_the_buffer(self, tmp_path):
        up = _uploader(tmp_path)
        entries = _segments(tmp_path, 15, uploaded=True, up=up)
        up._cleanup_disk(entries)
        assert sum(1 for path, _ in entries if path.exists()) == 10
        assert not entries[0][0].exists()  # oldest went first

    def test_unuploaded_segments_survive_soft_eviction(self, tmp_path):
        up = _uploader(tmp_path)
        entries = _segments(tmp_path, 15)  # none confirmed
        up._cleanup_disk(entries)
        assert all(path.exists() for path, _ in entries)

    def test_timestamps_preserved_for_dvr(self, tmp_path):
        up = _uploader(tmp_path)
        entries = _segments(tmp_path, 15, uploaded=True, up=up)
        up._cleanup_disk(entries)
        assert len(up._segment_timestamps) == 15


class TestHardEviction:
    def test_count_cap_drops_oldest_unsent(self, tmp_path):
        up = _uploader(tmp_path)
        entries = _segments(tmp_path, 25)
        up._cleanup_disk(entries)
        assert sum(1 for path, _ in entries if path.exists()) == 20
        assert up._force_dropped_count == 5

    def test_byte_cap_binds_before_count_cap_at_high_bitrate(self, tmp_path):
        """1000 segments is 75MB at 75KB each but 3.3GB at 3.3MB — the count
        cap alone is not a disk guarantee."""
        up = _uploader(tmp_path, max_disk_bytes=5 * HUGE)
        entries = _segments(tmp_path, 12, size=HUGE)
        up._cleanup_disk(entries)
        surviving = sum(1 for path, _ in entries if path.exists())
        assert surviving <= 5
        assert up._force_dropped_bytes > 0

    def test_small_segments_unaffected_by_byte_cap(self, tmp_path):
        """Healthy installs must see no behaviour change."""
        up = _uploader(tmp_path, max_disk_bytes=2147483648)
        entries = _segments(tmp_path, 15, uploaded=True, up=up)
        up._cleanup_disk(entries)
        assert sum(1 for path, _ in entries if path.exists()) == 10
        assert up._force_dropped_count == 0

    def test_published_segments_are_never_hard_dropped(self, tmp_path):
        up = _uploader(tmp_path)
        entries = _segments(tmp_path, 25)
        protected = entries[0][0].name
        up._publish_state = PublishState(
            entries=(PublishedEntry(protected, 6.0, time.time(), False, 0),),
            next_seq=1,
        )
        up._cleanup_disk(entries)
        assert (tmp_path / protected).exists()

    def test_free_space_floor_triggers_eviction(self, tmp_path):
        up = _uploader(tmp_path, min_free_bytes=10 * 1024 ** 3)
        entries = _segments(tmp_path, 25)
        usage = mock.Mock(free=1024 ** 3)  # 1GiB free, below the floor
        with mock.patch("shutil.disk_usage", return_value=usage):
            up._cleanup_disk(entries)
        assert up._force_dropped_count > 0

    def test_disk_usage_error_does_not_evict(self, tmp_path):
        up = _uploader(tmp_path, min_free_bytes=10 * 1024 ** 3)
        entries = _segments(tmp_path, 5, uploaded=True, up=up)
        with mock.patch("shutil.disk_usage", side_effect=OSError):
            up._cleanup_disk(entries)
        assert all(path.exists() for path, _ in entries)
