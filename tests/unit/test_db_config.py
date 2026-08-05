"""Tests for database configuration helpers."""

import pytest

from src.core.database.db_config import int_env


class TestIntEnv:
    """int_env parses integer environment variables with friendly errors."""

    def test_returns_int_for_valid_env(self, monkeypatch):
        monkeypatch.setenv("TEST_INT_ENV", "42")
        assert int_env("TEST_INT_ENV", "0") == 42

    def test_returns_default_when_env_missing(self, monkeypatch):
        monkeypatch.delenv("TEST_INT_ENV", raising=False)
        assert int_env("TEST_INT_ENV", "7") == 7

    def test_returns_default_when_env_empty(self, monkeypatch):
        monkeypatch.setenv("TEST_INT_ENV", "")
        assert int_env("TEST_INT_ENV", "9") == 9

    def test_raises_for_invalid_value(self, monkeypatch):
        monkeypatch.setenv("TEST_INT_ENV", "not-a-number")
        with pytest.raises(ValueError, match="Invalid integer value for TEST_INT_ENV"):
            int_env("TEST_INT_ENV", "0")


class TestProductionSignalDivergence:
    """config.is_production() is a strict superset of the bare-presence checks.

    Four call sites decide "are we in production?" independently of this function
    -- scripts/run_server.py, src/core/auth.py, src/core/logging_config.py and
    src/core/audit_logger.py -- each by testing ``FLY_APP_NAME or PRODUCTION`` for
    bare presence. The docstring on is_production() once claimed it matched their
    union. It does not, and the disagreement is operator-visible, so it is pinned
    here rather than described in prose.
    """

    @staticmethod
    def _bare_presence(env) -> bool:
        """The shape all four sites open-code today."""
        return bool(env.get("FLY_APP_NAME") or env.get("PRODUCTION"))

    def test_the_four_sites_still_open_code_the_bare_presence_check(self):
        """If a site converges onto is_production(), this test must be revisited --
        it is the trigger for re-deciding the PRODUCTION=false behavior change,
        not a decoration."""
        import re
        from pathlib import Path

        root = Path(__file__).resolve().parents[2]
        expected = {
            "scripts/run_server.py",
            "src/core/auth.py",
            "src/core/logging_config.py",
            "src/core/audit_logger.py",
        }
        found = {
            rel
            for rel in expected
            if re.search(
                r'os\.environ\.get\("FLY_APP_NAME"\)[^\n]*os\.environ\.get\("PRODUCTION"\)',
                (root / rel).read_text(),
            )
        }
        assert found == expected, f"bare-presence check moved or converged in: {expected - found}"

    def test_is_production_diverges_from_the_bare_presence_checks(self, monkeypatch):
        """The two disagreeing cases, stated as behavior rather than prose."""
        from src.core.config import is_production

        # PRODUCTION=false: this function honours the value; a presence check cannot.
        monkeypatch.delenv("ENVIRONMENT", raising=False)
        monkeypatch.delenv("FLY_APP_NAME", raising=False)
        monkeypatch.setenv("PRODUCTION", "false")
        env = {"PRODUCTION": "false"}
        assert is_production() is False
        assert self._bare_presence(env) is True, (
            "an operator who explicitly disables PRODUCTION still gets production "
            "auth/logging/audit behavior from the four open-coded sites"
        )

        # ENVIRONMENT=production alone: production here, invisible to a presence check.
        monkeypatch.delenv("PRODUCTION", raising=False)
        monkeypatch.setenv("ENVIRONMENT", "production")
        assert is_production() is True
        assert self._bare_presence({}) is False
