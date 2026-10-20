"""Admin approval and delivery resume from the persisted buy's account."""

import pytest

from tests.harness.sandbox_lifecycle import SandboxLifecycleEnv
from tests.helpers.media_buy_approval import login_as

pytestmark = [pytest.mark.admin, pytest.mark.requires_db]


def test_approval_persists_sandbox_completion_and_creatives(admin_client, integration_db):
    with SandboxLifecycleEnv() as env:
        env.seed_lifecycle(approval=True, adapter_type="google_ad_manager")
        creative_id, step_id = env.seed_shared_creative(pending_buy=True)
        login_as(admin_client, tenant_id=env.tenant_id)
        with env.approval_session_states() as writer_sessions:
            response = admin_client.post(
                f"/tenant/{env.tenant_id}/media-buy/sandbox-creative-buy/approve",
                data={"action": "approve"},
            )
        assert writer_sessions == [False]
        assert response.status_code == 302
        assert env.buy_state("sandbox-creative-buy")[0] == "scheduled"
        assert env.workflow_status(step_id) == "approved"
        creative, sandbox_package, live_package = env.creative_persistence(creative_id)
        assert creative["platform_creative_id"] == "live-creative-123"
        assert sandbox_package["sandbox_creative_ids"][creative_id].startswith("sandbox_creative_")
        assert "sandbox_creative_ids" not in live_package
        assert env.vendor_calls() == 0


def test_admin_delivery_uses_stored_sandbox_account(admin_client, integration_db):
    with SandboxLifecycleEnv() as env:
        env.seed_lifecycle(adapter_type="google_ad_manager")
        env.seed_owned_buy("sandbox", status="active")
        env.seed_delivery("owned-buy")
        env._commit_factory_data()
        login_as(admin_client, tenant_id=env.tenant_id)
        response = admin_client.get(f"/tenant/{env.tenant_id}/media-buy/owned-buy")
        assert response.status_code == 200
        assert '<span class="value">4,321</span>' in response.text
        assert '<span class="value">USD 43.21</span>' in response.text
        assert env.vendor_calls() == 0
