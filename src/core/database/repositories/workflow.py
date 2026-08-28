"""Workflow repository — tenant-scoped data access for workflow step tables.

Covers three ORM models:
- WorkflowStep: individual steps/tasks in a workflow
- ObjectWorkflowMapping: maps workflow steps to business objects
- Context (DBContext): conversation tracker for async operations

Core invariant: every query includes tenant_id in the WHERE clause (via Context join).
The tenant_id is set at construction time and injected into all queries automatically.

Write methods add objects to the session but never commit — the caller (or UoW)
handles commit/rollback at the boundary.

"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from src.core.database.models import Context as DBContext
from src.core.database.models import ObjectWorkflowMapping, Principal, WorkflowNotificationEvent, WorkflowStep
from src.core.database.repositories.notification_claim import finalize_notification_claim

_NOTIFIABLE_WORKFLOW_STATUSES = frozenset(
    {"requires_approval", "input-required", "completed", "failed", "rejected", "canceled"}
)


@dataclass(frozen=True)
class StaleCreativeUnblockLease:
    """Scalar scheduler work item for one expired creative-unblock lease."""

    tenant_id: str
    step_id: str
    media_buy_id: str


@dataclass(frozen=True)
class PendingWorkflowNotification:
    """Scalar workflow-event occurrence whose outbox is not acknowledged."""

    tenant_id: str
    step_id: str
    status: str
    event_id: str
    response_data: dict | None


class WorkflowRepository:
    """Tenant-scoped data access for WorkflowStep and ObjectWorkflowMapping.

    All queries filter by tenant_id (via Context join) automatically. Write
    methods modify the session but never commit — the Unit of Work handles that.

    Args:
        session: SQLAlchemy session (caller manages lifecycle).
        tenant_id: Tenant scope for all queries.
    """

    def __init__(self, session: Session, tenant_id: str) -> None:
        self._session = session
        self._tenant_id = tenant_id

    @property
    def tenant_id(self) -> str:
        return self._tenant_id

    # ------------------------------------------------------------------
    # WorkflowStep reads
    # ------------------------------------------------------------------

    def get_context(self, context_id: str, *, principal_id: str | None = None) -> DBContext | None:
        """Get a context by ID within the tenant, optionally scoped to a principal."""
        stmt = select(DBContext).where(
            DBContext.context_id == context_id,
            DBContext.tenant_id == self._tenant_id,
        )
        if principal_id is not None:
            stmt = stmt.where(DBContext.principal_id == principal_id)
        return self._session.scalars(stmt).first()

    def get_by_step_id(self, step_id: str) -> WorkflowStep | None:
        """Get a workflow step by its ID within the tenant."""
        return self._session.scalars(
            select(WorkflowStep)
            .join(DBContext)
            .where(
                WorkflowStep.step_id == step_id,
                DBContext.tenant_id == self._tenant_id,
            )
        ).first()

    def get_by_step_id_for_update(self, step_id: str) -> WorkflowStep | None:
        """Lock one tenant-scoped workflow row for a monotonic transition."""
        return self._session.scalars(
            select(WorkflowStep)
            .join(DBContext)
            .where(
                WorkflowStep.step_id == step_id,
                DBContext.tenant_id == self._tenant_id,
            )
            .execution_options(populate_existing=True)
            .with_for_update()
        ).first()

    def get_by_step_and_context(self, step_id: str, context_id: str) -> WorkflowStep | None:
        """Get a workflow step by ID and context within the tenant."""
        return self._session.scalars(
            select(WorkflowStep)
            .join(DBContext)
            .where(
                WorkflowStep.step_id == step_id,
                WorkflowStep.context_id == context_id,
                DBContext.tenant_id == self._tenant_id,
            )
        ).first()

    def get_by_step_id_or_raise(self, step_id: str) -> WorkflowStep:
        """Get a workflow step by ID or raise ``AdCPTaskNotFoundError``.

        Collapses the task fetch-and-raise guard shared by get_task/complete_task.
        No ``context`` parameter by design: those tools carry the FastMCP transport
        ``Context``, not an AdCP ``ContextObject``, so the task not-found envelope
        stays context-less rather than echoing a transport object into a repository.
        """
        step = self.get_by_step_id(step_id)
        if step is None:
            from src.core.exceptions import AdCPTaskNotFoundError

            raise AdCPTaskNotFoundError(f"Task {step_id} not found")
        return step

    def list_by_tenant(
        self,
        *,
        status: str | None = None,
        object_type: str | None = None,
        object_id: str | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> list[WorkflowStep]:
        """List workflow steps for the tenant, with optional filters.

        Args:
            status: Filter by step status (e.g., "pending", "requires_approval").
            object_type: Filter by associated object type (e.g., "media_buy").
            object_id: Filter by specific object ID (requires object_type).
            offset: Number of steps to skip.
            limit: Maximum number of steps to return.
        """
        stmt = (
            select(WorkflowStep)
            .join(DBContext)
            .where(
                DBContext.tenant_id == self._tenant_id,
            )
        )

        if status:
            stmt = stmt.where(WorkflowStep.status == status)

        if object_type and object_id:
            stmt = stmt.join(ObjectWorkflowMapping).where(
                ObjectWorkflowMapping.object_type == object_type,
                ObjectWorkflowMapping.object_id == object_id,
            )
        elif object_type:
            stmt = stmt.join(ObjectWorkflowMapping).where(
                ObjectWorkflowMapping.object_type == object_type,
            )

        stmt = stmt.order_by(WorkflowStep.created_at.desc()).offset(offset).limit(limit)
        return list(self._session.scalars(stmt).all())

    def count_by_tenant(
        self,
        *,
        status: str | None = None,
        object_type: str | None = None,
        object_id: str | None = None,
    ) -> int:
        """Count workflow steps matching the given filters.

        Uses the same filter logic as list_by_tenant but returns only the count.
        """
        stmt = (
            select(WorkflowStep)
            .join(DBContext)
            .where(
                DBContext.tenant_id == self._tenant_id,
            )
        )

        if status:
            stmt = stmt.where(WorkflowStep.status == status)

        if object_type and object_id:
            stmt = stmt.join(ObjectWorkflowMapping).where(
                ObjectWorkflowMapping.object_type == object_type,
                ObjectWorkflowMapping.object_id == object_id,
            )
        elif object_type:
            stmt = stmt.join(ObjectWorkflowMapping).where(
                ObjectWorkflowMapping.object_type == object_type,
            )

        result = self._session.scalar(select(func.count()).select_from(stmt.subquery()))
        return result or 0

    # ------------------------------------------------------------------
    # ObjectWorkflowMapping reads
    # ------------------------------------------------------------------

    def get_latest_mapping_for_object(self, object_type: str, object_id: str) -> ObjectWorkflowMapping | None:
        """Get the most recent workflow mapping for a specific object within the tenant."""
        return self._session.scalars(
            select(ObjectWorkflowMapping)
            .join(WorkflowStep, ObjectWorkflowMapping.step_id == WorkflowStep.step_id)
            .join(DBContext, WorkflowStep.context_id == DBContext.context_id)
            .where(
                ObjectWorkflowMapping.object_type == object_type,
                ObjectWorkflowMapping.object_id == object_id,
                DBContext.tenant_id == self._tenant_id,
            )
            .order_by(ObjectWorkflowMapping.created_at.desc())
        ).first()

    def get_actionable_step_for_object_for_update(
        self,
        object_type: str,
        object_id: str,
        *,
        tool_name: str,
        statuses: set[str],
        recover_processing_before: datetime | None = None,
    ) -> WorkflowStep | None:
        """Lock the newest matching actionable workflow, ignoring unrelated newer mappings."""
        status_predicate: ColumnElement[bool] = WorkflowStep.status.in_(statuses)
        if recover_processing_before is not None:
            status_predicate = or_(
                status_predicate,
                (
                    (WorkflowStep.status == "processing")
                    & (WorkflowStep.processing_started_at < recover_processing_before)
                ),
            )
        return self._session.scalars(
            select(WorkflowStep)
            .join(ObjectWorkflowMapping, ObjectWorkflowMapping.step_id == WorkflowStep.step_id)
            .join(DBContext, WorkflowStep.context_id == DBContext.context_id)
            .where(
                ObjectWorkflowMapping.object_type == object_type,
                ObjectWorkflowMapping.object_id == object_id,
                WorkflowStep.tool_name == tool_name,
                status_predicate,
                DBContext.tenant_id == self._tenant_id,
            )
            .order_by(ObjectWorkflowMapping.created_at.desc())
            .execution_options(populate_existing=True)
            .with_for_update()
        ).first()

    @staticmethod
    def list_stale_creative_unblock_leases(
        session: Session,
        *,
        before: datetime,
    ) -> list[StaleCreativeUnblockLease]:
        """List expired creative-unblock leases across tenants for the scheduler."""
        rows = session.execute(
            select(
                DBContext.tenant_id,
                WorkflowStep.step_id,
                ObjectWorkflowMapping.object_id,
            )
            .join(WorkflowStep, WorkflowStep.context_id == DBContext.context_id)
            .join(ObjectWorkflowMapping, ObjectWorkflowMapping.step_id == WorkflowStep.step_id)
            .where(
                ObjectWorkflowMapping.object_type == "media_buy",
                WorkflowStep.tool_name == "create_media_buy",
                WorkflowStep.status == "processing",
                WorkflowStep.processing_started_at < before,
            )
            .order_by(DBContext.tenant_id, WorkflowStep.step_id)
        ).all()
        return [StaleCreativeUnblockLease(tenant_id=row[0], step_id=row[1], media_buy_id=row[2]) for row in rows]

    def reclaim_processing_lease(self, step_id: str, *, before: datetime) -> WorkflowStep | None:
        """Renew one expired processing lease under a tenant-scoped row lock."""
        step = self.get_by_step_id_for_update(step_id)
        if (
            step is None
            or step.status != "processing"
            or step.processing_started_at is None
            or step.processing_started_at >= before
        ):
            return None
        step.processing_started_at = datetime.now(UTC)
        self._session.flush()
        return step

    @staticmethod
    def list_pending_workflow_notifications(session: Session) -> list[PendingWorkflowNotification]:
        """List immutable workflow-event occurrences awaiting publication."""
        rows = session.execute(
            select(
                WorkflowNotificationEvent.tenant_id,
                WorkflowNotificationEvent.step_id,
                WorkflowNotificationEvent.status,
                WorkflowNotificationEvent.event_id,
                WorkflowNotificationEvent.response_data,
            )
            .where(WorkflowNotificationEvent.delivered_at.is_(None))
            .order_by(
                WorkflowNotificationEvent.tenant_id,
                WorkflowNotificationEvent.step_id,
                WorkflowNotificationEvent.sequence,
            )
        ).all()
        return [
            PendingWorkflowNotification(
                tenant_id=row[0],
                step_id=row[1],
                status=row[2],
                event_id=row[3],
                response_data=row[4],
            )
            for row in rows
        ]

    def claim_notification_publication(
        self,
        step_id: str,
        status: str,
        *,
        event_id: str | None = None,
        lease_seconds: int = 300,
    ) -> tuple[str, str | None, dict | None] | None:
        """Lease the oldest matching occurrence and return event/token/payload."""
        stmt = (
            select(WorkflowNotificationEvent)
            .where(
                WorkflowNotificationEvent.tenant_id == self._tenant_id,
                WorkflowNotificationEvent.step_id == step_id,
                WorkflowNotificationEvent.delivered_at.is_(None),
            )
            .order_by(WorkflowNotificationEvent.sequence)
            .with_for_update()
        )
        event = self._session.scalars(stmt).first()
        now = datetime.now(UTC)
        claim_active = (
            event is not None
            and event.claimed_at is not None
            and event.claimed_at >= now - timedelta(seconds=lease_seconds)
        )
        if (
            event is not None
            and event.status != "canceled"
            and not claim_active
            and self._has_later_pending_cancellation(event)
        ):
            event.superseded_at = now
            event.delivered_at = now
            event.claimed_at = None
            event.claim_token = None
            self._session.flush()
            return event.event_id, None, None
        if (
            event is None
            or event.status != status
            or (event_id is not None and event.event_id != event_id)
            or claim_active
        ):
            return None
        token = str(uuid4())
        event.claimed_at = now
        event.claim_token = token
        self._session.flush()
        return event.event_id, token, event.response_data

    def mark_notifications_published(self, event_id: str, *, claim_token: str) -> bool:
        """Acknowledge one occurrence only for its current tenant-scoped owner."""
        event = self._get_notification_event_for_update(event_id)
        if not finalize_notification_claim(event, claim_token, published=True):
            return False
        self._session.flush()
        return True

    def release_notification_claim(self, event_id: str, *, claim_token: str) -> bool:
        """Release a failed publication lease for immediate scheduler retry."""
        event = self._get_notification_event_for_update(event_id)
        if not finalize_notification_claim(event, claim_token, published=False):
            return False
        self._session.flush()
        return True

    def record_notification_transition(self, step: WorkflowStep, status: str) -> str | None:
        """Record a directly-mutated transition before its surrounding commit."""
        return self._enqueue_notification_event(step, status)

    def _enqueue_notification_event(self, step: WorkflowStep, status: str) -> str | None:
        if status not in _NOTIFIABLE_WORKFLOW_STATUSES:
            return None
        if status == "canceled":
            superseded_at = datetime.now(UTC)
            older_events = self._session.scalars(
                select(WorkflowNotificationEvent)
                .where(
                    WorkflowNotificationEvent.tenant_id == self._tenant_id,
                    WorkflowNotificationEvent.step_id == step.step_id,
                    WorkflowNotificationEvent.delivered_at.is_(None),
                )
                .with_for_update()
            ).all()
            for event in older_events:
                # An active claim may already be sending on the wire. Preserve
                # it under FIFO so cancellation cannot overtake an in-flight
                # nonterminal callback. Unclaimed events are safe to supersede.
                if event.claimed_at is None:
                    event.superseded_at = superseded_at
                    event.delivered_at = superseded_at
        step.notification_sequence += 1
        event_id = f"workflow:{step.step_id}:{step.notification_sequence}:{status}"
        self._session.add(
            WorkflowNotificationEvent(
                event_id=event_id,
                tenant_id=self._tenant_id,
                context_id=step.context_id,
                step_id=step.step_id,
                sequence=step.notification_sequence,
                status=status,
                response_data=step.response_data,
            )
        )
        self._session.flush()
        return event_id

    def _get_notification_event_for_update(self, event_id: str) -> WorkflowNotificationEvent | None:
        return self._session.scalars(
            select(WorkflowNotificationEvent)
            .where(
                WorkflowNotificationEvent.event_id == event_id,
                WorkflowNotificationEvent.tenant_id == self._tenant_id,
            )
            .with_for_update()
        ).first()

    def _has_later_pending_cancellation(self, event: WorkflowNotificationEvent) -> bool:
        return (
            self._session.scalars(
                select(WorkflowNotificationEvent.event_id)
                .where(
                    WorkflowNotificationEvent.tenant_id == self._tenant_id,
                    WorkflowNotificationEvent.step_id == event.step_id,
                    WorkflowNotificationEvent.sequence > event.sequence,
                    WorkflowNotificationEvent.status == "canceled",
                    WorkflowNotificationEvent.delivered_at.is_(None),
                )
                .limit(1)
            ).first()
            is not None
        )

    def get_step_by_id(self, step_id: str) -> WorkflowStep | None:
        """Alias of :meth:`get_by_step_id` (identical tenant-scoped lookup).

        Retained for the admin/service callers that use this name; delegates so
        the query lives in exactly one place.
        """
        return self.get_by_step_id(step_id)

    def get_mappings_for_step(self, step_id: str) -> list[ObjectWorkflowMapping]:
        """Get all object mappings for a workflow step within the tenant."""
        return list(
            self._session.scalars(
                select(ObjectWorkflowMapping)
                .join(WorkflowStep, ObjectWorkflowMapping.step_id == WorkflowStep.step_id)
                .join(DBContext, WorkflowStep.context_id == DBContext.context_id)
                .where(
                    ObjectWorkflowMapping.step_id == step_id,
                    DBContext.tenant_id == self._tenant_id,
                )
            ).all()
        )

    def get_mappings_for_steps(self, step_ids: list[str]) -> dict[str, list[ObjectWorkflowMapping]]:
        """Get object mappings for multiple workflow steps within the tenant.

        Returns a dict mapping step_id -> list of ObjectWorkflowMapping.
        """
        if not step_ids:
            return {}

        mappings = list(
            self._session.scalars(
                select(ObjectWorkflowMapping)
                .join(WorkflowStep, ObjectWorkflowMapping.step_id == WorkflowStep.step_id)
                .join(DBContext, WorkflowStep.context_id == DBContext.context_id)
                .where(
                    ObjectWorkflowMapping.step_id.in_(step_ids),
                    DBContext.tenant_id == self._tenant_id,
                )
            ).all()
        )

        result: dict[str, list[ObjectWorkflowMapping]] = {sid: [] for sid in step_ids}
        for mapping in mappings:
            result[mapping.step_id].append(mapping)
        return result

    def get_all_steps(self, *, limit: int | None = None) -> list[WorkflowStep]:
        """Get all workflow steps for this tenant, newest first."""
        stmt = (
            select(WorkflowStep)
            .join(DBContext)
            .where(DBContext.tenant_id == self._tenant_id)
            .order_by(WorkflowStep.created_at.desc())
        )
        if limit:
            stmt = stmt.limit(limit)
        return list(self._session.scalars(stmt).all())

    # ------------------------------------------------------------------
    # ObjectWorkflowMapping writes
    # ------------------------------------------------------------------

    def add_mapping(
        self,
        *,
        step_id: str,
        object_type: str,
        object_id: str,
        action: str,
    ) -> ObjectWorkflowMapping:
        """Create and add an ObjectWorkflowMapping to the session.

        Does NOT commit — the caller (or UoW) handles that.
        """
        mapping = ObjectWorkflowMapping(
            step_id=step_id,
            object_type=object_type,
            object_id=object_id,
            action=action,
        )
        self._session.add(mapping)
        return mapping

    # ------------------------------------------------------------------
    # Principal reads (for audit logging)
    # ------------------------------------------------------------------

    def get_principal_name(self, principal_id: str) -> str | None:
        """Look up a principal's display name within the tenant.

        Returns the name string, or None if the principal is not found.
        """
        principal = self._session.scalars(
            select(Principal).filter_by(
                tenant_id=self._tenant_id,
                principal_id=principal_id,
            )
        ).first()
        return principal.name if principal else None

    # ------------------------------------------------------------------
    # WorkflowStep writes
    # ------------------------------------------------------------------

    def update_status(
        self,
        step_id: str,
        *,
        status: str,
        completed_at: datetime | None = None,
        response_data: dict[str, Any] | None = None,
        error_message: str | None = None,
    ) -> WorkflowStep | None:
        """Update the status of a workflow step.

        Returns the updated step, or None if not found.
        Does NOT commit — the caller handles that.
        """
        step = self.get_by_step_id_for_update(step_id)
        if step is None:
            return None

        old_status = step.status
        step.status = status
        if status == "processing":
            step.processing_started_at = datetime.now(UTC)
        if completed_at is not None:
            step.completed_at = completed_at
        if response_data is not None:
            step.response_data = response_data
        if error_message is not None:
            step.error_message = error_message
        elif status == "completed":
            # Clear error message on successful completion
            step.error_message = None

        if status != old_status:
            self._enqueue_notification_event(step, status)
        self._session.flush()
        return step

    def transition_status(
        self,
        step_id: str,
        *,
        status: str,
        allowed_from: set[str],
        expected_processing_started_at: datetime | None = None,
        completed_at: datetime | None = None,
        response_data: dict[str, Any] | None = None,
        error_message: str | None = None,
    ) -> WorkflowStep | None:
        """Lock and update only when the current state is explicitly allowed."""
        step = self.get_by_step_id_for_update(step_id)
        if (
            step is None
            or step.status not in allowed_from
            or (
                expected_processing_started_at is not None
                and step.processing_started_at != expected_processing_started_at
            )
        ):
            return None
        old_status = step.status
        step.status = status
        if status == "processing":
            step.processing_started_at = datetime.now(UTC)
        if completed_at is not None:
            step.completed_at = completed_at
        if response_data is not None:
            step.response_data = response_data
        if error_message is not None:
            step.error_message = error_message
        elif status == "completed":
            step.error_message = None
        if status != old_status:
            self._enqueue_notification_event(step, status)
        self._session.flush()
        return step
