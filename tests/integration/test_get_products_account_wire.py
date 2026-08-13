"""Wire-path tests: ``account`` reaches get_products' adapter dispatch.

``get_products`` consumes ``req.account`` (resolved via ``enrich_identity_with_account``
at the MCP/A2A/REST boundary, mirroring ``get_media_buy_delivery``) to decide the
``sandbox=`` mode passed to ``get_adapter`` when annotating pricing options. Before this
wiring, no transport declared ``account`` on ``get_products`` even though the pinned
3.1.1 ``get-products-request.json`` declares the field — so a sandbox account always
built the tenant's real adapter (``sandbox=False`` was hard-wired). Like
``test_create_media_buy_account_wire.py``, this proves two separate things:

1. ``account`` crosses the wire on every transport that declares it (a bogus account
   is rejected with ``ACCOUNT_NOT_FOUND``, which can only happen if the reference
   reached ``enrich_identity_with_account``).
2. The resolved mode reaches ``get_adapter``'s ``sandbox=`` keyword — the identity-keyed
   forwarding site this PR's structural guard now checks
   (``tests/unit/test_architecture_get_adapter_sandbox.py::IDENTITY_KEYED_SITES``).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from tests.factories import (
    AccountFactory,
    AgentAccountAccessFactory,
    PricingOptionFactory,
    PrincipalFactory,
    ProductFactory,
    TenantFactory,
)
from tests.harness.assertions import assert_rejected
from tests.harness.product import ProductEnv
from tests.harness.transport import Transport
from tests.helpers.sandbox_assertions import assert_all_live, assert_all_sandbox

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]


class TestGetProductsAccountWirePassthrough:
    """``account`` reaches enrich_identity_with_account through every wrapper."""

    def _run_account_wire(self, transport: Transport) -> None:
        with ProductEnv() as env:
            tenant = TenantFactory(tenant_id=f"t_gp_wire_{transport.value}")
            PrincipalFactory(tenant=tenant, principal_id=f"p_gp_wire_{transport.value}")
            env.set_policy_approved()
            env.set_ranking_disabled()

            result = env.call_via(
                transport,
                brief="video ads",
                account={"account_id": f"no-such-account-{transport.value}"},
            )

        assert_rejected(result, code="ACCOUNT_NOT_FOUND")

    def test_mcp_wire_forwards_account(self, integration_db):
        """MCP wrapper declares + forwards ``account`` -> boundary resolves it."""
        self._run_account_wire(Transport.MCP)

    def test_a2a_wire_forwards_account(self, integration_db):
        """A2A raw function forwards ``account`` -> boundary resolves it."""
        self._run_account_wire(Transport.A2A)

    def test_rest_wire_forwards_account(self, integration_db):
        """REST ``GetProductsBody.account`` + route passthrough -> boundary resolves it."""
        self._run_account_wire(Transport.REST)


class TestGetProductsAccountReferenceRoutesTheAdapter:
    """The mode an account reference RESOLVES TO reaches get_adapter's sandbox= kwarg.

    ``get_adapter`` is imported lazily inside ``_get_products_impl``, so the patch
    target is the source module (``adapter_helpers.get_adapter``), not ``products.py``
    — see ``tests/harness/product.py``'s harness docstring on lazy-import patch targets.
    """

    @staticmethod
    def _adapter_modes(*, account_sandbox: bool) -> MagicMock:
        mode = "sbx" if account_sandbox else "live"
        with patch("src.core.helpers.adapter_helpers.get_adapter") as mock_get_adapter:
            mock_get_adapter.return_value.get_supported_pricing_models.return_value = []

            with ProductEnv(tenant_id=f"t-gp-route-{mode}", principal_id=f"p-gp-route-{mode}") as env:
                tenant, principal = env.setup_default_data()
                env.set_policy_approved()
                env.set_ranking_disabled()

                product = ProductFactory(tenant=tenant, product_id=f"prod-gp-route-{mode}")
                PricingOptionFactory(product=product)

                AccountFactory(tenant=tenant, account_id="acc_gp_route", sandbox=account_sandbox)
                AgentAccountAccessFactory(
                    tenant_id=tenant.tenant_id,
                    principal_id=principal.principal_id,
                    account_id="acc_gp_route",
                )

                result = env.call_via(
                    Transport.MCP,
                    brief="video ads",
                    account={"account_id": "acc_gp_route"},
                )
                assert not result.is_error, f"dispatch failed: {result.error!r}"

            return mock_get_adapter

    def test_sandbox_account_reference_routes_to_the_sandbox_adapter(self, integration_db):
        assert_all_sandbox(self._adapter_modes(account_sandbox=True), context="get_products (account ref)")

    def test_live_account_reference_routes_to_the_live_adapter(self, integration_db):
        """Negative control — 'always sandbox' would disable real credential reads and still pass above."""
        assert_all_live(self._adapter_modes(account_sandbox=False), context="get_products (account ref)")


class TestGetProductsSandboxMarkerOnWire:
    """``GetProductsResponse.sandbox`` echoes the resolved account's mode on the real wire.

    Mirrors the sandbox-marker pattern in ``test_creative_sync_transport.py``: asserts on
    ``result.wire_response``, not the parsed payload, and pairs a positive case with a
    negative control so 'always sandbox: true' can't pass silently.
    """

    @staticmethod
    def _dispatch(*, account_sandbox: bool):
        mode = "sbx" if account_sandbox else "live"
        with ProductEnv(tenant_id=f"t-gp-marker-{mode}", principal_id=f"p-gp-marker-{mode}") as env:
            tenant, principal = env.setup_default_data()
            env.set_policy_approved()
            env.set_ranking_disabled()

            product = ProductFactory(tenant=tenant, product_id=f"prod-gp-marker-{mode}")
            PricingOptionFactory(product=product)

            AccountFactory(tenant=tenant, account_id="acc_gp_marker", sandbox=account_sandbox)
            AgentAccountAccessFactory(
                tenant_id=tenant.tenant_id,
                principal_id=principal.principal_id,
                account_id="acc_gp_marker",
            )

            result = env.call_via(
                Transport.MCP,
                brief="video ads",
                account={"account_id": "acc_gp_marker"},
            )
            assert not result.is_error, f"dispatch failed: {result.error!r}"
            assert result.wire_response is not None, "MCP must capture a real wire response"
            return result

    def test_sandbox_account_marks_the_response_on_the_wire(self, integration_db):
        result = self._dispatch(account_sandbox=True)
        assert result.wire_response.get("sandbox") is True, (
            f"sandbox-scoped get_products must carry sandbox: true on the wire, got {result.wire_response.get('sandbox')!r}"
        )

    def test_live_account_omits_the_marker(self, integration_db):
        """Negative control — 'always sandbox: true' would pass the test above."""
        result = self._dispatch(account_sandbox=False)
        assert result.wire_response.get("sandbox") is None, (
            f"live-scoped get_products must omit sandbox, got {result.wire_response.get('sandbox')!r}"
        )
