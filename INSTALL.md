# RPie-Streamer — Detailed Installation Guide

This guide walks through every step of setting up the streaming appliance, from unboxing the Raspberry Pi to a working live stream on Cloudflare.

---

## Part 1: Hardware Setup

### What You Need

| Item | Notes |
|------|-------|
| Raspberry Pi 5 (4GB or 8GB) | CanaKit with heatsink/fan recommended |
| MicroSD card (32GB+) | High-endurance card recommended (e.g. SanDisk MAX Endurance) |
| USB-C power supply for RPi 5 | 5V/5A (27W) — must be the official RPi 5 PSU or equivalent |
| POE IP security camera | Any ONVIF/RTSP compatible camera |
| POE switch or POE injector | Powers the camera over Ethernet |
| 2x Ethernet cables | Camera → switch, switch → RPi |
| Starlink kit (or home router) | For internet uplink |

### Wiring Diagram

```
                    POE Switch
                   ┌──────────┐
  Camera ──eth──►  │ Port 1   │  (POE powers camera)
                   │          │
  RPi 5  ◄──eth── │ Port 2   │  (data only)
                   └──────────┘
                       │
                  (optional: if switch needs uplink)
                       │
               Starlink Router / Home Router
                       │
                    Internet
```

If your Starlink router and POE switch are separate:
- Camera → POE switch (Port 1, provides power + data)
- RPi 5 → POE switch (Port 2, data only)
- POE switch uplink → Starlink router (for internet)

If using a simple POE injector instead of a switch:
- Camera → POE injector → RPi Ethernet port
- RPi WiFi → Starlink router (for internet)

### Physical Assembly

1. Insert the microSD card into the RPi 5 (bottom slot)
2. Attach the heatsink/fan to the RPi (follow CanaKit instructions)
3. Connect Ethernet cable from the POE switch to the RPi's Ethernet port
4. **Do not** plug in the USB-C power yet — we need to flash the SD card first

---

## Part 2: Preparing the Raspberry Pi

### Step 2.1: Flash Raspberry Pi OS

On your computer (Mac, Windows, or Linux):

