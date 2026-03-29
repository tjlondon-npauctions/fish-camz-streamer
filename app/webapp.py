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

    app.secret_key = manager.get(config, "web", "secret_key", "change-me-on-first-run")

    # Register blueprints
    from app.web.routes import routes
    from app.web.api import api

    app.register_blueprint(routes)
    app.register_blueprint(api)

    return app


def main() -> None:
    config = manager.load()

    log_level = manager.get(config, "system", "log_level", "INFO")
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    app = create_app()

    host = manager.get(config, "web", "host", "0.0.0.0")
    port = manager.get(config, "web", "port", 8080)

    logging.getLogger(__name__).info("Starting web UI on %s:%d", host, port)

    # Use waitress for production, Flask dev server as fallback
    try:
        from waitress import serve
        serve(app, host=host, port=port)
    except ImportError:
        app.run(host=host, port=port, debug=False)


if __name__ == "__main__":
    main()
