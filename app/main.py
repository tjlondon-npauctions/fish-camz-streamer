"""Stream engine entry point.

Loads config, starts FFmpeg with auto-restart, and runs the network monitor.
Handles SIGTERM for graceful shutdown (Docker sends this on 'docker stop').
"""

from __future__ import annotations

import logging
import signal
import sys
import threading

from app.config import manager
from app.network.monitor import NetworkMonitor
from app.streaming.engine import StreamEngine
from app.streaming.uploader import HLSUploader

logger = logging.getLogger(__name__)


def main() -> None:
    config = manager.load()

    # Configure logging
    log_level = manager.get(config, "system", "log_level", "INFO")
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    output_mode = manager.get(config, "output", "mode", "rtmp")
    logger.info("RPie-Streamer engine starting... (output_mode=%s)", output_mode)

    # Single shutdown event used throughout
    shutdown_event = threading.Event()
    engine = None
    net_monitor = None
    uploader = None

    def shutdown(signum, frame):
        logger.info("Received signal %d, shutting down...", signum)
        shutdown_event.set()
        if engine:
            engine.stop()
        if uploader:
            uploader.stop()
        if net_monitor:
            net_monitor.stop()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    if not manager.is_streaming_ready(config):
        logger.info("Streaming not configured yet. Waiting for setup via web UI.")
        logger.info("Open http://<pi-ip>:8080 to complete setup.")
        shutdown_event.wait()
        return

    # Create components
    engine = StreamEngine(config)
    state_dir = manager.get(config, "system", "state_dir", "/run/rpie")

    net_monitor = NetworkMonitor(
        check_host=manager.get(config, "network", "check_host", "1.1.1.1"),
        check_interval=manager.get(config, "network", "check_interval", 30),
        outage_threshold=manager.get(config, "network", "outage_threshold", 60),
        state_dir=state_dir,
    )

    # When network recovers from extended outage, restart the stream
    net_monitor.on_recovery(lambda: engine.restart())

    # Start HLS uploader if in HLS mode
    output_mode = manager.get(config, "output", "mode", "rtmp")
    if output_mode == "hls":
        bunny_zone = manager.get(config, "bunny", "storage_zone", "")
        bunny_key = manager.get(config, "bunny", "api_key", "")
        if bunny_zone and bunny_key:
            uploader = HLSUploader(
                segment_dir=manager.get(config, "hls", "segment_dir", "/run/rpie/hls"),
                storage_zone=bunny_zone,
                api_key=bunny_key,
                region=manager.get(config, "bunny", "region", ""),
                stream_path=manager.get(config, "bunny", "stream_path", "live"),
                state_dir=state_dir,
            )
            uploader.start()
        else:
            logger.warning("HLS mode enabled but Bunny CDN not configured")

    # Start monitors
    net_monitor.start()

    # Check auto_start
    if manager.get(config, "stream", "auto_start", True):
        logger.info("Auto-start enabled, beginning stream...")
        engine.run_with_auto_restart()  # Blocks until stop
    else:
        logger.info("Auto-start disabled, waiting for manual start via web UI.")
        shutdown_event.wait()


if __name__ == "__main__":
    main()
