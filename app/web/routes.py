"""Flask page routes: dashboard, settings, logs, setup wizard, login."""

from __future__ import annotations

import logging

from flask import Blueprint, flash, redirect, render_template, request, session, url_for

from app.config import manager
from app.web.auth import check_password, hash_password, require_auth

logger = logging.getLogger(__name__)

routes = Blueprint("routes", __name__)


@routes.before_request
def check_auth():
    """Check authentication before every page request."""
    # Allow static files and API without this check
    if request.endpoint and (
        request.endpoint.startswith("api.") or
        request.endpoint == "static"
    ):
        return

    config = manager.load()

    # First-run: redirect to setup
    if not manager.is_setup_complete(config):
        if request.endpoint not in ("routes.setup",):
            return redirect(url_for("routes.setup"))
        return

    # Not logged in: redirect to login
    if not session.get("authenticated"):
        if request.endpoint not in ("routes.login",):
            return redirect(url_for("routes.login"))


@routes.route("/")
def dashboard():
    config = manager.load()
    return render_template("dashboard.html", config=config)


@routes.route("/settings", methods=["GET", "POST"])
def settings():
    config = manager.load()

    if request.method == "POST":
        # Update camera settings
        manager.set_value(config, "camera", "rtsp_url", request.form.get("rtsp_url", "").strip())
        manager.set_value(config, "camera", "username", request.form.get("cam_username", "").strip())
        manager.set_value(config, "camera", "password", request.form.get("cam_password", ""))
        manager.set_value(config, "camera", "transport", request.form.get("transport", "tcp"))

        # Update Cloudflare settings
        manager.set_value(config, "cloudflare", "stream_key", request.form.get("stream_key", "").strip())
        rtmps_url = request.form.get("rtmps_url", "").strip()
        if rtmps_url:
            manager.set_value(config, "cloudflare", "rtmps_url", rtmps_url)

        # Update encoding settings
        manager.set_value(config, "encoding", "mode", request.form.get("encoding_mode", "auto"))
        manager.set_value(config, "encoding", "video_bitrate", request.form.get("video_bitrate", "2500k").strip())
        manager.set_value(config, "encoding", "preset", request.form.get("preset", "veryfast"))

        resolution = request.form.get("resolution", "source").strip()
        manager.set_value(config, "encoding", "resolution", resolution)

        # Update stream settings
        manager.set_value(config, "stream", "auto_start", "auto_start" in request.form)

        # Validate
        errors = manager.validate(config)
        if errors:
            for error in errors:
                flash(error, "error")
            return render_template("settings.html", config=config)

        manager.save(config)
        flash("Settings saved successfully.", "success")

        # Restart stream if requested
        if "restart_stream" in request.form:
            try:
                import docker
                client = docker.from_env()
                container = client.containers.get("rpie-streamer")
                container.restart(timeout=15)
                flash("Stream restarting...", "info")
            except Exception as e:
                flash(f"Could not restart stream: {e}", "error")

        return redirect(url_for("routes.settings"))

    return render_template("settings.html", config=config)


@routes.route("/logs")
def logs():
    return render_template("logs.html")


@routes.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        config = manager.load()
        password = request.form.get("password", "")
        stored_hash = manager.get(config, "web", "password_hash", "")

        if check_password(password, stored_hash):
            session["authenticated"] = True
            return redirect(url_for("routes.dashboard"))
        else:
            flash("Invalid password.", "error")

    return render_template("login.html")


@routes.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("routes.login"))


@routes.route("/setup", methods=["GET", "POST"])
def setup():
    config = manager.load()

    # Don't allow re-running setup if already configured
    if manager.is_setup_complete(config) and session.get("authenticated"):
        return redirect(url_for("routes.dashboard"))

    if request.method == "POST":
        step = request.form.get("step", "1")

        if step == "1":
            # Set password
            password = request.form.get("password", "")
            confirm = request.form.get("confirm_password", "")

            if len(password) < 6:
                flash("Password must be at least 6 characters.", "error")
                return render_template("setup.html", step=1, config=config)

            if password != confirm:
                flash("Passwords do not match.", "error")
                return render_template("setup.html", step=1, config=config)

            manager.set_value(config, "web", "username", request.form.get("username", "admin").strip())
            manager.set_value(config, "web", "password_hash", hash_password(password))
            manager.save(config)
            session["authenticated"] = True
            return render_template("setup.html", step=2, config=config)

        elif step == "2":
            # Camera setup
            rtsp_url = request.form.get("rtsp_url", "").strip()
            manager.set_value(config, "camera", "rtsp_url", rtsp_url)
            manager.set_value(config, "camera", "username", request.form.get("cam_username", "").strip())
            manager.set_value(config, "camera", "password", request.form.get("cam_password", ""))
            manager.save(config)
            return render_template("setup.html", step=3, config=config)

        elif step == "3":
            # Cloudflare setup
            manager.set_value(config, "cloudflare", "stream_key", request.form.get("stream_key", "").strip())
            rtmps_url = request.form.get("rtmps_url", "").strip()
            if rtmps_url:
                manager.set_value(config, "cloudflare", "rtmps_url", rtmps_url)
            manager.save(config)
            return render_template("setup.html", step=4, config=config)

        elif step == "4":
            # Confirm and start
            if manager.is_streaming_ready(config):
                try:
                    import docker
                    client = docker.from_env()
                    container = client.containers.get("rpie-streamer")
                    container.restart(timeout=15)
                except Exception as e:
                    logger.warning("Could not auto-start stream: %s", e)

            flash("Setup complete! Your stream is starting.", "success")
            return redirect(url_for("routes.dashboard"))

    return render_template("setup.html", step=1, config=config)
