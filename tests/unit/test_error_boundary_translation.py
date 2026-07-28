"""Tests for error boundary translation — AdCPError at each transport boundary.

Validates that:
- MCP boundary: AdCPError → ToolError with preserved error_code, message, and recovery
- A2A boundary: AdCPError → A2AError with correct JSON-RPC error code and recovery
- REST boundary: AdCPError → proper HTTP status code with recovery field
- ValueError and PermissionError are caught at boundaries
- extract_error_info handles AdCPError instances

"""

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.core.exceptions import (
    AdCPAdapterError,
    AdCPError,
    AdCPNotFoundError,
    AdCPValidationError,
)

# ---------------------------------------------------------------------------
# Wire-shape helpers — every boundary produces the AdCP spec two-layer
# envelope, so the boundary-specific wrappers below delegate to a single
# shared ``assert_envelope_shape`` in ``tests/helpers/``. A spec change to
# the envelope is now a one-place update.
# ---------------------------------------------------------------------------
from tests.helpers import assert_envelope_shape  # noqa: E402
from tests.helpers.pinned_schema import pinned_error_code_metadata
from tests.helpers.secret_scrub import SECRET_BEARING_MESSAGE, assert_no_secret_leak, serialize_wire_error

# Per-boundary assertion wrappers were removed in favor of the canonical
# `assert_envelope_shape` helper. Call sites use keyword flags directly:
#   MCP:  assert_envelope_shape(exc, code, check_mcp_tool_error=True, ...)
#   A2A:  assert_envelope_shape(data, code, recovery=...)
#   REST: assert_envelope_shape(body, code, recovery=..., message_substr=...)


class TestBestEffortBoundaryIdentity:
    """Observability scope resolution must enrich when possible and fail closed."""

    def test_returns_resolved_tenant_and_principal(self):
        from src.core.tool_error_logging import best_effort_boundary_identity

        identity = SimpleNamespace(tenant_id="tenant-observed", principal_id="principal-observed")

        assert best_effort_boundary_identity(lambda: identity, transport="mcp") == (
            "tenant-observed",
            "principal-observed",
        )

    def test_resolution_failure_degrades_to_unscoped_record(self):
        from src.core.tool_error_logging import best_effort_boundary_identity

        def fail_resolution():
            raise RuntimeError("identity backend unavailable")

        assert best_effort_boundary_identity(fail_resolution, transport="a2a") == (None, None)

    def test_tenant_hint_without_principal_is_unscoped(self):
        """Anonymous resolution cannot authorize tenant-visible sink writes."""
        from src.core.tool_error_logging import best_effort_boundary_identity

        identity = SimpleNamespace(tenant_id="header-selected-tenant", principal_id=None)

        assert best_effort_boundary_identity(lambda: identity, transport="rest") == (None, None)

    def test_a2a_activity_scope_does_not_fabricate_unknown_tenant(self):
        """Anonymous discovery activity remains unscoped like A2A boundary errors."""
        from src.a2a_server.adcp_a2a_server import _a2a_activity_scope

        assert _a2a_activity_scope(None) == (None, None)


class TestBoundaryObservabilitySanitization:
    """Tenant-visible and persistent sinks must never receive raw internal details."""

    def test_typed_internal_error_is_scrubbed_before_activity_and_audit_sinks(self):
        from src.core.tool_error_logging import record_boundary_error

        feed_records: list[dict[str, object]] = []
        audit_records: list[dict[str, object]] = []
        audit_logger = SimpleNamespace(log_operation=lambda **kwargs: audit_records.append(kwargs))

        with (
            patch(
                "src.services.activity_feed.activity_feed.log_error",
                side_effect=lambda **kwargs: feed_records.append(kwargs),
            ),
            patch(
                "src.core.audit_logger.get_audit_logger",
                return_value=audit_logger,
            ),
        ):
            record_boundary_error(
                "a2a",
                "create_media_buy",
                AdCPAdapterError(SECRET_BEARING_MESSAGE),
                tenant_id="tenant-safe",
                principal_id="principal-safe",
            )

        assert len(feed_records) == 1
        assert len(audit_records) == 1
        feed_payload = feed_records[0]
        audit_payload = audit_records[0]
        assert feed_payload["tenant_id"] == "tenant-safe"
        assert feed_payload["error_code"] == "SERVICE_UNAVAILABLE"
        feed_message = feed_payload["error_message"]
        audit_error = audit_payload["error"]
        assert isinstance(feed_message, str)
        assert isinstance(audit_error, str)
        assert feed_message.endswith(audit_error)
        assert_no_secret_leak(feed_payload, context="tenant activity error payload")
        assert_no_secret_leak(audit_payload, context="persistent audit error payload")


# ---------------------------------------------------------------------------
# MCP Boundary: extract_error_info
# ---------------------------------------------------------------------------


class TestAuthCodeConformance:
    """The auth taxonomy follows tagged AdCP 3.1.1, not the SDK's older vocabulary."""

    @pytest.mark.parametrize(
        ("error_class", "code"),
        [
            pytest.param("AdCPAuthMissingError", "AUTH_MISSING", id="missing-credentials"),
            pytest.param("AdCPAuthInvalidError", "AUTH_INVALID", id="rejected-credentials"),
        ],
    )
    def test_split_auth_error_defaults_match_pinned_enum_metadata(self, error_class, code):
        """Absent versus rejected credentials carry their spec code, recovery, and suggestion."""
        from src.core import exceptions

        metadata = pinned_error_code_metadata()[code]
        error = getattr(exceptions, error_class)("test auth failure")

        assert error.wire_error_code == code
        assert error.recovery == metadata["recovery"]
        assert error.suggestion == metadata["suggestion"]


class TestAuthContractOracle:
    """The shared transport oracle grades exact codes and both suggestion mirrors."""

    @pytest.mark.parametrize(
        ("transport", "credential_state", "expected_code"),
        [
            pytest.param("a2a", "missing", "AUTH_MISSING", id="a2a-missing"),
            pytest.param("a2a", "invalid", "AUTH_INVALID", id="a2a-invalid"),
            pytest.param("e2e_a2a", "missing", "AUTH_MISSING", id="e2e-a2a-missing"),
            pytest.param("mcp", "invalid", "AUTH_INVALID", id="mcp-invalid"),
            pytest.param("rest", "missing", "AUTH_MISSING", id="rest-missing"),
            pytest.param("e2e_mcp", "missing", "AUTH_MISSING", id="e2e-mcp-missing"),
            pytest.param("e2e_rest", "invalid", "AUTH_INVALID", id="e2e-rest-invalid"),
            pytest.param("impl", "invalid", "AUTH_REQUIRED", id="impl-invalid"),
        ],
    )
    def test_expected_auth_contract_is_transport_aware(self, transport, credential_state, expected_code):
        from tests.helpers.auth_contract import expected_auth_contract

        code, recovery, suggestion = expected_auth_contract(transport, credential_state)
        metadata = pinned_error_code_metadata()[expected_code]

        assert code == expected_code
        assert recovery == metadata["recovery"]
        assert suggestion == metadata["suggestion"]

    @pytest.mark.parametrize("unknown_transport", [None, "a22", "future_transport"])
    def test_expected_auth_contract_rejects_unknown_transport(self, unknown_transport):
        from tests.helpers.auth_contract import expected_auth_contract

        with pytest.raises(AssertionError, match="Unknown auth-contract transport"):
            expected_auth_contract(unknown_transport, "missing")

    def test_expected_auth_contract_rejects_unknown_credential_state(self):
        from tests.helpers.auth_contract import expected_auth_contract

        with pytest.raises(AssertionError, match="Unknown credential state"):
            expected_auth_contract("a2a", "invalidd")  # type: ignore[arg-type]

    @pytest.mark.parametrize("mutated_layer", ["errors", "adcp_error"])
    def test_two_layer_auth_contract_rejects_suggestion_drift(self, mutated_layer):
        from tests.helpers.auth_contract import assert_two_layer_auth_contract, expected_auth_contract

        code, recovery, suggestion = expected_auth_contract("a2a", "missing")
        envelope = {
            "errors": [
                {"code": code, "message": "Credentials required", "recovery": recovery, "suggestion": suggestion}
            ],
            "adcp_error": {
                "code": code,
                "message": "Credentials required",
                "recovery": recovery,
                "suggestion": suggestion,
            },
        }
        if mutated_layer == "errors":
            envelope["errors"][0]["suggestion"] = "drifted guidance"
        else:
            envelope["adcp_error"]["suggestion"] = "drifted guidance"

        with pytest.raises(AssertionError, match=r"suggestion must match pinned AUTH_MISSING guidance"):
            assert_two_layer_auth_contract(envelope, "a2a", "missing")


