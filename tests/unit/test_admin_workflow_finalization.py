"""Approval finalization retries DB commits without repeating adapter work."""

import ast
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, Mock, patch

import pytest
from sqlalchemy.exc import SQLAlchemyError

from src.core.database.models import PersistedMediaBuyStatus
from src.core.database.repositories.media_buy import (
    APPROVED_EXECUTION_SOURCE_STATUSES,
    ApprovalTrigger,
)
from src.core.database.repositories.workflow import WorkflowRepository
from src.core.tools.media_buy_create import ApprovalOutcome, ApprovalResult
from src.core.workflow_finalization import (
    ApprovalExecutionStatus,
    ApprovalFinalization,
    execute_and_finalize_media_buy_approval,
    finalize_media_buy_approval_step,
    media_buy_status_from_flight_dates,
    prepare_media_buy_approval_execution,
    reconcile_claimed_media_buy_approval_step,
)
from tests.factories import PrincipalFactory

_TRANSITIONED = object()


class _WorkflowRepo:
    def __init__(self, *, transition_result=_TRANSITIONED, existing=None) -> None:
        self.transitions = 0
        self.transition_result = transition_result
        self.existing = existing

    def transition_if_nonterminal(self, step_id, **kwargs):
        self.transitions += 1
        return self.transition_result

    def get_by_step_id(self, step_id):
        return self.existing

    def get_claimed_create_approval_step_for_media_buy(self, media_buy_id):
        return self.existing


def _persisted_row(status: str, *, revision: int = 1, confirmed_at=None):
    """A persisted media-buy row as the finalizer reads it.

    ``revision`` and ``confirmed_at`` are not decoration: the pinned success document
    lists both in ``required``, so the terminal payload reads them off the row rather
    than fabricating them, and a double without them fails construction.
    """
    return SimpleNamespace(status=status, revision=revision, confirmed_at=confirmed_at)


class _MediaBuyRepo:
    def __init__(self, *, media_buy=None) -> None:
        self.package_reads = 0
        self.media_buy = media_buy

    def get_packages(self, media_buy_id):
        self.package_reads += 1
        return []

    def update_status(self, media_buy_id, status):
        raise AssertionError("success finalization must not mark the buy failed")

    def get_by_id(self, media_buy_id):
        return self.media_buy


class _FailureMediaBuyRepo(_MediaBuyRepo):
    def __init__(self, *, media_buy=None) -> None:
        super().__init__(media_buy=media_buy)
        self.status_updates = []
        self.unknown_updates = []
        self.expected_lease_versions = []

    def update_status(self, media_buy_id, status):
        self.status_updates.append((media_buy_id, status))

    def mark_approved_execution_unknown(self, media_buy_id, *, expected_updated_at=None):
        self.unknown_updates.append(media_buy_id)
        self.expected_lease_versions.append(expected_updated_at)
        return True


class _LostLeaseMediaBuyRepo(_FailureMediaBuyRepo):
    def mark_approved_execution_unknown(self, media_buy_id, *, expected_updated_at=None):
        self.unknown_updates.append(media_buy_id)
        self.expected_lease_versions.append(expected_updated_at)
        return False


class _ApprovalUoW:
    def __init__(
        self,
        *,
        fail_commit: bool,
        workflows: _WorkflowRepo | None = None,
        media_buys: _MediaBuyRepo | None = None,
    ) -> None:
        self.fail_commit = fail_commit
        self.workflows = workflows or _WorkflowRepo()
        self.media_buys = media_buys or _MediaBuyRepo()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.fail_commit:
            raise SQLAlchemyError("simulated commit outage")


class _StopSignal:
    def __init__(self, waits):
        self._waits = iter(waits)
        self.set_called = False

    def wait(self, _timeout):
        return next(self._waits)

    def set(self):
        self.set_called = True


class _ThreadRecorder:
    def __init__(self):
        self.join_timeout = None

    def join(self, *, timeout):
        self.join_timeout = timeout


def test_plain_approval_completion_is_guarded_and_terminal():
    """No-execution approval completion accepts only an already-claimed step."""
    repo = WorkflowRepository(Mock(), "tenant_1")
    transitioned = SimpleNamespace(status="completed")
    with patch.object(repo, "_atomic_transition", return_value=transitioned) as transition:
        result = repo.complete_claimed_approval("step_1")

    transition.assert_called_once_with(
        "step_1",
        status="completed",
        status_guard=ANY,
        completed_at=ANY,
        response_data={"approved": True},
    )
    call = transition.call_args
    assert call.kwargs["status_guard"].right.value == "approved"
    assert call.kwargs["completed_at"].tzinfo is UTC
    assert result is transitioned


