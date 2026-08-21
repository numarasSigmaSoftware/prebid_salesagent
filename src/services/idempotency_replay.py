"""Backstop-agnostic verbatim-replay primitives for the AdCP idempotency cache.

The read/write half of idempotency that is identical across tools — probe the
verbatim success cache, conflict-check the stored payload hash, best-effort
record a fresh success, evict expired rows. Extracted from ``media_buy_create``
so every mutating tool can share ONE implementation rather than
copy-pasting the cache plumbing (DRY invariant).

What lives HERE (tool-agnostic):
    - ``raise_on_payload_conflict`` — same key + different canonical payload → CONFLICT
    - ``lookup_cached_replay``      — probe + conflict-check + decode, with optional ceiling
    - ``record_replayable_success`` — best-effort cache write (race-loser is a no-op)
    - ``maybe_evict_expired``       — probabilistic housekeeping in its own txn

What STAYS in the tool module (backstop-specific): the dup-booking unique-index
collision handling and the degraded post-race fallback. Those depend on the
tool's own backstop table (``media_buys.idempotency_key``) and its response
shape, so they cannot be generalized — they call INTO the primitives here.

Each caller supplies:
    - ``uow_factory``: a callable ``(tenant_id) -> ContextManager`` yielding an
      object with an ``idempotency_attempts`` :class:`IdempotencyAttemptRepository`
      (for example ``MediaBuyUoW``, ``AccountUoW``, or ``IdempotencyUoW``).
      Passed by the caller (not imported here) so a test that patches the caller's
      UoW symbol still swaps the object these primitives use.
    - ``decode``: a callable ``(response_envelope: dict) -> T | None`` that
      reconstructs the tool's domain replay result (``None`` == unusable → miss).
"""

from __future__ import annotations

import json
import logging
import random
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol, cast

from pydantic import BaseModel, TypeAdapter
from sqlalchemy.exc import IntegrityError

from src.core.application_context_values import serialize_application_context
from src.core.database.repositories.idempotency_attempt import COMPLETED, EXPIRED, IN_FLIGHT, RESERVED
from src.core.exceptions import (
    AdCPIdempotencyConflictError,
    AdCPIdempotencyExpiredError,
    AdCPIdempotencyInFlightError,
    AdCPServiceUnavailableError,
)
from src.core.idempotency_logging import redact_idempotency_key
from src.core.logging_config import scrub_control_chars

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator
    from contextlib import AbstractContextManager
    from datetime import timedelta

    from src.core.database.repositories.idempotency_attempt import IdempotencyAttemptRepository
    from src.core.resolved_identity import ResolvedIdentity

logger = logging.getLogger(__name__)

# Fraction of successful keyed writes that run storage reclamation. Eviction is
# pure housekeeping (read-path TTL filtering guarantees replay correctness), so
# the hot path almost never carries the DELETE; patchable in tests.
DEFAULT_EVICTION_PROBABILITY = 0.01


class _IdempotencyUoWLike(Protocol):
    """The slice of a UoW these primitives touch: an idempotency-attempts repo."""

    idempotency_attempts: IdempotencyAttemptRepository | None


def raise_on_payload_conflict(
    stored_hash: str | None,
    request_hash: str | None,
    *,
    details: dict[str, Any] | None = None,
) -> None:
    """Raise IDEMPOTENCY_CONFLICT when the same key carries a different canonical payload.

    Applied at every lookup point — the probe and any post-race recovery — so a
    conflicting duplicate can never be resolved to someone else's response.
    Production writes always store a hash (``record_success`` requires it); a row
    without one carries no conflict signal, so it never conflicts (legacy tolerance).

    ``details`` is echoed into the error envelope (e.g. the current resource
    version for an ETag-style conflict); ``None`` leaves the envelope's details
    unset, preserving the original create-path behaviour.
    """
    if stored_hash is not None and stored_hash != request_hash:
        raise AdCPIdempotencyConflictError(
            "idempotency_key was reused with a different request payload",
            details=details,
        )