class TestExtractErrorInfoAdCPError:
    """extract_error_info must recognize AdCPError and extract error_code + message + recovery."""

    def test_adcp_validation_error_extracts_code_and_message(self):
        """AdCPValidationError → ('VALIDATION_ERROR', 'bad field', 'correctable')."""
        from src.core.tool_error_logging import extract_error_info

        exc = AdCPValidationError("bad field")
        code, message, recovery = extract_error_info(exc)
        assert code == "VALIDATION_ERROR"
        assert message == "bad field"
        assert recovery == "correctable"

    def test_adcp_not_found_extracts_code_and_message(self):
        """AdCPNotFoundError → ('NOT_FOUND', 'resource missing', 'correctable').

        Recovery follows the wire code INVALID_REQUEST=correctable.
        """
        from src.core.tool_error_logging import extract_error_info

        exc = AdCPNotFoundError("resource missing")
        code, message, recovery = extract_error_info(exc)
        assert code == "NOT_FOUND"
        assert message == "resource missing"
        assert recovery == "correctable"

    def test_adcp_adapter_error_extracts_code_and_message(self):
        """AdCPAdapterError → ('SERVICE_UNAVAILABLE', 'GAM down', 'transient')."""
        from src.core.tool_error_logging import extract_error_info

        exc = AdCPAdapterError("GAM down")
        code, message, recovery = extract_error_info(exc)
        assert code == "SERVICE_UNAVAILABLE"
        assert message == "GAM down"
        assert recovery == "transient"

    # AdCPConflictError recovery (CONFLICT → transient) and AdCPBudgetExhaustedError
    # recovery (BUDGET_EXHAUSTED → terminal) are graded against the pinned enum by the
    # recovery-conformance oracle (#1417). The prior per-class literal methods
    # asserted the old correctable values and are removed.

    def test_adcp_gone_error_extracts_code_and_message(self):
        """AdCPGoneError → ('INVALID_STATE', 'proposal expired', 'correctable').

        Recovery defaults to ``correctable`` — the resource itself is gone but
        the buyer can recover by referencing a different resource.
        """
        from src.core.exceptions import AdCPGoneError
        from src.core.tool_error_logging import extract_error_info

        exc = AdCPGoneError("proposal expired")
        code, message, recovery = extract_error_info(exc)
        assert code == "INVALID_STATE"
        assert message == "proposal expired"
        assert recovery == "correctable"

    def test_adcp_service_unavailable_error_extracts_code_and_message(self):
        """AdCPServiceUnavailableError → ('SERVICE_UNAVAILABLE', 'product unavailable', 'transient')."""
        from src.core.exceptions import AdCPServiceUnavailableError
        from src.core.tool_error_logging import extract_error_info

        exc = AdCPServiceUnavailableError("product unavailable")
        code, message, recovery = extract_error_info(exc)
        assert code == "SERVICE_UNAVAILABLE"
        assert message == "product unavailable"
        assert recovery == "transient"

    def test_adcp_base_error_extracts_code_and_message(self):
        """AdCPError base → ('INTERNAL_ERROR', 'something broke', 'transient').

        Recovery follows the wire code SERVICE_UNAVAILABLE=transient.
        """
        from src.core.tool_error_logging import extract_error_info

        exc = AdCPError("something broke")
        code, message, recovery = extract_error_info(exc)
        assert code == "INTERNAL_ERROR"
        assert message == "something broke"
        assert recovery == "transient"

    def test_adcp_rate_limit_error_extracts_transient_recovery(self):
        """AdCPRateLimitError → ('RATE_LIMITED', 'too fast', 'transient')."""
        from src.core.exceptions import AdCPRateLimitError
        from src.core.tool_error_logging import extract_error_info

        exc = AdCPRateLimitError("too fast")
        code, message, recovery = extract_error_info(exc)
        assert code == "RATE_LIMITED"
        assert message == "too fast"
        assert recovery == "transient"

    def test_plain_exception_returns_none_recovery(self):
        """Non-AdCPError exceptions return None for recovery."""
        from src.core.tool_error_logging import extract_error_info

        exc = RuntimeError("unexpected")
        code, message, recovery = extract_error_info(exc)
        assert code == "RuntimeError"
        assert message == "unexpected"
        assert recovery is None

    def test_tool_error_with_recovery_arg(self):
        """ToolError with 3 args extracts recovery from third arg."""
        from fastmcp.exceptions import ToolError

        from src.core.tool_error_logging import extract_error_info

        exc = ToolError("SERVICE_UNAVAILABLE", "GAM down", "transient")
        code, message, recovery = extract_error_info(exc)
        assert code == "SERVICE_UNAVAILABLE"
        assert message == "GAM down"
        assert recovery == "transient"

    def test_tool_error_without_recovery_returns_none(self):
        """ToolError with 2 args returns None for recovery."""
        from fastmcp.exceptions import ToolError

        from src.core.tool_error_logging import extract_error_info

        exc = ToolError("VALIDATION_ERROR", "bad field")
        code, message, recovery = extract_error_info(exc)
        assert code == "VALIDATION_ERROR"
        assert message == "bad field"
        assert recovery is None


# ---------------------------------------------------------------------------
# MCP Boundary: with_error_logging translates AdCPError → ToolError
# ---------------------------------------------------------------------------


class TestMCPBoundaryAdCPErrorTranslation:
    """with_error_logging must catch AdCPError and re-raise as ToolError with recovery."""

    def test_adcp_validation_becomes_tool_error(self):
        """AdCPValidationError from tool → ToolError with VALIDATION_ERROR code."""
        from fastmcp.exceptions import ToolError

        from src.core.tool_error_logging import with_error_logging

        def failing_tool():
            raise AdCPValidationError("bad field")

        wrapped = with_error_logging(failing_tool)

        with pytest.raises(ToolError) as exc_info:
            wrapped()

        # ToolError should carry the error code from AdCPError
        assert "VALIDATION_ERROR" in str(exc_info.value) or (
            exc_info.value.args and exc_info.value.args[0] == "VALIDATION_ERROR"
        )

    def test_adcp_validation_tool_error_carries_recovery(self):
        """AdCPValidationError → ToolError envelope carries 'correctable' recovery."""
        from fastmcp.exceptions import ToolError

        from src.core.tool_error_logging import with_error_logging

        def failing_tool():
            raise AdCPValidationError("bad field")

        wrapped = with_error_logging(failing_tool)

        with pytest.raises(ToolError) as exc_info:
            wrapped()

        assert_envelope_shape(exc_info.value, "VALIDATION_ERROR", check_mcp_tool_error=True, recovery="correctable")

    def test_adcp_adapter_tool_error_carries_transient_recovery(self):
        """AdCPAdapterError → scrubbed ToolError envelope carries canonical recovery."""
        from fastmcp.exceptions import ToolError

        from src.core.tool_error_logging import with_error_logging

        def failing_tool():
            raise AdCPAdapterError("GAM down")

        wrapped = with_error_logging(failing_tool)

        with pytest.raises(ToolError) as exc_info:
            wrapped()

        assert_envelope_shape(
            exc_info.value,
            "SERVICE_UNAVAILABLE",
            check_mcp_tool_error=True,
            recovery="transient",
        )
        assert "GAM down" not in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_async_adcp_validation_becomes_tool_error(self):
        """Async: AdCPValidationError → ToolError envelope with preserved code and recovery."""
        from fastmcp.exceptions import ToolError

        from src.core.tool_error_logging import with_error_logging

        async def failing_tool():
            raise AdCPValidationError("bad field")

        wrapped = with_error_logging(failing_tool)

        with pytest.raises(ToolError) as exc_info:
            await wrapped()

        assert_envelope_shape(exc_info.value, "VALIDATION_ERROR", check_mcp_tool_error=True, recovery="correctable")

    def test_tool_error_still_passes_through(self):
        """Existing ToolError behavior must be preserved — re-raised unchanged."""
        from fastmcp.exceptions import ToolError

        from src.core.tool_error_logging import with_error_logging

        def failing_tool():
            raise ToolError("EXISTING_CODE", "existing message")

        wrapped = with_error_logging(failing_tool)

        with pytest.raises(ToolError) as exc_info:
            wrapped()

        # Should be the same ToolError, not wrapped
        assert exc_info.value.args[0] == "EXISTING_CODE"

    def test_valueerror_becomes_scrubbed_tool_error(self):
        """ValueError from tool → scrubbed ToolError with VALIDATION_ERROR code."""
        from fastmcp.exceptions import ToolError

        from src.core.tool_error_logging import with_error_logging

        def failing_tool():
            raise ValueError(SECRET_BEARING_MESSAGE)

        wrapped = with_error_logging(failing_tool)

        with pytest.raises(ToolError) as exc_info:
            wrapped()

        assert_envelope_shape(
            exc_info.value,
            "VALIDATION_ERROR",
            check_mcp_tool_error=True,
            recovery="correctable",
        )
        assert_no_secret_leak(str(exc_info.value), context="MCP ValueError envelope")

    def test_permission_error_becomes_scrubbed_tool_error(self):
        """PermissionError from tool → scrubbed ToolError with AUTH_REQUIRED code."""
        from fastmcp.exceptions import ToolError

        from src.core.tool_error_logging import with_error_logging

        def failing_tool():
            raise PermissionError(SECRET_BEARING_MESSAGE)

        wrapped = with_error_logging(failing_tool)

        with pytest.raises(ToolError) as exc_info:
            wrapped()

        assert_envelope_shape(
            exc_info.value,
            "AUTH_REQUIRED",
            check_mcp_tool_error=True,
            recovery="correctable",
        )
        assert_no_secret_leak(str(exc_info.value), context="MCP PermissionError envelope")

    def test_untyped_crash_becomes_scrubbed_tool_error(self):
        """A generic, unmapped exception → scrubbed SERVICE_UNAVAILABLE ToolError.

        The fifth ``normalize_to_adcp_error`` outcome, and the only one this class did not
        drive: the fall-through for an exception that is neither an ``AdCPError`` nor one of
        the mapped built-ins. The sibling cases above all inject something the normalization
        registry recognises, so removing the boundary's untyped arm left the whole suite
        green — the branch that turns a real crash into an AdCP envelope was unlocked.

        ``assert_sanitized_wire_error`` is deliberately not used: it requires the code to
        have a canonical sanitized presentation, and SERVICE_UNAVAILABLE is in the internal
        bucket rather than ``_SANITIZED_BY_WIRE_CODE``.
        """
        from fastmcp.exceptions import ToolError

        from src.core.tool_error_logging import with_error_logging

        def failing_tool():
            raise RuntimeError(SECRET_BEARING_MESSAGE)

        wrapped = with_error_logging(failing_tool)

        with pytest.raises(ToolError) as exc_info:
            wrapped()

        assert_envelope_shape(
            exc_info.value,
            "SERVICE_UNAVAILABLE",
            check_mcp_tool_error=True,
            recovery="transient",
        )
        assert exc_info.value.status_code == 500
        assert_no_secret_leak(str(exc_info.value), context="MCP untyped crash envelope")


def _uncovered_correctable_targets(registry, sanitized_by_code, internal_codes, standard_codes):
    """Registry semantic targets lacking a sanitized client-correctable category.

    ``adcp_class`` is the single authority read here and instantiated by production. Projectors
    provide kwargs only, so they cannot make runtime emit a different wire code. Pure over its
    inputs so the same logic grades the real registry and a known-bad synthetic registry.
    """
    uncovered = []
    for _exc_type, adcp_class, _kwargs_projector in registry:
        wire_code = adcp_class("probe").wire_error_code
        if wire_code in internal_codes or wire_code not in standard_codes:
            continue
        if wire_code not in sanitized_by_code:
            uncovered.append((adcp_class.__name__, wire_code))
    return uncovered


