"""Approval finalization retries DB commits without repeating adapter work."""

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import ANY, Mock, patch

from flask import Flask
from sqlalchemy.exc import SQLAlchemyError

from src.core.database.repositories.workflow import WorkflowRepository
from src.core.workflow_finalization import (
    finalize_media_buy_approval_step,
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
    def __init__(self) -> None:
        super().__init__()
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
    """tasks/get recovery terminalizes an active buy using stored state only."""
    claimed_step = SimpleNamespace(
        step_id="step_1",
        request_data={"context": {"trace_id": "trace_1"}},
    )
    lookup_uow = _ApprovalUoW(
        fail_commit=False,
        workflows=_WorkflowRepo(existing=claimed_step),
        media_buys=_MediaBuyRepo(media_buy=SimpleNamespace(status="active")),
    )
    finalization_uow = _ApprovalUoW(fail_commit=False)
    uows = iter((lookup_uow, finalization_uow))

    with patch("src.core.workflow_finalization.ApprovalUoW", side_effect=lambda _tenant_id: next(uows)):
        result = reconcile_claimed_media_buy_approval_step(
            tenant_id="tenant_1",
            media_buy_id="mb_1",
        )

    assert result.applied is True
    assert result.result is not None
    assert result.result.media_buy_id == "mb_1"
    assert result.result.context is not None
    assert result.result.context.trace_id == "trace_1"


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


def test_workflow_success_preserves_active_media_buy_status():
    """Workflow metadata publication must not undo the execution CAS."""
    from src.admin.blueprints.workflows import _complete_executed_media_buy

    app = Flask(__name__)
    app.secret_key = "test-secret"
    db = Mock()
    media_buy_repo = Mock()
    media_buy_repo.get_by_id.return_value = SimpleNamespace(status="active")
    media_buy_repo.update_status.return_value = SimpleNamespace(status="active")

    with (
        app.test_request_context(),
        patch(
            "src.admin.blueprints.workflows.finalize_media_buy_approval_step",
            return_value=SimpleNamespace(applied=True),
        ),
    ):
        response, status = _complete_executed_media_buy(
            db=db,
            media_buy_repo=media_buy_repo,
            tenant_id="tenant_1",
            step_id="step_1",
            media_buy_id="mb_1",
            request_data={},
            user_email="approver@example.com",
        )

    assert status == 200
    assert response.get_json() == {"success": True}
    media_buy_repo.update_status.assert_called_once_with(
        "mb_1",
        "active",
        approved_at=ANY,
        approved_by="approver@example.com",
    )
    db.commit.assert_called_once_with()


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