def raise_on_tool_conflict(stored_tool_name: str | None, tool_name: str) -> None:
    """Reject cross-tool key reuse even when both request payloads are empty."""
    if stored_tool_name is not None and stored_tool_name != tool_name:
        raise AdCPIdempotencyConflictError(
            "idempotency_key was reused for a different tool",
            details={"original_tool": stored_tool_name, "requested_tool": tool_name},
        )


def lookup_cached_replay[T](
    uow_factory: Callable[[str], AbstractContextManager[_IdempotencyUoWLike]],
    tenant_id: str,
    *,
    principal_id: str | None,
    account_id: str | None,
    tool_name: str,
    idempotency_key: str,
    request_hash: str | None,
    decode: Callable[[dict[str, Any]], T | None],
    enforce_ceiling: bool = False,
    conflict_details: dict[str, Any] | None = None,
) -> T | None:
    """Probe the verbatim success cache: conflict-check the stored hash, then decode.

    Shared read path for the front probe and any post-race recovery. The same
    key carrying a different canonical payload raises ``IDEMPOTENCY_CONFLICT``
    (checked BEFORE any decode); a hit whose stored envelope no longer validates
    returns ``None`` exactly like a miss, so callers fall through to fresh
    execution (probe) or the degraded fallback (post-race).

    ``enforce_ceiling=True`` (the front probe) additionally rate-limits a MISS:
    a fresh key would insert a new cache row, and the per-scope insert rate and
    row count are bounded — see :mod:`src.services.idempotency_policy`. The
    post-race path never enforces it (the loser inserts nothing).
    """
    with uow_factory(tenant_id) as uow:
        assert uow.idempotency_attempts is not None
        cached = uow.idempotency_attempts.find_by_key(
            principal_id=principal_id,
            account_id=account_id,
            idempotency_key=idempotency_key,
        )
        if cached is None:
            previous = uow.idempotency_attempts.find_including_expired(
                principal_id=principal_id,
                account_id=account_id,
                idempotency_key=idempotency_key,
            )
            previous_expires_at = getattr(previous, "expires_at", None)
            if isinstance(previous_expires_at, datetime) and previous_expires_at <= datetime.now(UTC):
                raise AdCPIdempotencyExpiredError(
                    "idempotency_key was seen before, but its replay window has expired",
                    suggestion=(
                        "Perform a natural-key existence check to determine whether the original mutation succeeded, "
                        "then accept that result or mint a fresh idempotency_key."
                    ),
                )
            if enforce_ceiling:
                from src.services.idempotency_policy import enforce_insert_ceiling

                enforce_insert_ceiling(
                    uow.idempotency_attempts,
                    principal_id=principal_id,
                    account_id=account_id,
                )
            return None
        raise_on_tool_conflict(cached.tool_name, tool_name)
        raise_on_payload_conflict(cached.payload_hash, request_hash, details=conflict_details)
        # An in-flight reservation carries a NULL envelope and is not replayable —
        # treat it as a miss (the caller falls through to fresh execution or the
        # degraded fallback). Completed rows always carry an envelope.
        if cached.response_envelope is None:
            return None
        return decode(cached.response_envelope)


def record_replayable_success(
    uow_factory: Callable[[str], AbstractContextManager[_IdempotencyUoWLike]],
    tenant_id: str,
    *,
    principal_id: str,
    account_id: str | None,
    tool_name: str,
    idempotency_key: str,
    response_model: BaseModel,
    protocol_status: str,
    payload_hash: str,
    eviction_probability: float = DEFAULT_EVICTION_PROBABILITY,
) -> None:
    """Best-effort store of a fresh success into the verbatim cache, then evict.

    The write is best-effort — a concurrent same-key winner raises
    ``IntegrityError`` on the unique index and is harmless (the buyer's retry
    replays the winner). Any other failure is logged and swallowed: the buyer's
    response is already computed; a cache-write miss only costs a re-execution on
    the (rare) retry. Callers guard the preconditions (genuine success carrying a
    key) BEFORE calling — this primitive assumes the write is wanted.

    Eviction runs AFTER the write commits, in its own transaction, so a
    tenant-wide DELETE deadlock can never roll back the just-cached success.
    """
    try:
        with uow_factory(tenant_id) as uow:
            assert uow.idempotency_attempts is not None
            uow.idempotency_attempts.record_success(
                principal_id=principal_id,
                account_id=account_id,
                tool_name=tool_name,
                idempotency_key=idempotency_key,
                response_model=response_model,
                protocol_status=protocol_status,
                payload_hash=payload_hash,
            )
    except IntegrityError:
        logger.info(
            "Idempotency cache race for key %s (tenant %s, principal %s) — winner already stored",
            scrub_control_chars(redact_idempotency_key(idempotency_key)),
            tenant_id,
            principal_id,
        )
    except Exception:
        logger.warning(
            "Best-effort idempotency cache write failed for key %s (tenant %s, principal %s)",
            scrub_control_chars(redact_idempotency_key(idempotency_key)),
            tenant_id,
            principal_id,
            exc_info=True,
        )
    maybe_evict_expired(uow_factory, tenant_id, probability=eviction_probability)