class TestSafeAdcpErrorSuggestionMatchesRecovery:
    """``safe_adcp_error``'s sanitized suggestion must not contradict the preserved recovery.

    The sanitizer scrubs the (possibly secret-bearing) message on every internal error but keeps
    the wire code + recovery. A terminal ``CONFIGURATION_ERROR`` (per 3.1.1 the buyer MUST NOT
    auto-retry; a seller operator must resolve it) paired with a "retry the request later"
    suggestion is a self-contradictory envelope. These pins are authored independently of the
    module's suggestion CONSTANTS — they assert the semantic ``retry`` property directly — so they
    are not tautological with the sanitizer that produces the value (the config webhook payload
    test derives its expected envelope through ``safe_adcp_error`` itself and only checks the
    generic message, so it can't see this).
    """

    def test_terminal_config_error_suggestion_does_not_advise_retry(self):
        from src.core.exceptions import AdCPConfigurationError, safe_adcp_error

        # A raise site that interpolates a secret into a CONFIGURATION_ERROR (recovery=terminal).
        safe = safe_adcp_error(AdCPConfigurationError(f"decrypt failed: {SECRET_BEARING_MESSAGE}"))

        assert safe.recovery == "terminal", "CONFIGURATION_ERROR recovery must be preserved as terminal"
        assert "retry" not in safe.suggestion.lower(), (
            f"a terminal error must not advise the buyer to retry, got: {safe.suggestion!r}"
        )
        # The secret is still scrubbed from BOTH message and suggestion (shared leak oracle).
        assert_no_secret_leak(safe.suggestion, context="suggestion")
        assert_no_secret_leak(safe.message, context="message")

    def test_transient_service_unavailable_suggestion_advises_retry(self):
        from src.core.exceptions import AdCPServiceUnavailableError, safe_adcp_error

        safe = safe_adcp_error(AdCPServiceUnavailableError("db connection pool exhausted"))

        assert safe.recovery == "transient", "SERVICE_UNAVAILABLE recovery must be preserved as transient"
        assert "retry" in safe.suggestion.lower(), (
            f"a transient error should advise the buyer to retry, got: {safe.suggestion!r}"
        )

    def test_untyped_internal_error_advises_retry(self):
        """An untyped crash sanitizes to a transient base error, so retry guidance is correct."""
        from src.core.exceptions import safe_adcp_error

        safe = safe_adcp_error(RuntimeError(f"kaboom {SECRET_BEARING_MESSAGE}"))

        assert safe.recovery == "transient"
        assert "retry" in safe.suggestion.lower()
        assert_no_secret_leak(safe.suggestion, context="suggestion")
        assert_no_secret_leak(safe.message, context="message")

    def test_unknown_internal_code_coerces_to_transient_service_unavailable(self):
        """An internal code the wire doesn't model coerces to SERVICE_UNAVAILABLE — whose canonical
        recovery is transient. The fallback must NOT emit SERVICE_UNAVAILABLE paired with terminal
        (the code and recovery would disagree); it emits transient + retry guidance."""
        from src.core.exceptions import AdCPError, safe_adcp_error

        safe = safe_adcp_error(
            AdCPError.synthesize(SECRET_BEARING_MESSAGE, error_code="UNKNOWN_INTERNAL", recovery="correctable")
        )

        assert safe.wire_error_code == "SERVICE_UNAVAILABLE"
        assert safe.recovery == "transient", "SERVICE_UNAVAILABLE's canonical recovery is transient, not terminal"
        assert "retry" in safe.suggestion.lower()
        assert_no_secret_leak(safe.message, context="message")
        assert_no_secret_leak(safe.suggestion, context="suggestion")

    def test_internal_error_recovery_normalized_to_canonical_not_instance_override(self):
        """A raise site can tag an internal error with a recovery that contradicts its wire code
        (AdCPAdapterError → SERVICE_UNAVAILABLE, but recovery='correctable'). The sanitizer
        NORMALIZES recovery to the code's canonical value so code + recovery + suggestion agree —
        the buyer never sees SERVICE_UNAVAILABLE + correctable + a 'retry later' suggestion."""
        from src.core.exceptions import AdCPAdapterError, safe_adcp_error

        safe = safe_adcp_error(AdCPAdapterError(f"db pool: {SECRET_BEARING_MESSAGE}", recovery="correctable"))

        assert safe.wire_error_code == "SERVICE_UNAVAILABLE"
        assert safe.recovery == "transient", "recovery must be normalized to the code's canonical value"
        assert "retry" in safe.suggestion.lower()
        assert_no_secret_leak(safe.message, context="message")
        assert_no_secret_leak(safe.suggestion, context="suggestion")

    def test_suggestion_table_covers_all_recovery_hints(self):
        """The suggestion table must be exhaustive over RecoveryHint — a new recovery value added
        without a suggestion would KeyError at runtime instead of silently mis-guiding."""
        from typing import get_args

        from src.core.exceptions import RecoveryHint, _sanitized_suggestion_for

        for recovery in get_args(RecoveryHint):
            assert _sanitized_suggestion_for(recovery), f"no sanitized suggestion for recovery={recovery!r}"

    def test_three_way_suggestion_semantics(self):
        """Each recovery class gets guidance matching its semantics: transient→retry,
        correctable→adjust/resubmit (NOT retry-later), terminal→no-retry."""
        from src.core.exceptions import _sanitized_suggestion_for

        assert "retry" in _sanitized_suggestion_for("transient").lower()
        assert "retry" not in _sanitized_suggestion_for("terminal").lower()
        correctable = _sanitized_suggestion_for("correctable").lower()
        assert "retry" not in correctable, "correctable is not a retry-later case"
        assert any(word in correctable for word in ("adjust", "resubmit", "review")), (
            "correctable must give correction guidance, not silence"
        )

    def test_sanitized_suggestions_match_pinned_spec_enum(self):
        """The category suggestion a scrub emits IS the code's canonical spec text.

        Grounds ``_SANITIZED_BY_WIRE_CODE``'s suggestion legs (and the shared spec-derived
        constants they source from) in the PINNED enum fixture — the independent authority —
        so a hand-edited suggestion that drifts from ``error-code.json`` enumMetadata reddens
        here. This is the oracle that caught the AUTH_REQUIRED suggestion advising retry while
        the spec enum says "do NOT auto-retry rejected credentials".
        """
        from src.core.exceptions import _SANITIZED_BY_WIRE_CODE, safe_adcp_error

        enum_meta = pinned_error_code_metadata()

        for wire_code, (_message, suggestion) in _SANITIZED_BY_WIRE_CODE.items():
            assert suggestion == enum_meta[wire_code]["suggestion"], (
                f"{wire_code} sanitized suggestion drifted from the pinned spec enum"
            )
        # And on the emitted error itself, not just the table.
        assert safe_adcp_error(ValueError("x")).suggestion == enum_meta["VALIDATION_ERROR"]["suggestion"]
        assert safe_adcp_error(PermissionError("x")).suggestion == enum_meta["AUTH_REQUIRED"]["suggestion"]

    def test_scrubbed_message_and_suggestion_match_semantic_category(self):
        """A scrubbed error's MESSAGE and SUGGESTION match its wire code — the machine field and
        the human guidance must not contradict. A VALIDATION_ERROR must not read "internal error",
        and an AUTH_REQUIRED must say authenticate/credentials, not "adjust the request". All are
        static and secret-free."""
        from src.core.exceptions import safe_adcp_error

        # VALIDATION_ERROR (raw ValueError): about the submitted fields, not an internal error.
        v = safe_adcp_error(ValueError(SECRET_BEARING_MESSAGE))
        assert v.wire_error_code == "VALIDATION_ERROR"
        assert "internal error" not in v.message.lower()
        assert "validate" in v.message.lower()
        assert "retry" not in v.suggestion.lower()
        assert "field" in v.suggestion.lower()

        # AUTH_REQUIRED (raw PermissionError): about credentials/permissions, not field correction.
        a = safe_adcp_error(PermissionError(SECRET_BEARING_MESSAGE))
        assert a.wire_error_code == "AUTH_REQUIRED"
        assert "internal error" not in a.message.lower()
        assert any(w in a.message.lower() for w in ("authenticat", "authoriz", "credential"))
        assert any(w in a.suggestion.lower() for w in ("credential", "permission"))

        # SERVICE_UNAVAILABLE (untyped internal): the generic internal message + retry IS correct.
        s = safe_adcp_error(RuntimeError(SECRET_BEARING_MESSAGE))
        assert s.wire_error_code == "SERVICE_UNAVAILABLE"
        assert "internal error" in s.message.lower()
        assert "retry" in s.suggestion.lower()

        for e in (v, a, s):
            assert_no_secret_leak(e.message, context=f"{e.wire_error_code} message")
            assert_no_secret_leak(e.suggestion, context=f"{e.wire_error_code} suggestion")

    def test_typed_and_raw_validation_scrubs_share_one_category_text(self):
        """Both scrub paths select sanitized text from ONE table.

        ``safe_adcp_error`` reaches the scrub two ways: the ``synthesize``-based
        ``_scrubbed_error`` (raw built-in) and the subclass-preserving ``type(exc)(...)``
        branch (untrusted ``AdCPValidationError``). The second cannot delegate to the first,
        so the selection lived at both sites — and the same wire code could yield different
        buyer text depending on the exception's provenance. Pin that they agree.
        """
        from src.core.exceptions import _SANITIZED_BY_WIRE_CODE, AdCPValidationError, safe_adcp_error

        expected_message, expected_suggestion = _SANITIZED_BY_WIRE_CODE["VALIDATION_ERROR"]

        typed = safe_adcp_error(AdCPValidationError(SECRET_BEARING_MESSAGE))
        raw = safe_adcp_error(ValueError(SECRET_BEARING_MESSAGE))

        assert typed.wire_error_code == raw.wire_error_code == "VALIDATION_ERROR"
        assert typed.message == expected_message == raw.message
        assert typed.suggestion == expected_suggestion == raw.suggestion
        # The typed branch exists to preserve the subclass; losing that would silently
        # change the raised type even though the text now matches.
        assert type(typed) is AdCPValidationError

        for e in (typed, raw):
            assert_no_secret_leak(e.message, context=f"{type(e).__name__} scrubbed message")
            assert_no_secret_leak(e.suggestion, context=f"{type(e).__name__} scrubbed suggestion")

    def test_sanitized_category_registry_covers_all_correctable_builtin_targets(self):
        """Completeness guard reconciling ``_BUILTIN_NORMALIZATION`` with ``_SANITIZED_BY_WIRE_CODE``.

        Production instantiates each entry's ``adcp_class`` directly; the kwargs projector cannot
        select another semantic class. The guard reads that same single authority. Every correctable
        standard target must have a category entry; internal/non-standard targets are exempt.
        """
        from src.core.exceptions import (
            _BUILTIN_NORMALIZATION,
            _SANITIZED_BY_WIRE_CODE,
            INTERNAL_WIRE_CODES,
            WIRE_STANDARD_CODES,
        )

        uncovered = _uncovered_correctable_targets(
            _BUILTIN_NORMALIZATION, _SANITIZED_BY_WIRE_CODE, INTERNAL_WIRE_CODES, WIRE_STANDARD_CODES
        )
        assert uncovered == [], (
            f"raw-exception normalizers whose client-correctable target lacks a "
            f"_SANITIZED_BY_WIRE_CODE entry (a scrubbed built-in would read 'An internal error "
            f"occurred'): {uncovered}. Add a category (message, suggestion) for each code."
        )

    def test_uncovered_detector_flags_special_projector_target(self):
        """Known-bad: a custom structured projector targeting an uncovered class is flagged.

        Projectors return constructor kwargs, not errors, so even input-dependent projector logic
        cannot vary the semantic target behind the guard's back.
        """
        from src.core.exceptions import (
            _SANITIZED_BY_WIRE_CODE,
            INTERNAL_WIRE_CODES,
            WIRE_STANDARD_CODES,
            AdCPNotFoundError,
            AdCPValidationError,
        )

        def _special_kwargs(exc):
            return {"message": "resource missing", "field": "id", "details": {"probe": str(exc)}}

        registry_without_invalid_request = dict(_SANITIZED_BY_WIRE_CODE)
        registry_without_invalid_request.pop("INVALID_REQUEST")
        known_bad_registry = (
            (KeyError, AdCPNotFoundError, _special_kwargs),
            (LookupError, AdCPValidationError, _special_kwargs),  # covered control
        )

        uncovered = _uncovered_correctable_targets(
            known_bad_registry, registry_without_invalid_request, INTERNAL_WIRE_CODES, WIRE_STANDARD_CODES
        )
        assert ("AdCPNotFoundError", "INVALID_REQUEST") in uncovered, (
            "detector must flag a special projector whose target is an uncovered correctable code"
        )
        assert all(code != "VALIDATION_ERROR" for _name, code in uncovered), (
            "a factory returning a covered code must NOT be flagged"
        )

    def test_normalization_projector_cannot_override_semantic_identity(self):
        """Known-bad: a projector emitting semantic overrides is caught at import time.

        ``AdCPError`` constructors accept ``error_code``/``status_code``/``recovery`` overrides.
        Without the allowlist gate, a projector could declare ``AdCPValidationError`` to the
        completeness guard but emit ``INVALID_REQUEST`` at runtime via ``error_code='NOT_FOUND'``.
        Enforcement moved OUT of the hot exception path (a raise inside a boundary translator
        shadows the original exception and fails open with no envelope) to a module-import gate;
        this drives the gate's shared detector with known-bad registries so a degraded matcher
        reddens here.
        """
        from src.core.exceptions import (
            AdCPValidationError,
            _build_normalized_error,
            _projector_key_violations,
        )

        bad_error_code = ((ValueError, AdCPValidationError, lambda e: {"message": str(e), "error_code": "NOT_FOUND"}),)
        findings = _projector_key_violations(bad_error_code)
        assert findings and "error_code" in findings[0], findings

        bad_context = (
            (
                ValueError,
                AdCPValidationError,
                lambda e: {"message": str(e), "context": {"debug": "postgresql://svc:TOPSECRET@db/prod"}},
            ),
        )
        findings = _projector_key_violations(bad_context)
        assert findings and "context" in findings[0], findings

        # The shipped registry passes the gate (the module imported), and presentation-only
        # kwargs construct the fixed semantic class.
        assert _projector_key_violations(()) == []
        built = _build_normalized_error(
            AdCPValidationError,
            {"message": "bad field", "field": "packages[0].budget", "details": {"safe": True}},
        )
        assert built.wire_error_code == "VALIDATION_ERROR"
        assert built.field == "packages[0].budget"
        assert built.details == {"safe": True}


