#!/bin/bash
# RPie-Streamer Installer
# Installs Docker and starts the streaming appliance on a Raspberry Pi.
#
# Usage: curl -sSL https://raw.githubusercontent.com/.../install-docker.sh | bash
# Or:    chmod +x install-docker.sh && ./install-docker.sh

set -e

echo "=========================================="
echo "  RPie-Streamer Installer"
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

# Detect current user (for Docker group)
ACTUAL_USER="${SUDO_USER:-$USER}"

echo "[1/6] Ensuring hostname discovery works..."
# Install avahi for .local hostname resolution (mDNS)
apt-get update -qq
apt-get install -y -qq avahi-daemon > /dev/null 2>&1 || true
systemctl enable avahi-daemon > /dev/null 2>&1 || true
systemctl start avahi-daemon > /dev/null 2>&1 || true
HOSTNAME=$(hostname)
echo "  Hostname: $HOSTNAME (reachable as ${HOSTNAME}.local on the network)"

echo "[2/6] Installing Docker..."
if command -v docker &> /dev/null; then
    echo "  Docker already installed: $(docker --version)"
else
    curl -fsSL https://get.docker.com | sh
    echo "  Docker installed successfully."
fi

echo "[3/6] Configuring Docker..."
# Add user to docker group
usermod -aG docker "$ACTUAL_USER" 2>/dev/null || true

# Enable Docker on boot
systemctl enable docker
systemctl start docker

echo "[4/6] Setting up RPie-Streamer..."
INSTALL_DIR="/home/$ACTUAL_USER/rpie-streamer"

if [[ -d "$INSTALL_DIR" ]]; then
    echo "  Existing installation found at $INSTALL_DIR"
    echo "  Updating..."
    cd "$INSTALL_DIR"
else
    echo "  Note: Clone or copy the rpie-streamer project to $INSTALL_DIR"
    echo "  Example: git clone <repo-url> $INSTALL_DIR"

    if [[ ! -d "$INSTALL_DIR" ]]; then
        echo "Error: $INSTALL_DIR does not exist."
        echo ""
        echo "Please copy the rpie-streamer project files to $INSTALL_DIR and re-run this script."
        echo "Or run from the project directory:"
        echo "  cd /path/to/rpie-streamer && sudo ./scripts/install-docker.sh"
        exit 1
    fi
fi

# If script is run from the project directory, use that
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
if [[ -f "$PROJECT_DIR/docker-compose.yml" ]]; then
    INSTALL_DIR="$PROJECT_DIR"
fi

cd "$INSTALL_DIR"

echo "[5/6] Creating data directory..."
mkdir -p data
chown -R "$ACTUAL_USER:$ACTUAL_USER" data

echo "[6/6] Starting RPie-Streamer..."
# Build and start containers
docker compose up -d --build

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
echo "  Complete the setup wizard to start streaming."
echo ""
echo "  To find this Pi later, just browse to:"
echo "    http://${HOSTNAME}.local:8080"
echo "  This works from any device on the same network."
echo ""
echo "  Useful commands (SSH in first: ssh ${ACTUAL_USER}@${HOSTNAME}.local):"
echo "    View logs:     cd $INSTALL_DIR && docker compose logs -f"
echo "    Stop:          cd $INSTALL_DIR && docker compose down"
echo "    Restart:       cd $INSTALL_DIR && docker compose restart"
echo "    Update:        cd $INSTALL_DIR && git pull && docker compose up -d --build"
echo ""
