"""Approval finalization retries DB commits without repeating adapter work."""

from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import ANY, Mock, patch

from sqlalchemy.exc import SQLAlchemyError

from src.core.database.repositories.media_buy import APPROVED_EXECUTION_SOURCE_STATUSES
from src.core.database.repositories.workflow import WorkflowRepository
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
        media_buys=_MediaBuyRepo(media_buy=SimpleNamespace(status="active")),
    )
    finalization_media_buy = SimpleNamespace(
        start_time=None,
        end_time=None,
        start_date=date(2099, 1, 1),
        end_date=date(2099, 1, 31),
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
        media_buys=_MediaBuyRepo(media_buy=SimpleNamespace(status="pending_approval")),
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
        media_buys=_MediaBuyRepo(media_buy=SimpleNamespace(status="activation_unknown")),
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


def test_adapter_failure_wrapper_persists_reconcilable_failed_status():
    """The public execution helper leaves durable evidence for reconciliation."""
    from src.core.tools.media_buy_create import _persist_approved_execution_outcome

    media_repo = _FailureMediaBuyRepo()
    uow = _ApprovalUoW(fail_commit=False, media_buys=media_repo)
    execute_once = Mock(return_value=(False, "adapter rejected"))
    execute_with_outcome = _persist_approved_execution_outcome(execute_once)

    with patch("src.core.database.repositories.MediaBuyUoW", return_value=uow):
        result = execute_with_outcome("mb_1", "tenant_1")

    assert result == (False, "adapter rejected")
    execute_once.assert_called_once_with("mb_1", "tenant_1", execution_claimed=False)
    assert media_repo.status_updates == [("mb_1", "failed")]


def test_post_creation_pending_wrapper_does_not_persist_false_failure():
    """An externally created order must never be rewritten as failed."""
    from src.core.tools.media_buy_create import _persist_approved_execution_outcome

    media_repo = _FailureMediaBuyRepo()
    uow = _ApprovalUoW(fail_commit=False, media_buys=media_repo)
    execute_once = Mock(return_value=(None, "local status commit failed"))
    execute_with_outcome = _persist_approved_execution_outcome(execute_once)

    with patch("src.core.database.repositories.MediaBuyUoW", return_value=uow) as uow_type:
        result = execute_with_outcome("mb_1", "tenant_1")

    assert result == (None, "local status commit failed")
    execute_once.assert_called_once_with("mb_1", "tenant_1", execution_claimed=False)
    uow_type.assert_not_called()
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


def test_shared_preparation_applies_creative_gate_before_execution_claim():
    """Every admin entry point uses the same blocking-creative business rule."""
    media_buy_repo = Mock()
    media_buy_repo.get_by_id.return_value = SimpleNamespace(
        status="pending_approval",
        principal_id="principal_1",
    )
    assignments = Mock()
    assignments.get_by_media_buy.return_value = [SimpleNamespace(creative_id="creative_1")]
    creatives = Mock()
    creatives.get_by_ids.return_value = [SimpleNamespace(creative_id="creative_1", status="pending_review")]

    outcome = prepare_media_buy_approval_execution(
        media_buys=media_buy_repo,
        assignments=assignments,
        creatives=creatives,
        media_buy_id="mb_1",
        approved_by="approver@example.com",
    )

    assert outcome.status is ApprovalExecutionStatus.WAITING_FOR_CREATIVES
    assert outcome.blocking_creative_ids == ("creative_1",)
    media_buy_repo.update_status.assert_called_once_with(
        "mb_1",
        "pending_creatives",
        approved_at=ANY,
        approved_by="approver@example.com",
    )
    creatives.get_by_ids.assert_called_once_with(["creative_1"], "principal_1")
    media_buy_repo.claim_approved_execution.assert_not_called()


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
        assignments = Mock()
        assignments.get_by_media_buy.return_value = [SimpleNamespace(creative_id="creative_1")]
        creatives = Mock()
        creatives.get_by_ids.return_value = [SimpleNamespace(creative_id="creative_1", status="approved")]

        outcome = prepare_media_buy_approval_execution(
            media_buys=media_buy_repo,
            assignments=assignments,
            creatives=creatives,
            media_buy_id="mb_1",
            approved_by=None,
        )

        assert outcome.status is ApprovalExecutionStatus.READY
        media_buy_repo.claim_approved_execution.assert_called_once_with("mb_1")


def test_shared_preparation_blocks_buy_without_creative_assignments():
    """No-assignment buys remain pending_creatives and never claim execution."""
    media_buy_repo = Mock()
    media_buy_repo.get_by_id.return_value = SimpleNamespace(
        status="pending_approval",
        principal_id="principal_1",
    )
    assignments = Mock()
    assignments.get_by_media_buy.return_value = []
    creatives = Mock()

    outcome = prepare_media_buy_approval_execution(
        media_buys=media_buy_repo,
        assignments=assignments,
        creatives=creatives,
        media_buy_id="mb_1",
        approved_by="approver@example.com",
    )

    assert outcome.status is ApprovalExecutionStatus.WAITING_FOR_CREATIVES
    assert outcome.blocking_creative_ids == ()
    creatives.get_by_ids.assert_not_called()
    media_buy_repo.update_status.assert_called_once_with(
        "mb_1",
        "pending_creatives",
        approved_at=ANY,
        approved_by="approver@example.com",
    )
    media_buy_repo.claim_approved_execution.assert_not_called()


def test_shared_preparation_treats_missing_creative_rows_as_blocking():
    """A dangling assignment cannot satisfy the canonical creative gate."""
    media_buy_repo = Mock()
    media_buy_repo.get_by_id.return_value = SimpleNamespace(
        status="pending_approval",
        principal_id="principal_1",
    )
    assignments = Mock()
    assignments.get_by_media_buy.return_value = [SimpleNamespace(creative_id="creative_missing")]
    creatives = Mock()
    creatives.get_by_ids.return_value = []

    outcome = prepare_media_buy_approval_execution(
        media_buys=media_buy_repo,
        assignments=assignments,
        creatives=creatives,
        media_buy_id="mb_1",
        approved_by=None,
    )

    assert outcome.status is ApprovalExecutionStatus.WAITING_FOR_CREATIVES
    assert outcome.blocking_creative_ids == ("creative_missing",)
    creatives.get_by_ids.assert_called_once_with(["creative_missing"], "principal_1")
    media_buy_repo.claim_approved_execution.assert_not_called()


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
            return_value=(True, None),
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
    execute.assert_called_once_with("mb_1", "tenant_1", execution_claimed=True)
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
            return_value=(True, None),
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
            return_value=(None, "adapter outcome unknown"),
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