# ---------------------------------------------------------------------------
# A2A Boundary: AdCPError → A2AError with proper JSON-RPC error code
# ---------------------------------------------------------------------------


class TestA2AHandlerExplicitSkillReraises:
    """``_handle_explicit_skill`` re-raises a WIRE-SAFE error (provenance decided at the seam).

    This class verifies the handler-internal contract: the skill dispatcher catches the
    exception, audits, and re-raises via ``safe_adcp_error`` so the OUTER ``on_message_send``
    boundary wraps a failure that is ALREADY sanitized:

    - CLIENT-CORRECTABLE typed errors (``AdCPValidationError``, ``AdCPNotFoundError``, …) re-raise
      VERBATIM — their controlled buyer-facing message is preserved.
    - INTERNAL typed errors (``AdCPAdapterError``/``AdCPServiceUnavailableError`` → SERVICE_UNAVAILABLE,
      terminal ``CONFIGURATION_ERROR``) are SCRUBBED at the seam — re-raised as a wire-safe base
      ``AdCPError`` with a generic message (a raise site can interpolate a secret).
    - RAW built-ins (``ValueError``/``PermissionError``) keep their SEMANTIC code (VALIDATION_ERROR /
      AUTH_REQUIRED — the same the synchronous boundaries emit) but their untrusted ``str(e)`` is
      scrubbed. Re-raising the *normalized typed* error here instead would hand the outer sanitizer
      a trusted ``AdCPValidationError`` and let the secret survive.

    Wire-envelope coverage for the A2A boundary lives in
    ``tests/integration/test_a2a_error_responses.py`` and the public failed-Task tests in
    ``tests/unit/test_a2a_error_routing.py``.
    """

    @pytest.mark.asyncio
    async def test_adcp_validation_propagates_for_dispatcher_wrap(self):
        """AdCPValidationError propagates verbatim; dispatcher will build envelope."""
        from src.a2a_server.adcp_a2a_server import AdCPRequestHandler

        handler = AdCPRequestHandler()

        async def mock_skill(params, token):
            raise AdCPValidationError("invalid param", _wire_safe_message=True)

        with patch.object(handler, "_handle_get_products_skill", mock_skill):
            with pytest.raises(AdCPValidationError) as exc_info:
                await handler._handle_explicit_skill("get_products", {}, "token")

            assert "invalid param" in exc_info.value.message
            assert exc_info.value.error_code == "VALIDATION_ERROR"
            assert exc_info.value.recovery == "correctable"

    @pytest.mark.asyncio
    async def test_adcp_adapter_scrubbed_at_seam(self):
        """AdCPAdapterError (internal → SERVICE_UNAVAILABLE) is scrubbed at the seam.

        Its raise sites do ``AdCPAdapterError(f"...: {e}")`` where ``e`` can bear a connection
        string, so the seam must not re-raise the raw message. Recovery stays transient (the
        code's canonical value)."""
        from src.a2a_server.adcp_a2a_server import AdCPRequestHandler
        from src.core.exceptions import AdCPError

        handler = AdCPRequestHandler()

        async def mock_skill(params, token):
            raise AdCPAdapterError(f"GAM down: {SECRET_BEARING_MESSAGE}")

        with patch.object(handler, "_handle_get_products_skill", mock_skill):
            with pytest.raises(AdCPError) as exc_info:
                await handler._handle_explicit_skill("get_products", {}, "token")

            assert_no_secret_leak(exc_info.value.message, context="seam-scrubbed message")
            assert "GAM down" not in exc_info.value.message
            assert exc_info.value.wire_error_code == "SERVICE_UNAVAILABLE"
            assert exc_info.value.recovery == "transient"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("exc_factory", "expected_code"),
        [
            pytest.param(lambda s: ValueError(s), "VALIDATION_ERROR", id="ValueError"),
            pytest.param(lambda s: PermissionError(s), "AUTH_REQUIRED", id="PermissionError"),
        ],
    )
    async def test_raw_builtin_keeps_semantic_code_but_scrubs_message(self, exc_factory, expected_code):
        """A raw ``ValueError``/``PermissionError`` raised in a skill keeps the SEMANTIC code the
        synchronous boundaries emit (VALIDATION_ERROR / AUTH_REQUIRED) but has its untrusted
        ``str(e)`` scrubbed — the seam must not re-raise a normalized typed error carrying the
        secret."""
        from src.a2a_server.adcp_a2a_server import AdCPRequestHandler
        from src.core.exceptions import AdCPError

        handler = AdCPRequestHandler()
        secret = SECRET_BEARING_MESSAGE

        async def mock_skill(params, token):
            raise exc_factory(secret)

        with patch.object(handler, "_handle_get_products_skill", mock_skill):
            with pytest.raises(AdCPError) as exc_info:
                await handler._handle_explicit_skill("get_products", {}, "token")

            assert exc_info.value.wire_error_code == expected_code
            assert_no_secret_leak(exc_info.value.message, context="seam-scrubbed builtin message")

    @pytest.mark.asyncio
    async def test_server_error_still_passes_through(self):
        """Existing A2AError behavior preserved — re-raised unchanged."""
        from a2a.types import MethodNotFoundError

        from src.a2a_server.adcp_a2a_server import AdCPRequestHandler

        handler = AdCPRequestHandler()

        async def mock_skill(params, token):
            raise MethodNotFoundError(message="not found")

        with patch.object(handler, "_handle_get_products_skill", mock_skill):
            with pytest.raises(MethodNotFoundError) as exc_info:
                await handler._handle_explicit_skill("get_products", {}, "token")

            # a2a-sdk 1.0: MethodNotFoundError is an A2AError subclass, re-raised as-is
            assert exc_info.value.message == "not found"


