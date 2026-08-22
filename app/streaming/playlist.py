"""Pure HLS playlist logic: parsing, upload scheduling, and playlist rendering.

Deliberately free of I/O and of imports from ``engine`` or ``uploader`` — every
decision the uploader makes on a degraded link lives here as a plain function
over plain data, so it can be tested without a network or a filesystem.

Three concerns:

* :func:`parse_playlist` — read FFmpeg's ``live.m3u8``.
* :func:`plan_uploads` — decide what to upload next when bandwidth is scarce.
* :func:`advance_published` / :func:`render_playlist` — build the playlist we
  publish to the CDN, which lists only segments known to be up there.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Container, Mapping, Optional, Sequence

# ── Parsing ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PlaylistEntry:
    """One segment as it appears in a playlist."""

    name: str
    duration: float
    program_date_time: Optional[float] = None  # epoch seconds, None if absent


def _parse_pdt(value: str) -> Optional[float]:
    """Parse an EXT-X-PROGRAM-DATE-TIME value to epoch seconds."""
    text = value.strip()
    if not text:
        return None
    # fromisoformat didn't accept a trailing "Z" until 3.11, and we target 3.9.
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def parse_playlist(text: str) -> list[PlaylistEntry]:
    """Parse an HLS playlist into its segment entries, oldest first.

    Tolerates CRLF, blank lines, unknown tags and malformed durations, and
    ignores ``#EXT-X-ENDLIST``. A segment line with no preceding ``#EXTINF``
    is skipped rather than guessed at.
    """
    entries: list[PlaylistEntry] = []
    pending_dur: Optional[float] = None
    pending_pdt: Optional[float] = None

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXTINF:"):
            try:
                pending_dur = float(line[len("#EXTINF:"):].split(",", 1)[0])
            except ValueError:
                pending_dur = None
        elif line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
            pending_pdt = _parse_pdt(line[len("#EXT-X-PROGRAM-DATE-TIME:"):])
        elif line.startswith("#"):
            continue
        elif pending_dur is not None:
            entries.append(PlaylistEntry(line, pending_dur, pending_pdt))
            pending_dur = None
            pending_pdt = None

    return entries


# ── Upload scheduling ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class UploadCandidate:
    name: str
    size: int


@dataclass(frozen=True)
class UploadPlan:
    """What to upload this cycle, in the order it should be attempted."""

    live: list[UploadCandidate]      # newest first — the live edge
    backfill: list[UploadCandidate]  # oldest first, at most one entry
    demoted: list[str]               # live-window names too big to make the deadline
    backlog_seconds: float


def plan_uploads(
    *,
    live_names: Sequence[str],
    on_disk: Sequence[UploadCandidate],
    confirmed: Container[str],
    throughput_bps: float,
    now: float,
    last_backfill_at: float,
    segment_duration: float,
    live_batch: int,
    live_catch_up: int,
    live_deadline: float,
    backfill_min_interval: float,
    backfill_suspend_backlog: float,
) -> UploadPlan:
    """Decide what to upload next, favouring the live edge over history.

    ``live_names`` is FFmpeg's own playlist window, oldest first — using it
    rather than a wall-clock cutoff means no clock trust is required. Anything
    on disk and outside that window is backfill.

    On a link that cannot keep up, uploading oldest-first (the historical
    behaviour) spends every byte on footage nobody is waiting for and the live
    edge is never reached. So once the live backlog passes ``live_catch_up`` we
    abandon the middle and work back from the newest segment.

    Below that threshold we are not really behind, and skipping would cost live
    continuity — a visible gap — to save bandwidth we are not short of. So a
    small backlog is worked through in order instead.

    Backfill gets a guaranteed slot only every ``backfill_min_interval`` —
    except when live has nothing pending, when it always gets one.
    """
    by_name = {c.name: c for c in on_disk}
    unconfirmed = [c for c in on_disk if c.name not in confirmed]
    backlog_seconds = len(unconfirmed) * segment_duration

    # ── Live edge ──
    live_pending = []
    for name in live_names:  # chronological
        if name in confirmed:
            continue
        candidate = by_name.get(name)
        if candidate is None:  # named in the playlist but already evicted
            continue
        live_pending.append(candidate)

    live: list[UploadCandidate] = []
    demoted: list[str] = []

    if len(live_pending) <= live_catch_up:
        # Barely behind: work through in order. A brief hiccup on a healthy
        # link must not cost continuity — the published playlist only moves
        # forward, so anything skipped here is lost from live for good.
        live = live_pending[:live_batch]
    else:
        # Genuinely behind: work back from the newest so the live edge is
        # reachable at all, accepting the gap that leaves behind.
        for candidate in reversed(live_pending):
            # Head-of-line guard: one oversized segment can block the live edge
            # for longer than the segment is live-relevant, while newer ones
            # pile up behind it. Demote rather than drop — it stays eligible
            # for backfill.
            if throughput_bps > 0 and candidate.size / throughput_bps > live_deadline:
                demoted.append(candidate.name)
                continue
            live.append(candidate)
            if len(live) >= live_batch:
                break
        # Selection is newest-first, but the *upload* order has to be
        # chronological: confirming a newer segment before an older one strands
        # the older one out of the playlist permanently.
        live.reverse()

    live_window = set(live_names)
    demoted_set = set(demoted)

    # ── Backfill: oldest first, one at a time ──
    backfill: list[UploadCandidate] = []
    pending_live = any(
        c.name in live_window and c.name not in demoted_set for c in unconfirmed
    )
    if pending_live:
        # Suspending backfill above the threshold is only safe while live has
        # work; otherwise a large backlog would stop all uploads permanently.
        due = (
            backfill_min_interval > 0
            and now - last_backfill_at >= backfill_min_interval
            and backlog_seconds < backfill_suspend_backlog
        )
    else:
        due = True

    if due:
        for candidate in unconfirmed:
            if candidate.name in live_window and candidate.name not in demoted_set:
                continue
            if any(c.name == candidate.name for c in live):
                continue
            backfill.append(candidate)
            break

    return UploadPlan(
        live=live,
        backfill=backfill,
        demoted=demoted,
        backlog_seconds=backlog_seconds,
    )


# ── Published playlist ────────────────────────────────────────────────────


@dataclass(frozen=True)
class PublishedEntry:
    name: str
    duration: float
    start_epoch: float
    discontinuity: bool  # emit #EXT-X-DISCONTINUITY before this entry
    seq: int             # media sequence number


@dataclass(frozen=True)
class PublishState:
    entries: tuple[PublishedEntry, ...] = ()
    next_seq: int = 0
    disc_seq: int = 0


def advance_published(
    state: PublishState,
    source: Sequence[PlaylistEntry],
    *,
    confirmed_times: Mapping[str, float],
    window: int,
    segment_duration: float,
    max_publish_age: float,
    now: float,
    gap_factor: float = 0.5,
) -> PublishState:
    """Append newly confirmed segments to the published window.

    Only segments present in ``confirmed_times`` — i.e. known to be on the CDN
    — are ever published, so the playlist can never advertise a 404. Segments
    that never made it are simply skipped; the resulting time gap is marked
    with a discontinuity rather than being papered over.

    ``max_publish_age`` guards seeding: when the window is empty we refuse to
    start from footage older than that, which makes "hours-old video presented
    as live" structurally impossible even if persisted state is lost.

    Discontinuity is judged on dead air — the space between one segment ending
    and the next beginning — rather than on start-to-start spacing, which would
    fire spuriously whenever segment durations vary (they follow the camera's
    keyframe interval, not our requested length). ``gap_factor`` of 0.5 means
    any genuinely missing segment is marked, while absorbing timing jitter.
    """
    entries = list(state.entries)
    next_seq = state.next_seq
    disc_seq = state.disc_seq
    published = {e.name for e in entries}

    for item in source:
        if item.name in published:
            continue
        start = confirmed_times.get(item.name)
        if start is None:  # not on the CDN — skip, do not publish a 404
            continue
        if not entries and now - start > max_publish_age:
            continue  # too stale to seed a fresh window
        if entries and start <= entries[-1].start_epoch:
            # Arrived after a newer segment was already published. A live
            # playlist must move forward in time, so this one belongs to the
            # DVR index only — it is still on the CDN, just not re-inserted.
            continue

        discontinuity = False
        if entries:
            last = entries[-1]
            gap = start - (last.start_epoch + last.duration)
            discontinuity = gap > gap_factor * segment_duration

        entries.append(
            PublishedEntry(
                name=item.name,
                duration=item.duration,
                start_epoch=start,
                discontinuity=discontinuity,
                seq=next_seq,
            )
        )
        published.add(item.name)
        next_seq += 1

    # Evict from the head, carrying discontinuities into DISCONTINUITY-SEQUENCE.
    while len(entries) > window:
        evicted = entries.pop(0)
        if evicted.discontinuity:
            disc_seq += 1

    # The first entry's own discontinuity flag is meaningless once it heads the
    # window — there is nothing before it to be discontinuous with.
    if entries and entries[0].discontinuity:
        entries[0] = replace(entries[0], discontinuity=False)

    return PublishState(entries=tuple(entries), next_seq=next_seq, disc_seq=disc_seq)


def _iso8601(epoch: float) -> str:
    dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def render_playlist(state: PublishState, *, target_duration: float) -> str:
    """Render the published window as a live HLS playlist.

    Never emits ``#EXT-X-ENDLIST`` — the player treats that as "stream over"
    and goes offline. There is deliberately no code path that produces it.
    """
    if not state.entries:
        return ""

    longest = max(e.duration for e in state.entries)
    target = int(math.ceil(max(target_duration, longest)))

    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:6",
        f"#EXT-X-TARGETDURATION:{target}",
        f"#EXT-X-MEDIA-SEQUENCE:{state.entries[0].seq}",
        f"#EXT-X-DISCONTINUITY-SEQUENCE:{state.disc_seq}",
    ]

    for entry in state.entries:
        if entry.discontinuity:
            lines.append("#EXT-X-DISCONTINUITY")
        lines.append(f"#EXT-X-PROGRAM-DATE-TIME:{_iso8601(entry.start_epoch)}")
        lines.append(f"#EXTINF:{entry.duration:.3f},")
        lines.append(entry.name)

    return "\n".join(lines) + "\n"
