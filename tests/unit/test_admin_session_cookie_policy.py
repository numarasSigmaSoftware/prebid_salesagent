"""The admin session cookie's Secure flag follows config.is_production().

The cookie block in create_app() has two branches, and the non-production one
sets SESSION_COOKIE_SECURE = False so local HTTP development works. That makes
"which branch runs" a security decision: any deployment this codebase considers
production but that the branch condition does not recognize serves its admin
session cookie without Secure.

While the condition was a literal ``os.environ.get("PRODUCTION") == "true"`` it
could not see FLY_APP_NAME, so a Fly.io deployment relying on Fly's
auto-populated app name took the development branch. These tests grade the
branch through create_app() rather than by reading the source, so a future
rewrite that keeps the shape and loses the behavior still fails.
"""

import os
from unittest.mock import patch

import pytest


@pytest.fixture
def app_config_under_env():
    """Build the admin app under a given environment and return its config.

    The production signal is read while create_app() runs, so the patch has to
    wrap construction, not just the assertion.
    """
    from src.admin.app import create_app

    def _build(**env):
        base = {"PRODUCTION": "", "ENVIRONMENT": "", "FLY_APP_NAME": ""}
        with patch.dict(os.environ, {**base, **env}):
            return create_app({"TESTING": True, "SECRET_KEY": "test-secret", "WTF_CSRF_ENABLED": False}).config

    return _build


class TestSessionCookieSecureFollowsProductionSignal:
    @pytest.mark.parametrize(
        "signal",
        [
            pytest.param({"PRODUCTION": "true"}, id="production-flag"),
            pytest.param({"ENVIRONMENT": "production"}, id="environment-declared"),
            pytest.param({"FLY_APP_NAME": "adcp-sales-agent"}, id="fly-app-name-only"),
        ],
    )
    def test_every_production_signal_secures_the_cookie(self, app_config_under_env, signal):
        """Each signal is sufficient on its own — including FLY_APP_NAME, which
        Fly.io populates without the operator setting anything."""
        config = app_config_under_env(**signal)

        assert config["SESSION_COOKIE_SECURE"] is True
        assert config["SESSION_COOKIE_SAMESITE"] == "None"

    def test_development_keeps_the_cookie_usable_over_http(self, app_config_under_env):
        """The regression guard for the above: no signal must still take the
        development branch, or local HTTP sessions break."""
        config = app_config_under_env()

        assert config["SESSION_COOKIE_SECURE"] is False
        assert config["SESSION_COOKIE_SAMESITE"] == "Lax"

    def test_explicitly_disabled_production_is_honoured(self, app_config_under_env):
        """PRODUCTION=false means "off". A bare-presence check would read the
        non-empty string as true and wrongly apply the production branch."""
        config = app_config_under_env(PRODUCTION="false")

        assert config["SESSION_COOKIE_SECURE"] is False
