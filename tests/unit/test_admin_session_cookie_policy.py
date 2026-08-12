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
import re
from pathlib import Path
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
        # HttpOnly and SameSite must not LOOSEN as a side effect of a deploy being
        # pulled into this branch. They were HTTPONLY=False + SAMESITE="None",
        # justified by an EventSource requirement that does not exist: nothing in
        # src/admin, templates or static reads document.cookie, EventSource sends
        # cookies itself without JS reading them, the one EventSource reference
        # left records that it was REPLACED by polling, and there is no
        # cross-origin config. Broadening is_production() would then have moved
        # Fly-only deploys from the development branch's True + "Lax" to False +
        # "None" — transport security gained, XSS and CSRF protection lost, in the
        # same step. The previous revision of this test pinned "None", so it would
        # have stayed green through exactly that.
        assert config["SESSION_COOKIE_HTTPONLY"] is True
        assert config["SESSION_COOKIE_SAMESITE"] == "Lax"

    def test_development_keeps_the_cookie_usable_over_http(self, app_config_under_env):
        """The regression guard for the above: no signal must still take the
        development branch, or local HTTP sessions break."""
        config = app_config_under_env()

        assert config["SESSION_COOKIE_SECURE"] is False
        assert config["SESSION_COOKIE_HTTPONLY"] is True
        assert config["SESSION_COOKIE_SAMESITE"] == "Lax"

    @pytest.mark.parametrize("axis", ["SESSION_COOKIE_HTTPONLY", "SESSION_COOKIE_SAMESITE"])
    def test_production_never_loosens_a_non_transport_axis(self, app_config_under_env, axis):
        """Production must not be WEAKER than development on any non-transport axis.

        Stated as a relation between the two branches rather than as two literals,
        so it keeps holding if the development defaults are ever retuned. Only
        SECURE is expected to differ, and only in the strengthening direction.
        """
        dev = app_config_under_env()
        prod = app_config_under_env(FLY_APP_NAME="adcp-sales-agent")

        assert prod[axis] == dev[axis], (
            f"{axis} differs between development and production ({dev[axis]!r} -> {prod[axis]!r}); "
            "production may only differ from development by being stricter, and only on SECURE"
        )
        assert prod["SESSION_COOKIE_SECURE"] is True and dev["SESSION_COOKIE_SECURE"] is False

    def test_explicitly_disabled_production_is_honoured(self, app_config_under_env):
        """PRODUCTION=false means "off". A bare-presence check would read the
        non-empty string as true and wrongly apply the production branch."""
        config = app_config_under_env(PRODUCTION="false")

        assert config["SESSION_COOKIE_SECURE"] is False


class TestDocumentedCookiePolicyMatchesTheImplementation:
    """docs/security.md must state the values create_app() actually sets.

    The guide documented `SameSite=None` "required for OAuth" and `Path=/admin/`
    long after the implementation moved to `Lax` and `/`, and never mentioned
    HttpOnly at all. Nothing failed, because prose is not executed — so an operator
    reading it got CSRF guidance that was the opposite of the deployed behaviour.

    Compares against the REAL config rather than a second literal list: a test that
    restated the expected values would just be a third place to drift.
    """

    DOC = Path(__file__).resolve().parents[2] / "docs" / "security.md"

    @pytest.mark.parametrize(
        "key",
        ["SESSION_COOKIE_SECURE", "SESSION_COOKIE_HTTPONLY", "SESSION_COOKIE_SAMESITE", "SESSION_COOKIE_PATH"],
    )
    def test_the_guide_states_the_configured_value(self, app_config_under_env, key):
        config = app_config_under_env(ENVIRONMENT="production")
        documented = re.search(rf"^{key} = (.+?)\s*(?:#.*)?$", self.DOC.read_text(), re.MULTILINE)
        assert documented, f"docs/security.md no longer documents {key}"

        actual = config[key]
        expected_literal = f'"{actual}"' if isinstance(actual, str) else str(actual)
        assert documented.group(1).strip() == expected_literal, (
            f"docs/security.md documents {key} = {documented.group(1).strip()} but create_app() sets "
            f"{expected_literal}. Update the guide — operators act on it."
        )
