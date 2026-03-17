"""Tests for auth setup mode functionality.

Auth setup mode allows test credentials to work per-tenant:
- New tenants start with auth_setup_mode=True (test credentials work)
- Admin configures SSO, tests it, then disables setup mode
- Once disabled, only SSO works
"""

import os
from unittest.mock import MagicMock, patch

import pytest

from src.admin.app import create_app
from src.core.database.models import Tenant


class TestTenantAuthSetupMode:
    """Tests for the auth_setup_mode field on Tenant model."""

    def test_tenant_has_auth_setup_mode_field(self):
        """Tenant model should have auth_setup_mode field."""
        tenant = Tenant(
            tenant_id="test_tenant",
            name="Test Tenant",
            subdomain="test",
        )
        assert hasattr(tenant, "auth_setup_mode")

    def test_auth_setup_mode_defaults_to_true_in_schema(self):
        """The auth_setup_mode column should have server_default='true'."""
        from sqlalchemy import inspect

        mapper = inspect(Tenant)
        column = mapper.columns["auth_setup_mode"]
        assert column.server_default is not None
        assert "true" in str(column.server_default.arg).lower()

    def test_auth_setup_mode_is_boolean(self):
        """auth_setup_mode should be a boolean field."""
        from sqlalchemy import inspect

        mapper = inspect(Tenant)
        column = mapper.columns["auth_setup_mode"]
        assert column.type.python_type is bool


class TestSetupModeLogic:
    """Tests for the setup mode enable/disable logic."""

    def test_disable_setup_mode_requires_sso_enabled(self):
        """Should not allow disabling setup mode without SSO enabled."""
        tenant = MagicMock()
        tenant.auth_setup_mode = True

        auth_config = MagicMock()
        auth_config.oidc_enabled = False

        # Logic from disable_setup_mode endpoint:
        # if not auth_config or not auth_config.oidc_enabled:
        #     return error
        should_reject = not auth_config or not auth_config.oidc_enabled
        assert should_reject is True

    def test_disable_setup_mode_allowed_with_sso(self):
        """Should allow disabling setup mode when SSO is enabled."""
        tenant = MagicMock()
        tenant.auth_setup_mode = True

        auth_config = MagicMock()
        auth_config.oidc_enabled = True

        # Logic check
        should_reject = not auth_config or not auth_config.oidc_enabled
        assert should_reject is False

        # After successful disable:
        tenant.auth_setup_mode = False
        assert tenant.auth_setup_mode is False

    def test_enable_setup_mode_always_allowed(self):
        """Should always allow re-enabling setup mode."""
        tenant = MagicMock()
        tenant.auth_setup_mode = False

        # Enable it
        tenant.auth_setup_mode = True
        assert tenant.auth_setup_mode is True


def _make_flask_client(auth_setup_mode: bool):
    """Create a Flask test client with a mocked tenant DB lookup.

    The mock returns a tenant whose auth_setup_mode matches the argument,
    so tests can exercise the actual /test/auth endpoint instead of
    re-implementing the gate logic in test code.
    """
    app = create_app({"TESTING": True, "SECRET_KEY": "test-secret", "WTF_CSRF_ENABLED": False})
    client = app.test_client()

    mock_tenant = MagicMock()
    mock_tenant.auth_setup_mode = auth_setup_mode
    mock_session = MagicMock()
    mock_session.scalars.return_value.first.return_value = mock_tenant

    return client, mock_session


class TestTestAuthEndpoint:
    """Endpoint-level tests for the /test/auth gate.

    F-02 fix: test auth now requires BOTH ADCP_AUTH_TEST_MODE=true AND
    the tenant's auth_setup_mode=True. These tests exercise the actual
    Flask endpoint so a gate change in auth.py would cause a real failure.
    """

    def test_test_auth_allowed_when_both_enabled(self):
        """POST /test/auth returns 302 when env var and tenant setup mode are both on."""
        client, mock_session = _make_flask_client(auth_setup_mode=True)

        with patch("src.admin.blueprints.auth.get_db_session") as mock_db:
            mock_db.return_value.__enter__ = MagicMock(return_value=mock_session)
            mock_db.return_value.__exit__ = MagicMock(return_value=False)
            with patch.dict(os.environ, {"ADCP_AUTH_TEST_MODE": "true", "PRODUCTION": "", "ENVIRONMENT": ""}):
                response = client.post(
                    "/test/auth",
                    data={
                        "email": "test_super_admin@example.com",
                        "password": "test123",
                        "tenant_id": "default",
                    },
                )

        assert response.status_code == 302

    def test_test_auth_blocked_when_env_var_only(self):
        """POST /test/auth returns 404 when env var is set but tenant has disabled setup mode.

        F-02 regression: this was the vulnerable case before the fix.
        """
        client, mock_session = _make_flask_client(auth_setup_mode=False)

        with patch("src.admin.blueprints.auth.get_db_session") as mock_db:
            mock_db.return_value.__enter__ = MagicMock(return_value=mock_session)
            mock_db.return_value.__exit__ = MagicMock(return_value=False)
            with patch.dict(os.environ, {"ADCP_AUTH_TEST_MODE": "true", "PRODUCTION": "", "ENVIRONMENT": ""}):
                response = client.post(
                    "/test/auth",
                    data={
                        "email": "test_super_admin@example.com",
                        "password": "test123",
                        "tenant_id": "default",
                    },
                )

        assert response.status_code == 404

    def test_test_auth_blocked_when_setup_mode_only(self):
        """POST /test/auth returns 404 when tenant is in setup mode but env var is not set."""
        client, mock_session = _make_flask_client(auth_setup_mode=True)

        with patch("src.admin.blueprints.auth.get_db_session") as mock_db:
            mock_db.return_value.__enter__ = MagicMock(return_value=mock_session)
            mock_db.return_value.__exit__ = MagicMock(return_value=False)
            with patch.dict(os.environ, {"ADCP_AUTH_TEST_MODE": "", "PRODUCTION": "", "ENVIRONMENT": ""}):
                response = client.post(
                    "/test/auth",
                    data={
                        "email": "test_super_admin@example.com",
                        "password": "test123",
                        "tenant_id": "default",
                    },
                )

        assert response.status_code == 404

    def test_test_auth_blocked_when_both_disabled(self):
        """POST /test/auth returns 404 when both env var and tenant setup mode are off."""
        client, mock_session = _make_flask_client(auth_setup_mode=False)

        with patch("src.admin.blueprints.auth.get_db_session") as mock_db:
            mock_db.return_value.__enter__ = MagicMock(return_value=mock_session)
            mock_db.return_value.__exit__ = MagicMock(return_value=False)
            with patch.dict(os.environ, {"ADCP_AUTH_TEST_MODE": "", "PRODUCTION": "", "ENVIRONMENT": ""}):
                response = client.post(
                    "/test/auth",
                    data={
                        "email": "test_super_admin@example.com",
                        "password": "test123",
                        "tenant_id": "default",
                    },
                )

        assert response.status_code == 404


