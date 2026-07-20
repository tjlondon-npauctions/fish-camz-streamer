"""Start/stop/restart the streamer Docker container from the web container.

Both the Flask API (``app/web/api.py``) and the heartbeat sender
(``app/heartbeat.py``) use this. The ``rpie-web`` container holds the Docker
socket, so these calls run in-process there. Prefers the Docker SDK and falls
back to the ``docker`` CLI if the SDK isn't importable.
"""

from __future__ import annotations

import logging
import subprocess

logger = logging.getLogger(__name__)

STREAMER_CONTAINER = "rpie-streamer"
VALID_ACTIONS = ("start", "stop", "restart")


def set_stream(action: str) -> dict:
    """Perform ``action`` on the streamer container.

    Returns ``{"ok": True, "action": action}`` on success or
    ``{"ok": False, "error": "..."}`` on failure. Never raises — callers
    (notably the heartbeat loop) must not be broken by a Docker hiccup.
    """
    if action not in VALID_ACTIONS:
        return {"ok": False, "error": f"Unknown action: {action}"}

    try:
        import docker

        client = docker.from_env()
        container = client.containers.get(STREAMER_CONTAINER)

        if action == "start":
            container.start()
        elif action == "stop":
            container.stop(timeout=15)
        elif action == "restart":
            container.restart(timeout=15)

        return {"ok": True, "action": action}

    except ImportError:
        # Docker SDK not available — fall back to the CLI.
        try:
            subprocess.run(
                ["docker", action, STREAMER_CONTAINER],
                check=True,
                capture_output=True,
                timeout=30,
            )
            return {"ok": True, "action": action}
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            logger.error("Stream control subprocess error: %s", e)
            return {"ok": False, "error": str(e)}

    except Exception as e:  # noqa: BLE001 — surface any Docker error as a result
        logger.error("Stream control error: %s", e)
        return {"ok": False, "error": str(e)}
