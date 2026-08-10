"""Tests for scheduler environment variable handling.

These tests ensure that scheduler modules handle edge cases in environment
variable parsing, particularly empty strings which can cause startup crashes.
"""

import importlib
import os
from unittest.mock import patch

import src.core.database.database_session as database_session_module
import src.services.delivery_webhook_scheduler as delivery_webhook_scheduler_module
import src.services.media_buy_status_scheduler as media_buy_status_scheduler_module

# Captured at collection time, before any test's autouse mock_all_external_dependencies
# fixture (tests/unit/conftest.py) has patched get_db_session.
_REAL_GET_DB_SESSION = database_session_module.get_db_session


def _reload_with_real_db_session(module):
    """Reload `module`, keeping its get_db_session bound to the real function.

    `importlib.reload()` re-executes the module's top level, including its
    `from ...database_session import get_db_session` binding. These tests run
    inside the autouse DB mock, so an unguarded reload would rebind
    get_db_session to this test's mock -- and because reload mutates the
    module object in place (sys.modules keeps pointing at the same object),
    that binding survives this test's teardown and corrupts every later test
    in the process that relies on this module's real get_db_session, including
    cross-suite runs like `make test-entity` that put unit and integration
    tests in one process. Temporarily restoring the source to the real
    function for just the reload closes that hole without disturbing the
    ambient mock for anything else.
    """
    with patch.object(database_session_module, "get_db_session", _REAL_GET_DB_SESSION):
        importlib.reload(module)


class TestDeliveryWebhookSchedulerEnvVar:
    """Test DELIVERY_WEBHOOK_INTERVAL environment variable handling."""

    def test_default_value_when_env_not_set(self):
        """Test that default value (3600) is used when env var is not set."""
        with patch.dict(os.environ, {}, clear=True):
            # Remove the env var if it exists
            os.environ.pop("DELIVERY_WEBHOOK_INTERVAL", None)

            _reload_with_real_db_session(delivery_webhook_scheduler_module)

            assert delivery_webhook_scheduler_module.SLEEP_INTERVAL_SECONDS == 3600

    def test_default_value_when_env_is_empty_string(self):
        """Test that default value is used when env var is empty string.

        This is a regression test for a production crash where docker-compose
        set DELIVERY_WEBHOOK_INTERVAL="" which caused int('') to raise ValueError.
        """
        with patch.dict(os.environ, {"DELIVERY_WEBHOOK_INTERVAL": ""}, clear=False):
            _reload_with_real_db_session(delivery_webhook_scheduler_module)

            # Should use default 3600, not crash with ValueError
            assert delivery_webhook_scheduler_module.SLEEP_INTERVAL_SECONDS == 3600

    def test_custom_value_when_env_is_set(self):
        """Test that custom value is used when env var is set to valid integer."""
        with patch.dict(os.environ, {"DELIVERY_WEBHOOK_INTERVAL": "1800"}, clear=False):
            _reload_with_real_db_session(delivery_webhook_scheduler_module)

            assert delivery_webhook_scheduler_module.SLEEP_INTERVAL_SECONDS == 1800


class TestMediaBuyStatusSchedulerEnvVar:
    """Test MEDIA_BUY_STATUS_CHECK_INTERVAL environment variable handling."""

    def test_default_value_when_env_not_set(self):
        """Test that default value (60) is used when env var is not set."""
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("MEDIA_BUY_STATUS_CHECK_INTERVAL", None)

            _reload_with_real_db_session(media_buy_status_scheduler_module)

            assert media_buy_status_scheduler_module.STATUS_CHECK_INTERVAL_SECONDS == 60

    def test_default_value_when_env_is_empty_string(self):
        """Test that default value is used when env var is empty string.

        This is a regression test - same pattern as DELIVERY_WEBHOOK_INTERVAL.
        """
        with patch.dict(os.environ, {"MEDIA_BUY_STATUS_CHECK_INTERVAL": ""}, clear=False):
            _reload_with_real_db_session(media_buy_status_scheduler_module)

            # Should use default 60, not crash with ValueError
            assert media_buy_status_scheduler_module.STATUS_CHECK_INTERVAL_SECONDS == 60

    def test_custom_value_when_env_is_set(self):
        """Test that custom value is used when env var is set to valid integer."""
        with patch.dict(os.environ, {"MEDIA_BUY_STATUS_CHECK_INTERVAL": "120"}, clear=False):
            _reload_with_real_db_session(media_buy_status_scheduler_module)

            assert media_buy_status_scheduler_module.STATUS_CHECK_INTERVAL_SECONDS == 120
