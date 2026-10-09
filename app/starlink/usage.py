"""Starlink data meter from the Starlink router's WAN byte counters.

The router (192.168.1.1:9000 on a standard Starlink setup) reports rx/tx
bytes per interface since it last booted. Its WAN interface carries every
byte the boat sends over Starlink — the Pi's video, crew phones, everything —
so it's the closest local measure of what counts against the data allowance
(the allowance itself is only visible in the Starlink account).

Counters reset when the router reboots, so they're sampled and the
differences accumulated into per-day totals, persisted on the SD card so
they survive Pi reboots too. Bytes the router counted while nobody was
sampling are still captured (the counter kept going) unless the router also
rebooted in that gap; those are lost, which errs towards under-counting.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import json
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

KEEP_DAYS = 62


def _day(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%d")


def find_wan(response_body: dict) -> Optional[dict]:
    """The WAN interface's counters from a getNetworkInterfaces body.

    The standard Starlink router names it ``wan0``. Prenup's Starlink Mini
    (built-in router) didn't expose a ``wan*`` interface, so fall back to the
    interface holding an internet-facing IPv4 — Starlink hands out CGNAT
    addresses (100.64.0.0/10, e.g. Vendetta's 100.103.24.79), which count.
    """
    interfaces = [i for i in response_body.get("networkInterfaces") or [] if i.get("up", True)]
    chosen = next((i for i in interfaces if str(i.get("name", "")).startswith("wan")), None)
    if chosen is None:
        chosen = next((i for i in interfaces if _has_internet_ipv4(i)), None)
    if chosen is None:
        return None
    try:
        return {
            "name": chosen.get("name", "?"),
            "rx": int((chosen.get("rxStats") or {}).get("bytes", 0)),
            "tx": int((chosen.get("txStats") or {}).get("bytes", 0)),
        }
    except (TypeError, ValueError):
        return None


_CGNAT = ipaddress.ip_network("100.64.0.0/10")


def _has_internet_ipv4(iface: dict) -> bool:
    for addr in iface.get("ipv4Addresses") or []:
        try:
            ip = ipaddress.ip_interface(addr).ip
        except ValueError:
            continue
        if ip in _CGNAT or ip.is_global:
            return True
    return False


class UsageLedger:
    """Per-day byte totals built from successive counter readings."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.data = self._load()

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
            if isinstance(data, dict) and isinstance(data.get("days"), dict):
                return data
        except (OSError, ValueError):
            pass
        return {"days": {}, "last": None, "resets": 0}

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data))
            os.replace(tmp, self.path)
        except OSError as e:
            logger.debug("Could not save Starlink usage: %s", e)

    def record(self, reading: dict, now: float) -> None:
        """Add the change since the previous reading to today's totals."""
        last = self.data.get("last")
        if last and last.get("name") == reading["name"]:
            day = self.data["days"].setdefault(_day(now), {"rx": 0, "tx": 0})
            for key in ("rx", "tx"):
                delta = reading[key] - last[key]
                if delta < 0:
                    # Router rebooted: its counter restarted from zero
                    delta = reading[key]
                    if key == "tx":
                        self.data["resets"] = self.data.get("resets", 0) + 1
                day[key] += delta
        # First reading (or a different WAN interface): baseline only — the
        # counter's existing value covers an unknown period since boot.
        self.data["last"] = dict(reading, at=now)
        self.data.setdefault("since", _day(now))
        self._prune(now)

    def _prune(self, now: float) -> None:
        cutoff = _day(now - KEEP_DAYS * 86400)
        for day in [d for d in self.data["days"] if d < cutoff]:
            del self.data["days"][day]

    def summary(self, now: float) -> dict:
        days = self.data["days"]
        today, yesterday = _day(now), _day(now - 86400)
        cutoff = _day(now - 29 * 86400)
        last30 = {"rx": 0, "tx": 0}
        for day, v in days.items():
            if day >= cutoff:
                last30["rx"] += v.get("rx", 0)
                last30["tx"] += v.get("tx", 0)
        return {
            # UTC dates, so the cloud can file each total under its own day
            "date": today,
            "yesterday_date": yesterday,
            "today": days.get(today, {"rx": 0, "tx": 0}),
            "yesterday": days.get(yesterday, {"rx": 0, "tx": 0}),
            "last_30_days": last30,
            "since": self.data.get("since"),
            "interface": (self.data.get("last") or {}).get("name"),
        }