def test_transient_finalization_commit_failure_retries_without_rerunning_adapter():
    """Bounded request retries can recover without repeating adapter work."""
    adapter = Mock(return_value=(True, None))
    failing_uows = [_ApprovalUoW(fail_commit=True) for _ in range(2)]
    successful_uow = _ApprovalUoW(fail_commit=False)
    uows = iter((*failing_uows, successful_uow))

    succeeded, error_message = adapter("mb_1", "tenant_1")
    with (
        patch("src.core.workflow_finalization.ApprovalUoW", side_effect=lambda _tenant_id: next(uows)) as uow_type,
        patch("src.core.workflow_finalization.time.sleep"),
    ):
        result = finalize_media_buy_approval_step(
            tenant_id="tenant_1",
            step_id="step_1",
            media_buy_id="mb_1",
            succeeded=succeeded,
            error_message=error_message,
        )

    assert result.applied is True
    assert result.result is not None
    assert result.result.media_buy_id == "mb_1"
    adapter.assert_called_once_with("mb_1", "tenant_1")
    assert uow_type.call_count == 3
    assert all(uow.workflows.transitions == 1 for uow in failing_uows)
    assert successful_uow.workflows.transitions == 1


def test_success_finalization_applies_requested_flight_status_atomically():
    """Workflow completion resolves creative-unblock status in the terminal UoW."""
    media_buy = SimpleNamespace(
        start_time=None,
        end_time=None,
        start_date=date(2099, 1, 1),
        end_date=date(2099, 1, 31),
        revision=1,
        confirmed_at=None,
    )
    media_buys = _FailureMediaBuyRepo(media_buy=media_buy)
    uow = _ApprovalUoW(fail_commit=False, media_buys=media_buys)

    with (
        patch("src.core.workflow_finalization.ApprovalUoW", return_value=uow),
        patch(
            "src.core.workflow_finalization.media_buy_status_from_flight_dates",
            return_value="scheduled",
        ) as resolve_status,
    ):
        result = finalize_media_buy_approval_step(
            tenant_id="tenant_1",
            step_id="step_1",
            media_buy_id="mb_1",
            succeeded=True,
            apply_flight_status=True,
        )

    assert result.applied is True
    resolve_status.assert_called_once_with(
        start_time=None,
        end_time=None,
        start_date=date(2099, 1, 1),
        end_date=date(2099, 1, 31),
    )
    assert media_buys.status_updates == [("mb_1", "scheduled")]


def test_permanent_finalization_outage_returns_retryable_pending_without_rerunning_adapter():
    """A sustained outage releases the request worker and leaves reconciliation work."""
    adapter = Mock(return_value=(True, None))
    failing_uows = [_ApprovalUoW(fail_commit=True) for _ in range(3)]
    uows = iter(failing_uows)

    succeeded, error_message = adapter("mb_1", "tenant_1")
    with (
        patch("src.core.workflow_finalization.ApprovalUoW", side_effect=lambda _tenant_id: next(uows)) as uow_type,
        patch("src.core.workflow_finalization.time.sleep"),
    ):
        result = finalize_media_buy_approval_step(
            tenant_id="tenant_1",
            step_id="step_1",
            media_buy_id="mb_1",
            succeeded=succeeded,
            error_message=error_message,
        )

    assert result.applied is False
    adapter.assert_called_once_with("mb_1", "tenant_1")
    assert uow_type.call_count == 3


def test_post_commit_unknown_verifies_stored_terminal_result():
    """A retry recognizes the first commit instead of reporting false failure."""
    persisted = {
        "media_buy_id": "mb_1",
        "packages": [],
        "status": "completed",
        "confirmed_at": "2026-01-01T00:00:00Z",
        "revision": 1,
    }
    first = _ApprovalUoW(fail_commit=True)
    terminal_repo = _WorkflowRepo(
        transition_result=None,
        existing=SimpleNamespace(status="completed", response_data=persisted),
    )
    second = _ApprovalUoW(fail_commit=False, workflows=terminal_repo)
    uows = iter((first, second))

    with (
        patch("src.core.workflow_finalization.ApprovalUoW", side_effect=lambda _tenant_id: next(uows)),
        patch("src.core.workflow_finalization.time.sleep"),
    ):
        result = finalize_media_buy_approval_step(
            tenant_id="tenant_1",
            step_id="step_1",
            media_buy_id="mb_1",
            succeeded=True,
        )

    assert result.applied is True
    assert result.result is not None
    assert result.result.media_buy_id == "mb_1"
    assert terminal_repo.transitions == 1


def test_reconciliation_uses_persisted_domain_state_without_adapter_call():
    """tasks/get recovery terminalizes success and re-derives its flight status."""
    claimed_step = SimpleNamespace(
        step_id="step_1",
        request_data={"context": {"trace_id": "trace_1"}},
    )
    lookup_uow = _ApprovalUoW(
        fail_commit=False,
        workflows=_WorkflowRepo(existing=claimed_step),
        media_buys=_MediaBuyRepo(media_buy=_persisted_row("active")),
    )
    finalization_media_buy = SimpleNamespace(
        start_time=None,
        end_time=None,
        start_date=date(2099, 1, 1),
        end_date=date(2099, 1, 31),
        revision=1,
        confirmed_at=None,
    )
    finalization_media_repo = _FailureMediaBuyRepo(media_buy=finalization_media_buy)
    finalization_uow = _ApprovalUoW(fail_commit=False, media_buys=finalization_media_repo)
    uows = iter((lookup_uow, finalization_uow))

    with (
        patch("src.core.workflow_finalization.ApprovalUoW", side_effect=lambda _tenant_id: next(uows)),
        patch("src.core.workflow_finalization.media_buy_status_from_flight_dates", return_value="scheduled"),
    ):
        result = reconcile_claimed_media_buy_approval_step(
            tenant_id="tenant_1",
            media_buy_id="mb_1",
        )

    assert result.applied is True
    assert result.result is not None
    assert result.result.media_buy_id == "mb_1"
    assert result.result.context is not None
    assert result.result.context.trace_id == "trace_1"
    assert finalization_media_repo.status_updates == [("mb_1", "scheduled")]


