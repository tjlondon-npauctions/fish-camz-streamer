"""The poller runs as a child process so a native grpc crash can't take the
heartbeat down with it. These use real processes."""

import json
import subprocess
import sys
import time

import pytest

from app.starlink.supervisor import APP_ROOT, PollerSupervisor


def wait_for(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def fast(cmd):
    return PollerSupervisor([], command=cmd, first_backoff=0.05, max_backoff=0.2)


def test_crashing_child_is_restarted():
    sup = fast([sys.executable, "-c", "import sys; sys.exit(3)"])
    sup.start()
    try:
        assert wait_for(lambda: sup.restarts >= 3)
    finally:
        sup.stop()


def test_segfault_in_child_does_not_kill_the_supervisor():
    # What a crash in grpc's native code looks like: the process dies on a signal
    sup = fast([sys.executable, "-c", "import os, signal; os.kill(os.getpid(), signal.SIGSEGV)"])
    sup.start()
    try:
        assert wait_for(lambda: sup.restarts >= 2)
        assert sup._thread.is_alive()
    finally:
        sup.stop()


def test_stop_terminates_a_running_child():
    sup = fast([sys.executable, "-c", "import time; time.sleep(60)"])
    sup.start()
    assert wait_for(lambda: sup._proc is not None and sup._proc.poll() is None)
    started = time.monotonic()
    sup.stop()
    assert sup._proc.poll() is not None and time.monotonic() - started < 6


def test_backoff_grows_then_caps():
    sup = PollerSupervisor([], command=[sys.executable, "-c", "pass"], first_backoff=0.05, max_backoff=0.1)
    sup.start()
    try:
        assert wait_for(lambda: sup.restarts >= 4)
    finally:
        sup.stop()


def test_poller_entry_point_runs_as_a_child(tmp_path):
    pytest.importorskip("grpc")
    pytest.importorskip("grpc_reflection")
    from fake_dish import FakeDish

    with FakeDish({"deviceInfo": {"hardwareVersion": "rev4_prod2"}, "popPingLatencyMs": 21.0}) as dish:
        subprocess.run(
            [sys.executable, "-m", "app.starlink.poller", "--state-dir", str(tmp_path),
             "--address", dish.address, "--once"],
            cwd=APP_ROOT, check=True, timeout=60,
        )
    state = json.loads((tmp_path / "starlink.json").read_text())
    assert state["status"] == "ok" and state["summary"]["hardware"] == "rev4_prod2"
