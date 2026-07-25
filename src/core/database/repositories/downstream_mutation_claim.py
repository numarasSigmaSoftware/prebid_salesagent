"""Tenant-scoped persistence for downstream mutation reconciliation claims."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select, update
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
        result_metadata: dict | None = None,
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
            result_metadata=result_metadata,
            expires_at=datetime.now(UTC) + ttl,
        )
        self._session.add(claim)
        self._session.flush()
        return claim

    def create_from_values(
        self,
        values: tuple[str, str | None, str, str, str, str, str],
        *,
        ttl: timedelta,
    ) -> DownstreamMutationClaim:
        """Create from the service's ordered durable-claim descriptor."""
        principal_id, account_id, idempotency_key, provider, operation_key, request_id, request_hash = values
        return self.create(
            principal_id=principal_id,
            account_id=account_id,
            idempotency_key=idempotency_key,
            provider=provider,
            operation_key=operation_key,
            downstream_request_id=request_id,
            request_hash=request_hash,
            ttl=ttl,
        )

    def transition(
        self,
        claim_id: str,
        *,
        expected_status: str,
        status: str,
        result_metadata: dict | None = None,
    ) -> bool:
        """Atomically transition one tenant-scoped claim from the expected state."""
        values: dict = {"status": status, "updated_at": datetime.now(UTC)}
        if result_metadata is not None:
            values["result_metadata"] = result_metadata
        result = self._session.execute(
            update(DownstreamMutationClaim)
            .where(
                DownstreamMutationClaim.claim_id == claim_id,
                DownstreamMutationClaim.tenant_id == self._tenant_id,
                DownstreamMutationClaim.status == expected_status,
            )
            .values(**values)
        )
        return (getattr(result, "rowcount", 0) or 0) == 1

    def release_planned(
        self,
        *,
        principal_id: str,
        account_id: str | None,
        idempotency_key: str,
    ) -> int:
        """Delete never-invoked claims after the owning request fails."""
        result = self._session.execute(
            delete(DownstreamMutationClaim).where(
                DownstreamMutationClaim.tenant_id == self._tenant_id,
                DownstreamMutationClaim.principal_id == principal_id,
                DownstreamMutationClaim.account_id == account_id,
                DownstreamMutationClaim.idempotency_key == idempotency_key,
                DownstreamMutationClaim.status == "planned",
            )
        )
        return int(getattr(result, "rowcount", 0) or 0)

    def compact_expired(self, *, now: datetime | None = None) -> int:
        """Turn expired terminal/non-ambiguous claims into lightweight tombstones.

        UNKNOWN rows retain their reconciliation metadata for operator action.
        Other expired rows keep only the scope, request hash, and downstream
        request ID needed to reject late key reuse safely.
        """
        current = now or datetime.now(UTC)
        result = self._session.execute(
            update(DownstreamMutationClaim)
            .where(
                DownstreamMutationClaim.tenant_id == self._tenant_id,
                DownstreamMutationClaim.expires_at <= current,
                DownstreamMutationClaim.status != "unknown",
                DownstreamMutationClaim.status != "expired",
            )
            .values(status="expired", result_metadata=None, updated_at=current)
        )
        return int(getattr(result, "rowcount", 0) or 0)
