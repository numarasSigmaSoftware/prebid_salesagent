"""Shared oracle for 'no raw secret leaked to a buyer-facing wire'.

The token set IS the definition of what counts as a leak — keep it in ONE place so
tightening it (adding a new secret shape) is a single edit, not an N-site lockstep
update where a missed copy silently under-asserts.
"""

from __future__ import annotations

import json

# A message shaped like the worst realistic leak: a connection string with credentials,
# a bearer token, an internal host, and inline SQL — everything a scrubbed error must never
# echo to the buyer.
SECRET_BEARING_MESSAGE = "postgresql://svc:hunter2@db.internal/prod TOKEN=abc123 SELECT * FROM principals"

_SECRET_TOKENS = ("hunter2", "postgresql://", "svc", "db.internal", "TOKEN=abc123", "SELECT", "principals")


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

    The production scrub table is the single source of truth for exact message
    and suggestion text.  Callers supply any raw business/input fragments that
    must be absent, so replacing a brittle raw-message assertion still proves
    the confidential text was removed.
    """
    from src.core.exceptions import _SANITIZED_BY_WIRE_CODE

    expected = _SANITIZED_BY_WIRE_CODE.get(code)
    assert expected is not None, f"{code!r} has no canonical sanitized presentation"
    expected_message, expected_suggestion = expected

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