def maybe_evict_expired(
    uow_factory: Callable[[str], AbstractContextManager[_IdempotencyUoWLike]],
    tenant_id: str,
    *,
    probability: float = DEFAULT_EVICTION_PROBABILITY,
) -> None:
    """Probabilistically reclaim expired cache rows in a separate short transaction.

    Runs OUTSIDE the cache-write transaction so a tenant-wide DELETE deadlock
    can never roll back a just-cached success, and only on ``probability`` of
    keyed successes so writes almost never pay for housekeeping. Best-effort by
    design — a failure here affects nothing the buyer sees.
    """
    if random.random() >= probability:
        return
    try:
        with uow_factory(tenant_id) as uow:
            assert uow.idempotency_attempts is not None
            uow.idempotency_attempts.expire_old()
            claim_repo = getattr(uow, "downstream_mutation_claims", None)
            if claim_repo is not None:
                claim_repo.compact_expired()
    except Exception:
        logger.warning("Best-effort idempotency cache eviction failed for tenant %s", tenant_id, exc_info=True)


# ---------------------------------------------------------------------------
# Durable two-phase reservation façade (reserve -> work -> complete/release)
# ---------------------------------------------------------------------------
# The reservation model provides first-insert-wins durability for every keyed
# protocol operation. Legacy probe/record helpers above remain only for direct
# in-process compatibility callers; production transports reserve first.


@dataclass(frozen=True)
class ReservationResult[T]:
    """Outcome of :func:`reserve_idempotent`.

    Exactly one field is set:

    - ``replay`` — the decoded verbatim success of a prior completed attempt;
      the caller returns it and does NO work.
    - ``attempt_id`` — this caller won the reservation; it must do the work and
      then :func:`complete_idempotent` (success) or :func:`release_idempotent`
      (failure) the row.

    A CONFLICT or IN_FLIGHT outcome does not reach here — it is raised as a typed
    ``AdCPIdempotencyConflictError`` / ``AdCPIdempotencyInFlightError``.
    """

    replay: T | None = None
    attempt_id: str | None = None


def _decode_reserved_replay[T](
    response_envelope: dict[str, Any] | None,
    decode: Callable[[dict[str, Any]], T | None],
) -> T:
    """Decode a completed reservation or fail closed.

    A completed row already owns the unique key. Treating schema drift or a
    cross-tool response shape as a cache miss would execute work without a new
    reservation and could repeat a mutation. Reservation-backed tools therefore
    return a transient failure until the replay window expires or the stored
    response becomes decodable.
    """
    decoded = decode(response_envelope) if response_envelope is not None else None
    if decoded is None:
        raise AdCPServiceUnavailableError(
            "The prior idempotent response could not be replayed safely",
            retry_after=1,
        )
    return decoded