def test_reconciliation_keeps_pre_adapter_pending_state_nonterminal():
    """An absent post-adapter marker can never be synthesized as success."""
    claimed_step = SimpleNamespace(step_id="step_1", request_data={})
    lookup_uow = _ApprovalUoW(
        fail_commit=False,
        workflows=_WorkflowRepo(existing=claimed_step),
        media_buys=_MediaBuyRepo(media_buy=_persisted_row("pending_approval")),
    )

    with patch("src.core.workflow_finalization.ApprovalUoW", return_value=lookup_uow):
        result = reconcile_claimed_media_buy_approval_step(
            tenant_id="tenant_1",
            media_buy_id="mb_1",
        )

    assert result.applied is False
    assert result.result is None


def test_reconciliation_terminalizes_ambiguous_activation_without_marking_buy_failed():
    """A durable execution claim becomes an operator-recoverable task failure."""
    claimed_step = SimpleNamespace(step_id="step_1", request_data={})
    lookup_uow = _ApprovalUoW(
        fail_commit=False,
        workflows=_WorkflowRepo(existing=claimed_step),
        media_buys=_MediaBuyRepo(media_buy=_persisted_row("activation_unknown")),
    )
    finalization_media_repo = _FailureMediaBuyRepo()
    finalization_uow = _ApprovalUoW(fail_commit=False, media_buys=finalization_media_repo)
    uows = iter((lookup_uow, finalization_uow))

    with patch("src.core.workflow_finalization.ApprovalUoW", side_effect=lambda _tenant_id: next(uows)):
        result = reconcile_claimed_media_buy_approval_step(
            tenant_id="tenant_1",
            media_buy_id="mb_1",
        )

    assert result.applied is True
    assert result.result is None
    assert finalization_uow.workflows.transitions == 1
    assert finalization_media_repo.status_updates == []


def test_reconciliation_keeps_recent_execution_claim_working():
    """Polling cannot fail a request while its adapter lease is active."""
    claimed_step = SimpleNamespace(step_id="step_1", request_data={})
    lookup_uow = _ApprovalUoW(
        fail_commit=False,
        workflows=_WorkflowRepo(existing=claimed_step),
        media_buys=_MediaBuyRepo(
            media_buy=SimpleNamespace(
                status="activating",
                updated_at=datetime.now(UTC),
            )
        ),
    )

    with patch("src.core.workflow_finalization.ApprovalUoW", return_value=lookup_uow):
        result = reconcile_claimed_media_buy_approval_step(
            tenant_id="tenant_1",
            media_buy_id="mb_1",
        )

    assert result.applied is False
    assert result.result is None


def test_reconciliation_terminalizes_expired_execution_claim():
    """A crashed pre-dispatch claim cannot leave tasks/get working forever."""
    claimed_step = SimpleNamespace(step_id="step_1", request_data={})
    lookup_uow = _ApprovalUoW(
        fail_commit=False,
        workflows=_WorkflowRepo(existing=claimed_step),
        media_buys=_MediaBuyRepo(
            media_buy=SimpleNamespace(
                status="activating",
                updated_at=datetime.now(UTC) - timedelta(minutes=16),
            )
        ),
    )
    finalization_media_repo = _FailureMediaBuyRepo()
    finalization_uow = _ApprovalUoW(fail_commit=False, media_buys=finalization_media_repo)
    uows = iter((lookup_uow, finalization_uow))

    with patch("src.core.workflow_finalization.ApprovalUoW", side_effect=lambda _tenant_id: next(uows)):
        result = reconcile_claimed_media_buy_approval_step(
            tenant_id="tenant_1",
            media_buy_id="mb_1",
        )

    assert result.applied is True
    assert finalization_media_repo.status_updates == []
    assert finalization_media_repo.unknown_updates == ["mb_1"]
    assert finalization_media_repo.expected_lease_versions == [
        lookup_uow.media_buys.media_buy.updated_at,
    ]


