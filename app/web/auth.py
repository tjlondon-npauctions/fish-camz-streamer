"""Authentication middleware for the web UI."""

from __future__ import annotations

import functools

import bcrypt
from flask import redirect, request, session, url_for

from app.config import manager


def require_auth(f):
    """Decorator: redirect to login if not authenticated, or to setup if not configured."""
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        config = manager.load()

        # First-run: no password set yet
        if not manager.is_setup_complete(config):
            if request.endpoint != "routes.setup":
                return redirect(url_for("routes.setup"))
            return f(*args, **kwargs)

        # Check session
        if not session.get("authenticated"):
            if request.endpoint not in ("routes.login", "routes.setup"):
                return redirect(url_for("routes.login"))

        return f(*args, **kwargs)
    return decorated


def hash_password(password: str) -> str:
    """Hash a password with bcrypt."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def check_password(password: str, hashed: str) -> bool:
    """Verify a password against its bcrypt hash."""
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False
