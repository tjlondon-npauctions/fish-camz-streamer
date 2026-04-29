#!/bin/bash
# RPie-Streamer Installer
# Sets up a Raspberry Pi as a Fish Camz streaming appliance:
#   1. Installs Docker
#   2. Configures camera network (static IP + DHCP server)
#   3. Pulls container images and starts services
#
# Usage: cd rpie-streamer && sudo ./scripts/install-docker.sh
#
# Prerequisites:
#   - Raspberry Pi OS (Bookworm or later)
#   - Internet connection
#   - USB ethernet adapter plugged in (for camera network)

set -e

echo "=========================================="
echo "  Fish Camz Streamer Installer"
echo "=========================================="
echo ""

# Check we're on Linux (RPi)
if [[ "$(uname)" != "Linux" ]]; then
    echo "Error: This installer is designed for Raspberry Pi OS (Linux)."
    echo "You appear to be running $(uname)."
    exit 1
fi

# Check for root/sudo
if [[ $EUID -ne 0 ]]; then
    echo "This script requires sudo privileges."
    echo "Re-running with sudo..."
    exec sudo "$0" "$@"
fi

# Detect current user (for Docker group and install dir)
ACTUAL_USER="${SUDO_USER:-$USER}"

# Find project directory (script lives in scripts/)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
INSTALL_DIR="/home/$ACTUAL_USER/rpie-streamer"

# If running from the project directory, use it
if [[ -f "$PROJECT_DIR/docker-compose.yml" ]]; then
    INSTALL_DIR="$PROJECT_DIR"
fi

if [[ ! -f "$INSTALL_DIR/docker-compose.yml" ]]; then
    echo "Error: docker-compose.yml not found."
    echo ""
    echo "Please clone or copy the rpie-streamer project to $INSTALL_DIR first:"
    echo "  git clone <repo-url> $INSTALL_DIR"
    echo ""
    echo "Then run this script again."
    exit 1
fi

cd "$INSTALL_DIR"

# ── Step 1: System dependencies ──

echo "[1/5] Installing system dependencies..."

apt-get update -qq

# mDNS for .local hostname resolution
apt-get install -y -qq avahi-daemon > /dev/null 2>&1 || true
systemctl enable avahi-daemon > /dev/null 2>&1 || true
systemctl start avahi-daemon > /dev/null 2>&1 || true

HOSTNAME=$(hostname)
echo "  Hostname: $HOSTNAME (reachable as ${HOSTNAME}.local)"

# ── Step 2: Docker ──

echo ""
echo "[2/5] Installing Docker..."

if command -v docker &> /dev/null; then
    echo "  Docker already installed: $(docker --version)"
else
    curl -fsSL https://get.docker.com | sh
    echo "  Docker installed."
fi

# Add user to docker group and enable on boot
usermod -aG docker "$ACTUAL_USER" 2>/dev/null || true
systemctl enable docker
systemctl start docker

# ── Step 3: Camera network ──

echo ""
echo "[3/5] Setting up camera network..."

if [[ -f "$INSTALL_DIR/scripts/setup-network.sh" ]]; then
    bash "$INSTALL_DIR/scripts/setup-network.sh"
else
    echo "  Warning: setup-network.sh not found, skipping network setup."
    echo "  Run it manually later: sudo ./scripts/setup-network.sh"
fi

# ── Step 4: Data directory ──

echo ""
echo "[4/5] Preparing data directory..."

mkdir -p data
chown -R "$ACTUAL_USER:$ACTUAL_USER" data

# ── Step 5: Start services ──

echo ""
echo "[5/5] Starting Fish Camz services..."

docker compose pull
docker compose up -d

echo ""
echo "=========================================="
echo "  Installation Complete!"
echo "=========================================="
echo ""

# Get IP and hostname
IP=$(hostname -I | awk '{print $1}')
HOSTNAME=$(hostname)

echo "  Open your browser and go to:"
echo ""
echo "    http://${HOSTNAME}.local:8080"
echo ""
echo "  Or by IP address:"
echo ""
echo "    http://$IP:8080"
echo ""
echo "  Complete the setup wizard to configure your camera and start streaming."
echo ""
echo "  What's running:"
echo "    - Web UI on port 8080 (setup wizard + dashboard)"
echo "    - Stream engine (starts after camera is configured)"
echo "    - Watchtower (auto-updates every 5 minutes from GHCR)"
echo "    - dnsmasq (DHCP for cameras on USB ethernet)"
echo ""
echo "  Useful commands (SSH in first: ssh ${ACTUAL_USER}@${HOSTNAME}.local):"
echo "    View logs:     cd $INSTALL_DIR && docker compose logs -f"
echo "    Stop:          cd $INSTALL_DIR && docker compose down"
echo "    Restart:       cd $INSTALL_DIR && docker compose restart"
echo "    Stream status: curl -s http://localhost:8080/api/status | python3 -m json.tool"
echo ""
