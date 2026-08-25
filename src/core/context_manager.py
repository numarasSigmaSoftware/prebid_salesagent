"""Context persistence manager for A2A protocol support."""

import asyncio
import logging
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from functools import partial
from typing import Any

from a2a.types import Task, TaskStatusUpdateEvent
from adcp import create_mcp_webhook_payload
from adcp.types import McpWebhookPayload
from adcp.webhooks import GeneratedTaskStatus
from pydantic import BaseModel
from rich.console import Console
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from src.core.async_utils import pin_task
from src.core.database.database_session import DatabaseManager, defer_until_after_commit, get_independent_db_session
from src.core.database.models import Context, ObjectWorkflowMapping, WorkflowStep
from src.core.database.models import Context as DBContext
from src.core.database.repositories.push_notification_config import (
    PushNotificationConfigRepository,
    task_push_config_id,
)
from src.core.database.repositories.workflow import WorkflowRepository
from src.core.exceptions import (
    AdCPError,
    build_two_layer_error_envelope,
    is_guaranteed_wire_error_code,
    normalize_to_adcp_error,
)
from src.core.logging_config import scrub_control_chars
from src.core.security.webhook_http import redact_webhook_url
from src.core.webhook_validator import validate_webhook_task_type
from src.services.protocol_webhook_service import get_protocol_webhook_service

logger = logging.getLogger(__name__)

console = Console()


def _add_object_workflow_mappings(
    session: Any,
    mappings: list[dict[str, str]] | None,
    *,
    step_id: str,
    step_type: str,
) -> None:
    """Add optional workflow mappings without inflating orchestration branches."""
    for mapping in mappings or []:
        session.add(
            ObjectWorkflowMapping(
                object_type=mapping["object_type"],
                object_id=mapping["object_id"],
                step_id=step_id,
                action=mapping.get("action", step_type),
                created_at=datetime.now(UTC),
            )
        )


def _serialize_workflow_request(
    request_data: dict[str, Any] | Any | None,
    request_metadata: dict[str, Any] | None,
) -> dict[str, Any] | Any | None:
    """Serialize and enrich workflow input at the persistence boundary."""
    if isinstance(request_data, BaseModel):
        request_data = request_data.model_dump(mode="json")
    if request_metadata and request_data is not None:
        request_data.update(request_metadata)
    return request_data


