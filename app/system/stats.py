"""System statistics collector for CPU, memory, temperature, and disk."""

from __future__ import annotations

import logging
from pathlib import Path

import psutil

logger = logging.getLogger(__name__)

# Raspberry Pi thermal zone
THERMAL_ZONE = Path("/sys/class/thermal/thermal_zone0/temp")


def get_system_stats() -> dict:
    """Collect current system statistics."""
    return {
        "cpu_percent": psutil.cpu_percent(interval=0.5),
        "memory": _memory_stats(),
        "disk": _disk_stats(),
        "temperature": _cpu_temperature(),
        "uptime_seconds": _system_uptime(),
        "network_interfaces": _network_interfaces(),
    }


def _memory_stats() -> dict:
    mem = psutil.virtual_memory()
    return {
        "total_mb": round(mem.total / (1024 * 1024)),
        "used_mb": round(mem.used / (1024 * 1024)),
        "percent": mem.percent,
    }


def _disk_stats() -> dict:
    disk = psutil.disk_usage("/")
    return {
        "total_gb": round(disk.total / (1024 * 1024 * 1024), 1),
        "used_gb": round(disk.used / (1024 * 1024 * 1024), 1),
        "free_gb": round(disk.free / (1024 * 1024 * 1024), 1),
        "percent": disk.percent,
    }


def _cpu_temperature() -> float:
    """Read CPU temperature in Celsius. Returns 0.0 if unavailable."""
    # Try RPi thermal zone first
    if THERMAL_ZONE.exists():
        try:
            temp_str = THERMAL_ZONE.read_text().strip()
            return round(int(temp_str) / 1000.0, 1)
        except (ValueError, OSError):
            pass

    # Fallback: psutil sensors
    try:
        temps = psutil.sensors_temperatures()
        if temps:
            for name, entries in temps.items():
                if entries:
                    return round(entries[0].current, 1)
    except (AttributeError, OSError):
        pass  # Not available on this platform

    return 0.0


def _system_uptime() -> int:
    """System uptime in seconds."""
    import time
    return int(time.time() - psutil.boot_time())


def _network_interfaces() -> list:
    """Get active network interfaces with IP addresses."""
    interfaces = []
    addrs = psutil.net_if_addrs()
    stats = psutil.net_if_stats()

    for name, addr_list in addrs.items():
        if name == "lo" or name.startswith("docker"):
            continue

        is_up = stats.get(name, None)
        if is_up and not is_up.isup:
            continue

        for addr in addr_list:
            if addr.family.name == "AF_INET":
                interfaces.append({
                    "name": name,
                    "ip": addr.address,
                })
                break

    return interfaces