class TestA2ADispatcherFailedSkillResult:
    """``_build_failed_skill_result`` emits a spec-compliant envelope for every exception.

    Both the AdCPError branch and the untyped-Exception fallthrough in the
    explicit-skill dispatcher land here, so the artifact DataPart always
    carries the two-layer envelope shape — never a flat ``{error: ...}`` dict.
    Storyboard runners depend on ``adcp_error.code`` and ``errors[0].code``
    being readable from any failure path.
    """

    def test_adcp_error_keeps_typed_code(self):
        """AdCPError instances flow through unchanged — typed code preserved."""
        from src.a2a_server.adcp_a2a_server import AdCPRequestHandler

        result = AdCPRequestHandler._build_failed_skill_result(
            "get_products",
            AdCPValidationError("bad input", _wire_safe_message=True),
        )

        assert result["success"] is False
        assert result["skill"] == "get_products"
        env = result["error_envelope"]
        assert env["errors"][0]["message"] == "bad input"
        assert env["adcp_error"]["code"] == "VALIDATION_ERROR"
        assert env["errors"][0]["code"] == "VALIDATION_ERROR"
        assert env["errors"][0]["recovery"] == "correctable"

    def test_untyped_exception_wrapped_in_sanitized_adcp_error(self):
        """Bare ``Exception`` is wrapped in a SANITIZED synthetic AdCPError.

        Per the A2A boundary security policy (``safe_adcp_error``), an untyped
        exception must NOT expose its raw ``str(exc)`` — which may carry credentials,
        connection strings, SQL, or hostnames. The message is replaced with a generic
        internal error, and the wire code is the safe ``SERVICE_UNAVAILABLE``
        (``AdCPError`` defaults to ``INTERNAL_ERROR`` → translated by ``wire_error_code``).
        """
        from src.a2a_server.adcp_a2a_server import AdCPRequestHandler

        result = AdCPRequestHandler._build_failed_skill_result(
            "get_products", RuntimeError(f"{SECRET_BEARING_MESSAGE} unexpected boom")
        )

        assert result["success"] is False
        env = result["error_envelope"]
        # Wire code is translated via ERROR_CODE_MAPPING
        assert env["adcp_error"]["code"] == "SERVICE_UNAVAILABLE"
        assert env["errors"][0]["code"] == "SERVICE_UNAVAILABLE"
        # The raw exception text is NEVER on the wire — only a generic internal message.
        assert_no_secret_leak(env, context="full failed-skill envelope")
        message = env["errors"][0]["message"]
        assert "unexpected boom" not in message
        assert "internal error" in message.lower()

    def test_untyped_exception_with_empty_message_still_sanitized(self):
        """An untyped exception with no string content still yields the generic sanitized
        message — never an empty ``message`` (spec requires non-empty) and never the raw
        exception class name (which could itself hint at internals).
        """
        from src.a2a_server.adcp_a2a_server import AdCPRequestHandler

        result = AdCPRequestHandler._build_failed_skill_result("get_products", RuntimeError())

        env = result["error_envelope"]
        message = env["errors"][0]["message"]
        assert message, "wire envelope message must be non-empty"
        assert message != "RuntimeError", "must not fall back to the exception class name"
        assert "internal error" in message.lower()

    def test_internal_bucket_typed_error_message_is_scrubbed(self):
        """A TYPED internal/infra error (SERVICE_UNAVAILABLE bucket) that interpolated a
        secret into its message is scrubbed at the boundary — code + recovery preserved.

        Reachable handlers build e.g. ``AdCPAdapterError(f"...: {e}")`` where ``e`` carries a
        DB connection string. Because it is already an ``AdCPError`` a naive sanitizer would
        trust it; ``safe_adcp_error`` instead replaces the message for the
        ``wire_error_code == "SERVICE_UNAVAILABLE"`` bucket while keeping the wire code and the
        buyer-facing retry semantics (``recovery``).
        """
        from src.a2a_server.adcp_a2a_server import AdCPRequestHandler

        secret = SECRET_BEARING_MESSAGE
        result = AdCPRequestHandler._build_failed_skill_result(
            "create_media_buy", AdCPAdapterError(f"Failed to create media buy: {secret}")
        )

        env = result["error_envelope"]
        blob = json.dumps(env)
        assert_no_secret_leak(blob)
        # Wire code preserved (adapter → SERVICE_UNAVAILABLE); recovery preserved (transient).
        assert env["adcp_error"]["code"] == "SERVICE_UNAVAILABLE"
        assert env["errors"][0]["code"] == "SERVICE_UNAVAILABLE"
        assert env["errors"][0]["recovery"] == "transient"
        assert "internal error" in env["errors"][0]["message"].lower()

    def test_internal_error_for_scrubs_full_jsonrpc_wire(self):
        """A typed internal-bucket error must not leak through the
        JSON-RPC ``error.message`` even while ``error.data`` is scrubbed.

        ``_internal_error_for`` previously built the top-level JSON-RPC message from the
        ORIGINAL ``exc.message`` — so an ``AdCPAdapterError(f"...{e}")`` carrying a DB URL
        had a sanitized envelope in ``error.data`` while the URL stayed visible in
        ``error.message``. Both layers of the FULL wire object must be clean. (The
        REACHABLE-handler proof — that a real push-config handler produces this scrubbed
        InternalError — is ``test_push_config_handler_scrubs_secret_on_full_wire`` below.)
        """
        from src.a2a_server.adcp_a2a_server import _internal_error_for

        secret = SECRET_BEARING_MESSAGE
        err = _internal_error_for(
            "set_task_push_notification_config", AdCPAdapterError(f"Failed to store config: {secret}")
        )

        full_wire = json.dumps({"message": err.message, "data": err.data})
        assert_no_secret_leak(full_wire)
        # Wire code + recovery still accurate in the envelope.
        assert err.data["adcp_error"]["code"] == "SERVICE_UNAVAILABLE"
        assert err.data["errors"][0]["recovery"] == "transient"

    @pytest.mark.asyncio
    async def test_push_config_handler_scrubs_secret_on_full_wire(self):
        """Reachable-path and serialized-wire proof: a real push-config
        handler whose backing store raises a secret-bearing internal error produces a
        JSON-RPC error whose full serialized wire (``{jsonrpc, id, error:{code,message,data}}``)
        leaks nothing.

        Drives the actual ``on_get_task_push_notification_config`` handler (one of the four
        push-config methods that surface through ``_internal_error_for``), captures the REAL
        raised ``InternalError``, and serializes it through the a2a SDK's own
        ``build_error_response`` — the exact function ``JsonRpcDispatcher._generate_error_response``
        uses to produce the wire — so the assertion is on the actual serialized JSON-RPC
        output, not a hand-built ``{message, data}`` dict.
        """
        from types import SimpleNamespace

        from a2a.server.request_handlers.response_helpers import build_error_response
        from a2a.types import InternalError

        import src.a2a_server.adcp_a2a_server as a2a_mod
        from src.a2a_server.adcp_a2a_server import AdCPRequestHandler

        secret = SECRET_BEARING_MESSAGE

        handler = AdCPRequestHandler()
        handler._get_auth_token = lambda context: "tok"  # noqa: ARG005
        # Identity object is irrelevant — _make_tool_context is stubbed to the scope the
        # handler actually reads. This keeps the test at unit altitude (no DB).
        handler._resolve_a2a_identity = lambda *a, **k: object()
        handler._make_tool_context = lambda *a, **k: SimpleNamespace(tenant_id="t", principal_id="p")

        class _BoomUoW:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                raise AdCPAdapterError(f"Config store unavailable: {secret}")

            def __exit__(self, *a):
                return False

        with patch.object(a2a_mod, "PushNotificationConfigUoW", _BoomUoW):
            with pytest.raises(InternalError) as exc_info:
                await handler.on_get_task_push_notification_config({"id": "cfg_1"}, context=None)

        err = exc_info.value
        # Serialize through the SDK's real JSON-RPC error builder (the dispatcher's path).
        wire_dict = build_error_response("req-1", err)
        serialized = serialize_wire_error(wire_dict)
        assert_no_secret_leak(serialized)
        assert wire_dict["error"]["data"]["adcp_error"]["code"] == "SERVICE_UNAVAILABLE"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("method_name", ["on_get_task", "on_cancel_task"])
    async def test_durable_task_handlers_scrub_secret_on_full_wire(self, method_name):
        """A DB failure inside tasks/get or tasks/cancel must surface as a sanitized
        JSON-RPC InternalError, never the raw exception text. These two handlers gained
        durable DB access without the boundary arm their siblings have — an untyped
        escape reached the SDK dispatcher, which echoes ``str(exc)`` verbatim on the
        wire. Serializes through the SDK's own ``build_error_response`` (the dispatcher's
        path) and asserts the full wire via the shared leak oracle."""
        from types import SimpleNamespace

        from a2a.server.request_handlers.response_helpers import build_error_response
        from a2a.types import GetTaskRequest, InternalError

        from src.a2a_server.adcp_a2a_server import AdCPRequestHandler

        handler = AdCPRequestHandler()
        handler._get_auth_token = lambda context: "tok"  # noqa: ARG005
        handler._resolve_a2a_identity = lambda *a, **k: SimpleNamespace(tenant_id="t", principal_id="p")

        def _boom_db():
            raise RuntimeError(SECRET_BEARING_MESSAGE)

        # No owned in-memory task, so both handlers fall through to the durable
        # (DB-touching) path, which is where the untyped failure fires.
        with patch("src.core.database.database_session.get_db_session", _boom_db):
            with pytest.raises(InternalError) as exc_info:
                await getattr(handler, method_name)(GetTaskRequest(id="task_x"), context=None)

        err = exc_info.value
        wire_dict = build_error_response("req-1", err)
        serialized = serialize_wire_error(wire_dict)
        assert_no_secret_leak(serialized, context=f"{method_name} JSON-RPC wire")
        assert wire_dict["error"]["data"]["adcp_error"]["code"] == "SERVICE_UNAVAILABLE"

    def test_internal_error_for_preserves_correctable_message(self):
        """The JSON-RPC message keeps the controlled text of a
        client-correctable typed error — the fix must not over-sanitize."""
        from src.a2a_server.adcp_a2a_server import _internal_error_for

        err = _internal_error_for(
            "set_task_push_notification_config",
            AdCPValidationError("url is required", _wire_safe_message=True),
        )
        assert "url is required" in err.message

    def test_client_correctable_typed_error_message_is_preserved(self):
        """A client-correctable typed error keeps its controlled message — the boundary must
        NOT over-sanitize. Buyers need the specific guidance to fix their request.
        """
        from src.a2a_server.adcp_a2a_server import AdCPRequestHandler

        result = AdCPRequestHandler._build_failed_skill_result(
            "get_products",
            AdCPValidationError("brief must not be empty", _wire_safe_message=True),
        )

        env = result["error_envelope"]
        assert env["errors"][0]["code"] == "VALIDATION_ERROR"
        assert env["errors"][0]["recovery"] == "correctable"
        assert env["errors"][0]["message"] == "brief must not be empty"

    def test_envelope_shape_matches_typed_branch(self):
        """Untyped fallthrough produces the SAME envelope shape as the typed branch.

        Storyboard runners must be able to parse the DataPart uniformly
        regardless of which catch branch produced the failure result. The
        set-equality on key names alone is not enough — values must also be
        type-equivalent (e.g., ``recovery`` is a string in both branches,
        not ``None`` in one and a value in the other) so a regression that
        nulls one branch's recovery is caught here.
        """
        from src.a2a_server.adcp_a2a_server import AdCPRequestHandler

        # Conformant raise sites carry a top-level suggestion; the untyped
        # branch synthesizes one, so the typed sample must too for shape parity.
        typed = AdCPRequestHandler._build_failed_skill_result(
            "s", AdCPValidationError("bad", suggestion="Correct the request and resend.")
        )
        untyped = AdCPRequestHandler._build_failed_skill_result("s", RuntimeError("boom"))

        assert set(typed.keys()) == set(untyped.keys())
        assert set(typed["error_envelope"].keys()) == set(untyped["error_envelope"].keys())

        typed_envelope = typed["error_envelope"]
        untyped_envelope = untyped["error_envelope"]
        assert set(typed_envelope["errors"][0].keys()) == set(untyped_envelope["errors"][0].keys())

        # Field-value pin: both branches must populate the same keys with
        # non-None values for the contract storyboard runners depend on.
        typed_adcp_error = typed_envelope["adcp_error"]
        untyped_adcp_error = untyped_envelope["adcp_error"]
        assert typed_adcp_error["code"] and untyped_adcp_error["code"], (
            f"Both branches must populate adcp_error.code; "
            f"typed={typed_adcp_error.get('code')!r}, untyped={untyped_adcp_error.get('code')!r}"
        )
        assert typed_adcp_error.get("recovery") and untyped_adcp_error.get("recovery"), (
            f"Both branches must populate adcp_error.recovery; "
            f"typed={typed_adcp_error.get('recovery')!r}, untyped={untyped_adcp_error.get('recovery')!r}"
        )

        typed_err0 = typed_envelope["errors"][0]
        untyped_err0 = untyped_envelope["errors"][0]
        assert typed_err0["code"] and untyped_err0["code"], (
            f"Both branches must populate errors[0].code; "
            f"typed={typed_err0.get('code')!r}, untyped={untyped_err0.get('code')!r}"
        )
        assert typed_err0["message"] and untyped_err0["message"], (
            f"Both branches must populate errors[0].message; "
            f"typed={typed_err0.get('message')!r}, untyped={untyped_err0.get('message')!r}"
        )


