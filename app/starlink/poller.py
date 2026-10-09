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

from app.starlink import history
from app.starlink.client import StarlinkClient, StarlinkError
from app.starlink.summary import payload, summarize
from app.starlink.usage import UsageLedger, find_wan

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
HISTORY_SECONDS = 60          # getHistory is ~60 KB — once a minute is plenty
HISTORY_RETRY_SECONDS = 600
KEEP_OUTAGES_S = 48 * 3600    # shown on the Pi dashboard
USAGE_SAMPLE_SECONDS = 60
USAGE_RETRY_SECONDS = 300  # no Starlink router on this boat (or it's elsewhere)


class StarlinkPoller:
    def __init__(
        self,
        state_dir: str,
        address: str = "192.168.100.1:9200",
        poll_interval: float = 15,
        timeout: float = 5,
        client: Optional[StarlinkClient] = None,
        router_address: str = "",
        router_client: Optional[StarlinkClient] = None,
        usage_path: Optional[str] = None,
    ):
        self.address = address
        self.poll_interval = poll_interval
        self._client = client or StarlinkClient(address, timeout=timeout)
        self._state_file = Path(state_dir) / STATE_FILE
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_logged_status: Optional[str] = None
        self._last_location_probe = 0.0
        # Data meter from the Starlink router's WAN counters (optional)
        self._router = router_client or (StarlinkClient(router_address, timeout=timeout) if router_address else None)
        self._ledger = UsageLedger(Path(usage_path)) if (self._router and usage_path) else None
        self._next_usage_sample = 0.0
        self._next_history = 0.0
        self._last_router_status: Optional[str] = None
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
        if self._router:
            self._router.close()

    def poll_once(self) -> float:
        """One poll. Returns seconds until the next one."""
        now = time.time()
        self._maybe_sample_usage(now)
        try:
            response = self._client.handle({"getStatus": {}})
        except StarlinkError as e:
            self._state.update(status=e.kind, reachable=e.kind not in ("unreachable", "unavailable"),
                               detail=str(e), updated_at=now)
            self._log_transition(e.kind, str(e))
            self._write()
            return RETRY_SECONDS.get(e.kind, 60)

        # Diagnostics carries overage_rate_limited on current firmware; a dish
        # that refuses it still gets a status-only summary.
        try:
            diagnostics = self._client.handle({"getDiagnostics": {}})
            self._state["diagnostics_status"] = "ok"
        except StarlinkError as e:
            diagnostics = None
            self._state["diagnostics_status"] = e.kind

        self._maybe_read_history(now)
        summary = summarize(response, diagnostics)
        recent = self._state.get("history") or {}
        if "drop_rate" in recent:
            # Mean over the last minute from the 1 Hz history — steadier than
            # getStatus's instantaneous reading, and status has no drop rate
            summary["pop_drop_rate"] = round(recent["drop_rate"], 4)
        if "latency_ms" in recent:
            summary["pop_latency_1m_ms"] = round(recent["latency_ms"], 1)
        self._state.update(
            status="ok",
            reachable=True,
            detail=summary.get("detail"),
            summary=summary,
            raw=response.get("dishGetStatus", {}),
            raw_diagnostics=_without_coordinates(payload(diagnostics)) if diagnostics else None,
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

    def _maybe_read_history(self, now: float) -> None:
        if now < self._next_history:
            return
        try:
            body = payload(self._client.handle({"getHistory": {}}))
        except StarlinkError as e:
            self._state["history_status"] = e.kind
            self._next_history = now + HISTORY_RETRY_SECONDS
            return
        self._next_history = now + HISTORY_SECONDS
        self._state["history_status"] = "ok"
        self._state["history"] = history.recent_window(body)
        self._state["outages"] = [e for e in history.outage_events(body) if now - e["t"] <= KEEP_OUTAGES_S]

    def _maybe_sample_usage(self, now: float) -> None:
        if not self._ledger or now < self._next_usage_sample:
            return
        try:
            reading = find_wan(payload(self._router.handle({"getNetworkInterfaces": {}})))
        except StarlinkError as e:
            self._state["usage_status"] = e.kind
            self._next_usage_sample = now + USAGE_RETRY_SECONDS
            if e.kind != self._last_router_status:
                self._last_router_status = e.kind
                logger.info("Starlink router data meter: %s — %s", e.kind, e)
            return
        self._next_usage_sample = now + USAGE_SAMPLE_SECONDS
        if reading is None:
            self._state["usage_status"] = "no_wan_interface"
            return
        if self._last_router_status != "ok":
            self._last_router_status = "ok"
            logger.info("Starlink router data meter: reading %s", reading["name"])
        self._ledger.record(reading, now)
        self._ledger.save()
        self._state["usage_status"] = "ok"
        self._state["usage"] = self._ledger.summary(now)

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


def _without_coordinates(diag: dict) -> dict:
    """Diagnostics minus position — dish GPS use is a separate, later step."""
    diag = dict(diag or {})
    if isinstance(diag.get("location"), dict):
        diag["location"] = {"enabled": diag["location"].get("enabled")}
    return diag


def for_heartbeat(state: dict) -> dict:
    """The part of starlink.json worth sending over a metered link (<1 KB)."""
    if not state:
        return {}
    out = {k: state.get(k) for k in ("status", "reachable", "detail", "updated_at",
                                     "last_success_at", "location_access", "usage", "usage_status")}
    out["summary"] = state.get("summary") or {}
    outages = history.for_heartbeat(state.get("outages") or [], time.time())
    if outages:
        out["outages"] = outages
    return {k: v for k, v in out.items() if v is not None}


def main(argv=None) -> None:
    """Entry point for the child process started by supervisor.py."""
    import argparse
    import signal

    ap = argparse.ArgumentParser(description="Starlink dish poller (child process)")
    ap.add_argument("--state-dir", required=True)
    ap.add_argument("--address", default="192.168.100.1:9200")
    ap.add_argument("--poll-interval", type=float, default=15)
    ap.add_argument("--router-address", default="")
    ap.add_argument("--usage-path", default=None)
    ap.add_argument("--once", action="store_true", help="poll once and exit (tests)")
    a = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    poller = StarlinkPoller(
        state_dir=a.state_dir, address=a.address, poll_interval=a.poll_interval,
        router_address=a.router_address, usage_path=a.usage_path,
    )
    if a.once:
        poller.poll_once()
        return
    signal.signal(signal.SIGTERM, lambda *_: poller.stop())
    logger.info("Starlink poller running as pid %d (dish at %s)", os.getpid(), a.address)
    poller._run()


if __name__ == "__main__":
    main()
