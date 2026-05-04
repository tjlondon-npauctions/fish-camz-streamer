"""Controlled OTA image updater.

Replaces Watchtower for fleet-managed devices. The cloud sets a target version
on a per-device basis; the Pi pulls the matching image from GHCR and recreates
its containers using the existing config (Watchtower-style — see
https://github.com/containrrr/watchtower for the inspiration).

State is persisted to /run/rpie/update_state.json (tmpfs) so a mid-update
container restart can report progress to the cloud once the new container
boots up. State file shape:

    {
      "status": "idle" | "downloading" | "restarting" | "failed",
      "target": "1.6.0",
      "error": "...",
      "started_at": 1714680000.0
    }
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

STATE_DIR = Path("/run/rpie")
STATE_FILE = STATE_DIR / "update_state.json"

# Containers we recreate. Order matters: streamer first (we are not it), then
# web (recreating us terminates this process; the daemon brings it back up).
MANAGED_CONTAINERS = ("rpie-streamer", "rpie-web")

# Failure cool-down: don't retry the same failed target for 5 minutes.
FAIL_RETRY_BACKOFF_SECONDS = 300

# Stuck-detection: if downloading for >10 min, assume something hung and fail.
STUCK_TIMEOUT_SECONDS = 600


def _load_state() -> dict:
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text())
    except Exception as e:
        logger.warning("update_state.json read failed: %s", e)
    return {"status": "idle", "target": "", "error": "", "started_at": 0.0}


def _save_state(state: dict) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state))
    except Exception as e:
        logger.warning("update_state.json write failed: %s", e)


def get_status() -> dict:
    """Return current update state — used by heartbeat to report status to cloud."""
    return _load_state()


class Updater:
    """Pulls Docker images from GHCR when target_version != current_version."""

    def __init__(self, current_version: str):
        self.current_version = current_version
        self._lock = threading.Lock()

    def maybe_update(
        self,
        target_version: Optional[str],
        registry: Optional[dict],
    ) -> dict:
        """Called from heartbeat after each successful response. Idempotent.

        registry: {"registry": "ghcr.io", "username": "...", "token": "PAT...",
                   "image_repo": "ghcr.io/owner/repo"}
        """
        # Use a non-blocking lock so a slow update doesn't queue up multiple
        # heartbeat-driven invocations.
        if not self._lock.acquire(blocking=False):
            return _load_state()

        try:
            return self._maybe_update_locked(target_version, registry)
        finally:
            self._lock.release()

    def _maybe_update_locked(
        self,
        target_version: Optional[str],
        registry: Optional[dict],
    ) -> dict:
        state = _load_state()

        # No target, or already on it → idle (and reset transient states).
        if not target_version or target_version == self.current_version:
            if state.get("status") in ("downloading", "restarting", "failed"):
                # We just came back up on the right version, or the cloud cleared
                # the target — clear stale state.
                state = {
                    "status": "idle",
                    "target": target_version or "",
                    "error": "",
                    "started_at": 0.0,
                }
                _save_state(state)
            return state

        status = state.get("status", "idle")
        started_at = float(state.get("started_at") or 0)

        # In-progress: don't restart, but check for stuck.
        if status in ("downloading", "restarting"):
            if started_at and time.time() - started_at > STUCK_TIMEOUT_SECONDS:
                state = {
                    "status": "failed",
                    "target": target_version,
                    "error": f"stuck in '{status}' for >{STUCK_TIMEOUT_SECONDS}s",
                    "started_at": time.time(),
                }
                _save_state(state)
            return state

        # Recently failed for the same target? Back off.
        if (
            status == "failed"
            and state.get("target") == target_version
            and started_at
            and time.time() - started_at < FAIL_RETRY_BACKOFF_SECONDS
        ):
            return state

        # Begin update.
        state = {
            "status": "downloading",
            "target": target_version,
            "error": "",
            "started_at": time.time(),
        }
        _save_state(state)
        logger.info("Updater: pulling %s", target_version)

        try:
            self._pull_and_tag(target_version, registry)
            state = {**state, "status": "restarting"}
            _save_state(state)
            logger.info("Updater: recreating containers onto %s", target_version)
            self._recreate_managed_containers()
            # If we reach here we didn't kill ourselves — log so we can investigate.
            logger.warning("Updater: recreate completed without exiting — unexpected")
        except Exception as e:
            logger.exception("Updater: failed for %s", target_version)
            state = {
                "status": "failed",
                "target": target_version,
                "error": str(e)[:300],
                "started_at": time.time(),
            }
            _save_state(state)

        return state

    # ──── internals ────

    def _pull_and_tag(self, target_version: str, registry: Optional[dict]) -> None:
        if not registry or not registry.get("token"):
            raise RuntimeError("registry credentials missing in heartbeat response")

        import docker  # imported lazily so the rest of the app boots without docker SDK

        client = docker.from_env()

        image_repo = registry.get("image_repo") or ""
        if not image_repo:
            raise RuntimeError("registry.image_repo missing")

        auth_config = {
            "username": registry.get("username", ""),
            "password": registry["token"],
        }

        # Pull the target tag.
        image = client.images.pull(image_repo, tag=target_version, auth_config=auth_config)
        # Re-tag as :latest so existing compose definitions (which pin :latest) pick it up.
        try:
            image.tag(image_repo, "latest")
        except Exception as e:
            # Re-tag failure isn't fatal — the recreate path can pin to the explicit tag instead.
            logger.warning("Updater: re-tag :latest failed: %s", e)
        logger.info("Updater: pulled %s:%s", image_repo, target_version)

    def _recreate_managed_containers(self) -> None:
        """Stop, remove, and recreate each managed container with the new :latest image."""
        import docker
        from docker.types import Mount

        client = docker.from_env()

        for name in MANAGED_CONTAINERS:
            try:
                container = client.containers.get(name)
            except docker.errors.NotFound:
                logger.warning("Updater: %s not found, skipping", name)
                continue

            attrs = container.attrs
            cfg = attrs.get("Config") or {}
            host_cfg = attrs.get("HostConfig") or {}

            # Image: pin to :latest so we get the just-pulled image.
            image_ref = cfg.get("Image", "")
            if image_ref and ":" in image_ref:
                base, _ = image_ref.rsplit(":", 1)
                new_image = f"{base}:latest"
            else:
                new_image = image_ref or "ghcr.io/tjlondon-npauctions/fish-camz-streamer:latest"

            # Translate Mounts → docker-py Mount objects (named volumes etc.)
            mounts: list[Mount] = []
            for m in host_cfg.get("Mounts") or []:
                if m.get("Type") == "volume":
                    mounts.append(
                        Mount(
                            target=m.get("Target", ""),
                            source=m.get("Source", ""),
                            type="volume",
                            read_only=bool(m.get("ReadOnly", False)),
                        )
                    )
                elif m.get("Type") == "bind":
                    # Binds also surface here in some docker versions — keep them.
                    mounts.append(
                        Mount(
                            target=m.get("Target", ""),
                            source=m.get("Source", ""),
                            type="bind",
                            read_only=bool(m.get("ReadOnly", False)),
                        )
                    )

            run_kwargs: dict = {
                "image": new_image,
                "name": name,
                "command": cfg.get("Cmd"),
                "entrypoint": cfg.get("Entrypoint"),
                "environment": cfg.get("Env") or [],
                "labels": cfg.get("Labels") or {},
                "network_mode": host_cfg.get("NetworkMode") or "default",
                "restart_policy": host_cfg.get("RestartPolicy") or {"Name": "unless-stopped"},
                "volumes": host_cfg.get("Binds") or [],
                "detach": True,
            }
            if mounts:
                run_kwargs["mounts"] = mounts

            hc = cfg.get("Healthcheck")
            if hc and hc.get("Test"):
                run_kwargs["healthcheck"] = {
                    "test": hc.get("Test"),
                    "interval": hc.get("Interval", 0),
                    "timeout": hc.get("Timeout", 0),
                    "retries": hc.get("Retries", 0),
                    "start_period": hc.get("StartPeriod", 0),
                }

            logger.info("Updater: recreating %s (image=%s)", name, new_image)
            container.stop(timeout=15)
            container.remove()
            client.containers.run(**run_kwargs)
            # Recreating rpie-web (the container running this process) terminates us.
            # Anything after this line for that case won't run — that's expected.
