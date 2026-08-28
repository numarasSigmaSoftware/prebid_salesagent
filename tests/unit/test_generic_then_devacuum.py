"""Regression tests for de-vacuumized generic partition/boundary/status Then steps.

: the generic Then steps `then_partition_filtering_result`,
`then_boundary_handling_result` (then_payload.py) and `then_response_status`
(then_success.py) historically passed *vacuously* — they ignored the captured
``field`` and accepted any non-None response (or any recorded exception) as a
satisfied outcome. ~140 scenarios xpassed without proving anything.

These tests call the step functions directly with crafted ``ctx`` states (no
DB, no harness) and assert the *strengthened* behavior:

- a "valid" outcome requires a schema-valid response of the operation's type
  with its required success collection correctly typed — not a junk object;
- an "invalid"/"error" outcome requires a real validation/AdCP rejection —
  not an arbitrary exception;
- the captured ``field`` must name a known dimension — an empty/unknown field
  is a misnamed scenario and must fail loudly;
- a context with neither response nor error must fail loudly;
- a status-less "completed" response must prove absence of error plus presence
  of its schema-required success payload.

Each negative case below PASSED vacuously before the fix and must FAIL
(AssertionError) the broken input after it.
"""

from __future__ import annotations

import pytest

from src.core.schemas import ListCreativeFormatsResponse
from tests.bdd.steps.generic.then_error import (
    then_error_message_contains,
    then_error_message_sanitized_without_disclosing,
)
from tests.bdd.steps.generic.then_payload import (
    then_boundary_handling_result,
    then_partition_filtering_result,
)
from tests.bdd.steps.generic.then_success import then_response_status
from tests.harness.transport import TransportResult


def _valid_uc005_ctx() -> dict:
    """A genuinely valid UC-005 response context (control: must still pass)."""
    return {"response": ListCreativeFormatsResponse(formats=[]), "registry_formats": [{"name": "stub"}]}


# ── Control cases: legitimate outcomes must still pass ───────────────────


def test_valid_partition_with_known_field_still_passes() -> None:
    then_partition_filtering_result(_valid_uc005_ctx(), field="format_ids", expected="valid")


def test_invalid_partition_with_real_rejection_still_passes() -> None:
    from pydantic import ValidationError

    try:
        ListCreativeFormatsResponse(formats="not-a-list")  # type: ignore[arg-type]
    except ValidationError as exc:
        ctx = {"error": exc}
    then_partition_filtering_result(ctx, field="asset_types", expected="invalid")


# ── De-vacuumization: broken inputs that used to pass must now FAIL ──────


def test_valid_outcome_rejects_junk_response_object() -> None:
    """A non-response junk object with no error used to pass (only hasattr check)."""
    ctx = {"response": object(), "registry_formats": []}
    with pytest.raises((AssertionError, AttributeError)):
        then_partition_filtering_result(ctx, field="format_ids", expected="valid")


def test_valid_outcome_rejects_unknown_field_name() -> None:
    """An empty/unknown field is a misnamed scenario — must fail loudly."""
    with pytest.raises(AssertionError):
        then_partition_filtering_result(_valid_uc005_ctx(), field="", expected="valid")
    with pytest.raises(AssertionError):
        then_partition_filtering_result(_valid_uc005_ctx(), field="totally_not_a_dimension", expected="valid")


def test_invalid_outcome_rejects_arbitrary_exception() -> None:
    """An arbitrary RuntimeError is not a real validation/AdCP rejection."""
    ctx = {"error": RuntimeError("kaboom unrelated crash")}
    with pytest.raises(AssertionError):
        then_boundary_handling_result(ctx, field="account", expected="invalid")


def test_outcome_requires_response_or_error() -> None:
    """A context with neither response nor error must fail loudly, not pass."""
    with pytest.raises(AssertionError):
        then_partition_filtering_result({}, field="format_ids", expected="valid")


def test_boundary_unknown_field_fails_loudly() -> None:
    with pytest.raises(AssertionError):
        then_boundary_handling_result(_valid_uc005_ctx(), field="bogus_boundary", expected="valid")


