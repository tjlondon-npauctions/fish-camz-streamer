from __future__ import annotations

import logging
import re
import socket
import uuid
import xml.etree.ElementTree as ET
from typing import Optional

logger = logging.getLogger(__name__)

# Known NVR model patterns (case-insensitive) — matches hardware or name fields
_NVR_PATTERNS = re.compile(
    r"nvr|network.video.recorder|dhr|xvr|dvr",
    re.IGNORECASE,
)

# Brand-specific channel URL templates.
# Each entry: (brand_name, [(channel_num, main_url_suffix, sub_url_suffix), ...])
# {ch} is replaced with the channel number.
BRAND_CHANNEL_TEMPLATES = {
    "uniview": {
        "main": "/unicast/c{ch}/s0/live",
        "sub": "/unicast/c{ch}/s1/live",
        "max_channels": 8,
    },
    "hikvision": {
        "main": "/Streaming/Channels/{ch}01",
        "sub": "/Streaming/Channels/{ch}02",
        "max_channels": 8,
    },
    "dahua": {
        "main": "/cam/realmonitor?channel={ch}&subtype=0",
        "sub": "/cam/realmonitor?channel={ch}&subtype=1",
        "max_channels": 8,
    },
    "reolink": {
        "main": "/h264Preview_{ch:02d}_main",
        "sub": "/h264Preview_{ch:02d}_sub",
        "max_channels": 8,
    },
}

# ONVIF WS-Discovery multicast address and port
WS_DISCOVERY_ADDR = "239.255.255.250"
WS_DISCOVERY_PORT = 3702

# WS-Discovery Probe template for ONVIF network video devices
PROBE_TEMPLATE = """<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope
    xmlns:soap="http://www.w3.org/2003/05/soap-envelope"
    xmlns:wsa="http://schemas.xmlsoap.org/ws/2004/08/addressing"
    xmlns:wsd="http://schemas.xmlsoap.org/ws/2005/04/discovery"
    xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
  <soap:Header>
    <wsa:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</wsa:Action>
    <wsa:MessageID>urn:uuid:{message_id}</wsa:MessageID>
    <wsa:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</wsa:To>
  </soap:Header>
  <soap:Body>
    <wsd:Probe>
      <wsd:Types>dn:NetworkVideoTransmitter</wsd:Types>
    </wsd:Probe>
  </soap:Body>
</soap:Envelope>"""

# XML namespaces used in WS-Discovery responses
NS = {
    "soap": "http://www.w3.org/2003/05/soap-envelope",
    "wsd": "http://schemas.xmlsoap.org/ws/2005/04/discovery",
    "wsa": "http://schemas.xmlsoap.org/ws/2004/08/addressing",
}


def _get_local_ips() -> list[str]:
    """Get IPv4 addresses for all non-loopback, non-docker interfaces."""
    import psutil

    ips = []
    for name, addrs in psutil.net_if_addrs().items():
        if name == "lo" or name.startswith("docker") or name.startswith("br-") or name.startswith("veth"):
            continue
        stats = psutil.net_if_stats().get(name)
        if stats and not stats.isup:
            continue
        for addr in addrs:
            if addr.family.name == "AF_INET" and addr.address != "127.0.0.1":
                ips.append(addr.address)
    return ips


def discover_cameras(timeout: float = 5.0) -> list[dict]:
    """Scan the local network for ONVIF cameras via WS-Discovery.

    Sends a multicast probe on every active network interface so cameras
    on all subnets (e.g. a POE camera on a USB ethernet dongle) are found.

    Args:
        timeout: Seconds to wait for responses

    Returns:
        List of dicts with keys: ip, port, xaddrs (service URLs), scopes
    """
    message_id = str(uuid.uuid4())
    probe_msg = PROBE_TEMPLATE.format(message_id=message_id).encode("utf-8")

    local_ips = _get_local_ips()
    if not local_ips:
        logger.warning("No network interfaces found for camera discovery")
        return []

    cameras = []
    seen_ips = set()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.settimeout(timeout)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)

    try:
        # Send probe on each interface so we reach all subnets
        for local_ip in local_ips:
            try:
                sock.setsockopt(
                    socket.IPPROTO_IP,
                    socket.IP_MULTICAST_IF,
                    socket.inet_aton(local_ip),
                )
                sock.sendto(probe_msg, (WS_DISCOVERY_ADDR, WS_DISCOVERY_PORT))
                logger.info("Sent WS-Discovery probe on %s", local_ip)
            except OSError as e:
                logger.debug("Could not probe on %s: %s", local_ip, e)

        logger.info("Waiting %ss for responses...", timeout)

        while True:
            try:
                data, addr = sock.recvfrom(65535)
                ip = addr[0]

                if ip in seen_ips:
                    continue
                seen_ips.add(ip)

                camera = _parse_probe_response(data, ip)
                if camera:
                    cameras.append(camera)
                    logger.info("Found camera at %s: %s", ip, camera.get("xaddrs", ""))

            except socket.timeout:
                break

    finally:
        sock.close()

    logger.info("Discovery complete: found %d camera(s)", len(cameras))
    return cameras