1. Download and install [Raspberry Pi Imager](https://www.raspberrypi.com/software/)
2. Insert the microSD card into your computer (use an adapter if needed)
3. Open Raspberry Pi Imager
4. Click **Choose Device** → select **Raspberry Pi 5**
5. Click **Choose OS** → select **Raspberry Pi OS (64-bit)** (the default, under "Raspberry Pi OS (other)" if needed)
6. Click **Choose Storage** → select your microSD card
7. Click **Next**

### Step 2.2: Configure OS Settings (Important!)

When prompted "Would you like to apply OS customisation settings?", click **Edit Settings**:

**General tab:**
- Set hostname: `rpie-streamer` (or any name you'll remember)
- Set username: `pi` (or your preference)
- Set password: Choose something secure — you'll need this to SSH in
- Configure WiFi: **Only if** the RPi won't be on Ethernet for internet
  - Enter your WiFi network name and password
  - Set country code (e.g. NZ, AU, US)
- Set locale: Your timezone

**Services tab:**
- **Enable SSH** — check "Use password authentication"

Click **Save**, then **Yes** to apply settings, then **Yes** to flash.

Wait for the flash to complete (takes a few minutes).

### Step 2.3: First Boot

1. Remove the microSD from your computer
2. Insert it into the Raspberry Pi
3. Connect the Ethernet cable (RPi → POE switch or router)
4. Plug in the USB-C power supply
5. Wait 1-2 minutes for the Pi to boot up

### Step 2.4: Find the Pi's IP Address

You don't need to know the Pi's IP address. Raspberry Pi OS broadcasts its hostname on the local network via mDNS, so you can use the `.local` hostname instead.

If you set the hostname to `rpie-streamer` during OS setup (Step 2.2), the Pi is reachable as **`rpie-streamer.local`** from any device on the same network.

### Step 2.5: Connect via SSH

From your computer's terminal (Mac/Linux) or PowerShell (Windows):

```bash
ssh pi@rpie-streamer.local
```

Replace `pi` with your username if you chose a different one.

This `.local` address works on Mac, Windows 10/11, and most Linux systems automatically. You'll use it throughout this guide instead of an IP address.

**If `.local` doesn't work** (rare — some older Android devices or corporate networks block mDNS), find the IP by one of these methods:
- **Router admin page:** Log into your router (usually `192.168.1.1`) → connected devices → find `rpie-streamer`
- **Fing app:** Free mobile app — connect to same network, scan, find the Pi
- **Monitor connected to Pi:** Log in and run `hostname -I`

Then use the IP instead: `ssh pi@192.168.1.50`

- Type `yes` when asked about the fingerprint
- Enter your password

You should see a prompt like:
```
pi@rpie-streamer:~ $
```

You're now connected to the Raspberry Pi.

---

## Part 3: Install RPie-Streamer

### Step 3.1: Update the System

```bash
sudo apt update && sudo apt upgrade -y
```

This may take a few minutes. Ensures all system packages are current.

### Step 3.2: Install Git

```bash
sudo apt install -y git
```

### Step 3.3: Clone the Project

```bash
cd ~
git clone <REPO_URL> rpie-streamer
```

Replace `<REPO_URL>` with the actual repository URL.

If the repo is private, you'll need to set up a personal access token or SSH key. For a simple approach with HTTPS:
```bash
git clone https://<TOKEN>@github.com/<user>/rpie-streamer.git rpie-streamer
```

### Step 3.4: Run the Installer

```bash
cd ~/rpie-streamer
sudo ./scripts/install-docker.sh
```

This script will:
1. Install Docker (if not already installed)
2. Add your user to the `docker` group
3. Enable Docker to start on boot
4. Build the Docker containers
5. Start the web UI

The output will end with something like:
```
==========================================
  Installation Complete!
==========================================

  Open your browser and go to:

    http://192.168.1.50:8080
```

**Note:** You may need to log out and back in (or reboot) for Docker group permissions to take effect:
```bash
sudo reboot
```

After reboot, SSH back in and verify Docker is running:
```bash
docker ps
```

You should see two containers: `rpie-web` and `rpie-streamer`.

---

## Part 4: Configure the Stream

### Step 4.1: Open the Web UI

On any device connected to the same network (phone, laptop, tablet), open a browser and go to:

```
http://rpie-streamer.local:8080
```

This works from any device on the same network — no need to know the IP address.

### Step 4.2: Complete the Setup Wizard

**Step 1: Create Admin Account**
- Choose a username (default: `admin`)
- Set a password (minimum 6 characters)
- Click **Next: Camera Setup**

**Step 2: Configure the Camera**
- Click **Scan** to search for cameras on the network
  - If your camera appears, click it to auto-fill the RTSP URL
  - If not found, enter the RTSP URL manually (see table below)
- If your camera requires credentials, expand "Camera Credentials" and enter them
- Click **Test Connection** to verify the camera is reachable
  - You should see: `Connected! h264 1920x1080 @ 30fps (passthrough OK)`
- Click **Next: Cloudflare Setup**

**Common RTSP URLs by camera brand:**

| Device | URL Pattern |
|--------|-------------|
| **Uniview NVR (ch1)** | **`rtsp://192.168.1.x:554/unicast/c1/s0/live`** |
| Uniview NVR (ch2) | `rtsp://192.168.1.x:554/unicast/c2/s0/live` |
| Uniview camera | `rtsp://192.168.1.x:554/unicast/c1/s0/live` |
| Hikvision | `rtsp://192.168.1.x:554/Streaming/Channels/101` |
| Dahua | `rtsp://192.168.1.x:554/cam/realmonitor?channel=1&subtype=0` |
| Reolink | `rtsp://192.168.1.x:554/1` |
| Amcrest | `rtsp://192.168.1.x:554/h264Preview_01_main` |
| Axis | `rtsp://192.168.1.x:554/media/video1` |
| Generic | `rtsp://192.168.1.x:554/stream1` |

**Important — NVR setup:** If you have a Uniview NVR (e.g. NVR501-04B-P4-IQ), connect to the **NVR's IP address**, not the camera's. The NVR has a built-in POE switch that creates an internal network for cameras — your Pi can't reach the cameras directly. The NVR exposes each camera as a channel: `c1` = port 1, `c2` = port 2, etc.

Replace `192.168.1.x` with your camera's actual IP address.

**Step 3: Cloudflare Stream Setup**
- Leave the RTMPS Endpoint as the default (`rtmps://live.cloudflare.com:443/live/`) unless Cloudflare gives you a different one
- Paste your **Stream Key** from Cloudflare (see Part 5 below for how to get this)
- Click **Next: Review & Start**

**Step 4: Review and Start**
- Review your settings
- Click **Start Streaming**

The stream should now be live! Check your Cloudflare dashboard to confirm.

---

## Part 5: Setting Up Cloudflare Stream

### Step 5.1: Create a Cloudflare Account

If you don't have one, sign up at [cloudflare.com](https://www.cloudflare.com/).

### Step 5.2: Enable Cloudflare Stream

1. Log into the [Cloudflare Dashboard](https://dash.cloudflare.com/)
2. Click **Stream** in the left sidebar
3. You may need to add a payment method (Stream is a paid service)

### Step 5.3: Create a Live Input

1. Go to **Stream** → **Live Inputs**
2. Click **Create a live input**
3. Give it a name (e.g. "Vessel Camera 1")
4. Select **RTMPS** as the protocol
5. Click **Create**

### Step 5.4: Copy Your Credentials

After creating the live input, you'll see:
- **RTMPS URL**: `rtmps://live.cloudflare.com:443/live/`
- **Stream Key**: A long alphanumeric string

Copy the **Stream Key** — you'll need this for the RPie-Streamer setup wizard (Step 4.2, Step 3).

### Step 5.5: Embed on Your Website

Cloudflare provides an embed code for your live stream. On the Live Input page:
1. Click the live input you created
2. Find the **Embed** section
3. Copy the `<iframe>` embed code
4. Paste it into your website's HTML

---

## Part 6: Verify Everything Works

### Check the Dashboard

Go to `http://rpie-streamer.local:8080` and look at the Dashboard:

- **Stream Status** should show a green dot and "Streaming"
- **FPS** should show your camera's framerate (e.g. 30.0)
- **Bitrate** should show the current bitrate (e.g. 2.5 Mbps)
- **Speed** should be 1.00x (if less, the Pi can't keep up — reduce quality in Settings)
- **Network** should show "Connected" with latency

### Check Cloudflare

1. Go to Cloudflare Dashboard → Stream → Live Inputs
2. Your live input should show as "Active" or "Connected"
3. Click it to see a preview of the stream

### Test Resilience

- **Reboot the Pi:** `sudo reboot` — after 1-2 minutes, the stream should resume automatically
- **Kill the stream:** On the Dashboard, click Stop, then Start — should reconnect
- **Unplug internet briefly:** Stream will reconnect automatically within 30 seconds

---

## Part 7: Ongoing Maintenance

### Updating the Software

SSH into the Pi:
```bash
cd ~/rpie-streamer
git pull
docker compose up -d --build
```

### Viewing Logs

Via the web UI: Go to the **Logs** page.

Via SSH:
```bash
cd ~/rpie-streamer
docker compose logs -f              # Both services
docker compose logs -f streamer     # Stream engine only
docker compose logs -f web          # Web UI only
```

### Changing the Stream Key

If you need to point to a different Cloudflare stream:
1. Open the web UI → Settings
2. Update the Stream Key
3. Click **Save & Restart Stream**

### Checking System Health

Via the web UI Dashboard, or via SSH:
```bash
# CPU temperature
cat /sys/class/thermal/thermal_zone0/temp
# Divide by 1000 for Celsius (e.g. 45000 = 45.0C)

# Docker status
docker ps
docker stats --no-stream

# Disk usage
df -h /
```

### If Something Goes Wrong

```bash
# Check container status
cd ~/rpie-streamer
docker compose ps

# Check for crash loops
docker compose logs --tail=50 streamer

# Full restart
docker compose down
docker compose up -d

# Nuclear option: rebuild everything
docker compose down
docker compose build --no-cache
docker compose up -d
```

---

## Part 8: Network Considerations for Starlink

### Bandwidth Requirements

| Quality | Bitrate | Min Upload Needed |
|---------|---------|-------------------|
| 720p 30fps | 1.5-2.5 Mbps | 3 Mbps |
| 1080p 30fps | 2.5-4 Mbps | 5 Mbps |
| 1080p 30fps (high quality) | 4-6 Mbps | 8 Mbps |

Starlink typically provides 5-20 Mbps upload, but can dip to 1-2 Mbps during congestion or obstructions.

### Recommendations

- **Default bitrate is 2500k** (2.5 Mbps) — safe for most Starlink conditions
- If the stream stutters, reduce to 1500k in Settings
- Use **codec copy mode** whenever possible (Auto mode handles this) — no CPU cost
- Starlink has brief 5-15 second dropouts during satellite handoffs — the streamer handles these automatically via FFmpeg's reconnect flags
- For extended outages (>60 seconds), the network monitor will detect the outage and automatically restart the stream when connectivity returns

### Static IP on the Pi

If you want the Pi to always have the same IP (recommended):

```bash
sudo nmcli con mod "Wired connection 1" \
  ipv4.method manual \
  ipv4.addresses 192.168.1.50/24 \
  ipv4.gateway 192.168.1.1 \
  ipv4.dns "1.1.1.1,8.8.8.8"

sudo nmcli con up "Wired connection 1"
```

Replace the IP addresses with ones appropriate for your network.

---

## Troubleshooting Quick Reference

| Problem | Check | Fix |
|---------|-------|-----|
| Can't SSH into Pi | Is Pi powered? Ethernet connected? | Check cable, try WiFi, connect monitor |
| Web UI won't load | Is Docker running? | `sudo systemctl start docker && cd ~/rpie-streamer && docker compose up -d` |
| Camera not found in scan | Same subnet? Camera powered? | Enter RTSP URL manually, check camera IP |
| "Test Connection" fails | Wrong RTSP URL? Credentials? | Try different URL patterns from the table, check camera admin page |
| Stream starts but Cloudflare shows nothing | Wrong stream key? Firewall? | Verify key in Cloudflare dashboard, check port 443 outbound |
| High CPU (>80%) | Transcoding instead of copy | Change encoding mode to "Auto" or "Copy" in Settings |
| Stream stutters | Low bandwidth | Reduce bitrate to 1500k, reduce resolution to 720p |
| Stream keeps restarting | Camera or network unstable | Check logs for errors, verify camera connection |