def reserve_idempotent[T](
    uow_factory: Callable[[str], AbstractContextManager[_IdempotencyUoWLike]],
    tenant_id: str,
    *,
    principal_id: str | None,
    account_id: str | None,
    tool_name: str,
    idempotency_key: str,
    request_hash: str,
    lease: timedelta,
    decode: Callable[[dict[str, Any]], T | None],
    enforce_ceiling: bool = False,
    conflict_details: dict[str, Any] | None = None,
    operation_class: str = "write",
    on_reserved: Callable[[Any], None] | None = None,
) -> ReservationResult[T]:
    """Reserve the idempotency key in its OWN committed transaction, or replay/raise.

    Runs :meth:`IdempotencyAttemptRepository.reserve` and commits the surrounding
    UoW so a RESERVED in_flight row is DURABLE before the caller performs any side
    effect — the durability the best-effort ``record_replayable_success`` path
    cannot give. Outcomes:

    - RESERVED → returns ``ReservationResult(attempt_id=...)`` (row committed).
    - COMPLETED, same payload hash → decodes and returns
      ``ReservationResult(replay=...)``.
    - COMPLETED, different payload hash → raises ``AdCPIdempotencyConflictError``.
    - IN_FLIGHT (a live same-key reservation) → raises
      ``AdCPIdempotencyInFlightError`` with the lease-derived ``retry_after``.

    ``enforce_ceiling=True`` rate-limits a fresh key BEFORE it inserts a row: a
    completed-row probe short-circuits replays/conflicts (never rate-limited —
    they insert nothing), and only a genuine miss runs
    :func:`enforce_insert_ceiling`. Enforcing before the INSERT is required: a
    post-insert count would include the reservation's own row and wrongly reject
    the Nth request. The unique-index INSERT remains the concurrency arbiter — the
    probe is a fast path + ceiling gate, not a substitute (two racers may both
    miss the probe, both pass the ceiling, then race the INSERT: one RESERVED, the
    other IN_FLIGHT).
    """
    with uow_factory(tenant_id) as uow:
        assert uow.idempotency_attempts is not None
        repo = uow.idempotency_attempts
        # Fast-path replay/conflict probe (completed rows only). A hit inserts
        # nothing, so it is never rate-limited.
        cached = repo.find_by_key(
            principal_id=principal_id,
            account_id=account_id,
            idempotency_key=idempotency_key,
        )
        if cached is not None:
            raise_on_tool_conflict(cached.tool_name, tool_name)
            raise_on_payload_conflict(cached.payload_hash, request_hash, details=conflict_details)
            return ReservationResult(
                replay=_decode_reserved_replay(cached.response_envelope, decode),
                attempt_id=None,
            )
        # Genuine miss → a fresh INSERT is coming. Enforce the ceiling now, BEFORE
        # the reserve() INSERT so the count excludes this request's own row.
        if enforce_ceiling:
            from src.services.idempotency_policy import enforce_insert_ceiling

            enforce_insert_ceiling(
                repo,
                principal_id=principal_id,
                account_id=account_id,
                operation_class=operation_class,
            )
        outcome = repo.reserve(
            principal_id=principal_id,
            account_id=account_id,
            tool_name=tool_name,
            idempotency_key=idempotency_key,
            payload_hash=request_hash,
            lease=lease,
            operation_class=operation_class,
        )
        if outcome.kind == EXPIRED:
            raise AdCPIdempotencyExpiredError(
                "idempotency_key was seen before, but its replay window has expired",
                suggestion=(
                    "Perform a natural-key existence check to determine whether the original mutation succeeded, "
                    "then accept that result or mint a fresh idempotency_key."
                ),
            )
        # Check the canonical payload before classifying a live or completed
        # collision. A different-payload retry is CONFLICT even while the first
        # request is still in flight.
        raise_on_tool_conflict(outcome.stored_tool_name, tool_name)
        raise_on_payload_conflict(outcome.stored_hash, request_hash, details=conflict_details)
        if outcome.kind == IN_FLIGHT:
            raise AdCPIdempotencyInFlightError(
                "A prior request with the same idempotency_key is still being processed",
                retry_after=outcome.retry_after or 1,
            )
        if outcome.kind == COMPLETED:
            return ReservationResult(
                replay=_decode_reserved_replay(outcome.response_envelope, decode),
                attempt_id=None,
            )
        # RESERVED (fresh INSERT or stolen expired lease). Fall through so the UoW
        # commits the in_flight row (and any steal UPDATE) on clean exit — the
        # reservation is durable only after this commit.
        assert outcome.kind == RESERVED
        reserved_attempt_id = outcome.attempt_id
        if on_reserved is not None:
            on_reserved(uow)
    return ReservationResult(replay=None, attempt_id=reserved_attempt_id)


