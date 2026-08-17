"""Comprehensive error path testing for AdCP tools.

⚠️ MIGRATION NOTICE: This test has been migrated to tests/integration_v2/ to use the new
pricing_options model. The original file in tests/integration/ is deprecated.

📊 BUDGET FORMAT: AdCP v2.2.0 Migration (2025-10-27)
All tests in this file use float budget format per AdCP v2.2.0 spec:
- Package.budget: float (e.g., 1000.0) - NOT Budget object
- Currency is determined by PricingOption, not Package
- Validation happens at Pydantic schema level (raises ToolError for constraint violations)

This test suite systematically exercises error handling paths that were previously
untested, ensuring:
1. Error responses are actually constructible (no NameErrors)
2. Error classes are properly imported
3. Error handling returns proper AdCP-compliant responses
4. All validation and authentication failures are handled gracefully

Background: PR #332 fixed a NameError where Error class wasn't imported but was
used in error responses. These tests prevent regression by actually executing
those error paths.
"""

import pytest
from fastmcp.exceptions import ToolError

from src.core.exceptions import AdCPValidationError
from src.core.tools import list_creatives_raw
from tests.factories import PrincipalFactory
from tests.harness._idempotency import fresh_idempotency_key

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]


@pytest.mark.integration
@pytest.mark.requires_db
class TestSyncCreativesErrorPaths:
    """Test error handling in sync_creatives."""

    @pytest.mark.asyncio
    async def test_invalid_creative_format_returns_error(self, integration_db):
        """Test that invalid creative format is handled gracefully."""
        from tests.harness import CreativeSyncEnv

        # Invalid creative - missing required fields
        invalid_creatives = [
            {
                "creative_id": "invalid_creative",
                # Missing format, assets, etc
            }
        ]

        # This raw boundary implements per-creative partial-success semantics:
        # buyer-invalid creative data must become an explicit failed item, not a
        # request-level exception. Supply the required request key so the test
        # reaches that validation path instead of false-greening on ingress.
        with CreativeSyncEnv(tenant_id="test_tenant", principal_id="test_principal") as env:
            env.setup_default_data()
            response = env.call_a2a(
                creatives=invalid_creatives,
                idempotency_key=fresh_idempotency_key(),
            )

        # sync_creatives_raw returned a response: it must report the failure
        # explicitly via a per-creative ``action == failed`` entry, not silently
        # accept the malformed creative as a success.
        assert response is not None, "sync_creatives_raw must not return None for invalid input"
        failed = [c for c in response.creatives if c.action == "failed"]
        succeeded = [c for c in response.creatives if c.action in ("created", "updated")]
        assert len(failed) == 1, (
            f"Invalid creative should land in creatives[] with action=failed, "
            f"got {[c.action for c in response.creatives]}"
        )
        assert not succeeded, f"Invalid creative must not be reported as created/updated, got {succeeded!r}"
        errors = failed[0].errors
        assert errors, "Failed creative must carry buyer-visible error details"
        error_text = " ".join(error.message for error in errors)
        assert "name" in error_text and "format_id" in error_text, (
            f"Malformed-creative error must identify the missing creative fields, got: {error_text}"
        )
        assert "idempotency_key" not in error_text, (
            f"Test stopped at request-key validation instead of creative validation: {error_text}"
        )


@pytest.mark.integration
@pytest.mark.requires_db
class TestListCreativesErrorPaths:
    """Test error handling in list_creatives."""

    @pytest.mark.asyncio
    async def test_invalid_date_format_returns_error(self, integration_db):
        """Test that invalid date format is handled with proper error."""
        from src.core.config_loader import set_current_tenant

        identity = PrincipalFactory.make_identity(
            tenant_id="test_tenant",
            principal_id="test_principal",
            protocol="a2a",
        )

        set_current_tenant(
            {
                "tenant_id": "test_tenant",
                "name": "Test Tenant",
                "subdomain": "test",
                "ad_server": "mock",
            }
        )

        # Should raise ToolError or AdCPValidationError, not NameError
        with pytest.raises((ToolError, AdCPValidationError)) as exc_info:
            await list_creatives_raw(
                created_after="not-a-date",  # Invalid format
                identity=identity,
            )

        # Verify it's a proper error, not NameError
        assert "date" in str(exc_info.value).lower()


