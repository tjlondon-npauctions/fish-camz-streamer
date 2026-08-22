"""Tests for the published-playlist state machine (app/streaming/playlist.py)."""

from app.streaming.playlist import (
    PlaylistEntry,
    PublishState,
    advance_published,
    render_playlist,
)

BASE = 1_787_000_000.0
SEG = 6.0


def _source(count, start=0, base=BASE, step=SEG, prefix="s1"):
    """FFmpeg-style playlist entries, contiguous in time."""
    return [
        PlaylistEntry(f"{prefix}_{start + i:06d}.ts", SEG, base + (start + i) * step)
        for i in range(count)
    ]


def _confirmed(entries):
    return {e.name: e.program_date_time for e in entries}


def _advance(state, source, *, confirmed=None, window=10, now=None, max_age=600.0):
    times = _confirmed(source) if confirmed is None else confirmed
    latest = max((t for t in times.values()), default=BASE)
    return advance_published(
        state,
        source,
        confirmed_times=times,
        window=window,
        segment_duration=SEG,
        max_publish_age=max_age,
        now=latest + 1.0 if now is None else now,
    )


class TestAdvancePublished:
    def test_appends_confirmed_segments(self):
        src = _source(3)
        state = _advance(PublishState(), src)
        assert [e.name for e in state.entries] == [e.name for e in src]
        assert [e.seq for e in state.entries] == [0, 1, 2]

    def test_unconfirmed_segments_never_published(self):
        """The whole point: the playlist can never advertise a 404."""
        src = _source(4)
        confirmed = _confirmed(src)
        del confirmed[src[1].name]
        del confirmed[src[2].name]
        state = _advance(PublishState(), src, confirmed=confirmed)
        assert [e.name for e in state.entries] == [src[0].name, src[3].name]

    def test_skipped_segments_produce_discontinuity(self):
        src = _source(4)
        confirmed = _confirmed(src)
        del confirmed[src[1].name]
        del confirmed[src[2].name]
        state = _advance(PublishState(), src, confirmed=confirmed)
        assert state.entries[0].discontinuity is False
        assert state.entries[1].discontinuity is True

    def test_no_discontinuity_when_time_contiguous(self):
        state = _advance(PublishState(), _source(5))
        assert all(not e.discontinuity for e in state.entries)

    def test_variable_durations_are_not_a_discontinuity(self):
        """Segment length follows the camera's keyframe interval, so alternating
        long/short segments are normal and must not be marked as gaps."""
        src = [
            PlaylistEntry("s1_000000.ts", 12.0, BASE),
            PlaylistEntry("s1_000001.ts", 6.0, BASE + 12.0),
            PlaylistEntry("s1_000002.ts", 12.0, BASE + 18.0),
        ]
        state = _advance(PublishState(), src)
        assert len(state.entries) == 3
        assert all(not e.discontinuity for e in state.entries)

    def test_single_missing_segment_is_a_discontinuity(self):
        src = _source(3)
        confirmed = _confirmed(src)
        del confirmed[src[1].name]
        state = _advance(PublishState(), src, confirmed=confirmed)
        assert state.entries[1].discontinuity is True

    def test_name_jump_alone_is_not_a_discontinuity(self):
        """Names restart on every FFmpeg session; only time gaps matter."""
        first = _source(2, prefix="s1")
        second = [
            PlaylistEntry("s2_000000.ts", SEG, first[-1].program_date_time + SEG),
        ]
        src = first + second
        state = _advance(PublishState(), src)
        assert [e.name for e in state.entries][-1] == "s2_000000.ts"
        assert state.entries[-1].discontinuity is False

    def test_first_entry_never_carries_discontinuity(self):
        src = _source(2)
        confirmed = _confirmed(src)
        del confirmed[src[0].name]
        state = _advance(PublishState(), src, confirmed=confirmed)
        assert state.entries[0].discontinuity is False

    def test_window_evicts_from_head(self):
        state = _advance(PublishState(), _source(15), window=10)
        assert len(state.entries) == 10
        assert state.entries[0].seq == 5

    def test_media_sequence_advances_with_eviction(self):
        state = _advance(PublishState(), _source(10), window=10)
        assert state.entries[0].seq == 0
        state = _advance(state, _source(3, start=10), window=10)
        assert state.entries[0].seq == 3

    def test_discontinuity_sequence_increments_on_eviction(self):
        src = _source(12)
        confirmed = _confirmed(src)
        del confirmed[src[1].name]  # gap -> entry 2 carries a discontinuity
        state = _advance(PublishState(), src, confirmed=confirmed, window=3)
        assert state.disc_seq >= 1

    def test_next_seq_monotonic_across_restart(self):
        """A restarted uploader must not send the media sequence backwards."""
        state = _advance(PublishState(), _source(5))
        resumed = PublishState(entries=(), next_seq=state.next_seq, disc_seq=state.disc_seq)
        after = _advance(resumed, _source(3, start=100, base=BASE + 600))
        assert after.entries[0].seq >= state.next_seq

    def test_already_published_not_duplicated(self):
        src = _source(3)
        state = _advance(PublishState(), src)
        state = _advance(state, src)
        assert len(state.entries) == 3

    def test_stale_footage_does_not_seed_empty_window(self):
        """Guards against hours-old video being presented as live."""
        src = _source(3)
        state = _advance(PublishState(), src, now=BASE + 10_000)
        assert state.entries == ()

    def test_fresh_footage_seeds_normally(self):
        src = _source(3)
        state = _advance(PublishState(), src, now=src[-1].program_date_time + 5)
        assert len(state.entries) == 3

    def test_max_publish_age_only_gates_seeding(self):
        """Once running, an older confirmed segment still appends."""
        src = _source(3)
        state = _advance(PublishState(), src)
        more = _source(1, start=3)
        state = _advance(state, more, now=more[0].program_date_time + 10_000)
        assert len(state.entries) == 4


