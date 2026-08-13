"""Known-bad self-tests for the shared secret-leak oracle.

``assert_no_secret_leak`` backs ~30 leak assertions across the A2A, webhook, and boundary
suites. Without a self-test the oracle can be silently defanged — emptying ``_SECRET_TOKENS``,
or dropping a single token, leaves every one of those callers GREEN while proving nothing.

The expectations here are stated INDEPENDENTLY of the constants they grade (see
``_EXPECTED_LEAK_FRAGMENTS``): a test parametrized over ``_SECRET_TOKENS`` would delete its own
case when a token was removed rather than reddening, which is no oracle at all.
"""

import pytest

from tests.helpers.secret_scrub import (
    _EXPECTED_SANITIZED_MESSAGE,
    _SECRET_TOKENS,
    SECRET_BEARING_MESSAGE,
    assert_no_secret_leak,
    assert_sanitized_wire_error,
)

# INDEPENDENT restatement of what the oracle must detect — deliberately NOT derived from
# ``_SECRET_TOKENS``. Parametrizing over the constant under test is circular: deleting a token
# would delete its own test case instead of reddening one, so the suite would stay green while
# the oracle silently weakened. Declared here, every drop from the shared set fails
# ``test_shared_token_set_matches_the_independent_expectation`` below.
#
# Not listed: the ``prod`` database name from the connection string. It is a real secret
# fragment but unusable as a bare substring — it matches the legitimate word "product", which
# appears throughout this codebase's buyer-facing copy, so tokenizing it would fire on clean
# messages now and for as long as "product" stays in that copy. The surrounding
# ``postgresql://`` and ``db.internal`` fragments already catch any leak of that connection
# string. The ``svc`` username has no such collision (verified: no hit for "svc" anywhere in
# src/) and IS tokenized below.
_EXPECTED_LEAK_FRAGMENTS = (
    "hunter2",  # password
    "postgresql://",  # connection-string scheme
    "svc",  # connection-string username
    "db.internal",  # internal hostname
    "TOKEN=abc123",  # bearer credential
    "SELECT",  # inline SQL
    "principals",  # internal table name
)


def test_oracle_rejects_the_secret_bearing_message_as_a_string():
    """The canonical leak payload must trip the oracle in its string form.

    Reddens if ``_SECRET_TOKENS`` is emptied — the exact defang the oracle can't otherwise see.
    """
    with pytest.raises(AssertionError, match="leaked to the buyer-facing wire"):
        assert_no_secret_leak(SECRET_BEARING_MESSAGE)


def test_oracle_rejects_the_secret_bearing_message_inside_an_envelope_dict():
    """Same payload nested in a two-layer envelope — the shape most callers actually pass.

    The oracle JSON-serializes non-str input, so a secret buried in a nested error object must
    be caught just as it is in a flat string.
    """
    envelope = {
        "adcp_error": {"code": "SERVICE_UNAVAILABLE", "message": SECRET_BEARING_MESSAGE},
        "errors": [{"code": "SERVICE_UNAVAILABLE", "message": SECRET_BEARING_MESSAGE}],
    }
    with pytest.raises(AssertionError, match="leaked to the buyer-facing wire"):
        assert_no_secret_leak(envelope)


@pytest.mark.parametrize("fragment", _EXPECTED_LEAK_FRAGMENTS)
def test_oracle_detects_every_expected_fragment_individually(fragment):
    """Each fragment must trip the oracle ON ITS OWN.

    Parametrized over the INDEPENDENT list, not ``_SECRET_TOKENS``: dropping a token from the
    shared set leaves this case standing and RED, which is the whole point. A blanket
    "the whole message trips it" check would stay green with a single token removed, because
    the remaining fragments would still fire.
    """
    with pytest.raises(AssertionError, match="leaked to the buyer-facing wire"):
        assert_no_secret_leak(f"prefix {fragment} suffix")


def test_shared_token_set_matches_the_independent_expectation():
    """The shared ``_SECRET_TOKENS`` must equal the independent list above.

    This is the tie that makes the parametrization non-circular: a token added to the shared set
    without updating the expectation (or removed from it) fails here, so the two literals cannot
    drift apart silently.
    """
    assert set(_SECRET_TOKENS) == set(_EXPECTED_LEAK_FRAGMENTS), (
        "shared token set drifted from the independent expectation — update both deliberately"
    )


