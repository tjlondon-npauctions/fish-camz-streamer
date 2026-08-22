"""Tests for HLS playlist parsing (app/streaming/playlist.py)."""

from app.streaming.playlist import parse_playlist


def _playlist(*body_lines):
    header = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        "#EXT-X-TARGETDURATION:6",
        "#EXT-X-MEDIA-SEQUENCE:0",
    ]
    return "\n".join(header + list(body_lines)) + "\n"


class TestParsePlaylist:
    def test_basic(self):
        text = _playlist("#EXTINF:6.000,", "s1_000000.ts", "#EXTINF:6.000,", "s1_000001.ts")
        entries = parse_playlist(text)
        assert [e.name for e in entries] == ["s1_000000.ts", "s1_000001.ts"]
        assert entries[0].duration == 6.0

    def test_crlf(self):
        text = "#EXTM3U\r\n#EXTINF:6.000,\r\ns1_000000.ts\r\n"
        entries = parse_playlist(text)
        assert len(entries) == 1
        assert entries[0].name == "s1_000000.ts"

    def test_blank_lines_ignored(self):
        text = _playlist("", "#EXTINF:6.000,", "", "s1_000000.ts", "")
        assert len(parse_playlist(text)) == 1

    def test_program_date_time_captured(self):
        text = _playlist(
            "#EXT-X-PROGRAM-DATE-TIME:2026-08-22T00:30:30.000Z",
            "#EXTINF:6.000,",
            "s1_000000.ts",
        )
        entries = parse_playlist(text)
        assert entries[0].program_date_time is not None
        # 2026-08-22T00:30:30Z
        assert abs(entries[0].program_date_time - 1787358630.0) < 1.0

    def test_program_date_time_absent_is_none(self):
        text = _playlist("#EXTINF:6.000,", "s1_000000.ts")
        assert parse_playlist(text)[0].program_date_time is None

    def test_program_date_time_not_carried_to_next_segment(self):
        text = _playlist(
            "#EXT-X-PROGRAM-DATE-TIME:2026-08-22T00:30:30.000Z",
            "#EXTINF:6.000,",
            "s1_000000.ts",
            "#EXTINF:6.000,",
            "s1_000001.ts",
        )
        entries = parse_playlist(text)
        assert entries[0].program_date_time is not None
        assert entries[1].program_date_time is None

    def test_malformed_pdt_skipped(self):
        text = _playlist(
            "#EXT-X-PROGRAM-DATE-TIME:not-a-date",
            "#EXTINF:6.000,",
            "s1_000000.ts",
        )
        entries = parse_playlist(text)
        assert len(entries) == 1
        assert entries[0].program_date_time is None

    def test_endlist_ignored(self):
        text = _playlist("#EXTINF:6.000,", "s1_000000.ts", "#EXT-X-ENDLIST")
        entries = parse_playlist(text)
        assert len(entries) == 1
        assert entries[0].name == "s1_000000.ts"

    def test_malformed_extinf_skips_segment(self):
        """A segment with an unparseable duration is dropped, not guessed at."""
        text = _playlist("#EXTINF:abc,", "s1_000000.ts", "#EXTINF:6.000,", "s1_000001.ts")
        entries = parse_playlist(text)
        assert [e.name for e in entries] == ["s1_000001.ts"]

    def test_segment_without_extinf_skipped(self):
        text = _playlist("s1_000000.ts")
        assert parse_playlist(text) == []

    def test_unknown_tags_ignored(self):
        text = _playlist("#EXT-X-INDEPENDENT-SEGMENTS", "#EXTINF:6.000,", "s1_000000.ts")
        assert len(parse_playlist(text)) == 1

    def test_empty_input(self):
        assert parse_playlist("") == []

    def test_header_only(self):
        assert parse_playlist(_playlist()) == []
