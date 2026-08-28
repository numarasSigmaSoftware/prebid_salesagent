"""Durable recovery for media-buy creation unblocked by creative approval."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import NamedTuple

from src.admin.utils.media_buy_approval import build_approved_media_buy_result
from src.core.context_manager import publish_workflow_notifications
from src.core.database.database_session import get_db_session
from src.core.database.repositories.uow import AdminCreativeUoW
from src.core.database.repositories.workflow import WorkflowRepository
from src.core.exceptions import AdCPAdapterError, AdCPServiceUnavailableError, build_two_layer_error_envelope
from src.core.tools.media_buy_create import execute_approved_media_buy

logger = logging.getLogger(__name__)

CREATIVE_UNBLOCK_LEASE_SECONDS = 300
_PROVIDER_APPLIED_STATUSES = frozenset({"active", "completed"})
_RETRYABLE_LOCAL_STATUSES = frozenset({"pending_creatives", "draft"})


class CreativeUnblockRecoveryResult(NamedTuple):
    """Outcome of one recovery pass, so a transient-and-deferred lease is never silently invisible."""

    recovered: int
    deferred: int


def compute_unblocked_media_buy_status(media_buy) -> str:
    """Compute the durable post-provider status from the media-buy flight window."""
    now = datetime.now(UTC)
    start_time = media_buy.start_time
    if start_time is None and media_buy.start_date:
        start_time = datetime.combine(media_buy.start_date, datetime.min.time()).replace(tzinfo=UTC)
    elif start_time is not None and start_time.tzinfo is None:
        start_time = start_time.replace(tzinfo=UTC)

    end_time = media_buy.end_time
    if end_time is None and media_buy.end_date:
        end_time = datetime.combine(media_buy.end_date, datetime.max.time()).replace(tzinfo=UTC)
    elif end_time is not None and end_time.tzinfo is None:
        end_time = end_time.replace(tzinfo=UTC)

    if start_time and end_time and start_time <= now <= end_time:
        return "active"
    return "scheduled"


def finalize_creative_unblock_workflow(
    *,
    tenant_id: str,
    step_id: str,
    media_buy_id: str,
    lease_started_at: datetime,
    success: bool,
    error_message: str | None,
) -> bool:
    """Atomically terminalize the leased workflow and then publish its result."""
    terminal_status = "completed" if success else "failed"
    transitioned = False
    with AdminCreativeUoW(tenant_id) as uow:
        assert uow.media_buys is not None
        assert uow.workflows is not None
        media_buy = uow.media_buys.get_by_id(media_buy_id)
        if success:
            if media_buy is None:
                success = False
                terminal_status = "failed"
                error_message = "Media buy disappeared while finalizing creative-unblock recovery"
            else:
                step = uow.workflows.get_by_step_id(step_id)
                request_data = step.request_data if step and isinstance(step.request_data, dict) else {}
                result = build_approved_media_buy_result(uow.media_buys, media_buy_id, request_data)
                transitioned = (
                    uow.workflows.transition_status(
                        step_id,
                        status="completed",
                        allowed_from={"processing"},
                        expected_processing_started_at=lease_started_at,
                        completed_at=datetime.now(UTC),
                        response_data=result.model_dump(mode="json"),
                    )
                    is not None
                )
                if transitioned:
                    if media_buy.status != "completed":
                        media_buy.status = compute_unblocked_media_buy_status(media_buy)
                    media_buy.approved_at = datetime.now(UTC)
                    media_buy.approved_by = "system"
        if not success:
            failure = build_two_layer_error_envelope(AdCPAdapterError(error_message or "Adapter creation failed"))
            transitioned = (
                uow.workflows.transition_status(
                    step_id,
                    status="failed",
                    allowed_from={"processing"},
                    expected_processing_started_at=lease_started_at,
                    completed_at=datetime.now(UTC),
                    response_data=failure,
                    error_message=error_message,
                )
                is not None
            )

    if transitioned:
        publish_workflow_notifications(step_id, terminal_status, tenant_id)
    return transitioned


def recover_stale_creative_unblock_workflows() -> CreativeUnblockRecoveryResult:
    """Reclaim expired leases and safely resume or terminalize each workflow."""
    cutoff = datetime.now(UTC) - timedelta(seconds=CREATIVE_UNBLOCK_LEASE_SECONDS)
    with get_db_session() as session:
        leases = WorkflowRepository.list_stale_creative_unblock_leases(session, before=cutoff)

    recovered = 0
    deferred = 0
    for lease in leases:
        with AdminCreativeUoW(lease.tenant_id) as uow:
            assert uow.media_buys is not None
            assert uow.workflows is not None
            step = uow.workflows.reclaim_processing_lease(lease.step_id, before=cutoff)
            if step is None:
                continue
            media_buy = uow.media_buys.get_by_id_for_update(lease.media_buy_id)
            media_status = media_buy.status if media_buy is not None else "missing"
            lease_started_at = step.processing_started_at
            assert lease_started_at is not None

        if media_status in _PROVIDER_APPLIED_STATUSES:
            success, error_message = True, None
        elif media_status in _RETRYABLE_LOCAL_STATUSES:
            try:
                approval = execute_approved_media_buy(
                    lease.media_buy_id,
                    lease.tenant_id,
                    raise_retryable=True,
                )
                success, error_message = approval.ok, approval.error_msg
            except AdCPServiceUnavailableError:
                logger.warning(
                    "Creative-unblock recovery remains ambiguous for %s/%s; lease will retry",
                    lease.tenant_id,
                    lease.media_buy_id,
                    exc_info=True,
                )
                deferred += 1
                continue
        else:
            success = False
            error_message = f"Media buy entered non-success status '{media_status}' during recovery"

        if finalize_creative_unblock_workflow(
            tenant_id=lease.tenant_id,
            step_id=lease.step_id,
            media_buy_id=lease.media_buy_id,
            lease_started_at=lease_started_at,
            success=success,
            error_message=error_message,
        ):
            recovered += 1
    return CreativeUnblockRecoveryResult(recovered=recovered, deferred=deferred)
