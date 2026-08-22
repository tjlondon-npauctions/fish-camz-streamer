"""Integration tests for HLSUploader._sync_once on a real filesystem.

Network I/O is stubbed at _upload_file/_upload_bytes so tests can script
arbitrary failure patterns without touching requests.
"""

import json
import time
from pathlib import Path
from unittest import mock

from app.streaming.playlist import parse_playlist
from app.streaming.uploader import HLSUploader

SEG = 6.0
# Anchor near now: the uploader refuses to seed a live playlist from footage
# older than max_publish_age, which is the point of that guard.
BASE = time.time() - 60.0


def _uploader(tmp_path, **overrides):
    kwargs = dict(
        segment_dir=str(tmp_path),
        storage_zone="zone",
        api_key="key",
        region="la",
        stream_path="prenup-live",
        state_dir=str(tmp_path / "run"),
        segment_duration=SEG,
        index_upload_interval=0,
    )
    kwargs.update(overrides)
    return HLSUploader(**kwargs)


def _write_segments(tmp_path, count, size=75_000, start=0, prefix="s1"):
    names = []
    for i in range(count):
        name = f"{prefix}_{start + i:06d}.ts"
        (tmp_path / name).write_bytes(b"x" * size)
        names.append(name)
    return names


def _write_playlist(tmp_path, names, base=BASE, duration=SEG, start_index=0):
    lines = ["#EXTM3U", "#EXT-X-VERSION:3", f"#EXT-X-TARGETDURATION:{int(duration)}",
             f"#EXT-X-MEDIA-SEQUENCE:{start_index}"]
    for i, name in enumerate(names):
        stamp = time.strftime(
            "%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(base + (start_index + i) * duration)
        )
        lines += [f"#EXT-X-PROGRAM-DATE-TIME:{stamp}", f"#EXTINF:{duration:.3f},", name]
    (tmp_path / "live.m3u8").write_text("\n".join(lines) + "\n")


class _Recorder:
    """Stands in for the transport, recording what was asked for."""

    def __init__(self, fail=()):
        self.files = []
        self.blobs = {}
        self.fail = set(fail)

    def upload_file(self, path, name, content_type):
        if name in self.fail:
            return False
        self.files.append(name)
        return True

    def upload_bytes(self, payload, name, content_type):
        if name in self.fail:
            return False
        self.blobs[name] = payload.decode("utf-8")
        return True


def _run(up, rec, cycles=1):
    with mock.patch.object(up, "_upload_file", side_effect=rec.upload_file), \
         mock.patch.object(up, "_upload_bytes", side_effect=rec.upload_bytes):
        for _ in range(cycles):
            up._sync_once()
    return rec


class TestHappyPath:
    def test_all_segments_uploaded(self, tmp_path):
        names = _write_segments(tmp_path, 4)
        _write_playlist(tmp_path, names)
        up = _uploader(tmp_path)
        rec = _run(up, _Recorder(), cycles=4)
        assert set(rec.files) == set(names)

    def test_published_playlist_moves_forward_in_time(self, tmp_path):
        """Newest-first uploading confirms segments out of order; the playlist
        must still be monotonic, with stragglers left to the DVR index."""
        names = _write_segments(tmp_path, 4)
        _write_playlist(tmp_path, names)
        up = _uploader(tmp_path)
        rec = _run(up, _Recorder(), cycles=4)

        published = parse_playlist(rec.blobs["live.m3u8"])
        assert [e.name for e in published] == sorted(e.name for e in published)
        for name in [e.name for e in published]:
            assert name in up._segment_timestamps

    def test_published_playlist_has_no_endlist(self, tmp_path):
        names = _write_segments(tmp_path, 3)
        _write_playlist(tmp_path, names)
        up = _uploader(tmp_path)
        rec = _run(up, _Recorder(), cycles=3)
        assert "#EXT-X-ENDLIST" not in rec.blobs["live.m3u8"]

    def test_ffmpeg_playlist_is_never_overwritten(self, tmp_path):
        """FFmpeg reads live.m3u8 back on restart; clobbering it breaks numbering."""
        names = _write_segments(tmp_path, 3)
        _write_playlist(tmp_path, names)
        before = (tmp_path / "live.m3u8").read_text()
        up = _uploader(tmp_path)
        _run(up, _Recorder(), cycles=3)
        assert (tmp_path / "live.m3u8").read_text() == before

    def test_live_edge_uploaded_before_history(self, tmp_path):
        old = _write_segments(tmp_path, 20)
        live = old[-3:]
        _write_playlist(tmp_path, live, start_index=17)
        up = _uploader(tmp_path)
        rec = _run(up, _Recorder(), cycles=1)
        assert set(rec.files) <= set(old)
        assert live[-1] in rec.files          # newest live segment went first
        assert old[0] not in rec.files[:2]    # ancient history did not


