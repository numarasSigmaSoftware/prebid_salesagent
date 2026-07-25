import pytest
from pydantic import ValidationError

from src.core.exceptions import (
    AdCPValidationError,
    build_two_layer_error_envelope,
    normalize_to_adcp_error,
    safe_adcp_error,
)
from src.core.validation_helpers import adcp_validation_boundary
from tests.helpers import assert_no_raw_validation_leak
from tests.helpers.secret_scrub import SECRET_BEARING_MESSAGE, assert_no_secret_leak


def test_pydantic_validation_error_normalization_is_structured_and_sanitized():
    error = ValidationError.from_exception_data(
        title="call[create_media_buy]",
        line_errors=[
            {
                "type": "missing",
                "loc": ("packages", 0, "product_id"),
                "input": {"secret": "buyer-input"},
            }
        ],
    )

    normalized = normalize_to_adcp_error(error)

    assert isinstance(normalized, AdCPValidationError)
    assert normalized.message == "Required field is missing."
    assert normalized.field == "packages[0].product_id"
    assert normalized.details == {
        "validation_errors": [
            {
                "loc": ["packages", 0, "product_id"],
                "msg": "Required field is missing.",
                "type": "missing",
            }
        ]
    }
    assert "buyer-input" not in normalized.message
    assert_no_raw_validation_leak(normalized.message)


def test_a2a_validation_boundary_preserves_contextual_error_format():
    error = ValidationError.from_exception_data(
        title="CreateMediaBuyRequest",
        line_errors=[
            {
                "type": "missing",
                "loc": ("packages", 0, "product_id"),
                "input": {"secret": "buyer-input"},
            }
        ],
    )

    with pytest.raises(AdCPValidationError) as exc_info:
        with adcp_validation_boundary():
            raise error

    assert "Invalid parameters:" in exc_info.value.message
    assert "packages.0.product_id: Required field is missing" in exc_info.value.message
    assert exc_info.value.field == "packages[0].product_id"
    assert exc_info.value.suggestion == ("Provide the required 'packages[0].product_id' field and resend the request.")
    assert exc_info.value.details == {
        "validation_errors": [
            {
                "loc": ["packages", 0, "product_id"],
                "msg": "Required field is missing.",
                "type": "missing",
            }
        ]
    }
    assert "buyer-input" not in exc_info.value.message
    assert_no_raw_validation_leak(exc_info.value.message)


def test_boundary_wrapped_extra_field_value_is_wire_safe():
    """A typed boundary translation must not become a sanitizer bypass."""
    error = ValidationError.from_exception_data(
        title="Request",
        line_errors=[
            {
                "type": "extra_forbidden",
                "loc": (SECRET_BEARING_MESSAGE,),
                "input": SECRET_BEARING_MESSAGE,
            }
        ],
    )

    with pytest.raises(AdCPValidationError) as exc_info:
        with adcp_validation_boundary(context="request"):
            raise error

    envelope = build_two_layer_error_envelope(safe_adcp_error(exc_info.value))
    assert_no_secret_leak(envelope)
    assert envelope["errors"][0]["field"] == "unrecognized_field"
    assert envelope["errors"][0]["details"] == {
        "validation_errors": [
            {
                "loc": ["unrecognized_field"],
                "msg": "Extra field is not allowed by the AdCP request schema.",
                "type": "extra_forbidden",
            }
        ]
    }


def test_raw_pydantic_projection_preserves_safe_multi_field_details():
    """Raw TypeAdapter validation keeps every field without raw values."""
    error = ValidationError.from_exception_data(
        title="call[list_creative_formats]",
        line_errors=[
            {"type": "int_parsing", "loc": ("max_width",), "input": SECRET_BEARING_MESSAGE},
            {"type": "int_parsing", "loc": ("min_height",), "input": SECRET_BEARING_MESSAGE},
        ],
    )

    envelope = build_two_layer_error_envelope(safe_adcp_error(error))
    assert_no_secret_leak(envelope)
    assert envelope["errors"][0]["field"] == "max_width"
    assert envelope["errors"][0]["details"]["validation_errors"] == [
        {"loc": ["max_width"], "msg": "Expected an integer value.", "type": "int_parsing"},
        {"loc": ["min_height"], "msg": "Expected an integer value.", "type": "int_parsing"},
    ]


def test_mapping_key_location_is_redacted():
    """Non-schema mapping keys are request data, not safe field metadata."""
    error = ValidationError.from_exception_data(
        title="Targeting",
        line_errors=[
            {
                "type": "string_type",
                "loc": ("key_value_pairs", SECRET_BEARING_MESSAGE),
                "input": {"bad": "value"},
            }
        ],
    )

    envelope = build_two_layer_error_envelope(safe_adcp_error(error))
    assert_no_secret_leak(envelope)
    assert envelope["errors"][0]["field"] == "key_value_pairs.nested_field"
    assert envelope["errors"][0]["details"]["validation_errors"][0]["loc"] == [
        "key_value_pairs",
        "nested_field",
    ]


def test_identifier_shaped_mapping_key_location_is_redacted():
    """Mapping-key safety cannot depend on punctuation in the secret."""
    secret_key = "hunter2"
    error = ValidationError.from_exception_data(
        title="Targeting",
        line_errors=[
            {
                "type": "string_type",
                "loc": ("key_value_pairs", secret_key),
                "input": {"bad": "value"},
            }
        ],
    )

    envelope = build_two_layer_error_envelope(safe_adcp_error(error))
    assert secret_key not in str(envelope)
    assert envelope["errors"][0]["field"] == "key_value_pairs.nested_field"


def test_untrusted_typed_validation_preserves_transient_recovery():
    """Sanitizing request-derived text must not alter retry semantics."""
    error = AdCPValidationError(SECRET_BEARING_MESSAGE, recovery="transient")

    envelope = build_two_layer_error_envelope(safe_adcp_error(error))

    assert envelope["adcp_error"]["recovery"] == "transient"
    assert envelope["errors"][0]["recovery"] == "transient"
    assert_no_secret_leak(envelope)


def test_untrusted_typed_validation_message_is_scrubbed():
    """Typing an exception does not make interpolated request data trustworthy."""
    error = AdCPValidationError(
        f"Invalid created_after date format: {SECRET_BEARING_MESSAGE}",
        field="created_after",
        suggestion=f"Remove {SECRET_BEARING_MESSAGE} and retry",
        details={"rejected": SECRET_BEARING_MESSAGE},
    )

    envelope = build_two_layer_error_envelope(safe_adcp_error(error))
    assert_no_secret_leak(envelope)
    assert envelope["errors"][0]["field"] == "created_after"
    assert "details" not in envelope["errors"][0]


def test_string_length_projection_retains_numeric_schema_constraint():
    """Safe numeric constraint metadata remains actionable on the wire."""
    error = ValidationError.from_exception_data(
        title="WebhookAuth",
        line_errors=[
            {
                "type": "string_too_short",
                "loc": ("authentication", "credentials"),
                "input": SECRET_BEARING_MESSAGE,
                "ctx": {"min_length": 32},
            }
        ],
    )

    envelope = build_two_layer_error_envelope(safe_adcp_error(error))
    assert_no_secret_leak(envelope)
    assert "at least 32 characters" in envelope["errors"][0]["message"]
