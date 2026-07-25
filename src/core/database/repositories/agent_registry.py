"""Repository for tenant-scoped signals agent configurations."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.core.database.models import SignalsAgent


class SignalsAgentRepository:
    """Tenant-scoped access to configured signals agents."""

    def __init__(self, session: Session, tenant_id: str) -> None:
        self._session = session
        self._tenant_id = tenant_id

    def list_enabled_signals_agents(self) -> list[SignalsAgent]:
        """Return enabled signals agents configured for the tenant."""
        return list(
            self._session.scalars(
                select(SignalsAgent).where(
                    SignalsAgent.tenant_id == self._tenant_id,
                    SignalsAgent.enabled.is_(True),
                )
            ).all()
        )
