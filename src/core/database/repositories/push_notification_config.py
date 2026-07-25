"""PushNotificationConfig repository — tenant-scoped data access.

Core invariant: every query includes both ``tenant_id`` AND ``principal_id``
in the WHERE clause. PushNotificationConfig rows belong to a single
(tenant, principal) pair; cross-principal lookups are not exposed.

Write methods add objects to the session but never commit — the Unit of Work
(``PushNotificationConfigUoW``) handles commit/rollback at the boundary.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from src.core.database.models import PushNotificationConfig
from src.core.webhook_validator import WebhookURLValidator


def task_push_config_id(
    tenant_id: str,
    principal_id: str,
    session_id: str,
    supplied_id: str | None,
) -> str:
    """Return a globally unique stable ID for one task registration."""
    material = "\0".join((tenant_id, principal_id, session_id, supplied_id or ""))
    return f"pnc_{hashlib.sha256(material.encode()).hexdigest()[:16]}"


@dataclass(frozen=True)
class PushNotificationTarget:
    """Session-independent scalar snapshot used by outbound delivery workers."""

    url: str
    media_buy_id: str
    operation_id: str | None
    token: str | None
    application_context: dict | None
    sequence_number: int
    authentication_type: str | None
    authentication_token: str | None
    webhook_secret: str | None
    auth_blocked_at: datetime | None


class PushNotificationConfigRepository:
    """Tenant + principal scoped access for PushNotificationConfig.

    All queries filter by ``tenant_id`` automatically. Principal scope is
    required on every method — there is no cross-principal lookup.

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
    # Lookups
    # ------------------------------------------------------------------

    def get_by_id(
        self,
        config_id: str,
        principal_id: str,
        *,
        active_only: bool = True,
    ) -> PushNotificationConfig | None:
        """Get a single config by ID within the (tenant, principal) scope.

        Args:
            config_id: The config's primary-key id.
            principal_id: Principal scope filter.
            active_only: If True (default), only return configs where
                ``is_active`` is True. Pass False to include soft-deleted rows
                (e.g. for an upsert that needs to re-activate them).
        """
        stmt = select(PushNotificationConfig).where(
            PushNotificationConfig.tenant_id == self._tenant_id,
            PushNotificationConfig.principal_id == principal_id,
            PushNotificationConfig.id == config_id,
        )
        if active_only:
            stmt = stmt.where(PushNotificationConfig.is_active.is_(True))
        return self._session.scalars(stmt).first()

    def get_active_for_media_buy_url(
        self,
        *,
        principal_id: str,
        media_buy_id: str,
        url: str,
    ) -> PushNotificationConfig | None:
        """Return the callback bound to one media buy and exact opaque URL."""
        return self._session.scalars(
            select(PushNotificationConfig).where(
                PushNotificationConfig.tenant_id == self._tenant_id,
                PushNotificationConfig.principal_id == principal_id,
                PushNotificationConfig.media_buy_id == media_buy_id,
                PushNotificationConfig.url == url,
                PushNotificationConfig.is_active.is_(True),
            )
        ).first()

    def get_active_for_task(
        self,
        *,
        config_id: str,
        principal_id: str,
        media_buy_id: str | None,
        session_id: str,
    ) -> PushNotificationConfig | None:
        """Gate a task callback on its durable originating registration."""
        scope_filter: ColumnElement[bool] = PushNotificationConfig.media_buy_id.is_(None)
        if media_buy_id is not None:
            scope_filter = or_(scope_filter, PushNotificationConfig.media_buy_id == media_buy_id)
        return self._session.scalars(
            select(PushNotificationConfig).where(
                PushNotificationConfig.tenant_id == self._tenant_id,
                PushNotificationConfig.id == config_id,
                PushNotificationConfig.principal_id == principal_id,
                PushNotificationConfig.session_id == session_id,
                PushNotificationConfig.is_active.is_(True),
                scope_filter,
            )
        ).first()

    def list_active_by_principal(self, principal_id: str) -> list[PushNotificationConfig]:
        """Return all active configs for a principal within this tenant."""
        return list(
            self._session.scalars(
                select(PushNotificationConfig).where(
                    PushNotificationConfig.tenant_id == self._tenant_id,
                    PushNotificationConfig.principal_id == principal_id,
                    PushNotificationConfig.is_active.is_(True),
                )
            ).all()
        )

    def list_active_for_task(self, *, principal_id: str, task_id: str) -> list[PushNotificationConfig]:
        """Return native A2A registrations bound to one exact task."""
        return list(
            self._session.scalars(
                select(PushNotificationConfig).where(
                    PushNotificationConfig.tenant_id == self._tenant_id,
                    PushNotificationConfig.principal_id == principal_id,
                    PushNotificationConfig.session_id == task_id,
                    PushNotificationConfig.is_active.is_(True),
                )
            ).all()
        )

    def get_active_a2a_task_config(
        self,
        *,
        config_id: str,
        principal_id: str,
        task_id: str,
    ) -> PushNotificationConfig | None:
        """Return one exact native A2A task registration."""
        return self._session.scalars(
            select(PushNotificationConfig).where(
                PushNotificationConfig.tenant_id == self._tenant_id,
                PushNotificationConfig.principal_id == principal_id,
                PushNotificationConfig.id == config_id,
                PushNotificationConfig.session_id == task_id,
                PushNotificationConfig.is_active.is_(True),
            )
        ).first()

    def soft_delete_a2a_task_config(
        self,
        *,
        config_id: str,
        principal_id: str,
        task_id: str,
    ) -> bool:
        """Disable one exact native A2A task registration."""
        config = self.get_active_a2a_task_config(
            config_id=config_id,
            principal_id=principal_id,
            task_id=task_id,
        )
        if config is None:
            return False
        config.is_active = False
        config.updated_at = datetime.now(UTC)
        self._session.flush()
        return True

    def claim_delivery_targets(
        self,
        principal_id: str,
        media_buy_id: str,
        event_key: str,
    ) -> list[PushNotificationTarget]:
        """Bind one durable event sequence to the media buy's active callbacks.

        Rows are locked until the UoW commits. Repeating the same deterministic
        ``event_key`` reuses its sequence number after a process restart; a new
        event advances the registration-local sequence exactly once.
        """
        configs = list(
            self._session.scalars(
                select(PushNotificationConfig)
                .where(
                    PushNotificationConfig.tenant_id == self._tenant_id,
                    PushNotificationConfig.principal_id == principal_id,
                    PushNotificationConfig.media_buy_id == media_buy_id,
                    PushNotificationConfig.is_active.is_(True),
                )
                .with_for_update()
            ).all()
        )
        targets: list[PushNotificationTarget] = []
        for config in configs:
            if config.last_event_key != event_key:
                config.last_event_key = event_key
                config.last_event_sequence += 1
            targets.append(
                PushNotificationTarget(
                    url=config.url,
                    media_buy_id=media_buy_id,
                    operation_id=config.operation_id,
                    token=config.token,
                    application_context=config.application_context,
                    sequence_number=config.last_event_sequence,
                    authentication_type=config.authentication_type,
                    authentication_token=config.authentication_token,
                    webhook_secret=config.webhook_secret,
                    auth_blocked_at=config.auth_blocked_at,
                )
            )
        self._session.flush()
        return targets

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def upsert(
        self,
        *,
        config_id: str,
        principal_id: str,
        url: str,
        authentication_type: str | None,
        authentication_token: str | None,
        validation_token: str | None,
        session_id: str | None = None,
        media_buy_id: str | None = None,
        operation_id: str | None = None,
        token: str | None = None,
        application_context: dict | None = None,
    ) -> tuple[PushNotificationConfig, bool]:
        """Insert or update a config within the (tenant, principal) scope.

        Returns:
            (config, created): ``created`` is True if a new row was inserted,
            False if an existing row was updated (or reactivated).

        Raises:
            ValueError: If ``url`` fails the *registration* SSRF gate
                (``WebhookURLValidator.validate_webhook_url_registration`` —
                no DNS; optional localhost under ``ADCP_TESTING``). Deliberate
                defense-in-depth: callers also gate before upsert. Outbound
                protocol send uses ``validate_outbound_webhook_url``;
                application delivery (``kind="Application"``) uses the same
                ``reject_unsafe_outbound_webhook_url`` /
                ``validate_outbound_webhook_url`` path.
        """
        is_valid, error_msg = WebhookURLValidator.validate_webhook_url_registration(url)
        if not is_valid:
            raise ValueError(f"Invalid webhook URL: {error_msg}")

        existing = self.get_by_id(config_id, principal_id, active_only=False)
        now = datetime.now(UTC)

        if existing is not None:
            registration_changed = (
                existing.media_buy_id != media_buy_id
                or existing.operation_id != operation_id
                or existing.token != token
                or existing.url != url
            )
            existing.url = url
            existing.authentication_type = authentication_type
            existing.authentication_token = authentication_token
            existing.validation_token = validation_token
            existing.session_id = session_id
            existing.media_buy_id = media_buy_id
            existing.operation_id = operation_id
            existing.token = token
            existing.application_context = application_context
            if registration_changed:
                existing.last_event_key = None
                existing.last_event_sequence = 0
            existing.updated_at = now
            existing.is_active = True
            self._session.flush()
            return existing, False

        config = PushNotificationConfig(
            id=config_id,
            tenant_id=self._tenant_id,
            principal_id=principal_id,
            session_id=session_id,
            media_buy_id=media_buy_id,
            url=url,
            operation_id=operation_id,
            token=token,
            application_context=application_context,
            authentication_type=authentication_type,
            authentication_token=authentication_token,
            validation_token=validation_token,
            is_active=True,
        )
        self._session.add(config)
        self._session.flush()
        return config, True

    def soft_delete(self, config_id: str, principal_id: str) -> bool:
        """Mark a config inactive within the (tenant, principal) scope.

        Finds the row regardless of its current ``is_active`` value and
        sets ``is_active=False``. Idempotent — calling on an already-inactive
        row still returns True.

        Returns:
            True if a matching row was found (and is now inactive),
            False if no row with that ``(tenant, principal, id)`` exists.
        """
        config = self.get_by_id(config_id, principal_id, active_only=False)
        if config is None:
            return False
        config.is_active = False
        config.updated_at = datetime.now(UTC)
        self._session.flush()
        return True
