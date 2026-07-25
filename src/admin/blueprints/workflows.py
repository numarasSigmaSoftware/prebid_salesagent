"""Workflow approval and review blueprint for Admin UI."""

import json
import logging
from datetime import UTC, datetime
from typing import Any

from flask import Blueprint, Response, flash, jsonify, redirect, render_template, request, url_for
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.admin.utils import echo_context, require_tenant_access, session_user_email
from src.admin.utils.audit_decorator import log_admin_action
from src.core.database.database_session import get_db_session
from src.core.database.models import Context
from src.core.database.models import Principal as ModelPrincipal
from src.core.database.repositories import MediaBuyRepository
from src.core.database.repositories.creative import CreativeAssignmentRepository, CreativeRepository
from src.core.database.repositories.workflow import APPROVABLE_STEP_STATUSES, WorkflowRepository
from src.core.workflow_finalization import finalize_media_buy_approval_step

logger = logging.getLogger(__name__)

workflows_bp = Blueprint("workflows", __name__)


@workflows_bp.route("/<tenant_id>/workflows")
@require_tenant_access()
def list_workflows(tenant_id, **kwargs):
    """List all workflows and pending approvals."""
    from src.core.database.models import AuditLog, Tenant

    with get_db_session() as db:
        # Get tenant
        tenant = db.scalars(select(Tenant).filter_by(tenant_id=tenant_id)).first()
        if not tenant:
            return "Tenant not found", 404

        # Get all workflow steps via repository (tenant-scoped)
        workflow_repo = WorkflowRepository(db, tenant_id)
        all_steps = workflow_repo.get_all_steps()

        # Separate pending approval steps for summary. Uses the canonical approvable set, not an
        # inline literal: a subset literal here undercounts (it would drop ``requires_approval``
        # and the legacy ``approval`` alias) and drifts the moment the set changes.
        pending_steps = [s for s in all_steps if s.status in APPROVABLE_STEP_STATUSES]

        # Get media buys for context
        media_buy_repo = MediaBuyRepository(db, tenant_id)
        media_buys = media_buy_repo.list_all_ordered_by_created()

        # Build summary stats
        summary = {
            "active_buys": len([mb for mb in media_buys if mb.status == "active"]),
            "pending_tasks": len(pending_steps),
            "completed_today": 0,  # TODO: Calculate from workflow history
            "total_spend": sum(mb.budget or 0 for mb in media_buys if mb.status == "active"),
        }

        # Format all workflow steps for display in tasks tab
        workflows_list = []
        for step in all_steps:
            context = db.scalars(select(Context).filter_by(context_id=step.context_id)).first()
            principal = None
            if context and context.principal_id:
                principal = db.scalars(
                    select(ModelPrincipal).filter_by(principal_id=context.principal_id, tenant_id=tenant_id)
                ).first()

            workflows_list.append(
                {
                    "step_id": step.step_id,
                    "context_id": step.context_id,
                    "step_type": step.step_type,
                    "tool_name": step.tool_name,
                    "status": step.status,
                    "created_at": step.created_at,
                    "completed_at": step.completed_at,
                    "principal_name": principal.name if principal else "Unknown",
                    "assigned_to": step.assigned_to,
                    "error_message": step.error_message,
                    "request_data": step.request_data,
                }
            )

        # Get recent audit logs
        stmt = select(AuditLog).filter(AuditLog.tenant_id == tenant_id).order_by(AuditLog.timestamp.desc()).limit(100)
        audit_logs = db.scalars(stmt).all()

        logger.info(f"[workflows] Querying audit logs for tenant_id={tenant_id}")
        logger.info(f"[workflows] Found {len(audit_logs)} audit logs")
        if audit_logs:
            logger.info(
                f"[workflows] Latest audit log: operation={audit_logs[0].operation}, success={audit_logs[0].success}, timestamp={audit_logs[0].timestamp}"
            )
        else:
            all_logs_stmt = select(AuditLog).order_by(AuditLog.timestamp.desc()).limit(5)
            all_logs = db.scalars(all_logs_stmt).all()
            logger.warning(
                f"[workflows] No audit logs for tenant {tenant_id}, but found {len(all_logs)} logs total in database"
            )
            if all_logs:
                logger.warning(f"[workflows] Sample log tenant_ids: {[log.tenant_id for log in all_logs]}")

        return render_template(
            "workflows.html",
            tenant=tenant,
            tenant_id=tenant_id,
            summary=summary,
            workflows=workflows_list,
            media_buys=media_buys,
            tasks=workflows_list,
            audit_logs=audit_logs,
        )


