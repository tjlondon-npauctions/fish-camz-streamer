"""The live playlist must keep publishing while a backlog uploads.

After a multi-hour outage the Pi has ~1000 buffered segments. The uploader
used to push every one of them before re-uploading ``live.m3u8``, so the CDN
playlist stayed frozen (black player) for the whole catch-up — and each
restart began the pass again. Vendetta 2, 2026-09-27.
"""

from app.streaming.uploader import HLSUploader


def _uploader(tmp_path):
    up = HLSUploader(
        segment_dir=str(tmp_path),
        storage_zone="zone",
        api_key="key",
        stream_path="vessel",
    )
    up._calls = []
    up._fail = set()

    def fake_upload(local_path, remote_name, content_type):
        up._calls.append(remote_name)
        return remote_name not in up._fail

    up._upload_file = fake_upload
    return up


def _setup(tmp_path, backlog, live):
    for name in backlog + live:
        (tmp_path / name).write_bytes(b"x")
    lines = ["#EXTM3U", "#EXT-X-TARGETDURATION:8"]
    for name in live:
        lines += ["#EXTINF:6.0,", name]
    (tmp_path / "live.m3u8").write_text("\n".join(lines) + "\n")


BACKLOG = [f"s100_{i:06d}.ts" for i in range(5)]
LIVE = ["s200_000000.ts", "s200_000001.ts"]


class TestLiveEdgeFirst:
    def test_playlist_published_before_backlog(self, tmp_path):
        _setup(tmp_path, BACKLOG, LIVE)
        up = _uploader(tmp_path)
        up._sync_once()
        assert up._calls[:3] == LIVE + ["live.m3u8"]
        assert up._calls[3:8] == BACKLOG  # backlog still drains, oldest first

    def test_backlog_budget_bounds_the_cycle(self, tmp_path):
        _setup(tmp_path, BACKLOG, LIVE)
        up = _uploader(tmp_path)
        up._backlog_budget_seconds = 0
        up._sync_once()
        assert up._calls[:3] == LIVE + ["live.m3u8"]
        assert not set(BACKLOG) & set(up._calls)

        # Next cycles still republish the playlist, and nothing is re-sent
        up._backlog_budget_seconds = 10
        up._calls.clear()
        up._sync_once()
        assert up._calls[0] == "live.m3u8"
        assert up._calls[1:] == BACKLOG

    def test_playlist_withheld_if_a_live_segment_fails(self, tmp_path):
        _setup(tmp_path, [], LIVE)
        up = _uploader(tmp_path)
        up._fail = {LIVE[1]}
        up._sync_once()
        assert "live.m3u8" not in up._calls

        up._fail = set()
        up._calls.clear()
        up._sync_once()
        assert up._calls == [LIVE[1], "live.m3u8"]

    def test_segment_deleted_mid_cycle_is_skipped(self, tmp_path):
        _setup(tmp_path, BACKLOG, LIVE)
        up = _uploader(tmp_path)
        real = up._upload_file

        def delete_next(local_path, remote_name, content_type):
            if remote_name == BACKLOG[0]:
                (tmp_path / BACKLOG[1]).unlink()
            return real(local_path, remote_name, content_type)

        up._upload_file = delete_next
        up._sync_once()
        assert BACKLOG[1] not in up._calls
        assert BACKLOG[2] in up._calls

    def test_uploaded_segments_are_indexed(self, tmp_path):
        _setup(tmp_path, BACKLOG, LIVE)
        up = _uploader(tmp_path)
        up._sync_once()
        assert set(BACKLOG + LIVE) <= set(up._segment_timestamps)


def _age(tmp_path, name, seconds):
    import os
    import time as _t
    t = _t.time() - seconds
    os.utime(tmp_path / name, (t, t))


class TestCatchUpPolicy:
    def test_old_unsent_segments_are_skipped(self, tmp_path):
        _setup(tmp_path, BACKLOG, LIVE)
        for name in BACKLOG[:3]:
            _age(tmp_path, name, 20 * 60)
        up = _uploader(tmp_path)
        up._sync_once()
        assert not any((tmp_path / n).exists() for n in BACKLOG[:3])
        assert not set(BACKLOG[:3]) & set(up._calls)
        assert [c for c in up._calls if c in BACKLOG] == BACKLOG[3:]
        assert up.get_status()["backlog_skipped_count"] == 3

    def test_live_segments_are_never_skipped(self, tmp_path):
        _setup(tmp_path, [], LIVE)
        for name in LIVE:
            _age(tmp_path, name, 60 * 60)
        up = _uploader(tmp_path)
        up._sync_once()
        assert up._calls[:3] == LIVE + ["live.m3u8"]
        assert all((tmp_path / n).exists() for n in LIVE)

    def test_none_uploads_everything(self, tmp_path):
        _setup(tmp_path, BACKLOG, LIVE)
        _age(tmp_path, BACKLOG[0], 5 * 60 * 60)
        up = _uploader(tmp_path)
        up._catch_up_minutes = None
        up._sync_once()
        assert BACKLOG[0] in up._calls


