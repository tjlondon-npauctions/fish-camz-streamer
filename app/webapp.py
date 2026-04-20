"""Flask web UI entry point."""

from __future__ import annotations

import logging

from flask import Flask

from app.config import manager


def create_app() -> Flask:
    config = manager.load()

    app = Flask(
        __name__,
        template_folder="web/templates",
        static_folder="../static",
    )

    secret_key = manager.get(config, "web", "secret_key", "")
    if not secret_key:
        raise RuntimeError("web.secret_key is not set in config. Run setup or reload config.")
    app.secret_key = secret_key

    # Register blueprints
    from app.web.routes import routes
    from app.web.api import api

    app.register_blueprint(routes)
    app.register_blueprint(api)

    return app


def main() -> None:
    import traceback

    try:
        config = manager.load()

        log_level = manager.get(config, "system", "log_level", "INFO")
        logging.basicConfig(
            level=getattr(logging, log_level, logging.INFO),
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        app = create_app()

        # Start GPS reader (if enabled)
        from app.gps.reader import GpsReader
        gps_cfg = config.get("gps", {})
        if gps_cfg.get("enabled", False):
            gps_reader = GpsReader(
                gpsd_host=gps_cfg.get("gpsd_host", "localhost"),
                gpsd_port=gps_cfg.get("gpsd_port", 2947),
                poll_interval=gps_cfg.get("poll_interval", 5),
                state_dir=manager.get(config, "system", "state_dir", "/run/rpie"),
            )
            gps_reader.start()

        # Start heartbeat to Fishcamz backend (if configured)
        from app.heartbeat import HeartbeatSender
        heartbeat = HeartbeatSender(config)
        heartbeat.start()

        # Auto-start Cloudflare Tunnel on boot if configured and not already running.
        # Closes a gap where the tunnel was only spawned from the settings form handler,
        # so a manually-removed container would never come back without re-saving settings.
        if manager.get(config, "remote_access", "enabled", False):
            tunnel_token = manager.get(config, "remote_access", "tunnel_token", "")
            if tunnel_token:
                try:
                    import docker
                    client = docker.from_env()
                    try:
                        existing = client.containers.get("rpie-tunnel")
                        if existing.status != "running":
                            raise docker.errors.NotFound("not running")
                    except docker.errors.NotFound:
                        from app.web.routes import _start_tunnel
                        _start_tunnel(tunnel_token)
                except Exception as e:
                    logging.getLogger(__name__).warning("Tunnel autostart skipped: %s", e)

        host = manager.get(config, "web", "host", "0.0.0.0")
        port = manager.get(config, "web", "port", 8080)

        logging.getLogger(__name__).info("Starting web UI on %s:%d", host, port)

        # Use waitress for production, Flask dev server as fallback
        try:
            from waitress import serve
            serve(app, host=host, port=port)
        except ImportError:
            app.run(host=host, port=port, debug=False)
    except Exception:
        traceback.print_exc()
        raise


if __name__ == "__main__":
    main()