class TestRenderPlaylist:
    def test_structure(self):
        state = _advance(PublishState(), _source(3))
        text = render_playlist(state, target_duration=SEG)
        assert text.startswith("#EXTM3U\n")
        assert "#EXT-X-VERSION:6" in text
        assert "#EXT-X-MEDIA-SEQUENCE:0" in text
        assert "#EXT-X-DISCONTINUITY-SEQUENCE:0" in text
        assert text.count("#EXTINF:") == 3

    def test_media_sequence_matches_first_entry(self):
        state = _advance(PublishState(), _source(14), window=10)
        text = render_playlist(state, target_duration=SEG)
        assert f"#EXT-X-MEDIA-SEQUENCE:{state.entries[0].seq}" in text

    def test_target_duration_covers_longest_segment(self):
        src = [PlaylistEntry("s1_000000.ts", 10.4, BASE)]
        state = _advance(PublishState(), src)
        text = render_playlist(state, target_duration=SEG)
        assert "#EXT-X-TARGETDURATION:11" in text

    def test_program_date_time_present_and_increasing(self):
        state = _advance(PublishState(), _source(3))
        text = render_playlist(state, target_duration=SEG)
        stamps = [l for l in text.splitlines() if l.startswith("#EXT-X-PROGRAM-DATE-TIME:")]
        assert len(stamps) == 3
        assert all(s.endswith("Z") for s in stamps)
        assert stamps == sorted(stamps)

    def test_discontinuity_tag_emitted(self):
        src = _source(3)
        confirmed = _confirmed(src)
        del confirmed[src[1].name]
        state = _advance(PublishState(), src, confirmed=confirmed)
        text = render_playlist(state, target_duration=SEG)
        assert "#EXT-X-DISCONTINUITY\n" in text

    def test_no_discontinuity_tag_when_contiguous(self):
        state = _advance(PublishState(), _source(4))
        text = render_playlist(state, target_duration=SEG)
        assert "#EXT-X-DISCONTINUITY\n" not in text

    def test_never_emits_endlist(self):
        """ENDLIST makes the player treat the stream as finished."""
        for count in (0, 1, 5, 10):
            state = _advance(PublishState(), _source(count))
            text = render_playlist(state, target_duration=SEG)
            assert "#EXT-X-ENDLIST" not in text

    def test_empty_state_renders_nothing(self):
        assert render_playlist(PublishState(), target_duration=SEG) == ""

    def test_short_window_still_valid(self):
        """On a slow link we publish 1-5 entries rather than waiting."""
        for count in (1, 2, 5):
            state = _advance(PublishState(), _source(count))
            text = render_playlist(state, target_duration=SEG)
            assert text.startswith("#EXTM3U")
            assert text.count("#EXTINF:") == count
            assert text.rstrip().endswith(".ts")

    def test_relative_filenames(self):
        state = _advance(PublishState(), _source(2))
        text = render_playlist(state, target_duration=SEG)
        assert "https://" not in text