@workflows_bp.route("/<tenant_id>/workflows/<workflow_id>/steps/<step_id>/review")
@require_tenant_access()
def review_workflow_step(tenant_id, workflow_id, step_id):
    """Show detailed review page for a workflow step requiring approval."""
    with get_db_session() as db:
        # Get the workflow step via repository (tenant-scoped)
        workflow_repo = WorkflowRepository(db, tenant_id)
        step = workflow_repo.get_step_by_id(step_id)

        if not step:
            flash("Workflow step not found", "error")
            return redirect(url_for("tenants.dashboard", tenant_id=tenant_id))

        # Get the context for tenant/principal info
        context = db.scalars(select(Context).filter_by(context_id=step.context_id)).first()

        # Get principal info
        principal = None
        if context and context.principal_id:
            principal = db.scalars(
                select(ModelPrincipal).filter_by(principal_id=context.principal_id, tenant_id=tenant_id)
            ).first()

        # Parse request data
        request_data = step.request_data if step.request_data else {}

        # Format the data for display
        formatted_request = json.dumps(request_data, indent=2)

        return render_template(
            "workflow_review.html",
            tenant_id=tenant_id,
            workflow_id=workflow_id,
            step=step,
            context=context,
            principal=principal,
            request_data=request_data,
            formatted_request=formatted_request,
        )


def _refused_decision_response(
    workflow_repo: WorkflowRepository, step_id: str, awaiting_desc: str
) -> tuple[Response, int]:
    """Map a refused approval/rejection compare-and-set to the right HTTP error.

    ``claim_approval`` / ``reject_if_approvable`` return None for EITHER a step that does not
    exist (404) OR one that exists but is no longer in an approvable status (409 Conflict —
    e.g. already approved by a concurrent request, or canceled). Distinguish via a tenant-scoped
    fetch so a genuine concurrency conflict returns 409, not a misleading 404.
    """
    existing = workflow_repo.get_by_step_id(step_id)
    if existing is None:
        return jsonify({"error": "Workflow step not found"}), 404
    return jsonify({"error": f"Workflow step is not {awaiting_desc} (status: {existing.status})"}), 409


def _unapproved_creative_ids(db: Session, tenant_id: str, media_buy_id: str) -> list[str]:
    """Return creatives that still block adapter execution for a media buy."""
    assignments = CreativeAssignmentRepository(db, tenant_id).get_by_media_buy(media_buy_id)
    if not assignments:
        return []
    creative_ids = [assignment.creative_id for assignment in assignments]
    creatives = CreativeRepository(db, tenant_id).admin_get_by_ids(creative_ids)
    return [creative.creative_id for creative in creatives if creative.status not in ["approved", "active"]]


def _complete_plain_workflow_approval(db: Session, tenant_id: str, step_id: str) -> tuple[Response, int]:
    """Complete a no-execution approval in the claim's original transaction."""
    completed = WorkflowRepository(db, tenant_id).complete_claimed_approval(step_id)
    if completed is None:
        db.rollback()
        logger.error("[APPROVAL] Claimed workflow step %s could not be completed atomically", step_id)
        return jsonify({"success": False, "error": "Workflow result could not be finalized"}), 503
    db.commit()
    flash("Workflow step approved successfully", "success")
    return jsonify({"success": True}), 200


