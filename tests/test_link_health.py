"""Link verdicts from the uploader's own uploads — no speed tests."""

import requests
import urllib3

from app.streaming.link_health import (
    AUTH, BUNNY, DROPPED, OFFLINE, OK, OTHER, SLOW,
    LinkEvent, assess_link, classify_exception, classify_http,
)

NOW = 10_000.0
MB = 1_000_000


def ok(ago, size=800_000, seconds=1.0):
    return LinkEvent(NOW - ago, OK, size, seconds)


def fail(ago, kind):
    return LinkEvent(NOW - ago, kind)


class TestVerdict:
    def test_no_events(self):
        assert assess_link([], NOW, 900)["status"] == "unknown"

    def test_healthy(self):
        # 800 KB/s = 6.4 Mbps vs a 900 kbps stream
        r = assess_link([ok(i * 6) for i in range(10)], NOW, 900)
        assert r["status"] == "ok"
        assert r["upload_kbps"] == 6400
        assert r["headroom"] > 7

    def test_upload_slower_than_stream_is_throttled(self):
        # 100 KB/s = 800 kbps vs a 900 kbps stream
        r = assess_link([ok(i * 6, 200_000, 2.0) for i in range(10)], NOW, 900)
        assert r["status"] == "throttled"
        assert "falling behind" in r["detail"]

    def test_little_headroom_is_marginal(self):
        r = assess_link([ok(i * 6, 150_000, 1.0) for i in range(10)], NOW, 900)  # 1.2 Mbps
        assert r["status"] == "marginal"

    def test_connect_failures_mean_down(self):
        events = [ok(200)] + [fail(i * 10, OFFLINE) for i in range(5)]
        assert assess_link(events, NOW, 900)["status"] == "down"

    def test_timeouts_mean_throttled(self):
        events = [ok(200)] + [fail(i * 10, SLOW) for i in range(5)]
        assert assess_link(events, NOW, 900)["status"] == "throttled"

    def test_bunny_errors_blamed_on_bunny(self):
        events = [ok(200)] + [fail(i * 10, BUNNY) for i in range(5)]
        assert assess_link(events, NOW, 900)["status"] == "bunny_error"

    def test_rejected_key(self):
        events = [fail(i * 10, AUTH) for i in range(5)]
        assert assess_link(events, NOW, 900)["status"] == "bunny_auth"

    def test_recovered_link_is_not_down(self):
        # Failures a few minutes ago, clean uploads since
        events = [fail(240, OFFLINE), fail(230, OFFLINE)] + [ok(i * 6) for i in range(8)]
        r = assess_link(events, NOW, 900)
        assert r["status"] == "ok"
        assert r["failures"] == {OFFLINE: 2}  # still reported, just not judged

    def test_goes_down_promptly_despite_recent_successes(self):
        # Healthy until 30s ago, failing since: must not wait for the
        # earlier successes to age out (found in the local outage test)
        events = [ok(30 + i * 6) for i in range(10)] + [fail(i * 2, OFFLINE) for i in range(14)]
        assert assess_link(events, NOW, 900)["status"] == "down"

    def test_brief_blip_is_marginal_not_down(self):
        events = [ok(40), fail(35, OFFLINE), fail(33, OFFLINE)] + [ok(i * 6) for i in range(5)]
        r = assess_link(events, NOW, 900)
        assert r["status"] == "marginal"
        assert "last minute" in r["detail"]

    def test_failure_just_after_success_is_not_yet_down(self):
        events = [ok(5), fail(2, OFFLINE)]
        assert assess_link(events, NOW, 900)["status"] == "marginal"

    def test_old_events_ignored(self):
        assert assess_link([fail(400, OFFLINE)], NOW, 900)["status"] == "unknown"

    def test_small_uploads_excluded_from_speed(self):
        # A 400-byte playlist taking 0.3s would read as ~10 kbps
        events = [ok(i * 6) for i in range(5)] + [ok(i * 6 + 1, 400, 0.3) for i in range(5)]
        assert assess_link(events, NOW, 900)["upload_kbps"] == 6400

    def test_speed_without_stream_bitrate(self):
        r = assess_link([ok(5)], NOW, None)
        assert r["upload_kbps"] == 6400 and r["headroom"] is None


class TestClassify:
    def test_http(self):
        assert classify_http(401) == AUTH
        assert classify_http(403) == AUTH
        assert classify_http(500) == BUNNY
        assert classify_http(429) == BUNNY
        assert classify_http(404) == OTHER

    def test_connect_timeout_is_offline(self):
        assert classify_exception(requests.exceptions.ConnectTimeout()) == OFFLINE

    def test_read_timeout_is_slow(self):
        assert classify_exception(requests.exceptions.ReadTimeout()) == SLOW

    def test_dns_failure_is_offline(self):
        inner = urllib3.exceptions.NewConnectionError(None, "Failed to resolve 'la.storage.bunnycdn.com'")
        exc = requests.exceptions.ConnectionError(urllib3.exceptions.MaxRetryError(None, "/", inner))
        assert classify_exception(exc) == OFFLINE

    def test_reset_mid_upload_is_dropped(self):
        exc = requests.exceptions.ConnectionError("('Connection aborted.', ConnectionResetError(104))")
        assert classify_exception(exc) == DROPPED


def test_speed_follows_recent_uploads_not_the_whole_window():
    # Fast for minutes, then throttled to ~800 kbps for the last 10 uploads
    fast = [ok(290 - i * 6) for i in range(30)]
    slow = [ok(60 - i * 6, 200_000, 2.0) for i in range(10)]
    r = assess_link(fast + slow, NOW, 900)
    assert r["upload_kbps"] == 800
    assert r["status"] == "throttled"
