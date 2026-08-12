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

from src.core.schema_helpers import to_account_reference
from src.core.transport_helpers import enrich_identity_with_account
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

                identity = env.identity_for(Transport.MCP)
                identity = enrich_identity_with_account(identity, to_account_reference({"account_id": "acc_gp_route"}))
                response = env.call_impl(brief="video ads", identity=identity)
                assert not response.errors, f"dispatch failed: {response.errors!r}"

            return mock_get_adapter

    def test_sandbox_account_reference_routes_to_the_sandbox_adapter(self, integration_db):
        assert_all_sandbox(self._adapter_modes(account_sandbox=True), context="get_products (account ref)")

    def test_live_account_reference_routes_to_the_live_adapter(self, integration_db):
        """Negative control — 'always sandbox' would disable real credential reads and still pass above."""
        assert_all_live(self._adapter_modes(account_sandbox=False), context="get_products (account ref)")