@pytest.mark.integration
class TestImportValidation:
    """Meta-test: Verify Error class is actually importable where used."""

    def test_error_class_is_constructible(self):
        """Verify Error class can be constructed (basic smoke test)."""
        from src.core.schemas import Error

        error = Error(code="test_code", message="test message")
        assert error.code == "test_code"
        assert error.message == "test message"

    def test_error_class_imported_in_main(self):
        """Verify Error class is imported in main.py (regression test for PR #332)."""
        import src.core.main
        from src.core.schemas import Error

        # Verify Error is accessible from main module
        assert hasattr(src.core.main, "Error")
        # Verify it's the same class
        assert src.core.main.Error is Error

    def test_create_media_buy_response_with_errors(self):
        """Verify CreateMediaBuyError can contain Error objects.

        Protocol fields (adcp_version, status) removed in protocol envelope migration.
        Note: CreateMediaBuyResponse is a union type (CreateMediaBuySuccess | CreateMediaBuyError),
        so for error responses we use CreateMediaBuyError directly.
        """
        from src.core.schemas import CreateMediaBuyError, Error

        response = CreateMediaBuyError(
            errors=[Error(code="test", message="test error")],
        )

        assert len(response.errors) == 1
        assert response.errors[0].code == "test"


@pytest.mark.integration
class TestRecoveryFieldInErrorResponses:
    """Verify recovery field appears in REST error responses via the exception handler.

    The REST boundary now serializes the AdCP spec 3.0.0 two-layer envelope:
    recovery lives inside ``adcp_error.recovery`` and ``errors[0].recovery``,
    not at the top level. These tests confirm the full chain: AdCPError raised
    -> exception handler -> envelope JSON body.
    """

    @pytest.mark.parametrize(
        ("error_factory_path", "exc_message", "expected_status", "expected_code", "expected_recovery"),
        [
            (
                "src.core.exceptions.AdCPValidationError",
                "bad input",
                400,
                "VALIDATION_ERROR",
                "correctable",
            ),
            (
                "src.core.exceptions.AdCPAdapterError",
                "GAM unavailable",
                502,
                "SERVICE_UNAVAILABLE",
                "transient",
            ),
            (
                "src.core.exceptions.AdCPBudgetExceededError",
                "budget exceeds ceiling",
                422,
                "BUDGET_EXCEEDED",
                "correctable",
            ),
            (
                "src.core.exceptions.AdCPCreativeRejectedError",
                "creative failed policy review",
                422,
                "CREATIVE_REJECTED",
                "correctable",
            ),
            (
                "src.core.exceptions.AdCPProductUnavailableError",
                "product is offline",
                422,
                "PRODUCT_UNAVAILABLE",
                "correctable",
            ),
        ],
        ids=[
            "validation_error_correctable",
            "adapter_error_transient",
            "budget_exceeded_correctable",
            "creative_rejected_correctable",
            "product_unavailable_correctable",
        ],
    )
    def test_rest_recovery_field_propagates_from_typed_error(
        self, error_factory_path, exc_message, expected_status, expected_code, expected_recovery
    ):
        """REST exception handler propagates recovery hint from typed AdCPError to both envelope layers."""
        import importlib
        from unittest.mock import patch

        from starlette.testclient import TestClient

        from src.app import app
        from tests.helpers import assert_envelope_shape

        module_path, _, class_name = error_factory_path.rpartition(".")
        exc_class = getattr(importlib.import_module(module_path), class_name)

        with patch(
            "src.core.tools.capabilities.get_adcp_capabilities_raw",
            side_effect=exc_class(exc_message),
        ):
            client = TestClient(app, raise_server_exceptions=False)
            response = client.get("/api/v1/capabilities")
            assert response.status_code == expected_status
            assert_envelope_shape(response.json(), expected_code, recovery=expected_recovery)

    def test_rest_custom_recovery_override_preserved(self):
        """Custom recovery= override is preserved through REST boundary (both layers)."""
        from unittest.mock import patch

        from starlette.testclient import TestClient

        from src.app import app
        from src.core.exceptions import AdCPNotFoundError

        with patch(
            "src.core.tools.capabilities.get_adcp_capabilities_raw",
            side_effect=AdCPNotFoundError("temporarily gone", recovery="transient"),
        ):
            client = TestClient(app, raise_server_exceptions=False)
            response = client.get("/api/v1/capabilities")
            assert response.status_code == 404
            body = response.json()
            recovery_msg = "Custom recovery='transient' must be preserved at envelope level, not default 'terminal'"
            assert body["adcp_error"]["recovery"] == "transient", recovery_msg
            assert body["errors"][0]["recovery"] == "transient"

    def test_to_dict_serialization_roundtrip(self):
        """AdCPError.to_dict() -> JSON -> verify recovery is present and correct."""
        import json

        from src.core.exceptions import (
            AdCPBudgetExhaustedError,
            AdCPRateLimitError,
            AdCPValidationError,
        )

        cases = [
            (AdCPValidationError("bad"), "correctable"),
            (AdCPRateLimitError("slow"), "transient"),
            # BUDGET_EXHAUSTED recovery is terminal per the pinned enum (#1417).
            (AdCPBudgetExhaustedError("broke"), "terminal"),
        ]

        for exc, expected_recovery in cases:
            d = exc.to_dict()
            # Simulate JSON roundtrip (what happens in real HTTP response)
            json_str = json.dumps(d)
            deserialized = json.loads(json_str)
            roundtrip_msg = f"{type(exc).__name__}: recovery lost in JSON roundtrip"
            assert deserialized["recovery"] == expected_recovery, roundtrip_msg


