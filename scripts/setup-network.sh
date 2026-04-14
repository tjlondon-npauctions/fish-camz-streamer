#!/bin/bash
# Configure static IP on a secondary ethernet interface for POE camera connection.
#
# This sets up a persistent NetworkManager connection so the USB ethernet
# dongle (eth1) always gets 192.168.0.10/24 on boot — allowing the Pi
# to reach a POE camera on that subnet.
#
# Usage: sudo ./scripts/setup-network.sh [interface] [ip_address]
# Defaults: interface=eth1, ip=192.168.0.10/24
#
# Also enables gpsd for USB GPS dongles if a GPS device is detected.

set -e

IFACE="${1:-eth1}"
IP_ADDR="${2:-192.168.0.10/24}"

echo "=========================================="
echo "  Network Setup for Fish Camz"
echo "=========================================="
echo ""

# Check for root/sudo
if [[ $EUID -ne 0 ]]; then
    echo "This script requires sudo. Re-running with sudo..."
    exec sudo "$0" "$@"
fi

# ── Static IP on secondary ethernet (for POE camera) ──

echo "[1/3] Configuring static IP on $IFACE ($IP_ADDR)..."

if ! ip link show "$IFACE" &>/dev/null; then
    echo "  Warning: Interface $IFACE not found."
    echo "  Plug in the USB ethernet dongle and re-run this script."
    echo "  Available interfaces:"
    ip -brief link show | grep -v "lo\|docker\|br-\|veth"
    echo ""
else
    # Check if a NetworkManager connection already exists for this interface
    if nmcli connection show "$IFACE" &>/dev/null; then
        echo "  Updating existing connection '$IFACE'..."
        nmcli connection modify "$IFACE" \
            ipv4.method manual \
            ipv4.addresses "$IP_ADDR" \
            connection.autoconnect yes
    else
        echo "  Creating new connection '$IFACE'..."
        nmcli connection add \
            type ethernet \
            con-name "$IFACE" \
            ifname "$IFACE" \
            ipv4.method manual \
            ipv4.addresses "$IP_ADDR" \
            connection.autoconnect yes
    fi

    # Bring it up now
    nmcli connection up "$IFACE" 2>/dev/null || true
    echo "  Done: $IFACE has IP $(ip -4 addr show "$IFACE" | grep inet | awk '{print $2}')"
fi

# ── GPS setup ──

echo ""
echo "[2/3] Checking for GPS device..."

GPS_DEV=""
for dev in /dev/ttyACM* /dev/ttyUSB*; do
    if [[ -e "$dev" ]]; then
        GPS_DEV="$dev"
        break
    fi
done

if [[ -n "$GPS_DEV" ]]; then
    echo "  Found GPS device at $GPS_DEV"

    # Install gpsd if not present
    if ! command -v gpsd &>/dev/null; then
        echo "  Installing gpsd..."
        apt-get update -qq
        apt-get install -y -qq gpsd gpsd-clients > /dev/null 2>&1
    fi

    # Configure gpsd
    cat > /etc/default/gpsd << EOF
# GPS daemon configuration (managed by setup-network.sh)
START_DAEMON="true"
USBAUTO="true"
DEVICES="$GPS_DEV"
GPSD_OPTIONS="-n"
EOF

    systemctl enable gpsd
    systemctl restart gpsd
    echo "  gpsd configured and started on $GPS_DEV"
else
    echo "  No GPS device found (plug in USB GPS dongle if needed)"
fi

# ── Summary ──

echo ""
echo "[3/3] Verifying configuration..."
echo ""
echo "  Network interfaces:"
ip -brief addr show | grep -v "lo\|docker\|br-\|veth" | while read -r line; do
    echo "    $line"
done

echo ""
echo "  NetworkManager connections (autoconnect):"
nmcli -t -f NAME,AUTOCONNECT connection show | while IFS=: read -r name auto; do
    if [[ "$auto" == "yes" ]]; then
        echo "    $name: auto-connect on boot"
    fi
done

if [[ -n "$GPS_DEV" ]]; then
    echo ""
    echo "  GPS: $GPS_DEV via gpsd"
fi

echo ""
echo "=========================================="
echo "  Network setup complete!"
echo "=========================================="
echo ""
echo "  These settings persist across reboots."
echo "  Camera subnet: $(echo "$IP_ADDR" | cut -d/ -f1 | sed 's/\.[0-9]*$/.x/')"
echo "  Pi address on camera network: $(echo "$IP_ADDR" | cut -d/ -f1)"
echo ""