@pytest.mark.parametrize("fragment", _EXPECTED_LEAK_FRAGMENTS)
def test_canonical_message_actually_carries_every_fragment(fragment):
    """``SECRET_BEARING_MESSAGE`` must literally contain each fragment it is supposed to model.

    The message and the token set are two independent literals; nothing else ties them. Without
    this, the canonical payload could stop carrying (say) the bearer token while every caller
    that injects it kept passing — proving the scrub only against the fragments that remained.
    """
    assert fragment in SECRET_BEARING_MESSAGE, (
        f"{fragment!r} is expected to leak but is absent from SECRET_BEARING_MESSAGE"
    )


def test_oracle_passes_a_clean_blob():
    """The counter-control: a scrubbed message must NOT trip the oracle.

    Without this, an oracle that raised unconditionally would satisfy every known-bad test
    above while making all ~30 callers vacuous in the opposite direction.
    """
    assert_no_secret_leak("An internal error occurred while processing the request.")
    assert_no_secret_leak({"adcp_error": {"code": "SERVICE_UNAVAILABLE", "message": "Request failed."}})


def test_oracle_refuses_none_rather_than_passing_vacuously():
    """An absent value must fail loudly: a caller asserting on a field production stopped
    populating would otherwise "pass" while proving nothing."""
    with pytest.raises(AssertionError, match="cannot prove a scrub"):
        assert_no_secret_leak(None)


def test_expected_sanitized_message_keys_match_production_table():
    """``_EXPECTED_SANITIZED_MESSAGE``'s key set must equal ``_SANITIZED_BY_WIRE_CODE``'s.

    Key-set equality ONLY — never value equality, which would reintroduce the exact
    circularity ``_EXPECTED_SANITIZED_MESSAGE`` exists to avoid (assert_sanitized_wire_error's
    message check is independently pinned literal text, deliberately not read from
    production; see that function's docstring). Without this, a code added to
    ``_SANITIZED_BY_WIRE_CODE`` without a matching literal added here wouldn't surface until
    some later scenario happened to call ``assert_sanitized_wire_error`` for that new code —
    this test catches the gap immediately, the same role
    ``test_shared_token_set_matches_the_independent_expectation`` plays for ``_SECRET_TOKENS``.
    """
    from src.core.exceptions import _SANITIZED_BY_WIRE_CODE

    assert set(_EXPECTED_SANITIZED_MESSAGE.keys()) == set(_SANITIZED_BY_WIRE_CODE.keys()), (
        "_EXPECTED_SANITIZED_MESSAGE (tests/helpers/secret_scrub.py) drifted from "
        "_SANITIZED_BY_WIRE_CODE (src/core/exceptions.py) — update both deliberately"
    )


# ---------------------------------------------------------------------------
# Known-bad self-tests for ``assert_sanitized_wire_error``
#
# The sibling of the ``assert_no_secret_leak`` block above, which this file
# previously lacked. ``assert_sanitized_wire_error`` pins two EQUALITIES — the
# canonical scrubbed message and the pinned suggestion — on both envelope layers.
# Weakening either to a truthiness check (``assert error.get("message")``) leaves
# every one of its ~160 callers green while proving only that SOME text is present:
# an envelope echoing the raw, unscrubbed error would satisfy it. The cases below
# feed the oracle values that are non-empty but WRONG, so a truthiness weakening
# reddens here.
# ---------------------------------------------------------------------------

_SANITIZED_CASE_CODE = "VALIDATION_ERROR"


def _canonical_sanitized_envelope(code: str = _SANITIZED_CASE_CODE) -> dict:
    """A two-layer envelope that ``assert_sanitized_wire_error`` must ACCEPT."""
    from tests.helpers.pinned_schema import pinned_error_code_suggestion

    layer = {
        "code": code,
        "message": _EXPECTED_SANITIZED_MESSAGE[code],
        "suggestion": pinned_error_code_suggestion(code),
    }
    return {"adcp_error": dict(layer), "errors": [dict(layer)]}


def test_sanitized_oracle_accepts_the_canonical_envelope():
    """Counter-control: the canonical scrub must PASS.

    Without it, an oracle that raised unconditionally would satisfy every known-bad
    case below while making all of its callers vacuous in the opposite direction.
    """
    assert_sanitized_wire_error(_canonical_sanitized_envelope(), _SANITIZED_CASE_CODE)


