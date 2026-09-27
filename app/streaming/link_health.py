"""Judge the internet link from the uploads the uploader is already making.

No speed tests: on a data-capped Starlink link they cost real data, and the
number that matters is whether *our* uploads keep up with the stream. Each
upload is recorded as an event; ``assess_link`` turns the recent ones into a
verdict the dashboard can show — down, throttled, a Bunny problem, or fine.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Iterable, Optional

WINDOW_SECONDS = 300  # what "recent" means for speed and failure counts
NOW_SECONDS = 60      # failures this recent make a healthy link "marginal"
# No success for this long, with failures since the last one, means the link
# is failing *now* — don't wait for older successes to age out of a window.
FAILING_AFTER_SECONDS = 20

# Small uploads (the playlist) are dominated by round-trip latency, so they'd
# drag the speed estimate far below what the link can actually carry.
MIN_THROUGHPUT_BYTES = 100_000
# Upload speed comes from the most recent uploads only (~1 min of segments).
# Averaging the whole window made it lag: in the local throttle test it still
# read ~1.2 Mbps two minutes after the link was cut to 700 kbps.
SPEED_SAMPLES = 10

# Failure kinds
OFFLINE = "offline"   # couldn't open a connection at all: DNS, refused, unreachable
SLOW = "slow"         # connected, then timed out mid-transfer — throttled or saturated
DROPPED = "dropped"   # connection broke mid-transfer
BUNNY = "bunny"       # Bunny answered with 5xx / 429
AUTH = "auth"         # Bunny rejected the key: 401 / 403
OTHER = "other"       # anything else (other 4xx)
OK = "ok"


@dataclass
class LinkEvent:
    at: float          # epoch seconds
    kind: str          # OK or one of the failure kinds
    bytes: int = 0
    seconds: float = 0.0


def classify_http(status_code: int) -> str:
    if status_code in (401, 403):
        return AUTH
    if status_code == 429 or status_code >= 500:
        return BUNNY
    return OTHER


def classify_exception(exc: BaseException) -> str:
    """Map a requests exception to a failure kind."""
    import requests

    # ConnectTimeout subclasses both ConnectionError and Timeout — check it first
    if isinstance(exc, requests.exceptions.ConnectTimeout):
        return OFFLINE
    if isinstance(exc, requests.exceptions.Timeout):
        return SLOW
    if isinstance(exc, requests.exceptions.ConnectionError):
        msg = str(exc)
        # urllib3 wraps DNS failures, refusals and unreachable networks as
        # NewConnectionError / NameResolutionError; anything else broke mid-way
        if "NewConnectionError" in msg or "NameResolution" in msg or "Name or service" in msg:
            return OFFLINE
        return DROPPED
    return OTHER


def assess_link(
    events: Iterable[LinkEvent],
    now: float,
    stream_kbps: Optional[float],
) -> dict:
    """Summarise recent upload events into a verdict for the dashboard."""
    recent = [e for e in events if now - e.at <= WINDOW_SECONDS]
    latest = [e for e in recent if now - e.at <= NOW_SECONDS]

    ok = [e for e in recent if e.kind == OK]
    failures = Counter(e.kind for e in recent if e.kind != OK)

    sized = [e for e in ok if e.bytes >= MIN_THROUGHPUT_BYTES and e.seconds > 0][-SPEED_SAMPLES:]
    upload_kbps = None
    if sized:
        upload_kbps = sum(e.bytes for e in sized) * 8 / sum(e.seconds for e in sized) / 1000

    headroom = None
    if upload_kbps and stream_kbps:
        headroom = upload_kbps / stream_kbps

    status, detail = _verdict(now, recent, latest, failures, upload_kbps, stream_kbps, headroom)
    return {
        "status": status,
        "detail": detail,
        "upload_kbps": round(upload_kbps) if upload_kbps else None,
        "stream_kbps": round(stream_kbps) if stream_kbps else None,
        "headroom": round(headroom, 2) if headroom else None,
        "uploads_ok": len(ok),
        "failures": dict(failures),
        "window_seconds": WINDOW_SECONDS,
    }


def _verdict(now, recent, latest, failures, upload_kbps, stream_kbps, headroom):
    if not recent:
        return "unknown", "No uploads in the last 5 minutes"

    last_ok = max((e.at for e in recent if e.kind == OK), default=None)
    since_ok = [e for e in recent if last_ok is None or e.at > last_ok]
    failing_now = Counter(e.kind for e in since_ok if e.kind != OK)
    stalled = failing_now and (last_ok is None or now - last_ok >= FAILING_AFTER_SECONDS)

    if failures[AUTH] and last_ok is None:
        return "bunny_auth", "Bunny is rejecting the upload key (HTTP 401/403) — check the Bunny settings"

    if stalled:
        top = failing_now.most_common(1)[0][0]
        if top == OFFLINE:
            return "down", "Internet down — can't connect to Bunny"
        if top == SLOW:
            return "throttled", "Uploads are timing out — link is throttled or saturated"
        if top == DROPPED:
            return "down", "Connections are dropping mid-upload"
        if top == BUNNY:
            return "bunny_error", "Bunny is returning errors — likely their side, not the boat's internet"
        if top == AUTH:
            return "bunny_auth", "Bunny is rejecting the upload key (HTTP 401/403) — check the Bunny settings"
        return "bunny_error", "Bunny is refusing uploads — check the uploader log"

    if headroom is not None and headroom < 1.0:
        return "throttled", (
            f"Upload ~{_fmt(upload_kbps)} but the stream needs ~{_fmt(stream_kbps)} — falling behind"
        )

    latest_failures = Counter(e.kind for e in latest if e.kind != OK)
    network_failures = latest_failures[OFFLINE] + latest_failures[SLOW] + latest_failures[DROPPED]
    if network_failures or (headroom is not None and headroom < 1.5):
        parts = []
        if headroom is not None and headroom < 1.5:
            parts.append(f"only {headroom:.1f}× what the stream needs")
        if network_failures:
            parts.append(f"{network_failures} failed upload{'s' if network_failures != 1 else ''} in the last minute")
        return "marginal", "Keeping up, but " + " and ".join(parts)

    if failures[BUNNY]:
        return "ok", f"Keeping up ({failures[BUNNY]} Bunny error{'s' if failures[BUNNY] != 1 else ''} in 5 min)"
    return "ok", "Keeping up"


def _fmt(kbps: Optional[float]) -> str:
    if not kbps:
        return "?"
    if kbps >= 1000:
        return f"{kbps / 1000:.1f} Mbps"
    return f"{kbps:.0f} kbps"