def test_expired_reconciliation_cannot_fail_task_after_worker_completes():
    """A lost takeover CAS leaves the workflow untouched for late success."""
    media_repo = _LostLeaseMediaBuyRepo()
    uow = _ApprovalUoW(fail_commit=False, media_buys=media_repo)
    observed_lease_version = datetime.now(UTC) - timedelta(minutes=16)

    with patch("src.core.workflow_finalization.ApprovalUoW", return_value=uow):
        result = finalize_media_buy_approval_step(
            tenant_id="tenant_1",
            step_id="step_1",
            media_buy_id="mb_1",
            succeeded=False,
            mark_media_buy_failed=False,
            mark_media_buy_unknown=True,
            media_buy_expected_updated_at=observed_lease_version,
        )

    assert result.applied is False
    assert uow.workflows.transitions == 0
    assert media_repo.unknown_updates == ["mb_1"]
    assert media_repo.expected_lease_versions == [observed_lease_version]


def test_definitive_adapter_refusal_persists_failed_status():
    """A refusal the ad server ANSWERED leaves durable FAILED evidence.

    This obligation used to live on a decorator that wrapped the whole execution and
    keyed off a tri-state boolean. It is now the writer's own failure arm, reached only
    where the adapter said no — which is the same rule, expressed where it applies.
    """
    from src.core.tools.media_buy_create import ApprovalOutcome, _mark_approval_failed

    media_repo = _FailureMediaBuyRepo()
    uow = _ApprovalUoW(fail_commit=False, media_buys=media_repo)

    with patch("src.core.database.repositories.uow.MediaBuyUoW", return_value=uow):
        result = _mark_approval_failed("tenant_1", "mb_1", "adapter rejected")

    assert result.outcome is ApprovalOutcome.FAILED
    assert result.error_msg == "adapter rejected"
    assert media_repo.status_updates == [("mb_1", PersistedMediaBuyStatus.FAILED)]


def test_post_dispatch_ambiguity_does_not_persist_a_false_failure():
    """An externally created order must never be rewritten as failed.

    The ad server was asked and may have acted, so the outcome is unknown, not failed.
    It is recorded as the claim's own ambiguous state for the lease reconciler, and
    NOTHING writes FAILED.
    """
    from src.core.tools.media_buy_create import ApprovalOutcome, _mark_approved_execution_unknown

    media_repo = _FailureMediaBuyRepo()
    uow = _ApprovalUoW(fail_commit=False, media_buys=media_repo)

    with patch("src.core.database.repositories.MediaBuyUoW", return_value=uow):
        result = _mark_approved_execution_unknown("mb_1", "tenant_1", "local status commit failed")

    assert result.outcome is ApprovalOutcome.PENDING_RECONCILIATION
    assert result.error_msg == "local status commit failed"
    assert media_repo.unknown_updates == ["mb_1"]
    assert media_repo.status_updates == []


def test_live_execution_lease_renews_durable_claim():
    """The worker heartbeat prevents age-only takeover while it is alive."""
    from src.core.tools.media_buy_create import _ApprovalExecutionLease

    media_repo = Mock()
    media_repo.renew_approved_execution_lease.return_value = True
    uow = _ApprovalUoW(fail_commit=False, media_buys=media_repo)
    lease = _ApprovalExecutionLease("mb_1", "tenant_1")

    with patch("src.core.database.repositories.MediaBuyUoW", return_value=uow):
        renewed = lease._renew_once()

    assert renewed is True
    media_repo.renew_approved_execution_lease.assert_called_once_with("mb_1")


def test_live_execution_lease_loop_renews_and_stops_after_lease_loss():
    """The heartbeat loop renews once and exits when its CAS loses."""
    from src.core.tools.media_buy_create import _ApprovalExecutionLease

    lease = _ApprovalExecutionLease("mb_1", "tenant_1")
    stop = _StopSignal([False])
    renewals = []

    def lose_lease():
        renewals.append("renewed")
        return False

    lease._stop = stop
    lease._renew_once = lose_lease
    lease._run()

    assert renewals == ["renewed"]


def test_live_execution_lease_error_log_sanitizes_media_buy_id(caplog):
    """A crafted identifier cannot inject a second approval log record."""
    from src.core.tools.media_buy_create import _ApprovalExecutionLease

    lease = _ApprovalExecutionLease("mb_1\r\nFORGED", "tenant_1")
    lease._stop = _StopSignal([False, True])

    def failed_renewal():
        raise RuntimeError("renewal failed")

    lease._renew_once = failed_renewal
    with caplog.at_level("ERROR", logger="src.core.tools.media_buy_create"):
        lease._run()

    message = next(
        record.getMessage() for record in caplog.records if "Could not renew execution lease" in record.getMessage()
    )
    assert "\r" not in message
    assert "\n" not in message
    assert "mb_1FORGED" in message


def test_live_execution_lease_stop_prevents_renewal_and_joins_thread():
    """Closing the heartbeat wakes its wait and joins without another renewal."""
    from src.core.tools.media_buy_create import _ApprovalExecutionLease

    lease = _ApprovalExecutionLease("mb_1", "tenant_1")
    stop = _StopSignal([True])
    thread = _ThreadRecorder()

    def unexpected_renewal():
        raise AssertionError("stopped lease must not renew")

    lease._stop = stop
    lease._thread = thread
    lease._renew_once = unexpected_renewal
    lease._run()
    lease.close()

    assert stop.set_called is True
    assert thread.join_timeout == 1.0


