"""Tenant-scoped data access for principals."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.core.database.models import Principal


class PrincipalRepository:
    """Tenant-scoped read access for Principal records."""

    def __init__(self, session: Session, tenant_id: str) -> None:
        self._session = session
        self._tenant_id = tenant_id

    def get_by_id(self, principal_id: str) -> Principal | None:
        """Return a principal within this tenant, if present."""
        return self._session.scalars(
            select(Principal).where(
                Principal.tenant_id == self._tenant_id,
                Principal.principal_id == principal_id,
            )
        ).first()

    def get_name(self, principal_id: str) -> str | None:
        """Return a principal's display name within this tenant, if present."""
        principal = self.get_by_id(principal_id)
        return principal.name if principal else None