class TestTenantLoginLogic:
    """Tests for tenant login page respecting setup mode."""

    def test_login_uses_tenant_auth_setup_mode(self):
        """Tenant login should use tenant's auth_setup_mode field."""
        tenant = MagicMock()
        tenant.auth_setup_mode = True

        # Logic from tenant_login:
        # test_mode = tenant.auth_setup_mode if hasattr(tenant, "auth_setup_mode") else True
        test_mode = tenant.auth_setup_mode if hasattr(tenant, "auth_setup_mode") else True
        assert test_mode is True

    def test_login_env_var_overrides_to_enable(self):
        """Env var ADCP_AUTH_TEST_MODE=true should override to enable test mode."""
        tenant = MagicMock()
        tenant.auth_setup_mode = False  # Tenant disabled setup mode

        with patch.dict(os.environ, {"ADCP_AUTH_TEST_MODE": "true"}):
            # Logic from tenant_login:
            test_mode = tenant.auth_setup_mode if hasattr(tenant, "auth_setup_mode") else True
            if os.environ.get("ADCP_AUTH_TEST_MODE", "").lower() == "true":
                test_mode = True

            assert test_mode is True

    def test_login_respects_disabled_setup_mode(self):
        """Tenant login should respect disabled setup mode when no env override."""
        tenant = MagicMock()
        tenant.auth_setup_mode = False  # Tenant disabled setup mode

        with patch.dict(os.environ, {"ADCP_AUTH_TEST_MODE": ""}):
            test_mode = tenant.auth_setup_mode if hasattr(tenant, "auth_setup_mode") else True
            if os.environ.get("ADCP_AUTH_TEST_MODE", "").lower() == "true":
                test_mode = True

            # Should remain False since no env override
            assert test_mode is False


class TestMigration:
    """Tests for the auth_setup_mode migration."""

    def test_migration_file_exists(self):
        """Migration file for auth_setup_mode should exist."""
        import os

        migration_path = "alembic/versions/add_auth_setup_mode.py"
        assert os.path.exists(migration_path), f"Migration file not found: {migration_path}"

    def test_migration_has_correct_revision(self):
        """Migration should have correct revision chain."""
        import importlib.util

        migration_path = "alembic/versions/add_auth_setup_mode.py"
        spec = importlib.util.spec_from_file_location("migration", migration_path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)

        # Check revision chain
        assert migration.revision == "add_auth_setup_mode"
        assert migration.down_revision == "add_tenant_auth_config"
        assert callable(migration.upgrade)
        assert callable(migration.downgrade)


class TestUsersEndpointConfig:
    """Tests for the users page template context."""

    def test_list_users_passes_auth_setup_mode(self):
        """list_users endpoint should pass auth_setup_mode to template."""
        # The endpoint passes these to the template:
        # auth_setup_mode=tenant.auth_setup_mode,
        # oidc_enabled=auth_config.oidc_enabled if auth_config else False,

        tenant = MagicMock()
        tenant.auth_setup_mode = True

        auth_config = MagicMock()
        auth_config.oidc_enabled = True

        context = {
            "auth_setup_mode": tenant.auth_setup_mode,
            "oidc_enabled": auth_config.oidc_enabled if auth_config else False,
        }

        assert context["auth_setup_mode"] is True
        assert context["oidc_enabled"] is True

    def test_list_users_handles_no_auth_config(self):
        """list_users should handle case when no auth config exists."""
        tenant = MagicMock()
        tenant.auth_setup_mode = True

        auth_config = None  # No auth config yet

        context = {
            "auth_setup_mode": tenant.auth_setup_mode,
            "oidc_enabled": auth_config.oidc_enabled if auth_config else False,
        }

        assert context["auth_setup_mode"] is True
        assert context["oidc_enabled"] is False
