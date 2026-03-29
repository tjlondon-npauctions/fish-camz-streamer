# RPie-Streamer

Unattended live video streaming appliance. POE IP camera → Raspberry Pi 5 → FFmpeg → Cloudflare RTMPS.

## Quick Reference

```bash
# Run tests
python3 -m pytest tests/ -v

# Start locally (without Docker)
python3 -m app.webapp   # Web UI on :8080
python3 -m app.main     # Stream engine

# Docker
docker compose up -d --build    # Build and start
docker compose logs -f          # View logs
docker compose restart          # Restart both services
docker compose down             # Stop everything
```

## Architecture

Two Docker containers sharing a tmpfs volume for state:

- **rpie-web** (`app/webapp.py`) — Flask web UI on port 8080. Controls the streamer via the Docker socket.
- **rpie-streamer** (`app/main.py`) — FFmpeg subprocess manager. Reads camera RTSP, outputs to Cloudflare RTMPS.

Communication: Web writes config to `data/config.yaml` (bind mount), streamer writes state to `/run/rpie/state.json` (tmpfs volume), web reads state via API.

## Project Structure

```
app/
  config/manager.py        — YAML config load/save/validate
  camera/discovery.py      — ONVIF WS-Discovery (UDP multicast)
  camera/probe.py          — ffprobe wrapper → StreamInfo dataclass
  streaming/ffmpeg_builder.py — Pure function: config + probe → FFmpeg args
  streaming/engine.py      — FFmpeg subprocess lifecycle + auto-restart
  streaming/health.py      — Parses FFmpeg stderr for metrics
  network/monitor.py       — Ping-based connectivity checker
  system/stats.py          — CPU/mem/temp/disk via psutil
  web/routes.py            — Flask page routes (dashboard, settings, logs, setup, login)
  web/api.py               — JSON API endpoints (/api/status, /api/stream/*, etc.)
  web/auth.py              — Session auth + bcrypt password hashing
  webapp.py                — Flask app factory + waitress server
  main.py                  — Stream engine entry point + signal handling
config/default_config.yaml — Default config (copied to data/ on first run)
```

## Key Conventions

- **Python 3.9+ compatible** — Use `from __future__ import annotations` and `Optional[X]` instead of `X | None`
- **Pure functions where possible** — `ffmpeg_builder.py` has no side effects, takes config dict + probe, returns args list
- **Config is a plain dict** — No ORM, no Pydantic. Access via `manager.get(config, "section", "key", default)`
- **Volatile state goes to tmpfs** (`/run/rpie/`), persistent config to `data/config.yaml`
- **Templates use Pico CSS** from CDN + vanilla JS with AJAX polling. No frontend build step.
- **XSS prevention** — Never use innerHTML with untrusted data. Use textContent or DOM element creation.
- **FFmpeg commands are built as lists** — Never shell strings. Pass to `subprocess.Popen(args_list)`.

## Testing

Tests cover the three core pure-logic modules:
- `tests/test_config_manager.py` — Config load/save/validate/merge
- `tests/test_ffmpeg_builder.py` — Every codec path (auto, copy, transcode) and edge case
- `tests/test_health.py` — FFmpeg stderr parsing, stall detection, speed warnings

Run with: `python3 -m pytest tests/ -v`

## Config Schema

See `config/default_config.yaml` for all keys. Critical ones:
- `camera.rtsp_url` — Camera RTSP URL (required for streaming)
- `cloudflare.stream_key` — Cloudflare Stream Live key (required for streaming)
- `encoding.mode` — `auto` (copy if H.264, else transcode), `copy`, or `transcode`
- `stream.auto_start` — Start streaming on boot if config is complete
- `web.password_hash` — bcrypt hash, empty = setup wizard shown

## Known Limitations / Future Work

- No CSRF protection on forms (add Flask-WTF if exposing beyond LAN)
- No local recording buffer during internet outages (reconnect-only)
- Remote access not yet implemented (planned: Cloudflare Tunnel)
- Docker socket mount for container control (acceptable on LAN, review if exposing remotely)
