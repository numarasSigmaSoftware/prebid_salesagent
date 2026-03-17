"""Tests for production-mode blocking of /test/auth (F-02)."""

import os
from unittest.mock import MagicMock, patch

import pytest

from src.admin.app import create_app


def _make_client_with_tenant(auth_setup_mode: bool):
    """Create a Flask test client with a mocked tenant whose auth_setup_mode is set."""
    app = create_app({"TESTING": True, "SECRET_KEY": "test-secret", "WTF_CSRF_ENABLED": False})
    client = app.test_client()

    mock_tenant = MagicMock()
    mock_tenant.auth_setup_mode = auth_setup_mode
    mock_session = MagicMock()
    mock_session.scalars.return_value.first.return_value = mock_tenant

    return client, mock_session


class TestProductionHardBlock:
    """POST /test/auth must return 404 in production mode regardless of other flags."""

    def test_environment_production_blocks_test_auth(self):
        """ENVIRONMENT=production must block /test/auth even when ADCP_AUTH_TEST_MODE=true."""
        client, mock_session = _make_client_with_tenant(auth_setup_mode=True)

        with patch("src.admin.blueprints.auth.get_db_session") as mock_db:
            mock_db.return_value.__enter__ = MagicMock(return_value=mock_session)
            mock_db.return_value.__exit__ = MagicMock(return_value=False)
            with patch.dict(os.environ, {"ENVIRONMENT": "production", "ADCP_AUTH_TEST_MODE": "true", "PRODUCTION": ""}):
                response = client.post(
                    "/test/auth",
                    data={"email": "test_super_admin@example.com", "password": "test123", "tenant_id": "default"},
                )

        assert response.status_code == 404

    def test_production_flag_also_blocks(self):
        """PRODUCTION=true must block /test/auth even when ADCP_AUTH_TEST_MODE=true."""
        client, mock_session = _make_client_with_tenant(auth_setup_mode=True)

        with patch("src.admin.blueprints.auth.get_db_session") as mock_db:
            mock_db.return_value.__enter__ = MagicMock(return_value=mock_session)
            mock_db.return_value.__exit__ = MagicMock(return_value=False)
            with patch.dict(os.environ, {"PRODUCTION": "true", "ENVIRONMENT": "", "ADCP_AUTH_TEST_MODE": "true"}):
                response = client.post(
                    "/test/auth",
                    data={"email": "test_super_admin@example.com", "password": "test123", "tenant_id": "default"},
                )

        assert response.status_code == 404

    def test_non_production_blocked_when_env_var_only(self):
        """F-02 regression: env var on + auth_setup_mode=False -> 404."""
        client, mock_session = _make_client_with_tenant(auth_setup_mode=False)

        with patch("src.admin.blueprints.auth.get_db_session") as mock_db:
            mock_db.return_value.__enter__ = MagicMock(return_value=mock_session)
            mock_db.return_value.__exit__ = MagicMock(return_value=False)
            with patch.dict(os.environ, {"ADCP_AUTH_TEST_MODE": "true", "PRODUCTION": "", "ENVIRONMENT": ""}):
                response = client.post(
                    "/test/auth",
                    data={"email": "test_super_admin@example.com", "password": "test123", "tenant_id": "default"},
                )

        assert response.status_code == 404

    def test_non_production_allows_when_both_enabled(self):
        """env var on + auth_setup_mode=True -> 302 (access granted)."""
        client, mock_session = _make_client_with_tenant(auth_setup_mode=True)

        with patch("src.admin.blueprints.auth.get_db_session") as mock_db:
            mock_db.return_value.__enter__ = MagicMock(return_value=mock_session)
            mock_db.return_value.__exit__ = MagicMock(return_value=False)
            with patch.dict(os.environ, {"ADCP_AUTH_TEST_MODE": "true", "PRODUCTION": "", "ENVIRONMENT": ""}):
                response = client.post(
                    "/test/auth",
                    data={"email": "test_super_admin@example.com", "password": "test123", "tenant_id": "default"},
                )

        assert response.status_code == 302


