"""Write-claim-before-invoke guard for consequential adapter mutations."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ValidationError

from src.adapters.base import DownstreamMutation, ReconciliationOutcome
from src.core.database.repositories.idempotency_attempt import DEFAULT_REPLAY_TTL
from src.core.database.repositories.uow import IdempotencyUoW
from src.core.exceptions import AdCPIdempotencyConflictError, AdCPServiceUnavailableError
from src.core.idempotency_canonical import canonical_payload_hash

if TYPE_CHECKING:
    from src.adapters.base import AdServerAdapter
    from src.core.resolved_identity import ResolvedIdentity


def _provider_name(adapter: AdServerAdapter) -> str:
    """Return a stable provider identifier without stringifying test doubles."""
    configured_name = getattr(adapter, "adapter_name", None)
    if isinstance(configured_name, str) and configured_name:
        return configured_name
    class_name = getattr(adapter.__class__, "adapter_name", None)
    if isinstance(class_name, str) and class_name:
        return class_name
    return adapter.__class__.__name__


def _request_id(
    tenant_id: str,
    principal_id: str,
    idempotency_key: str,
    provider: str,
    operation_key: str,
) -> str:
    material = "\0".join((tenant_id, principal_id, idempotency_key, provider, operation_key))
    return hashlib.sha256(material.encode()).hexdigest()


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
) -> tuple[str, str, str, DownstreamMutation]:
    tenant_id = identity.tenant_id
    principal_id = identity.principal_id
    if not tenant_id or not principal_id:
        raise AdCPServiceUnavailableError("A resolved identity is required for downstream reconciliation")
    provider = _provider_name(adapter)
    downstream_request_id = _request_id(tenant_id, principal_id, idempotency_key, provider, operation_key)
    canonical_implementation_date = (
        implementation_date.isoformat() if isinstance(implementation_date, date | datetime) else implementation_date
    )
    request_hash = canonical_payload_hash(
        {
            "media_buy_id": media_buy_id,
            "action": action,
            "package_id": package_id,
            "budget": budget,
            "implementation_date": canonical_implementation_date,
        }
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
) -> None:
    """Persist a planned provider claim in the reservation transaction."""
    if not adapter.supports_media_buy_update_reconciliation:
        provider = _provider_name(adapter)
        from src.core.exceptions import AdCPCapabilityNotSupportedError

        raise AdCPCapabilityNotSupportedError(
            f"{provider} cannot safely reconcile update_media_buy retries",
            details={"adapter": provider, "action": action},
        )
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
    )
    repo = uow.downstream_mutation_claims
    assert repo is not None
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
        )
    elif existing.request_hash != request_hash:
        raise AdCPIdempotencyConflictError("idempotency_key was reused with a different downstream mutation")


def _transition_claim(
    tenant_id: str,
    claim_id: str,
    *,
    status: str,
    result: BaseModel | None = None,
) -> None:
    with IdempotencyUoW(tenant_id) as uow:
        assert uow.downstream_mutation_claims is not None
        result_kwargs = {"result_metadata": {"response": result.model_dump(mode="json")}} if result is not None else {}
        uow.downstream_mutation_claims.transition(claim_id, status=status, **result_kwargs)


def _recover_prior_result[T: BaseModel](
    *,
    adapter: AdServerAdapter,
    mutation: DownstreamMutation,
    tenant_id: str,
    claim_id: str,
    claim_status: str,
    result_metadata: dict[str, Any] | None,
    is_new: bool,
    response_decoder: Callable[[Any], T],
) -> T | None:
    if claim_status == ReconciliationOutcome.APPLIED.value:
        try:
            return response_decoder((result_metadata or {})["response"])
        except (KeyError, TypeError, ValidationError) as exc:
            raise AdCPServiceUnavailableError(
                "The applied downstream mutation result cannot be reconstructed safely",
                retry_after=1,
            ) from exc
    if is_new or claim_status == "planned":
        return None

    reconciliation = adapter.reconcile_media_buy_update(mutation)
    if reconciliation.outcome is ReconciliationOutcome.UNKNOWN:
        _transition_claim(tenant_id, claim_id, status="unknown")
        raise AdCPServiceUnavailableError(
            "The prior downstream mutation outcome is unknown; retry after provider reconciliation",
            retry_after=30,
        )
    if reconciliation.outcome is ReconciliationOutcome.NOT_APPLIED:
        return None
    if reconciliation.response is None:
        raise AdCPServiceUnavailableError(
            "The provider confirmed the mutation but did not return a reconstructable result",
            retry_after=30,
        )
    result = response_decoder(reconciliation.response)
    _transition_claim(
        tenant_id,
        claim_id,
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
    response_decoder: Callable[[Any], T],
    work: Callable[[], T],
) -> T:
    """Execute once, or reconcile a durable claim before any retry."""
    tenant_id = identity.tenant_id
    principal_id = identity.principal_id
    provider = _provider_name(adapter)
    if not adapter.supports_media_buy_update_reconciliation:
        from src.core.exceptions import AdCPCapabilityNotSupportedError

        raise AdCPCapabilityNotSupportedError(
            f"{provider} cannot safely reconcile update_media_buy retries",
            details={"adapter": provider, "action": action},
        )
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
    )
    assert tenant_id is not None and principal_id is not None

    with IdempotencyUoW(tenant_id) as uow:
        assert uow.downstream_mutation_claims is not None
        repo = uow.downstream_mutation_claims
        claim = repo.get(
            principal_id=principal_id,
            account_id=identity.account_id,
            idempotency_key=idempotency_key,
            provider=provider,
            operation_key=operation_key,
        )
        is_new = claim is None
        if claim is None:
            claim = repo.create(
                principal_id=principal_id,
                account_id=identity.account_id,
                idempotency_key=idempotency_key,
                provider=provider,
                operation_key=operation_key,
                downstream_request_id=downstream_request_id,
                request_hash=request_hash,
                ttl=DEFAULT_REPLAY_TTL,
            )
        claim_id = claim.claim_id
        claim_status = claim.status
        stored_hash = claim.request_hash
        result_metadata = claim.result_metadata

    if stored_hash != request_hash:
        raise AdCPIdempotencyConflictError("idempotency_key was reused with a different downstream mutation")

    prior_result = _recover_prior_result(
        adapter=adapter,
        mutation=mutation,
        tenant_id=tenant_id,
        claim_id=claim_id,
        claim_status=claim_status,
        result_metadata=result_metadata,
        is_new=is_new,
        response_decoder=response_decoder,
    )
    if prior_result is not None:
        return prior_result

    _transition_claim(tenant_id, claim_id, status="invoked")
    try:
        result = work()
    except Exception:
        _transition_claim(tenant_id, claim_id, status="unknown")
        raise

    _transition_claim(
        tenant_id,
        claim_id,
        status=ReconciliationOutcome.APPLIED.value,
        result=result,
    )
    return result
