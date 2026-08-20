# New Pi Provisioning Runbook

Bare Pi → live vessel on fishcamz.com. Supersedes `INSTALL.md`, which still
documents the old Cloudflare-RTMPS path and is kept only for reference.

Allow ~45 minutes, most of it `apt upgrade` and the Docker pull.

---

## Part 0 — Do this before the Pi arrives

| # | Prep | Detail |
|---|------|--------|
| 1 | Flash the SD card | See Part 1 — do it now so the Pi boots straight into a usable state |
| 2 | Create the vessel in admin | https://www.fishcamz.com/admin/vessels/new → copy the **registration token** (shown once; regenerate from the vessel page if lost) |
| 3 | Pick a **stream path** | Unique per vessel, e.g. `patriot-deck`, `tomoffice`. This is the folder inside the shared Bunny storage zone — **two vessels on the same path will overwrite each other's live stream** |
| 4 | Pick a **camera subnet** | Never `192.168.0.x` or `192.168.1.x` — routers use those and the collision kills inbound SSH/web (see Troubleshooting). Use `192.168.5.10/24`, or `192.168.10.10/24` if this Pi shares a boat with another |
| 5 | Have the Bunny storage API key to hand | Bunny dashboard → Storage → `fish-camz` → FTP & API Access → Password. Same key every vessel |
| 6 | Hardware | Pi 5 + official 27W PSU, 32GB+ high-endurance SD, **USB ethernet adapter** (camera LAN), PoE switch/injector, PoE camera, optional USB GPS dongle |

### Values you'll paste in later

```
Backend URL:        https://www.fishcamz.com
Registration token: <from step 2>
Bunny storage zone: fish-camz
Bunny region:       la           ← required; the wizard does not ask for it
Bunny CDN URL:      https://cdn.fishcamz.com
Bunny stream path:  <from step 3>
```

---

## Part 1 — Flash the SD card

Raspberry Pi Imager → Raspberry Pi 5 → Raspberry Pi OS (64-bit) → your card.

In **Edit Settings** before writing:

- **Hostname:** `fishcamz-<vessel>` (e.g. `fishcamz-patriot`) — you'll SSH to `<hostname>.local`
- **Username:** `admin` (matches the existing fleet)
- **Password:** set one, save it to the password manager
- **Locale/timezone:** set correctly — the Pi's clock feeds DVR timestamps, and a wrong clock is painful to fix later
- **Services → Enable SSH** with password authentication
- **WiFi:** only if the Pi won't have ethernet uplink

## Part 2 — First boot

1. SD in, built-in ethernet (`eth0`) → boat/home router (internet uplink)
2. USB ethernet adapter (`eth1`) → PoE switch → camera. Leave the camera powered off/unplugged for now if you like; DHCP will pick it up whenever it comes up
3. Power on, wait ~2 minutes

```bash
ssh admin@fishcamz-<vessel>.local
```

If `.local` fails, find the IP on the router's client list. mDNS is unreliable on some networks.

```bash
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y git
```

## Part 3 — Install

The repo is public, so no token needed:

```bash
cd ~
git clone https://github.com/tjlondon-npauctions/fish-camz-streamer.git rpie-streamer
cd ~/rpie-streamer
sudo ./scripts/install-docker.sh
```

This installs Docker + avahi, runs `setup-network.sh`, then pulls and starts the containers.

**Then set the camera network explicitly** — the installer's default is `192.168.0.10/24`,
which collides with common router LANs:

```bash
sudo ./scripts/setup-network.sh eth1 192.168.5.10/24
```

Verify before moving on:

```bash
ip -brief addr show | grep -v 'lo\|docker\|veth'   # eth0 = router DHCP, eth1 = 192.168.5.10
ip route | awk '{print $1, $3, $5}'                # no /24 listed twice on two interfaces
cat /etc/dnsmasq.d/camera-dhcp.conf                # dhcp-range must match the subnet you chose
systemctl is-active dnsmasq                        # active
docker ps                                          # rpie-web, rpie-streamer, rpie-watchtower
```

Reboot once so the `docker` group applies to `admin`:

```bash
sudo reboot
```

## Part 4 — Setup wizard

Browse to `http://fishcamz-<vessel>.local:8080` from any device on the same network.

1. **Password** — vessel name, username `admin`, a password
2. **Camera** — power the camera on, wait 30s for its DHCP lease, then **Scan**.
   Enter camera credentials first if it needs them; **Test Connection** should report
   `passthrough OK`. If the scan misses it, check the lease:
   `journalctl -u dnsmasq | tail -20`, then enter the RTSP URL manually
   (Uniview: `rtsp://192.168.5.x:554/unicast/c1/s0/live`)
