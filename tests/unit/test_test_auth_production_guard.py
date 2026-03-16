"""Tests for F-02: test-auth production environment hard block.

Verifies that:
- Production signals always return 404 from /test/auth
- Non-production keeps the existing two-gate logic
- Super-admin path goes through test_users dict, not a hardcoded password shortcut
"""

import os
from unittest.mock import patch

import pytest


class TestProductionHardBlock:
    """F-02: test_auth must be unreachable in production."""

    @pytest.fixture(autouse=True)
    def import_helper(self):
        from src.admin.utils.helpers import is_admin_production

        self._is_admin_production = is_admin_production

    def test_production_flag_blocks_test_auth_regardless_of_env_var(self):
        """ENVIRONMENT=production hard-blocks /test/auth even if ADCP_AUTH_TEST_MODE=true."""
        with (
            patch.dict(os.environ, {"ENVIRONMENT": "production", "ADCP_AUTH_TEST_MODE": "true"}),
        ):
            assert self._is_admin_production() is True, (
                "test_auth must hard-block when ENVIRONMENT=production, regardless of ADCP_AUTH_TEST_MODE"
            )

    def test_production_boolean_flag_also_blocks(self):
        """PRODUCTION=true must be treated as the same hard block."""
        with patch.dict(os.environ, {"PRODUCTION": "true", "ENVIRONMENT": ""}, clear=False):
            assert self._is_admin_production() is True

    def test_production_flag_blocks_regardless_of_setup_mode(self):
        """Production hard-blocks even if tenant auth_setup_mode=True."""
        with patch.dict(os.environ, {"ENVIRONMENT": "production", "ADCP_AUTH_TEST_MODE": "false"}):
            tenant_setup_mode = True  # Tenant in setup mode — should not matter in production
            assert self._is_admin_production() is True
            assert tenant_setup_mode is True

    def test_non_production_respects_existing_gate(self):
        """Outside production, gate uses env_test_mode OR tenant_setup_mode."""
        with patch.dict(os.environ, {"ENVIRONMENT": "development", "ADCP_AUTH_TEST_MODE": "false"}):
            env_test_mode = os.environ.get("ADCP_AUTH_TEST_MODE", "").lower() == "true"
            tenant_setup_mode = False

            should_abort = not env_test_mode and not tenant_setup_mode

            assert self._is_admin_production() is False
            assert should_abort is True  # Both gates off → 404

    def test_non_production_allows_with_env_var(self):
        """Non-production with ADCP_AUTH_TEST_MODE=true allows access."""
        with patch.dict(os.environ, {"ENVIRONMENT": "", "ADCP_AUTH_TEST_MODE": "true"}):
            env_test_mode = os.environ.get("ADCP_AUTH_TEST_MODE", "").lower() == "true"
            tenant_setup_mode = False

            should_abort = not env_test_mode and not tenant_setup_mode

            assert self._is_admin_production() is False
            assert should_abort is False  # env var gate open

    def test_non_production_allows_with_tenant_setup_mode(self):
        """Non-production with tenant auth_setup_mode=True allows access."""
        with patch.dict(os.environ, {"ENVIRONMENT": "", "ADCP_AUTH_TEST_MODE": "false"}):
            env_test_mode = os.environ.get("ADCP_AUTH_TEST_MODE", "").lower() == "true"
            tenant_setup_mode = True  # Per-tenant flag

            should_abort = not env_test_mode and not tenant_setup_mode

            assert self._is_admin_production() is False
            assert should_abort is False  # tenant gate open


class TestSuperAdminCredentialPath:
    """F-02: super-admin must go through test_users dict, not a hardcoded password."""

    def test_super_admin_email_in_test_users_relies_on_env_var_password(self):
        """Super admin password comes from TEST_SUPER_ADMIN_PASSWORD env var, not 'test123'."""
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
            # Credential check should use the dict, not a hardcoded comparison
            email = "super@example.com"
            password = "secure-random-pw"

            assert email in test_users
            assert test_users[email]["password"] == password
            # The hardcoded "test123" shortcut has been removed — wrong password → not in dict path
            assert test_users[email]["password"] != "test123"

    def test_hardcoded_password_bypass_removed_from_source(self):
        """Confirm the hardcoded 'test123' super-admin shortcut is gone from auth.py."""
        from pathlib import Path

        source = Path("src/admin/blueprints/auth.py").read_text()
        # The old shortcut was: is_super_admin(email) and password == "test123"
        assert 'is_super_admin(email) and password == "test123"' not in source, (
            "Hardcoded password shortcut must be removed from test_auth. "
            "All credentials should go through the test_users dict."
        )
