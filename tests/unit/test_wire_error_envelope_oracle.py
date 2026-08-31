"""Regression tests for the error-path wire oracle (tests.bdd.steps._outcome_helpers).

The single ``wire_error_envelope`` accessor this module originally graded was
superseded by a two-accessor split:

* :func:`~tests.bdd.steps._outcome_helpers.wire_error_dict` — the loud guard
  (a real-wire transport that captured no envelope raises) plus the narrow
  ``wire_capture_unavailable`` exemption and an IMPL/no-wire fallback to
  ``synthesized_error_envelope``. This is the semantic successor: it is the
  only accessor that carries the guard these regression tests exist for.
* :func:`~tests.bdd.steps._outcome_helpers.wire_error_envelope_or_none` — the
  tolerant read (real envelope or ``None``, never raises), for callers that
  must distinguish "a real envelope was captured" before delegating to
  ``TransportResult.assert_wire_error``.

Two contract differences the split introduced, recorded here rather than
papered over:

1. Where the old accessor returned ``None`` for the no-wire cases (the
   ``wire_capture_unavailable`` exemption and IMPL), ``wire_error_dict``
   returns the ``synthesized_error_envelope`` fallback. The tests below pin
   that identity, which is strictly more than the old ``is None`` assertion.
2. "No ``result`` at all" (rejected before dispatch reached a transport) was a
   third legitimate ``None`` case on the old accessor. ``wire_error_dict``
   does not exempt it — it raises. The tolerant path for that case moved to
   ``wire_error_envelope_or_none``; both halves are pinned below.
"""

from unittest.mock import MagicMock

import pytest

from tests.bdd.steps._outcome_helpers import wire_error_dict, wire_error_envelope_or_none
from tests.harness.transport import Transport


@pytest.mark.parametrize("transport", [Transport.MCP, Transport.A2A, Transport.REST])
def test_real_transport_missing_capture_raises(transport):
    """A real-wire transport reporting no envelope, with no exemption, is a dispatcher bug."""
    result = MagicMock(wire_error_envelope=None, wire_capture_unavailable=False)

    with pytest.raises(AssertionError, match="wire_error_envelope missing"):
        wire_error_dict({"transport": transport, "result": result})


def test_a2a_direct_raw_dispatch_falls_back_to_synthesized_without_raising():
    """A2A's documented direct-raw mode (e.g. CreativeSyncEnv) never captures wire — by design.

    Regression guard: an earlier version of this oracle could not distinguish
    "this dispatch mode never promised wire" from "the dispatcher failed to
    stash it", so every direct-raw A2A error assertion would have raised a
    false "dispatcher regression" alarm the moment it was used.
    """
    synthesized = {"adcp_error": {"code": "SYNTHESIZED"}}
    result = MagicMock(
        wire_error_envelope=None,
        wire_capture_unavailable=True,
        synthesized_error_envelope=synthesized,
    )

    assert wire_error_dict({"transport": Transport.A2A, "result": result}) == synthesized


def test_a2a_full_pipeline_missing_capture_still_raises():
    """The exemption is narrow: a full-pipeline A2A dispatch still owes a real envelope."""
    result = MagicMock(wire_error_envelope=None, wire_capture_unavailable=False)

    with pytest.raises(AssertionError, match="wire_error_envelope missing"):
        wire_error_dict({"transport": Transport.A2A, "result": result})


def test_captured_envelope_is_returned():
    envelope = {"adcp_error": {"code": "VALIDATION_ERROR"}}
    result = MagicMock(wire_error_envelope=envelope, wire_capture_unavailable=False)

    assert wire_error_dict({"transport": Transport.REST, "result": result}) == envelope


def test_impl_transport_falls_back_to_synthesized_without_raising():
    """IMPL has no wire by definition — the synthesized envelope stands in, no guard trip."""
    synthesized = {"adcp_error": {"code": "SYNTHESIZED"}}
    result = MagicMock(
        wire_error_envelope=None,
        wire_capture_unavailable=False,
        synthesized_error_envelope=synthesized,
    )

    assert wire_error_dict({"transport": Transport.IMPL, "result": result}) == synthesized


def test_pre_dispatch_rejection_trips_the_guard_on_the_strict_accessor():
    """No ``result`` at all is NOT an exemption on ``wire_error_dict`` — it raises loudly.

    Contract difference (2) in the module docstring: the old single accessor
    treated "rejected before dispatch reached a transport" as a legitimate
    ``None``. The strict accessor does not, so a real-wire scenario that never
    produced a result fails loudly instead of silently asserting nothing.
    """
    with pytest.raises(AssertionError, match="wire_error_envelope missing"):
        wire_error_dict({"transport": Transport.A2A, "result": None})

    with pytest.raises(AssertionError, match="wire_error_envelope missing"):
        wire_error_dict({"transport": Transport.A2A})


def test_pre_dispatch_rejection_returns_none_on_the_tolerant_accessor():
    """The tolerant read is where callers get ``None`` for a pre-dispatch rejection.

    Preserves what the retired accessor's third exemption proved: a caller that
    must fall back to the reconstructed ``ctx['error']`` has an accessor that
    reports "no real envelope" without raising.
    """
    assert wire_error_envelope_or_none({"transport": Transport.A2A, "result": None}) is None
    assert wire_error_envelope_or_none({"transport": Transport.A2A}) is None


def test_tolerant_accessor_never_synthesizes():
    """The tolerant read reports the REAL envelope or ``None`` — never the synthesized fallback."""
    result = MagicMock(
        wire_error_envelope=None,
        wire_capture_unavailable=True,
        synthesized_error_envelope={"adcp_error": {"code": "SYNTHESIZED"}},
    )

    assert wire_error_envelope_or_none({"transport": Transport.A2A, "result": result}) is None
