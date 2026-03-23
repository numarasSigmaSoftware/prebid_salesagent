"""Integration coverage for shared creative review completion flow."""

from __future__ import annotations

from unittest.mock import ANY, AsyncMock, Mock, patch

import pytest
from sqlalchemy import select

from src.core.context_manager import ContextManager
from src.core.database.database_session import get_db_session
from src.core.database.models import Creative, CreativeReview, MediaBuy, WorkflowStep
from src.core.tools.task_management import complete_task
from tests.factories import CreativeAssignmentFactory, CreativeFactory, MediaBuyFactory, PrincipalFactory, TenantFactory
from tests.harness._base import IntegrationEnv


@pytest.mark.integration
@pytest.mark.requires_db
@pytest.mark.asyncio
async def test_complete_task_creative_approval_applies_shared_review_flow(integration_db):
    """Creative approval via complete_task must match the shared admin review behavior."""
    with IntegrationEnv(tenant_id="creative-review-flow", principal_id="principal_creative_review") as env:
        tenant = TenantFactory(
            tenant_id="creative-review-flow",
            subdomain="creative-review-flow",
            slack_webhook_url="https://hooks.slack.test/creative-review-flow",
        )
        principal = PrincipalFactory(tenant=tenant, principal_id="principal_creative_review")
        media_buy = MediaBuyFactory(
            tenant=tenant,
            principal=principal,
            media_buy_id="mb_creative_review",
            status="pending_creatives",
        )
        creative = CreativeFactory(
            tenant=tenant,
            principal=principal,
            creative_id="creative_creative_review",
            status="pending_review",
        )
        CreativeAssignmentFactory(
            creative=creative,
            media_buy=media_buy,
            package_id="pkg_creative_review",
        )

        context_manager = ContextManager()
        context = context_manager.create_context(
            tenant_id=tenant.tenant_id,
            principal_id=principal.principal_id,
        )
        step = context_manager.create_workflow_step(
            context_id=context.context_id,
            step_type="creative_approval",
            owner="publisher",
            status="requires_approval",
            tool_name="sync_creatives",
            request_data={"operation": "sync_creatives"},
            object_mappings=[{"object_type": "creative", "object_id": creative.creative_id, "action": "review"}],
        )

        identity = env.identity
        audit_logger = Mock()
        notifier = Mock()

        with (
            patch(
                "src.services.creative_review_service.call_webhook_for_creative_status",
                new=AsyncMock(return_value=True),
            ) as webhook,
            patch("src.services.slack_notifier.get_slack_notifier", return_value=notifier) as get_notifier,
            patch("src.core.audit_logger.AuditLogger", return_value=audit_logger),
            patch(
                "src.core.tools.media_buy_create.execute_approved_media_buy", return_value=(True, None)
            ) as execute_buy,
        ):
            result = await complete_task(
                task_id=step.step_id,
                status="completed",
                response_data={"decision": "approved", "reviewer": "qa@example.com"},
                identity=identity,
            )

            assert result["status"] == "completed"

            with get_db_session() as session:
                refreshed_creative = session.scalars(
                    select(Creative).filter_by(
                        creative_id=creative.creative_id,
                        tenant_id=tenant.tenant_id,
                        principal_id=principal.principal_id,
                    )
                ).one()
                refreshed_media_buy = session.scalars(
                    select(MediaBuy).filter_by(
                        media_buy_id=media_buy.media_buy_id,
                        tenant_id=tenant.tenant_id,
                    )
                ).one()
                refreshed_step = session.scalars(select(WorkflowStep).filter_by(step_id=step.step_id)).one()
                human_review = session.scalars(
                    select(CreativeReview).filter_by(
                        creative_id=creative.creative_id,
                        tenant_id=tenant.tenant_id,
                        principal_id=principal.principal_id,
                        review_type="human",
                        final_decision="approved",
                    )
                ).first()

            assert refreshed_creative.status == "approved"
            assert refreshed_creative.approved_by == principal.principal_id
            assert refreshed_step.status == "completed"
            assert human_review is not None
            assert refreshed_media_buy.status == "active"

            webhook.assert_awaited_once_with(creative_id=creative.creative_id, tenant_id=tenant.tenant_id)
            get_notifier.assert_called_once_with({"features": {"slack_webhook_url": tenant.slack_webhook_url}})
            notifier.send_message.assert_called_once()
            execute_buy.assert_called_once_with(media_buy.media_buy_id, tenant.tenant_id)
            audit_logger.log_operation.assert_any_call(
                operation="complete_task",
                principal_name=principal.principal_id,
                principal_id=principal.principal_id,
                adapter_id="admin_ui",
                success=True,
                details=ANY,
                tenant_id=tenant.tenant_id,
            )
