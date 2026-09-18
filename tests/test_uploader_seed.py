"""The DVR index must survive a restart.

``_upload_segment_index`` republishes ``segments.json`` wholesale from the
uploader's in-memory map. Without seeding, every FFmpeg restart would replace
the CDN's index with only what the new process had uploaded, erasing the DVR
timeline for older footage that is still on Bunny.
"""

import json

import pytest

from app.streaming.uploader import HLSUploader


def _uploader(tmp_path):
    return HLSUploader(
        segment_dir=str(tmp_path),
        storage_zone="zone",
        api_key="key",
        stream_path="vessel",
    )


def _write_index(tmp_path, segments):
    (tmp_path / "segments.json").write_text(
        json.dumps({"segments": segments, "segment_duration": 6})
    )


class TestSeedTimestamps:
    def test_loads_previous_index(self, tmp_path):
        _write_index(tmp_path, {"s1_000001.ts": 1000.0, "s1_000002.ts": 1010.0})
        up = _uploader(tmp_path)
        up._seed_timestamps()
        assert up._segment_timestamps == {
            "s1_000001.ts": 1000.0,
            "s1_000002.ts": 1010.0,
        }

    def test_missing_file_is_not_an_error(self, tmp_path):
        up = _uploader(tmp_path)
        up._seed_timestamps()
        assert up._segment_timestamps == {}

    def test_corrupt_json_is_not_an_error(self, tmp_path):
        (tmp_path / "segments.json").write_text("{not json")
        up = _uploader(tmp_path)
        up._seed_timestamps()
        assert up._segment_timestamps == {}

    @pytest.mark.parametrize("payload", ['{"segments": []}', '{"segments": null}', "{}"])
    def test_unexpected_shapes_are_ignored(self, tmp_path, payload):
        (tmp_path / "segments.json").write_text(payload)
        up = _uploader(tmp_path)
        up._seed_timestamps()
        assert up._segment_timestamps == {}

    def test_malformed_entries_are_skipped_not_fatal(self, tmp_path):
        _write_index(tmp_path, {"good.ts": 1000.0, "bad.ts": "not-a-number"})
        up = _uploader(tmp_path)
        up._seed_timestamps()
        assert up._segment_timestamps == {"good.ts": 1000.0}


class TestIndexSurvivesRestart:
    def test_republished_index_keeps_history_and_adds_new(self, tmp_path):
        """The regression this guards: old entries must not be dropped."""
        _write_index(tmp_path, {"old_000001.ts": 1000.0, "old_000002.ts": 1010.0})

        up = _uploader(tmp_path)
        up._seed_timestamps()
        up._segment_timestamps["new_000001.ts"] = 2000.0

        uploaded = []
        up._upload_file = lambda path, name, ctype: uploaded.append(name) or True
        up._upload_segment_index()

        assert uploaded == ["segments.json"]
        written = json.loads((tmp_path / "segments.json").read_text())["segments"]
        assert written == {
            "old_000001.ts": 1000.0,
            "old_000002.ts": 1010.0,
            "new_000001.ts": 2000.0,
        }
