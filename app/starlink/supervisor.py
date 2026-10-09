"""Run the Starlink poller as a child process and keep it running.

The poller talks to the dish through grpc, whose core is native code. A crash
in there (a segfault, say) can't be caught as a Python exception: it kills
whatever process it's in. Run in-process, that would be the web container's
main process — the one sending heartbeats, and viewers see a vessel as
offline when its heartbeat stops. As a child process, a crash only costs the
Starlink readings until the restart below; the heartbeat keeps going and
keeps reading the last state file.

The web process itself never imports grpc.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional, Sequence

logger = logging.getLogger(__name__)

APP_ROOT = Path(__file__).resolve().parent.parent.parent


class PollerSupervisor:
    def __init__(
        self,
        poller_args: Sequence[str],
        command: Optional[Sequence[str]] = None,
        first_backoff: float = 10,
        max_backoff: float = 600,
        healthy_after: float = 600,
    ):
        self._cmd = list(command or [sys.executable, "-m", "app.starlink.poller", *poller_args])
        self._first_backoff = first_backoff
        self._max_backoff = max_backoff
        self._healthy_after = healthy_after
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._proc: Optional[subprocess.Popen] = None
        self.restarts = 0

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="starlink-supervisor", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5) -> None:
        self._stop.set()
        proc = self._proc
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
        if self._thread:
            self._thread.join(timeout=timeout)

    def _run(self) -> None:
        backoff = self._first_backoff
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self._proc = subprocess.Popen(self._cmd, cwd=APP_ROOT)
            except OSError as e:
                logger.warning("Starlink poller could not start: %s", e)
                self._stop.wait(self._max_backoff)
                continue

            code = self._proc.wait()
            if self._stop.is_set():
                return

            lived = time.monotonic() - started
            if lived >= self._healthy_after:
                backoff = self._first_backoff  # it had been fine for a while
            # Negative = killed by a signal (e.g. -11 SIGSEGV from native code)
            logger.warning(
                "Starlink poller exited (code %s) after %.0fs — restarting in %.0fs",
                code, lived, backoff,
            )
            self.restarts += 1
            self._stop.wait(backoff)
            backoff = min(backoff * 2, self._max_backoff)
