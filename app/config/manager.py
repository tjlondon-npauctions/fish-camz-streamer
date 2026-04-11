import logging
import os
import secrets
import shutil
import tempfile
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent.parent
DEFAULT_CONFIG = BASE_DIR / "config" / "default_config.yaml"
DATA_DIR = BASE_DIR / "data"
CONFIG_FILE = DATA_DIR / "config.yaml"
CONFIG_BACKUP = DATA_DIR / "config.yaml.bak"

REQUIRED_FOR_RTMP = [
    ("camera", "rtsp_url"),
    ("cloudflare", "stream_key"),
]

REQUIRED_FOR_HLS = [
    ("camera", "rtsp_url"),
    ("bunny", "storage_zone"),
    ("bunny", "api_key"),
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
    """Load config from data/config.yaml, creating from defaults if needed.

    If the config file is corrupted, attempts recovery from backup.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    if not CONFIG_FILE.exists():
        shutil.copy2(DEFAULT_CONFIG, CONFIG_FILE)

    config = _load_yaml(CONFIG_FILE)

    # If corrupted, try backup
    if config is None:
        logger.warning("Config file corrupted, attempting recovery from backup...")
        if CONFIG_BACKUP.exists():
            config = _load_yaml(CONFIG_BACKUP)
            if config is not None:
                logger.info("Recovered config from backup.")
                save(config)
            else:
                logger.error("Backup also corrupted. Resetting to defaults.")
                config = {}
        else:
            logger.error("No backup available. Resetting to defaults.")
            config = {}

    # Merge any new default keys that don't exist in user config
    defaults = _load_yaml(DEFAULT_CONFIG) or {}
    config = _deep_merge(defaults, config)

    # Auto-generate secret key if missing
    if not config.get("web", {}).get("secret_key"):
        config.setdefault("web", {})["secret_key"] = secrets.token_hex(32)
        save(config)

    return config


def _load_yaml(path: Path) -> dict:
    """Load and validate a YAML file. Returns None if corrupted."""
    try:
        with open(path, "r") as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            return None
        return data
    except (yaml.YAMLError, OSError) as e:
        logger.error("Failed to load %s: %s", path, e)
        return None


def save(config: dict) -> None:
    """Save config atomically with backup.

    Writes to a temp file first, then renames (atomic on POSIX).
    Keeps one backup of the previous config.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Backup current config before overwriting
    if CONFIG_FILE.exists():
        try:
            shutil.copy2(CONFIG_FILE, CONFIG_BACKUP)
        except OSError:
            pass

    # Atomic write: temp file + rename
    try:
        fd, tmp_path = tempfile.mkstemp(dir=DATA_DIR, suffix=".yaml.tmp")
        with os.fdopen(fd, "w") as f:
            yaml.dump(config, f, default_flow_style=False, sort_keys=False)
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, CONFIG_FILE)
    except OSError as e:
        logger.error("Failed to save config: %s", e)
        # Clean up temp file if rename failed
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


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
    output_mode = get(config, "output", "mode", "rtmp")
    required = REQUIRED_FOR_HLS if output_mode == "hls" else REQUIRED_FOR_RTMP
    for section, key in required:
        if not get(config, section, key):
            return False
    return True