def _parse_probe_response(data: bytes, ip: str) -> Optional[dict]:
    """Parse a WS-Discovery ProbeMatch response."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        logger.warning("Invalid XML from %s", ip)
        return None

    # Find ProbeMatch elements
    matches = root.findall(".//wsd:ProbeMatch", NS)
    if not matches:
        return None

    match = matches[0]

    # Extract XAddrs (service endpoint URLs)
    xaddrs_elem = match.find("wsd:XAddrs", NS)
    xaddrs = xaddrs_elem.text.strip() if xaddrs_elem is not None and xaddrs_elem.text else ""

    # Extract scopes (device info like name, model, location)
    scopes_elem = match.find("wsd:Scopes", NS)
    scopes = scopes_elem.text.strip() if scopes_elem is not None and scopes_elem.text else ""

    # Parse scopes for human-readable info
    name = ""
    hardware = ""
    for scope in scopes.split():
        if "/name/" in scope:
            name = scope.split("/name/")[-1]
        elif "/hardware/" in scope:
            hardware = scope.split("/hardware/")[-1]

    # Classify device type based on hardware model and scopes
    device_type = _classify_device(hardware, name, scopes)

    return {
        "ip": ip,
        "xaddrs": xaddrs,
        "name": name or f"Camera at {ip}",
        "hardware": hardware,
        "scopes": scopes,
        "device_type": device_type,
    }


def _classify_device(hardware: str, name: str, scopes: str) -> str:
    """Classify an ONVIF device as 'nvr' or 'camera' based on metadata."""
    combined = f"{hardware} {name} {scopes}"
    if _NVR_PATTERNS.search(combined):
        return "nvr"
    return "camera"


def detect_brand(hardware: str, name: str, scopes: str) -> Optional[str]:
    """Guess the device brand from ONVIF metadata.

    Returns a key from BRAND_CHANNEL_TEMPLATES, or None if unknown.
    """
    combined = f"{hardware} {name} {scopes}".lower()
    if "uniview" in combined or "unv" in combined:
        return "uniview"
    if "hikvision" in combined or "hikv" in combined:
        return "hikvision"
    if "dahua" in combined:
        return "dahua"
    if "reolink" in combined:
        return "reolink"
    return None


def get_channel_urls(
    ip: str,
    brand: Optional[str] = None,
    username: str = "",
    password: str = "",
    max_channels: int = 8,
) -> list[dict]:
    """Build a list of candidate channel URLs for a device.

    If brand is known, uses brand-specific templates.
    If brand is None, tries all known brands' channel-1 patterns.

    Returns list of dicts: {channel, quality, url, brand}
    """
    auth = f"{username}:{password}@" if username else ""
    base = f"rtsp://{auth}{ip}:554"
    candidates = []

    if brand and brand in BRAND_CHANNEL_TEMPLATES:
        tmpl = BRAND_CHANNEL_TEMPLATES[brand]
        limit = min(max_channels, tmpl["max_channels"])
        for ch in range(1, limit + 1):
            candidates.append({
                "channel": ch,
                "quality": "main",
                "url": base + tmpl["main"].format(ch=ch),
                "brand": brand,
            })
            candidates.append({
                "channel": ch,
                "quality": "sub",
                "url": base + tmpl["sub"].format(ch=ch),
                "brand": brand,
            })
    else:
        # Unknown brand — try channel 1 of each brand to detect which works
        for b, tmpl in BRAND_CHANNEL_TEMPLATES.items():
            candidates.append({
                "channel": 1,
                "quality": "main",
                "url": base + tmpl["main"].format(ch=1),
                "brand": b,
            })

    return candidates


def get_common_rtsp_urls(ip: str, username: str = "", password: str = "") -> list[str]:
    """Generate common RTSP URL patterns to try for a given camera IP.

    Different manufacturers use different RTSP paths. This returns a list
    of the most common patterns to try via ffprobe.
    """
    auth = f"{username}:{password}@" if username else ""
    base = f"rtsp://{auth}{ip}"

    return [
        # Uniview camera / NVR (channels 1-4)
        f"{base}:554/unicast/c1/s0/live", # Uniview ch1 main stream
        f"{base}:554/unicast/c1/s1/live", # Uniview ch1 sub stream
        f"{base}:554/unicast/c2/s0/live", # Uniview ch2 main stream
        f"{base}:554/unicast/c3/s0/live", # Uniview ch3 main stream
        f"{base}:554/unicast/c4/s0/live", # Uniview ch4 main stream
        # Other brands
        f"{base}:554/stream1",            # Generic
        f"{base}:554/cam/realmonitor?channel=1&subtype=0",  # Dahua
        f"{base}:554/Streaming/Channels/101",  # Hikvision main
        f"{base}:554/Streaming/Channels/102",  # Hikvision sub
        f"{base}:554/live/ch00_1",        # TVT / generic
        f"{base}:554/h264Preview_01_main", # Amcrest
        f"{base}:554/media/video1",       # Axis
        f"{base}:554/videoMain",          # Foscam
        f"{base}:554/1",                  # Reolink
        f"{base}:554/",                   # Fallback
    ]