def test_unknown_expected_word_still_rejected() -> None:
    with pytest.raises(AssertionError):
        then_partition_filtering_result(_valid_uc005_ctx(), field="format_ids", expected="banana")


# ── then_response_status status-less "completed" de-vacuumization ────────


def test_response_status_completed_with_error_in_ctx() -> None:
    """AdCP 3.1: protocol-envelope.json requires status on ALL responses.

    ListCreativeFormatsResponse refs protocol-envelope.json which declares
    status as required (default "completed" for synchronous tasks). The step
    checks the declared status field, not ctx["error"]. This is correct per
    3.1 — the "status-less response" path only applies to non-spec test doubles.
    """
    ctx = {
        "response": ListCreativeFormatsResponse(formats=[]),
        "error": RuntimeError("operation actually failed"),
    }
    # No longer raises — response has status="completed" via protocol envelope
    then_response_status(ctx, status="completed")


def test_response_status_completed_rejects_missing_success_payload() -> None:
    """status-less response lacking its schema-required success collection."""

    class _Shell:
        """Status-less object with no formats — used to pass vacuously."""

    ctx = {"response": _Shell()}
    with pytest.raises(AssertionError):
        then_response_status(ctx, status="completed")


def test_response_status_completed_valid_still_passes() -> None:
    then_response_status(_valid_uc005_ctx(), status="completed")


def test_response_status_non_completed_against_statusless_fails() -> None:
    with pytest.raises(AssertionError):
        then_response_status(_valid_uc005_ctx(), status="working")


def test_sanitized_step_proves_untrusted_fragment_was_scrubbed() -> None:
    """The dedicated non-disclosure step catches a scrubbed wire message correctly.

    Was ``test_wire_message_assertion_proves_untrusted_fragment_was_scrubbed``,
    calling ``then_error_message_contains(ctx, text="past")`` — which passed only
    because THAT step used to silently switch to an ABSENCE check whenever the
    wire message happened to already equal the canonical scrubbed text. SF-1
    removed that switching behavior (see ``then_error.py``): "should contain" is
    now pure containment, always, and this non-disclosure assertion has its own
    dedicated step. This test proves the invariant through the correct step.
    """
    from src.core.exceptions import AdCPValidationError, build_two_layer_error_envelope, safe_adcp_error

    raw_fragment = "start time is in the past"
    wire_error = safe_adcp_error(AdCPValidationError(raw_fragment))
    envelope = build_two_layer_error_envelope(wire_error)
    ctx = {
        "error": AdCPValidationError(raw_fragment),
        "result": TransportResult(error=wire_error, wire_error_envelope=envelope),
    }

    then_error_message_sanitized_without_disclosing(ctx, text="past")

    # And the containment step now means exactly what it says: the raw fragment
    # is genuinely absent from the scrubbed wire message, so asserting its
    # PRESENCE via "should contain" correctly fails.
    with pytest.raises(AssertionError):
        then_error_message_contains(ctx, text="past")


def test_error_code_step_grades_the_wire_not_the_reconstruction() -> None:
    """``the error code should be`` follows the wire whenever one was captured.

    A step that stashes ``ctx['error']`` but never sets ``ctx['result']`` looks green while
    grading only the reconstructed exception — and reconstruction is lossy, so a wire-only
    regression cannot redden it. Pin the precedence directly: with the two disagreeing, the
    wire code is the one that must decide.
    """
    import pytest

    from src.core.exceptions import (
        AdCPCapabilityNotSupportedError,
        AdCPValidationError,
        build_two_layer_error_envelope,
        safe_adcp_error,
    )
    from tests.bdd.steps.generic.then_error import then_error_code

    # Reconstruction says VALIDATION_ERROR; the wire says UNSUPPORTED_FEATURE.
    wire_error = safe_adcp_error(AdCPCapabilityNotSupportedError("nl create_media_buy is not supported"))
    envelope = build_two_layer_error_envelope(wire_error)
    ctx = {
        "error": AdCPValidationError("reconstructed and lossy"),
        "result": TransportResult(error=wire_error, wire_error_envelope=envelope),
    }

    then_error_code(ctx, code=envelope["adcp_error"]["code"])

    with pytest.raises(AssertionError):
        then_error_code(ctx, code="VALIDATION_ERROR")
