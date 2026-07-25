"""Occurrence-level workflow notification outbox contracts."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest

from src.core.database.models import A2ATaskNotificationEvent, WorkflowNotificationEvent
from src.core.database.repositories.a2a_task import A2ATaskRepository
from src.core.database.repositories.workflow import WorkflowRepository


def _step(status: str = "pending") -> SimpleNamespace:
    return SimpleNamespace(
        step_id="step-1",
        context_id="context-1",
        status=status,
        response_data=None,
        error_message=None,
        completed_at=None,
        processing_started_at=None,
        notification_sequence=0,
    )


def test_notifiable_transition_enqueues_before_repository_flush_returns() -> None:
    session = MagicMock()
    repository = WorkflowRepository(session, "tenant-1")
    step = _step()
    with patch.object(repository, "get_by_step_id_for_update", return_value=step):
        repository.update_status("step-1", status="input-required", response_data={"reason": "creative"})

    event = session.add.call_args.args[0]
    assert isinstance(event, WorkflowNotificationEvent)
    assert event.event_id == "workflow:step-1:1:input-required"
    assert event.tenant_id == "tenant-1"
    assert event.response_data == {"reason": "creative"}
    session.add.assert_called_once_with(event)
    assert session.flush.call_args_list == [call(), call()]


def test_status_cycle_creates_distinct_occurrence_ids() -> None:
    session = MagicMock()
    repository = WorkflowRepository(session, "tenant-1")
    step = _step()
    with patch.object(repository, "get_by_step_id_for_update", return_value=step):
        repository.update_status("step-1", status="input-required", response_data={"generation": 1})
        repository.update_status("step-1", status="processing")
        repository.update_status("step-1", status="input-required", response_data={"generation": 2})

    events = [call.args[0] for call in session.add.call_args_list]
    assert [event.event_id for event in events] == [
        "workflow:step-1:1:input-required",
        "workflow:step-1:2:input-required",
    ]
    assert [event.response_data for event in events] == [{"generation": 1}, {"generation": 2}]


def test_stale_notification_claim_cannot_acknowledge_event() -> None:
    session = MagicMock()
    query_result = MagicMock()
    query_result.first.return_value = SimpleNamespace(claim_token="new-owner")
    session.scalars.return_value = query_result
    repository = WorkflowRepository(session, "tenant-1")

    assert repository.mark_notifications_published("event-1", claim_token="stale-owner") is False
    session.flush.assert_not_called()


def test_later_workflow_occurrence_cannot_overtake_older_pending_event() -> None:
    session = MagicMock()
    query_result = MagicMock()
    query_result.first.return_value = SimpleNamespace(
        event_id="workflow:step-1:1:input-required",
        step_id="step-1",
        sequence=1,
        status="input-required",
        claimed_at=None,
    )
    no_cancellation = MagicMock()
    no_cancellation.first.return_value = None
    session.scalars.side_effect = [query_result, no_cancellation]
    repository = WorkflowRepository(session, "tenant-1")

    assert (
        repository.claim_notification_publication(
            "step-1",
            "completed",
            event_id="workflow:step-1:2:completed",
        )
        is None
    )
    session.flush.assert_not_called()


@pytest.mark.parametrize("claimed_at", [None, datetime(2020, 1, 1, tzinfo=UTC)])
def test_retryable_older_event_is_superseded_after_later_cancellation(
    claimed_at: datetime | None,
) -> None:
    session = MagicMock()
    older = SimpleNamespace(
        event_id="workflow:step-1:1:input-required",
        step_id="step-1",
        sequence=1,
        status="input-required",
        claimed_at=claimed_at,
        claim_token="failed-or-expired",
        delivered_at=None,
        superseded_at=None,
    )

    def result(value):
        query_result = MagicMock()
        query_result.first.return_value = value
        return query_result

    session.scalars.side_effect = [result(older), result("workflow:step-1:2:canceled")]
    repository = WorkflowRepository(session, "tenant-1")

    assert repository.claim_notification_publication(
        "step-1",
        "input-required",
        event_id=older.event_id,
    ) == (
        older.event_id,
        None,
        None,
    )
    assert older.delivered_at is not None
    assert older.superseded_at == older.delivered_at
    assert older.claimed_at is None
    assert older.claim_token is None


def test_public_publisher_commits_claim_time_supersession() -> None:
    from src.core.context_manager import publish_workflow_notifications

    session = MagicMock()
    session.__enter__.return_value = session
    session.__exit__.return_value = None
    repository = MagicMock()
    repository.claim_notification_publication.return_value = (
        "workflow:step-1:1:input-required",
        None,
        None,
    )
    with (
        patch("src.core.context_manager.get_independent_db_session", return_value=session),
        patch("src.core.context_manager.WorkflowRepository", return_value=repository),
        patch("src.services.a2a_task_lifecycle.publish_workflow_task_transition") as publish_native,
    ):
        assert publish_workflow_notifications("step-1", "input-required", "tenant-1") is False

    session.commit.assert_called_once_with()
    publish_native.assert_not_called()


def test_cancellation_supersedes_older_pending_occurrences_before_enqueue() -> None:
    session = MagicMock()
    unclaimed = SimpleNamespace(
        delivered_at=None,
        superseded_at=None,
        claimed_at=None,
        claim_token=None,
    )
    in_flight = SimpleNamespace(
        delivered_at=None,
        superseded_at=None,
        claimed_at=object(),
        claim_token="active-claim",
    )
    pending_result = MagicMock()
    pending_result.all.return_value = [unclaimed, in_flight]
    session.scalars.return_value = pending_result
    repository = WorkflowRepository(session, "tenant-1")
    step = _step("input-required")
    step.notification_sequence = 1
    with patch.object(repository, "get_by_step_id_for_update", return_value=step):
        repository.transition_status(
            "step-1",
            status="canceled",
            allowed_from={"input-required"},
        )

    assert unclaimed.superseded_at is not None
    assert unclaimed.delivered_at == unclaimed.superseded_at
    assert in_flight.superseded_at is None
    assert in_flight.delivered_at is None
    assert in_flight.claim_token == "active-claim"
    canceled_event = session.add.call_args.args[0]
    assert canceled_event.event_id == "workflow:step-1:2:canceled"


def test_standalone_task_status_cycle_uses_generation_backed_event_ids() -> None:
    session = MagicMock()
    task = SimpleNamespace(
        notification_sequence=0,
        last_notification_status=None,
        last_notification_event_id=None,
    )

    def result(value):
        query_result = MagicMock()
        query_result.first.return_value = value
        return query_result

    session.scalars.side_effect = [result(task), result(None), result(task), result(None)]
    repository = A2ATaskRepository(session, "tenant-1")

    first = repository.enqueue_notification(
        task_id="task-1",
        principal_id="principal-1",
        status="input-required",
        task_payload={"generation": 1},
    )
    second = repository.enqueue_notification(
        task_id="task-1",
        principal_id="principal-1",
        status="input-required",
        task_payload={"generation": 2},
    )

    assert first == "a2a-task:task-1:1:input-required"
    assert second == "a2a-task:task-1:2:input-required"
    events = [call.args[0] for call in session.add.call_args_list]
    assert all(isinstance(event, A2ATaskNotificationEvent) for event in events)
