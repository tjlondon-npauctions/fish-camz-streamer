"""getHistory parsing, checked against Vendetta's real output (2026-09-29)."""

from app.starlink.history import (
    GPS_TO_UNIX_S, NOTABLE_OUTAGE_S, for_heartbeat, outage_events, recent_window,
)

# Excerpt of Vendetta's eventLog (Unix ns) and outages (GPS ns) — the first
# outage below is the same event as the 1790614511.96 log entry.
EVENT_LOG = {"events": [
    {"severity": "EVENT_SEVERITY_ADVISORY", "reason": "EVENT_REASON_OUTAGE_BOOTING",
     "startTimestampNs": "1790548950698976444", "durationNs": "59141387999"},
    {"severity": "EVENT_SEVERITY_WARNING", "reason": "EVENT_REASON_OUTAGE_NO_PINGS",
     "startTimestampNs": "1790614511960339711", "durationNs": "940323728"},
    {"severity": "EVENT_SEVERITY_WARNING", "reason": "EVENT_REASON_OUTAGE_NO_DOWNLINK",
     "startTimestampNs": "1790612790000676914", "durationNs": "599976645"},
    {"severity": "EVENT_SEVERITY_INFO", "reason": "EVENT_REASON_SOMETHING_ELSE",
     "startTimestampNs": "1790612000000000000", "durationNs": "1"},
]}
OUTAGES = [{"cause": "NO_PINGS", "startTimestampNs": "1474649729960339711",
            "durationNs": "940323728", "didSwitch": True}]


def test_gps_offset_matches_the_event_log():
    assert round(1474649729.960339711 + GPS_TO_UNIX_S, 3) == 1790614511.96


def test_event_log_preferred_and_sorted():
    events = outage_events({"eventLog": EVENT_LOG, "outages": OUTAGES})
    assert [e["cause"] for e in events] == ["BOOTING", "NO_DOWNLINK", "NO_PINGS"]
    assert events[0] == {"t": 1790548950.699, "d": 59.141, "cause": "BOOTING"}


def test_falls_back_to_outages_with_gps_conversion():
    events = outage_events({"outages": OUTAGES})
    assert events == [{"t": 1790614511.96, "d": 0.94, "cause": "NO_PINGS"}]


def test_ring_buffer_newest_samples():
    size = 900
    current = 108691  # Vendetta's counter
    ring = [0.0] * size
    newest = (current - 1) % size
    for i in range(60):
        ring[(newest - i) % size] = 0.5  # last minute: 50% drops
    w = recent_window({"current": str(current), "popPingDropRate": ring}, seconds=60)
    assert w["drop_rate"] == 0.5


def test_latency_ignores_zero_seconds_and_nan():
    ring = [20.0, 0.0, float("nan"), 40.0]
    w = recent_window({"current": 4, "popPingLatencyMs": ring}, seconds=60)
    assert w["latency_ms"] == 30.0


def test_short_history_after_boot():
    w = recent_window({"current": 2, "popPingDropRate": [0.0, 1.0] + [0.9] * 898}, seconds=60)
    assert w["drop_rate"] == 0.5  # only the 2 real samples


def test_missing_or_odd_history():
    assert recent_window({}) == {} and outage_events({}) == []
    assert recent_window({"current": "x"}) == {}


class TestHeartbeatWindows:
    NOW = 1_800_000_000.0

    def ev(self, age, d):
        return {"t": self.NOW - age, "d": d, "cause": "NO_PINGS"}

    def test_brief_blips_only_while_fresh(self):
        out = for_heartbeat([self.ev(60, 0.8), self.ev(1200, 0.8)], self.NOW)
        assert len(out) == 1

    def test_long_outage_survives_heartbeats_being_down(self):
        # 40-minute outage ended 3h ago: heartbeats failed during it, so it
        # must still be sent now the link is back
        out = for_heartbeat([self.ev(3 * 3600 + 2400, 2400.0)], self.NOW)
        assert len(out) == 1

    def test_notable_threshold(self):
        assert for_heartbeat([self.ev(3600, NOTABLE_OUTAGE_S)], self.NOW)
        assert not for_heartbeat([self.ev(3600, NOTABLE_OUTAGE_S - 0.1)], self.NOW)

    def test_capped(self):
        assert len(for_heartbeat([self.ev(i, 0.5) for i in range(500)], self.NOW)) == 100
