"""Shared creative review domain flow for admin routes and task completion."""

from __future__ import annotations

import asyncio
import logging
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from src.core.database.repositories.creative import CreativeRepository
from src.core.database.repositories.uow import AdminCreativeUoW

logger = logging.getLogger(__name__)


@dataclass
class CreativeReviewSideEffects:
    """Post-commit side effects to execute after a creative review decision commits."""

    webhook_data: dict[str, Any]
    slack_data: dict[str, Any]
    audit_data: dict[str, Any]
    media_buy_ids_to_execute: list[str]


def _run_async_side_effect(coro):
    """Run an async side effect from either sync or async call sites."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    result: dict[str, Any] = {}
    error: dict[str, BaseException] = {}

    def _runner() -> None:
        try:
            result["value"] = asyncio.run(coro)
        except BaseException as exc:  # pragma: no cover - re-raised below
            error["value"] = exc

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    thread.join()

    if "value" in error:
        raise error["value"]
    return result.get("value")


def _compute_media_buy_status_from_flight_dates(media_buy) -> str:
    """Compute status based on flight dates: 'active' if within window, else 'scheduled'."""
    now = datetime.now(UTC)

    start_time = None
    if media_buy.start_time:
        raw_start = media_buy.start_time
        start_time = raw_start.replace(tzinfo=UTC) if raw_start.tzinfo is None else raw_start.astimezone(UTC)
    elif media_buy.start_date:
        start_time = datetime.combine(media_buy.start_date, datetime.min.time()).replace(tzinfo=UTC)

    end_time = None
    if media_buy.end_time:
        raw_end = media_buy.end_time
        end_time = raw_end.replace(tzinfo=UTC) if raw_end.tzinfo is None else raw_end.astimezone(UTC)
    elif media_buy.end_date:
        end_time = datetime.combine(media_buy.end_date, datetime.max.time()).replace(tzinfo=UTC)

    if start_time and end_time and now >= start_time and now <= end_time:
        return "active"

    return "scheduled"


def _create_human_review_record(
    creative_repo: CreativeRepository,
    *,
    creative_id: str,
    tenant_id: str,
    principal_id: str,
    reviewer_email: str,
    reason: str,
    is_override: bool,
    final_decision: str,
):
    """Create and add a human CreativeReview record via the repository."""
    from src.core.database.models import CreativeReview

    review_id = f"review_{uuid.uuid4().hex[:12]}"
    human_review = CreativeReview(
        review_id=review_id,
        creative_id=creative_id,
        tenant_id=tenant_id,
        principal_id=principal_id,
        reviewed_at=datetime.now(UTC),
        review_type="human",
        reviewer_email=reviewer_email,
        ai_decision=None,
        confidence_score=None,
        policy_triggered=None,
        reason=reason,
        recommendations=None,
        human_override=is_override,
        final_decision=final_decision,
    )
    creative_repo.create_review(human_review)
    return human_review


async def _call_webhook_for_creative_status(creative_id: str, tenant_id: str) -> bool:
    """Send protocol-level push notification for creative status update."""
    if not tenant_id:
        raise ValueError("tenant_id is required for _call_webhook_for_creative_status")

    from src.core.schemas import CreativeStatusEnum
    from src.services.protocol_webhook_service import get_protocol_webhook_service

    try:
        with AdminCreativeUoW(tenant_id) as uow:
            assert uow.workflows is not None
            assert uow.creatives is not None
            mapping = uow.workflows.get_latest_mapping_for_object("creative", creative_id)

            if not mapping:
                logger.debug(f"No workflow mapping found for creative {creative_id}; skipping webhook notification")
                return False

            step = uow.workflows.get_step_by_id(mapping.step_id)
            if not step or not step.request_data:
                logger.debug(
                    f"Workflow step missing or has no request_data for creative {creative_id}; skipping webhook notification"
                )
                return False

            all_mappings = [m for m in uow.workflows.get_mappings_for_step(step.step_id) if m.object_type == "creative"]
            if not all_mappings:
                logger.debug(f"No creative mappings found for workflow step {step.step_id}")
                return False

            creative_ids = [m.object_id for m in all_mappings]
            all_creatives = uow.creatives.admin_get_by_ids(creative_ids)
            pending_count = sum(1 for c in all_creatives if c.status == CreativeStatusEnum.pending_review.value)

            if pending_count > 0:
                logger.info(
                    f"Creative {creative_id} reviewed, but {pending_count}/{len(all_creatives)} "
                    f"creatives still pending in task {step.step_id}; not firing webhook yet"
                )
                return False

            logger.info(f"All {len(all_creatives)} creatives in task {step.step_id} have been reviewed; firing webhook")

            from adcp.types import McpWebhookPayload, SyncCreativeResult, SyncCreativesSuccessResponse
            from adcp import create_a2a_webhook_payload, create_mcp_webhook_payload
            from adcp.webhooks import GeneratedTaskStatus

            creatives: list[SyncCreativeResult] = [
                SyncCreativeResult(
                    creative_id=creative.creative_id,
                    action="updated",
                    status=creative.status,
                    platform_id=None,
                    errors=[],
                    review_feedback=None,
                    assigned_to=None,
                    assignment_errors=None,
                )
                for creative in all_creatives
            ]

            response = SyncCreativesSuccessResponse(
                creatives=creatives,
                dry_run=False,
                context=step.request_data.get("context") if isinstance(step.request_data, dict) else None,
            )

            push_config = None
            protocol = "mcp"
            if isinstance(step.request_data, dict):
                push_config = step.request_data.get("push_notification_config")
                protocol = step.request_data.get("protocol", "mcp")

            if not push_config:
                logger.debug(f"No push_notification_config found in workflow step {step.step_id}; skipping webhook")
                return False

            webhook_service = get_protocol_webhook_service()

            if protocol == "a2a":
                payload = create_a2a_webhook_payload(
                    task_id=step.context_id,
                    status=GeneratedTaskStatus.COMPLETED,
                    response=response,
                )
            else:
                payload = create_mcp_webhook_payload(
                    payload=McpWebhookPayload(
                        task_id=step.context_id,
                        status=GeneratedTaskStatus.COMPLETED,
                        response=response,
                    )
                )

            return webhook_service.send(
                push_notification_config=push_config,
                payload=payload,
                protocol=protocol,
            )
    except Exception as e:
        logger.warning(f"Failed to send creative status webhook for {creative_id}: {e}")
        return False


def apply_creative_review_decision(
    uow: AdminCreativeUoW,
    *,
    creative_id: str,
    decision: str,
    actor: str,
    rejection_reason: str | None = None,
) -> CreativeReviewSideEffects:
    """Apply an approval/rejection decision to a creative inside an open AdminCreativeUoW."""
    assert uow.creatives is not None
    assert uow.assignments is not None
    assert uow.media_buys is not None
    assert uow.tenant_config is not None

    creative = uow.creatives.admin_get_by_id(creative_id)
    if not creative:
        raise ValueError(f"Creative {creative_id} not found")

    prior_ai_review = uow.creatives.get_prior_ai_review(creative_id)
    is_approval = decision == "approved"
    is_override = bool(
        prior_ai_review
        and (
            (is_approval and prior_ai_review.ai_decision in ["rejected", "reject"])
            or (not is_approval and prior_ai_review.ai_decision in ["approved", "approve"])
        )
    )

    review_reason = "Human approval" if is_approval else (rejection_reason or "Rejected via complete_task")
    final_decision = "approved" if is_approval else "rejected"

    _create_human_review_record(
        uow.creatives,
        creative_id=creative_id,
        tenant_id=uow.creatives.tenant_id,
        principal_id=creative.principal_id,
        reviewer_email=actor,
        reason=review_reason,
        is_override=is_override,
        final_decision=final_decision,
    )

    creative.status = final_decision
    creative.approved_at = datetime.now(UTC)
    creative.approved_by = actor

    if not is_approval:
        if not creative.data:
            creative.data = {}
        creative.data["rejection_reason"] = review_reason
        creative.data["rejected_at"] = datetime.now(UTC).isoformat()
        uow.creatives.update_data(creative, creative.data)

    webhook_data = {"creative_id": creative_id, "tenant_id": uow.creatives.tenant_id}

    slack_data: dict[str, Any] = {}
    tenant = uow.tenant_config.get_tenant()
    if tenant and tenant.slack_webhook_url:
        principal_name = uow.creatives.get_principal_name(creative.principal_id)
        if is_approval:
            message = f"\u2705 Creative approved: {creative.name} ({creative.format}) from {principal_name}"
        else:
            message = (
                f"\u274c Creative rejected: {creative.name} ({creative.format}) from {principal_name}\n"
                f"Reason: {review_reason}"
            )
        slack_data = {"slack_webhook_url": tenant.slack_webhook_url, "message": message}

    audit_data = {
        "creative_id": creative_id,
        "creative_name": creative.name,
        "format": creative.format,
        "principal_id": creative.principal_id,
        "human_override": is_override,
    }
    if not is_approval:
        audit_data["rejection_reason"] = review_reason

    media_buy_ids_to_execute: list[str] = []
    if is_approval:
        assignments = uow.assignments.get_by_creative(creative_id)
        for assignment in assignments:
            media_buy_id = assignment.media_buy_id
            media_buy = uow.media_buys.get_by_id(media_buy_id)
            if not media_buy or media_buy.status not in {"pending_creatives", "draft"}:
                continue

            all_assignments = uow.assignments.get_by_media_buy(media_buy_id)
            creative_ids = [a.creative_id for a in all_assignments]
            all_creatives = uow.creatives.admin_get_by_ids(creative_ids)
            unapproved_creatives = [c.creative_id for c in all_creatives if c.status not in ["approved", "active"]]
            if not unapproved_creatives:
                media_buy_ids_to_execute.append(media_buy_id)

    return CreativeReviewSideEffects(
        webhook_data=webhook_data,
        slack_data=slack_data,
        audit_data=audit_data,
        media_buy_ids_to_execute=media_buy_ids_to_execute,
    )


def execute_creative_review_side_effects(
    side_effects: list[CreativeReviewSideEffects],
    *,
    tenant_id: str,
    actor: str,
    operation: str,
) -> None:
    """Execute post-commit side effects for one or more creative review decisions."""
    from src.core.audit_logger import AuditLogger

    media_buy_ids_to_execute: set[str] = set()

    for effect in side_effects:
        if effect.webhook_data:
            _run_async_side_effect(
                _call_webhook_for_creative_status(
                    creative_id=effect.webhook_data["creative_id"],
                    tenant_id=effect.webhook_data["tenant_id"],
                )
            )

        if effect.slack_data:
            try:
                from src.services.slack_notifier import get_slack_notifier

                tenant_config = {"features": {"slack_webhook_url": effect.slack_data["slack_webhook_url"]}}
                notifier = get_slack_notifier(tenant_config)
                notifier.send_message(effect.slack_data["message"])
            except Exception as slack_e:
                logger.warning(f"Failed to send Slack notification: {slack_e}")

        if effect.audit_data:
            audit_logger = AuditLogger(adapter_name="AdminUI", tenant_id=tenant_id)
            audit_logger.log_operation(
                operation=operation,
                principal_name=actor,
                principal_id=actor,
                adapter_id="admin_ui",
                success=True,
                details=effect.audit_data,
                tenant_id=tenant_id,
            )

        media_buy_ids_to_execute.update(effect.media_buy_ids_to_execute)

    if not media_buy_ids_to_execute:
        return

    from src.core.tools.media_buy_create import execute_approved_media_buy

    for media_buy_id in sorted(media_buy_ids_to_execute):
        logger.info(f"[CREATIVE REVIEW] All creatives approved for media buy {media_buy_id}, executing adapter creation")
        success, error_msg = execute_approved_media_buy(media_buy_id, tenant_id)
        if success:
            with AdminCreativeUoW(tenant_id) as uow:
                assert uow.media_buys is not None
                mb = uow.media_buys.get_by_id(media_buy_id)
                if mb:
                    new_status = _compute_media_buy_status_from_flight_dates(mb)
                    mb.status = new_status
                    mb.approved_at = datetime.now(UTC)
                    mb.approved_by = "system"
            continue

        logger.error(f"[CREATIVE REVIEW] Adapter creation failed for {media_buy_id}: {error_msg}")
