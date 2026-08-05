"""SA-013: approving a sandbox media buy must not dispatch to a real ad platform.

`execute_approved_media_buy` runs after an operator approves a pending buy — long after
the request identity is gone — so it must re-derive sandbox mode from the buy's own
account. A regression passing `sandbox=False` here would create a real order, upload real
creatives, and approve real objects on the tenant's ad server.

Written as an integration test on purpose. Two attempts to drive this path with unit mocks
failed on `session.scalars(...).first().side_effect` ordering: the sandbox derivation adds
an account lookup to the same mock chain, so the sequence either exhausts ("Adapter
creation failed: " with an empty message) or steals a slot package reconstruction needed
("Failed to reconstruct package pkg_1: "). Real rows remove the ordering problem entirely.

AdCP 3.1.1 ``dist/docs/3.1.1/media-buy/advanced-topics/sandbox.mdx`` §Seller
implementation: sandbox requests MUST NOT make real ad platform API calls.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from tests.harness._base import IntegrationEnv

pytestmark = pytest.mark.requires_db

_MODULE = "src.core.tools.media_buy_create"


class _ExecEnv(IntegrationEnv):
    """Bare integration env: binds factory sessions, patches nothing.

    The executor is called directly, so no transport patches are wanted — only the real
    database the factories write into.
    """

    EXTERNAL_PATCHES: dict[str, str] = {}


def _seed_approved_buy(*, tenant_id: str, sandbox: bool):
    """A pending_approval buy whose raw_request reconstructs, owned by a sandbox/live account."""
    from tests.factories import (
        AccountFactory,
        AgentAccountAccessFactory,
        MediaBuyFactory,
        MediaPackageFactory,
        PricingOptionFactory,
        PrincipalFactory,
        ProductFactory,
        TenantFactory,
    )

    tenant = TenantFactory(tenant_id=tenant_id, ad_server="mock")
    principal = PrincipalFactory(tenant=tenant, principal_id=f"p_{tenant_id}")

    account = AccountFactory(tenant=tenant, account_id=f"acc_{tenant_id}", sandbox=sandbox)
    AgentAccountAccessFactory(
        tenant_id=tenant.tenant_id, principal_id=principal.principal_id, account_id=account.account_id
    )

    product = ProductFactory(tenant=tenant, product_id="prod_exec")
    PricingOptionFactory(product=product, pricing_model="cpm", currency="USD", rate=Decimal("10.00"))

    buy = MediaBuyFactory(
        tenant=tenant,
        principal=principal,
        account_id=account.account_id,
        status="pending_approval",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 12, 31),
        start_time=datetime(2026, 1, 1, tzinfo=UTC),
        end_time=datetime(2026, 12, 31, 23, 59, 59, tzinfo=UTC),
        raw_request={
            "brand": {"domain": "acme-exec.com"},
            "start_time": "2026-01-01T00:00:00Z",
            "end_time": "2026-12-31T23:59:59Z",
            "packages": [
                {
                    "package_id": "pkg_exec",
                    "product_id": "prod_exec",
                    "pricing_option_id": "po_exec",
                    "budget": 1000.0,
                }
            ],
        },
    )
    # The executor reads persisted packages, not only raw_request.
    MediaPackageFactory(
        media_buy=buy,
        package_id="pkg_exec",
        package_config={"product_id": "prod_exec", "pricing_option_id": "po_exec"},
    )
    return buy


def _dispatch_modes(*, tenant_id: str, sandbox: bool) -> list[bool]:
    """Run the approval executor; return the sandbox= of every create dispatch it made."""
    from src.core.schemas import CreateMediaBuySuccess
    from src.core.tools.media_buy_create import execute_approved_media_buy

    adapter_response = CreateMediaBuySuccess.sync_success(media_buy_id="gam_order_1", packages=[])

    with _ExecEnv(tenant_id=tenant_id) as env:
        buy = _seed_approved_buy(tenant_id=tenant_id, sandbox=sandbox)
        env._commit_factory_data()

        with (
            patch(f"{_MODULE}._execute_adapter_media_buy_creation", return_value=adapter_response) as mock_create,
            patch(f"{_MODULE}.get_adapter", return_value=MagicMock()),
            patch(f"{_MODULE}._validate_creatives_before_adapter_call"),
        ):
            success, error = execute_approved_media_buy(buy.media_buy_id, tenant_id)

    assert mock_create.call_args_list, (
        f"the executor never reached its create dispatch (success={success}, error={error}); "
        "asserting over an empty call list would pass vacuously"
    )
    return [c.kwargs.get("sandbox") for c in mock_create.call_args_list]


class TestApprovedExecutionSandboxDispatch:
    def test_approving_a_sandbox_buy_dispatches_in_sandbox_mode(self, integration_db):
        modes = _dispatch_modes(tenant_id="t_exec_sbx", sandbox=True)

        assert all(modes), (
            f"approving a SANDBOX buy dispatched live (modes={modes}); the approval would "
            "create a real order on the tenant's ad server"
        )

    def test_approving_a_live_buy_dispatches_in_live_mode(self, integration_db):
        """Negative control — 'always sandbox' would silently stop real approvals."""
        modes = _dispatch_modes(tenant_id="t_exec_live", sandbox=False)

        assert not any(modes), f"expected a live dispatch for a live account, got {modes}"