@pytest.mark.parametrize("layer", ["adcp_error", "errors"])
@pytest.mark.parametrize(
    ("field", "wrong_value", "expected_match"),
    [
        (
            "message",
            "Something went wrong while validating your request.",
            "did not use the canonical VALIDATION_ERROR scrub",
        ),
        (
            "suggestion",
            "Please try again later or contact support.",
            "did not use the canonical VALIDATION_ERROR guidance",
        ),
    ],
)
def test_sanitized_oracle_rejects_wrong_but_non_empty_text(layer, field, wrong_value, expected_match):
    """A plausible-but-wrong message/suggestion must trip the oracle, on EITHER layer.

    Reddens if the corresponding equality pin is weakened to truthiness — the exact
    defang the oracle cannot otherwise see, since every value fed here is non-empty.
    Parametrized across both layers because asserting only ``adcp_error`` would let a
    divergence between the two layers pass every call site.
    """
    envelope = _canonical_sanitized_envelope()
    if layer == "adcp_error":
        envelope["adcp_error"][field] = wrong_value
    else:
        envelope["errors"][0][field] = wrong_value

    with pytest.raises(AssertionError, match=expected_match):
        assert_sanitized_wire_error(envelope, _SANITIZED_CASE_CODE)


def test_sanitized_oracle_rejects_a_leaked_rejected_fragment():
    """``rejected_fragments`` must actually be scanned, case-insensitively."""
    envelope = _canonical_sanitized_envelope()
    envelope["adcp_error"]["details"] = {"echo": "SuperSecretBrief"}

    with pytest.raises(AssertionError, match="leaked through the sanitized"):
        assert_sanitized_wire_error(
            envelope,
            _SANITIZED_CASE_CODE,
            rejected_fragments=("supersecretbrief",),
        )


# ---------------------------------------------------------------------------
# Known-bad self-test for ``assert_failed_task_no_secret_leak``'s SURFACE
#
# That oracle's whole reason to exist is that a failed A2A Task has MORE than one
# client-facing carrier: the structured DataPart envelope AND every human-readable
# TextPart. Narrowing the scanned surface to the envelope alone leaves all 62 tests
# in test_a2a_error_routing.py green — none of them produces a Task whose TextPart
# carries a secret the envelope does not. The case below is exactly that Task.
# ---------------------------------------------------------------------------


def _failed_task_with(text: str, envelope: dict) -> object:
    """A failed Task whose error artifact carries ``text`` and ``envelope``."""
    from a2a.types import Artifact, Part, Task, TaskState, TaskStatus

    from tests.utils.a2a_helpers import _dict_to_value

    return Task(
        id="t-scrub",
        status=TaskStatus(state=TaskState.TASK_STATE_FAILED),
        artifacts=[
            Artifact(
                artifact_id="e1",
                name="error_result",
                parts=[Part(text=text), Part(data=_dict_to_value(envelope))],
            )
        ],
    )


_CLEAN_FAILED_ENVELOPE = {
    "adcp_error": {"code": "SERVICE_UNAVAILABLE", "message": "Request failed."},
    "errors": [{"code": "SERVICE_UNAVAILABLE", "message": "Request failed."}],
}


def test_failed_task_oracle_scans_the_textpart_not_only_the_envelope():
    """A clean DataPart envelope with a secret-bearing TextPart must TRIP the oracle.

    Reddens if the scanned surface is narrowed to the envelope alone — the one
    weakening no existing A2A error-routing test would catch, because none of them
    produces a Task whose TextPart leaks something its envelope does not.
    """
    from tests.utils.a2a_helpers import assert_failed_task_no_secret_leak

    task = _failed_task_with(SECRET_BEARING_MESSAGE, _CLEAN_FAILED_ENVELOPE)

    with pytest.raises(AssertionError, match="leaked to the buyer-facing wire"):
        assert_failed_task_no_secret_leak(task)


def test_failed_task_oracle_still_scans_the_envelope():
    """The mirror case: a clean TextPart cannot excuse a leaking DataPart."""
    from tests.utils.a2a_helpers import assert_failed_task_no_secret_leak

    leaking = {
        "adcp_error": {"code": "SERVICE_UNAVAILABLE", "message": SECRET_BEARING_MESSAGE},
        "errors": [{"code": "SERVICE_UNAVAILABLE", "message": SECRET_BEARING_MESSAGE}],
    }
    task = _failed_task_with("Request failed.", leaking)

    with pytest.raises(AssertionError, match="leaked to the buyer-facing wire"):
        assert_failed_task_no_secret_leak(task)


def test_failed_task_oracle_passes_a_fully_clean_task():
    """Counter-control: a Task clean on BOTH carriers must not trip the oracle."""
    from tests.utils.a2a_helpers import assert_failed_task_no_secret_leak

    assert_failed_task_no_secret_leak(_failed_task_with("Request failed.", _CLEAN_FAILED_ENVELOPE))