def test_blocking_creative_log_sanitizes_identifiers(caplog):
    """The workflow creative gate neutralizes CR/LF before logging IDs."""
    from flask import Flask

    from src.admin.blueprints import workflows as workflows_module

    app = Flask(__name__)
    app.secret_key = "test-only"
    db = Mock()
    media_buy_repo = Mock()
    media_buy_repo.get_by_id.return_value = SimpleNamespace(
        status="pending_approval",
        principal_id="principal_1",
    )
    preparation = SimpleNamespace(status=ApprovalExecutionStatus.READY, blocking_creative_ids=())
    held = SimpleNamespace(
        status=ApprovalExecutionStatus.WAITING_FOR_CREATIVES,
        blocking_creative_ids=("creative_1\r\nFORGED",),
        error_message=None,
    )

    with (
        app.test_request_context("/"),
        patch.object(workflows_module, "MediaBuyRepository", return_value=media_buy_repo),
        patch.object(
            workflows_module,
            "prepare_media_buy_approval_execution",
            return_value=preparation,
        ),
        patch.object(
            workflows_module,
            "execute_and_finalize_media_buy_approval",
            return_value=held,
        ),
        caplog.at_level("WARNING", logger="src.admin.blueprints.workflows"),
    ):
        _response, status = workflows_module._approve_mapped_media_buy(
            db=db,
            tenant_id="tenant_1",
            step_id="step_1",
            media_buy_id="mb_1",
            request_data={},
            user_email="reviewer@example.com",
        )

    assert status == 200
    message = next(record.getMessage() for record in caplog.records if "creatives not approved" in record.getMessage())
    assert "\r" not in message
    assert "\n" not in message


def test_shared_preparation_uses_repository_execution_source_statuses():
    """The precheck and atomic CAS share one exact eligibility vocabulary."""
    assert APPROVED_EXECUTION_SOURCE_STATUSES == ("pending_approval", "pending_creatives", "draft")

    for status in APPROVED_EXECUTION_SOURCE_STATUSES:
        media_buy_repo = Mock()
        media_buy_repo.get_by_id.return_value = SimpleNamespace(
            status=status,
            principal_id="principal_1",
        )
        media_buy_repo.claim_approved_execution.return_value = True
        outcome = prepare_media_buy_approval_execution(
            media_buys=media_buy_repo,
            media_buy_id="mb_1",
        )

        assert outcome.status is ApprovalExecutionStatus.READY
        # The trigger travels to the CAS, which enforces the source guard IN the UPDATE.
        media_buy_repo.claim_approved_execution.assert_called_once_with("mb_1", trigger=ApprovalTrigger.HUMAN_DECISION)


def test_creative_unblock_may_not_promote_a_buy_awaiting_a_human_decision():
    """A creative approval must never stand in for the human approval it is not.

    ``pending_approval`` is in the canonical execution source set, so a trigger that
    ignored the split would claim and execute a buy no human has approved. The route
    used to encode this as a bare ``{"pending_creatives", "draft"}`` literal with the
    reasoning nowhere; the split now lives beside the canonical set and is derived.

    Graded both ways so this cannot pass by refusing everything.
    """
    for status, trigger, expected in (
        ("pending_approval", ApprovalTrigger.CREATIVE_UNBLOCK, ApprovalExecutionStatus.NOT_EXECUTABLE),
        ("pending_approval", ApprovalTrigger.HUMAN_DECISION, ApprovalExecutionStatus.READY),
        ("pending_creatives", ApprovalTrigger.CREATIVE_UNBLOCK, ApprovalExecutionStatus.READY),
        ("draft", ApprovalTrigger.CREATIVE_UNBLOCK, ApprovalExecutionStatus.READY),
    ):
        media_buy_repo = Mock()
        media_buy_repo.get_by_id.return_value = SimpleNamespace(status=status, principal_id="principal_1")
        media_buy_repo.claim_approved_execution.return_value = True
        outcome = prepare_media_buy_approval_execution(
            media_buys=media_buy_repo,
            media_buy_id="mb_1",
            trigger=trigger,
        )

        assert outcome.status is expected, f"{status} + {trigger} should be {expected}, got {outcome.status}"
        if expected is ApprovalExecutionStatus.NOT_EXECUTABLE:
            media_buy_repo.claim_approved_execution.assert_not_called()


def test_not_executable_is_distinct_from_claim_refused():
    """The two refusals mean different things and callers render them differently.

    NOT_EXECUTABLE says "nothing to execute here, finish the workflow step yourself";
    CLAIM_REFUSED says "this WAS a candidate and another request already claimed it, do
    not proceed". Collapsing them is what made the routes pre-filter in the first place.
    """
    absent = Mock()
    absent.get_by_id.return_value = None
    assert (
        prepare_media_buy_approval_execution(
            media_buys=absent,
            media_buy_id="mb_1",
        ).status
        is ApprovalExecutionStatus.NOT_EXECUTABLE
    )

    lost_the_claim = Mock()
    lost_the_claim.get_by_id.return_value = SimpleNamespace(status="pending_approval", principal_id="principal_1")
    lost_the_claim.claim_approved_execution.return_value = False
    assert (
        prepare_media_buy_approval_execution(
            media_buys=lost_the_claim,
            media_buy_id="mb_1",
        ).status
        is ApprovalExecutionStatus.CLAIM_REFUSED
    )


