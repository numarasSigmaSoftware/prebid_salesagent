"""is_production() is the single source of truth for "are we in production".

src/core/auth.py, src/core/logging_config.py, and src/core/audit_logger.py each
used to hand-roll their own copy of this check as
``os.environ.get("FLY_APP_NAME") or os.environ.get("PRODUCTION")`` wrapped in
``bool()``. That has two bugs relative to the canonical
``src.core.config.is_production()``: it never looks at ENVIRONMENT=production,
and ``bool(os.environ.get("PRODUCTION"))`` treats ANY non-empty string --
including "false", "0", "no" -- as truthy, so an operator setting
PRODUCTION=false to explicitly turn it off was silently still "in production"
for these three modules while config.py's own (now-fixed) check correctly
read it as off. All three now call is_production() directly; this file pins
that they actually do, not just that is_production() itself is correct.
"""

import logging
import os
from unittest.mock import patch

import src.core.audit_logger as audit_logger_module
import src.core.auth as auth_module
import src.core.logging_config as logging_config_module
from src.core.config import is_production
from tests.unit.conftest import reload_module_with_real_db_session


class TestIsProductionSignals:
    """The canonical signal set: ENVIRONMENT=production, PRODUCTION (vocabulary), FLY_APP_NAME."""

    def test_no_signals_is_not_production(self):
        with patch.dict(os.environ, {}, clear=True):
            assert is_production() is False

    def test_environment_production_is_production(self):
        with patch.dict(os.environ, {"ENVIRONMENT": "production"}, clear=True):
            assert is_production() is True

    def test_environment_production_is_case_insensitive(self):
        with patch.dict(os.environ, {"ENVIRONMENT": "Production"}, clear=True):
            assert is_production() is True

    def test_environment_development_is_not_production(self):
        with patch.dict(os.environ, {"ENVIRONMENT": "development"}, clear=True):
            assert is_production() is False

    def test_production_true_is_production(self):
        with patch.dict(os.environ, {"PRODUCTION": "true"}, clear=True):
            assert is_production() is True

    def test_production_false_is_not_production(self):
        """The regression this fix targets: PRODUCTION=false must mean off."""
        with patch.dict(os.environ, {"PRODUCTION": "false"}, clear=True):
            assert is_production() is False

    def test_production_zero_is_not_production(self):
        with patch.dict(os.environ, {"PRODUCTION": "0"}, clear=True):
            assert is_production() is False

    def test_fly_app_name_set_is_production(self):
        with patch.dict(os.environ, {"FLY_APP_NAME": "salesagent"}, clear=True):
            assert is_production() is True

    def test_fly_app_name_empty_is_not_production(self):
        with patch.dict(os.environ, {"FLY_APP_NAME": ""}, clear=True):
            assert is_production() is False


class TestAuthVerboseLogFollowsIsProduction:
    """auth._VERBOSE_AUTH_LOG is a module-level constant: must be re-derived via reload.

    No file handles or other resources are opened by reloading this module, so
    a plain before/after reload (via the shared, get_db_session-safe helper) is
    enough -- unlike audit_logger below, there's nothing extra to clean up.
    """

    def test_production_false_leaves_verbose_logging_on(self):
        """Regression case: the old hand-rolled bool() check got this backwards."""
        with patch.dict(os.environ, {"PRODUCTION": "false"}, clear=True):
            reload_module_with_real_db_session(auth_module)
            assert auth_module._VERBOSE_AUTH_LOG is True
        reload_module_with_real_db_session(auth_module)  # restore for later tests

    def test_environment_production_turns_verbose_logging_off(self):
        """The old hand-rolled check never looked at ENVIRONMENT at all."""
        with patch.dict(os.environ, {"ENVIRONMENT": "production"}, clear=True):
            reload_module_with_real_db_session(auth_module)
            assert auth_module._VERBOSE_AUTH_LOG is False
        reload_module_with_real_db_session(auth_module)  # restore for later tests


class TestLoggingConfigFollowsIsProduction:
    """setup_structured_logging() calls is_production() fresh on every invocation.

    Unlike auth._VERBOSE_AUTH_LOG, this is a per-call function, not a module-level
    constant -- no reload needed, and nothing to restore afterward.
    """

    def test_production_false_uses_development_format(self):
        with (
            patch.dict(os.environ, {"PRODUCTION": "false"}, clear=True),
            patch.object(logging_config_module.logging, "basicConfig") as mock_basic_config,
        ):
            logging_config_module.setup_structured_logging()
            mock_basic_config.assert_called_once_with(
                level=logging_config_module.logging.INFO,
                format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
                force=True,
            )

    def test_environment_production_enables_json_logging(self):
        with (
            patch.dict(os.environ, {"ENVIRONMENT": "production"}, clear=True),
            patch.object(logging_config_module.logging, "basicConfig") as mock_basic_config,
        ):
            logging_config_module.setup_structured_logging()
            mock_basic_config.assert_not_called()


class TestAuditLoggerConsoleHandlerFollowsIsProduction:
    """audit_logger attaches a console StreamHandler only outside production.

    Reloading audit_logger.py re-opens its two FileHandlers (audit.log,
    error.log) at module level -- real file descriptors, not something to
    leak across repeated reloads. Each test explicitly removes and closes
    whatever handlers ITS reload added, instead of reloading a second time
    to "restore" (which would just open another pair of file descriptors).
    """

    def _reload_and_diff_handlers(self, env: dict[str, str]) -> list[logging.Handler]:
        before = list(audit_logger_module.audit_logger.handlers)
        with patch.dict(os.environ, env, clear=True):
            reload_module_with_real_db_session(audit_logger_module)
        after = audit_logger_module.audit_logger.handlers
        added = [h for h in after if h not in before]
        return added

    def _cleanup(self, added: list[logging.Handler]) -> None:
        for handler in added:
            audit_logger_module.audit_logger.removeHandler(handler)
            handler.close()

    def test_production_false_attaches_console_handler(self):
        """Regression case: the old hand-rolled check got this backwards too."""
        added = self._reload_and_diff_handlers({"PRODUCTION": "false"})
        try:
            assert any(type(h) is logging.StreamHandler for h in added)
        finally:
            self._cleanup(added)

    def test_environment_production_omits_console_handler(self):
        added = self._reload_and_diff_handlers({"ENVIRONMENT": "production"})
        try:
            assert not any(type(h) is logging.StreamHandler for h in added)
        finally:
            self._cleanup(added)
