"""SA-006: an account-scoped delivery request must not reach buys of another account.

`get_media_buy_delivery` accepts an account reference, but target selection fetched buys
by principal only. A request scoped to a SANDBOX account could therefore pull in a LIVE
buy of the same principal — explicitly by id or by browsing — and read it through the
tenant's real ad server, defeating the sandbox guarantee through an account-scoping hole.

Driven against a real database because `_get_media_buy_delivery_impl` cannot be driven far
enough with unit mocks: it raises before the decision seam, and asserting on an empty call
list passes vacuously.

AdCP 3.1.1 ``dist/docs/3.1.1/media-buy/advanced-topics/sandbox.mdx`` §Seller
implementation: sandbox requests MUST NOT make real ad platform API calls.
"""

from __future__ import annotations

from datetime import date

import pytest
from adcp.types import AccountReference, AccountReferenceById

pytestmark = pytest.mark.requires_db


def _seed_two_accounts(tenant, principal):
    """One sandbox account and one live account, each owning a media buy.

    Factories only — no session.add()/get_db_session() in the test body
    (CLAUDE.md §Test Fixtures, enforced by test_architecture_repository_pattern).
    """
    from tests.factories import AccountFactory, AgentAccountAccessFactory, MediaBuyFactory

    buys = {}
    for account_id, sandbox in (("acc_sbx", True), ("acc_live", False)):
        AccountFactory(tenant=tenant, account_id=account_id, sandbox=sandbox)
        # Resolution enforces agent access; without the grant the request fails
        # authorization before the scoping filter is ever reached.
        AgentAccountAccessFactory(
            tenant_id=tenant.tenant_id,
            principal_id=principal.principal_id,
            account_id=account_id,
        )
        buys[account_id] = MediaBuyFactory(
            tenant=tenant,
            principal=principal,
            account_id=account_id,
            start_date=date(2026, 1, 1),
            end_date=date(2026, 12, 31),
        )
    return buys["acc_sbx"], buys["acc_live"]


class TestDeliveryAccountScoping:
    def test_sandbox_scoped_request_excludes_the_live_buy(self, integration_db):
        """Both buys belong to the principal; only the in-scope one may be returned."""
        from tests.factories import PrincipalFactory, TenantFactory
        from tests.harness import DeliveryPollEnv

        with DeliveryPollEnv(tenant_id="t_scope", principal_id="p_scope") as env:
            tenant = TenantFactory(tenant_id="t_scope")
            principal = PrincipalFactory(tenant=tenant, principal_id="p_scope")
            sandbox_buy, live_buy = _seed_two_accounts(tenant, principal)

            env.set_adapter_response(sandbox_buy.media_buy_id, impressions=1000)
            env.set_adapter_response(live_buy.media_buy_id, impressions=2000)

            # Driven through MCP, not call_impl: account resolution
            # (enrich_identity_with_account) is a transport-boundary responsibility, so
            # identity.account_id — which the filter keys on — is only populated on a
            # real wrapper path.
            response = env.call_mcp(
                media_buy_ids=[sandbox_buy.media_buy_id, live_buy.media_buy_id],
                account=AccountReference(root=AccountReferenceById(account_id="acc_sbx")),
            )

            returned = {d.media_buy_id for d in (response.media_buy_deliveries or [])}

            assert live_buy.media_buy_id not in returned, (
                f"a buy from acc_live survived a request scoped to acc_sbx (returned={returned}); "
                "it would have been read through the tenant's real adapter"
            )

    def test_unscoped_request_still_returns_both(self, integration_db):
        """Negative control: without an account reference, both buys remain in scope.

        Without this, 'return nothing' would satisfy the test above.
        """
        from tests.factories import PrincipalFactory, TenantFactory
        from tests.harness import DeliveryPollEnv

        with DeliveryPollEnv(tenant_id="t_scope2", principal_id="p_scope2") as env:
            tenant = TenantFactory(tenant_id="t_scope2")
            principal = PrincipalFactory(tenant=tenant, principal_id="p_scope2")
            sandbox_buy, live_buy = _seed_two_accounts(tenant, principal)

            env.set_adapter_response(sandbox_buy.media_buy_id, impressions=1000)
            env.set_adapter_response(live_buy.media_buy_id, impressions=2000)

            response = env.call_mcp(media_buy_ids=[sandbox_buy.media_buy_id, live_buy.media_buy_id])

            returned = {d.media_buy_id for d in (response.media_buy_deliveries or [])}

            assert returned == {sandbox_buy.media_buy_id, live_buy.media_buy_id}, (
                f"unscoped request lost buys (returned={returned}); account filtering must "
                "apply only when the request carries an account reference"
            )