def _complete_executed_media_buy(
    *,
    db: Session,
    media_buy_repo: MediaBuyRepository,
    tenant_id: str,
    step_id: str,
    media_buy_id: str,
    request_data: dict[str, Any],
    user_email: str,
) -> tuple[Response, int]:
    """Publish domain and workflow success after adapter execution."""
    db.expire_all()
    media_buy = media_buy_repo.get_by_id(media_buy_id)
    if media_buy is None or media_buy.status != "active":
        logger.error(
            "[APPROVAL] Media buy %s did not retain its active execution result; leaving workflow pending",
            media_buy_id,
        )
        message = (
            "Media buy was created externally, but its active local state could not be confirmed. "
            "The workflow remains pending for safe reconciliation."
        )
        return jsonify({"success": False, "error": message, "pending": True}), 503

    media_buy_repo.update_status(
        media_buy_id,
        "active",
        approved_at=datetime.now(UTC),
        approved_by=user_email,
    )
    db.commit()

    finalization = finalize_media_buy_approval_step(
        tenant_id=tenant_id,
        step_id=step_id,
        media_buy_id=media_buy_id,
        succeeded=True,
        context=echo_context(request_data),
    )
    if not finalization.applied:
        logger.error("[APPROVAL] Adapter succeeded but workflow step %s could not be finalized", step_id)
        return jsonify({"success": False, "error": "Workflow result could not be finalized"}), 500

    logger.info("[APPROVAL] Media buy %s successfully created in adapter", media_buy_id)
    flash("Workflow step approved and media buy created successfully", "success")
    return jsonify({"success": True}), 200


def _execute_claimed_media_buy(
    *,
    db: Session,
    media_buy_repo: MediaBuyRepository,
    tenant_id: str,
    step_id: str,
    media_buy_id: str,
    request_data: dict[str, Any],
    user_email: str,
) -> tuple[Response, int]:
    """Claim once, execute externally, and route the durable outcome."""
    if not media_buy_repo.claim_approved_execution(media_buy_id):
        db.rollback()
        return jsonify({"success": False, "error": "Media buy is already executing or no longer pending"}), 409
    db.commit()

    from src.core.tools.media_buy_create import execute_approved_media_buy

    logger.info("[APPROVAL] Executing adapter creation for approved media buy %s", media_buy_id)
    success, error_msg = execute_approved_media_buy(
        media_buy_id,
        tenant_id,
        execution_claimed=True,
    )
    if success is None:
        logger.error(
            "[APPROVAL] External media buy creation succeeded but activation remains pending for %s",
            media_buy_id,
        )
        message = (
            "Media buy was created externally, but activation could not be finalized. "
            "The workflow remains pending for safe reconciliation."
        )
        flash(message, "warning")
        return jsonify({"success": False, "error": message, "pending": True}), 503
    if success is False:
        finalize_media_buy_approval_step(
            tenant_id=tenant_id,
            step_id=step_id,
            media_buy_id=media_buy_id,
            succeeded=False,
            error_message=error_msg,
        )
        logger.error("[APPROVAL] Adapter creation failed for %s: %s", media_buy_id, error_msg)
        flash(f"Workflow approved but media buy creation failed: {error_msg}", "error")
        return jsonify({"success": False, "error": error_msg}), 500
    return _complete_executed_media_buy(
        db=db,
        media_buy_repo=media_buy_repo,
        tenant_id=tenant_id,
        step_id=step_id,
        media_buy_id=media_buy_id,
        request_data=request_data,
        user_email=user_email,
    )


def _approve_mapped_media_buy(
    *,
    db: Session,
    tenant_id: str,
    step_id: str,
    media_buy_id: str,
    request_data: dict[str, Any],
    user_email: str,
) -> tuple[Response, int]:
    """Handle the media-buy-specific portion of a claimed workflow approval."""
    media_buy_repo = MediaBuyRepository(db, tenant_id)
    media_buy = media_buy_repo.get_by_id(media_buy_id)
    logger.info(
        "[APPROVAL] Media buy lookup: found=%s, status=%s",
        media_buy is not None,
        media_buy.status if media_buy else "N/A",
    )
    if media_buy is None or media_buy.status != "pending_approval":
        logger.warning(
            "[APPROVAL] Media buy not executed: media_buy=%s, status=%s",
            media_buy is not None,
            media_buy.status if media_buy else "N/A",
        )
        return _complete_plain_workflow_approval(db, tenant_id, step_id)

    unapproved_creatives = _unapproved_creative_ids(db, tenant_id, media_buy_id)
    if unapproved_creatives:
        logger.warning(
            "[APPROVAL] Cannot execute adapter creation yet - %s creatives not approved: %s",
            len(unapproved_creatives),
            unapproved_creatives,
        )
        flash(
            f"Media buy approved! Waiting for {len(unapproved_creatives)} creative(s) "
            "to be approved before creating in GAM.",
            "info",
        )
        media_buy.status = "pending_creatives"
        db.commit()
        return jsonify({"success": True}), 200

    return _execute_claimed_media_buy(
        db=db,
        media_buy_repo=media_buy_repo,
        tenant_id=tenant_id,
        step_id=step_id,
        media_buy_id=media_buy_id,
        request_data=request_data,
        user_email=user_email,
    )


