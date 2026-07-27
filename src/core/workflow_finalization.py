"""Shared terminalization for manually approved media-buy workflow steps."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from types import SimpleNamespace
from typing import Any, cast

from adcp.types import ContextObject
from sqlalchemy.exc import SQLAlchemyError

from src.core.database.repositories import ApprovalUoW, WorkflowUoW
from src.core.database.repositories.creative import CreativeAssignmentRepository, CreativeRepository
from src.core.database.repositories.media_buy import APPROVED_EXECUTION_SOURCE_STATUSES, MediaBuyRepository
from src.core.exceptions import build_two_layer_error_envelope, safe_adcp_error
from src.core.schemas import CreateMediaBuySuccess, Package
from src.core.tools._media_buy_status import resolve_canonical_status

logger = logging.getLogger(__name__)

_FINALIZATION_ATTEMPTS = 3
_FINALIZATION_RETRY_DELAY_SECONDS = 0.05
_APPROVAL_EXECUTION_LEASE = timedelta(minutes=15)
_RECOVERABLE_SUCCESS_STATUSES = frozenset({"active", "scheduled", "completed"})


def _retryable_database_error(exc: BaseException) -> bool:
    return isinstance(exc, SQLAlchemyError) or (
        isinstance(exc, RuntimeError) and str(exc).startswith("Database is unhealthy")
    )


@dataclass(frozen=True)
class ApprovalFinalization:
    """Outcome of the terminal compare-and-set after external execution."""

    applied: bool
    result: CreateMediaBuySuccess | None = None


class ApprovalExecutionStatus(StrEnum):
    """Business states returned by the shared approval execution flow."""

    READY = "ready"
    WAITING_FOR_CREATIVES = "waiting_for_creatives"
    CLAIM_REFUSED = "claim_refused"
    PENDING_RECONCILIATION = "pending_reconciliation"
    FAILED = "failed"
    SUCCEEDED = "succeeded"
    FINALIZATION_FAILED = "finalization_failed"


@dataclass(frozen=True)
class ApprovalExecutionOutcome:
    """Typed outcome rendered independently by each admin transport."""

    status: ApprovalExecutionStatus
    finalization: ApprovalFinalization | None = None
    error_message: str | None = None
    blocking_creative_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class _ApprovalTerminalPayload:
    """Values persisted by one terminal workflow transition."""

    status: str
    response_data: dict[str, Any]
    stored_error: str | None
    result: CreateMediaBuySuccess | None


@dataclass(frozen=True)
class _ApprovalRecoveryState:
    """Durable domain state used to reconcile a claimed approval."""

    step_id: str
    request_data: dict[str, Any]
    media_buy_status: str
    media_buy_updated_at: datetime | None


def _run_database_operation_with_retries[T](
    operation: Callable[[], T],
    *,
    retry_message: str,
    exhausted_message: str | None = None,
) -> T | None:
    """Retry one database-only operation without repeating external work."""
    for attempt in range(1, _FINALIZATION_ATTEMPTS + 1):
        try:
            return operation()
        except (SQLAlchemyError, RuntimeError) as exc:
            if not _retryable_database_error(exc):
                raise
            if attempt == _FINALIZATION_ATTEMPTS:
                if exhausted_message is not None:
                    logger.error(exhausted_message)
                return None
            logger.warning(retry_message, attempt)
            time.sleep(_FINALIZATION_RETRY_DELAY_SECONDS * (2 ** (attempt - 1)))
    return None


def _approval_terminal_payload(
    *,
    uow: ApprovalUoW,
    media_buy_id: str | None,
    succeeded: bool,
    error_message: str | None,
    context: ContextObject | dict[str, Any] | None,
) -> _ApprovalTerminalPayload:
    """Build the safe response and result stored by a terminal transition."""
    assert uow.media_buys is not None
    if succeeded:
        result: CreateMediaBuySuccess | None = None
        response_data: dict[str, Any] = {"approved": True}
        if media_buy_id is not None:
            packages = uow.media_buys.get_packages(media_buy_id)
            result = CreateMediaBuySuccess.sync_success(
                media_buy_id=media_buy_id,
                packages=[Package(package_id=package.package_id) for package in packages],
                context=context,
            )
            response_data = result.model_dump(mode="json", exclude_none=True)
        return _ApprovalTerminalPayload("completed", response_data, None, result)

    source = safe_adcp_error(RuntimeError(error_message or "Approved media buy execution failed"))
    return _ApprovalTerminalPayload(
        "failed",
        build_two_layer_error_envelope(source),
        source.message,
        None,
    )


def _unknown_execution_claim_result(
    *,
    uow: ApprovalUoW,
    step_id: str,
    media_buy_id: str | None,
    mark_media_buy_unknown: bool,
    media_buy_expected_updated_at: datetime | None,
) -> ApprovalFinalization | None:
    """Claim an expired execution lease before publishing a failed task."""
    if not mark_media_buy_unknown or media_buy_id is None:
        return None
    assert uow.media_buys is not None
    assert uow.workflows is not None
    if uow.media_buys.mark_approved_execution_unknown(
        media_buy_id,
        expected_updated_at=media_buy_expected_updated_at,
    ):
        return None

    existing = uow.workflows.get_by_step_id(step_id)
    already_failed = existing is not None and existing.status == "failed" and existing.response_data
    return ApprovalFinalization(applied=bool(already_failed))


def _existing_terminal_finalization(
    *,
    existing: Any,
    expected_status: str,
    succeeded: bool,
    media_buy_id: str | None,
) -> ApprovalFinalization:
    """Return an idempotent result for a transition another worker completed."""
    if existing is not None and existing.status == expected_status and existing.response_data:
        if not succeeded or media_buy_id is None:
            return ApprovalFinalization(applied=True)
        stored_result = CreateMediaBuySuccess.model_validate(existing.response_data)
        if stored_result.media_buy_id == media_buy_id:
            return ApprovalFinalization(applied=True, result=stored_result)
        return ApprovalFinalization(applied=False)
    if existing is not None and existing.status == "approved":
        raise SQLAlchemyError("approval finalization did not reach a terminal state")
    return ApprovalFinalization(applied=False)


def _finalize_media_buy_approval_once(
    *,
    tenant_id: str,
    step_id: str,
    media_buy_id: str | None,
    succeeded: bool,
    error_message: str | None,
    context: ContextObject | dict[str, Any] | None,
    mark_media_buy_failed: bool,
    mark_media_buy_unknown: bool,
    media_buy_expected_updated_at: datetime | None,
    apply_flight_status: bool,
) -> ApprovalFinalization:
    """Attempt one atomic terminal transition in a fresh UoW."""
    with ApprovalUoW(tenant_id) as uow:
        assert uow.workflows is not None
        assert uow.media_buys is not None
        claim_result = _unknown_execution_claim_result(
            uow=uow,
            step_id=step_id,
            media_buy_id=media_buy_id,
            mark_media_buy_unknown=mark_media_buy_unknown,
            media_buy_expected_updated_at=media_buy_expected_updated_at,
        )
        if claim_result is not None:
            return claim_result

        payload = _approval_terminal_payload(
            uow=uow,
            media_buy_id=media_buy_id,
            succeeded=succeeded,
            error_message=error_message,
            context=context,
        )
        transitioned = uow.workflows.transition_if_nonterminal(
            step_id,
            status=payload.status,
            completed_at=datetime.now(UTC),
            response_data=payload.response_data,
            error_message=payload.stored_error,
        )
        if transitioned is None:
            return _existing_terminal_finalization(
                existing=uow.workflows.get_by_step_id(step_id),
                expected_status=payload.status,
                succeeded=succeeded,
                media_buy_id=media_buy_id,
            )
        if not succeeded and media_buy_id is not None and mark_media_buy_failed and not mark_media_buy_unknown:
            uow.media_buys.update_status(media_buy_id, "failed")
        elif succeeded and media_buy_id is not None and apply_flight_status:
            media_buy = uow.media_buys.get_by_id(media_buy_id)
            if media_buy is None:
                raise SQLAlchemyError("approved media buy disappeared during terminal finalization")
            uow.media_buys.update_status(
                media_buy_id,
                media_buy_status_from_flight_dates(
                    start_time=media_buy.start_time,
                    end_time=media_buy.end_time,
                    start_date=cast(date, media_buy.start_date),
                    end_date=cast(date, media_buy.end_date),
                ),
            )
        return ApprovalFinalization(applied=True, result=payload.result)


def finalize_media_buy_approval_step(
    *,
    tenant_id: str,
    step_id: str,
    media_buy_id: str | None,
    succeeded: bool,
    error_message: str | None = None,
    context: ContextObject | dict[str, Any] | None = None,
    mark_media_buy_failed: bool = True,
    mark_media_buy_unknown: bool = False,
    media_buy_expected_updated_at: datetime | None = None,
    apply_flight_status: bool = False,
) -> ApprovalFinalization:
    """Persist the durable terminal outcome of an approval execution.

    The adapter runs in its own UoW, so this helper accepts scalars only and
    opens a fresh session.  Workflow status, failure-domain status, and stored
    response are committed together.  The repository's terminal guard ensures
    an already-finalized decision is never overwritten.
    """

    def operation() -> ApprovalFinalization:
        return _finalize_media_buy_approval_once(
            tenant_id=tenant_id,
            step_id=step_id,
            media_buy_id=media_buy_id,
            succeeded=succeeded,
            error_message=error_message,
            context=context,
            mark_media_buy_failed=mark_media_buy_failed,
            mark_media_buy_unknown=mark_media_buy_unknown,
            media_buy_expected_updated_at=media_buy_expected_updated_at,
            apply_flight_status=apply_flight_status,
        )

    result = _run_database_operation_with_retries(
        operation,
        retry_message=(
            "Approval finalization transaction failed; retrying without re-running the adapter (attempt %s)"
        ),
        exhausted_message=(
            "Approval finalization remains pending after bounded retries; "
            "tasks/get reconciliation will retry from persisted domain state"
        ),
    )
    return result or ApprovalFinalization(applied=False)


def _claimed_approval_step(
    tenant_id: str,
    media_buy_id: str,
) -> tuple[str, dict[str, Any]] | None:
    """Load the claimed approval needed for creative-unblock finalization."""
    with WorkflowUoW(tenant_id) as uow:
        assert uow.workflows is not None
        step = uow.workflows.get_claimed_create_approval_step_for_media_buy(media_buy_id)
        if step is None:
            return None
        request_data = dict(step.request_data) if isinstance(step.request_data, dict) else {}
        return step.step_id, request_data


def finalize_latest_media_buy_approval_step(
    *,
    tenant_id: str,
    media_buy_id: str,
    succeeded: bool,
    error_message: str | None = None,
    apply_flight_status: bool = False,
) -> ApprovalFinalization:
    """Finalize the latest claimed approval mapped to a creative-unblocked buy."""
    claimed = _run_database_operation_with_retries(
        lambda: _claimed_approval_step(tenant_id, media_buy_id),
        retry_message="Claimed approval lookup failed; retrying (attempt %s)",
    )
    if claimed is None:
        return ApprovalFinalization(applied=False)
    step_id, request_data = claimed

    return finalize_media_buy_approval_step(
        tenant_id=tenant_id,
        step_id=step_id,
        media_buy_id=media_buy_id,
        succeeded=succeeded,
        error_message=error_message,
        context=request_data.get("context") if isinstance(request_data.get("context"), dict) else None,
        apply_flight_status=apply_flight_status,
    )


def media_buy_status_from_flight_dates(
    *,
    start_time: datetime | None,
    end_time: datetime | None,
    start_date: date | None,
    end_date: date | None,
    now: datetime | None = None,
) -> str:
    """Resolve an approved buy's persisted flight status through the canonical lifecycle resolver."""
    current = now or datetime.now(UTC)
    current_utc = current.replace(tzinfo=UTC) if current.tzinfo is None else current.astimezone(UTC)

    def normalize_utc(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    flight = SimpleNamespace(
        status="active",
        start_time=normalize_utc(start_time),
        end_time=normalize_utc(end_time),
        start_date=start_date,
        end_date=end_date,
        is_paused=False,
    )
    canonical = resolve_canonical_status(flight, current_utc.date())
    # Persistence calls this pre-flight state ``scheduled``; the wire lifecycle
    # resolver calls the same state ``pending_start``.
    return "scheduled" if canonical == "pending_start" else canonical


def _approval_creative_gate(
    *,
    assignments: CreativeAssignmentRepository,
    creatives: CreativeRepository,
    media_buy_id: str,
    principal_id: str,
) -> tuple[bool, tuple[str, ...]]:
    """Return whether the buy has at least one assignment and all are approved."""
    creative_ids = list(
        dict.fromkeys(assignment.creative_id for assignment in assignments.get_by_media_buy(media_buy_id))
    )
    if not creative_ids:
        return False, ()
    creative_rows = creatives.get_by_ids(creative_ids, principal_id)
    status_by_id = {creative.creative_id: creative.status for creative in creative_rows}
    blocking_ids = tuple(
        creative_id for creative_id in creative_ids if status_by_id.get(creative_id) not in {"approved", "active"}
    )
    return not blocking_ids, blocking_ids


def prepare_media_buy_approval_execution(
    *,
    media_buys: MediaBuyRepository,
    assignments: CreativeAssignmentRepository,
    creatives: CreativeRepository,
    media_buy_id: str,
    approved_by: str | None,
) -> ApprovalExecutionOutcome:
    """Apply the creative gate and atomically claim one adapter execution.

    The caller owns the surrounding UoW so the human workflow decision and
    the irreversible domain claim can commit together.
    """
    media_buy = media_buys.get_by_id(media_buy_id)
    if media_buy is None or media_buy.status not in APPROVED_EXECUTION_SOURCE_STATUSES:
        return ApprovalExecutionOutcome(ApprovalExecutionStatus.CLAIM_REFUSED)

    gate_satisfied, blocking_ids = _approval_creative_gate(
        assignments=assignments,
        creatives=creatives,
        media_buy_id=media_buy_id,
        principal_id=media_buy.principal_id,
    )
    approval_fields: dict[str, Any] = {}
    if approved_by is not None:
        approval_fields = {"approved_at": datetime.now(UTC), "approved_by": approved_by}
    if not gate_satisfied:
        media_buys.update_status(
            media_buy_id,
            "pending_creatives",
            **approval_fields,
        )
        return ApprovalExecutionOutcome(
            ApprovalExecutionStatus.WAITING_FOR_CREATIVES,
            blocking_creative_ids=blocking_ids,
        )

    if not media_buys.claim_approved_execution(media_buy_id):
        return ApprovalExecutionOutcome(ApprovalExecutionStatus.CLAIM_REFUSED)
    if approval_fields:
        media_buys.update_fields(media_buy_id, **approval_fields)
    return ApprovalExecutionOutcome(ApprovalExecutionStatus.READY)


def _finalize_approval_execution(
    *,
    tenant_id: str,
    media_buy_id: str,
    step_id: str | None,
    succeeded: bool,
    error_message: str | None,
    context: ContextObject | dict[str, Any] | None,
    apply_flight_status: bool,
) -> ApprovalFinalization:
    """Finalize either an explicitly identified or creative-unblocked step."""
    if step_id is None:
        return finalize_latest_media_buy_approval_step(
            tenant_id=tenant_id,
            media_buy_id=media_buy_id,
            succeeded=succeeded,
            error_message=error_message,
            apply_flight_status=apply_flight_status,
        )
    return finalize_media_buy_approval_step(
        tenant_id=tenant_id,
        step_id=step_id,
        media_buy_id=media_buy_id,
        succeeded=succeeded,
        error_message=error_message,
        context=context,
        apply_flight_status=apply_flight_status,
    )


def apply_media_buy_execution_outcome(
    *,
    tenant_id: str,
    media_buy_id: str,
    step_id: str | None,
    success: bool | None,
    error_message: str | None,
    context: ContextObject | dict[str, Any] | None = None,
    apply_flight_status: bool = False,
) -> ApprovalExecutionOutcome:
    """Own the adapter tri-state to durable workflow-finalization mapping."""
    if success is None:
        return ApprovalExecutionOutcome(
            ApprovalExecutionStatus.PENDING_RECONCILIATION,
            error_message=error_message,
        )

    finalization = _finalize_approval_execution(
        tenant_id=tenant_id,
        media_buy_id=media_buy_id,
        step_id=step_id,
        succeeded=success,
        error_message=error_message,
        context=context,
        apply_flight_status=apply_flight_status,
    )
    if not finalization.applied or (success and finalization.result is None):
        return ApprovalExecutionOutcome(
            ApprovalExecutionStatus.FINALIZATION_FAILED,
            finalization=finalization,
            error_message=error_message,
        )
    return ApprovalExecutionOutcome(
        ApprovalExecutionStatus.SUCCEEDED if success else ApprovalExecutionStatus.FAILED,
        finalization=finalization,
        error_message=error_message,
    )


def execute_and_finalize_media_buy_approval(
    *,
    tenant_id: str,
    media_buy_id: str,
    step_id: str | None,
    context: ContextObject | dict[str, Any] | None = None,
    apply_flight_status: bool = False,
) -> ApprovalExecutionOutcome:
    """Execute one claimed media buy and publish its durable tri-state result."""
    from src.core.tools.media_buy_create import execute_approved_media_buy

    success, error_message = execute_approved_media_buy(
        media_buy_id,
        tenant_id,
        execution_claimed=True,
    )
    return apply_media_buy_execution_outcome(
        tenant_id=tenant_id,
        media_buy_id=media_buy_id,
        step_id=step_id,
        success=success,
        error_message=error_message,
        context=context,
        apply_flight_status=apply_flight_status,
    )


def _approval_recovery_state(
    tenant_id: str,
    media_buy_id: str,
) -> _ApprovalRecoveryState | None:
    """Load workflow and domain rows together for durable reconciliation."""
    with ApprovalUoW(tenant_id) as uow:
        assert uow.workflows is not None
        assert uow.media_buys is not None
        step = uow.workflows.get_claimed_create_approval_step_for_media_buy(media_buy_id)
        media_buy = uow.media_buys.get_by_id(media_buy_id)
        if step is None or media_buy is None:
            return None
        request_data = dict(step.request_data) if isinstance(step.request_data, dict) else {}
        return _ApprovalRecoveryState(
            step_id=step.step_id,
            request_data=request_data,
            media_buy_status=media_buy.status,
            media_buy_updated_at=getattr(media_buy, "updated_at", None),
        )


def _approval_context(request_data: dict[str, Any]) -> dict[str, Any] | None:
    context = request_data.get("context")
    return context if isinstance(context, dict) else None


def _utc_datetime(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _reconcile_approval_state(
    *,
    tenant_id: str,
    media_buy_id: str,
    state: _ApprovalRecoveryState,
) -> ApprovalFinalization:
    """Project one persisted media-buy state into a terminal workflow result."""
    if state.media_buy_status in _RECOVERABLE_SUCCESS_STATUSES:
        return finalize_media_buy_approval_step(
            tenant_id=tenant_id,
            step_id=state.step_id,
            media_buy_id=media_buy_id,
            succeeded=True,
            context=_approval_context(state.request_data),
            apply_flight_status=True,
        )
    if state.media_buy_status == "failed":
        return finalize_media_buy_approval_step(
            tenant_id=tenant_id,
            step_id=state.step_id,
            media_buy_id=media_buy_id,
            succeeded=False,
            error_message="Approved media buy execution failed",
        )
    if state.media_buy_status == "activation_unknown":
        return finalize_media_buy_approval_step(
            tenant_id=tenant_id,
            step_id=state.step_id,
            media_buy_id=media_buy_id,
            succeeded=False,
            error_message="Approved media buy execution requires operator reconciliation",
            mark_media_buy_failed=False,
        )
    if state.media_buy_status != "activating":
        return ApprovalFinalization(applied=False)

    observed_updated_at = _utc_datetime(state.media_buy_updated_at)
    stale_before = datetime.now(UTC) - _APPROVAL_EXECUTION_LEASE
    if observed_updated_at is not None and observed_updated_at > stale_before:
        return ApprovalFinalization(applied=False)
    return finalize_media_buy_approval_step(
        tenant_id=tenant_id,
        step_id=state.step_id,
        media_buy_id=media_buy_id,
        succeeded=False,
        error_message="Approved media buy execution requires operator reconciliation",
        mark_media_buy_failed=False,
        mark_media_buy_unknown=True,
        media_buy_expected_updated_at=observed_updated_at,
    )


def reconcile_claimed_media_buy_approval_step(
    *,
    tenant_id: str,
    media_buy_id: str,
) -> ApprovalFinalization:
    """Recover a claimed approval from persisted domain state only.

    ``execute_approved_media_buy`` durably marks a successful buy active and
    its public wrapper marks a failed buy failed. A later tasks/get request can
    therefore finish the workflow after a database outage without invoking the
    external adapter again.
    """
    state = _run_database_operation_with_retries(
        lambda: _approval_recovery_state(tenant_id, media_buy_id),
        retry_message="Approval reconciliation lookup failed; retrying (attempt %s)",
    )
    if state is None:
        return ApprovalFinalization(applied=False)
    return _reconcile_approval_state(
        tenant_id=tenant_id,
        media_buy_id=media_buy_id,
        state=state,
    )
