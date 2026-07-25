"""Write-claim-before-invoke guard for consequential adapter mutations."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, date, datetime
from functools import partial
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.adapters.base import DownstreamMutation, ReconciliationOutcome, ReconciliationResult
from src.core.database.database_session import get_coordination_db_session
from src.core.database.repositories.downstream_mutation_claim import DownstreamMutationClaimRepository
from src.core.database.repositories.idempotency_attempt import DEFAULT_REPLAY_TTL
from src.core.exceptions import AdCPIdempotencyConflictError, AdCPIdempotencyExpiredError, AdCPServiceUnavailableError
from src.core.idempotency_canonical import canonical_payload_hash

if TYPE_CHECKING:
    from src.adapters.base import AdServerAdapter
    from src.core.resolved_identity import ResolvedIdentity

type ClaimExecutionScope = tuple[str, str, str | None, str, str, str, str, str]


def _provider_name(adapter: AdServerAdapter) -> str:
    """Return a stable provider identifier without stringifying test doubles."""
    configured_name = getattr(adapter, "adapter_name", None)
    if isinstance(configured_name, str) and configured_name:
        return configured_name
    class_name = getattr(adapter.__class__, "adapter_name", None)
    if isinstance(class_name, str) and class_name:
        return class_name
    return adapter.__class__.__name__


@contextmanager
def _downstream_operation_fence(scope: str) -> Iterator[Session]:
    """Serialize one provider operation across workers, including reconciliation.

    A PostgreSQL session advisory lock closes the otherwise unavoidable
    pause between the final claim check and a non-transactional provider call.
    If a worker dies, PostgreSQL releases the lock with its connection; the next
    worker then reconciles the exact downstream request ID before deciding
    whether it may invoke. The yielded session also owns every claim transition,
    so execution never needs a third connection while a caller transaction and
    the provider-operation fence are both held.
    """
    with get_coordination_db_session() as session:
        lock_key = func.hashtextextended(scope, 0)
        session.execute(select(func.pg_advisory_lock(lock_key)))
        session.commit()
        try:
            yield session
        finally:
            session.rollback()
            session.execute(select(func.pg_advisory_unlock(lock_key)))
            session.commit()


def _require_media_buy_reconciliation(adapter: AdServerAdapter, *, operation: str, action: str) -> str:
    """Return the provider name or reject an unsupported consequential invocation."""
    provider = _provider_name(adapter)
    supported = getattr(adapter, f"supports_media_buy_{operation}_reconciliation")
    if supported:
        return provider
    from src.core.exceptions import AdCPCapabilityNotSupportedError

    raise AdCPCapabilityNotSupportedError(
        f"{provider} cannot safely reconcile {operation}_media_buy retries",
        details={"adapter": provider, "action": action},
    )


def _require_media_buy_update_reconciliation(adapter: AdServerAdapter, action: str) -> str:
    """Return the provider name or reject before any consequential invocation."""
    return _require_media_buy_reconciliation(adapter, operation="update", action=action)


def _require_media_buy_create_reconciliation(adapter: AdServerAdapter) -> str:
    """Return the provider name or reject an unfenceable create before invocation."""
    return _require_media_buy_reconciliation(adapter, operation="create", action="create_media_buy")


def _request_id(
    tenant_id: str,
    principal_id: str,
    account_id: str | None,
    idempotency_key: str,
    provider: str,
    operation_key: str,
) -> str:
    material = "\0".join((tenant_id, principal_id, account_id or "", idempotency_key, provider, operation_key))
    return hashlib.sha256(material.encode()).hexdigest()


def _ensure_claim_live(claim: Any) -> None:
    """Keep expired provider evidence as a fail-closed tombstone."""
    expires_at = getattr(claim, "expires_at", None)
    if isinstance(expires_at, datetime) and expires_at <= datetime.now(UTC):
        raise AdCPIdempotencyExpiredError(
            "idempotency_key was seen before, but its downstream reconciliation window has expired",
            suggestion=(
                "Perform a natural-key existence check to determine whether the original mutation succeeded, "
                "then accept that result or mint a fresh idempotency_key."
            ),
        )


def _mutation_descriptor(
    *,
    adapter: AdServerAdapter,
    identity: ResolvedIdentity,
    idempotency_key: str,
    operation_key: str,
    media_buy_id: str,
    action: str,
    package_id: str | None,
    budget: int | None,
    implementation_date: Any,
    request_hash: str,
) -> tuple[str, str, str, DownstreamMutation]:
    tenant_id = identity.tenant_id
    principal_id = identity.principal_id
    if not tenant_id or not principal_id:
        raise AdCPServiceUnavailableError("A resolved identity is required for downstream reconciliation")
    provider = _provider_name(adapter)
    downstream_request_id = _request_id(
        tenant_id,
        principal_id,
        identity.account_id,
        idempotency_key,
        provider,
        operation_key,
    )
    canonical_implementation_date = (
        implementation_date.isoformat() if isinstance(implementation_date, date | datetime) else implementation_date
    )
    mutation = DownstreamMutation(
        downstream_request_id=downstream_request_id,
        media_buy_id=media_buy_id,
        action=action,
        package_id=package_id,
        budget=budget,
        implementation_date=implementation_date,
    )
    return provider, downstream_request_id, request_hash, mutation


def plan_reconciled_media_buy_update(
    *,
    uow: Any,
    adapter: AdServerAdapter,
    identity: ResolvedIdentity,
    idempotency_key: str,
    operation_key: str,
    media_buy_id: str,
    action: str,
    package_id: str | None,
    budget: int | None,
    implementation_date: Any = None,
    request_hash: str,
) -> None:
    """Persist a planned provider claim in the reservation transaction."""
    _require_media_buy_update_reconciliation(adapter, action)
    provider, downstream_request_id, request_hash, _ = _mutation_descriptor(
        adapter=adapter,
        identity=identity,
        idempotency_key=idempotency_key,
        operation_key=operation_key,
        media_buy_id=media_buy_id,
        action=action,
        package_id=package_id,
        budget=budget,
        implementation_date=implementation_date,
        request_hash=request_hash,
    )
    repo = uow.downstream_mutation_claims
    assert repo is not None
    canonical_implementation_date = (
        implementation_date.isoformat() if isinstance(implementation_date, date | datetime) else implementation_date
    )
    existing = repo.get(
        principal_id=identity.principal_id,
        account_id=identity.account_id,
        idempotency_key=idempotency_key,
        provider=provider,
        operation_key=operation_key,
    )
    if existing is None:
        repo.create(
            principal_id=identity.principal_id,
            account_id=identity.account_id,
            idempotency_key=idempotency_key,
            provider=provider,
            operation_key=operation_key,
            downstream_request_id=downstream_request_id,
            request_hash=request_hash,
            ttl=DEFAULT_REPLAY_TTL,
            result_metadata=(
                {"implementation_date": canonical_implementation_date}
                if canonical_implementation_date is not None
                else None
            ),
        )
    else:
        _ensure_claim_live(existing)
        if existing.request_hash != request_hash:
            raise AdCPIdempotencyConflictError("idempotency_key was reused with a different downstream mutation")


def plan_reconciled_media_buy_create(
    *,
    uow: Any,
    adapter: AdServerAdapter,
    identity: ResolvedIdentity,
    idempotency_key: str,
    request_hash: str,
) -> None:
    """Persist the provider-create claim beside the attempt reservation."""
    provider = _require_media_buy_create_reconciliation(adapter)
    tenant_id = identity.tenant_id
    principal_id = identity.principal_id
    if not tenant_id or not principal_id:
        raise AdCPServiceUnavailableError("A resolved identity is required for downstream reconciliation")
    operation_key = "create_media_buy"
    downstream_request_id = _request_id(
        tenant_id,
        principal_id,
        identity.account_id,
        idempotency_key,
        provider,
        operation_key,
    )
    repo = uow.downstream_mutation_claims
    assert repo is not None
    existing = repo.get(
        principal_id=principal_id,
        account_id=identity.account_id,
        idempotency_key=idempotency_key,
        provider=provider,
        operation_key=operation_key,
    )
    if existing is None:
        repo.create(
            principal_id=principal_id,
            account_id=identity.account_id,
            idempotency_key=idempotency_key,
            provider=provider,
            operation_key=operation_key,
            downstream_request_id=downstream_request_id,
            request_hash=request_hash,
            ttl=DEFAULT_REPLAY_TTL,
        )
    else:
        _ensure_claim_live(existing)
        if existing.request_hash != request_hash:
            raise AdCPIdempotencyConflictError("idempotency_key was reused with a different downstream mutation")


def release_planned_downstream_claims(
    *,
    uow: Any,
    identity: ResolvedIdentity,
    idempotency_key: str,
) -> None:
    """Remove only never-invoked claims when non-cacheable request work fails."""
    repo = uow.downstream_mutation_claims
    assert repo is not None
    principal_id = identity.principal_id
    if principal_id is None:
        raise AdCPServiceUnavailableError("A principal is required to release downstream mutation claims")
    repo.release_planned(
        principal_id=principal_id,
        account_id=identity.account_id,
        idempotency_key=idempotency_key,
    )


def planned_claim_release_callback(
    *,
    identity: ResolvedIdentity,
    idempotency_key: str,
) -> Callable[..., None]:
    """Bind shared planned-claim cleanup for mutation tool wrappers."""
    return partial(
        release_planned_downstream_claims,
        identity=identity,
        idempotency_key=idempotency_key,
    )


def _transition_claim(
    session: Session,
    repo: DownstreamMutationClaimRepository,
    claim_id: str,
    *,
    expected_status: str,
    status: str,
    result: BaseModel | dict[str, Any] | None = None,
    continuation_metadata: dict[str, Any] | None = None,
) -> None:
    serialized_result = result.model_dump(mode="json") if isinstance(result, BaseModel) else result
    result_metadata = {"response": serialized_result} if result is not None else {}
    if continuation_metadata:
        result_metadata["continuation"] = continuation_metadata
    platform_ids = getattr(result, "_platform_line_item_ids", None)
    if isinstance(platform_ids, dict):
        result_metadata["platform_line_item_ids"] = platform_ids
    result_kwargs = {"result_metadata": result_metadata} if result_metadata else {}
    transitioned = repo.transition(
        claim_id,
        expected_status=expected_status,
        status=status,
        **result_kwargs,
    )
    if not transitioned:
        session.rollback()
        raise AdCPServiceUnavailableError(
            "The downstream mutation claim could not be acquired safely",
            retry_after=1,
        )
    session.commit()


def _recover_prior_result[T: BaseModel](
    *,
    claim_status: str,
    result_metadata: dict[str, Any] | None,
    response_decoder: Callable[[Any], T],
) -> T | None:
    if claim_status == ReconciliationOutcome.APPLIED.value:
        try:
            result = response_decoder((result_metadata or {})["response"])
            return _restore_result_metadata(result, result_metadata)
        except (KeyError, TypeError, ValidationError) as exc:
            raise AdCPServiceUnavailableError(
                "The applied downstream mutation result cannot be reconstructed safely",
                retry_after=1,
            ) from exc
    return None


def _restore_result_metadata[T: BaseModel](result: T, result_metadata: dict[str, Any] | None) -> T:
    """Attach provider and local-continuation metadata to a decoded result."""
    platform_ids = (result_metadata or {}).get("platform_line_item_ids")
    if isinstance(platform_ids, dict):
        object.__setattr__(result, "_platform_line_item_ids", platform_ids)
    continuation = (result_metadata or {}).get("continuation")
    if isinstance(continuation, dict):
        for name, value in continuation.items():
            object.__setattr__(result, f"_{name}", value)
    return result


def _reconcile_ambiguous_claim[T: BaseModel](
    *,
    session: Session,
    repo: DownstreamMutationClaimRepository,
    claim_id: str,
    claim_status: str,
    mutation: DownstreamMutation,
    reconcile: Callable[[DownstreamMutation], ReconciliationResult],
    response_decoder: Callable[[Any], T],
    continuation_metadata: dict[str, Any] | None = None,
) -> tuple[T | None, str]:
    """Resolve an invoked/unknown claim without blindly invoking again."""
    try:
        reconciliation = reconcile(mutation)
    except Exception as exc:
        if claim_status == "invoked":
            _transition_claim(
                session,
                repo,
                claim_id,
                expected_status="invoked",
                status=ReconciliationOutcome.UNKNOWN.value,
            )
        raise AdCPServiceUnavailableError(
            "The provider reconciliation read failed; the mutation will not be invoked again",
            retry_after=30,
        ) from exc
    if reconciliation.outcome is ReconciliationOutcome.APPLIED:
        if reconciliation.response is None:
            raise AdCPServiceUnavailableError(
                "The provider proved the mutation applied but could not reconstruct its response",
                retry_after=30,
            )
        _transition_claim(
            session,
            repo,
            claim_id,
            expected_status=claim_status,
            status=ReconciliationOutcome.APPLIED.value,
            result=reconciliation.response,
            continuation_metadata=continuation_metadata,
        )
        decoded = response_decoder(reconciliation.response)
        metadata = {
            "platform_line_item_ids": getattr(reconciliation.response, "_platform_line_item_ids", None),
            "continuation": continuation_metadata,
        }
        return _restore_result_metadata(decoded, metadata), claim_status
    if reconciliation.outcome is ReconciliationOutcome.NOT_APPLIED:
        _transition_claim(
            session,
            repo,
            claim_id,
            expected_status=claim_status,
            status=ReconciliationOutcome.NOT_APPLIED.value,
        )
        return None, ReconciliationOutcome.NOT_APPLIED.value
    if claim_status == "invoked":
        _transition_claim(
            session,
            repo,
            claim_id,
            expected_status="invoked",
            status=ReconciliationOutcome.UNKNOWN.value,
        )
    raise AdCPServiceUnavailableError(
        "The prior downstream mutation outcome is unknown; the provider operation was not invoked again",
        retry_after=30,
    )


def _execute_claimed_provider_operation[T: BaseModel](
    *,
    tenant_id: str,
    principal_id: str,
    account_id: str | None,
    idempotency_key: str,
    provider: str,
    operation_key: str,
    downstream_request_id: str,
    request_hash: str,
    adapter: AdServerAdapter,
    mutation: DownstreamMutation,
    reconcile: Callable[[DownstreamMutation], ReconciliationResult],
    response_decoder: Callable[[Any], T],
    work: Callable[[], T],
    is_applied: Callable[[T], bool] = lambda _result: True,
    continuation_metadata: dict[str, Any] | None = None,
) -> T:
    """Execute one claimed provider operation or fail closed after ambiguity."""
    fence_scope = downstream_request_id
    claim_scope: ClaimExecutionScope = (
        tenant_id,
        principal_id,
        account_id,
        idempotency_key,
        provider,
        operation_key,
        downstream_request_id,
        request_hash,
    )
    with _downstream_operation_fence(fence_scope) as fence_session:
        return _execute_claimed_provider_operation_under_fence(
            fence_session=fence_session,
            claim_scope=claim_scope,
            adapter=adapter,
            mutation=mutation,
            reconcile=reconcile,
            response_decoder=response_decoder,
            work=work,
            is_applied=is_applied,
            continuation_metadata=continuation_metadata,
        )


def _execute_claimed_provider_operation_under_fence[T: BaseModel](
    *,
    fence_session: Session,
    claim_scope: ClaimExecutionScope,
    adapter: AdServerAdapter,
    mutation: DownstreamMutation,
    reconcile: Callable[[DownstreamMutation], ReconciliationResult],
    response_decoder: Callable[[Any], T],
    work: Callable[[], T],
    is_applied: Callable[[T], bool],
    continuation_metadata: dict[str, Any] | None = None,
) -> T:
    """Run claim state transitions while holding the cross-worker operation fence."""
    (
        tenant_id,
        principal_id,
        account_id,
        idempotency_key,
        provider,
        operation_key,
        downstream_request_id,
        request_hash,
    ) = claim_scope
    repo = DownstreamMutationClaimRepository(fence_session, tenant_id)
    claim = repo.get(
        principal_id=principal_id,
        account_id=account_id,
        idempotency_key=idempotency_key,
        provider=provider,
        operation_key=operation_key,
    )
    if claim is None:
        claim = repo.create_from_values(
            (
                principal_id,
                account_id,
                idempotency_key,
                provider,
                operation_key,
                downstream_request_id,
                request_hash,
            ),
            ttl=DEFAULT_REPLAY_TTL,
        )
    else:
        _ensure_claim_live(claim)
    claim_id = claim.claim_id
    claim_status = claim.status
    stored_hash = claim.request_hash
    result_metadata = claim.result_metadata
    fence_session.commit()

    if stored_hash != request_hash:
        raise AdCPIdempotencyConflictError("idempotency_key was reused with a different downstream mutation")
    stored_implementation_date = (result_metadata or {}).get("implementation_date")
    if stored_implementation_date is not None:
        mutation = replace(
            mutation,
            implementation_date=datetime.fromisoformat(stored_implementation_date),
        )
    prior_result = _recover_prior_result(
        claim_status=claim_status,
        result_metadata=result_metadata,
        response_decoder=response_decoder,
    )
    if prior_result is not None:
        return prior_result

    if claim_status in {"invoked", ReconciliationOutcome.UNKNOWN.value}:
        reconciled_result, claim_status = _reconcile_ambiguous_claim(
            session=fence_session,
            repo=repo,
            claim_id=claim_id,
            claim_status=claim_status,
            mutation=mutation,
            reconcile=reconcile,
            response_decoder=response_decoder,
            continuation_metadata=continuation_metadata,
        )
        if reconciled_result is not None:
            return reconciled_result

    _transition_claim(fence_session, repo, claim_id, expected_status=claim_status, status="invoked")
    adapter.prepare_downstream_mutation(mutation)
    try:
        result = work()
    except Exception:
        _transition_claim(fence_session, repo, claim_id, expected_status="invoked", status="unknown")
        raise
    if is_applied(result):
        _transition_claim(
            fence_session,
            repo,
            claim_id,
            expected_status="invoked",
            status=ReconciliationOutcome.APPLIED.value,
            result=result,
            continuation_metadata=continuation_metadata,
        )
    else:
        _transition_claim(
            fence_session,
            repo,
            claim_id,
            expected_status="invoked",
            status=ReconciliationOutcome.NOT_APPLIED.value,
        )
    return result


def execute_reconciled_creative_build(
    *,
    tenant_id: str,
    principal_id: str,
    account_id: str | None,
    idempotency_key: str,
    agent_url: str,
    creative_id: str,
    request_payload: dict[str, Any],
    work: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    """Execute one creative-agent build once, replaying its durable result.

    Creative agents do not expose a universal reconciliation read. A crash
    after invocation is therefore recorded as ``unknown`` and fails closed;
    a successful response is committed independently before local creative
    persistence so a retry can reuse it without invoking the agent again.
    """
    provider = f"creative-agent:{agent_url}"
    operation_key = f"sync_creatives:{creative_id}:build"
    request_hash = canonical_payload_hash(request_payload)
    downstream_request_id = _request_id(
        tenant_id,
        principal_id,
        account_id,
        idempotency_key,
        provider,
        operation_key,
    )
    with _downstream_operation_fence(downstream_request_id) as session:
        repo = DownstreamMutationClaimRepository(session, tenant_id)
        claim = repo.get(
            principal_id=principal_id,
            account_id=account_id,
            idempotency_key=idempotency_key,
            provider=provider,
            operation_key=operation_key,
        )
        if claim is None:
            claim = repo.create_from_values(
                (
                    principal_id,
                    account_id,
                    idempotency_key,
                    provider,
                    operation_key,
                    downstream_request_id,
                    request_hash,
                ),
                ttl=DEFAULT_REPLAY_TTL,
            )
        else:
            _ensure_claim_live(claim)
        claim_id = claim.claim_id
        status = claim.status
        stored_hash = claim.request_hash
        metadata = claim.result_metadata
        session.commit()

        if stored_hash != request_hash:
            raise AdCPIdempotencyConflictError("idempotency_key was reused with different creative build inputs")
        if status == ReconciliationOutcome.APPLIED.value:
            response = (metadata or {}).get("response")
            if isinstance(response, dict):
                return response
            raise AdCPServiceUnavailableError(
                "The applied creative build result cannot be reconstructed safely",
                retry_after=30,
            )
        if status in {"invoked", ReconciliationOutcome.UNKNOWN.value}:
            raise AdCPServiceUnavailableError(
                "The prior creative build outcome is unknown; the agent was not invoked again",
                retry_after=30,
            )

        _transition_claim(session, repo, claim_id, expected_status=status, status="invoked")
        try:
            result = work()
        except Exception:
            _transition_claim(
                session,
                repo,
                claim_id,
                expected_status="invoked",
                status=ReconciliationOutcome.UNKNOWN.value,
            )
            raise
        _transition_claim(
            session,
            repo,
            claim_id,
            expected_status="invoked",
            status=ReconciliationOutcome.APPLIED.value,
            result=result,
        )
        return result


def execute_reconciled_media_buy_update[T: BaseModel](
    *,
    adapter: AdServerAdapter,
    identity: ResolvedIdentity,
    idempotency_key: str,
    operation_key: str,
    media_buy_id: str,
    action: str,
    package_id: str | None,
    budget: int | None,
    implementation_date: Any = None,
    request_hash: str,
    response_decoder: Callable[[Any], T],
    work: Callable[[], T],
    is_applied: Callable[[T], bool] = lambda _result: True,
) -> T:
    """Execute once, or reconcile a durable claim before any retry."""
    tenant_id = identity.tenant_id
    principal_id = identity.principal_id
    provider = _require_media_buy_update_reconciliation(adapter, action)
    provider, downstream_request_id, request_hash, mutation = _mutation_descriptor(
        adapter=adapter,
        identity=identity,
        idempotency_key=idempotency_key,
        operation_key=operation_key,
        media_buy_id=media_buy_id,
        action=action,
        package_id=package_id,
        budget=budget,
        implementation_date=implementation_date,
        request_hash=request_hash,
    )
    assert tenant_id is not None and principal_id is not None

    return _execute_claimed_provider_operation(
        tenant_id=tenant_id,
        principal_id=principal_id,
        account_id=identity.account_id,
        idempotency_key=idempotency_key,
        provider=provider,
        operation_key=operation_key,
        downstream_request_id=downstream_request_id,
        request_hash=request_hash,
        adapter=adapter,
        mutation=mutation,
        reconcile=adapter.reconcile_media_buy_update,
        response_decoder=response_decoder,
        work=work,
        is_applied=is_applied,
    )


def execute_reconciled_media_buy_create[T: BaseModel](
    *,
    adapter: AdServerAdapter,
    identity: ResolvedIdentity,
    idempotency_key: str,
    request_hash: str,
    response_decoder: Callable[[Any], T],
    work: Callable[[], T],
    is_applied: Callable[[T], bool] = lambda _result: True,
    continuation_metadata: dict[str, Any] | None = None,
) -> T:
    """Fence one provider create; an ambiguous prior invocation never repeats."""
    tenant_id = identity.tenant_id
    principal_id = identity.principal_id
    provider = _require_media_buy_create_reconciliation(adapter)
    if not tenant_id or not principal_id:
        raise AdCPServiceUnavailableError("A resolved identity is required for downstream reconciliation")
    operation_key = "create_media_buy"
    downstream_request_id = _request_id(
        tenant_id,
        principal_id,
        identity.account_id,
        idempotency_key,
        provider,
        operation_key,
    )
    mutation = DownstreamMutation(
        downstream_request_id=downstream_request_id,
        media_buy_id="",
        action="create_media_buy",
        package_id=None,
        budget=None,
    )

    return _execute_claimed_provider_operation(
        tenant_id=tenant_id,
        principal_id=principal_id,
        account_id=identity.account_id,
        idempotency_key=idempotency_key,
        provider=provider,
        operation_key=operation_key,
        downstream_request_id=downstream_request_id,
        request_hash=request_hash,
        adapter=adapter,
        mutation=mutation,
        reconcile=adapter.reconcile_media_buy_create,
        response_decoder=response_decoder,
        work=work,
        is_applied=is_applied,
        continuation_metadata=continuation_metadata,
    )