# ---------------------------------------------------------------------------
# REST Boundary: AdCPError → HTTP status code via exception handler
# ---------------------------------------------------------------------------


class TestRESTBoundaryAdCPErrorTranslation:
    """REST endpoints propagate AdCPError to the app-level exception handler with recovery."""

    def test_auth_error_observability_ignores_client_selected_tenant_scope(self):
        """REST auth rejection stays unscoped and performs no second identity lookup."""
        from starlette.requests import Request

        from src.app import _envelope_response
        from src.core.exceptions import AdCPAuthenticationError

        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/api/v1/capabilities",
                "headers": [(b"x-adcp-tenant", b"tenant-rest")],
            }
        )
        error = AdCPAuthenticationError("rejected")

        with (
            patch("src.app._best_effort_rest_identity") as mock_identity,
            patch("src.app.record_boundary_error") as mock_record,
        ):
            response = _envelope_response(request, error, original=error)

        mock_identity.assert_not_called()
        mock_record.assert_called_once_with(
            "rest",
            "/api/v1/capabilities",
            error,
            tenant_id=None,
            principal_id=None,
        )
        assert response.status_code == error.status_code

    def test_anonymous_validation_error_cannot_write_to_header_selected_tenant(self):
        """Pre-auth validation failures remain unscoped despite tenant hints."""
        from starlette.requests import Request

        from src.app import _envelope_response

        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/api/v1/media-buys",
                "headers": [(b"x-adcp-tenant", b"victim-tenant")],
            }
        )
        error = AdCPValidationError("malformed request")
        anonymous = SimpleNamespace(tenant_id="victim-tenant", principal_id=None)

        with (
            patch("src.app.resolve_identity", return_value=anonymous),
            patch("src.app.record_boundary_error") as mock_record,
        ):
            response = _envelope_response(request, error, original=error)

        mock_record.assert_called_once_with(
            "rest",
            "/api/v1/media-buys",
            error,
            tenant_id=None,
            principal_id=None,
        )
        assert response.status_code == error.status_code

    def test_boundary_log_receives_the_original_not_the_sanitized_twin(self):
        """The privileged server log must see the exception as raised.

        ``record_boundary_error`` picks log severity from ``isinstance(error, AdCPError)``:
        an untyped crash gets ERROR + ``exc_info=True``, a typed error gets WARNING. Handing
        it ``safe_adcp_error(exc)`` made that test always true, so a genuine REST crash
        logged at WARNING with the scrubbed message and no traceback — while MCP and A2A,
        which pass the original, kept theirs.

        Identity (``is``) is the load-bearing check: an equality assertion would also pass
        on a re-derived sanitized twin, which is exactly the bug.
        """
        import json as _json

        from starlette.requests import Request

        from src.app import _envelope_response
        from src.core.exceptions import safe_adcp_error

        request = Request({"type": "http", "method": "GET", "path": "/api/v1/capabilities", "headers": []})
        original = RuntimeError(SECRET_BEARING_MESSAGE)
        wire_error = safe_adcp_error(original)

        with (
            patch("src.app._best_effort_rest_identity", return_value=(None, None)),
            patch("src.app.record_boundary_error") as mock_record,
        ):
            response = _envelope_response(request, wire_error, original=original)

        assert mock_record.call_args.args[2] is original
        # The wire still carries the sanitized error, never the original's text.
        assert response.status_code == wire_error.status_code
        assert_no_secret_leak(_json.loads(response.body), context="REST envelope body")

    def test_adcp_validation_from_impl_returns_400(self):
        """AdCPValidationError raised in _impl → REST returns 400 with correctable recovery."""
        from starlette.testclient import TestClient

        from src.app import app

        with patch(
            "src.core.tools.capabilities.get_adcp_capabilities_raw",
            side_effect=AdCPValidationError("invalid request", _wire_safe_message=True),
        ):
            client = TestClient(app, raise_server_exceptions=False)
            response = client.get("/api/v1/capabilities")
            assert response.status_code == 400
            assert_envelope_shape(
                response.json(), "VALIDATION_ERROR", recovery="correctable", message_substr="invalid request"
            )

    def test_untyped_impl_failure_returns_sanitized_two_layer_envelope(self):
        """Unexpected REST crashes stay typed without exposing raw exception text."""
        from starlette.testclient import TestClient

        from src.app import app

        with patch(
            "src.core.tools.capabilities.get_adcp_capabilities_raw",
            side_effect=RuntimeError(SECRET_BEARING_MESSAGE),
        ):
            client = TestClient(app, raise_server_exceptions=False)
            response = client.get("/api/v1/capabilities")

        assert response.status_code == 500
        assert_envelope_shape(response.json(), "SERVICE_UNAVAILABLE", recovery="transient")
        assert_no_secret_leak(response.json(), context="REST untyped failure envelope")

    def test_adcp_not_found_from_impl_returns_404(self):
        """AdCPNotFoundError raised in _impl → REST returns 404 with correctable recovery.

        Recovery matches the pinned enumMetadata of the WIRE code:
        INVALID_REQUEST=correctable.
        """
        from starlette.testclient import TestClient

        from src.app import app

        with patch(
            "src.core.tools.capabilities.get_adcp_capabilities_raw",
            side_effect=AdCPNotFoundError("resource not found"),
        ):
            client = TestClient(app, raise_server_exceptions=False)
            response = client.get("/api/v1/capabilities")
            assert response.status_code == 404
            # AdCPNotFoundError's NOT_FOUND is INTERNAL_CODES; envelope translates
            # to INVALID_REQUEST so the wire code stays in STANDARD_ERROR_CODES.
            assert_envelope_shape(response.json(), "INVALID_REQUEST", recovery="correctable")

    def test_adcp_adapter_from_impl_returns_502(self):
        """AdCPAdapterError raised in _impl → REST returns 502 with transient recovery."""
        from starlette.testclient import TestClient

        from src.app import app

        with patch(
            "src.core.tools.capabilities.get_adcp_capabilities_raw",
            side_effect=AdCPAdapterError("GAM unavailable"),
        ):
            client = TestClient(app, raise_server_exceptions=False)
            response = client.get("/api/v1/capabilities")
            assert response.status_code == 502
            assert_envelope_shape(response.json(), "SERVICE_UNAVAILABLE", recovery="transient")

    def test_adcp_conflict_from_impl_returns_409(self):
        """AdCPConflictError raised in _impl → REST returns 409 with transient recovery."""
        from starlette.testclient import TestClient

        from src.app import app
        from src.core.exceptions import AdCPConflictError

        with patch(
            "src.core.tools.capabilities.get_adcp_capabilities_raw",
            side_effect=AdCPConflictError("duplicate key"),
        ):
            client = TestClient(app, raise_server_exceptions=False)
            response = client.get("/api/v1/capabilities")
            assert response.status_code == 409
            # CONFLICT recovery is transient per the pinned enum (#1417).
            assert_envelope_shape(response.json(), "CONFLICT", recovery="transient")

    def test_adcp_service_unavailable_from_impl_returns_503(self):
        """AdCPServiceUnavailableError raised in _impl → REST returns 503 with transient recovery."""
        from starlette.testclient import TestClient

        from src.app import app
        from src.core.exceptions import AdCPServiceUnavailableError

        with patch(
            "src.core.tools.capabilities.get_adcp_capabilities_raw",
            side_effect=AdCPServiceUnavailableError("product unavailable"),
        ):
            client = TestClient(app, raise_server_exceptions=False)
            response = client.get("/api/v1/capabilities")
            assert response.status_code == 503
            assert_envelope_shape(response.json(), "SERVICE_UNAVAILABLE", recovery="transient")


class TestGlobalToolErrorHandler:
    """Global ``@app.exception_handler(ToolError)`` translates ToolError to envelope.

    Removes the need for every REST route to wrap its body in
    ``try/except ToolError`` — the global handler catches both plain ``ToolError``
    and ``AdCPToolError`` (subclass) and produces the same envelope shape as
    the per-route ``handle_tool_error`` did. Verifies the wiring works
    end-to-end through the REST stack.
    """

    def test_adcp_tool_error_through_global_handler_preserves_status(self):
        """AdCPToolError from _impl is caught by the global handler with original status_code."""
        from starlette.testclient import TestClient

        from src.app import app
        from src.core.exceptions import AdCPMediaBuyNotFoundError, build_two_layer_error_envelope
        from src.core.tool_error_logging import AdCPToolError

        source = AdCPMediaBuyNotFoundError("buy_x missing")
        tool_error = AdCPToolError(build_two_layer_error_envelope(source), status_code=source.status_code)

        with patch(
            "src.core.tools.capabilities.get_adcp_capabilities_raw",
            side_effect=tool_error,
        ):
            client = TestClient(app, raise_server_exceptions=False)
            response = client.get("/api/v1/capabilities")
            assert response.status_code == 404
            assert_envelope_shape(response.json(), "MEDIA_BUY_NOT_FOUND", recovery="correctable")

    def test_plain_tool_error_with_known_code_through_global_handler(self):
        """Plain ToolError("VALIDATION_ERROR", "msg") → 400 via global handler + status map."""
        from fastmcp.exceptions import ToolError
        from starlette.testclient import TestClient

        from src.app import app

        with patch(
            "src.core.tools.capabilities.get_adcp_capabilities_raw",
            side_effect=ToolError("VALIDATION_ERROR", "missing field"),
        ):
            client = TestClient(app, raise_server_exceptions=False)
            response = client.get("/api/v1/capabilities")
            assert response.status_code == 400


