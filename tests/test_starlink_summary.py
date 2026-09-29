"""Summaries and verdicts from real dish output — every field optional."""

import copy
import json
from pathlib import Path

from app.starlink.summary import (
    DISABLED, NOT_READY, OBSTRUCTED, OK, RATE_LIMITED, ALERT, summarize,
)

TUNA = json.loads((Path(__file__).parent / "fixtures" / "starlink_status_tuna.json").read_text())


def with_status(**changes):
    r = copy.deepcopy(TUNA)
    r["dishGetStatus"].update(changes)
    return r


class TestTuna:
    def test_real_output_summarises(self):
        s = summarize(TUNA)
        assert s["state"] == OK
        assert s["hardware"] == "rev4_prod2"
        assert s["firmware"] == "2026.09.16.mr87006"
        assert s["api_version"] == "43"
        assert s["pop_latency_ms"] == 24.1
        assert s["uplink_kbps"] == 1936
        assert s["obstruction_fraction"] == 0.0012
        assert s["gps_sats"] == 19
        assert s["reboot_hour_local"] == 3
        assert s["uptime_s"] == 113528
        assert "alerts" not in s  # empty lists are dropped

    def test_heartbeat_sized(self):
        assert len(json.dumps(summarize(TUNA))) < 800


class TestVerdicts:
    def test_patriot_overage_flag(self):
        # Patriot's app export: overageRateLimited, no *RestrictedReason fields
        r = with_status(overageRateLimited=True)
        del r["dishGetStatus"]["ulBandwidthRestrictedReason"]
        s = summarize(r)
        assert s["state"] == RATE_LIMITED
        assert "data allowance" in s["detail"]

    def test_restricted_reason(self):
        s = summarize(with_status(ulBandwidthRestrictedReason="OVERAGE_LIMIT"))
        assert s["state"] == RATE_LIMITED and "upload OVERAGE_LIMIT" in s["detail"]

    def test_disabled_account_outranks_everything(self):
        s = summarize(with_status(disablementCode="NO_ACTIVE_ACCOUNT", overageRateLimited=True))
        assert s["state"] == DISABLED

    def test_not_ready(self):
        r = with_status()
        r["dishGetStatus"]["readyStates"]["rf"] = False
        assert summarize(r)["state"] == NOT_READY

    def test_no_signal(self):
        assert summarize(with_status(isSnrAboveNoiseFloor=False))["state"] == NOT_READY

    def test_obstructed_now(self):
        r = with_status(obstructionStats={"fractionObstructed": 0.2, "currentlyObstructed": True})
        assert summarize(r)["state"] == OBSTRUCTED

    def test_hardware_alert(self):
        s = summarize(with_status(alerts={"dishThermalThrottle": True, "motorsStuck": False}))
        assert s["state"] == ALERT and s["alerts"] == ["dishThermalThrottle"]

    def test_partial_obstruction_is_still_ok(self):
        s = summarize(with_status(obstructionStats={"fractionObstructed": 0.05}))
        assert s["state"] == OK and "5.0%" in s["detail"]


class TestOlderOrStrangerFirmware:
    def test_empty_response(self):
        s = summarize({})
        assert s["state"] == OK and set(s) == {"state", "detail"}

    def test_missing_sections(self):
        s = summarize({"apiVersion": "30", "dishGetStatus": {"popPingLatencyMs": 40}})
        assert s["pop_latency_ms"] == 40 and s["api_version"] == "30"

    def test_nan_and_string_numbers(self):
        s = summarize(with_status(popPingLatencyMs="NaN", deviceState={"uptimeS": "12"}))
        assert "pop_latency_ms" not in s and s["uptime_s"] == 12
