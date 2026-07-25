"""Authentication rejections must carry the pinned error-code suggestion on the wire.

Core invariant: every authentication rejection carries the pinned top-level
``suggestion`` in the two-layer wire envelope. Every wire boundary uses the
``AUTH_MISSING``/``AUTH_INVALID`` split from the pinned AdCP 3.1.1 enum; direct
implementation helpers retain ``AUTH_REQUIRED`` where credential state is
unknowable.

Wire-first per tests/CLAUDE.md § Error Verification Policy: the
missing-principal case drives the real A2A wire; the remaining helpers are
checked on the envelope the production boundary translator builds for their
raise (``build_two_layer_error_envelope`` — the same builder every transport
dispatcher calls).
"""

from typing import Any

import pytest

from src.core.exceptions import AdCPError, build_two_layer_error_envelope
from tests.helpers.auth_contract import assert_two_layer_auth_contract

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]


def _assert_legacy_impl_auth_with_suggestion(envelope: dict[str, Any]) -> None:
    assert_two_layer_auth_contract(envelope, "impl", "missing")


class TestRequirePrincipalIdA2ASuggestion:
    """A rejected A2A identity on the real wire carries the AUTH_INVALID suggestion."""

    def test_missing_principal_a2a_envelope_carries_suggestion(self, integration_db):
        """A resolved identity with no principal_id represents rejected credentials.

        The A2A wire must therefore carry AUTH_INVALID with terminal recovery
        and the pinned no-retry suggestion.
        """
        from tests.factories import PrincipalFactory, TenantFactory
        from tests.harness.media_buy_list import MediaBuyListEnv
        from tests.harness.transport import Transport

        with MediaBuyListEnv(tenant_id="t1", principal_id="p1") as env:
            TenantFactory(tenant_id="t1")
            identity = PrincipalFactory.make_identity(principal_id=None, tenant_id="t1")

            result = env.call_via(Transport.A2A, identity=identity)

            assert result.is_error, (
                f"A missing principal_id must be rejected on the A2A wire, got success payload: {result.payload!r}"
            )
            result.assert_wire_error(
                "AUTH_INVALID",
                recovery="terminal",
                require_suggestion=True,
            )


class TestRestAuthSuggestion:
    """The real REST dependency path emits the pinned missing-auth suggestion."""

    def test_missing_rest_credentials_emit_pinned_suggestion(self, integration_db):
        from tests.factories import TenantFactory
        from tests.harness.account_list import AccountListEnv
        from tests.harness.transport import Transport

        with AccountListEnv(tenant_id="rest_auth_sugg", principal_id="rest_auth_principal") as env:
            TenantFactory(tenant_id="rest_auth_sugg")

            result = env.call_via(Transport.REST, identity=None)

        assert result.is_error, f"Expected REST auth rejection, got payload: {result.payload!r}"
        assert_two_layer_auth_contract(result.wire_error_envelope, "rest", "missing")


class TestRejectedCredentialWireBoundaries:
    """Every wire transport resolves and rejects a token with no principal row."""

    @pytest.mark.parametrize("transport", ["a2a", "mcp", "rest"])
    def test_rejected_token_runs_real_auth_chain(self, integration_db, transport):
        from tests.factories import TenantFactory
        from tests.harness.account_list import AccountListEnv
        from tests.harness.transport import Transport

        wire_transport = Transport(transport)
        tenant_id = f"rejected_token_{transport}"
        with AccountListEnv(tenant_id=tenant_id, principal_id=f"principal_{transport}") as env:
            TenantFactory(tenant_id=tenant_id)
            result = env.call_via(
                wire_transport,
                presented_auth_token="expired-or-revoked-token",
            )

        assert result.is_error, f"Expected {transport} to reject a token with no principal row"
        assert_two_layer_auth_contract(result.wire_error_envelope, wire_transport, "invalid")

    @pytest.mark.parametrize("transport", ["a2a", "mcp", "rest"])
    def test_rejected_legacy_token_is_missing_standard_authorization(self, integration_db, transport):
        """A rejected legacy credential does not satisfy the v3.1.1 Authorization split."""
        from tests.factories import TenantFactory
        from tests.harness.account_list import AccountListEnv
        from tests.harness.transport import Transport

        wire_transport = Transport(transport)
        tenant_id = f"rejected_legacy_token_{transport}"
        with AccountListEnv(tenant_id=tenant_id, principal_id=f"principal_{transport}") as env:
            TenantFactory(tenant_id=tenant_id)
            result = env.call_via(
                wire_transport,
                presented_legacy_auth_token="expired-or-revoked-legacy-token",
            )

        assert result.is_error, f"Expected {transport} to reject a legacy token with no principal row"
        assert_two_layer_auth_contract(result.wire_error_envelope, wire_transport, "missing")


class TestAuthHelperFamilySuggestion:
    """The remaining AUTH_REQUIRED raise sites in src/core/auth.py carry a suggestion.

    Each case drives the production helper and asserts on the envelope the
    production boundary translator builds for its raise — the same
    ``build_two_layer_error_envelope`` every transport dispatcher calls.
    """

    def test_resolve_principal_not_found_carries_suggestion(self, integration_db):
        from src.core.auth import resolve_principal_or_raise
        from tests.factories import TenantFactory
        from tests.harness._base import BareIntegrationEnv

        with BareIntegrationEnv(tenant_id="auth_sugg_t1") as env:
            TenantFactory(tenant_id="auth_sugg_t1")
            env.get_session()  # commit factory data

            with pytest.raises(AdCPError) as exc_info:
                resolve_principal_or_raise("nonexistent-principal", tenant_id="auth_sugg_t1")

        _assert_legacy_impl_auth_with_suggestion(build_two_layer_error_envelope(exc_info.value))

    def test_require_tenant_missing_carries_suggestion(self):
        from src.core.auth import require_tenant
        from tests.factories import PrincipalFactory

        identity = PrincipalFactory.make_identity(tenant=None)

        with pytest.raises(AdCPError) as exc_info:
            require_tenant(identity)

        _assert_legacy_impl_auth_with_suggestion(build_two_layer_error_envelope(exc_info.value))

    def test_invalid_token_carries_suggestion(self, integration_db):
        """get_principal_from_context with an invalid token (require_valid_token=True)
        raises AUTH_REQUIRED whose envelope must carry the top-level suggestion.
        """
        from src.core.auth import get_principal_from_context
        from tests.factories import TenantFactory
        from tests.harness._base import BareIntegrationEnv

        with BareIntegrationEnv(tenant_id="auth_sugg_t2") as env:
            TenantFactory(tenant_id="auth_sugg_t2")
            env.get_session()  # commit factory data

            class _HeaderCarrier:
                """Duck-typed context: get_http_headers() returns {} outside an
                HTTP request, so get_principal_from_context falls back to
                ``context.headers`` — the documented sync-tool seam."""

                headers = {
                    "x-adcp-auth": "not-a-real-token",
                    "x-adcp-tenant": "auth_sugg_t2",
                }

            with pytest.raises(AdCPError) as exc_info:
                get_principal_from_context(_HeaderCarrier())

        _assert_legacy_impl_auth_with_suggestion(build_two_layer_error_envelope(exc_info.value))