class TestRESTSymmetricValueErrorAndPermissionError:
    """REST mirrors MCP/A2A by wrapping ValueError and PermissionError in envelopes.

    Without these handlers, a raw ``ValueError`` raised by application code
    would surface as a 500 server error on REST while the same exception
    produces a 400 VALIDATION_ERROR envelope on MCP and A2A. Cross-transport
    symmetry: every transport translates the same Python exception to the
    same wire shape.
    """

    @pytest.mark.parametrize(
        ("exception", "expected_code", "expected_status"),
        [
            pytest.param(ValueError(SECRET_BEARING_MESSAGE), "VALIDATION_ERROR", 400, id="value-error"),
            pytest.param(PermissionError(SECRET_BEARING_MESSAGE), "AUTH_REQUIRED", 403, id="permission-error"),
        ],
    )
    def test_raw_error_returns_scrubbed_envelope(self, exception, expected_code, expected_status):
        """Raw built-ins retain their semantic code without exposing their message."""
        from starlette.testclient import TestClient

        from src.app import app

        with patch(
            "src.core.tools.capabilities.get_adcp_capabilities_raw",
            side_effect=exception,
        ):
            client = TestClient(app, raise_server_exceptions=False)
            response = client.get("/api/v1/capabilities")
            assert response.status_code == expected_status
            assert_envelope_shape(
                response.json(),
                expected_code,
                recovery="correctable",
            )
            assert_no_secret_leak(response.json(), context=f"REST {type(exception).__name__} envelope")

    def test_request_validation_error_unaffected(self):
        """FastAPI's RequestValidationError handler is NOT overridden by our ValueError handler.

        ``RequestValidationError`` is not a ``ValueError`` subclass, so FastAPI's
        existing 422 + ``{"detail": [...]}`` response shape for request-body
        validation failures continues to work — only application-raised
        ``ValueError`` is wrapped into the AdCP envelope.
        """
        from fastapi.exceptions import RequestValidationError

        assert not issubclass(RequestValidationError, ValueError), (
            "RequestValidationError must not inherit ValueError, otherwise our "
            "ValueError handler would shadow FastAPI's request-body 422 handler."
        )


# ---------------------------------------------------------------------------
# REST defensive ToolError catch: handle_tool_error must preserve status_code
# ---------------------------------------------------------------------------


def _synthetic_tool_error(source):
    """Wrap an AdCPError as the AdCPToolError a REST route catches defensively."""
    from src.core.exceptions import build_two_layer_error_envelope
    from src.core.tool_error_logging import AdCPToolError

    return AdCPToolError(build_two_layer_error_envelope(source), status_code=source.status_code)


class TestHandleToolErrorPreservesStatusCode:
    """``handle_tool_error`` must use the source AdCPError's status_code.

    REST routes catch ``ToolError`` defensively (when downstream code is
    wrapped by ``with_error_logging`` and translates AdCPError → AdCPToolError).
    The wire HTTP status must reflect the original AdCPError's classification
    (400/401/403/404/422/etc.) — not the hardcoded 500 it used to default to,
    which caused 4xx errors to be mislabeled as 5xx on this defensive path.
    """

    @pytest.mark.parametrize(
        ("source_cls_name", "message", "expected_status"),
        [
            ("AdCPValidationError", "invalid request", 400),
            ("AdCPAuthenticationError", "token expired", 401),
            ("AdCPMediaBuyNotFoundError", "buy_x missing", 404),
            ("AdCPBudgetTooLowError", "below minimum", 422),
            ("AdCPAdapterError", "GAM unavailable", 502),
        ],
    )
    def test_preserves_source_status_code(self, source_cls_name, message, expected_status):
        """handle_tool_error uses the source AdCPError's status_code, not a hardcoded 500.

        A new typed subclass is one parametrize row, not one method.
        """
        import src.core.exceptions as exceptions_mod
        from src.core.tool_error_logging import handle_tool_error

        source = getattr(exceptions_mod, source_cls_name)(message)
        response = handle_tool_error(_synthetic_tool_error(source))
        assert response.status_code == expected_status

    def test_plain_tool_error_falls_back_to_500(self):
        """Plain ToolError defaults to a scrubbed 500 envelope."""
        from fastmcp.exceptions import ToolError

        from src.core.tool_error_logging import handle_tool_error

        response = handle_tool_error(ToolError(SECRET_BEARING_MESSAGE))
        assert response.status_code == 500
        assert_no_secret_leak(response.body)

    def test_plain_tool_error_with_known_code_uses_status_map(self):
        """Plain ToolError("VALIDATION_ERROR", "msg") → 400 via _ERROR_CODE_TO_STATUS.

        Legacy paths that construct ToolError directly (without going through
        AdCPToolError) used to land at 500 because ``AdCPError`` defaulted to
        500 and only ``error_code`` was overridden. The map ensures the HTTP
        status matches the wire code on this defensive fallback path.
        """
        from fastmcp.exceptions import ToolError

        from src.core.tool_error_logging import handle_tool_error

        response = handle_tool_error(ToolError("VALIDATION_ERROR", SECRET_BEARING_MESSAGE))
        assert response.status_code == 400
        assert_no_secret_leak(response.body)

    def test_plain_tool_error_with_auth_code_returns_403(self):
        """Plain ToolError("AUTH_REQUIRED", "msg") → 403 via _ERROR_CODE_TO_STATUS.

        AUTH_REQUIRED is declared by both AdCPAuthenticationError (401) and
        AdCPAuthorizationError (403). The auto-derived table picks the
        more restrictive status (403) since a plain-ToolError fallback
        carries no context to disambiguate. A prior hand-coded
        ``AUTH_REQUIRED → 401`` mapping conflicted with
        AdCPAuthorizationError.status_code=403.
        """
        from fastmcp.exceptions import ToolError

        from src.core.tool_error_logging import handle_tool_error

        response = handle_tool_error(ToolError("AUTH_REQUIRED", "missing token"))
        assert response.status_code == 403

    def test_plain_tool_error_with_not_found_code_returns_404(self):
        """Plain ToolError("MEDIA_BUY_NOT_FOUND", "msg") → 404 via _ERROR_CODE_TO_STATUS."""
        from fastmcp.exceptions import ToolError

        from src.core.tool_error_logging import handle_tool_error

        response = handle_tool_error(ToolError("MEDIA_BUY_NOT_FOUND", "buy_x missing"))
        assert response.status_code == 404

    def test_plain_tool_error_with_unknown_code_falls_back_to_500(self):
        """Plain ToolError with an unmapped wire code defaults to 500."""
        from fastmcp.exceptions import ToolError

        from src.core.tool_error_logging import handle_tool_error

        response = handle_tool_error(ToolError("WEIRD_LEGACY_CODE", "what is this"))
        assert response.status_code == 500

    @pytest.mark.parametrize(
        ("error_code", "contradictory_recovery", "expected_recovery"),
        [
            ("SERVICE_UNAVAILABLE", "terminal", "transient"),
            ("CONFIGURATION_ERROR", "transient", "terminal"),
        ],
    )
    def test_plain_tool_error_recovery_is_canonicalized(
        self,
        error_code,
        contradictory_recovery,
        expected_recovery,
    ):
        """An untyped positional hint cannot contradict standardized code metadata."""
        from fastmcp.exceptions import ToolError

        from src.core.tool_error_logging import handle_tool_error
        from tests.helpers import assert_envelope_shape

        response = handle_tool_error(ToolError(error_code, SECRET_BEARING_MESSAGE, contradictory_recovery))
        assert_envelope_shape(json.loads(response.body), error_code, recovery=expected_recovery)
        assert_no_secret_leak(response.body)


# ---------------------------------------------------------------------------
# to_dict() serialization: recovery field present and correct
# ---------------------------------------------------------------------------


class TestToDictRecoveryField:
    """AdCPError.to_dict() must include recovery in the serialized dict."""

    def test_to_dict_includes_recovery_for_all_subclasses(self):
        """Every AdCPError subclass produces recovery in to_dict() output."""
        from src.core.exceptions import (
            AdCPAdapterError,
            AdCPBudgetExhaustedError,
            AdCPConflictError,
            AdCPError,
            AdCPGoneError,
            AdCPNotFoundError,
            AdCPRateLimitError,
            AdCPServiceUnavailableError,
            AdCPValidationError,
        )

        cases = [
            # Recovery follows the wire code: base
            # AdCPError→SERVICE_UNAVAILABLE=transient,
            # AdCPNotFoundError→INVALID_REQUEST=correctable.
            (AdCPError("internal"), "transient"),
            (AdCPValidationError("bad field"), "correctable"),
            (AdCPNotFoundError("missing"), "correctable"),
            (AdCPConflictError("duplicate"), "transient"),
            (AdCPGoneError("expired"), "correctable"),
            (AdCPBudgetExhaustedError("no budget"), "terminal"),
            (AdCPRateLimitError("slow down"), "transient"),
            (AdCPAdapterError("GAM down"), "transient"),
            (AdCPServiceUnavailableError("unavailable"), "transient"),
        ]

        for exc, expected_recovery in cases:
            d = exc.to_dict()
            assert "recovery" in d, f"{type(exc).__name__}.to_dict() missing 'recovery' key"
            msg = f"{type(exc).__name__}.to_dict() recovery={d['recovery']!r}, expected {expected_recovery!r}"
            assert d["recovery"] == expected_recovery, msg

    def test_to_dict_custom_recovery_override(self):
        """Custom recovery= kwarg overrides class default in to_dict() output."""
        from src.core.exceptions import AdCPNotFoundError

        # Default is "correctable" (wire INVALID_REQUEST)
        default_exc = AdCPNotFoundError("gone")
        assert default_exc.to_dict()["recovery"] == "correctable"

        # Override to "terminal"
        overridden = AdCPNotFoundError("permanently gone", recovery="terminal")
        assert overridden.to_dict()["recovery"] == "terminal"

    def test_to_dict_roundtrip_preserves_all_fields(self):
        """Serialize to dict, reconstruct, verify recovery survives the roundtrip."""
        from src.core.exceptions import AdCPAdapterError

        original = AdCPAdapterError("GAM timeout", details={"retry_after": 30})
        d = original.to_dict()

        # Verify all fields present
        assert d == {
            "error_code": "SERVICE_UNAVAILABLE",
            "message": "GAM timeout",
            "recovery": "transient",
            "details": {"retry_after": 30},
        }


# ---------------------------------------------------------------------------
# Custom recovery override preservation through all boundaries
# ---------------------------------------------------------------------------