class ContextManager(DatabaseManager):
    """Manages persistent context for conversations and tasks.

    Inherits from DatabaseManager for standardized session management.
    """

    def __init__(self):
        super().__init__()

    def create_context(
        self,
        tenant_id: str,
        principal_id: str,
        initial_conversation: list[dict[str, Any]] | None = None,
        *,
        context_id: str | None = None,
    ) -> Context:
        """Create a new context for asynchronous operations.

        Note: Synchronous operations don't need a context.
        This is only for async/HITL workflows where we need to track conversation.

        Args:
            tenant_id: The tenant ID
            principal_id: The principal ID
            initial_conversation: Optional initial conversation history

        Returns:
            The created Context object
        """
        context_id = context_id or f"ctx_{uuid.uuid4().hex[:12]}"

        context = Context(
            context_id=context_id,
            tenant_id=tenant_id,
            principal_id=principal_id,
            conversation_history=initial_conversation or [],
            last_activity_at=datetime.now(UTC),
        )

        session = self.session
        try:
            with session.begin_nested():
                session.add(context)
                session.flush()
            if self._owns_session:
                session.commit()
            console.print(f"[green]Created context {context_id} for principal {principal_id}[/green]")
            # Refresh to get any database-generated values
            session.refresh(context)
            # Detach from session
            session.expunge(context)
            return context
        except IntegrityError:
            if self._owns_session:
                session.rollback()
            existing = WorkflowRepository(session, tenant_id).get_context(
                context_id,
                principal_id=principal_id,
            )
            if existing is None:
                raise
            session.expunge(existing)
            return existing
        except Exception as e:
            self.rollback()
            console.print(f"[red]Failed to create context: {e}[/red]")
            raise
        finally:
            self.close()

    def get_context(self, context_id: str, *, tenant_id: str, principal_id: str) -> Context | None:
        """Get a context within its tenant and principal boundary.

        Args:
            context_id: The context ID
            tenant_id: Tenant that must own the context
            principal_id: Principal that must own the context

        Returns:
            The Context object or None if not found
        """
        session = self.session
        try:
            context = WorkflowRepository(session, tenant_id).get_context(
                context_id,
                principal_id=principal_id,
            )
            if context:
                # Detach from session
                session.expunge(context)
            return context
        finally:
            self.close()

    def get_or_create_context(
        self, tenant_id: str, principal_id: str, context_id: str | None = None, is_async: bool = False
    ) -> Context | None:
        """Get existing context or create new one if needed.

        For synchronous operations, returns None.
        For asynchronous operations, returns or creates a context.

        Args:
            tenant_id: The tenant ID
            principal_id: The principal ID
            context_id: Optional existing context ID
            is_async: Whether this is an async operation needing context

        Returns:
            Context object for async operations, None for sync operations
        """
        if not is_async:
            return None

        if context_id:
            return self.get_context(
                context_id,
                tenant_id=tenant_id,
                principal_id=principal_id,
            )
        else:
            return self.create_context(tenant_id, principal_id)

    def update_activity(self, context_id: str) -> None:
        """Update the last activity timestamp for a context.

        Args:
            context_id: The context ID
        """
        try:
            stmt = select(Context).filter_by(context_id=context_id)
            context = self.session.scalars(stmt).first()
            if context:
                context.last_activity_at = datetime.now(UTC)
                self.commit()
        finally:
            self.close()

    def create_workflow_step(
        self,
        context_id: str,
        step_type: str,  # tool_call, approval, notification, etc.
        owner: str,  # principal, publisher, system - who needs to act
        status: str = "pending",  # pending, in_progress, completed, failed, requires_approval
        tool_name: str | None = None,
        request_data: dict[str, Any] | Any | None = None,
        response_data: dict[str, Any] | None = None,
        assigned_to: str | None = None,
        error_message: str | None = None,
        transaction_details: dict[str, Any] | None = None,
        object_mappings: list[dict[str, str]] | None = None,
        initial_comment: str | None = None,
        request_metadata: dict[str, Any] | None = None,
        step_id: str | None = None,
        tenant_id: str | None = None,
    ) -> WorkflowStep:
        """Create a workflow step in the database.

        Args:
            context_id: The context ID
            step_type: Type of step (tool_call, approval, etc.)
            owner: Who needs to act (principal=advertiser, publisher=seller, system=automated)
            status: Step status
            tool_name: Optional tool name if this is a tool call
            request_data: Original request data (dict or Pydantic model — serialized at this boundary)
            response_data: Response/result data
            assigned_to: Specific user/system if assigned
            error_message: Error message if failed
            transaction_details: Actual API calls made
            object_mappings: List of objects this step relates to [{object_type, object_id, action}]
            initial_comment: Optional initial comment to add
            request_metadata: Extra metadata to merge into request_data after serialization

        Returns:
            The created WorkflowStep object
        """
        request_data = _serialize_workflow_request(request_data, request_metadata)
        step_id = step_id or f"step_{uuid.uuid4().hex[:12]}"

        # Initialize comments array with initial comment if provided
        comments = []
        if initial_comment:
            comments.append({"user": "system", "timestamp": datetime.now(UTC).isoformat(), "text": initial_comment})

        step = WorkflowStep(
            step_id=step_id,
            context_id=context_id,
            step_type=step_type,
            owner=owner,
            status=status,
            tool_name=tool_name,
            request_data=request_data if request_data is not None else {},
            response_data=response_data if response_data is not None else {},
            assigned_to=assigned_to,
            error_message=error_message,
            transaction_details=transaction_details if transaction_details is not None else {},
            comments=comments,
            created_at=datetime.now(UTC),
        )

        if status == "completed":
            step.completed_at = datetime.now(UTC)

        session = self.session
        try:
            with session.begin_nested():
                session.add(step)

                _add_object_workflow_mappings(
                    session,
                    object_mappings,
                    step_id=step_id,
                    step_type=step_type,
                )
                session.flush()
            if self._owns_session:
                session.commit()
            session.refresh(step)
            # Detach from session
            session.expunge(step)
            console.print(f"[green]Created workflow step {step_id} for context {context_id}[/green]")
            return step
        except IntegrityError:
            if self._owns_session:
                session.rollback()
            if tenant_id is None:
                raise
            existing = WorkflowRepository(session, tenant_id).get_by_step_and_context(
                step_id,
                context_id,
            )
            if existing is None:
                raise
            session.expunge(existing)
            return existing
        except Exception as e:
            self.rollback()
            console.print(f"[red]Failed to create workflow step: {e}[/red]")
            raise
        finally:
            self.close()

    def update_workflow_step(
        self,
        step_id: str,
        status: str | None = None,
        response_data: dict[str, Any] | Any | None = None,
        error_message: str | None = None,
        transaction_details: dict[str, Any] | None = None,
        add_comment: dict[str, str] | None = None,
        tenant_id: str | None = None,
        notify: bool = True,
    ) -> None:
        """Update a workflow step's status and data.

        Args:
            step_id: The step ID
            status: New status
            response_data: Response/result data. Accepts Pydantic models (serialized
                automatically) or plain dicts. Callers should NOT call .model_dump()
                — pass the model directly.
            error_message: Error message if failed
            transaction_details: Actual API calls made
            add_comment: Optional comment to add {user, comment}
            tenant_id: Tenant scope — joins through Context for isolation.
                If provided, the step must belong to this tenant or no update occurs.
            notify: Emit an asynchronous callback for a genuine post-submission
                status transition. Initial/terminal inline responses set this false.
        """
        # Infrastructure-boundary serialization: _impl functions pass Pydantic
        # models, this method serializes to dict for DB storage.
        # MIGRATION IN PROGRESS: pre-refactor callers still pass pre-serialized
        # dicts. As _impl callers migrate (tracked by the shrinking allowlist in
        # test_architecture_no_model_dump_in_impl), the dict branch becomes dead.
        # When allowlist hits zero, tighten the type to BaseModel-only and remove
        # the isinstance branch.
        if response_data is not None and hasattr(response_data, "model_dump"):
            response_data = response_data.model_dump(mode="json")
        session = self.session
        try:
            stmt = select(WorkflowStep).filter_by(step_id=step_id)
            if tenant_id:
                stmt = stmt.join(DBContext).where(DBContext.tenant_id == tenant_id)
            stmt = stmt.with_for_update()

            step = session.scalars(stmt).first()
            if step:
                old_status = step.status  # Capture old status before changing

                if status:
                    step.status = status
                    if status in ["completed", "failed"] and not step.completed_at:
                        step.completed_at = datetime.now(UTC)

                if response_data is not None:
                    step.response_data = response_data
                if error_message is not None:
                    step.error_message = error_message
                if transaction_details is not None:
                    step.transaction_details = transaction_details

                if add_comment:
                    # Ensure comments is a list
                    if not isinstance(step.comments, list):
                        step.comments = []
                    # Create a new list to trigger SQLAlchemy change detection
                    new_comments = list(step.comments)
                    new_comments.append(
                        {
                            "user": add_comment.get("user", "system"),
                            "timestamp": datetime.now(UTC).isoformat(),
                            "text": add_comment.get("text", add_comment.get("comment", "")),
                        }
                    )
                    step.comments = new_comments

                # DEBUG: Log the condition check values BEFORE commit
                console.print("[magenta]🔍 PRE-COMMIT WEBHOOK DEBUG:[/magenta]")
                console.print("[magenta]   update_workflow_step called with:[/magenta]")
                console.print(f"[magenta]     step_id={step_id}[/magenta]")
                console.print(f"[magenta]     status parameter={status}[/magenta]")
                console.print("[magenta]   Database state BEFORE commit:[/magenta]")
                console.print(f"[magenta]     old_status={old_status}[/magenta]")
                console.print(f"[magenta]     new step.status={step.status}[/magenta]")
                console.print("[magenta]   Condition evaluation:[/magenta]")
                console.print(f"[magenta]     status parameter truthy? {bool(status)}[/magenta]")
                console.print(f"[magenta]     step object exists? {step is not None}[/magenta]")
                console.print(f"[magenta]     Will trigger webhook? {status and step}[/magenta]")

                owns_transaction = self._owns_session
                status_changed = bool(status and status != old_status)
                if status_changed and notify:
                    assert status is not None
                    step_tenant_id = tenant_id or step.context.tenant_id
                    WorkflowRepository(session, step_tenant_id).record_notification_transition(step, status)
                if status_changed and notify and not owns_transaction:
                    assert status is not None
                    defer_until_after_commit(
                        session,
                        partial(_publish_workflow_notifications_after_commit, step_id, status, step_tenant_id),
                    )
                self.commit()
                console.print(f"[green]✅ Updated workflow step {step_id} (committed to database)[/green]")

                # DEBUG: Log the condition check values AFTER commit
                console.print("[yellow]🔍 POST-COMMIT WEBHOOK DEBUG:[/yellow]")
                console.print(f"[yellow]   status={status}[/yellow]")
                console.print(f"[yellow]   old_status={old_status}[/yellow]")
                console.print(f"[yellow]   step exists={step is not None}[/yellow]")
                console.print(f"[yellow]   Webhook trigger condition (status and step): {status and step}[/yellow]")

                # Send push notifications if status changed
                if status_changed and notify and owns_transaction:
                    assert status is not None
                    publish_workflow_notifications(step.step_id, status, step.context.tenant_id)
                elif not status:
                    console.print(f"[yellow]⚠️ WEBHOOK SKIPPED: status={status}, step={step is not None}[/yellow]")
        finally:
            self.close()

    def audit_workflow_step_failure(self, step_id: str, exc: Exception) -> None:
        """Mark a workflow step failed with the spec two-layer envelope as ``response_data``.

        The webhook delivery path at ``_send_push_notifications`` emits
        ``step.response_data`` to push notification subscribers. Without
        structured payload, async subscribers receive ``status=failed`` with
        an empty body. This helper builds the full two-layer envelope
        (``adcp_error`` + ``errors[]``) via ``build_two_layer_error_envelope``
        so async and sync paths see the same wire shape.

        Untyped exceptions are normalized to ``AdCPError`` via
        ``normalize_to_adcp_error``. Wire-code enforcement ensures webhook
        subscribers only see codes in ``WIRE_STANDARD_CODES``.

        Wraps the ``update_workflow_step`` call in ``try/except`` so a DB
        hiccup during audit doesn't replace the original exception that the
        caller is about to re-raise.
        """
        try:
            source = normalize_to_adcp_error(exc)

            # Defensive wire-code enforcement: webhook subscribers must only
            # see codes in ``WIRE_STANDARD_CODES``. If the wire code falls
            # outside the standard set, override with SERVICE_UNAVAILABLE
            # so async subscribers never receive an internal-only code.
            # Structured fields (details/field/suggestion/context) carry
            # forward so buyer agents and webhook subscribers retain
            # machine-actionable correction context across the rewrite.
            wire_code = source.wire_error_code
            if not is_guaranteed_wire_error_code(wire_code):
                source = AdCPError.synthesize(
                    source.message or str(source),
                    error_code="SERVICE_UNAVAILABLE",
                    recovery="terminal",
                    details=source.details,
                    field=source.field,
                    suggestion=source.suggestion,
                    context=source.context,
                )

            response_data = build_two_layer_error_envelope(source)
            error_message = source.message or str(source)

            self.update_workflow_step(
                step_id,
                status="failed",
                error_message=error_message,
                response_data=response_data,
            )
        except Exception:
            # Original exception must survive — log and swallow so the caller's
            # bare ``raise`` propagates the real error to the buyer.
            logger.exception(
                "Failed to audit workflow_step %s after exception — original exception will still re-raise",
                step_id,
            )

    def audit_workflow_step_failure_if_present(self, step: WorkflowStep | None, exc: Exception) -> None:
        """Mark ``step`` as failed if it exists; do not re-raise.

        Standalone variant of :py:meth:`audit_workflow_step_failure_ctx` for callers
        that need to interleave additional observability (e.g., Slack
        notification on the untyped branch in ``_create_media_buy_impl``)
        between the workflow-step audit and the re-raise. The caller
        re-raises explicitly.

        ``audit_workflow_step_failure`` is internally wrapped in
        try/except so a DB hiccup during audit cannot shadow the original
        exception when the caller re-raises.
        """
        if step is not None:
            self.audit_workflow_step_failure(step.step_id, exc)

    @contextmanager
    def audit_workflow_step_failure_ctx(self, get_step: "Callable[[], WorkflowStep | None]") -> Iterator[None]:
        """Context manager: mark the workflow step as failed if any exception escapes the block.

        Single source of truth for "what happens when an _impl owning a
        workflow step fails". Wraps the try-body so the wire-shape envelope
        is threaded into ``response_data`` and async webhook subscribers
        see the same shape the synchronous caller receives.

        Accepts a ``get_step`` callable (typically ``lambda: step``) rather
        than the step directly. Workflow steps are constructed INSIDE the
        guarded block (after early validation), so the callable closure
        resolves the current step value at exception time — not at entry,
        when it may still be ``None``.

        Re-raises the original exception unchanged. Delegates the actual
        audit work to :py:meth:`audit_workflow_step_failure_if_present` so the two
        public APIs (context manager + standalone helper) share the same
        underlying call.
        """
        try:
            yield
        except Exception as exc:
            self.audit_workflow_step_failure_if_present(get_step(), exc)
            raise

    def audit_workflow_step_result(
        self,
        step_id: str,
        response_obj: BaseModel,
        *,
        status: str = "completed",
        error_message: str | None = None,
        add_comment: dict[str, str] | None = None,
        request_obj: BaseModel | None = None,
    ) -> None:
        """Persist a workflow step's result, serializing the response inside ContextManager.

        Owns the ``model_dump`` that the update-media-buy ``_impl`` previously
        open-coded as ``update_workflow_step(..., response_data=<obj>.model_dump(mode="json"))``,
        keeping serialization in the persistence layer (the no-model_dump-in-_impl
        boundary). ``status`` reflects the outcome — ``"completed"`` for a success
        result, ``"failed"`` for an adapter-returned error variant,
        ``"requires_approval"`` for a pending-approval step. ``request_obj``, when
        given, is serialized under the ``request_data`` key so the approval step
        records the originating request alongside the response.
        """
        response_data = response_obj.model_dump(mode="json")
        if request_obj is not None:
            response_data["request_data"] = request_obj.model_dump(mode="json")
        self.update_workflow_step(
            step_id,
            status=status,
            response_data=response_data,
            error_message=error_message,
            add_comment=add_comment,
        )

    def mark_human_needed(
        self,
        context_id: str,
        reason: str,
        clarification_details: str | None = None,
    ) -> None:
        """Mark that human intervention is needed for this context.

        Args:
            context_id: The context ID
            reason: Why human review is needed
            clarification_details: Additional details about what needs review
        """
        self.create_workflow_step(
            context_id=context_id,
            step_type="approval",
            owner="publisher",  # Publisher needs to review
            status="requires_approval",
            request_data={
                "reason": reason,
                "details": clarification_details,
                "protocol": "mcp",  # Default to MCP for internal system actions
            },
            initial_comment=reason,
        )

    def get_pending_steps(
        self,
        owner: str | None = None,
        assigned_to: str | None = None,
        tenant_id: str | None = None,
    ) -> list[WorkflowStep]:
        """Get pending workflow steps from the work queue.

        The owner field tells us who needs to act:
        - 'principal': waiting on the advertiser/buyer
        - 'publisher': waiting on the publisher/seller
        - 'system': automated system processing

        Args:
            owner: Filter by owner (principal, publisher, system)
            assigned_to: Filter by specific assignee
            tenant_id: Tenant scope — joins through Context for isolation.
                If provided, only steps belonging to this tenant are returned.

        Returns:
            List of pending WorkflowStep objects
        """
        session = self.session
        try:
            stmt = select(WorkflowStep).where(WorkflowStep.status.in_(["pending", "requires_approval"]))

            if tenant_id:
                stmt = stmt.join(DBContext).where(DBContext.tenant_id == tenant_id)

            if owner:
                stmt = stmt.where(WorkflowStep.owner == owner)
            if assigned_to:
                stmt = stmt.where(WorkflowStep.assigned_to == assigned_to)

            steps = session.scalars(stmt).all()
            # Detach all from session
            for step in steps:
                session.expunge(step)
            return list(steps)
        finally:
            self.close()

    def get_object_lifecycle(
        self, object_type: str, object_id: str, tenant_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Get all workflow steps for an object's lifecycle.

        Args:
            object_type: Type of object (media_buy, creative, product, etc.)
            object_id: The object's ID
            tenant_id: Tenant scope — joins through Context for isolation.
                If provided, only mappings belonging to this tenant are returned.

        Returns:
            List of workflow steps with their details
        """
        session = self.session
        try:
            # Query object mappings to find all related steps, scoped to tenant via Context join
            stmt = (
                select(ObjectWorkflowMapping)
                .join(WorkflowStep)
                .where(
                    ObjectWorkflowMapping.object_type == object_type,
                    ObjectWorkflowMapping.object_id == object_id,
                )
                .order_by(ObjectWorkflowMapping.created_at)
            )
            if tenant_id:
                stmt = stmt.join(DBContext).where(DBContext.tenant_id == tenant_id)

            mappings = session.scalars(stmt).all()

            lifecycle = []
            for mapping in mappings:
                step = mapping.workflow_step
                if step:
                    lifecycle.append(
                        {
                            "step_id": step.step_id,
                            "action": mapping.action,
                            "step_type": step.step_type,
                            "status": step.status,
                            "owner": step.owner,
                            "assigned_to": step.assigned_to,
                            "created_at": step.created_at.isoformat() if step.created_at else None,
                            "completed_at": step.completed_at.isoformat() if step.completed_at else None,
                            "tool_name": step.tool_name,
                            "error_message": step.error_message,
                            "comments": step.comments,
                        }
                    )

            return lifecycle
        finally:
            self.close()

    def add_message(self, context_id: str, role: str, content: str) -> None:
        """Add a message to the conversation history.

        This is for human-readable messages (clarifications, refinements).
        Tool calls and operational steps go in workflow_steps.

        Args:
            context_id: The context ID
            role: Message role (user, assistant, system)
            content: Message content
        """
        session = self.session
        try:
            stmt = select(Context).filter_by(context_id=context_id)

            context = session.scalars(stmt).first()
            if context:
                if not isinstance(context.conversation_history, list):
                    context.conversation_history = []

                context.conversation_history.append(
                    {"role": role, "content": content, "timestamp": datetime.now(UTC).isoformat()}
                )
                context.last_activity_at = datetime.now(UTC)
                self.commit()
        finally:
            self.close()

    def set_tool_state(self, context_id: str, tool_name: str, state: dict[str, Any]) -> None:
        """Set the current tool state in a context.

        This is for tracking partial progress within a tool for HITL scenarios.

        Args:
            context_id: The context ID
            tool_name: The tool name
            state: The tool state
        """
        # For now, we can store this in the latest workflow step's response_data
        # or create a dedicated notification step
        pass

    def get_context_status(self, context_id: str) -> dict[str, Any]:
        """Get the overall status of a context by checking its workflow steps.

        Status is derived from the workflow steps, not stored in context itself.

        Args:
            context_id: The context ID

        Returns:
            Status information derived from workflow steps
        """
        session = self.session
        try:
            stmt = select(WorkflowStep).filter_by(context_id=context_id)
            steps = session.scalars(stmt).all()

            if not steps:
                return {"status": "no_steps", "summary": "No workflow steps created"}

            # Count steps by status
            status_counts = {"pending": 0, "in_progress": 0, "requires_approval": 0, "completed": 0, "failed": 0}

            for step in steps:
                if step.status in status_counts:
                    status_counts[step.status] += 1

            # Determine overall status
            if status_counts["failed"] > 0:
                overall_status = "has_failures"
            elif status_counts["requires_approval"] > 0:
                overall_status = "awaiting_approval"
            elif status_counts["pending"] > 0 or status_counts["in_progress"] > 0:
                overall_status = "pending_steps"
            else:
                overall_status = "all_completed"

            return {"status": overall_status, "counts": status_counts, "total_steps": len(steps)}
        finally:
            self.close()

    def get_contexts_for_principal(self, tenant_id: str, principal_id: str, limit: int = 10) -> list[Context]:
        """Get recent contexts for a principal.

        Args:
            tenant_id: The tenant ID
            principal_id: The principal ID
            limit: Maximum number of contexts to return

        Returns:
            List of Context objects ordered by last activity
        """
        session = self.session
        try:
            stmt = (
                select(Context)
                .filter_by(tenant_id=tenant_id, principal_id=principal_id)
                .order_by(Context.last_activity_at.desc())
                .limit(limit)
            )
            contexts = session.scalars(stmt).all()

            # Detach all from session
            for context in contexts:
                session.expunge(context)
            return list(contexts)
        finally:
            self.close()

    def link_workflow_to_object(
        self,
        step_id: str,
        object_type: str,
        object_id: str,
        action: str | None = None,
        tenant_id: str | None = None,
    ) -> None:
        """Link a workflow step to an object after the step is created.

        This is useful when you need to associate objects with a workflow step
        after the step has already been created.

        Args:
            step_id: The workflow step ID
            object_type: Type of object (media_buy, creative, product, etc.)
            object_id: The object's ID
            action: Optional action being performed (defaults to step_type)
            tenant_id: Tenant scope — joins through Context for isolation.
                If provided, the step must belong to this tenant or no link is created.
        """
        session = self.session
        try:
            # Get the step to use its step_type as default action
            stmt = select(WorkflowStep).filter_by(step_id=step_id)
            if tenant_id:
                stmt = stmt.join(DBContext).where(DBContext.tenant_id == tenant_id)
            step = session.scalars(stmt).first()

            if not step:
                console.print(f"[yellow]⚠️ Step {step_id} not found, cannot link object[/yellow]")
                return

            obj_mapping = ObjectWorkflowMapping(
                object_type=object_type,
                object_id=object_id,
                step_id=step_id,
                action=action or step.step_type,
                created_at=datetime.now(UTC),
            )
            session.add(obj_mapping)
            self.commit()
            console.print(f"[green]✅ Linked {object_type} {object_id} to workflow step {step_id}[/green]")
        except Exception as e:
            self.rollback()
            console.print(f"[red]Failed to link object to workflow: {e}[/red]")
            raise
        finally:
            self.close()

    def _send_push_notifications(
        self,
        step: WorkflowStep,
        new_status: str,
        session: Any,
        *,
        event_id: str | None = None,
        response_data: dict | None = None,
    ) -> bool:
        """Send push notifications via registered webhooks for workflow step status changes.

        Args:
            step: The workflow step that was updated
            new_status: The new status value
            session: Active database session
        """
        try:
            import requests

            from src.core.database.models import PushNotificationConfig

            # Get object mappings for this step
            stmt = select(ObjectWorkflowMapping).filter_by(step_id=step.step_id)
            mappings = session.scalars(stmt).all()

            if not mappings:
                console.print(f"[yellow]No object mappings found for step {step.step_id}[/yellow]")
                return True
            # A workflow transition is one logical task event even when the step
            # maps to several domain objects.
            mapping = mappings[0]

            # Get context to find tenant_id
            context_stmt = select(Context).filter_by(context_id=step.context_id)
            context = session.scalars(context_stmt).first()
            if not context:
                console.print(f"[yellow]No context found for step {step.step_id}[/yellow]")
                return True

            tenant_id = context.tenant_id
            principal_id = context.principal_id

            cfg_dict = (step.request_data or {}).get("push_notification_config") or {}
            if not cfg_dict.get("url"):
                console.print("[yellow]No push notification config present; skipping webhook[/yellow]")
                return True
            config_id = task_push_config_id(
                tenant_id,
                principal_id,
                step.context_id,
                cfg_dict.get("id"),
            )
            registered = PushNotificationConfigRepository(session, tenant_id).get_active_for_task(
                config_id=config_id,
                principal_id=principal_id,
                media_buy_id=mapping.object_id if mapping.object_type == "media_buy" else None,
                session_id=step.context_id,
            )
            if registered is None:
                console.print("[yellow]Task callback is not durably registered; skipping webhook[/yellow]")
                return True
            # The callback embedded in this durable workflow request is the
            # originating task registration. Never fan a task transition out to
            # unrelated active rows owned by the same principal.
            console.print(
                f"[cyan]📦 Processing mapping: {mapping.object_type} {mapping.object_id} action={mapping.action}[/cyan]"
            )

            url = registered.url
            context_obj = getattr(step, "context", None)
            derived_tenant_id = tenant_id or getattr(context_obj, "tenant_id", None)
            derived_principal_id = principal_id or getattr(context_obj, "principal_id", None)
            push_notification_config = PushNotificationConfig(
                id=registered.id,
                tenant_id=derived_tenant_id,
                principal_id=derived_principal_id,
                media_buy_id=mapping.object_id if mapping.object_type == "media_buy" else None,
                url=url,
                authentication_type=registered.authentication_type,
                authentication_token=registered.authentication_token,
                token=registered.token,
                application_context=registered.application_context,
                is_active=True,
            )

            service = get_protocol_webhook_service()
            safe_url = scrub_control_chars(redact_webhook_url(push_notification_config.url))
            console.print(
                f"[cyan]📤 Sending webhook to {safe_url} for {mapping.object_type} {mapping.object_id}[/cyan]"
            )

            task_type_str = step.tool_name or mapping.action or "unknown"
            try:
                wire_status = "input-required" if new_status == "requires_approval" else new_status
                status_enum = GeneratedTaskStatus(wire_status)
            except ValueError:
                status_enum = GeneratedTaskStatus.unknown
            wire_task_type = validate_webhook_task_type(task_type_str)

            # This path is the AdCP task-argument registration, which always
            # receives mcp-webhook-payload regardless of the inbound transport.
            # Native A2A TaskPushNotificationConfig delivery is handled by the
            # A2A server's task-bound registration path.
            mcp_payload = create_mcp_webhook_payload(
                task_id=step.step_id,
                status=status_enum,
                task_type=wire_task_type,
                result=step.response_data if response_data is None else response_data,
                operation_id=registered.operation_id,
                context_id=step.context_id,
                token=registered.token,
            )
            payload: Task | TaskStatusUpdateEvent | McpWebhookPayload | dict[str, Any]
            payload = mcp_payload.model_dump(mode="json", exclude_none=True)
            stable_event_id = event_id or f"workflow:{step.step_id}:{wire_status}"
            payload["idempotency_key"] = stable_event_id
            application_context = registered.application_context
            if application_context is not None:
                payload["context"] = application_context

            metadata: dict[str, Any] = {
                "task_type": task_type_str,
                "tenant_id": derived_tenant_id,
                "principal_id": derived_principal_id,
                "event_id": f"{stable_event_id}:{registered.id}",
            }

            try:
                notification = service.send_notification(
                    push_notification_config=push_notification_config,
                    payload=payload,
                    metadata=metadata,
                )
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    sent = asyncio.run(notification)
                else:
                    task = loop.create_task(notification)

                    def _log_task_result(t: asyncio.Task) -> None:
                        try:
                            t.result()
                            console.print(f"[green]✅ Webhook sent successfully for {safe_url}[/green]")
                        except Exception as exc:
                            console.print(f"[red]❌ Webhook failed for {safe_url}: {type(exc).__name__}[/red]")

                    pin_task(task, on_done=_log_task_result)
                    # A running event loop cannot be synchronously joined. The
                    # durable outbox owns retries; scheduling the pinned task is
                    # therefore a successful handoff, not proof of delivery.
                    return True
                if sent:
                    console.print(f"[green]✅ Webhook sent successfully for {safe_url}[/green]")
                return sent
            except requests.exceptions.Timeout:
                console.print(f"[red]❌ Webhook timeout for {safe_url}[/red]")
                return False
            except requests.exceptions.RequestException as e:
                console.print(f"[red]❌ Webhook failed for {safe_url}: {type(e).__name__}[/red]")
                return False

        except Exception as e:
            console.print(f"[red]Error sending push notifications: {type(e).__name__}[/red]")
            # Don't fail the workflow update if notifications fail
            import traceback

            traceback.print_exc()
            return False


def get_context_manager() -> ContextManager:
    """Return a request-local manager; SQLAlchemy sessions are never shared."""
    return ContextManager()


def _publish_workflow_notifications_after_commit(step_id: str, status: str, tenant_id: str) -> None:
    """After-commit callback adapter that intentionally discards delivery state."""
    publish_workflow_notifications(step_id, status, tenant_id)


def _publish_workflow_notifications_sync(
    step_id: str,
    status: str,
    tenant_id: str,
    *,
    event_id: str | None = None,
) -> bool:
    """Publish one durable workflow-transition occurrence under a lease."""
    from src.services.a2a_task_lifecycle import publish_workflow_task_transition

    with get_independent_db_session() as session:
        claimed = WorkflowRepository(session, tenant_id).claim_notification_publication(
            step_id,
            status,
            event_id=event_id,
        )
        if claimed is None:
            return False
        claimed_event_id, claim_token, response_data = claimed
        session.commit()
        if claim_token is None:
            return False

    try:
        native_succeeded = publish_workflow_task_transition(
            step_id,
            status,
            tenant_id,
            event_id=claimed_event_id,
            response_data=response_data,
        )
        callback_succeeded = True
        with get_independent_db_session() as session:
            step = WorkflowRepository(session, tenant_id).get_by_step_id(step_id)
            if step is not None:
                callback_succeeded = ContextManager()._send_push_notifications(
                    step,
                    status,
                    session,
                    event_id=claimed_event_id,
                    response_data=response_data,
                )
        succeeded = native_succeeded and callback_succeeded
    except Exception:
        logger.warning(
            "Workflow notification publication failed for %s/%s",
            tenant_id,
            step_id,
            exc_info=True,
        )
        succeeded = False
    with get_independent_db_session() as session:
        repository = WorkflowRepository(session, tenant_id)
        if succeeded:
            finalized = repository.mark_notifications_published(claimed_event_id, claim_token=claim_token)
        else:
            finalized = repository.release_notification_claim(claimed_event_id, claim_token=claim_token)
        session.commit()
    if not finalized:
        logger.warning("Lost workflow notification claim for %s", claimed_event_id)
    return succeeded and finalized


def publish_workflow_notifications(
    step_id: str,
    status: str,
    tenant_id: str,
    *,
    event_id: str | None = None,
) -> bool:
    """Publish an occurrence now, or hand it to a worker from an async loop."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return _publish_workflow_notifications_sync(
            step_id,
            status,
            tenant_id,
            event_id=event_id,
        )

    task = loop.create_task(
        asyncio.to_thread(
            _publish_workflow_notifications_sync,
            step_id,
            status,
            tenant_id,
            event_id=event_id,
        )
    )
    pin_task(
        task,
        on_done=lambda completed: (
            logger.exception(
                "Asynchronous workflow notification publication failed for %s/%s",
                tenant_id,
                step_id,
                exc_info=completed.exception(),
            )
            if not completed.cancelled() and completed.exception() is not None
            else None
        ),
    )
    return True


def publish_pending_workflow_notifications() -> int:
    """Drain terminal workflow notification outbox rows idempotently."""
    with get_independent_db_session() as session:
        pending = WorkflowRepository.list_pending_workflow_notifications(session)
    published = 0
    for item in pending:
        try:
            if publish_workflow_notifications(item.step_id, item.status, item.tenant_id, event_id=item.event_id):
                published += 1
        except Exception:
            logger.warning(
                "Failed to publish pending workflow notification for step_id=%s",
                item.step_id,
                exc_info=True,
            )
    return published
