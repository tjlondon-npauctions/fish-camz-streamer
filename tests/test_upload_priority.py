"""Tests for upload scheduling on a bandwidth-starved link."""

from app.streaming.playlist import UploadCandidate, plan_uploads

SEG = 6.0
NOW = 1_787_000_000.0
SMALL = 75_000      # a healthy 6s segment at ~100kbps
HUGE = 3_300_000    # what a 4.4Mbps camera produces
LINK = 43_750.0     # bytes/sec at ~350kbps


def _disk(count, size=SMALL, start=0, prefix="s1"):
    return [UploadCandidate(f"{prefix}_{start + i:06d}.ts", size) for i in range(count)]


def _plan(disk, live_names, confirmed=(), **overrides):
    kwargs = dict(
        live_names=live_names,
        on_disk=disk,
        confirmed=set(confirmed),
        throughput_bps=0.0,
        now=NOW,
        last_backfill_at=NOW,
        segment_duration=SEG,
        live_batch=2,
        live_deadline=30.0,
        backfill_min_interval=120.0,
        backfill_suspend_backlog=900.0,
    )
    kwargs.update(overrides)
    return plan_uploads(**kwargs)


class TestLiveOrdering:
    def test_live_window_selects_the_newest_segments(self):
        """Selection favours the live edge over history."""
        disk = _disk(10)
        live = [c.name for c in disk[-4:]]
        plan = _plan(disk, live)
        assert {c.name for c in plan.live} == {disk[-1].name, disk[-2].name}

    def test_live_uploads_run_in_chronological_order(self):
        """Confirming a newer segment first would strand the older one out of
        the playlist permanently — a dropped segment and a visible stutter."""
        disk = _disk(10)
        plan = _plan(disk, [c.name for c in disk[-4:]], live_batch=3)
        names = [c.name for c in plan.live]
        assert names == sorted(names)

    def test_live_batch_caps_the_round(self):
        disk = _disk(10)
        plan = _plan(disk, [c.name for c in disk], live_batch=3)
        assert len(plan.live) == 3

    def test_confirmed_segments_skipped(self):
        disk = _disk(4)
        plan = _plan(disk, [c.name for c in disk], confirmed=[disk[-1].name])
        names = [c.name for c in plan.live]
        assert disk[-1].name not in names
        assert disk[-2].name in names

    def test_segment_named_but_evicted_is_skipped(self):
        disk = _disk(2)
        plan = _plan(disk, ["s1_000000.ts", "s1_000001.ts", "s1_000099.ts"])
        assert "s1_000099.ts" not in [c.name for c in plan.live]


class TestHeadOfLineGuard:
    def test_oversized_segment_demoted(self):
        """3.3MB at 43KB/s is 75s — it would block the live edge outright."""
        disk = _disk(3, size=HUGE)
        plan = _plan(disk, [c.name for c in disk], throughput_bps=LINK)
        assert plan.live == []
        assert set(plan.demoted) == {c.name for c in disk}

    def test_not_demoted_when_throughput_unknown(self):
        disk = _disk(3, size=HUGE)
        plan = _plan(disk, [c.name for c in disk], throughput_bps=0.0)
        assert plan.demoted == []
        assert len(plan.live) == 2

    def test_not_demoted_when_link_is_fast(self):
        disk = _disk(3, size=HUGE)
        plan = _plan(disk, [c.name for c in disk], throughput_bps=10_000_000.0)
        assert plan.demoted == []

    def test_demoted_segment_still_reachable_by_backfill(self):
        """Demotion must never mean data loss."""
        disk = _disk(2, size=HUGE)
        plan = _plan(disk, [c.name for c in disk], throughput_bps=LINK)
        assert plan.live == []
        assert len(plan.backfill) == 1


class TestBackfill:
    def test_backfill_runs_when_live_is_idle(self):
        disk = _disk(5)
        live = [c.name for c in disk[-2:]]
        plan = _plan(disk, live, confirmed=[c.name for c in disk[-2:]])
        assert len(plan.backfill) == 1
        assert plan.backfill[0].name == disk[0].name

    def test_backfill_suppressed_while_live_pending_and_recent(self):
        disk = _disk(5)
        plan = _plan(disk, [c.name for c in disk[-2:]], last_backfill_at=NOW)
        assert plan.backfill == []

    def test_backfill_gets_a_slot_after_the_interval(self):
        disk = _disk(5)
        plan = _plan(disk, [c.name for c in disk[-2:]], last_backfill_at=NOW - 200.0)
        assert len(plan.backfill) == 1

    def test_backfill_suspended_when_backlog_is_hopeless(self):
        disk = _disk(300)
        plan = _plan(disk, [c.name for c in disk[-2:]], last_backfill_at=NOW - 5000.0)
        assert plan.backlog_seconds > 900.0
        assert plan.backfill == []

    def test_huge_backlog_still_uploads_when_live_is_idle(self):
        """Otherwise a big backlog would stop every upload forever."""
        disk = _disk(300)
        live = [c.name for c in disk[-2:]]
        plan = _plan(disk, live, confirmed=[c.name for c in disk[-2:]],
                     last_backfill_at=NOW - 5000.0)
        assert len(plan.backfill) == 1

    def test_backfill_is_oldest_first(self):
        disk = _disk(5)
        plan = _plan(disk, [], last_backfill_at=NOW - 200.0)
        assert plan.backfill[0].name == disk[0].name

    def test_no_segment_appears_in_both_lists(self):
        disk = _disk(6)
        plan = _plan(disk, [c.name for c in disk], last_backfill_at=NOW - 200.0)
        assert not ({c.name for c in plan.live} & {c.name for c in plan.backfill})


class TestBacklog:
    def test_backlog_seconds_counts_unconfirmed_only(self):
        disk = _disk(10)
        plan = _plan(disk, [], confirmed=[c.name for c in disk[:4]])
        assert plan.backlog_seconds == 6 * SEG

    def test_backlog_zero_when_all_confirmed(self):
        disk = _disk(4)
        plan = _plan(disk, [], confirmed=[c.name for c in disk])
        assert plan.backlog_seconds == 0.0
        assert plan.live == []
        assert plan.backfill == []
