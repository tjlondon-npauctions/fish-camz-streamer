"""Read the dish's getHistory: recent link quality and outage events.

Format as served by fw 2026.09 (DishGetHistoryResponse):

- ``popPingDropRate`` / ``popPingLatencyMs`` / ``uplinkThroughputBps`` / …:
  ring buffers of 900 one-second samples (15 min). ``current`` counts every
  sample since boot, so the newest is at ``(current - 1) % len``.
- ``eventLog.events``: outages since the dish booted (~30h on Vendetta), with
  UNIX-epoch nanosecond timestamps, a severity and an EVENT_REASON_OUTAGE_*.
  Preferred: longer reach and plain Unix time.
- ``outages``: the last ~15 min only, with GPS-epoch timestamps. Converted
  with GPS_TO_UNIX_S, verified against eventLog on Vendetta (2026-09-29): GPS
  1474649729.96 + 315964782 = Unix 1790614511.96, the same NO_PINGS 0.94 s event.

Pure functions; the poller does the I/O.
"""

from __future__ import annotations

from typing import Optional

GPS_TO_UNIX_S = 315_964_800 - 18  # GPS epoch (1980-01-06) minus leap seconds

# Sub-second NO_PINGS / NO_DOWNLINK blips with didSwitch=true happen ~2–3 times
# an hour on a healthy dish (satellite handovers) — the player's buffer absorbs
# them. The admin shows them as counts; anything this long is an "outage".
NOTABLE_OUTAGE_S = 5.0


def recent_window(body: dict, seconds: int = 60) -> dict:
    """Mean drop rate / latency / throughput over the newest ``seconds`` samples."""
    try:
        current = int(body.get("current", 0))
    except (TypeError, ValueError):
        return {}
    out: dict = {}
    for key, name in (("popPingDropRate", "drop_rate"), ("popPingLatencyMs", "latency_ms"),
                      ("uplinkThroughputBps", "uplink_bps"), ("downlinkThroughputBps", "downlink_bps")):
        ring = body.get(key)
        if not isinstance(ring, list) or not ring or current <= 0:
            continue
        size = len(ring)
        count = min(seconds, current, size)
        values = []
        for i in range(count):
            v = ring[(current - 1 - i) % size]
            if isinstance(v, (int, float)) and v == v:  # skip NaN
                values.append(float(v))
        # Latency is 0 for seconds with no ping reply — those are drops, not speed
        if name == "latency_ms":
            values = [v for v in values if v > 0]
        if values:
            out[name] = sum(values) / len(values)
    return out


def outage_events(body: dict) -> list[dict]:
    """Outages as ``{"t": unix_s, "d": seconds, "cause": "NO_PINGS"}``, oldest first."""
    events = []
    log = (body.get("eventLog") or {}).get("events")
    if isinstance(log, list) and log:
        for e in log:
            reason = str(e.get("reason", ""))
            if not reason.startswith("EVENT_REASON_OUTAGE"):
                continue
            t = _ns_to_s(e.get("startTimestampNs"))
            d = _ns_to_s(e.get("durationNs"))
            if t is None:
                continue
            events.append({"t": round(t, 3), "d": round(d or 0.0, 3),
                           "cause": reason.replace("EVENT_REASON_OUTAGE_", "") or "UNKNOWN"})
    else:
        for o in body.get("outages") or []:
            t = _ns_to_s(o.get("startTimestampNs"))
            if t is None:
                continue
            events.append({"t": round(t + GPS_TO_UNIX_S, 3), "d": round(_ns_to_s(o.get("durationNs")) or 0.0, 3),
                           "cause": str(o.get("cause", "UNKNOWN"))})
    events.sort(key=lambda e: e["t"])
    return events


def for_heartbeat(events: list[dict], now: float, brief_window_s: float = 900,
                  notable_window_s: float = 6 * 3600) -> list[dict]:
    """Which events to (re)send. Brief blips only while fresh; notable outages
    for hours, because heartbeats fail during exactly those outages and the
    event must still be in the payload once the link is back. The cloud
    de-duplicates by start time."""
    out = []
    for e in events:
        age = now - e["t"]
        window = notable_window_s if e["d"] >= NOTABLE_OUTAGE_S else brief_window_s
        if 0 <= age <= window:
            out.append(e)
    return out[-100:]


def _ns_to_s(v) -> Optional[float]:
    try:
        return int(v) / 1e9
    except (TypeError, ValueError):
        return None