@pytest.mark.parametrize(
    ("creative_status", "blocks"),
    [
        ("approved", False),
        ("active", True),
        ("processing", True),
        ("pending_review", True),
        ("suspended", True),
        ("rejected", True),
        ("archived", True),
    ],
)
def test_creative_gate_admits_only_approved(creative_status, blocks):
    """``approved`` is the only creative status that clears the gate.

    The gate is ``CreativeAssignmentRepository.unapproved_creative_ids`` — one home,
    rooted at the media buy and tenant-scoped by the repository itself. Three routes
    used to open-code it and the three disagreed, one of them admitting ``active``.

    ``active`` sits on the REFUSAL side and every member of the pinned 3.1
    ``enums/creative-status.json`` is present, so this table is the enum plus the one
    value that is NOT in it. Re-admitting ``active`` reddens the second row.
    """
    from src.core.database.repositories.creative import CreativeAssignmentRepository

    repo = CreativeAssignmentRepository(Mock(), "tenant_1")
    with (
        patch.object(
            CreativeAssignmentRepository,
            "get_by_media_buy",
            return_value=[SimpleNamespace(creative_id="creative_1")],
        ),
        patch(
            "src.core.database.repositories.creative.CreativeRepository.admin_get_by_ids",
            return_value=[SimpleNamespace(creative_id="creative_1", status=creative_status)],
        ),
    ):
        assert repo.unapproved_creative_ids("mb_1") == (["creative_1"] if blocks else [])


def test_creative_gate_does_not_hold_a_buy_with_no_assignments():
    """A buy with no creatives is not waiting on any, so the gate does not hold it.

    This resolves the third disagreement between the old open-codings: one of them
    read an empty assignment list as an unsatisfied gate, holding a buy for creatives
    nobody had assigned.
    """
    from src.core.database.repositories.creative import CreativeAssignmentRepository

    repo = CreativeAssignmentRepository(Mock(), "tenant_1")
    with (
        patch.object(CreativeAssignmentRepository, "get_by_media_buy", return_value=[]),
        patch("src.core.database.repositories.creative.CreativeRepository.admin_get_by_ids") as admin_get_by_ids,
    ):
        assert repo.unapproved_creative_ids("mb_1") == []
    admin_get_by_ids.assert_not_called()


def test_flight_status_uses_canonical_pre_active_and_completed_lifecycle():
    """Creative unblocking preserves the buy's flight-aware status semantics."""
    start = datetime(2026, 8, 1, tzinfo=UTC)
    end = datetime(2026, 8, 31, 23, 59, tzinfo=UTC)

    assert (
        media_buy_status_from_flight_dates(
            start_time=start,
            end_time=end,
            start_date=None,
            end_date=None,
            now=datetime(2026, 7, 1, tzinfo=UTC),
        )
        == "scheduled"
    )
    assert (
        media_buy_status_from_flight_dates(
            start_time=None,
            end_time=None,
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 31),
            now=datetime(2026, 8, 15, tzinfo=UTC),
        )
        == "active"
    )
    assert (
        media_buy_status_from_flight_dates(
            start_time=start,
            end_time=end,
            start_date=None,
            end_date=None,
            now=datetime(2026, 9, 1, tzinfo=UTC),
        )
        == "completed"
    )
    # UTC normalization happens before the canonical date resolver: this end
    # instant is September 1 UTC even though its source-zone date is August 31.
    assert (
        media_buy_status_from_flight_dates(
            start_time=datetime.fromisoformat("2026-08-01T00:00:00-03:00"),
            end_time=datetime.fromisoformat("2026-08-31T23:30:00-03:00"),
            start_date=None,
            end_date=None,
            now=datetime(2026, 9, 1, 1, tzinfo=UTC),
        )
        == "active"
    )
    assert (
        media_buy_status_from_flight_dates(
            start_time=None,
            end_time=None,
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 31),
            now=datetime(2026, 9, 1, tzinfo=UTC),
        )
        == "completed"
    )


