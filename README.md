# RPie-Streamer

Live video streaming appliance for fishing vessels. Streams from a POE IP security camera through a Raspberry Pi 5 to Cloudflare Stream Live.

## How It Works

```
POE Camera ──RTSP──► Raspberry Pi 5 ──RTMPS──► Cloudflare Stream ──► Website
              LAN        (FFmpeg)           Starlink/Internet
```

The Pi runs two Docker containers:
- **rpie-web** — Web management UI on port 8080
- **rpie-streamer** — FFmpeg engine that streams to Cloudflare

## Hardware Required

- Raspberry Pi 5 (4GB+ recommended, with heatsink/fan)
- POE IP security camera (any ONVIF/RTSP compatible)
- POE switch or injector (powers the camera via Ethernet)
- Ethernet cables
- MicroSD card (32GB+, high-endurance recommended)
- Power supply for the Pi
- Internet: Starlink or standard router

## Wiring

```
Camera ──ethernet──► POE Switch ──ethernet──► Raspberry Pi
                                              │
                                              ──ethernet──► Starlink Router / Internet
```

## Installation

### 1. Prepare the Raspberry Pi

Flash Raspberry Pi OS (64-bit, Bookworm) to the SD card using [Raspberry Pi Imager](https://www.raspberrypi.com/software/). Enable SSH during setup.

### 2. Connect and Install

SSH into the Pi and run:

```bash
# Clone the project
git clone <repo-url> ~/rpie-streamer
cd ~/rpie-streamer

# Run the installer (installs Docker, builds containers, starts the web UI)
sudo ./scripts/install-docker.sh
```

### 3. Complete Setup

Open a browser and go to:

```
http://<pi-ip-address>:8080
```

The setup wizard will walk you through:
1. **Create a password** for the admin interface
2. **Configure the camera** — scan the network or enter the RTSP URL manually
3. **Enter your Cloudflare Stream key** — from your Cloudflare dashboard
4. **Start streaming**

## Cloudflare Setup

1. Log in to [Cloudflare Dashboard](https://dash.cloudflare.com)
2. Go to **Stream** > **Live Inputs**
3. Create a new Live Input
4. Copy the **RTMPS URL** and **Stream Key**
5. Use these in the RPie-Streamer setup wizard

## Common RTSP URLs by Camera Brand

| Brand     | Typical RTSP URL                                     |
|-----------|------------------------------------------------------|
| Uniview   | `rtsp://<ip>:554/unicast/c1/s0/live`                 |
| Hikvision | `rtsp://<ip>:554/Streaming/Channels/101`             |
| Dahua     | `rtsp://<ip>:554/cam/realmonitor?channel=1&subtype=0`|
| Reolink   | `rtsp://<ip>:554/1`                                  |
| Amcrest   | `rtsp://<ip>:554/h264Preview_01_main`                |
| Axis      | `rtsp://<ip>:554/media/video1`                       |
| Generic   | `rtsp://<ip>:554/stream1`                            |

## Managing the Streamer

### Web Interface

Access the dashboard at `http://<pi-ip>:8080`:
- **Dashboard** — Live stream status, FPS, bitrate, system resources
- **Settings** — Camera, Cloudflare, encoding, and behavior configuration
- **Logs** — Real-time FFmpeg and system logs

### Command Line

```bash
cd ~/rpie-streamer

# View live logs
docker compose logs -f

# Restart everything
docker compose restart

# Stop everything
docker compose down

# Start again
docker compose up -d
```

## Updating

```bash
cd ~/rpie-streamer
git pull                          # Get latest code
docker compose up -d --build      # Rebuild and restart
```

## Troubleshooting

### Stream won't start
- Check the camera RTSP URL is correct (use "Test Connection" in Settings)
- Verify the Cloudflare stream key is correct
- Check logs for error messages

### Poor video quality / stuttering
- Reduce video bitrate in Settings (try 1500k for low bandwidth)
- Switch encoding mode to "Copy" if the camera outputs H.264
- Check network latency on the Dashboard

### Can't access the web UI
- Verify the Pi is connected to the network: `ping <pi-ip>`
- Check Docker is running: `sudo systemctl status docker`
- Check containers: `docker compose ps`

### Camera not found in scan
- Ensure the camera and Pi are on the same network/subnet
- Try entering the RTSP URL manually
- Check if the camera requires credentials

### High CPU usage
- If encoding mode is "Transcode", the Pi is re-encoding the video
- Switch to "Auto" or "Copy" mode — most cameras output H.264 natively
- Reduce resolution to 720p in Settings

## Architecture

The application is designed for unattended operation:

- **Auto-start on boot** — Docker starts with the OS, containers restart automatically
- **Crash recovery** — FFmpeg auto-restarts with exponential backoff (5s → 120s)
- **Network resilience** — Short Starlink dropouts handled by FFmpeg reconnection; extended outages trigger automatic recovery
- **SD card protection** — All temporary data stored in RAM (tmpfs), only config.yaml writes to disk
- **No default credentials** — Setup wizard forces password creation
