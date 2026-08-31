"""Regression tests for the error-path wire oracle (tests.bdd.steps._outcome_helpers.wire_error_dict).

Retargeted from the pre-#1858 ``wire_error_envelope`` oracle to
``wire_error_dict`` (the #1858 rework split one function into
``wire_error_dict`` — loud guard + IMPL/no-wire-by-design fallback to
``synthesized_error_envelope`` — and ``wire_error_envelope_or_none`` — no
guard, no fallback). ``wire_error_dict`` is the direct behavioral successor
for these cases: same loud guard, same ``wire_capture_unavailable``
exemption, plus a synthesized-envelope fallback so it never silently returns
``None`` on a legitimate no-real-wire case.
"""

from unittest.mock import MagicMock

import pytest

from tests.bdd.steps._outcome_helpers import wire_error_dict
from tests.harness.transport import Transport


@pytest.mark.parametrize("transport", [Transport.MCP, Transport.A2A, Transport.REST])
def test_real_transport_missing_capture_raises(transport):
    """A real-wire transport reporting no envelope, with no exemption, is a dispatcher bug."""
    result = MagicMock(wire_error_envelope=None, wire_capture_unavailable=False, synthesized_error_envelope=None)

    with pytest.raises(AssertionError, match="wire_error_envelope missing"):
        wire_error_dict({"transport": transport, "result": result})


def test_a2a_direct_raw_dispatch_returns_synthesized_without_raising():
    """A2A's documented direct-raw mode (e.g. CreativeSyncEnv) never captures wire — by design.

    Regression guard: an earlier version of this oracle could not distinguish
    "this dispatch mode never promised wire" from "the dispatcher failed to
    stash it", so every direct-raw A2A error assertion would have raised a
    false "dispatcher regression" alarm the moment it was used. Unlike the
    pre-#1858 ``wire_error_envelope`` (which returned ``None`` here),
    ``wire_error_dict`` falls back to the synthesized envelope — its contract
    is to always carry SOME envelope on an error dispatch.
    """
    envelope = {"adcp_error": {"code": "VALIDATION_ERROR"}}
    result = MagicMock(wire_error_envelope=None, wire_capture_unavailable=True, synthesized_error_envelope=envelope)

    assert wire_error_dict({"transport": Transport.A2A, "result": result}) == envelope


def test_a2a_full_pipeline_missing_capture_still_raises():
    """The exemption is narrow: a full-pipeline A2A dispatch still owes a real envelope."""
    result = MagicMock(wire_error_envelope=None, wire_capture_unavailable=False, synthesized_error_envelope=None)

    with pytest.raises(AssertionError, match="wire_error_envelope missing"):
        wire_error_dict({"transport": Transport.A2A, "result": result})


def test_captured_envelope_is_returned():
    envelope = {"adcp_error": {"code": "VALIDATION_ERROR"}}
    result = MagicMock(wire_error_envelope=envelope, wire_capture_unavailable=False)

    assert wire_error_dict({"transport": Transport.REST, "result": result}) == envelope


def test_impl_transport_returns_synthesized_without_raising():
    envelope = {"adcp_error": {"code": "VALIDATION_ERROR"}}
    result = MagicMock(wire_error_envelope=None, wire_capture_unavailable=False, synthesized_error_envelope=envelope)

    assert wire_error_dict({"transport": Transport.IMPL, "result": result}) == envelope


def test_pre_dispatch_rejection_raises_with_no_synthesized_fallback():
    """No ``result`` at all (rejected before reaching any transport) has nothing to fall back on.

    ``ctx.get("transport")`` not being ``Transport.IMPL`` no longer matters
    once ``result`` itself is absent: ``wire_error_dict`` treats a missing
    ``result`` as having no envelope of any kind, so the fallback's own
    assertion fires instead of silently returning ``None``.
    """
    with pytest.raises(AssertionError, match="No wire_error_envelope or synthesized_error_envelope"):
        wire_error_dict({"transport": Transport.A2A, "result": None})
    with pytest.raises(AssertionError, match="No wire_error_envelope or synthesized_error_envelope"):
        wire_error_dict({"transport": Transport.A2A})
