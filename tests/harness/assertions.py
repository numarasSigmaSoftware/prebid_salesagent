"""Shared assertion helpers for multi-transport behavioral tests.

These helpers verify transport-specific envelope shapes and shared
payload properties. Use with TransportResult from dispatchers.

Usage::

    result = env.call_via(Transport.REST, creatives=[...])
    assert_envelope(result, Transport.REST)
    assert result.is_success
    assert result.payload.creatives[0].action == CreativeAction.created
"""

from __future__ import annotations

from typing import Any

from tests.harness.transport import Transport, TransportResult


def assert_envelope(result: TransportResult, transport: Transport) -> None:
    """Assert transport-specific envelope shape is correct."""
    assert result.envelope.get("transport") == transport.value, (
        f"Expected envelope transport={transport.value}, got {result.envelope}"
    )

    if transport == Transport.REST:
        assert_rest_envelope(result)


def assert_rest_envelope(result: TransportResult, expected_status: int = 200) -> None:
    """Assert REST-specific envelope: HTTP status + content-type."""
    assert result.envelope.get("status_code") == expected_status, (
        f"Expected HTTP {expected_status}, got {result.envelope.get('status_code')}"
    )
    content_type = result.envelope.get("content_type", "")
    assert "application/json" in content_type, f"Expected JSON content-type, got {content_type}"


def assert_error_result(
    result: TransportResult,
    expected_type: type[Exception],
    match: str | None = None,
) -> None:
    """Assert result is an error of the expected type, optionally matching message."""
    assert result.is_error, f"Expected error but got success: {result.payload}"
    assert isinstance(result.error, expected_type), (
        f"Expected {expected_type.__name__}, got {type(result.error).__name__}: {result.error}"
    )
    if match is not None:
        import re

        assert re.search(match, str(result.error)), (
            f"Error message {str(result.error)!r} does not match pattern {match!r}"
        )


def assert_rejected(
    result: TransportResult,
    *,
    code: str | None = None,
    field: str | None = None,
    reason: str | None = None,
    message_contains: str | None = None,
) -> None:
    """Assert the buyer-visible wire envelope rejected the request.

    The reconstructed ``result.error`` is deliberately not authoritative:
    reconstruction is lossy and may retain privileged internal validation
    details that the sanitized wire correctly omits.

    Args:
        result: TransportResult from env.call_via()
        code: Expected error code (e.g., "VALIDATION_ERROR").
        field: Expected field name (e.g., "max_width", "agent_url").
        reason: Expected error reason (e.g., "Field required",
            "Input should be a valid integer"). This distinguishes
            "field missing" from "field has wrong type" on the same field.
        message_contains: Additional substring that must appear in the error.
    """
    assert result.is_error, f"Expected rejection but got success: {result.payload}"

    envelope = result.wire_error_envelope or result.synthesized_error_envelope
    assert isinstance(envelope, dict), f"Expected a wire error envelope, got: {envelope!r}"
    errors = envelope.get("errors") or []
    assert errors and all(isinstance(error, dict) for error in errors), (
        f"Expected buyer-visible errors[] objects, got: {envelope!r}"
    )
    adcp_error = envelope.get("adcp_error") or {}

    if code is not None:
        assert adcp_error.get("code") == code
        assert errors[0].get("code") == code

    if field is not None:
        wire_fields = [str(error.get("field") or "") for error in errors]
        assert any(actual == field or actual.endswith(f".{field}") for actual in wire_fields), (
            f"Expected field '{field}' in buyer-visible errors[], got: {wire_fields}"
        )

    if reason is not None:
        wire_messages = [str(error.get("message") or "") for error in errors]
        assert any(reason in message for message in wire_messages), (
            f"Expected reason '{reason}' in buyer-visible errors[], got: {wire_messages}"
        )

    if message_contains is not None:
        wire_messages = [str(error.get("message") or "") for error in errors]
        assert any(message_contains in message for message in wire_messages), (
            f"Expected '{message_contains}' in buyer-visible errors[], got: {wire_messages}"
        )


def assert_payload_field(
    result: TransportResult,
    field: str,
    expected: Any,
) -> None:
    """Assert a specific field on the payload matches expected value."""
    assert result.is_success, f"Expected success but got error: {result.error}"
    actual = getattr(result.payload, field)  # Let AttributeError propagate for typos
    assert actual == expected, f"payload.{field}: expected {expected!r}, got {actual!r}"
