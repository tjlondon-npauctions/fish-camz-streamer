"""Ping parsing: loss and jitter from a 5-packet check."""

from app.network.monitor import NetworkMonitor

IPUTILS_OK = """PING 1.1.1.1 (1.1.1.1) 56(84) bytes of data.
64 bytes from 1.1.1.1: icmp_seq=1 ttl=57 time=41.2 ms

--- 1.1.1.1 ping statistics ---
5 packets transmitted, 4 received, 20% packet loss, time 812ms
rtt min/avg/max/mdev = 38.101/45.520/60.311/8.702 ms
"""

IPUTILS_DEAD = """--- 1.1.1.1 ping statistics ---
5 packets transmitted, 0 received, 100% packet loss, time 4090ms
"""


def test_parses_rtt_and_jitter():
    assert NetworkMonitor._parse_ping_rtt(IPUTILS_OK) == (45.52, 8.702)


def test_loss_history(tmp_path):
    m = NetworkMonitor(state_dir=str(tmp_path))
    m._record_loss(IPUTILS_OK)
    m._record_loss(IPUTILS_DEAD)
    status = m.get_status()
    assert status["loss_percent"] == 100.0
    assert status["loss_percent_avg"] == 60.0


def test_unparseable_output_counts_as_loss(tmp_path):
    m = NetworkMonitor(state_dir=str(tmp_path))
    m._record_loss("")
    assert m.get_status()["loss_percent"] == 100.0
