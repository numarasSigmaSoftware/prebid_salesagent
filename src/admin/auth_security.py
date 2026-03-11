"""Shared security helpers for admin authentication flows."""

from __future__ import annotations

import os
from urllib.parse import unquote, urlsplit


DEFAULT_TEST_PASSWORD = "test123"


def is_production_environment() -> bool:
    """Return True when running with production semantics."""
    return os.environ.get("PRODUCTION", "").lower() == "true" or os.environ.get("ENVIRONMENT", "").lower() == "production"


def is_test_auth_enabled(tenant_setup_mode: bool = False) -> bool:
    """Return whether test auth should be available for the current request."""
    if is_production_environment():
        return False
    return os.environ.get("ADCP_AUTH_TEST_MODE", "").lower() == "true" or tenant_setup_mode


def get_test_users() -> dict[str, dict[str, str]]:
    """Return configured non-default test users.

    Passwords must be supplied explicitly and may not use the historical default.
    """
    users = {}
    candidates = (
        (
            os.environ.get("TEST_SUPER_ADMIN_EMAIL", "test_super_admin@example.com").lower(),
            os.environ.get("TEST_SUPER_ADMIN_PASSWORD"),
            "Test Super Admin",
            "super_admin",
        ),
        (
            os.environ.get("TEST_TENANT_ADMIN_EMAIL", "test_tenant_admin@example.com").lower(),
            os.environ.get("TEST_TENANT_ADMIN_PASSWORD"),
            "Test Tenant Admin",
            "tenant_admin",
        ),
        (
            os.environ.get("TEST_TENANT_USER_EMAIL", "test_tenant_user@example.com").lower(),
            os.environ.get("TEST_TENANT_USER_PASSWORD"),
            "Test Tenant User",
            "tenant_user",
        ),
    )

    for email, password, name, role in candidates:
        if not password or password == DEFAULT_TEST_PASSWORD:
            continue
        users[email] = {"password": password, "name": name, "role": role}

    return users


def sanitize_next_url(target: str | None) -> str | None:
    """Return a safe internal redirect target or None."""
    if not target:
        return None

    normalized = target.strip()
    for _ in range(2):
        decoded = unquote(normalized)
        if decoded == normalized:
            break
        normalized = decoded.strip()

    if not normalized or any(ch in normalized for ch in ("\r", "\n")):
        return None

    parts = urlsplit(normalized)
    if parts.scheme or parts.netloc:
        return None

    if normalized.startswith("//") or not normalized.startswith("/"):
        return None

    return normalized


def build_safe_next_path(request) -> str:
    """Build a relative redirect target from the current Flask request."""
    query_string = request.query_string.decode("utf-8") if request.query_string else ""
    return f"{request.path}?{query_string}" if query_string else request.path


def store_login_next_url(flask_session, target: str | None) -> None:
    """Store a safe login redirect target if valid; otherwise clear it."""
    safe_target = sanitize_next_url(target)
    if safe_target:
        flask_session["login_next_url"] = safe_target
    else:
        flask_session.pop("login_next_url", None)


def pop_safe_login_next_url(flask_session) -> str | None:
    """Pop and validate the stored login redirect target."""
    return sanitize_next_url(flask_session.pop("login_next_url", None))


def get_safe_login_next_url(flask_session) -> str | None:
    """Read the stored login redirect target without trusting it blindly."""
    return sanitize_next_url(flask_session.get("login_next_url"))
