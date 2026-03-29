import time
from unittest import mock

from app.streaming.health import HealthMonitor


SAMPLE_LINE = (
    "frame= 1500 fps=30.0 q=28.0 Lsize=   12345kB "
    "time=00:00:50.00 bitrate=2024.5kbits/s speed=1.00x"
)

SLOW_LINE = (
    "frame= 100 fps=15.0 q=28.0 Lsize=   1234kB "
    "time=00:00:06.67 bitrate=1500.0kbits/s speed=0.50x"
)

NO_MATCH_LINE = "[info] Stream mapping:"


class TestHealthMonitor:
    def test_parses_progress_line(self):
        mon = HealthMonitor()
        mon.parse_line(SAMPLE_LINE)
        snap = mon.get_snapshot()
        assert snap.frame_count == 1500
        assert snap.fps == 30.0
        assert snap.bitrate_kbps == 2024.5
        assert snap.speed == 1.0
        assert snap.elapsed_seconds == 50.0

    def test_ignores_non_progress_lines(self):
        mon = HealthMonitor()
        mon.parse_line(NO_MATCH_LINE)
        snap = mon.get_snapshot()
        assert snap.frame_count == 0

    def test_detects_slow_speed(self):
        mon = HealthMonitor()
        mon.parse_line(SLOW_LINE)
        snap = mon.get_snapshot()
        assert snap.is_slow is True
        assert snap.speed == 0.5

    def test_normal_speed_not_flagged(self):
        mon = HealthMonitor()
        mon.parse_line(SAMPLE_LINE)
        snap = mon.get_snapshot()
        assert snap.is_slow is False

    def test_stall_detection(self):
        mon = HealthMonitor(stall_timeout=2)
        mon.parse_line(SAMPLE_LINE)

        # Simulate time passing with no new frames
        with mock.patch("app.streaming.health.time") as mock_time:
            mock_time.time.return_value = time.time() + 5
            # Parse same frame count again
            mon._latest.timestamp = time.time() - 5
            snap = mon.get_snapshot()
            assert snap.is_stalled is True

    def test_reset_clears_state(self):
        mon = HealthMonitor()
        mon.parse_line(SAMPLE_LINE)
        assert mon.get_snapshot().frame_count == 1500

        mon.reset()
        snap = mon.get_snapshot()
        assert snap.frame_count == 0
        assert snap.fps == 0.0

    def test_multiple_lines_updates(self):
        mon = HealthMonitor()
        mon.parse_line(SAMPLE_LINE)
        assert mon.get_snapshot().frame_count == 1500

        line2 = SAMPLE_LINE.replace("frame= 1500", "frame= 3000")
        line2 = line2.replace("time=00:00:50.00", "time=00:01:40.00")
        mon.parse_line(line2)
        assert mon.get_snapshot().frame_count == 3000

    def test_carriage_return_separated(self):
        """FFmpeg uses \\r to overwrite progress — multiple updates in one line."""
        mon = HealthMonitor()
        combined = (
            "frame=  100 fps=25.0 q=28.0 size=   500kB time=00:00:04.00 bitrate=1024.0kbits/s speed=1.00x\r"
            "frame=  200 fps=25.0 q=28.0 size=  1000kB time=00:00:08.00 bitrate=1024.0kbits/s speed=1.00x"
        )
        mon.parse_line(combined)
        snap = mon.get_snapshot()
        assert snap.frame_count == 200
        assert snap.elapsed_seconds == 8.0

    def test_partial_fields(self):
        """Parse line with only some fields present."""
        mon = HealthMonitor()
        mon.parse_line("frame=  500 fps=30.0 time=00:00:16.67")
        snap = mon.get_snapshot()
        assert snap.frame_count == 500
        assert snap.fps == 30.0
        assert snap.elapsed_seconds == 16.67