def complete_idempotent(
    uow: _IdempotencyUoWLike,
    *,
    attempt_id: str,
    response_model: BaseModel | dict[str, Any],
    protocol_status: str,
) -> None:
    """Flip the reserved row to completed on the SHARED work UoW (strict, atomic).

    Called INSIDE the caller's work transaction (e.g. ``SyncAccountsUoW``) so the
    cached success and the guarded side effect commit as one unit — unlike the
    best-effort ``record_replayable_success``, a completion failure rolls the
    whole atomic unit back. The model is serialized inside the repository
    (no-model-dump-in-impl).
    """
    assert uow.idempotency_attempts is not None
    completed = uow.idempotency_attempts.complete(
        attempt_id,
        response_model=response_model,
        protocol_status=protocol_status,
    )
    if not completed:
        raise AdCPServiceUnavailableError(
            "The idempotency reservation expired before completion; the result was not cached safely",
            retry_after=1,
        )


def release_idempotent(
    uow_factory: Callable[[str], AbstractContextManager[_IdempotencyUoWLike]],
    tenant_id: str,
    *,
    attempt_id: str,
    on_release: Callable[..., None] | None = None,
) -> None:
    """Delete the reserved in_flight row in its OWN transaction (handler failed).

    Runs after the work transaction has rolled back, so it needs a fresh session.
    Best-effort: a release failure only leaves an in_flight row that expires and
    becomes stealable after its lease — it never caches an error. Errors are
    never cached, so a retry after release re-executes.
    """
    try:
        with uow_factory(tenant_id) as uow:
            assert uow.idempotency_attempts is not None
            released = uow.idempotency_attempts.release(attempt_id)
            if released and on_release is not None:
                on_release(uow=uow)
    except Exception:
        logger.warning(
            "Best-effort idempotency reservation release failed for attempt %s (tenant %s) — "
            "the in_flight row will expire and become stealable",
            attempt_id,
            tenant_id,
            exc_info=True,
        )


@contextmanager
def release_reservation_on_error(
    uow_factory: Callable[[str], AbstractContextManager[_IdempotencyUoWLike]],
    tenant_id: str,
    attempt_id: str | None,
    *,
    on_release: Callable[..., None] | None = None,
) -> Iterator[None]:
    """Release a won reservation only when the guarded work raises.

    Compose this immediately outside the domain UoW in the same ``with``
    statement. Python exits context managers right-to-left, so the domain UoW
    rolls back first and the reservation is then deleted in a fresh transaction.
    Successful work leaves the completed row intact.
    """
    try:
        yield
    except Exception:
        if attempt_id is not None:
            release_idempotent(
                uow_factory,
                tenant_id,
                attempt_id=attempt_id,
                on_release=on_release,
            )
        raise


def _read_scope(identity: ResolvedIdentity | None) -> tuple[str, str, str | None] | None:
    """Resolve the durable read scope, or None when the caller cannot be safely scoped.

    AdCP 3.1.1 (security.mdx, "Idempotency"): cache entries are scoped per
    ``(authenticated_agent, account_id, idempotency_key)`` — there is no
    anonymous slot in that tuple. An unauthenticated caller has no
    ``authenticated_agent``, so there is no scope the durable cache can safely
    key on for them.

    Persisting such a request under ``(tenant, NULL, NULL)`` does not weaken the
    scope, it FUSES it: the lookup index is NULLS NOT DISTINCT, so every
    anonymous caller of a tenant would share one row-space and one insert-rate
    bucket. Keys are client-chosen, so one caller could induce a collision and
    receive another caller's cached envelope, and one caller's read burst would
    rate-limit every other anonymous reader tenant-wide. [prior defect]

    Refusing the read outright is not spec-consistent either:
    ``authentication.mdx`` designates ``get_products``, ``get_adcp_capabilities``
    and ``list_creative_formats`` public (no auth required), and
    ``security.mdx`` states buyer SDKs send ``idempotency_key`` uniformly across
    every tool call and that a seller accepting a supplied key on a pure-read
    task MUST apply the replay contract. A blanket refusal turns every
    SDK-originated anonymous discovery call into ``AUTH_REQUIRED``, which is not
    "applying" the contract, it is breaking the public operation.

    The resolution: for an unauthenticated caller, this returns ``None`` so the
    reservation/replay/rate-limit machinery is skipped entirely — no row is
    ever written, so no fusion is possible either. The caller falls through to
    running the read directly, exactly as it would with no ``idempotency_key``
    supplied at all; the underlying tool's own authorization (if it has one)
    still applies inside that call, unaffected by this function, which makes a
    caching decision, not an authorization one. This mirrors the existing
    write-path precedent (media_buy_create.py: skip the cache, not the
    operation, when ``principal_id`` is ``None``) rather than adding a second
    mechanism for the same shape of decision.
    """
    tenant_id = identity.tenant_id if identity is not None else None
    if tenant_id is None and identity is not None and isinstance(identity.tenant, dict):
        tenant_id = identity.tenant.get("tenant_id")
    if not tenant_id:
        raise AdCPServiceUnavailableError(
            "A tenant scope is required to honor the supplied idempotency_key",
            retry_after=1,
        )
    principal_id = identity.principal_id if identity else None
    if not principal_id:
        return None
    return tenant_id, principal_id, identity.account_id if identity else None


