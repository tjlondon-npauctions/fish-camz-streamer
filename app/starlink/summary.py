"""Turn a raw dish ``getStatus`` response into a compact summary + verdict.

Pure functions, no grpc. Every field is optional: dishes in the fleet run
different hardware and firmware, and the same fact can appear under
different names (Patriot's app export says ``overageRateLimited``; Tuna's
gRPC says ``ulBandwidthRestrictedReason``). Whatever is missing is left out
of the summary rather than guessed.
"""

from __future__ import annotations

from typing import Any, Optional

NO_LIMIT = {"NO_LIMIT", "", None}

# Verdicts, most serious first
DISABLED = "disabled"          # account/service problem (disablementCode)
NOT_READY = "not_ready"        # booting, searching, no signal
RATE_LIMITED = "rate_limited"  # Starlink is capping bandwidth (data allowance, policy)
OBSTRUCTED = "obstructed"
ALERT = "alert"                # hardware alerts (thermal, motors, …)
OK = "ok"


def summarize(response: dict) -> dict:
    """Compact, heartbeat-sized summary of a getStatus response."""
    status = response.get("dishGetStatus") or {}
    info = status.get("deviceInfo") or {}
    obstruction = status.get("obstructionStats") or {}
    gps = status.get("gpsStats") or {}
    config = status.get("config") or {}

    summary: dict[str, Any] = {
        "api_version": _str(response.get("apiVersion")),
        "dish_id": info.get("id"),
        "hardware": info.get("hardwareVersion"),
        "firmware": info.get("softwareVersion"),
        "class_of_service": status.get("classOfService"),
        "mobility_class": status.get("mobilityClass"),
        "uptime_s": _num((status.get("deviceState") or {}).get("uptimeS")),
        "pop_latency_ms": _round(status.get("popPingLatencyMs"), 1),
        "pop_drop_rate": _round(status.get("popPingDropRate"), 3),
        "downlink_kbps": _kbps(status.get("downlinkThroughputBps")),
        "uplink_kbps": _kbps(status.get("uplinkThroughputBps")),
        "obstruction_fraction": _round(obstruction.get("fractionObstructed"), 4),
        "currently_obstructed": obstruction.get("currentlyObstructed"),
        "snr_ok": status.get("isSnrAboveNoiseFloor"),
        "signal_quality": status.get("signalQuality"),
        "disablement_code": status.get("disablementCode"),
        "dl_restricted": status.get("dlBandwidthRestrictedReason"),
        "ul_restricted": status.get("ulBandwidthRestrictedReason"),
        "overage_rate_limited": _find_flag(status, "overageRateLimited"),
        "alerts": sorted(k for k, v in (status.get("alerts") or {}).items() if v is True),
        "not_ready": sorted(k for k, v in (status.get("readyStates") or {}).items() if v is False),
        "gps_valid": gps.get("gpsValid"),
        "gps_sats": gps.get("gpsSats"),
        "software_update_state": status.get("softwareUpdateState"),
        "reboot_hour_local": config.get("swupdateRebootHour"),
        "utc_offset_s": info.get("utcOffsetS"),
    }
    summary = {k: v for k, v in summary.items() if v is not None and v != []}
    summary["state"], summary["detail"] = verdict(summary)
    return summary


def verdict(s: dict) -> tuple[str, str]:
    code = s.get("disablement_code")
    if code and code != "OKAY":
        return DISABLED, f"Starlink service disabled: {code}"

    if s.get("not_ready"):
        return NOT_READY, "Dish not ready: " + ", ".join(s["not_ready"])
    if s.get("snr_ok") is False:
        return NOT_READY, "No usable signal (SNR below noise floor)"

    limited = [
        f"{direction} {reason}"
        for direction, reason in (("upload", s.get("ul_restricted")), ("download", s.get("dl_restricted")))
        if reason not in NO_LIMIT
    ]
    if s.get("overage_rate_limited") is True:
        return RATE_LIMITED, "Rate limited — data allowance used up"
    if limited:
        return RATE_LIMITED, "Rate limited by Starlink: " + ", ".join(limited)

    if s.get("currently_obstructed") is True or "obstructed" in s.get("alerts", []):
        return OBSTRUCTED, "Dish is obstructed right now"

    alerts = [a for a in s.get("alerts", []) if a != "obstructed"]
    if alerts:
        return ALERT, "Dish alerts: " + ", ".join(alerts)

    fraction = s.get("obstruction_fraction")
    if fraction is not None and fraction >= 0.01:
        return OK, f"Connected ({fraction * 100:.1f}% of sky obstructed)"
    return OK, "Connected"


def _find_flag(obj: Any, key: str) -> Optional[bool]:
    """Look for a boolean anywhere in the response — its location varies."""
    if isinstance(obj, dict):
        if isinstance(obj.get(key), bool):
            return obj[key]
        for v in obj.values():
            found = _find_flag(v, key)
            if found is not None:
                return found
    return None


def _num(v) -> Optional[float]:
    # int64s arrive as strings from MessageToDict; NaN arrives as "NaN"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def _round(v, digits) -> Optional[float]:
    f = _num(v)
    return None if f is None else round(f, digits)


def _kbps(v) -> Optional[int]:
    f = _num(v)
    return None if f is None else round(f / 1000)


def _str(v) -> Optional[str]:
    return None if v is None else str(v)
