"""Regression tests for real-versus-synthesized harness error envelopes."""

from typing import Any

from src.core.exceptions import AdCPInvalidRequestError
from tests.harness.dispatchers import A2ADispatcher


class _RaisingA2AEnv:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def call_a2a(self, **kwargs: Any) -> None:
        raise self.error


def test_a2a_missing_wire_capture_is_only_exposed_as_synthesized() -> None:
    """A diagnostic fallback must not masquerade as observed A2A wire bytes."""
    result = A2ADispatcher().dispatch(_RaisingA2AEnv(AdCPInvalidRequestError("bad request")))

    assert result.wire_error_envelope is None
    assert result.synthesized_error_envelope is not None
    result.assert_synthesized_error("INVALID_REQUEST")


def test_a2a_real_wire_capture_takes_precedence_over_synthesized() -> None:
    """A stashed artifact envelope is retained verbatim as the wire authority."""
    error = AdCPInvalidRequestError("bad request")
    wire_error = {
        "code": "INVALID_REQUEST",
        "message": "bad request",
        "recovery": "correctable",
    }
    wire = {"errors": [wire_error], "adcp_error": dict(wire_error)}
    error._wire_error_envelope = wire  # type: ignore[attr-defined]

    result = A2ADispatcher().dispatch(_RaisingA2AEnv(error))

    assert result.wire_error_envelope is wire
    assert result.synthesized_error_envelope is None
    result.assert_wire_error("INVALID_REQUEST")
