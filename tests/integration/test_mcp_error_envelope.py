#!/usr/bin/env python3
"""Wire-level MCP error envelope tests.

Companion to tests/integration/test_a2a_error_responses.py — verifies that
typed AdCPError raised inside an MCP-routed _impl surfaces on the wire as a
spec two-layer envelope (``adcp_error`` + ``errors[]``) inside the
FastMCP CallToolResult content text. The MCP boundary translator
(src/core/tool_error_logging.py:_translate_to_tool_error) builds the envelope
via build_two_layer_error_envelope and wraps it in an AdCPToolError whose
``str(self)`` is the JSON-encoded envelope.

Exercises the full FastMCP pipeline end-to-end:
Client(mcp) → middleware → TypeAdapter → tool → _impl → typed raise →
boundary translator → wire envelope. Mock-only equivalents do not prove
this wiring.

Production-validator tests (BUDGET_TOO_LOW, INVALID_REQUEST past start_time)
drive REAL invalid input through the full pipeline — no ``_impl`` patching.
This exercises the actual production validators
(src/core/tools/media_buy_create.py:1756 budget, :1791 start_time) and the
AdCPValidationError raised directly.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from tests.factories.principal import PrincipalFactory
from tests.helpers import assert_envelope_shape
from tests.helpers.adcp_factories import create_test_package_request_dict
from tests.helpers.auth_contract import assert_two_layer_auth_contract
from tests.helpers.mcp_envelope_capture import call_mcp_tool_capturing_envelope
from tests.integration.conftest import seed_error_test_tenant

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]


_TENANT_ID = "mcp_envelope_test"
_PRINCIPAL_ID = "mcp_envelope_principal"
_ACCESS_TOKEN = "mcp_envelope_token_456"
_PRODUCT_ID = "mcp_envelope_product"


@pytest.fixture
def mcp_real_tenant_setup(integration_db):
    """Real-DB ResolvedIdentity for end-to-end MCP wire tests against production validators."""
    from tests.harness._base import IntegrationEnv

    with IntegrationEnv():
        yield seed_error_test_tenant(
            tenant_id=_TENANT_ID,
            principal_id=_PRINCIPAL_ID,
            access_token=_ACCESS_TOKEN,
            product_id=_PRODUCT_ID,
            subdomain="mcpenv",
            tenant_name="MCP Envelope Test Tenant",
            advertiser_id="mock_adv_456",
        )["identity"]


@pytest.mark.integration
@pytest.mark.requires_db
class TestMcpWireErrorEnvelope:
    """MCP-routed _impl raises typed AdCPError → spec two-layer envelope on wire."""

    def test_update_media_buy_not_found_emits_two_layer_envelope_on_wire(self, integration_db):
        """AdCPMediaBuyNotFoundError from _impl surfaces as a two-layer envelope on the MCP wire.

        Flow exercised end-to-end:
            Client(mcp).call_tool("update_media_buy", {"media_buy_id": "nonexistent", "paused": True})
              → FastMCP middleware chain
              → TypeAdapter validates args
              → update_media_buy MCP wrapper (src/core/tools/media_buy_update.py)
              → _update_media_buy_impl → MediaBuyRepository.get_by_id returns None
              → raise AdCPMediaBuyNotFoundError("Media buy 'nonexistent' not found.")
              → with_error_logging wrapper catches it
              → _translate_to_tool_error builds envelope via build_two_layer_error_envelope
              → raises AdCPToolError(envelope, status_code=404)
              → FastMCP serializes str(error) = JSON envelope into CallToolResult.content[0].text

        This is the wire-level shape. The harness's _unwrap_mcp_tool_error would
        normally parse this and reconstruct an AdCPError — we bypass it here to
        inspect the wire bytes directly.

        MEDIA_BUY_NOT_FOUND is a STANDARD_ERROR_CODES entry — it passes through
        the boundary translator unchanged (no ERROR_CODE_MAPPING rewrite).
        """
        identity = PrincipalFactory.make_identity(protocol="mcp")

        is_error, envelope = call_mcp_tool_capturing_envelope(
            "update_media_buy",
            {
                "media_buy_id": "mb_does_not_exist_mcp_wire",
                "paused": True,  # need ≥1 updatable field to pass pre-lookup validation
            },
            identity,
        )

        assert is_error, "Nonexistent media_buy_id must produce a tool error"
        assert envelope is not None, "Error must include content text carrying the envelope"

        # MEDIA_BUY_NOT_FOUND is a STANDARD_ERROR_CODES entry — passes through unchanged.
        # AdCPMediaBuyNotFoundError overrides AdCPNotFoundError's terminal default
        # with correctable (the buyer can correct by supplying the right media_buy_id).
        assert_envelope_shape(
            envelope,
            "MEDIA_BUY_NOT_FOUND",
            recovery="correctable",
            message_substr="mb_does_not_exist_mcp_wire",
        )
        # Two-layer invariant: errors[0].message is byte-identical to adcp_error.message.
        single_error_msg = "errors[0].message must be byte-identical to adcp_error.message in single-error case"
        assert envelope["errors"][0]["message"] == envelope["adcp_error"]["message"], single_error_msg

    def test_create_media_buy_budget_too_low_emits_envelope_on_wire(self, mcp_real_tenant_setup):
        """Production BUDGET_TOO_LOW validator surfaces as a spec two-layer envelope on the wire.

        Drives REAL invalid input (per-package ``budget=0``) through the full pipeline:
            Client(mcp).call_tool("create_media_buy", real_invalid_payload)
              → middleware resolves identity (patched to use real tenant/principal)
              → create_media_buy MCP wrapper builds CreateMediaBuyRequest
              → _create_media_buy_impl validation: get_total_budget() == 0
              → raise AdCPBudgetTooLowError(...) (line 1758)
              → except block translates to AdCPValidationError (line 2221)
              → with_error_logging → _translate_to_tool_error → wire envelope

        No ``_impl`` patching — exercises the actual production validator
        and the structured-error→AdCPError translation path.

        BUDGET_TOO_LOW is a spec STANDARD code (passthrough — not remapped).
        """
        identity = mcp_real_tenant_setup
        start_time = (datetime.now(UTC) + timedelta(days=1)).isoformat()
        end_time = (datetime.now(UTC) + timedelta(days=31)).isoformat()

        is_error, envelope = call_mcp_tool_capturing_envelope(
            "create_media_buy",
            {
                "brand": {"domain": "wiretest.example"},
                "idempotency_key": f"int-key-{uuid.uuid4().hex}",
                "packages": [
                    create_test_package_request_dict(
                        product_id=_PRODUCT_ID,
                        pricing_option_id="cpm_usd_fixed",
                        budget=0,
                    )
                ],
                "start_time": start_time,
                "end_time": end_time,
            },
            identity,
        )

        assert is_error, "BUDGET_TOO_LOW must produce a tool error"
        assert envelope is not None, "Error must include content text carrying the envelope"
        assert_envelope_shape(envelope, "BUDGET_TOO_LOW", recovery="correctable")

    def test_create_media_buy_validation_error_emits_envelope_on_wire(self, mcp_real_tenant_setup):
        """Production past-start-time validator surfaces INVALID_REQUEST on the wire.

        Drives REAL invalid input (``start_time`` in the past) through the
        full pipeline. Production validator at
        src/core/tools/media_buy_create.py:1791 raises
        ``AdCPValidationError(error_code="INVALID_REQUEST")`` directly; the
        MCP boundary translator builds the wire envelope.

        No ``_impl`` patching — exercises the actual production validator.

        INVALID_REQUEST is a spec STANDARD code (passthrough — not remapped).
        """
        identity = mcp_real_tenant_setup

        is_error, envelope = call_mcp_tool_capturing_envelope(
            "create_media_buy",
            {
                "brand": {"domain": "wiretest.example"},
                "idempotency_key": f"int-key-{uuid.uuid4().hex}",
                "packages": [
                    create_test_package_request_dict(
                        product_id=_PRODUCT_ID,
                        pricing_option_id="cpm_usd_fixed",
                        budget=5000.0,
                    )
                ],
                "start_time": "2020-01-01T00:00:00Z",  # in the past
                "end_time": "2020-02-01T00:00:00Z",
            },
            identity,
        )

        assert is_error, "INVALID_REQUEST must produce a tool error"
        assert envelope is not None, "Error must include content text carrying the envelope"
        assert_envelope_shape(envelope, "INVALID_REQUEST", recovery="correctable")
        for error in (envelope["adcp_error"], envelope["errors"][0]):
            assert error["message"] == "Start time cannot be in the past."
            assert error["field"] == "start_time"
            assert error["suggestion"] == "Use a future datetime or 'asap' for immediate start."

        from tests.helpers.secret_scrub import serialize_wire_error

        serialized = serialize_wire_error(envelope)
        assert "2020-01-01" not in serialized
        assert "2020-02-01" not in serialized

    def test_duplicate_product_id_echoes_the_buyers_value_on_the_real_wire(self, mcp_real_tenant_setup):
        """Production's _wire_safe_message=True opt-in (media_buy_create.py ~2537) actually
        reaches the wire — not just the mechanism tested in isolation by
        tests/unit/test_exception_normalization.py's mirrored-template tests.

        Drives REAL duplicate product_ids through the full pipeline, no ``_impl`` patching.
        Removing the opt-in at that raise site would redden this test (the message would
        fall back to the generic VALIDATION_ERROR scrub text and no longer contain
        "Duplicate" or the product_id) — mutation-verified during this fix's development.

        Uses a credential-/URL-shaped product_id (duplicate-detection runs before any
        catalog lookup — media_buy_create.py's duplicate check at ~2528 precedes the
        AdCPProductNotFoundError check at ~2572, so this value never needs to resolve to a
        real product) to show the echo carries a value of that SHAPE intact, rather than
        only the tidy fixture id.

        What ``assert_no_secret_leak`` below does and does not prove: it is a regression
        guard that no SERVER-side fragment (connection string, token, inline SQL — the
        fixed set in tests/helpers/secret_scrub.py) joins this message. It is NOT made
        stronger by the buyer value above, which deliberately contains none of those
        fragments; it reads the same either way. The guard was shown to bite by mutation
        during development — appending a fake connection string to the raise site's
        f-string reddened it — not by anything this particular product_id contributes.
        A sentinel cannot be threaded through this path instead: the buyer value is
        expected to echo, so it cannot simultaneously serve as a must-be-absent marker.
        """
        identity = mcp_real_tenant_setup
        credential_shaped_product_id = "https://example.com/callback?token=buyer-rotated-secret-789"

        is_error, envelope = call_mcp_tool_capturing_envelope(
            "create_media_buy",
            {
                "brand": {"domain": "wiretest.example"},
                "idempotency_key": f"int-key-{uuid.uuid4().hex}",
                "packages": [
                    create_test_package_request_dict(
                        product_id=credential_shaped_product_id, pricing_option_id="cpm_usd_fixed", budget=5000.0
                    ),
                    create_test_package_request_dict(
                        product_id=credential_shaped_product_id, pricing_option_id="cpm_usd_fixed", budget=5000.0
                    ),
                ],
                "start_time": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
                "end_time": (datetime.now(UTC) + timedelta(days=31)).isoformat(),
            },
            identity,
        )

        assert is_error, "duplicate product_id must produce a tool error"
        assert envelope is not None, "Error must include content text carrying the envelope"
        assert_envelope_shape(envelope, "VALIDATION_ERROR", recovery="correctable")
        for error in (envelope["adcp_error"], envelope["errors"][0]):
            assert "Duplicate" in error["message"]
            assert credential_shaped_product_id in error["message"]

        from tests.helpers.secret_scrub import assert_no_secret_leak

        assert_no_secret_leak(envelope)

    def test_managed_only_targeting_dimension_emits_envelope_on_wire(self, mcp_real_tenant_setup):
        """Production's _wire_safe_message=True opt-in (media_buy_create.py ~2893) actually
        reaches the wire for the targeting-overlay raise site.

        Drives a REAL managed-only dimension (key_value_pairs) through the full pipeline —
        the same trigger tests/bdd/steps/generic/given_media_buy.py's
        given_managed_targeting_dimension uses for the BDD scenario covering this raise
        site. Removing the opt-in would redden this test the same way it reddens that
        scenario (mutation-verified during this fix's development).
        """
        identity = mcp_real_tenant_setup

        is_error, envelope = call_mcp_tool_capturing_envelope(
            "create_media_buy",
            {
                "brand": {"domain": "wiretest.example"},
                "idempotency_key": f"int-key-{uuid.uuid4().hex}",
                "packages": [
                    create_test_package_request_dict(
                        product_id=_PRODUCT_ID,
                        pricing_option_id="cpm_usd_fixed",
                        budget=5000.0,
                        targeting_overlay={"key_value_pairs": {"section": "sports"}},
                    )
                ],
                "start_time": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
                "end_time": (datetime.now(UTC) + timedelta(days=31)).isoformat(),
            },
            identity,
        )

        assert is_error, "managed-only targeting dimension must produce a tool error"
        assert envelope is not None, "Error must include content text carrying the envelope"
        assert_envelope_shape(envelope, "INVALID_REQUEST", recovery="correctable")
        for error in (envelope["adcp_error"], envelope["errors"][0]):
            assert "managed" in error["message"].lower()

        from tests.helpers.secret_scrub import assert_no_secret_leak

        assert_no_secret_leak(envelope)

    def test_typed_validation_failure_from_impl_emits_scrubbed_envelope_on_wire(
        self, mcp_real_tenant_setup, monkeypatch
    ):
        """A TYPED failure escaping the create implementation keeps its envelope on the MCP wire.

        This is intentionally injected below request parsing and above the normal
        tool boundary: it proves the middleware/tool translator, rather than a
        particular business validator, preserves both error-envelope layers.

        The injected exception is typed on purpose — this grades that an ``AdCPValidationError``
        raise-site message is SCRUBBED off the wire, which nothing else covers. The untyped
        crash path is a different branch with a different code, graded by its own sibling
        below; the two cannot share one test because SERVICE_UNAVAILABLE has no canonical
        sanitized presentation for ``assert_sanitized_wire_error`` to check.
        """
        from src.core.exceptions import AdCPValidationError

        async def _raise_top_level_failure(*_args, **_kwargs):
            raise AdCPValidationError("forced MCP top-level failure")

        monkeypatch.setattr("src.core.tools.media_buy_create._create_media_buy_impl", _raise_top_level_failure)
        identity = mcp_real_tenant_setup

        is_error, envelope = call_mcp_tool_capturing_envelope(
            "create_media_buy",
            {
                "brand": {"domain": "wiretest.example"},
                "idempotency_key": f"int-key-{uuid.uuid4().hex}",
                "packages": [
                    create_test_package_request_dict(
                        product_id=_PRODUCT_ID,
                        pricing_option_id="cpm_usd_fixed",
                        budget=5000.0,
                    )
                ],
                "start_time": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
                "end_time": (datetime.now(UTC) + timedelta(days=31)).isoformat(),
            },
            identity,
        )

        assert is_error, "top-level implementation failure must produce an MCP tool error"
        assert envelope is not None, "MCP tool error must carry the two-layer envelope"
        assert_envelope_shape(
            envelope,
            "VALIDATION_ERROR",
            recovery="correctable",
        )
        from tests.helpers.secret_scrub import assert_sanitized_wire_error

        assert_sanitized_wire_error(
            envelope,
            "VALIDATION_ERROR",
            rejected_fragments=("forced MCP top-level failure",),
        )

    def test_untyped_impl_crash_emits_sanitized_envelope_on_wire(self, mcp_real_tenant_setup, monkeypatch):
        """An UNTYPED crash in the implementation still reaches the buyer as an AdCP envelope.

        The typed sibling above travels a path that already carried an envelope before this
        boundary existed. This one drives the fall-through arm — an exception the
        normalization registry does not recognise — which is the branch that turns a real
        crash into ``SERVICE_UNAVAILABLE``/``transient`` instead of a bare transport error.

        Distinct from the unit-level oracle in ``test_error_boundary_translation.py``: this
        one runs through a real ``Client(mcp)`` call, so it also proves FastMCP does not
        replace the message before the buyer sees it.
        """

        from tests.helpers.secret_scrub import SECRET_BEARING_MESSAGE, assert_no_secret_leak

        async def _raise_untyped_crash(*_args, **_kwargs):
            raise RuntimeError(SECRET_BEARING_MESSAGE)

        monkeypatch.setattr("src.core.tools.media_buy_create._create_media_buy_impl", _raise_untyped_crash)
        identity = mcp_real_tenant_setup

        is_error, envelope = call_mcp_tool_capturing_envelope(
            "create_media_buy",
            {
                "brand": {"domain": "wiretest.example"},
                "idempotency_key": f"int-key-{uuid.uuid4().hex}",
                "packages": [
                    create_test_package_request_dict(
                        product_id=_PRODUCT_ID,
                        pricing_option_id="cpm_usd_fixed",
                        budget=5000.0,
                    )
                ],
                "start_time": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
                "end_time": (datetime.now(UTC) + timedelta(days=31)).isoformat(),
            },
            identity,
        )

        assert is_error, "untyped implementation crash must produce an MCP tool error"
        assert envelope is not None, "MCP tool error must carry the two-layer envelope"
        assert_envelope_shape(envelope, "SERVICE_UNAVAILABLE", recovery="transient")
        assert_no_secret_leak(envelope, context="MCP untyped crash wire envelope")

    def test_get_media_buy_delivery_missing_identity_emits_auth_envelope_on_wire(self, integration_db):
        """Missing identity in get_media_buy_delivery surfaces AUTH_MISSING on the MCP wire.

        Flow:
            Client(mcp).call_tool("get_media_buy_delivery", {...}) with identity=None
              → middleware classifies absent transport credentials
              → AdCPAuthMissingError carries error_code="AUTH_MISSING"
              → _translate_to_tool_error emits the two-layer wire envelope
        """
        is_error, envelope = call_mcp_tool_capturing_envelope(
            "get_media_buy_delivery",
            {"media_buy_ids": ["any_id"]},
            identity=None,
        )

        assert is_error, "Missing identity must produce a tool error"
        assert envelope is not None, "Error must include content text carrying the envelope"

        # AdCPAuthMissingError -> AUTH_MISSING with correctable recovery (#1417).
        assert_two_layer_auth_contract(envelope, "mcp", "missing")
        assert "identity" in envelope["adcp_error"]["message"].lower() or (
            "auth" in envelope["adcp_error"]["message"].lower()
        ), f"Envelope message must mention identity/auth, got: {envelope['adcp_error']['message']}"

    def test_get_media_buys_unsupported_account_filter_emits_envelope_on_wire(self, integration_db):
        """``get_media_buys`` with ``account_id`` surfaces UNSUPPORTED_FEATURE on the MCP wire.

        Flow:
            Client(mcp).call_tool("get_media_buys", {"account_id": "..."})
              → middleware resolves identity (patched)
              → _get_media_buys_impl raises AdCPCapabilityNotSupportedError
              → error_code "UNSUPPORTED_FEATURE" passes through STANDARD_ERROR_CODES
              → boundary translator builds the wire envelope

        Recovery is ``correctable`` per the documented spec divergence (the
        buyer can retry without the unsupported parameter); a generic
        VALIDATION_ERROR would not carry that retry semantic. Pins the
        production raise → wire shape end-to-end.
        """
        from tests.factories import PrincipalFactory

        identity = PrincipalFactory.make_identity(
            tenant_id="any_tenant_unsupported_wire",
            principal_id="any_principal_unsupported_wire",
            protocol="mcp",
        )

        is_error, envelope = call_mcp_tool_capturing_envelope(
            "get_media_buys",
            {"account_id": "acc_unsupported_wire_test"},
            identity,
        )

        assert is_error, "Unsupported account_id filter must produce a tool error"
        assert envelope is not None, "Error must include content text carrying the envelope"
        assert_envelope_shape(envelope, "UNSUPPORTED_FEATURE", recovery="correctable")
        account_msg = (
            f"Envelope message must explain the unsupported parameter, got: {envelope['adcp_error']['message']}"
        )
        assert "account" in envelope["adcp_error"]["message"].lower(), account_msg
