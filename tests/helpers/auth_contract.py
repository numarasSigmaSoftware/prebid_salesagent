"""Transport-aware assertions for the pinned AdCP authentication error contract."""

from __future__ import annotations

from typing import Literal

from tests.helpers.envelope_assertions import assert_envelope_shape
from tests.helpers.pinned_schema import pinned_error_code_metadata, pinned_error_code_suggestion

CredentialState = Literal["missing", "invalid"]

_WIRE_TRANSPORTS = frozenset({"a2a", "mcp", "rest", "e2e_a2a", "e2e_mcp", "e2e_rest"})
_LEGACY_IMPL_TRANSPORTS = frozenset({"impl"})


def expected_auth_contract(transport: object, credential_state: CredentialState) -> tuple[str, str, str]:
    """Return the exact code, recovery, and suggestion for an auth rejection.

    Every wire transport implements the AdCP 3.1.1
    ``AUTH_MISSING``/``AUTH_INVALID`` split. Direct implementation calls lack
    credential-presence information and retain the deprecated
    ``AUTH_REQUIRED`` compatibility code.
    """
    if credential_state not in ("missing", "invalid"):
        raise AssertionError(f"Unknown credential state: {credential_state!r}")

    transport_name = str(transport)
    if transport_name in _WIRE_TRANSPORTS:
        code = "AUTH_MISSING" if credential_state == "missing" else "AUTH_INVALID"
    elif transport_name in _LEGACY_IMPL_TRANSPORTS:
        code = "AUTH_REQUIRED"
    else:
        raise AssertionError(f"Unknown auth-contract transport: {transport_name!r}")

    metadata = pinned_error_code_metadata()[code]
    recovery = metadata.get("recovery")
    if not isinstance(recovery, str) or not recovery:
        raise AssertionError(f"Pinned error code {code!r} has no non-empty recovery")
    return code, recovery, pinned_error_code_suggestion(code)


def assert_two_layer_auth_contract(
    envelope: dict | None,
    transport: object,
    credential_state: CredentialState,
) -> None:
    """Assert both mirrored wire layers match the pinned auth contract."""
    expected_code, expected_recovery, expected_suggestion = expected_auth_contract(transport, credential_state)
    assert_envelope_shape(envelope, expected_code, recovery=expected_recovery)
    assert envelope is not None

    errors = envelope.get("errors") or []
    assert errors and isinstance(errors[0], dict), f"Auth envelope has no errors[0] object: {envelope!r}"
    adcp_error = envelope.get("adcp_error")
    assert isinstance(adcp_error, dict), f"Auth envelope has no adcp_error object: {envelope!r}"

    for layer_name, error_object in (("errors[0]", errors[0]), ("adcp_error", adcp_error)):
        assert error_object.get("suggestion") == expected_suggestion, (
            f"{layer_name}.suggestion must match pinned {expected_code} guidance; "
            f"expected {expected_suggestion!r}, got {error_object.get('suggestion')!r}"
        )