class TestRestBoundaryAuditObservability:
    """A REST boundary error leaves an audit row when identity is resolvable.

    The MCP and A2A boundaries already write a tenant-scoped audit row on
    error; REST previously emitted only a WARNING log line because identity
    is not resolved on ``request.state`` at the exception-handler boundary.
    ``_envelope_response`` now resolves identity best-effort from the request's
    ``x-adcp-auth`` token and forwards it to ``record_boundary_error`` so all
    three transports leave the same persistent trail.
    """

    def test_rest_error_with_valid_token_writes_audit_row(self, sample_principal):
        """REST 4xx with a valid token writes an audit row scoped to the resolved principal."""
        from unittest.mock import patch

        from sqlalchemy import select
        from starlette.testclient import TestClient

        from src.app import app
        from src.core.database.database_session import get_db_session
        from src.core.database.models import AuditLog
        from src.core.exceptions import AdCPMediaBuyNotFoundError

        with patch(
            "src.core.tools.capabilities.get_adcp_capabilities_raw",
            side_effect=AdCPMediaBuyNotFoundError("buy_x missing"),
        ):
            client = TestClient(app, raise_server_exceptions=False)
            response = client.get(
                "/api/v1/capabilities",
                headers={"x-adcp-auth": sample_principal["access_token"]},
            )

        assert response.status_code == 404

        with get_db_session() as session:
            stmt = select(AuditLog).filter_by(tenant_id="test_tenant", adapter_id="rest_boundary")
            audit_log = session.scalars(stmt).first()

        assert audit_log is not None, "REST boundary error must write an audit row when identity resolves"
        assert audit_log.success is False
        assert audit_log.principal_id == "test_principal"
        assert "buy_x missing" in (audit_log.error_message or "")