def _response_with_current_context(response: Any, context: Any) -> Any:
    """Replace cached correlation context with the current retry's context."""
    if isinstance(response, dict):
        updated = dict(response)
        updated.pop("context", None)
        serialized = serialize_application_context(context)
        if serialized is not None:
            updated["context"] = serialized
        return updated
    if not isinstance(response, BaseModel):
        return response

    if "context" in type(response).model_fields:
        return response.model_copy(update={"context": context})
    nested = getattr(response, "response", None)
    if isinstance(nested, BaseModel) and "context" in type(nested).model_fields:
        return response.model_copy(update={"response": nested.model_copy(update={"context": context})})
    return response


def mark_idempotent_replay[T](response: T, *, context: Any) -> T:
    """Overlay retry-local context and the replay marker on a cached response.

    ``context`` is deliberately excluded from canonical idempotency hashing:
    retries may carry a new correlation value, and the normative context echo
    contract requires that current value rather than the value stored with the
    original success. FastMCP ``ToolResult`` mirrors structured content in a
    JSON text block, so both representations are updated together.
    """
    response = _response_with_current_context(response, context)
    if isinstance(response, dict):
        return cast(T, {**response, "replayed": True})
    if not isinstance(response, BaseModel):
        return response

    structured = getattr(response, "structured_content", None)
    if isinstance(structured, dict):
        replayed_structured = cast(dict[str, Any], _response_with_current_context(structured, context))
        replayed_structured["replayed"] = True
        content = []
        for block in getattr(response, "content", []):
            text = getattr(block, "text", None)
            if isinstance(text, str):
                try:
                    decoded_text = json.loads(text)
                except (TypeError, ValueError):
                    decoded_text = None
                if decoded_text == structured:
                    block = block.model_copy(
                        update={
                            "text": json.dumps(
                                replayed_structured,
                                ensure_ascii=False,
                                separators=(",", ":"),
                            )
                        }
                    )
            content.append(block)
        return cast(
            T,
            response.model_copy(
                update={
                    "content": content,
                    "structured_content": replayed_structured,
                }
            ),
        )
    if "replayed" in type(response).model_fields:
        return cast(T, response.model_copy(update={"replayed": True}))
    return response