class TestNoFourOhFours:
    def test_published_playlist_only_names_confirmed_segments(self, tmp_path):
        """The core guarantee: never advertise a segment that isn't on the CDN."""
        names = _write_segments(tmp_path, 8)
        _write_playlist(tmp_path, names)
        up = _uploader(tmp_path)
        rec = _run(up, _Recorder(fail={names[1], names[3], names[6]}), cycles=8)

        published = parse_playlist(rec.blobs["live.m3u8"])
        for entry in published:
            assert entry.name in up._segment_timestamps
        assert names[1] not in [e.name for e in published]

    def test_starved_link_still_advances_the_live_edge(self, tmp_path):
        """One upload per cycle: the playlist must still track the newest video."""
        up = _uploader(tmp_path, live_batch=1)
        rec = _Recorder()
        published_names = []
        for cycle in range(6):
            names = _write_segments(tmp_path, 1, start=cycle)
            _write_playlist(tmp_path, _write_window(tmp_path, cycle), start_index=0)
            _run(up, rec, cycles=1)
            if "live.m3u8" in rec.blobs:
                published_names = [e.name for e in parse_playlist(rec.blobs["live.m3u8"])]
        assert published_names
        assert published_names[-1] == f"s1_{5:06d}.ts"

    def test_discontinuity_marked_when_a_segment_never_lands(self, tmp_path):
        """Feed segments one per cycle as FFmpeg does, failing one mid-stream."""
        up = _uploader(tmp_path, live_batch=1)
        rec = _Recorder(fail={"s1_000003.ts"})
        names = []
        for i in range(6):
            names += _write_segments(tmp_path, 1, start=i)
            _write_playlist(tmp_path, names)
            _run(up, rec, cycles=1)

        published = [e.name for e in parse_playlist(rec.blobs["live.m3u8"])]
        assert "s1_000003.ts" not in published
        assert "s1_000004.ts" in published
        assert "#EXT-X-DISCONTINUITY\n" in rec.blobs["live.m3u8"]


def _write_window(tmp_path, upto):
    names = [f"s1_{i:06d}.ts" for i in range(upto + 1)]
    for name in names:
        path = tmp_path / name
        if not path.exists():
            path.write_bytes(b"x" * 75_000)
    return names


class TestRestartPersistence:
    def test_new_instance_does_not_re_upload(self, tmp_path):
        """Root cause of 'hours-old footage as live': state died with the process."""
        names = _write_segments(tmp_path, 5)
        _write_playlist(tmp_path, names)

        first = _uploader(tmp_path)
        _run(first, _Recorder(), cycles=5)
        first._persist_state(force=True)

        second = _uploader(tmp_path)
        second._load_state()
        rec = _run(second, _Recorder(), cycles=2)

        assert rec.files == []
        for name in names:
            assert name in second._segment_timestamps

    def test_media_sequence_never_goes_backwards(self, tmp_path):
        names = _write_segments(tmp_path, 6)
        _write_playlist(tmp_path, names)
        first = _uploader(tmp_path)
        rec = _run(first, _Recorder(), cycles=6)
        first_seq = parse_playlist(rec.blobs["live.m3u8"])
        assert first_seq
        first._persist_state(force=True)
        before = first._publish_state.next_seq

        second = _uploader(tmp_path)
        second._load_state()
        assert second._publish_state.next_seq >= before

    def test_upgrade_path_seeds_from_segments_json(self, tmp_path):
        """A Pi upgrading has segments.json but no uploader_state.json. It must
        not blast its whole disk buffer at Bunny on first boot."""
        names = _write_segments(tmp_path, 10)
        (tmp_path / "segments.json").write_text(json.dumps({
            "segments": {n: BASE + i * SEG for i, n in enumerate(names)},
            "segment_duration": SEG,
            "updated_at": BASE,
        }))
        _write_playlist(tmp_path, names)

        up = _uploader(tmp_path)
        up._load_state()
        rec = _run(up, _Recorder(), cycles=2)
        assert rec.files == []

    def test_corrupt_state_degrades_to_cold_start(self, tmp_path):
        _write_segments(tmp_path, 2)
        (tmp_path / "uploader_state.json").write_text("{not json")
        up = _uploader(tmp_path)
        up._load_state()  # must not raise
        assert up._segment_timestamps == {}


class TestIndexUploads:
    def test_index_yields_to_live_segments(self, tmp_path):
        names = _write_segments(tmp_path, 4)
        _write_playlist(tmp_path, names)
        up = _uploader(tmp_path)
        rec = _run(up, _Recorder(), cycles=1)
        assert "segments.json" not in rec.blobs

    def test_index_uploaded_once_live_is_caught_up(self, tmp_path):
        names = _write_segments(tmp_path, 3)
        _write_playlist(tmp_path, names)
        up = _uploader(tmp_path)
        rec = _run(up, _Recorder(), cycles=4)
        assert "segments.json" in rec.blobs

    def test_index_not_resent_when_unchanged(self, tmp_path):
        names = _write_segments(tmp_path, 2)
        _write_playlist(tmp_path, names)
        up = _uploader(tmp_path, index_upload_interval=0)
        rec = _run(up, _Recorder(), cycles=4)
        sent = rec.blobs.get("segments.json")
        assert sent is not None
        first_hash = up._last_index_hash
        _run(up, rec, cycles=2)
        assert up._last_index_hash == first_hash

    def test_index_is_a_superset_of_everything_uploaded(self, tmp_path):
        names = _write_segments(tmp_path, 6)
        _write_playlist(tmp_path, names)
        up = _uploader(tmp_path, index_upload_interval=0)
        rec = _run(up, _Recorder(fail={names[2]}), cycles=6)
        index = json.loads(rec.blobs["segments.json"])["segments"]
        for name in rec.files:
            assert name in index


class TestMissingPlaylist:
    def test_backfill_continues_during_ffmpeg_restart_gap(self, tmp_path):
        """engine.stop() deletes live.m3u8; that must not halt uploads."""
        names = _write_segments(tmp_path, 4)
        up = _uploader(tmp_path)
        rec = _run(up, _Recorder(), cycles=4)
        assert rec.files  # backfill ran with no playlist present

    def test_no_playlist_published_without_a_source(self, tmp_path):
        _write_segments(tmp_path, 3)
        up = _uploader(tmp_path)
        rec = _run(up, _Recorder(), cycles=2)
        assert "live.m3u8" not in rec.blobs
