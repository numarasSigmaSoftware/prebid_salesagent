"""Shared oracle for 'no raw secret leaked to a buyer-facing wire'.

The token set IS the definition of what counts as a leak — keep it in ONE place so
tightening it (adding a new secret shape) is a single edit, not an N-site lockstep
update where a missed copy silently under-asserts.
"""

from __future__ import annotations

import json

from tests.helpers.pinned_schema import pinned_error_code_suggestion

# A message shaped like the worst realistic leak: a connection string with credentials,
# a bearer token, an internal host, and inline SQL — everything a scrubbed error must never
# echo to the buyer.
SECRET_BEARING_MESSAGE = "postgresql://svc:hunter2@db.internal/prod TOKEN=abc123 SELECT * FROM principals"

_SECRET_TOKENS = ("hunter2", "postgresql://", "svc", "db.internal", "TOKEN=abc123", "SELECT", "principals")

# Independently-pinned expected sanitized MESSAGE text per wire code — deliberately literal,
# NOT read from src.core.exceptions._SANITIZED_BY_WIRE_CODE. The AdCP spec (core/error.json)
# does not mandate exact message wording (only that it be human-readable), so there is no
# independent spec/fixture source to read it from the way pinned_error_code_suggestion() reads
# suggestions. Pinning it here as a literal means a change to production's table without a
# matching change here reddens this oracle, instead of both sides moving together silently —
# the exact gap a prior version of this file had (both sides read the same production table).
_EXPECTED_SANITIZED_MESSAGE: dict[str, str] = {
    "INVALID_REQUEST": "The request is malformed or contains unsupported fields; review it and resubmit.",
    "VALIDATION_ERROR": "The request could not be validated; review the submitted fields and resubmit.",
    "AUTH_REQUIRED": "Authentication or authorization failed; provide valid credentials or the required permissions.",
}


def serialize_wire_error(wire: object) -> str:
    """Serialize one SDK wire-error carrier through the shared test oracle."""
    if isinstance(wire, dict):
        payload = wire
    else:
        model_dump = getattr(wire, "model_dump", None)
        assert callable(model_dump), f"Unsupported wire-error carrier: {type(wire).__name__}"
        payload = model_dump()
    return json.dumps(payload, default=str)


def assert_no_secret_leak(blob: object, *, context: str = "") -> None:
    """Assert no ``SECRET_BEARING_MESSAGE`` token appears in ``blob`` (a str, or a JSON-able
    envelope/dict). Raises with the offending token and a truncated haystack.

    Refuses ``None``: an absent value must not satisfy the oracle vacuously — a caller
    asserting on a field that production stopped populating should fail loudly, not pass.
    """
    assert blob is not None, (
        f"assert_no_secret_leak given None{f' ({context})' if context else ''} — absent value cannot prove a scrub"
    )
    haystack = blob if isinstance(blob, str) else json.dumps(blob, default=str)
    where = f" ({context})" if context else ""
    for token in _SECRET_TOKENS:
        assert token not in haystack, (
            f"secret fragment {token!r} leaked to the buyer-facing wire{where}: {haystack[:300]}"
        )


def assert_sanitized_wire_error(
    envelope: dict,
    code: str,
    *,
    rejected_fragments: tuple[str, ...] = (),
) -> None:
    """Assert both error-envelope layers use the canonical scrubbed presentation.

    Two INDEPENDENT oracles, neither read from production's own scrub table
    (``src.core.exceptions._SANITIZED_BY_WIRE_CODE``) — reading expected values from the
    same table production writes them from means a change to that table moves both sides
    of the assertion together, silently. ``expected_message`` is a literal pinned in THIS
    file (``_EXPECTED_SANITIZED_MESSAGE`` — the spec doesn't mandate exact wording, so
    there's no independent source to read it from); ``expected_suggestion`` is read from
    the vendored AdCP schema pin's ``enumMetadata`` (``pinned_error_code_suggestion``),
    which production's own suggestion constants are written to mirror. Callers supply any
    raw business/input fragments that must be absent, so replacing a brittle raw-message
    assertion still proves the confidential text was removed.
    """
    expected_message = _EXPECTED_SANITIZED_MESSAGE.get(code)
    assert expected_message is not None, f"{code!r} has no independently-pinned sanitized message in this file"
    expected_suggestion = pinned_error_code_suggestion(code)

    adcp_error = envelope.get("adcp_error") or {}
    errors = envelope.get("errors") or []
    assert errors, f"Expected errors[0] in two-layer envelope: {envelope}"
    for label, error in (("adcp_error", adcp_error), ("errors[0]", errors[0])):
        assert error.get("message") == expected_message, (
            f"{label}.message did not use the canonical {code} scrub: {error}"
        )
        assert error.get("suggestion") == expected_suggestion, (
            f"{label}.suggestion did not use the canonical {code} guidance: {error}"
        )

    serialized = serialize_wire_error(envelope).lower()
    for fragment in rejected_fragments:
        assert fragment.lower() not in serialized, (
            f"raw rejected fragment {fragment!r} leaked through the sanitized {code} envelope: {serialized[:500]}"
        )