def _decode_read_response(
    envelope: dict[str, Any],
    response_type: type[Any] | None,
    context: Any = None,
) -> Any:
    """Reconstruct and mark a cached read response."""
    try:
        payload = envelope["response"]
        if response_type is None:
            if not isinstance(payload, dict):
                return None
            return mark_idempotent_replay(payload, context=context)
        model_fields = getattr(response_type, "model_fields", {})
        if {"content", "structured_content"}.issubset(model_fields) and isinstance(payload, dict):
            # FastMCP ToolResult's custom __init__ converts a serialized list of
            # content blocks into one JSON text block. Validate the blocks
            # directly, then bypass that lossy constructor for byte-stable replay.
            content = TypeAdapter(model_fields["content"].annotation).validate_python(payload.get("content", []))
            response = response_type.model_construct(
                content=content,
                structured_content=payload.get("structured_content"),
                meta=payload.get("meta"),
            )
        else:
            response = response_type.model_validate(payload)
        return mark_idempotent_replay(response, context=context)
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def execute_idempotent_read_sync[T: BaseModel | dict[str, Any]](
    *,
    tool_name: str,
    idempotency_key: str | None,
    identity: ResolvedIdentity | None,
    raw_wire_payload: dict[str, Any],
    response_type: type[T] | None,
    work: Callable[[], T],
) -> T:
    """Execute a synchronous read with durable replay when a key is supplied."""
    if idempotency_key is None:
        return work()

    from src.core.database.repositories.idempotency_attempt import DEFAULT_IN_FLIGHT_LEASE
    from src.core.database.repositories.uow import IdempotencyUoW
    from src.core.idempotency_canonical import canonical_payload_hash

    scope = _read_scope(identity)
    if scope is None:
        # No authenticated agent to scope a durable cache entry to (see
        # _read_scope). Run the read directly, exactly as with no
        # idempotency_key at all; the tool's own authorization, if any,
        # still applies inside work().
        return work()
    tenant_id, principal_id, account_id = scope
    reservation = reserve_idempotent(
        IdempotencyUoW,
        tenant_id,
        principal_id=principal_id,
        account_id=account_id,
        tool_name=tool_name,
        idempotency_key=idempotency_key,
        request_hash=canonical_payload_hash(raw_wire_payload),
        lease=DEFAULT_IN_FLIGHT_LEASE,
        decode=lambda envelope: _decode_read_response(envelope, response_type, raw_wire_payload.get("context")),
        enforce_ceiling=True,
        operation_class="read",
    )
    if reservation.replay is not None:
        return reservation.replay
    assert reservation.attempt_id is not None
    with release_reservation_on_error(IdempotencyUoW, tenant_id, reservation.attempt_id):
        response = work()
        with IdempotencyUoW(tenant_id) as uow:
            complete_idempotent(
                uow,
                attempt_id=reservation.attempt_id,
                response_model=response,
                protocol_status="completed",
            )
        return response


async def execute_idempotent_read[T: BaseModel | dict[str, Any]](
    *,
    tool_name: str,
    idempotency_key: str | None,
    identity: ResolvedIdentity | None,
    raw_wire_payload: dict[str, Any],
    response_type: type[T] | None,
    work: Callable[[], Awaitable[T]],
) -> T:
    """Execute an asynchronous read with the same durable semantics."""
    if idempotency_key is None:
        return await work()

    from src.core.database.repositories.idempotency_attempt import DEFAULT_IN_FLIGHT_LEASE
    from src.core.database.repositories.uow import IdempotencyUoW
    from src.core.idempotency_canonical import canonical_payload_hash

    scope = _read_scope(identity)
    if scope is None:
        # No authenticated agent to scope a durable cache entry to (see
        # _read_scope). Run the read directly, exactly as with no
        # idempotency_key at all; the tool's own authorization, if any,
        # still applies inside work().
        return await work()
    tenant_id, principal_id, account_id = scope
    reservation = reserve_idempotent(
        IdempotencyUoW,
        tenant_id,
        principal_id=principal_id,
        account_id=account_id,
        tool_name=tool_name,
        idempotency_key=idempotency_key,
        request_hash=canonical_payload_hash(raw_wire_payload),
        lease=DEFAULT_IN_FLIGHT_LEASE,
        decode=lambda envelope: _decode_read_response(envelope, response_type, raw_wire_payload.get("context")),
        enforce_ceiling=True,
        operation_class="read",
    )
    if reservation.replay is not None:
        return reservation.replay
    assert reservation.attempt_id is not None
    with release_reservation_on_error(IdempotencyUoW, tenant_id, reservation.attempt_id):
        response = await work()
        with IdempotencyUoW(tenant_id) as uow:
            complete_idempotent(
                uow,
                attempt_id=reservation.attempt_id,
                response_model=response,
                protocol_status="completed",
            )
        return response
