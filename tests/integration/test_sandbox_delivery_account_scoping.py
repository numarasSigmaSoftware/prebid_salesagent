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

from tests.helpers.sandbox_assertions import assert_all_live, assert_all_sandbox, sandbox_modes

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

            # Exact-set, not just absence: `live not in returned` is also satisfied by an
            # EMPTY response, which is how an over-broad filter would slip through green.
            assert returned == {sandbox_buy.media_buy_id}, (
                f"expected only the in-scope sandbox buy, got {returned}; "
                f"{live_buy.media_buy_id} present means a buy from another account was read "
                "through the tenant's real adapter, and an empty set means the filter is over-broad"
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


class TestDeliveryAdapterModes:
    """SA-010: the account filter is not enough — assert which adapter each buy is read
    through. The scoping tests above pass on returned IDs alone, so replacing
    sandbox_by_buy[...] with False would leave them green (the mocked adapter is
    mode-agnostic)."""

    def _modes_for(self, *, scoped_account: str | None):
        from tests.factories import PrincipalFactory, TenantFactory
        from tests.harness import DeliveryPollEnv

        suffix = scoped_account or "none"
        with DeliveryPollEnv(tenant_id=f"t_mode_{suffix}", principal_id=f"p_mode_{suffix}") as env:
            tenant = TenantFactory(tenant_id=f"t_mode_{suffix}")
            principal = PrincipalFactory(tenant=tenant, principal_id=f"p_mode_{suffix}")
            sandbox_buy, live_buy = _seed_two_accounts(tenant, principal)

            env.set_adapter_response(sandbox_buy.media_buy_id, impressions=1000)
            env.set_adapter_response(live_buy.media_buy_id, impressions=2000)

            kwargs = {"media_buy_ids": [sandbox_buy.media_buy_id, live_buy.media_buy_id]}
            if scoped_account is not None:
                kwargs["account"] = AccountReference(root=AccountReferenceById(account_id=scoped_account))

            env.call_mcp(**kwargs)

            return env.mock["adapter"]

    def test_sandbox_scoped_request_uses_only_a_sandbox_adapter(self, integration_db):
        assert_all_sandbox(self._modes_for(scoped_account="acc_sbx"), context="sandbox-scoped delivery")

    def test_live_scoped_request_uses_only_a_live_adapter(self, integration_db):
        """Negative control — 'always sandbox' would pass the test above."""
        assert_all_live(self._modes_for(scoped_account="acc_live"), context="live-scoped delivery")

    def test_unscoped_mixed_request_uses_both_modes(self, integration_db):
        """Both buys are in play, each read through the adapter its own account dictates."""
        modes = sandbox_modes(self._modes_for(scoped_account=None))

        assert set(modes) == {True, False}, (
            f"a mixed unscoped request must read each buy through its own mode, got {modes}"
        )
