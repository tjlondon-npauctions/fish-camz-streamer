#!/bin/bash
# Configure networking for a Fish Camz Pi:
#   - Static IP on secondary ethernet (USB dongle) for POE camera
#   - dnsmasq DHCP server so cameras get an IP automatically
#   - GPS auto-detection and gpsd setup
#
# Usage: sudo ./scripts/setup-network.sh [interface] [ip_address]
# Defaults: interface=eth1, ip=192.168.0.10/24
#
# This makes camera setup plug-and-play: plug a camera into the USB
# ethernet adapter and it gets a DHCP lease, discoverable via ONVIF.

set -e

IFACE="${1:-eth1}"
IP_ADDR="${2:-192.168.0.10/24}"
# Extract base subnet for DHCP range (e.g. 192.168.0 from 192.168.0.10/24)
SUBNET_BASE=$(echo "$IP_ADDR" | cut -d/ -f1 | sed 's/\.[0-9]*$//')
NETMASK="255.255.255.0"

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

echo "[1/4] Configuring static IP on $IFACE ($IP_ADDR)..."

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

# ── dnsmasq DHCP server for camera discovery ──

echo ""
echo "[2/4] Setting up DHCP server for camera auto-discovery..."

if command -v dnsmasq &>/dev/null; then
    echo "  dnsmasq already installed."
else
    echo "  Installing dnsmasq..."
    apt-get update -qq
    apt-get install -y -qq dnsmasq > /dev/null 2>&1
fi

DNSMASQ_CONF="/etc/dnsmasq.d/camera-dhcp.conf"
cat > "$DNSMASQ_CONF" << EOF
# DHCP server for camera on $IFACE
# Managed by Fish Camz setup-network.sh — do not edit manually
interface=$IFACE
bind-interfaces

# Disable DNS (we only need DHCP)
port=0

# DHCP range: .50 to .150, 24h lease
dhcp-range=${SUBNET_BASE}.50,${SUBNET_BASE}.150,${NETMASK},24h

# Log DHCP events for debugging
log-dhcp
EOF

systemctl enable dnsmasq > /dev/null 2>&1
systemctl restart dnsmasq
echo "  dnsmasq configured: DHCP range ${SUBNET_BASE}.50-150 on $IFACE"

# ── GPS setup ──

echo ""
echo "[3/4] Checking for GPS device..."

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
echo "[4/4] Verifying configuration..."
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

echo ""
echo "  DHCP server: dnsmasq on $IFACE (${SUBNET_BASE}.50-150)"

if systemctl is-active dnsmasq > /dev/null 2>&1; then
    echo "  dnsmasq status: running"
else
    echo "  WARNING: dnsmasq is not running! Check: journalctl -u dnsmasq"
fi

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
echo "  Camera subnet: ${SUBNET_BASE}.x"
echo "  Pi address on camera network: $(echo "$IP_ADDR" | cut -d/ -f1)"
echo "  Cameras plugged in will get an IP via DHCP automatically."
echo ""
