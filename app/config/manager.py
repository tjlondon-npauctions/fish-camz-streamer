import os
import secrets
import shutil
from pathlib import Path

import yaml

BASE_DIR = Path(__file__).resolve().parent.parent.parent
DEFAULT_CONFIG = BASE_DIR / "config" / "default_config.yaml"
DATA_DIR = BASE_DIR / "data"
CONFIG_FILE = DATA_DIR / "config.yaml"

REQUIRED_FOR_STREAMING = [
    ("camera", "rtsp_url"),
    ("cloudflare", "stream_key"),
]


def _deep_merge(base: dict, override: dict) -> dict:
    """Merge override into base, adding missing keys from base without overwriting."""
    merged = base.copy()
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load() -> dict:
    """Load config from data/config.yaml, creating from defaults if needed."""
    if not CONFIG_FILE.exists():
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(DEFAULT_CONFIG, CONFIG_FILE)

    with open(CONFIG_FILE, "r") as f:
        config = yaml.safe_load(f) or {}

    # Merge any new default keys that don't exist in user config
    with open(DEFAULT_CONFIG, "r") as f:
        defaults = yaml.safe_load(f) or {}

    config = _deep_merge(defaults, config)

    # Auto-generate secret key if missing
    if not config.get("web", {}).get("secret_key"):
        config.setdefault("web", {})["secret_key"] = secrets.token_hex(32)
        save(config)

    return config


def save(config: dict) -> None:
    """Save config to data/config.yaml."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)
    # Restrict permissions to owner-only (protect stream key, password hash)
    os.chmod(CONFIG_FILE, 0o600)


def get(config: dict, section: str, key: str, default=None):
    """Get a nested config value."""
    return config.get(section, {}).get(key, default)


def set_value(config: dict, section: str, key: str, value) -> dict:
    """Set a nested config value and return the updated config."""
    config.setdefault(section, {})[key] = value
    return config


def validate(config: dict) -> list[str]:
    """Validate config, returning a list of error messages (empty = valid)."""
    errors = []

    rtsp_url = get(config, "camera", "rtsp_url", "")
    if rtsp_url and not rtsp_url.startswith("rtsp://"):
        errors.append("Camera RTSP URL must start with rtsp://")

    stream_key = get(config, "cloudflare", "stream_key", "")
    if stream_key and " " in stream_key:
        errors.append("Cloudflare stream key must not contain spaces")

    mode = get(config, "encoding", "mode", "auto")
    if mode not in ("auto", "copy", "transcode"):
        errors.append(f"Encoding mode must be auto, copy, or transcode (got '{mode}')")

    bitrate = get(config, "encoding", "video_bitrate", "2500k")
    if not bitrate.endswith("k") and not bitrate.endswith("M"):
        errors.append("Video bitrate must end with 'k' or 'M' (e.g. '2500k')")

    port = get(config, "web", "port", 8080)
    if not isinstance(port, int) or port < 1 or port > 65535:
        errors.append("Web port must be a number between 1 and 65535")

    return errors


def is_setup_complete(config: dict) -> bool:
    """Check if the first-run setup has been completed (password set)."""
    return bool(get(config, "web", "password_hash"))


def is_streaming_ready(config: dict) -> bool:
    """Check if all required fields for streaming are configured."""
    for section, key in REQUIRED_FOR_STREAMING:
        if not get(config, section, key):
            return False
    return True
