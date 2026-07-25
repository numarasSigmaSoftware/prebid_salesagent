"""Tenant- and principal-scoped durable native A2A task storage."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.core.database.models import A2ATaskNotificationEvent, A2ATaskRecord
from src.core.database.repositories.notification_claim import finalize_notification_claim


@dataclass(frozen=True)
class PendingA2ATaskNotification:
    """Scalar identity for one scheduler-visible pending event."""

    tenant_id: str
    event_id: str


@dataclass(frozen=True)
class ClaimedA2ATaskNotification:
    """Session-independent payload held under a publication claim."""

    task_payload: dict
    principal_id: str
    status: str
    claim_token: str | None
    already_delivered: bool = False


class A2ATaskRepository:
    """Persist task protobuf JSON without allowing cross-owner access."""

    def __init__(self, session: Session, tenant_id: str) -> None:
        self._session = session
        self._tenant_id = tenant_id

    def get_owned(self, task_id: str, principal_id: str) -> A2ATaskRecord | None:
        return self._session.scalars(
            select(A2ATaskRecord).where(
                A2ATaskRecord.task_id == task_id,
                A2ATaskRecord.tenant_id == self._tenant_id,
                A2ATaskRecord.principal_id == principal_id,
            )
        ).first()

    def get_owned_for_update(self, task_id: str, principal_id: str) -> A2ATaskRecord | None:
        """Lock one owned task while validating and applying a state transition."""
        return self._session.scalars(
            select(A2ATaskRecord)
            .where(
                A2ATaskRecord.task_id == task_id,
                A2ATaskRecord.tenant_id == self._tenant_id,
                A2ATaskRecord.principal_id == principal_id,
            )
            .with_for_update()
        ).first()

    def get_by_workflow_step(self, workflow_step_id: str) -> A2ATaskRecord | None:
        return self._session.scalars(
            select(A2ATaskRecord).where(
                A2ATaskRecord.tenant_id == self._tenant_id,
                A2ATaskRecord.workflow_step_id == workflow_step_id,
            )
        ).first()

    def get_by_workflow_step_for_update(self, workflow_step_id: str) -> A2ATaskRecord | None:
        """Lock the task correlated with a workflow before transitioning it."""
        return self._session.scalars(
            select(A2ATaskRecord)
            .where(
                A2ATaskRecord.tenant_id == self._tenant_id,
                A2ATaskRecord.workflow_step_id == workflow_step_id,
            )
            .with_for_update()
        ).first()

    def upsert(
        self,
        *,
        task_id: str,
        principal_id: str,
        context_id: str | None,
        workflow_step_id: str | None,
        status: str,
        task_payload: dict,
    ) -> A2ATaskRecord:
        record = self.get_owned(task_id, principal_id)
        if record is None:
            record = A2ATaskRecord(
                task_id=task_id,
                tenant_id=self._tenant_id,
                principal_id=principal_id,
                context_id=context_id,
                workflow_step_id=workflow_step_id,
                status=status,
                task_payload=task_payload,
            )
            self._session.add(record)
        else:
            record.context_id = context_id
            if workflow_step_id is not None:
                record.workflow_step_id = workflow_step_id
            record.status = status
            record.task_payload = task_payload
        self._session.flush()
        return record

    def enqueue_notification(
        self,
        *,
        task_id: str,
        principal_id: str,
        status: str,
        task_payload: dict,
        reuse_last_event: bool = False,
    ) -> str:
        """Persist one status-specific event in the task-state transaction."""
        task = self._session.scalars(
            select(A2ATaskRecord)
            .where(
                A2ATaskRecord.task_id == task_id,
                A2ATaskRecord.tenant_id == self._tenant_id,
                A2ATaskRecord.principal_id == principal_id,
            )
            .with_for_update()
        ).first()
        if task is None:
            raise ValueError(f"Task {task_id!r} must exist before its notification is enqueued")
        if reuse_last_event and task.last_notification_status == status and task.last_notification_event_id is not None:
            return task.last_notification_event_id
        task.notification_sequence += 1
        event_id = f"a2a-task:{task_id}:{task.notification_sequence}:{status}"
        event = self._session.scalars(
            select(A2ATaskNotificationEvent).where(
                A2ATaskNotificationEvent.event_id == event_id,
                A2ATaskNotificationEvent.tenant_id == self._tenant_id,
            )
        ).first()
        if event is None:
            event = A2ATaskNotificationEvent(
                event_id=event_id,
                tenant_id=self._tenant_id,
                task_id=task_id,
                principal_id=principal_id,
                status=status,
                task_payload=task_payload,
            )
            self._session.add(event)
        elif event.delivered_at is None:
            event.task_payload = task_payload
        task.last_notification_status = status
        task.last_notification_event_id = event_id
        self._session.flush()
        return event_id

    def claim_notification_publication(
        self,
        event_id: str,
        *,
        lease_seconds: int = 300,
    ) -> ClaimedA2ATaskNotification | None:
        """Lease one tenant-scoped event and return only detached scalar data."""
        event = self._session.scalars(
            select(A2ATaskNotificationEvent)
            .where(
                A2ATaskNotificationEvent.event_id == event_id,
                A2ATaskNotificationEvent.tenant_id == self._tenant_id,
            )
            .with_for_update()
        ).first()
        if event is None:
            return None
        if event.delivered_at is not None:
            return ClaimedA2ATaskNotification(
                task_payload=dict(event.task_payload),
                principal_id=event.principal_id,
                status=event.status,
                claim_token=None,
                already_delivered=True,
            )
        now = datetime.now(UTC)
        if event.claimed_at is not None and event.claimed_at >= now - timedelta(seconds=lease_seconds):
            return None
        claim_token = str(uuid4())
        event.claimed_at = now
        event.claim_token = claim_token
        self._session.flush()
        return ClaimedA2ATaskNotification(
            task_payload=dict(event.task_payload),
            principal_id=event.principal_id,
            status=event.status,
            claim_token=claim_token,
        )

    def mark_notification_published(self, event_id: str, *, claim_token: str) -> bool:
        """Acknowledge delivery only for the current tenant-scoped lease."""
        event = self._get_notification_for_update(event_id)
        if not finalize_notification_claim(event, claim_token, published=True):
            return False
        self._session.flush()
        return True

    def release_notification_claim(self, event_id: str, *, claim_token: str) -> bool:
        """Release a failed delivery claim without deleting its durable event."""
        event = self._get_notification_for_update(event_id)
        if not finalize_notification_claim(event, claim_token, published=False):
            return False
        self._session.flush()
        return True

    def _get_notification_for_update(self, event_id: str) -> A2ATaskNotificationEvent | None:
        return self._session.scalars(
            select(A2ATaskNotificationEvent)
            .where(
                A2ATaskNotificationEvent.event_id == event_id,
                A2ATaskNotificationEvent.tenant_id == self._tenant_id,
            )
            .with_for_update()
        ).first()

    @staticmethod
    def list_pending_notifications(session: Session) -> list[PendingA2ATaskNotification]:
        """List pending events for the cross-tenant scheduler."""
        rows = session.execute(
            select(A2ATaskNotificationEvent.tenant_id, A2ATaskNotificationEvent.event_id)
            .where(A2ATaskNotificationEvent.delivered_at.is_(None))
            .order_by(A2ATaskNotificationEvent.tenant_id, A2ATaskNotificationEvent.created_at)
        ).all()
        return [PendingA2ATaskNotification(tenant_id=row[0], event_id=row[1]) for row in rows]
