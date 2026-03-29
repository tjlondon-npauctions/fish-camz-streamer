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

echo "[1/5] Installing Docker..."
if command -v docker &> /dev/null; then
    echo "  Docker already installed: $(docker --version)"
else
    curl -fsSL https://get.docker.com | sh
    echo "  Docker installed successfully."
fi

echo "[2/5] Configuring Docker..."
# Add user to docker group
usermod -aG docker "$ACTUAL_USER" 2>/dev/null || true

# Enable Docker on boot
systemctl enable docker
systemctl start docker

echo "[3/5] Setting up RPie-Streamer..."
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

echo "[4/5] Creating data directory..."
mkdir -p data
chown -R "$ACTUAL_USER:$ACTUAL_USER" data

echo "[5/5] Starting RPie-Streamer..."
# Build and start containers
docker compose up -d --build

echo ""
echo "=========================================="
echo "  Installation Complete!"
echo "=========================================="
echo ""

# Get IP address
IP=$(hostname -I | awk '{print $1}')
echo "  Open your browser and go to:"
echo ""
echo "    http://$IP:8080"
echo ""
echo "  Complete the setup wizard to start streaming."
echo ""
echo "  Useful commands:"
echo "    View logs:     cd $INSTALL_DIR && docker compose logs -f"
echo "    Stop:          cd $INSTALL_DIR && docker compose down"
echo "    Restart:       cd $INSTALL_DIR && docker compose restart"
echo "    Update:        cd $INSTALL_DIR && docker compose pull && docker compose up -d"
echo ""