def test_shared_execution_success_finalizes_exact_step_once():
    """The L2 orchestration owns adapter tri-state interpretation and finalization."""
    result = SimpleNamespace(media_buy_id="mb_1")
    with (
        patch(
            "src.core.tools.media_buy_create.execute_approved_media_buy",
            return_value=ApprovalResult(outcome=ApprovalOutcome.EXECUTED),
        ) as execute,
        patch(
            "src.core.workflow_finalization.finalize_media_buy_approval_step",
            return_value=ApprovalFinalization(applied=True, result=result),
        ) as finalize,
    ):
        outcome = execute_and_finalize_media_buy_approval(
            tenant_id="tenant_1",
            media_buy_id="mb_1",
            step_id="step_1",
            context={"trace_id": "trace_1"},
        )

    assert outcome.status is ApprovalExecutionStatus.SUCCEEDED
    assert outcome.finalization == ApprovalFinalization(applied=True, result=result)
    execute.assert_called_once_with("mb_1", "tenant_1", execution_claimed=True, approved_by=None, approved_at=None)
    finalize.assert_called_once_with(
        tenant_id="tenant_1",
        step_id="step_1",
        media_buy_id="mb_1",
        succeeded=True,
        error_message=None,
        context={"trace_id": "trace_1"},
        apply_flight_status=False,
    )


def test_shared_execution_persists_flight_aware_status_with_terminal_step():
    """The creative-unblock status correction is part of terminal finalization."""
    result = SimpleNamespace(media_buy_id="mb_1")
    with (
        patch(
            "src.core.tools.media_buy_create.execute_approved_media_buy",
            return_value=ApprovalResult(outcome=ApprovalOutcome.EXECUTED),
        ),
        patch(
            "src.core.workflow_finalization.finalize_latest_media_buy_approval_step",
            return_value=ApprovalFinalization(applied=True, result=result),
        ) as finalize,
    ):
        outcome = execute_and_finalize_media_buy_approval(
            tenant_id="tenant_1",
            media_buy_id="mb_1",
            step_id=None,
            apply_flight_status=True,
        )

    assert outcome.status is ApprovalExecutionStatus.SUCCEEDED
    finalize.assert_called_once_with(
        tenant_id="tenant_1",
        media_buy_id="mb_1",
        succeeded=True,
        error_message=None,
        apply_flight_status=True,
    )


def test_shared_execution_ambiguous_outcome_stays_nonterminal():
    """A post-dispatch unknown never enters either terminal finalizer."""
    with (
        patch(
            "src.core.tools.media_buy_create.execute_approved_media_buy",
            return_value=ApprovalResult(
                outcome=ApprovalOutcome.PENDING_RECONCILIATION,
                error_msg="adapter outcome unknown",
            ),
        ),
        patch("src.core.workflow_finalization.finalize_media_buy_approval_step") as finalize_exact,
        patch("src.core.workflow_finalization.finalize_latest_media_buy_approval_step") as finalize_latest,
    ):
        outcome = execute_and_finalize_media_buy_approval(
            tenant_id="tenant_1",
            media_buy_id="mb_1",
            step_id="step_1",
        )

    assert outcome.status is ApprovalExecutionStatus.PENDING_RECONCILIATION
    finalize_exact.assert_not_called()
    finalize_latest.assert_not_called()


def test_tasks_get_reconciles_approved_step_before_building_terminal_artifact():
    """The durable polling path invokes reconciliation and re-reads the step."""
    from a2a.types import TaskState

    from src.a2a_server.adcp_a2a_server import AdCPRequestHandler
    from tests.utils.a2a_helpers import extract_data_from_artifact

    handler = AdCPRequestHandler()
    identity = PrincipalFactory.make_identity(
        tenant_id="tenant_1",
        principal_id="principal_1",
        tenant={"tenant_id": "tenant_1"},
        protocol="a2a",
    )
    approved_step = SimpleNamespace(
        step_id="step_1",
        context_id="context_1",
        status="approved",
        tool_name="create_media_buy",
        response_data=None,
    )
    completed_payload = {
        "media_buy_id": "mb_1",
        "packages": [],
        "status": "completed",
        "confirmed_at": "2026-01-01T00:00:00Z",
        "revision": 1,
    }
    completed_step = SimpleNamespace(
        step_id="step_1",
        context_id="context_1",
        status="completed",
        tool_name="create_media_buy",
        response_data=completed_payload,
    )
    first_repo = Mock()
    first_repo.get_mappings_for_step.return_value = [
        SimpleNamespace(object_type="media_buy", object_id="mb_1"),
    ]
    owned_results = iter(
        (
            (object(), first_repo, approved_step),
            (object(), Mock(), completed_step),
        )
    )

    @contextmanager
    def owned_step(*_args, **_kwargs):
        yield next(owned_results)

    with (
        patch.object(handler, "_owned_durable_step", side_effect=owned_step),
        patch("src.core.workflow_finalization.reconcile_claimed_media_buy_approval_step") as reconcile,
    ):
        task = handler._durable_task_from_step("task_1", identity)

    reconcile.assert_called_once_with(tenant_id="tenant_1", media_buy_id="mb_1")
    assert task is not None
    assert task.status.state == TaskState.TASK_STATE_COMPLETED
    assert task.artifacts and task.artifacts[0].name == "media_buy_result"
    assert extract_data_from_artifact(task.artifacts[0])["media_buy_id"] == "mb_1"


