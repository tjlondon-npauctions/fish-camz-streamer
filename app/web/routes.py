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
    # Allow static files, API, and player without this check
    if request.endpoint and (
        request.endpoint.startswith("api.") or
        request.endpoint == "static" or
        request.endpoint == "routes.player"
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
        # Update vessel
        manager.set_value(config, "vessel", "name", request.form.get("vessel_name", "").strip())

        # Update camera settings
        manager.set_value(config, "camera", "rtsp_url", request.form.get("rtsp_url", "").strip())
        manager.set_value(config, "camera", "username", request.form.get("cam_username", "").strip())
        cam_password = request.form.get("cam_password", "")
        if cam_password:  # Only overwrite if a new value was entered
            manager.set_value(config, "camera", "password", cam_password)
        manager.set_value(config, "camera", "transport", request.form.get("transport", "tcp"))

        # Update output mode
        manager.set_value(config, "output", "mode", request.form.get("output_mode", "rtmp"))

        # Update Cloudflare settings
        stream_key = request.form.get("stream_key", "").strip()
        if stream_key:  # Only overwrite if a new value was entered
            manager.set_value(config, "cloudflare", "stream_key", stream_key)
        rtmps_url = request.form.get("rtmps_url", "").strip()
        if rtmps_url:
            manager.set_value(config, "cloudflare", "rtmps_url", rtmps_url)

        # Update Bunny CDN settings
        bunny_zone = request.form.get("bunny_storage_zone", "").strip()
        if bunny_zone:
            manager.set_value(config, "bunny", "storage_zone", bunny_zone)
        bunny_key = request.form.get("bunny_api_key", "").strip()
        if bunny_key:
            manager.set_value(config, "bunny", "api_key", bunny_key)
        manager.set_value(config, "bunny", "region", request.form.get("bunny_region", "").strip())
        bunny_cdn = request.form.get("bunny_cdn_url", "").strip()
        if bunny_cdn:
            manager.set_value(config, "bunny", "cdn_url", bunny_cdn)
        bunny_path = request.form.get("bunny_stream_path", "").strip()
        if bunny_path:
            manager.set_value(config, "bunny", "stream_path", bunny_path)

        # Update encoding settings
        manager.set_value(config, "encoding", "mode", request.form.get("encoding_mode", "auto"))
        manager.set_value(config, "encoding", "video_bitrate", request.form.get("video_bitrate", "2500k").strip())
        manager.set_value(config, "encoding", "preset", request.form.get("preset", "veryfast"))

        resolution = request.form.get("resolution", "source").strip()
        manager.set_value(config, "encoding", "resolution", resolution)

        # Update stream settings
        manager.set_value(config, "stream", "auto_start", "auto_start" in request.form)

        # Update remote access
        manager.set_value(config, "remote_access", "enabled", "remote_enabled" in request.form)
        tunnel_token = request.form.get("tunnel_token", "").strip()
        if tunnel_token:
            manager.set_value(config, "remote_access", "tunnel_token", tunnel_token)

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

        # Manage Cloudflare Tunnel
        remote_enabled = manager.get(config, "remote_access", "enabled", False)
        remote_token = manager.get(config, "remote_access", "tunnel_token", "")
        if remote_enabled and remote_token:
            _start_tunnel(remote_token)
            flash("Remote access tunnel starting...", "info")
        elif not remote_enabled:
            _stop_tunnel()

        return redirect(url_for("routes.settings"))

    return render_template("settings.html", config=config)


@routes.route("/logs")
def logs():
    config = manager.load()
    return render_template("logs.html", config=config)


@routes.route("/help")
def help_page():
    config = manager.load()
    return render_template("help.html", config=config)


@routes.route("/player")
def player():
    config = manager.load()
    vessel_name = manager.get(config, "vessel", "name", "")
    output_mode = manager.get(config, "output", "mode", "rtmp")
    playlist_url = ""
    if output_mode == "hls":
        cdn_url = manager.get(config, "bunny", "cdn_url", "").rstrip("/")
        stream_path = manager.get(config, "bunny", "stream_path", "live")
        if cdn_url:
            playlist_url = f"{cdn_url}/{stream_path}/live.m3u8"
    return render_template("player.html", vessel_name=vessel_name, playlist_url=playlist_url)


@routes.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        config = manager.load()
        password = request.form.get("password", "")
        stored_hash = manager.get(config, "web", "password_hash", "")

        if check_password(password, stored_hash):
            session.clear()  # Prevent session fixation
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

            manager.set_value(config, "vessel", "name", request.form.get("vessel_name", "").strip())
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
            # Output setup
            output_mode = request.form.get("output_mode", "rtmp")
            manager.set_value(config, "output", "mode", output_mode)

            if output_mode == "hls":
                manager.set_value(config, "bunny", "storage_zone", request.form.get("bunny_storage_zone", "").strip())
                bunny_key = request.form.get("bunny_api_key", "").strip()
                if bunny_key:
                    manager.set_value(config, "bunny", "api_key", bunny_key)
                bunny_cdn = request.form.get("bunny_cdn_url", "").strip()
                if bunny_cdn:
                    manager.set_value(config, "bunny", "cdn_url", bunny_cdn)
            else:
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


def _start_tunnel(token: str) -> None:
    """Start the Cloudflare Tunnel container via Docker SDK."""
    try:
        import docker
        client = docker.from_env()

        # Remove existing tunnel container if present
        try:
            old = client.containers.get("rpie-tunnel")
            old.stop(timeout=10)
            old.remove()
        except docker.errors.NotFound:
            pass

        # Start new tunnel container
        client.containers.run(
            "cloudflare/cloudflared:latest",
            command=["tunnel", "run"],
            name="rpie-tunnel",
            environment={"TUNNEL_TOKEN": token},
            network_mode="host",
            restart_policy={"Name": "unless-stopped"},
            detach=True,
        )
        logger.info("Cloudflare Tunnel container started")
    except Exception as e:
        logger.warning("Could not start tunnel: %s", e)


def _stop_tunnel() -> None:
    """Stop the Cloudflare Tunnel container."""
    try:
        import docker
        client = docker.from_env()
        container = client.containers.get("rpie-tunnel")
        container.stop(timeout=10)
        container.remove()
        logger.info("Cloudflare Tunnel container stopped")
    except Exception as e:
        logger.debug("Tunnel stop: %s", e)
