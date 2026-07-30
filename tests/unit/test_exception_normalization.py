import pytest
from pydantic import ValidationError

from src.core.exceptions import (
    VALIDATION_ERROR_SUGGESTION,
    AdCPInvalidRequestError,
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
    assert exc_info.value.suggestion == VALIDATION_ERROR_SUGGESTION
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


def test_unexpected_keyword_argument_is_reported_as_unrecognized_field():
    """MCP call-signature errors use the same safe field label as Pydantic extras."""
    error = ValidationError.from_exception_data(
        title="call[list_creative_formats]",
        line_errors=[
            {
                "type": "unexpected_keyword_argument",
                "loc": (SECRET_BEARING_MESSAGE,),
                "input": SECRET_BEARING_MESSAGE,
            }
        ],
    )

    envelope = build_two_layer_error_envelope(safe_adcp_error(error))

    assert_no_secret_leak(envelope)
    assert envelope["errors"][0]["field"] == "unrecognized_field"
    assert envelope["errors"][0]["details"]["validation_errors"] == [
        {
            "loc": ["unrecognized_field"],
            "msg": "Extra field is not allowed by the AdCP request schema.",
            "type": "unexpected_keyword_argument",
        }
    ]


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
    """Explicitly untrusted typed text is scrubbed without changing retry semantics."""
    error = AdCPValidationError(
        SECRET_BEARING_MESSAGE,
        recovery="transient",
        _wire_safe_message=False,
    )

    envelope = build_two_layer_error_envelope(safe_adcp_error(error))

    assert envelope["adcp_error"]["recovery"] == "transient"
    assert envelope["errors"][0]["recovery"] == "transient"
    assert_no_secret_leak(envelope)


def test_untrusted_typed_validation_message_is_scrubbed():
    """Typed validation text is untrusted unless a caller explicitly opts in."""
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


def test_numeric_range_projection_retains_safe_schema_bound():
    """Numeric constraint metadata is actionable without echoing rejected input."""
    error = ValidationError.from_exception_data(
        title="Package",
        line_errors=[
            {
                "type": "greater_than_equal",
                "loc": ("budget",),
                "input": SECRET_BEARING_MESSAGE,
                "ctx": {"ge": 0},
            }
        ],
    )

    envelope = build_two_layer_error_envelope(safe_adcp_error(error))

    assert_no_secret_leak(envelope)
    assert envelope["errors"][0]["message"] == "Value must be greater than or equal to 0."


def test_deliberate_typed_validation_message_requires_explicit_wire_safe_opt_in():
    """Audited static business guidance remains actionable after explicit opt-in."""
    error = AdCPValidationError(
        "At least one field must be provided for update.",
        field="request",
        suggestion="Provide a field to update and resend.",
        _wire_safe_message=True,
    )

    envelope = build_two_layer_error_envelope(safe_adcp_error(error))

    assert envelope["errors"][0]["message"] == "At least one field must be provided for update."
    assert envelope["errors"][0]["suggestion"] == "Provide a field to update and resend."


def test_wire_safe_opt_in_governs_details_and_message_together():
    """``_wire_safe_message`` is ONE gate over BOTH untrusted text channels.

    ``message`` and ``details`` on a typed ``AdCPValidationError`` have the same
    provenance — the raise site builds both, from the same values, with the same
    (absent) audit. So they are trusted together or scrubbed together; there is no
    state where the raise site's prose is too untrusted to emit but its structured
    payload of the same values is fine.

    The scrubbed half is pinned by
    ``test_untrusted_typed_validation_message_is_scrubbed`` (``details`` absent).
    This pins the OTHER half, which was unenforced: an audited site that opts in
    keeps its structured diagnostics, so opting in — not weakening the scrub — is
    the sanctioned route to putting ``details`` on the wire.

    Why this needs an oracle at all: read from the scrub branch alone, the dropped
    ``details`` looks like an oversight (a reviewer read it exactly that way and
    proposed forwarding ``normalize_to_adcp_error(exc).details``). On that branch
    ``normalize_to_adcp_error`` returns the SAME typed instance it was given, so
    that forward would re-emit the very raise-site payload the branch exists to
    withhold — 8 of the 9 ``details=``-passing sites in ``src/`` sit on it.
    """
    audited = AdCPValidationError(
        "Budget must be positive.",
        field="budget",
        details={"rejected_value": -5, "minimum": 0},
        _wire_safe_message=True,
    )

    envelope = build_two_layer_error_envelope(safe_adcp_error(audited))

    # Spec 3.1.1 core/error.json documents details.rejected_value as the buyer's own
    # offending value, "echoed for buyer-side diagnostic clarity" — and VALIDATION_ERROR's
    # canonical suggestion ("review error details and fix field values") points AT it.
    assert envelope["errors"][0]["details"] == {"rejected_value": -5, "minimum": 0}
    assert envelope["adcp_error"]["details"] == {"rejected_value": -5, "minimum": 0}

    # Same raise site, same details, opt-in withdrawn -> both channels withheld together.
    unaudited = AdCPValidationError(
        "Budget must be positive.",
        field="budget",
        details={"rejected_value": -5, "minimum": 0},
    )

    scrubbed = build_two_layer_error_envelope(safe_adcp_error(unaudited))

    assert "details" not in scrubbed["errors"][0]
    assert "details" not in scrubbed["adcp_error"]
    assert scrubbed["errors"][0]["message"] != "Budget must be positive."


# A URL/token-shaped string standing in for "the buyer's own submitted value" in the four
# tests below. These tests prove TWO SEPARATE properties, not one — do not read the
# assertions as contradictory:
#   1. THIS value echoes to the wire — that's the expected, spec-sanctioned behavior (it's
#      the buyer's own data; they already have it — see safe_adcp_error's grounding comment).
#   2. No OTHER content — specifically nothing from SECRET_BEARING_MESSAGE's _SECRET_TOKENS,
#      standing in for genuine system/adapter secrets — ever rides along with it.
# Property 2 is only STATABLE if this constant is a different literal than
# SECRET_BEARING_MESSAGE: asserting "value X is present" and "value X's own tokens are
# absent" on the identical string is a contradiction, not a stronger test. Using a distinct
# literal here is what lets the test prove "this raise site interpolates ONLY the buyer's
# value, nothing adapter/system-sourced" — which is the actual security property that
# matters, not "this specific fixture never appears."
_BUYER_SUPPLIED_CREDENTIAL_SHAPED_VALUE = "https://example.com/callback?token=buyer-rotated-secret-789"


def test_duplicate_product_id_echoes_the_buyers_own_value_after_audited_opt_in():
    """Mirrors media_buy_create.py's duplicate-product_id raise site exactly.

    Proves the AdCP spec's sanctioned buyer-value echo (dist/schemas/3.1.1/core/error.json
    ``details.rejected_value``: "the offending value the buyer supplied, echoed for
    buyer-side diagnostic clarity") survives to the wire for a credential-/URL-shaped
    value, while confirming this exact template has no path for a genuine system secret
    to leak alongside it — the template only ever interpolates the buyer's own submitted
    product_ids, nothing adapter- or system-sourced.
    """
    duplicate_products = [_BUYER_SUPPLIED_CREDENTIAL_SHAPED_VALUE]
    error_msg = (
        f"Duplicate product_id(s) found in packages: {', '.join(duplicate_products)}. "
        "Each product can only be used once per media buy."
    )
    error = AdCPValidationError(
        error_msg,
        suggestion="Each package must reference a distinct product_id; remove the duplicate package or change its product_id.",
        _wire_safe_message=True,
    )

    envelope = build_two_layer_error_envelope(safe_adcp_error(error))

    assert _BUYER_SUPPLIED_CREDENTIAL_SHAPED_VALUE in envelope["errors"][0]["message"]
    assert_no_secret_leak(envelope)


def test_duplicate_product_id_without_wire_safe_opt_in_is_scrubbed():
    """Same buyer value, opt-in removed — proves the flag is load-bearing, not decorative."""
    error_msg = (
        f"Duplicate product_id(s) found in packages: {_BUYER_SUPPLIED_CREDENTIAL_SHAPED_VALUE}. "
        "Each product can only be used once per media buy."
    )
    error = AdCPValidationError(
        error_msg,
        suggestion="Each package must reference a distinct product_id; remove the duplicate package or change its product_id.",
    )

    envelope = build_two_layer_error_envelope(safe_adcp_error(error))

    assert _BUYER_SUPPLIED_CREDENTIAL_SHAPED_VALUE not in envelope["errors"][0]["message"]


def test_targeting_overlay_echoes_the_buyers_own_unknown_field_after_audited_opt_in():
    """Mirrors media_buy_create.py's targeting-overlay raise site exactly.

    ``validate_unknown_targeting_fields`` (the real production validator) reports
    ``model_extra`` keys — a duck-typed stub stands in for ``Targeting`` here because
    this environment's ``extra='forbid'`` config (CLAUDE.md's Schema Validation
    pattern) rejects an unknown field at construction time, before model_extra could
    ever be populated for a direct Targeting() construction; the validator itself
    accepts ``Any`` and only reads ``.model_extra``, so the stub exercises the same
    code the raise site calls.
    """
    from types import SimpleNamespace

    from src.services.targeting_capabilities import validate_unknown_targeting_fields

    stub = SimpleNamespace(model_extra={_BUYER_SUPPLIED_CREDENTIAL_SHAPED_VALUE: "irrelevant"})
    violations = validate_unknown_targeting_fields(stub)
    assert violations == [f"{_BUYER_SUPPLIED_CREDENTIAL_SHAPED_VALUE} is not a recognized targeting field"]

    error_msg = f"Targeting validation failed: {'; '.join(violations)}"
    error = AdCPInvalidRequestError(
        error_msg,
        suggestion="Check targeting constraints.",
        field="targeting_overlay",
        _wire_safe_message=True,
    )

    envelope = build_two_layer_error_envelope(safe_adcp_error(error))

    assert _BUYER_SUPPLIED_CREDENTIAL_SHAPED_VALUE in envelope["errors"][0]["message"]
    assert_no_secret_leak(envelope)


def test_targeting_overlay_without_wire_safe_opt_in_is_scrubbed():
    """Same buyer value, opt-in removed — proves the flag is load-bearing, not decorative."""
    violations = [f"{_BUYER_SUPPLIED_CREDENTIAL_SHAPED_VALUE} is not a recognized targeting field"]
    error_msg = f"Targeting validation failed: {'; '.join(violations)}"
    error = AdCPInvalidRequestError(
        error_msg,
        suggestion="Check targeting constraints.",
        field="targeting_overlay",
    )

    envelope = build_two_layer_error_envelope(safe_adcp_error(error))

    assert _BUYER_SUPPLIED_CREDENTIAL_SHAPED_VALUE not in envelope["errors"][0]["message"]