3. **Output** — choose **HLS**, then:
   - Storage zone: `fish-camz`
   - API key: the Bunny storage password
   - CDN URL: `https://cdn.fishcamz.com`
4. **Start**

## Part 5 — Settings (the wizard doesn't cover these)

`http://fishcamz-<vessel>.local:8080/settings`:

- **Bunny region:** `la` — **mandatory.** Blank means the Pi uploads to Bunny's default
  region endpoint, not the LA zone where our storage actually lives
- **Bunny stream path:** the unique slug from Part 0 step 3
- **Backend URL:** `https://www.fishcamz.com`
- **Registration token:** paste it
- **Heartbeat interval:** 60

Save & Restart Stream.

## Part 6 — GPS (if a dongle is fitted)

`setup-network.sh` already detected the dongle and configured `gpsd` on the host.
There is **no GPS toggle in the web UI** — it has to go in the config file:

```bash
cgps -s                                    # confirm a fix first (may take minutes with a cold sky view)
cd ~/rpie-streamer
sudo sed -i 's/^  enabled: false/  enabled: true/' data/config.yaml   # check it hit the gps: block
grep -A1 '^gps:' data/config.yaml
docker restart rpie-web rpie-streamer
curl -s localhost:8080/api/gps
```

GPS is what drives vessel location on the site and dock-aware auto-start/stop.

## Part 7 — Verify end to end

On the Pi:

```bash
curl -s localhost:8080/api/status | python3 -m json.tool | head -30
docker logs --tail 50 rpie-streamer
```

Look for: `running: true`, non-zero fps/bitrate, `speed` at 1.00x, and **copy mode**
(not transcode) if the camera is H.264.

From your Mac:

```bash
curl -s -o /dev/null -w "%{http_code}\n" https://cdn.fishcamz.com/<stream-path>/live.m3u8
```

Cloud side:

- `/admin/devices` — a device row auto-creates on the first heartbeat, showing
  version, temperature, disk
- `/admin/vessels/<id>` — `rpiLinked`, live status, and the linked-device card
- Public vessel page — the player should show live video

## Part 8 — Optional extras

- **Dock auto-stream:** vessel admin page → set home port lat/lng, enable Dock Control.
  Requires a GPS fix; it starts/stops the stream on the geofence and sends "went live" emails
- **Pi Connect:** `sudo apt install -y rpi-connect && rpi-connect signin` — outbound-only
  cloud relay, the escape hatch when LAN routing breaks. Worth doing on every vessel Pi
- **Cloudflare tunnel / Zero Trust SSH:** see `docs/SSH-SETUP.md`; token goes in
  Settings → Remote Access

---

## Version note

`docker compose pull` fetches `:latest`, which is currently **1.7.0**. Version 1.7.1
(camera-probe retry — stops a transient probe failure silently dropping the stream from
copy into CPU-heavy transcode on restart) is committed locally but **not pushed**, so
it isn't on GHCR. To put a specific build on this Pi once it is:

```bash
cd ~/rpie-streamer && bash scripts/manual-update.sh 1.7.1
```

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| SSH + web admin dead, but cloud shows the Pi online and streaming | eth0 and eth1 in the same /24 — return traffic goes out the camera interface | `sudo ./scripts/setup-network.sh eth1 192.168.5.10/24`, power-cycle the camera, re-scan for it in the web UI |
| Camera never gets a lease | dnsmasq conf not rewritten, or a stray `.bak` in `/etc/dnsmasq.d/` breaking the service | `cat /etc/dnsmasq.d/camera-dhcp.conf`, `journalctl -u dnsmasq`; delete any `*.bak` in that directory |
| Script "succeeds" but changes nothing | Pi-local `scripts/` drifted from the repo (bind-mount deploys skip it) | `git pull` in `~/rpie-streamer`, or rsync `scripts/` from the Mac, then re-run |
| CPU pinned, stream soft | Fell back to transcode after a failed camera probe | Fixed in 1.7.1; until then `docker restart rpie-streamer` while the camera is up |
| Stream runs but nothing on the CDN | Bunny region blank, or wrong storage key | Settings → region `la`; check `docker logs rpie-streamer` for 401s |
| Another vessel's stream flickers/breaks | Two Pis sharing a Bunny stream path | Give each vessel a unique path in Settings |
| DVR timestamps wrong | Host clock off (Patriot runs ~4h ahead) | Fix the host clock/timezone — container restarts don't help |
| Heartbeat 401 | Token typo, or vessel deleted cloud-side | Regenerate on the vessel admin page, re-paste |