def _source_string_literals(tree: ast.AST) -> list[str]:
    """Every string literal in ``tree``, as the AUTHOR wrote it semantically.

    Reading literals off the AST rather than scanning source lines is what makes the
    re-inline guards below survive wrapping: these messages are longer than the repo's
    120-char line limit, so a real copy-paste is re-wrapped by the formatter into
    implicit-concatenation pieces that NO single source line contains. A per-line
    substring scan therefore misses exactly the re-inline it exists to catch. Python
    folds adjacent literals into one ``ast.Constant``, so the AST sees the joined
    string the author meant.

    F-strings arrive as ``ast.JoinedStr``; their literal parts are joined with a NUL
    placeholder standing in for each interpolation, so a fragment is matched only when
    it lies wholly within literal text and can never span an interpolated value.
    """
    literals: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            literals.append(node.value)
        elif isinstance(node, ast.JoinedStr):
            literals.append("".join(part.value if isinstance(part, ast.Constant) else "\x00" for part in node.values))
    return literals


def _reinline_offenders(fragment: str) -> list[str]:
    """Files under ``src/`` outside ``approval.py`` whose source carries ``fragment``."""
    src_root = Path(__file__).resolve().parents[2] / "src"
    offenders = []
    for path in sorted(src_root.rglob("*.py")):
        if path.name == "approval.py":
            continue
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:  # pragma: no cover - a syntax error fails elsewhere, loudly
            continue
        if any(fragment in literal for literal in _source_string_literals(tree)):
            offenders.append(str(path.relative_to(src_root)))
    return offenders


class TestWaitingForCreativesMessage:
    """One adapter-agnostic message for the WAITING_FOR_CREATIVES outcome.

    Both admin approve routes branch on the SAME
    ``ApprovalExecutionStatus.WAITING_FOR_CREATIVES`` from
    ``prepare_media_buy_approval_execution``, but each hand-wrote its own flash text. The
    copies drifted on a business fact: one said "in the ad server", the other "in GAM" —
    wrong for every tenant on a non-GAM adapter, and this branch fires for all of them.
    """

    def test_message_is_adapter_agnostic(self):
        from src.admin.utils.approval import waiting_for_creatives_message

        message = waiting_for_creatives_message(3)

        assert message == (
            "Media buy approved! Waiting for 3 creative(s) to be approved before creating in the ad server."
        )
        # The drift that shipped: GAM is one of several registered adapters.
        assert "GAM" not in message

    def test_no_route_reinlines_the_message(self):
        """The wording lives in exactly one place — checked on the DISTINCTIVE clause.

        A route that re-inlines the sentence can drift again without any behavioural test
        noticing, so this guard carries the single-source property.

        Fragment selection is the whole difficulty. Matching the text AFTER "creative(s)"
        — "...before creating in the ad server" — is exactly the clause that HAD drifted
        to "...in GAM", so a re-inline of the historical variant would not match and the
        guard would pass. Matching the salutation "Media buy approved!" is generic enough
        that a re-inline of the distinctive clause without it sails past. So the fragment
        is the count-interpolated middle: distinctive AND drift-stable.
        """
        from src.admin.utils.approval import waiting_for_creatives_message

        message = waiting_for_creatives_message(2)
        fragment = message[message.index("creative(s)") : message.index("before")].strip()
        assert len(fragment) >= 25, (
            f"message shape changed — {fragment!r} is too short to identify the branch. "
            f"Re-derive this guard's fragment rather than letting it match broadly."
        )

        offenders = sorted(_reinline_offenders(fragment))

        assert offenders == [], f"WAITING_FOR_CREATIVES wording re-inlined outside approval.py: {offenders}"


class TestPendingReconciliationMessage:
    """One operator-facing message for the PENDING_RECONCILIATION outcome.

    Both admin approve routes (operations.py, workflows.py) branch on the SAME
    ``ApprovalExecutionStatus.PENDING_RECONCILIATION`` from
    ``prepare_media_buy_approval_execution``, but each hand-inlined the identical sentence —
    the exact shape that let WAITING_FOR_CREATIVES's copies drift (see
    TestWaitingForCreativesMessage above). This mirrors that guard for the sibling outcome.
    """

    def test_no_route_reinlines_the_message(self):
        """The wording lives in exactly one place.

        Derives the fragment FROM the constant rather than hardcoding it, so rewording the
        message re-points the guard automatically instead of silently disarming it. Uses the
        opening clause rather than the closing one: the message is already written as two
        implicitly-concatenated pieces in its own source, so a re-inline is re-wrapped by the
        formatter at whatever column it lands in, and the leading clause is the stable half.
        """
        from src.admin.utils.approval import APPROVED_MEDIA_BUY_PENDING_RECONCILIATION_MESSAGE

        fragment = APPROVED_MEDIA_BUY_PENDING_RECONCILIATION_MESSAGE.split(".", 1)[0].strip()
        assert len(fragment) >= 15, (
            f"message shape changed — {fragment!r} is too short to discriminate; re-derive this guard's fragment"
        )

        offenders = _reinline_offenders(fragment)

        assert offenders == [], f"PENDING_RECONCILIATION wording re-inlined outside approval.py: {offenders}"
