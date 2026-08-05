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

Per-site mutation matrix (each site mutated ALONE — a blanket mutation of all sites only
proves the first assertion fires, which is how an earlier version of this test overstated
its reach):

    media_buy_create.py:583   helper's own get_adapter      CAUGHT
    media_buy_create.py:1080  executor -> helper forwarding CAUGHT
    media_buy_create.py:1201  creative upload               NOT CAUGHT — branch not reached
    media_buy_create.py:1258  final order approval          NOT CAUGHT — branch not reached

The buy now completes successfully with an approved, dimension-bearing creative assigned,
but the upload and approval branches still do not execute, so their adapter selections are
ungraded. Closing them needs the assignment/response shape those branches require (the
approval branch also looks GAM-specific: it gates on ``adapter.orders_manager``).

AdCP 3.1.1 ``dist/docs/3.1.1/media-buy/advanced-topics/sandbox.mdx`` §Seller
implementation: sandbox requests MUST NOT make real ad platform API calls.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from tests.harness._base import IntegrationEnv
from tests.helpers.sandbox_assertions import assert_all_live, assert_all_sandbox

pytestmark = pytest.mark.requires_db

_MODULE = "src.core.tools.media_buy_create"


class _ExecEnv(IntegrationEnv):
    """Bare integration env: binds factory sessions, patches nothing.

    The executor is called directly, so no transport patches are wanted — only the real
    database the factories write into.
    """

    EXTERNAL_PATCHES: dict[str, str] = {}


def _seed_approved_buy(*, tenant_id: str, sandbox: bool, buy_id: str = "mb_exec"):
    """A pending_approval buy whose raw_request reconstructs, owned by a sandbox/live account."""
    from tests.factories import (
        AccountFactory,
        AgentAccountAccessFactory,
        CreativeAssignmentFactory,
        CreativeFactory,
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
        media_buy_id=buy_id,
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
    # An APPROVED creative assigned to the package, so the creative-upload branch runs
    # (a pending_review creative is filtered out before the adapter is touched).
    creative = CreativeFactory(
        tenant=tenant,
        principal=principal,
        approved=True,
        # Root-level url/width/height is the supported simple-creative shape; without
        # dimensions and a content URL the executor rejects the buy before any upload.
        data={"url": "https://cdn.example.com/banner.jpg", "width": 300, "height": 250},
    )
    CreativeAssignmentFactory(creative=creative, media_buy_id=buy_id, package_id="pkg_exec")

    # The executor reads persisted packages, not only raw_request.
    MediaPackageFactory(
        media_buy=buy,
        package_id="pkg_exec",
        package_config={"product_id": "prod_exec", "pricing_option_id": "po_exec"},
    )
    return buy


def _run_executor(*, tenant_id: str, sandbox: bool):
    """Run the approval executor for real and return its get_adapter mock.

    ``_execute_adapter_media_buy_creation`` is deliberately NOT patched: patching it grades
    only the first forwarding hop (executor -> helper) and leaves the helper's own
    get_adapter, the creative upload, and the final order approval unproven. Every adapter
    the run selects is recorded on one mock, so the shared strict assertions cover all
    three sites at once.
    """
    from src.core.schemas import CreateMediaBuySuccess
    from src.core.tools.media_buy_create import execute_approved_media_buy

    adapter = MagicMock()
    adapter.create_media_buy.return_value = CreateMediaBuySuccess.sync_success(media_buy_id="gam_order_1", packages=[])
    adapter.creatives_manager.add_creative_assets.return_value = []
    adapter.orders_manager.approve_order.return_value = True

    with _ExecEnv(tenant_id=tenant_id) as env:
        buy = _seed_approved_buy(tenant_id=tenant_id, sandbox=sandbox)
        env._commit_factory_data()

        with patch(f"{_MODULE}.get_adapter", return_value=adapter) as mock_get_adapter:
            success, error = execute_approved_media_buy(buy.media_buy_id, tenant_id)

    print(
        "PROBE upload_called:",
        adapter.creatives_manager.add_creative_assets.called,
        "approve_called:",
        adapter.orders_manager.approve_order.called,
        "selections:",
        len(mock_get_adapter.call_args_list),
        "success:",
        success,
        "err:",
        error,
    )
    assert adapter.create_media_buy.called, (
        f"the executor never reached the adapter's create_media_buy (success={success}, "
        f"error={error}); asserting over an empty selection list would pass vacuously"
    )
    return mock_get_adapter


class TestApprovedExecutionSandboxDispatch:
    """Grades EVERY adapter the approval run selects: creation, creative upload, approval."""

    def test_approving_a_sandbox_buy_uses_sandbox_adapters_throughout(self, integration_db):
        assert_all_sandbox(_run_executor(tenant_id="t_exec_sbx", sandbox=True), context="approved-buy execution")

    def test_approving_a_live_buy_uses_live_adapters_throughout(self, integration_db):
        """Negative control — 'always sandbox' would silently stop real approvals."""
        assert_all_live(_run_executor(tenant_id="t_exec_live", sandbox=False), context="approved-buy execution")
