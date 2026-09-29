"""Background poller: dish status → /run/rpie/starlink.json.

Runs in the web container next to the GPS reader. The heartbeat and the
dashboard only ever read the file this writes, so a slow or wedged dish can
never hold up a heartbeat (a late heartbeat makes the vessel look offline
to viewers even when the stream is fine).

Every boat is wired differently, so "no dish here" is a normal state, not
an error: it's retried slowly and logged only when the state changes.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional

from app.starlink.client import StarlinkClient, StarlinkError
from app.starlink.summary import summarize

logger = logging.getLogger(__name__)

STATE_FILE = "starlink.json"

# How long to wait before trying again after each kind of failure
RETRY_SECONDS = {
    "unreachable": 60,     # no dish at the address (yet) — boats get rewired
    "not_permitted": 300,
    "unsupported": 1800,   # firmware without reflection/this API won't change soon
    "unavailable": 3600,   # grpc missing from the image
    "error": 60,
}
LOCATION_PROBE_SECONDS = 600


class StarlinkPoller:
    def __init__(
        self,
        state_dir: str,
        address: str = "192.168.100.1:9200",
        poll_interval: float = 15,
        timeout: float = 5,
        client: Optional[StarlinkClient] = None,
    ):
        self.address = address
        self.poll_interval = poll_interval
        self._client = client or StarlinkClient(address, timeout=timeout)
        self._state_file = Path(state_dir) / STATE_FILE
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_logged_status: Optional[str] = None
        self._last_location_probe = 0.0
        self._state: dict = {
            "enabled": True,
            "address": address,
            "status": "starting",
            "reachable": False,
        }

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="starlink-poller", daemon=True)
        self._thread.start()
        logger.info("Starlink poller started (dish at %s)", self.address)

    def stop(self) -> None:
        self._stop.set()
        self._client.close()

    def poll_once(self) -> float:
        """One poll. Returns seconds until the next one."""
        now = time.time()
        try:
            response = self._client.handle({"getStatus": {}})
        except StarlinkError as e:
            self._state.update(status=e.kind, reachable=e.kind not in ("unreachable", "unavailable"),
                               detail=str(e), updated_at=now)
            self._log_transition(e.kind, str(e))
            self._write()
            return RETRY_SECONDS.get(e.kind, 60)

        summary = summarize(response)
        self._state.update(
            status="ok",
            reachable=True,
            detail=summary.get("detail"),
            summary=summary,
            raw=response.get("dishGetStatus", {}),
            updated_at=now,
            last_success_at=now,
        )
        self._log_transition("ok", f"{summary.get('hardware')} fw {summary.get('firmware')}: {summary.get('detail')}")

        if now - self._last_location_probe >= LOCATION_PROBE_SECONDS:
            self._probe_location(now)
        self._write()
        return self.poll_interval

    # ── internals ───────────────────────────────────────────────────────────

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                delay = self.poll_once()
            except Exception as e:  # never let the poller take the web container down
                logger.exception("Starlink poller error: %s", e)
                delay = 60
            self._stop.wait(delay)

    def _probe_location(self, now: float) -> None:
        """Record whether location is available on this dish. Coordinates
        are deliberately discarded — using dish GPS is a later, separate step."""
        self._last_location_probe = now
        try:
            response = self._client.handle({"getLocation": {"source": "GPS"}})
            has_fix = bool((response.get("getLocation") or {}).get("lla"))
            self._state["location_access"] = "enabled" if has_fix else "no_fix"
            self._state["location_detail"] = "" if has_fix else "Location allowed but no fix reported"
        except StarlinkError as e:
            self._state["location_access"] = e.kind
            self._state["location_detail"] = str(e)

    def _log_transition(self, status: str, detail: str) -> None:
        if status == self._last_logged_status:
            return
        self._last_logged_status = status
        log = logger.info if status in ("ok", "unreachable") else logger.warning
        log("Starlink: %s — %s", status, detail)

    def _write(self) -> None:
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._state))
            os.replace(tmp, self._state_file)
        except OSError as e:
            logger.debug("Could not write Starlink state: %s", e)


def for_heartbeat(state: dict) -> dict:
    """The part of starlink.json worth sending over a metered link (~300 bytes)."""
    if not state:
        return {}
    out = {k: state.get(k) for k in ("status", "reachable", "detail", "updated_at",
                                     "last_success_at", "location_access")}
    out["summary"] = state.get("summary") or {}
    return {k: v for k, v in out.items() if v is not None}
