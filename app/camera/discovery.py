from __future__ import annotations

import logging
import socket
import uuid
import xml.etree.ElementTree as ET
from typing import Optional

logger = logging.getLogger(__name__)

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


def discover_cameras(timeout: float = 5.0) -> list[dict]:
    """Scan the local network for ONVIF cameras via WS-Discovery.

    Sends a multicast probe and collects responses from ONVIF-compliant
    devices. Returns basic info about each device found.

    Args:
        timeout: Seconds to wait for responses

    Returns:
        List of dicts with keys: ip, port, xaddrs (service URLs), scopes
    """
    message_id = str(uuid.uuid4())
    probe_msg = PROBE_TEMPLATE.format(message_id=message_id).encode("utf-8")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.settimeout(timeout)

    # Enable multicast
    sock.setsockopt(
        socket.IPPROTO_IP,
        socket.IP_MULTICAST_TTL,
        2,
    )

    cameras = []
    seen_ips = set()

    try:
        sock.sendto(probe_msg, (WS_DISCOVERY_ADDR, WS_DISCOVERY_PORT))
        logger.info("Sent WS-Discovery probe, waiting %ss for responses...", timeout)

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

    return {
        "ip": ip,
        "xaddrs": xaddrs,
        "name": name or f"Camera at {ip}",
        "hardware": hardware,
        "scopes": scopes,
    }


def get_common_rtsp_urls(ip: str, username: str = "", password: str = "") -> list[str]:
    """Generate common RTSP URL patterns to try for a given camera IP.

    Different manufacturers use different RTSP paths. This returns a list
    of the most common patterns to try via ffprobe.
    """
    auth = f"{username}:{password}@" if username else ""
    base = f"rtsp://{auth}{ip}"

    return [
        f"{base}:554/stream1",           # Generic
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
