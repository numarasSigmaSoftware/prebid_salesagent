"""Tenant-scoped persistence for downstream mutation reconciliation claims."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from src.core.database.models import DownstreamMutationClaim


class DownstreamMutationClaimRepository:
    """Create and transition durable provider-operation claims."""

    def __init__(self, session: Session, tenant_id: str) -> None:
        self._session = session
        self._tenant_id = tenant_id

    def get(
        self,
        *,
        principal_id: str,
        account_id: str | None,
        idempotency_key: str,
        provider: str,
        operation_key: str,
    ) -> DownstreamMutationClaim | None:
        return self._session.scalars(
            select(DownstreamMutationClaim).where(
                DownstreamMutationClaim.tenant_id == self._tenant_id,
                DownstreamMutationClaim.principal_id == principal_id,
                DownstreamMutationClaim.account_id == account_id,
                DownstreamMutationClaim.idempotency_key == idempotency_key,
                DownstreamMutationClaim.provider == provider,
                DownstreamMutationClaim.operation_key == operation_key,
            )
        ).first()

    def create(
        self,
        *,
        principal_id: str,
        account_id: str | None,
        idempotency_key: str,
        provider: str,
        operation_key: str,
        downstream_request_id: str,
        request_hash: str,
        ttl: timedelta,
    ) -> DownstreamMutationClaim:
        claim = DownstreamMutationClaim(
            tenant_id=self._tenant_id,
            principal_id=principal_id,
            account_id=account_id,
            idempotency_key=idempotency_key,
            provider=provider,
            operation_key=operation_key,
            downstream_request_id=downstream_request_id,
            request_hash=request_hash,
            status="planned",
            expires_at=datetime.now(UTC) + ttl,
        )
        self._session.add(claim)
        self._session.flush()
        return claim

    def transition(self, claim_id: str, *, status: str, result_metadata: dict | None = None) -> bool:
        result = self._session.execute(
            update(DownstreamMutationClaim)
            .where(
                DownstreamMutationClaim.claim_id == claim_id,
                DownstreamMutationClaim.tenant_id == self._tenant_id,
            )
            .values(status=status, result_metadata=result_metadata, updated_at=datetime.now(UTC))
        )
        return (getattr(result, "rowcount", 0) or 0) == 1
