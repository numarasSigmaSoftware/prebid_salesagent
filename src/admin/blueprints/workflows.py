"""Workflow approval and review blueprint for Admin UI."""

import json
import logging

from flask import Blueprint, flash, jsonify, redirect, render_template, request, session, url_for
from sqlalchemy import select

from src.admin.utils import approve_media_buy_through_writer, require_tenant_access
from src.admin.utils.audit_decorator import log_admin_action
from src.admin.utils.media_buy_approval import build_approved_media_buy_result
from src.core.context_manager import publish_workflow_notifications
from src.core.database.database_session import get_db_session
from src.core.database.models import Context
from src.core.database.models import Principal as ModelPrincipal
from src.core.database.repositories import MediaBuyRepository
from src.core.database.repositories.workflow import WorkflowRepository
from src.core.exceptions import AdCPAdapterError, AdCPPolicyViolationError, build_two_layer_error_envelope
from src.core.tools.media_buy_create import ApprovalOutcome

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

        # Separate pending approval steps for summary
        pending_steps = [s for s in all_steps if s.status == "pending_approval"]

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


@workflows_bp.route("/<tenant_id>/workflows/<workflow_id>/steps/<step_id>/approve", methods=["POST"])
@require_tenant_access()
@log_admin_action("approve_workflow_step")
def approve_workflow_step(tenant_id, workflow_id, step_id):
    """Approve a workflow step."""
    try:
        with get_db_session() as db:
            # Get and update the workflow step via repository (tenant-scoped)
            workflow_repo = WorkflowRepository(db, tenant_id)

            user_info = session.get("user", {})
            user_email = user_info.get("email", "system") if isinstance(user_info, dict) else str(user_info)

            existing_step = workflow_repo.get_by_step_and_context(step_id, workflow_id)
            if existing_step is None:
                return jsonify({"error": "Workflow step not found"}), 404

            step = workflow_repo.transition_status(
                step_id,
                status="approved",
                allowed_from={"pending", "pending_approval", "requires_approval", "submitted", "input-required"},
            )

            if not step:
                return jsonify({"error": "Workflow step is no longer actionable"}), 409

            db.commit()

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

                # Get the media buy
                media_buy_repo = MediaBuyRepository(db, tenant_id)
                media_buy = media_buy_repo.get_by_id(media_buy_id)

                logger.info(
                    f"[APPROVAL] Media buy lookup: found={media_buy is not None}, status={media_buy.status if media_buy else 'N/A'}"
                )

                if media_buy and media_buy.status == "pending_approval":
                    approval = approve_media_buy_through_writer(media_buy_id, tenant_id, approved_by=user_email)

                    if approval.outcome is ApprovalOutcome.HELD_PENDING_CREATIVES:
                        return jsonify({"success": True}), 200

                    # approve_media_buy_through_writer already ran the creative gate,
                    # executed the adapter, and resolved/persisted the flight-window
                    # status and revision bump in one write — this route must not
                    # re-derive any of that or re-execute the adapter.
                    if not approval.ok:
                        logger.error(f"[APPROVAL] Adapter creation failed for {media_buy_id}: {approval.error_msg}")
                        failure = build_two_layer_error_envelope(
                            AdCPAdapterError(approval.error_msg or "Adapter creation failed")
                        )
                        workflow_repo.update_status(
                            step_id,
                            status="failed",
                            response_data=failure,
                            error_message=approval.error_msg,
                        )
                        db.commit()
                        publish_workflow_notifications(step_id, "failed", tenant_id)
                        flash(f"Workflow approved but media buy creation failed: {approval.error_msg}", "error")
                        return jsonify({"success": False, "error": approval.error_msg}), 500

                    result = build_approved_media_buy_result(
                        media_buy_repo,
                        media_buy_id,
                        step.request_data if isinstance(step.request_data, dict) else {},
                    )
                    workflow_repo.update_status(
                        step_id,
                        status="completed",
                        response_data=result.model_dump(mode="json"),
                    )
                    db.commit()
                    publish_workflow_notifications(step_id, "completed", tenant_id)

                    logger.info(f"[APPROVAL] Media buy {media_buy_id} successfully created in adapter")
                    flash("Workflow step approved and media buy created successfully", "success")
                else:
                    logger.warning(
                        f"[APPROVAL] Media buy not executed: media_buy={media_buy is not None}, status={media_buy.status if media_buy else 'N/A'}"
                    )
                    flash("Workflow step approved successfully", "success")
            else:
                workflow_repo.update_status(
                    step_id,
                    status="completed",
                    response_data={"status": "approved"},
                )
                db.commit()
                publish_workflow_notifications(step_id, "completed", tenant_id)
                flash("Workflow step approved successfully", "success")

            return jsonify({"success": True}), 200

    except Exception as e:
        logger.error(f"Error approving workflow step {step_id}: {e}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


@workflows_bp.route("/<tenant_id>/workflows/<workflow_id>/steps/<step_id>/reject", methods=["POST"])
@require_tenant_access()
@log_admin_action("reject_workflow_step")
def reject_workflow_step(tenant_id, workflow_id, step_id):
    """Reject a workflow step with a reason."""
    try:
        data = request.get_json() or {}
        reason = data.get("reason", "No reason provided")

        with get_db_session() as db:
            # Get and update the workflow step via repository (tenant-scoped)
            workflow_repo = WorkflowRepository(db, tenant_id)

            user_info = session.get("user", {})
            user_email = user_info.get("email", "system") if isinstance(user_info, dict) else str(user_info)

            existing_step = workflow_repo.get_by_step_and_context(step_id, workflow_id)
            if existing_step is None:
                return jsonify({"error": "Workflow step not found"}), 404

            step = workflow_repo.transition_status(
                step_id,
                status="rejected",
                allowed_from={"pending", "pending_approval", "requires_approval", "submitted", "input-required"},
                error_message=reason,
                response_data=build_two_layer_error_envelope(AdCPPolicyViolationError(reason)),
            )

            if not step:
                return jsonify({"error": "Workflow step is no longer actionable"}), 409

            db.commit()
            publish_workflow_notifications(step_id, "rejected", tenant_id)

            flash("Workflow step rejected", "info")
            return jsonify({"success": True}), 200

    except Exception as e:
        logger.error(f"Error rejecting workflow step {step_id}: {e}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500