class TestDashboardStats:
    def test_backlog_is_measured(self, tmp_path):
        _setup(tmp_path, BACKLOG, LIVE)
        _age(tmp_path, BACKLOG[0], 120)
        up = _uploader(tmp_path)
        up._backlog_budget_seconds = 0
        up._sync_once()
        status = up.get_status()
        assert status["backlog_segments"] == len(BACKLOG)
        assert status["backlog_bytes"] == len(BACKLOG)  # 1 byte each
        assert status["backlog_oldest_mtime"] == (tmp_path / BACKLOG[0]).stat().st_mtime
        assert status["playlist_published_at"] > 0

        up._backlog_budget_seconds = 10
        up._sync_once()
        assert up.get_status()["backlog_segments"] == 0

    def test_playlist_time_only_moves_on_success(self, tmp_path):
        _setup(tmp_path, [], LIVE)
        up = _uploader(tmp_path)
        up._fail = {"live.m3u8"}
        up._sync_once()
        assert up.get_status()["playlist_published_at"] == 0


class TestLinkReporting:
    def test_status_carries_link_and_stream_bitrate(self, tmp_path):
        _setup(tmp_path, [], LIVE)
        up = _uploader(tmp_path)
        up._sync_once()
        # 1 byte per 6s segment
        assert up._stream_kbps == 2 * 8 / 12.0 / 1000
        assert "link" in up.get_status()

    def test_real_upload_path_records_outcomes(self, tmp_path):
        import requests

        class Resp:
            def __init__(self, code):
                self.status_code = code
                self.text = ""

        class Session:
            def __init__(self):
                self.outcomes = [Resp(201), Resp(503), requests.exceptions.ReadTimeout()]

            def put(self, url, data, headers, timeout):
                data.read()
                o = self.outcomes.pop(0)
                if isinstance(o, Exception):
                    raise o
                return o

        up = HLSUploader(segment_dir=str(tmp_path), storage_zone="z", api_key="k")
        up._session = Session()
        up._requests = requests
        seg = tmp_path / "s1_000000.ts"
        seg.write_bytes(b"x" * 200_000)

        assert up._upload_file(seg, seg.name, "video/mp2t") is True
        assert up._upload_file(seg, seg.name, "video/mp2t") is False
        assert up._upload_file(seg, seg.name, "video/mp2t") is False
        kinds = [e.kind for e in up._link_events]
        assert kinds == ["ok", "bunny", "slow"]
        assert up._link_events[0].bytes == 200_000
        assert up.get_status()["link"]["failures"] == {"bunny": 1, "slow": 1}


class TestFailingLink:
    def test_backlog_stops_at_first_failure(self, tmp_path):
        _setup(tmp_path, BACKLOG, LIVE)
        up = _uploader(tmp_path)
        up._fail = {BACKLOG[1]}
        assert up._sync_once() is False
        backlog_calls = [c for c in up._calls if c in BACKLOG]
        assert backlog_calls == BACKLOG[:2]  # tried 0 (ok) and 1 (failed), then stopped

    def test_no_backlog_when_live_edge_fails(self, tmp_path):
        _setup(tmp_path, BACKLOG, LIVE)
        up = _uploader(tmp_path)
        up._fail = {LIVE[0]}
        assert up._sync_once() is False
        assert up._calls == [LIVE[0]]

    def test_healthy_cycle_reports_success(self, tmp_path):
        _setup(tmp_path, BACKLOG, LIVE)
        assert _uploader(tmp_path)._sync_once() is True


class TestRestart:
    def test_segments_uploaded_by_a_previous_run_are_not_resent(self, tmp_path):
        import json
        _setup(tmp_path, BACKLOG, LIVE)
        # Previous run uploaded the first three backlog segments
        (tmp_path / "segments.json").write_text(json.dumps(
            {"segments": {n: 1000.0 for n in BACKLOG[:3]}}))
        up = _uploader(tmp_path)
        up._seed_timestamps()
        up._sync_once()
        assert not set(BACKLOG[:3]) & set(up._calls)
        assert set(BACKLOG[3:]) <= set(up._calls)

    def test_old_buffer_from_previous_run_is_not_counted_as_skipped(self, tmp_path):
        import json
        _setup(tmp_path, BACKLOG, LIVE)
        for name in BACKLOG:
            _age(tmp_path, name, 30 * 60)
        (tmp_path / "segments.json").write_text(json.dumps(
            {"segments": {n: 1000.0 for n in BACKLOG}}))
        up = _uploader(tmp_path)
        up._seed_timestamps()
        up._sync_once()
        assert up.get_status()["backlog_skipped_count"] == 0
        assert up.get_status()["backlog_segments"] == 0


class TestBacklogMeasure:
    def test_counts_segments_written_during_the_cycle(self, tmp_path):
        _setup(tmp_path, [], LIVE)
        up = _uploader(tmp_path)
        real = up._upload_file

        def slow_link(local_path, remote_name, content_type):
            # FFmpeg finishes two more segments while this upload runs
            if remote_name == "live.m3u8":
                for n in ("s200_000002.ts", "s200_000003.ts", "s200_000004.ts"):
                    (tmp_path / n).write_bytes(b"x")
            return real(local_path, remote_name, content_type)

        up._upload_file = slow_link
        up._sync_once()
        # 000002 and 000003 are waiting; 000004 (newest, unlisted) is still being written
        assert up.get_status()["backlog_segments"] == 2
        assert up.get_status()["backlog_behind_live"] == 2  # not in the playlist either

    def test_behind_live_excludes_playlist_segments(self, tmp_path):
        _setup(tmp_path, BACKLOG, LIVE)
        up = _uploader(tmp_path)
        up._fail = {"live.m3u8"}  # nothing after the live edge gets sent
        up._sync_once()
        status = up.get_status()
        assert status["backlog_segments"] == len(BACKLOG)
        assert status["backlog_behind_live"] == len(BACKLOG)