class TestCustomRecoveryOverrideMCPBoundary:
    """Custom recovery= override must propagate through MCP boundary (with_error_logging)."""

    def test_custom_recovery_propagates_through_mcp_boundary(self):
        """AdCPNotFoundError(recovery='transient') -> ToolError carries 'transient' not 'terminal'."""
        from fastmcp.exceptions import ToolError

        from src.core.exceptions import AdCPNotFoundError
        from src.core.tool_error_logging import with_error_logging

        def failing_tool():
            raise AdCPNotFoundError("temporarily missing", recovery="transient")

        wrapped = with_error_logging(failing_tool)

        with pytest.raises(ToolError) as exc_info:
            wrapped()

        # AdCPNotFoundError's NOT_FOUND code maps to INVALID_REQUEST at the wire
        # boundary so output is spec-compliant; custom recovery still propagates.
        assert_envelope_shape(
            exc_info.value,
            "INVALID_REQUEST",
            check_mcp_tool_error=True,
            recovery="transient",
            message_substr="temporarily missing",
        )

    def test_custom_recovery_in_extract_error_info(self):
        """extract_error_info returns overridden recovery, not class default."""
        from src.core.exceptions import AdCPValidationError
        from src.core.tool_error_logging import extract_error_info

        # Override correctable -> terminal
        exc = AdCPValidationError("fatal validation", recovery="terminal")
        code, message, recovery = extract_error_info(exc)
        assert code == "VALIDATION_ERROR"
        assert recovery == "terminal"  # Custom, not default "correctable"


class TestCustomRecoveryOverrideA2ABoundary:
    """Custom recovery= override must propagate through A2A boundary."""

    @pytest.mark.asyncio
    async def test_custom_recovery_propagates_through_a2a_boundary(self):
        """AdCPNotFoundError(recovery='transient') propagates with the override intact."""
        from src.a2a_server.adcp_a2a_server import AdCPRequestHandler
        from src.core.exceptions import AdCPNotFoundError

        handler = AdCPRequestHandler()

        async def mock_skill(params, token):
            raise AdCPNotFoundError("temporarily missing", recovery="transient")

        with patch.object(handler, "_handle_get_products_skill", mock_skill):
            with pytest.raises(AdCPNotFoundError) as exc_info:
                await handler._handle_explicit_skill("get_products", {}, "token")
            assert exc_info.value.recovery == "transient"


class TestInternalRecoveryCanonicalizationRESTBoundary:
    """Internal errors use the pinned recovery at the REST wire boundary."""

    def test_custom_internal_recovery_is_canonicalized_at_rest_boundary(self):
        """AdCPAdapterError(recovery='terminal') emits scrubbed canonical SERVICE_UNAVAILABLE."""
        from starlette.testclient import TestClient

        from src.app import app
        from src.core.exceptions import AdCPAdapterError

        with patch(
            "src.core.tools.capabilities.get_adcp_capabilities_raw",
            side_effect=AdCPAdapterError("permanent failure", recovery="terminal"),
        ):
            client = TestClient(app, raise_server_exceptions=False)
            response = client.get("/api/v1/capabilities")
            assert response.status_code == 502
            assert_envelope_shape(response.json(), "SERVICE_UNAVAILABLE", recovery="transient")
            assert "permanent failure" not in response.text


# ---------------------------------------------------------------------------
# Roundtrip: raise → catch at boundary → serialize → deserialize → check recovery
# ---------------------------------------------------------------------------


class TestRecoveryRoundtrip:
    """Full roundtrip through raise -> boundary catch -> serialize -> verify recovery."""

    def test_mcp_roundtrip_all_subclasses(self):
        """All 11 AdCPError subclasses: raise -> with_error_logging -> ToolError -> extract_error_info."""
        from src.core.exceptions import (
            AdCPAdapterError,
            AdCPBudgetExhaustedError,
            AdCPConflictError,
            AdCPError,
            AdCPGoneError,
            AdCPNotFoundError,
            AdCPRateLimitError,
            AdCPServiceUnavailableError,
            AdCPValidationError,
        )
        from src.core.tool_error_logging import extract_error_info, with_error_logging

        # AdCPError (INTERNAL_ERROR) and AdCPNotFoundError (NOT_FOUND) hold internal
        # codes; the boundary translates to STANDARD_ERROR_CODES (SERVICE_UNAVAILABLE
        # and INVALID_REQUEST respectively). Other subclasses already use STANDARD codes.
        cases = [
            # Recovery matches the pinned classification of the WIRE code
            #: SERVICE_UNAVAILABLE=transient, INVALID_REQUEST=correctable.
            (AdCPError, "internal", "SERVICE_UNAVAILABLE", "transient"),
            (AdCPValidationError, "bad", "VALIDATION_ERROR", "correctable"),
            (AdCPNotFoundError, "missing", "INVALID_REQUEST", "correctable"),
            (AdCPConflictError, "dup", "CONFLICT", "transient"),
            (AdCPGoneError, "expired", "INVALID_STATE", "correctable"),
            (AdCPBudgetExhaustedError, "broke", "BUDGET_EXHAUSTED", "terminal"),
            (AdCPRateLimitError, "slow", "RATE_LIMITED", "transient"),
            (AdCPAdapterError, "down", "SERVICE_UNAVAILABLE", "transient"),
            (AdCPServiceUnavailableError, "offline", "SERVICE_UNAVAILABLE", "transient"),
        ]

        for exc_class, msg, expected_code, expected_recovery in cases:

            def make_tool(klass=exc_class, message=msg):
                def failing():
                    raise klass(message)

                return failing

            from fastmcp.exceptions import ToolError

            wrapped = with_error_logging(make_tool())

            with pytest.raises(ToolError) as exc_info:
                wrapped()

            tool_error = exc_info.value

            # Step 1: ToolError carries the spec-compliant envelope
            assert_envelope_shape(tool_error, expected_code, check_mcp_tool_error=True, recovery=expected_recovery)

            # Step 2: extract_error_info can read it back
            code, message_out, recovery = extract_error_info(tool_error)
            assert code == expected_code, f"{exc_class.__name__}: roundtrip code mismatch"
            assert recovery == expected_recovery, f"{exc_class.__name__}: roundtrip recovery mismatch"

    @pytest.mark.asyncio
    async def test_a2a_handler_explicit_skill_reraises_all_subclasses(self):
        """Recovery is preserved for every AdCPError subclass through ``_handle_explicit_skill``,
        but INTERNAL subclasses are scrubbed at the seam (generic message, wire SERVICE_UNAVAILABLE)
        while CLIENT-CORRECTABLE subclasses re-raise verbatim (original type + message).

        Scope: handler-internal re-raise. The outer ``on_message_send`` boundary wraps the
        propagated exception into a Task artifact; wire-level coverage is in
        ``tests/integration/test_a2a_error_responses.py``.
        """
        from src.a2a_server.adcp_a2a_server import AdCPRequestHandler
        from src.core.exceptions import (
            _SANITIZED_INTERNAL_MESSAGE,
            AdCPAdapterError,
            AdCPBudgetExhaustedError,
            AdCPConflictError,
            AdCPError,
            AdCPGoneError,
            AdCPNotFoundError,
            AdCPRateLimitError,
            AdCPServiceUnavailableError,
            AdCPValidationError,
        )

        # Recovery matches the pinned classification of the WIRE code. The
        # SERVICE_UNAVAILABLE trio is the internal bucket — scrubbed at the seam; the rest are
        # client-correctable and re-raise verbatim.
        cases = [
            (AdCPError, "internal", "transient", True),
            (AdCPValidationError, "bad", "correctable", False),
            (AdCPNotFoundError, "missing", "correctable", False),
            (AdCPConflictError, "dup", "transient", False),
            (AdCPGoneError, "expired", "correctable", False),
            (AdCPBudgetExhaustedError, "broke", "terminal", False),
            (AdCPRateLimitError, "slow", "transient", False),
            (AdCPAdapterError, "down", "transient", True),
            (AdCPServiceUnavailableError, "offline", "transient", True),
        ]

        handler = AdCPRequestHandler()

        for exc_class, msg, expected_recovery, scrubbed in cases:

            async def mock_skill(params, token, klass=exc_class, message=msg):
                kwargs = {"_wire_safe_message": True} if issubclass(klass, AdCPValidationError) else {}
                raise klass(message, **kwargs)

            with patch.object(handler, "_handle_get_products_skill", mock_skill):
                with pytest.raises(AdCPError) as exc_info:
                    await handler._handle_explicit_skill("get_products", {}, "token")
                raised = exc_info.value
                assert raised.recovery == expected_recovery, f"{exc_class.__name__}: recovery"
                if scrubbed:
                    assert raised.message == _SANITIZED_INTERNAL_MESSAGE, f"{exc_class.__name__}: not scrubbed"
                    assert raised.wire_error_code == "SERVICE_UNAVAILABLE", f"{exc_class.__name__}: wire code"
                else:
                    assert isinstance(raised, exc_class), f"{exc_class.__name__}: client-correctable must be verbatim"
                    assert raised.message == msg, f"{exc_class.__name__}: message must be preserved"

    def test_rest_roundtrip_all_subclasses(self):
        """All 11 AdCPError subclasses: raise -> REST handler -> JSON body -> verify recovery."""
        from starlette.testclient import TestClient

        from src.app import app
        from src.core.exceptions import (
            AdCPAdapterError,
            AdCPBudgetExhaustedError,
            AdCPConflictError,
            AdCPError,
            AdCPGoneError,
            AdCPNotFoundError,
            AdCPRateLimitError,
            AdCPServiceUnavailableError,
            AdCPValidationError,
        )

        # Same internal-code -> standard-code translation as the MCP/A2A roundtrip
        # tests above. HTTP status_code is preserved (it comes from the exception
        # class directly, not from the wire code translation).
        cases = [
            # Recovery matches the pinned classification of the WIRE code.
            (AdCPError, "internal", 500, "SERVICE_UNAVAILABLE", "transient"),
            (AdCPValidationError, "bad", 400, "VALIDATION_ERROR", "correctable"),
            (AdCPNotFoundError, "missing", 404, "INVALID_REQUEST", "correctable"),
            (AdCPConflictError, "dup", 409, "CONFLICT", "transient"),
            (AdCPGoneError, "expired", 410, "INVALID_STATE", "correctable"),
            (AdCPBudgetExhaustedError, "broke", 422, "BUDGET_EXHAUSTED", "terminal"),
            (AdCPRateLimitError, "slow", 429, "RATE_LIMITED", "transient"),
            (AdCPAdapterError, "down", 502, "SERVICE_UNAVAILABLE", "transient"),
            (AdCPServiceUnavailableError, "offline", 503, "SERVICE_UNAVAILABLE", "transient"),
        ]

        for exc_class, msg, expected_status, expected_code, expected_recovery in cases:
            with patch(
                "src.core.tools.capabilities.get_adcp_capabilities_raw",
                side_effect=exc_class(msg),
            ):
                client = TestClient(app, raise_server_exceptions=False)
                response = client.get("/api/v1/capabilities")
                status_msg = f"{exc_class.__name__}: status {response.status_code}, expected {expected_status}"
                assert response.status_code == expected_status, status_msg
                assert_envelope_shape(response.json(), expected_code, recovery=expected_recovery)