@pytest.mark.requires_db
class TestA2AMissingTokenRecordsTheBoundaryError:
    """A2A's missing-token rejection must leave the same trail as its siblings.

    `record_boundary_error`'s docstring states that all three boundaries
    delegate to it so log severity, activity-feed publishing and audit logging
    stay in lockstep — a claimed invariant with no oracle. A2A broke it in one
    specific spot: the missing-token branch raises `InvalidRequestError`, an
    `A2AError` subclass, and `except A2AError: raise` re-raises it without ever
    reaching the recorder. Measured over the real transports before the fix:
    A2A 0, REST 1, MCP 1.

    The gap is INSIDE A2A too, not only across transports — the sibling
    INVALID-token path a few lines down does record.
    """

    @staticmethod
    async def _send_without_token(host: str | None = None):
        from a2a.types import SendMessageRequest

        from src.a2a_server.adcp_a2a_server import AdCPRequestHandler
        from tests.a2a_helpers import make_a2a_context
        from tests.utils.a2a_helpers import create_a2a_message_with_skill

        handler = AdCPRequestHandler()
        params = SendMessageRequest(
            message=create_a2a_message_with_skill("create_media_buy", {"promoted_offering": "x"})
        )
        headers = {"host": host} if host else {}
        return await handler.on_message_send(params, context=make_a2a_context(headers=headers))

    @pytest.mark.asyncio
    async def test_the_rejection_is_recorded_before_it_is_raised(self, integration_db):
        from unittest.mock import patch

        from a2a.utils.errors import A2AError

        with patch("src.a2a_server.adcp_a2a_server.record_boundary_error_for_identity") as recorder:
            with pytest.raises(A2AError):
                await self._send_without_token()

        assert recorder.call_count == 1, (
            f"A2A missing-token rejection recorded {recorder.call_count} boundary errors; "
            "REST and MCP each record 1 for the same event"
        )
        transport, operation, error, _identity = recorder.call_args.args
        assert transport == "a2a"
        assert operation == "message_processing"
        assert error.error_code == "AUTH_REQUIRED"
        # The identity argument is deliberately NOT asserted None any more.
        # A2A resolves a header-only identity here so the rejection can be
        # scoped to a tenant and written durably, matching REST and MCP; with
        # no host header there is simply nothing to resolve. Whether that
        # produces a durable row is graded by
        # test_a_resolvable_tenant_gets_a_durable_row_not_just_a_log_line.

    @pytest.mark.asyncio
    async def test_a_resolvable_tenant_gets_a_durable_row_not_just_a_log_line(self, integration_db, sample_tenant):
        """Parity with REST and MCP means a ROW, not a log line.

        The first version of this fix passed identity=None, so
        record_boundary_error returned before the activity feed and the audit
        log — it recorded nothing durable, and the docstring's claim that all
        three boundaries stay in lockstep was still false. A2A now resolves a
        header-only tenant for the rejection, exactly as REST's
        _best_effort_rest_identity does, so an operator can see A2A auth
        failures at all.

        A header-resolved tenant is not an authorization decision: it scopes
        WHERE the refusal is recorded. The caller is still unauthenticated and
        still refused — the envelope assertion below is unchanged.
        """
        from unittest.mock import patch

        from a2a.utils.errors import A2AError

        with (
            patch("src.services.activity_feed.activity_feed") as activity_feed,
            patch("src.core.audit_logger.get_audit_logger") as audit_logger,
            pytest.raises(A2AError),
        ):
            await self._send_without_token(host=f"{sample_tenant['subdomain']}.example.com")

        activity_feed.log_error.assert_called_once_with(
            tenant_id=sample_tenant["tenant_id"],
            principal_name="anonymous",
            error_message=(
                "message_processing: Authentication required - Bearer token required in Authorization header"
            ),
            error_code="AUTH_REQUIRED",
        )
        # Grade the WRITE, not the factory. assert_called_once_with("A2A", …)
        # only confirms get_audit_logger(...) was constructed — a regression
        # dropping log_operation, or flipping success=True, would not redden it.
        audit_logger.assert_called_once_with("A2A", sample_tenant["tenant_id"])
        audit_logger.return_value.log_operation.assert_called_once_with(
            operation="message_processing",
            principal_name="anonymous",
            principal_id="anonymous",
            adapter_id="a2a_boundary",
            success=False,
            error="Authentication required - Bearer token required in Authorization header",
        )

    @pytest.mark.asyncio
    async def test_the_buyer_still_receives_the_auth_envelope(self, integration_db):
        """Recording must not change what reaches the wire."""
        from a2a.utils.errors import A2AError

        with pytest.raises(A2AError) as exc_info:
            await self._send_without_token()

        envelope = exc_info.value.data
        assert envelope["adcp_error"]["code"] == "AUTH_REQUIRED"
        assert envelope["errors"][0]["code"] == "AUTH_REQUIRED"
