"""Tests for scheduler environment variable handling.

These tests ensure that scheduler modules handle edge cases in environment
variable parsing, particularly empty strings which can cause startup crashes.
"""

import os
from unittest.mock import patch

import src.services.delivery_webhook_scheduler as delivery_webhook_scheduler_module
import src.services.media_buy_status_scheduler as media_buy_status_scheduler_module
from tests.unit.conftest import reload_module_with_real_db_session as _reload_with_real_db_session


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