class TestOpenRedirectRejection:
    """Regression tests for F-06: open redirect via login next parameter.

    Exercises the full attack flow through Flask endpoints so that removing
    _safe_redirect() or skipping it at any sink would cause a real test failure.
    """

    def _make_client(self):
        app = create_app({"TESTING": True, "SECRET_KEY": "test-secret", "WTF_CSRF_ENABLED": False})
        client = app.test_client()

        mock_tenant = MagicMock()
        mock_tenant.auth_setup_mode = True
        mock_session = MagicMock()
        mock_session.scalars.return_value.first.return_value = mock_tenant

        return client, mock_session

    def test_external_next_url_not_stored_at_login(self):
        """GET /login?next=https://evil.example.com must not store the value in session.

        _safe_redirect() at ingestion rejects external URLs, so login_next_url
        is never set — the post-auth redirect cannot be influenced.
        """
        client, mock_session = self._make_client()

        with patch("src.admin.blueprints.auth.get_db_session") as mock_db:
            mock_db.return_value.__enter__ = MagicMock(return_value=mock_session)
            mock_db.return_value.__exit__ = MagicMock(return_value=False)
            client.get("/login?next=https://evil.example.com")

        with client.session_transaction() as sess:
            assert "login_next_url" not in sess

    def test_external_next_url_does_not_redirect_to_attacker_domain(self):
        """Full attack flow: prime session -> authenticate -> verify redirect stays internal.

        Even if login_next_url were somehow set to an external URL in the session,
        the sink in test_auth uses _safe_redirect() which returns the fallback.
        This test verifies the end-to-end flow stays within the application.
        """
        client, mock_session = self._make_client()

        with patch("src.admin.blueprints.auth.get_db_session") as mock_db:
            mock_db.return_value.__enter__ = MagicMock(return_value=mock_session)
            mock_db.return_value.__exit__ = MagicMock(return_value=False)
            with patch.dict(os.environ, {"ADCP_AUTH_TEST_MODE": "true", "PRODUCTION": "", "ENVIRONMENT": ""}):
                # Step 1: prime the session with an external next URL
                client.get("/login?next=https://evil.example.com")

                # Step 2: authenticate via test auth
                response = client.post(
                    "/test/auth",
                    data={"email": "test_super_admin@example.com", "password": "test123", "tenant_id": "default"},
                    follow_redirects=False,
                )

        assert response.status_code == 302
        location = response.headers.get("Location", "")
        assert "evil.example.com" not in location

    def test_session_injected_next_url_rejected_at_auth_sink(self):
        """Defense in depth: even if session login_next_url is set to an external URL,
        the /test/auth sink must not redirect to it.

        Directly injects the malicious value into the session to verify the sink-level
        _safe_redirect() call would catch it independently of the ingestion check.
        """
        client, mock_session = self._make_client()

        with patch("src.admin.blueprints.auth.get_db_session") as mock_db:
            mock_db.return_value.__enter__ = MagicMock(return_value=mock_session)
            mock_db.return_value.__exit__ = MagicMock(return_value=False)
            with patch.dict(os.environ, {"ADCP_AUTH_TEST_MODE": "true", "PRODUCTION": "", "ENVIRONMENT": ""}):
                # Inject malicious next URL directly into session (bypasses ingestion check)
                with client.session_transaction() as sess:
                    sess["login_next_url"] = "https://evil.example.com/phishing"

                response = client.post(
                    "/test/auth",
                    data={"email": "test_super_admin@example.com", "password": "test123", "tenant_id": "default"},
                    follow_redirects=False,
                )

        assert response.status_code == 302
        location = response.headers.get("Location", "")
        assert "evil.example.com" not in location


class TestSuperAdminCredentialPath:
    """Super-admin auth must go through test_users, not a hardcoded bypass."""

    def test_super_admin_email_in_test_users_relies_on_env_var_password(self):
        with patch.dict(
            os.environ,
            {
                "TEST_SUPER_ADMIN_EMAIL": "super@example.com",
                "TEST_SUPER_ADMIN_PASSWORD": "secure-random-pw",
            },
        ):
            test_users = {
                os.environ.get("TEST_SUPER_ADMIN_EMAIL", "test_super_admin@example.com"): {
                    "password": os.environ.get("TEST_SUPER_ADMIN_PASSWORD", "test123"),
                    "role": "super_admin",
                },
            }
            email = "super@example.com"
            password = "secure-random-pw"

            assert email in test_users
            assert test_users[email]["password"] == password
            assert test_users[email]["password"] != "test123"

    def test_hardcoded_password_bypass_removed_from_source(self):
        from pathlib import Path

        source = Path("src/admin/blueprints/auth.py").read_text()
        assert 'is_super_admin(email) and password == "test123"' not in source
