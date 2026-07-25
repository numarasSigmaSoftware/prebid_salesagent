"""Tenant-scoped durable identities for reporting webhook events."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.core.database.models import WebhookDeliveryLog


@dataclass(frozen=True)
class ClaimedWebhookEvent:
    """Stable wire identity and sequence for one logical reporting event."""

    idempotency_key: str
    sequence_number: int
    status: str
    event_payload: dict | None


class WebhookDeliveryLogRepository:
    """Claim random, retry-stable wire IDs within one tenant."""

    def __init__(self, session: Session, tenant_id: str) -> None:
        self._session = session
        self._tenant_id = tenant_id

    def claim_event(
        self,
        *,
        principal_id: str,
        media_buy_id: str,
        webhook_url: str,
        logical_event_key: str,
        task_type: str,
        notification_type: str,
    ) -> ClaimedWebhookEvent:
        """Return the existing event or durably stage a random new wire key."""
        # A first event has no row for SELECT FOR UPDATE to lock. Serialize the
        # complete lookup → max(sequence) → insert transition for the sequence
        # domain so concurrent same-key retries return the winner and distinct
        # keys cannot allocate the same sequence number.
        lock_material = "\x1f".join(
            (
                self._tenant_id,
                principal_id,
                media_buy_id,
                task_type,
            )
        )
        self._session.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(lock_material, 0))))
        existing = self._session.scalars(
            select(WebhookDeliveryLog)
            .where(
                WebhookDeliveryLog.tenant_id == self._tenant_id,
                WebhookDeliveryLog.principal_id == principal_id,
                WebhookDeliveryLog.media_buy_id == media_buy_id,
                WebhookDeliveryLog.webhook_url == webhook_url,
                WebhookDeliveryLog.logical_event_key == logical_event_key,
            )
            .with_for_update()
        ).first()
        if existing is not None:
            return ClaimedWebhookEvent(
                existing.id,
                existing.sequence_number,
                existing.status,
                existing.event_payload,
            )

        sequence_number = (
            self._session.scalar(
                select(func.coalesce(func.max(WebhookDeliveryLog.sequence_number), 0)).where(
                    WebhookDeliveryLog.tenant_id == self._tenant_id,
                    WebhookDeliveryLog.media_buy_id == media_buy_id,
                    WebhookDeliveryLog.task_type == task_type,
                )
            )
            or 0
        ) + 1
        event = WebhookDeliveryLog(
            id=str(uuid4()),
            tenant_id=self._tenant_id,
            principal_id=principal_id,
            media_buy_id=media_buy_id,
            webhook_url=webhook_url,
            task_type=task_type,
            logical_event_key=logical_event_key,
            sequence_number=sequence_number,
            notification_type=notification_type,
            attempt_count=0,
            status="pending",
        )
        self._session.add(event)
        self._session.flush()
        return ClaimedWebhookEvent(event.id, sequence_number, event.status, None)

    def store_payload_if_absent(self, event_id: str, payload: dict) -> dict:
        """Persist and return the immutable first wire payload for an event."""
        event = self._session.scalars(
            select(WebhookDeliveryLog)
            .where(
                WebhookDeliveryLog.id == event_id,
                WebhookDeliveryLog.tenant_id == self._tenant_id,
            )
            .with_for_update()
        ).one()
        if event.event_payload is None:
            event.event_payload = payload
            self._session.flush()
        return event.event_payload
