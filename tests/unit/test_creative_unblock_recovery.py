"""Crash-recovery coverage for creative-unblocked media-buy workflows."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.core.context_manager import publish_pending_workflow_notifications, publish_workflow_notifications
from src.core.database.repositories.workflow import PendingWorkflowNotification, StaleCreativeUnblockLease
from src.core.exceptions import AdCPServiceUnavailableError
from src.services.creative_unblock_recovery import (
    finalize_creative_unblock_workflow,
    recover_stale_creative_unblock_workflows,
)


def _uow_for_status(status: str) -> MagicMock:
    uow = MagicMock()
    uow.__enter__.return_value = uow
    uow.__exit__.return_value = None
    uow.workflows.reclaim_processing_lease.return_value = SimpleNamespace(
        step_id="step-1",
        processing_started_at=datetime(2026, 7, 25, tzinfo=UTC),
    )
    uow.media_buys.get_by_id_for_update.return_value = SimpleNamespace(status=status)
    return uow


def _lease() -> StaleCreativeUnblockLease:
    return StaleCreativeUnblockLease(tenant_id="tenant-1", step_id="step-1", media_buy_id="buy-1")


def test_autonomous_recovery_terminalizes_provider_applied_buy_without_reinvocation() -> None:
    scan = MagicMock()
    scan.__enter__.return_value = scan
    scan.__exit__.return_value = None
    with (
        patch("src.services.creative_unblock_recovery.get_db_session", return_value=scan),
        patch(
            "src.services.creative_unblock_recovery.WorkflowRepository.list_stale_creative_unblock_leases",
            return_value=[_lease()],
        ),
        patch("src.services.creative_unblock_recovery.AdminCreativeUoW", return_value=_uow_for_status("active")),
        patch("src.services.creative_unblock_recovery.execute_approved_media_buy") as execute,
        patch(
            "src.services.creative_unblock_recovery.finalize_creative_unblock_workflow",
            return_value=True,
        ) as finalize,
    ):
        assert recover_stale_creative_unblock_workflows() == 1

    execute.assert_not_called()
    finalize.assert_called_once_with(
        tenant_id="tenant-1",
        step_id="step-1",
        media_buy_id="buy-1",
        lease_started_at=datetime(2026, 7, 25, tzinfo=UTC),
        success=True,
        error_message=None,
    )


def test_ambiguous_provider_recovery_renews_lease_without_terminal_failure() -> None:
    scan = MagicMock()
    scan.__enter__.return_value = scan
    scan.__exit__.return_value = None
    with (
        patch("src.services.creative_unblock_recovery.get_db_session", return_value=scan),
        patch(
            "src.services.creative_unblock_recovery.WorkflowRepository.list_stale_creative_unblock_leases",
            return_value=[_lease()],
        ),
        patch(
            "src.services.creative_unblock_recovery.AdminCreativeUoW",
            return_value=_uow_for_status("pending_creatives"),
        ),
        patch(
            "src.services.creative_unblock_recovery.execute_approved_media_buy",
            side_effect=AdCPServiceUnavailableError("provider outcome unknown", retry_after=30),
        ),
        patch("src.services.creative_unblock_recovery.finalize_creative_unblock_workflow") as finalize,
    ):
        assert recover_stale_creative_unblock_workflows() == 0

    finalize.assert_not_called()


def test_negative_media_buy_state_never_replays_as_success() -> None:
    scan = MagicMock()
    scan.__enter__.return_value = scan
    scan.__exit__.return_value = None
    with (
        patch("src.services.creative_unblock_recovery.get_db_session", return_value=scan),
        patch(
            "src.services.creative_unblock_recovery.WorkflowRepository.list_stale_creative_unblock_leases",
            return_value=[_lease()],
        ),
        patch("src.services.creative_unblock_recovery.AdminCreativeUoW", return_value=_uow_for_status("rejected")),
        patch(
            "src.services.creative_unblock_recovery.finalize_creative_unblock_workflow",
            return_value=True,
        ) as finalize,
    ):
        assert recover_stale_creative_unblock_workflows() == 1

    assert finalize.call_args.kwargs["success"] is False
    assert "rejected" in finalize.call_args.kwargs["error_message"]


def test_stale_lease_owner_cannot_mutate_media_buy_or_publish() -> None:
    lease_started_at = datetime(2026, 7, 25, tzinfo=UTC)
    media_buy = SimpleNamespace(
        status="active",
        start_time=None,
        start_date=None,
        end_time=None,
        end_date=None,
        approved_at=None,
        approved_by=None,
    )
    uow = MagicMock()
    uow.__enter__.return_value = uow
    uow.__exit__.return_value = None
    uow.media_buys.get_by_id.return_value = media_buy
    uow.media_buys.get_packages.return_value = []
    uow.workflows.get_by_step_id.return_value = SimpleNamespace(request_data={})
    uow.workflows.transition_status.return_value = None

    with (
        patch("src.services.creative_unblock_recovery.AdminCreativeUoW", return_value=uow),
        patch("src.services.creative_unblock_recovery.publish_workflow_notifications") as publish,
    ):
        assert (
            finalize_creative_unblock_workflow(
                tenant_id="tenant-1",
                step_id="step-1",
                media_buy_id="buy-1",
                lease_started_at=lease_started_at,
                success=True,
                error_message=None,
            )
            is False
        )

    assert media_buy.status == "active"
    assert media_buy.approved_at is None
    publish.assert_not_called()
    assert uow.workflows.transition_status.call_args.kwargs["expected_processing_started_at"] == lease_started_at


def test_terminal_notification_outbox_is_autonomously_drained() -> None:
    session = MagicMock()
    session.__enter__.return_value = session
    session.__exit__.return_value = None
    pending = PendingWorkflowNotification(
        tenant_id="tenant-1",
        step_id="step-1",
        status="completed",
        event_id="workflow:step-1:1:completed",
        response_data={"ok": True},
    )
    with (
        patch("src.core.context_manager.get_independent_db_session", return_value=session),
        patch(
            "src.core.context_manager.WorkflowRepository.list_pending_workflow_notifications",
            return_value=[pending],
        ),
        patch("src.core.context_manager.publish_workflow_notifications") as publish,
    ):
        assert publish_pending_workflow_notifications() == 1

    publish.assert_called_once_with(
        "step-1",
        "completed",
        "tenant-1",
        event_id="workflow:step-1:1:completed",
    )


@pytest.mark.parametrize("first_delivery", [False, RuntimeError("delivery failed")])
def test_failed_terminal_delivery_releases_claim_and_successful_retry_acknowledges(
    first_delivery: bool | RuntimeError,
) -> None:
    session = MagicMock()
    session.__enter__.return_value = session
    session.__exit__.return_value = None
    repository = MagicMock()
    repository.claim_notification_publication.side_effect = [
        ("workflow:step-1:1:completed", "claim-1", {"ok": True}),
        ("workflow:step-1:1:completed", "claim-2", {"ok": True}),
    ]
    repository.get_by_step_id.return_value = None
    repository.release_notification_claim.return_value = True
    repository.mark_notifications_published.return_value = True
    with (
        patch("src.core.context_manager.get_independent_db_session", return_value=session),
        patch("src.core.context_manager.WorkflowRepository", return_value=repository),
        patch(
            "src.services.a2a_task_lifecycle.publish_workflow_task_transition",
            side_effect=[first_delivery, True],
        ),
    ):
        assert publish_workflow_notifications("step-1", "completed", "tenant-1") is False
        assert publish_workflow_notifications("step-1", "completed", "tenant-1") is True

    repository.release_notification_claim.assert_called_once_with(
        "workflow:step-1:1:completed",
        claim_token="claim-1",
    )
    repository.mark_notifications_published.assert_called_once_with(
        "workflow:step-1:1:completed",
        claim_token="claim-2",
    )
