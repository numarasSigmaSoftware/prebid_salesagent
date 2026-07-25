"""MCP TypeAdapter validation errors are emitted as AdCP wire envelopes.

These tests use the real in-memory FastMCP client and server:

    Client(mcp) -> middleware -> FastMCP TypeAdapter -> CallToolResult

The payloads fail before the tool body runs, so no database setup is required.
"""

from __future__ import annotations

import pytest

from tests.helpers import assert_envelope_shape, assert_no_raw_validation_leak
from tests.helpers.adcp_factories import create_test_package_request_dict
from tests.helpers.mcp_envelope_capture import call_mcp_tool_capturing_envelope

pytestmark = pytest.mark.integration


def test_typeadapter_validation_error_emits_adcp_envelope_on_mcp_wire():
    tool_name = "list_creatives"
    params = {"filters": {"concept_ids": []}}
    is_error, envelope = call_mcp_tool_capturing_envelope(
        tool_name,
        params,
        stub_lifecycle_schedulers=True,
    )

    assert is_error, f"{tool_name}: invalid typed params must produce a tool error"
    assert envelope is not None, f"{tool_name}: no MCP wire error envelope captured"
    assert_envelope_shape(envelope, "VALIDATION_ERROR", recovery="correctable", message_substr="List should have")
    assert envelope["errors"][0].get("suggestion"), f"{tool_name}: envelope must include a recovery suggestion"
    assert envelope["errors"][0].get("field") == "filters.concept_ids"
    assert_no_raw_validation_leak(envelope["errors"][0]["message"])


def test_create_media_buy_missing_key_preserves_field_on_mcp_wire():
    is_error, envelope = call_mcp_tool_capturing_envelope(
        "create_media_buy",
        {
            "brand": {"domain": "wiretest.example"},
            "packages": [
                {
                    "product_id": "prod_1",
                    "budget": 5000,
                    "pricing_option_id": "cpm_usd_fixed",
                }
            ],
            "start_time": "2026-08-01T00:00:00Z",
            "end_time": "2026-09-01T00:00:00Z",
            "po_number": "WIRE-1",
        },
        stub_lifecycle_schedulers=True,
    )

    assert is_error
    assert envelope is not None
    assert_envelope_shape(
        envelope,
        "VALIDATION_ERROR",
        recovery="correctable",
        message_substr="Required field is missing",
    )
    assert envelope["errors"][0].get("field") == "idempotency_key"
    assert_no_raw_validation_leak(envelope["errors"][0]["message"])


def test_create_media_buy_typeadapter_error_uses_storyboard_invalid_request_code():
    package = create_test_package_request_dict(
        product_id="prod_missing",
        pricing_option_id="cpm_usd_fixed",
        budget=5000,
    )
    package["creatives"] = [
        {
            "creative_id": "creative-missing-asset-type",
            "name": "Missing asset discriminator",
            "assets": {"main": {"url": "https://example.com/creative.jpg"}},
        }
    ]
    is_error, envelope = call_mcp_tool_capturing_envelope(
        "create_media_buy",
        {
            "brand": {"domain": "wiretest.example"},
            "idempotency_key": "wire-missing-asset-type",
            "packages": [package],
            "start_time": "2026-08-01T00:00:00Z",
            "end_time": "2026-09-01T00:00:00Z",
        },
        stub_lifecycle_schedulers=True,
    )

    assert is_error
    assert_envelope_shape(envelope, "INVALID_REQUEST", recovery="correctable", message_substr="Field required")
