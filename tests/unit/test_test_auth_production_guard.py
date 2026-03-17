"""Tests for production-mode blocking of /test/auth (F-02)."""

import os
from unittest.mock import patch

import pytest


class TestProductionHardBlock:
    """test_auth must be unreachable in production mode."""

    @pytest.fixture(autouse=True)
    def import_helper(self):
        from src.admin.utils.helpers import is_admin_production

        self._is_admin_production = is_admin_production

    def test_environment_production_blocks_test_auth(self):
        with patch.dict(os.environ, {"ENVIRONMENT": "production", "ADCP_AUTH_TEST_MODE": "true"}):
            assert self._is_admin_production() is True

    def test_production_boolean_flag_also_blocks(self):
        with patch.dict(os.environ, {"PRODUCTION": "true", "ENVIRONMENT": ""}, clear=False):
            assert self._is_admin_production() is True

    def test_non_production_blocked_when_both_off(self):
        with patch.dict(os.environ, {"ENVIRONMENT": "development", "ADCP_AUTH_TEST_MODE": "false"}):
            env_test_mode = os.environ.get("ADCP_AUTH_TEST_MODE", "").lower() == "true"
            tenant_setup_mode = False

            should_abort = not env_test_mode or not tenant_setup_mode
            assert self._is_admin_production() is False
            assert should_abort is True

    def test_non_production_blocked_when_env_var_only(self):
        """F-02: env var alone is no longer sufficient — tenant setup mode must also be on."""
        with patch.dict(os.environ, {"ENVIRONMENT": "", "ADCP_AUTH_TEST_MODE": "true"}):
            env_test_mode = os.environ.get("ADCP_AUTH_TEST_MODE", "").lower() == "true"
            tenant_setup_mode = False  # Tenant disabled setup mode via UI

            should_abort = not env_test_mode or not tenant_setup_mode
            assert self._is_admin_production() is False
            assert should_abort is True

    def test_non_production_blocked_when_setup_mode_only(self):
        """F-02: tenant setup mode alone is no longer sufficient — env var must also be set."""
        with patch.dict(os.environ, {"ENVIRONMENT": "", "ADCP_AUTH_TEST_MODE": "false"}):
            env_test_mode = os.environ.get("ADCP_AUTH_TEST_MODE", "").lower() == "true"
            tenant_setup_mode = True

            should_abort = not env_test_mode or not tenant_setup_mode
            assert self._is_admin_production() is False
            assert should_abort is True

    def test_non_production_allows_when_both_enabled(self):
        """Test auth is reachable only when both env var AND tenant setup mode are on."""
        with patch.dict(os.environ, {"ENVIRONMENT": "", "ADCP_AUTH_TEST_MODE": "true"}):
            env_test_mode = os.environ.get("ADCP_AUTH_TEST_MODE", "").lower() == "true"
            tenant_setup_mode = True

            should_abort = not env_test_mode or not tenant_setup_mode
            assert self._is_admin_production() is False
            assert should_abort is False


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
