"""Data meter from the Starlink router's WAN counters."""

import json
from pathlib import Path

from app.starlink.summary import payload
from app.starlink.usage import UsageLedger, find_wan

VENDETTA = json.loads((Path(__file__).parent / "fixtures" / "starlink_router_interfaces_vendetta.json").read_text())
DAY = 86400
T0 = 1790640000.0  # 2026-09-29 00:00 UTC


def reading(rx, tx, name="wan0"):
    return {"name": name, "rx": rx, "tx": tx}


def test_finds_wan_in_real_router_output():
    assert find_wan(payload(VENDETTA)) == {"name": "wan0", "rx": 565114755, "tx": 325779715}


def test_no_wan_interface():
    assert find_wan({"networkInterfaces": [{"name": "lan0"}]}) is None
    assert find_wan({}) is None


def test_first_reading_is_baseline_only(tmp_path):
    ledger = UsageLedger(tmp_path / "u.json")
    ledger.record(reading(5_000_000, 3_000_000), T0 + 100)
    assert ledger.summary(T0 + 100)["today"] == {"rx": 0, "tx": 0}


def test_accumulates_deltas(tmp_path):
    ledger = UsageLedger(tmp_path / "u.json")
    ledger.record(reading(1000, 500), T0 + 100)
    ledger.record(reading(1600, 900), T0 + 160)
    ledger.record(reading(2000, 1500), T0 + 220)
    assert ledger.summary(T0 + 220)["today"] == {"rx": 1000, "tx": 1000}


def test_router_reboot_counts_from_zero(tmp_path):
    ledger = UsageLedger(tmp_path / "u.json")
    ledger.record(reading(10_000, 10_000), T0 + 100)
    ledger.record(reading(300, 200), T0 + 160)  # rebooted, counted 300/200 since
    assert ledger.summary(T0 + 160)["today"] == {"rx": 300, "tx": 200}
    assert ledger.data["resets"] == 1


def test_days_and_30_day_total(tmp_path):
    ledger = UsageLedger(tmp_path / "u.json")
    ledger.record(reading(0, 0), T0 - DAY + 10)
    ledger.record(reading(100, 1000), T0 - DAY + 70)   # yesterday
    ledger.record(reading(150, 3000), T0 + 70)         # today
    s = ledger.summary(T0 + 70)
    assert s["yesterday"] == {"rx": 100, "tx": 1000}
    assert s["today"] == {"rx": 50, "tx": 2000}
    assert s["last_30_days"] == {"rx": 150, "tx": 3000}


def test_survives_restart(tmp_path):
    path = tmp_path / "u.json"
    ledger = UsageLedger(path)
    ledger.record(reading(0, 0), T0 + 10)
    ledger.record(reading(0, 700), T0 + 70)
    ledger.save()
    again = UsageLedger(path)
    again.record(reading(0, 1000), T0 + 130)
    assert again.summary(T0 + 130)["today"]["tx"] == 1000


def test_corrupt_file_starts_fresh(tmp_path):
    path = tmp_path / "u.json"
    path.write_text("{nope")
    assert UsageLedger(path).summary(T0)["today"] == {"rx": 0, "tx": 0}


def test_old_days_pruned(tmp_path):
    ledger = UsageLedger(tmp_path / "u.json")
    ledger.data["days"]["2026-01-01"] = {"rx": 1, "tx": 1}
    ledger.record(reading(0, 0), T0)
    assert "2026-01-01" not in ledger.data["days"]


def test_summary_carries_utc_dates(tmp_path):
    s = UsageLedger(tmp_path / "u.json").summary(T0 + 100)
    assert s["date"] == "2026-09-29" and s["yesterday_date"] == "2026-09-28"