@workflows_bp.route("/<tenant_id>/workflows/<workflow_id>/steps/<step_id>/approve", methods=["POST"])
@require_tenant_access()
@log_admin_action("approve_workflow_step")
def approve_workflow_step(tenant_id, workflow_id, step_id):
    """Approve a workflow step.

    ``workflow_id`` is a cosmetic path segment only: WorkflowStep has no workflow_id
    column and the value is never populated (an unwired stub — see the TODO at
    mcp_context_wrapper). The step is a tenant-scoped primary key, so authorization is
    complete at (tenant, step_id); there is nothing to scope against the URL's workflow.
    Wiring a real workflow grouping (and validating the step against it) is separate work.
    """
    del workflow_id  # cosmetic; see docstring
    try:
        with get_db_session() as db:
            # Get and update the workflow step via repository (tenant-scoped)
            workflow_repo = WorkflowRepository(db, tenant_id)

            user_email = session_user_email(default="system")

            # Atomic compare-and-set: requires_approval/pending_approval → approved. Because
            # ``approved`` is non-terminal, a broad terminal-guard would let a second concurrent
            # approver win an approved→approved no-op and ALSO run execute_approved_media_buy
            # below (duplicate adapter work). claim_approval admits exactly one approver; a
            # loser gets None → 409 Conflict (not 404) and does NOT execute.
            step = workflow_repo.claim_approval(step_id)

            if not step:
                return _refused_decision_response(workflow_repo, step_id, "awaiting approval")

            request_data = dict(step.request_data) if isinstance(step.request_data, dict) else {}

            # Check if this is a media buy creation workflow step
            mappings = workflow_repo.get_mappings_for_step(step_id)
            mapping = next((m for m in mappings if m.object_type == "media_buy"), None)

            logger.info(
                f"[APPROVAL] Checking for ObjectWorkflowMapping: step_id={step_id}, found={mapping is not None}"
            )
            if mapping:
                logger.info(
                    f"[APPROVAL] Found mapping: object_type={mapping.object_type}, object_id={mapping.object_id}"
                )

            if mapping:
                media_buy_id = mapping.object_id
                logger.info(f"[APPROVAL] Workflow step {step_id} approved for media buy {media_buy_id}")
                return _approve_mapped_media_buy(
                    db=db,
                    tenant_id=tenant_id,
                    step_id=step_id,
                    media_buy_id=media_buy_id,
                    request_data=request_data,
                    user_email=user_email,
                )
            return _complete_plain_workflow_approval(db, tenant_id, step_id)

    except Exception as e:
        logger.error(f"Error approving workflow step {step_id}: {e}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


@workflows_bp.route("/<tenant_id>/workflows/<workflow_id>/steps/<step_id>/reject", methods=["POST"])
@require_tenant_access()
@log_admin_action("reject_workflow_step")
def reject_workflow_step(tenant_id, workflow_id, step_id):
    """Reject a workflow step with a reason.

    ``workflow_id`` is a cosmetic path segment only (see ``approve_workflow_step``): no
    backing column, never populated; authorization is complete at (tenant, step_id).
    """
    del workflow_id  # cosmetic; see approve_workflow_step
    try:
        data = request.get_json() or {}
        reason = data.get("reason", "No reason provided")

        with get_db_session() as db:
            # Get and update the workflow step via repository (tenant-scoped)
            workflow_repo = WorkflowRepository(db, tenant_id)

            user_email = session_user_email(default="system")

            # Atomic compare-and-set with the SAME source-state guard as approve: a step that
            # has already been approved (execution underway) cannot be rejected — that would
            # strand a live ad-server order behind a rejected workflow. A loser gets None →
            # 409 Conflict (not 404).
            step = workflow_repo.reject_if_approvable(step_id, error_message=reason)

            if not step:
                return _refused_decision_response(workflow_repo, step_id, "awaiting a decision")

            db.commit()

            flash("Workflow step rejected", "info")
            return jsonify({"success": True}), 200

    except Exception as e:
        logger.error(f"Error rejecting workflow step {step_id}: {e}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500
