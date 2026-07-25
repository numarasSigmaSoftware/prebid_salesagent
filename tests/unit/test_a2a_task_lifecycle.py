"""Durable native A2A task callback lifecycle contracts."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from a2a.server.context import ServerCallContext
from a2a.types import Artifact, CancelTaskRequest, Part, Task, TaskNotCancelableError, TaskState, TaskStatus
from google.protobuf import json_format, struct_pb2

from src.core.database.repositories.a2a_task import ClaimedA2ATaskNotification
from src.services.a2a_task_lifecycle import publish_workflow_task_transition, send_native_task_webhooks
from tests.factories.principal import PrincipalFactory


def _value(data: dict) -> struct_pb2.Value:
    value = struct_pb2.Value()
    json_format.ParseDict(data, value)
    return value


def _uow(*, registrations: list | None = None) -> MagicMock:
    uow = MagicMock()
    uow.__enter__.return_value = uow
    uow.__exit__.return_value = None
    uow.push_notification_configs.list_active_for_task.return_value = registrations or []
    return uow


@asynccontextmanager
async def _valid_url_scope(**_kwargs):
    yield


def test_terminal_callback_sends_exact_task_artifacts() -> None:
    task = Task(
        id="task-1",
        context_id="context-1",
        status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED),
        artifacts=[
            Artifact(
                artifact_id="result-1",
                name="result",
                parts=[Part(data=_value({"media_buy_id": "buy-1"}))],
            )
        ],
    )
    registration = SimpleNamespace(
        id="config-1",
        url="https://buyer.example/callback",
        authentication_type=None,
        authentication_token=None,
    )
    service = MagicMock()
    service.send_notification = AsyncMock(return_value=True)
    with (
        patch("src.services.a2a_task_lifecycle.A2ATaskUoW", return_value=_uow(registrations=[registration])),
        patch("src.services.a2a_task_lifecycle.validated_callback_url_scope", _valid_url_scope),
        patch("src.services.a2a_task_lifecycle.require_valid_callback_config_urls"),
        patch("src.services.a2a_task_lifecycle.get_protocol_webhook_service", return_value=service),
    ):
        asyncio.run(
            send_native_task_webhooks(
                task,
                tenant_id="tenant-1",
                principal_id="principal-1",
                status="completed",
            )
        )

    assert service.send_notification.await_args.kwargs["payload"] is task
    callback_task = service.send_notification.await_args.kwargs["payload"]
    assert json_format.MessageToDict(callback_task.artifacts[0]) == json_format.MessageToDict(task.artifacts[0])


def test_one_callback_failure_does_not_suppress_the_next() -> None:
    task = Task(
        id="task-1",
        status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED),
    )
    registrations = [
        SimpleNamespace(
            id="bad", url="https://bad.example/callback", authentication_type=None, authentication_token=None
        ),
        SimpleNamespace(
            id="good",
            url="https://good.example/callback",
            authentication_type=None,
            authentication_token=None,
        ),
    ]
    service = MagicMock()
    service.send_notification = AsyncMock(side_effect=[RuntimeError("token=SECRET"), True])
    with (
        patch("src.services.a2a_task_lifecycle.A2ATaskUoW", return_value=_uow(registrations=registrations)),
        patch("src.services.a2a_task_lifecycle.validated_callback_url_scope", _valid_url_scope),
        patch("src.services.a2a_task_lifecycle.require_valid_callback_config_urls"),
        patch("src.services.a2a_task_lifecycle.get_protocol_webhook_service", return_value=service),
    ):
        asyncio.run(
            send_native_task_webhooks(
                task,
                tenant_id="tenant-1",
                principal_id="principal-1",
                status="completed",
            )
        )

    assert service.send_notification.await_count == 2


def test_committed_workflow_completion_updates_same_native_task() -> None:
    original = Task(
        id="task-1",
        context_id="context-1",
        status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED),
    )
    record = SimpleNamespace(
        principal_id="principal-1",
        status="submitted",
        task_payload=json_format.MessageToDict(original, preserving_proto_field_name=True),
    )
    workflow = SimpleNamespace(response_data={"media_buy_id": "buy-1", "status": "active"})
    uow = _uow()
    uow.tasks.get_by_workflow_step_for_update.return_value = record
    uow.workflows.get_by_step_id.return_value = workflow
    with (
        patch("src.services.a2a_task_lifecycle.A2ATaskUoW", return_value=uow),
        patch("src.services.a2a_task_lifecycle._run_task_delivery", return_value=True) as deliver,
    ):
        publish_workflow_task_transition(
            "workflow-1",
            "completed",
            "tenant-1",
            event_id="workflow:workflow-1:1:completed",
            response_data=workflow.response_data,
        )

    persisted = uow.tasks.upsert.call_args.kwargs
    assert persisted["task_id"] == "task-1"
    assert persisted["workflow_step_id"] == "workflow-1"
    completed = json_format.ParseDict(persisted["task_payload"], Task())
    assert completed.status.state == TaskState.TASK_STATE_COMPLETED
    assert json_format.MessageToDict(completed.artifacts[0].parts[0].data) == workflow.response_data
    deliver.assert_called_once_with(
        completed,
        tenant_id="tenant-1",
        principal_id="principal-1",
        status="completed",
        event_id="workflow:workflow-1:1:completed",
    )


def test_workflow_publisher_never_regresses_canceled_task() -> None:
    canceled = Task(
        id="task-1",
        status=TaskStatus(state=TaskState.TASK_STATE_CANCELED),
    )
    record = SimpleNamespace(
        principal_id="principal-1",
        status="canceled",
        task_payload=json_format.MessageToDict(canceled, preserving_proto_field_name=True),
    )
    uow = _uow()
    uow.tasks.get_by_workflow_step_for_update.return_value = record
    with (
        patch("src.services.a2a_task_lifecycle.A2ATaskUoW", return_value=uow),
        patch("src.services.a2a_task_lifecycle._run_task_delivery") as deliver,
    ):
        publish_workflow_task_transition(
            "workflow-1",
            "completed",
            "tenant-1",
            event_id="workflow:workflow-1:2:completed",
            response_data=None,
        )

    uow.tasks.upsert.assert_not_called()
    deliver.assert_not_called()


@pytest.mark.parametrize("delivery_result", [False, RuntimeError("delivery failed")])
def test_task_notification_failure_releases_claim_for_scheduler_retry(
    delivery_result: bool | RuntimeError,
) -> None:
    from src.services.a2a_task_lifecycle import publish_task_notification

    task = Task(id="task-1", status=TaskStatus(state=TaskState.TASK_STATE_CANCELED))
    claimed = ClaimedA2ATaskNotification(
        task_payload=json_format.MessageToDict(task, preserving_proto_field_name=True),
        principal_id="principal-1",
        status="canceled",
        claim_token="claim-1",
    )
    claim_uow = _uow()
    claim_uow.tasks.claim_notification_publication.return_value = claimed
    release_uow = _uow()
    delivery = {"side_effect": delivery_result} if isinstance(delivery_result, Exception) else {"return_value": False}
    with (
        patch("src.services.a2a_task_lifecycle.A2ATaskUoW", side_effect=[claim_uow, release_uow]),
        patch("src.services.a2a_task_lifecycle._run_task_delivery", **delivery),
    ):
        assert publish_task_notification("event-1", "tenant-1") is False

    release_uow.tasks.release_notification_claim.assert_called_once_with("event-1", claim_token="claim-1")
    release_uow.tasks.mark_notification_published.assert_not_called()


def test_task_notification_success_acknowledges_current_claim() -> None:
    from src.services.a2a_task_lifecycle import publish_task_notification

    task = Task(id="task-1", status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED))
    claimed = ClaimedA2ATaskNotification(
        task_payload=json_format.MessageToDict(task, preserving_proto_field_name=True),
        principal_id="principal-1",
        status="completed",
        claim_token="claim-1",
    )
    claim_uow = _uow()
    claim_uow.tasks.claim_notification_publication.return_value = claimed
    ack_uow = _uow()
    ack_uow.tasks.mark_notification_published.return_value = True
    with (
        patch("src.services.a2a_task_lifecycle.A2ATaskUoW", side_effect=[claim_uow, ack_uow]),
        patch("src.services.a2a_task_lifecycle._run_task_delivery", return_value=True),
    ):
        assert publish_task_notification("event-1", "tenant-1") is True

    ack_uow.tasks.mark_notification_published.assert_called_once_with("event-1", claim_token="claim-1")
    ack_uow.tasks.release_notification_claim.assert_not_called()


def test_task_notification_delivery_does_not_report_success_after_lost_claim() -> None:
    from src.services.a2a_task_lifecycle import publish_task_notification

    task = Task(id="task-1", status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED))
    claimed = ClaimedA2ATaskNotification(
        task_payload=json_format.MessageToDict(task, preserving_proto_field_name=True),
        principal_id="principal-1",
        status="completed",
        claim_token="stale-claim",
    )
    claim_uow = _uow()
    claim_uow.tasks.claim_notification_publication.return_value = claimed
    stale_ack_uow = _uow()
    stale_ack_uow.tasks.mark_notification_published.return_value = False
    with (
        patch("src.services.a2a_task_lifecycle.A2ATaskUoW", side_effect=[claim_uow, stale_ack_uow]),
        patch("src.services.a2a_task_lifecycle._run_task_delivery", return_value=True),
    ):
        assert publish_task_notification("event-1", "tenant-1") is False


def test_committed_rejection_uses_rejected_state_and_exact_error_artifact() -> None:
    original = Task(
        id="task-1",
        status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED),
    )
    record = SimpleNamespace(
        principal_id="principal-1",
        status="submitted",
        task_payload=json_format.MessageToDict(original, preserving_proto_field_name=True),
    )
    rejection = {
        "adcp_error": {"code": "POLICY_VIOLATION", "message": "Inventory policy declined"},
        "errors": [{"code": "POLICY_VIOLATION", "message": "Inventory policy declined"}],
    }
    uow = _uow()
    uow.tasks.get_by_workflow_step_for_update.return_value = record
    uow.workflows.get_by_step_id.return_value = SimpleNamespace(response_data=rejection)
    with (
        patch("src.services.a2a_task_lifecycle.A2ATaskUoW", return_value=uow),
        patch("src.services.a2a_task_lifecycle._run_task_delivery", return_value=True) as deliver,
    ):
        publish_workflow_task_transition(
            "workflow-1",
            "rejected",
            "tenant-1",
            event_id="workflow:workflow-1:1:rejected",
            response_data=rejection,
        )

    persisted = uow.tasks.upsert.call_args.kwargs
    rejected_task = json_format.ParseDict(persisted["task_payload"], Task())
    assert rejected_task.status.state == TaskState.TASK_STATE_REJECTED
    assert json_format.MessageToDict(rejected_task.artifacts[0].parts[0].data) == rejection
    deliver.assert_called_once_with(
        rejected_task,
        tenant_id="tenant-1",
        principal_id="principal-1",
        status="rejected",
        event_id="workflow:workflow-1:1:rejected",
    )


@pytest.mark.asyncio
async def test_running_loop_waits_for_workflow_callback_result() -> None:
    original = Task(
        id="task-1",
        status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED),
    )
    record = SimpleNamespace(
        principal_id="principal-1",
        status="submitted",
        task_payload=json_format.MessageToDict(original, preserving_proto_field_name=True),
    )
    uow = _uow()
    uow.tasks.get_by_workflow_step_for_update.return_value = record
    uow.workflows.get_by_step_id.return_value = SimpleNamespace(response_data={"ok": True})
    with (
        patch("src.services.a2a_task_lifecycle.A2ATaskUoW", return_value=uow),
        patch("src.services.a2a_task_lifecycle._run_task_delivery", return_value=True) as deliver,
    ):
        assert (
            publish_workflow_task_transition(
                "workflow-1",
                "completed",
                "tenant-1",
                event_id="workflow:workflow-1:1:completed",
                response_data={"ok": True},
            )
            is True
        )

    delivered_task = json_format.ParseDict(uow.tasks.upsert.call_args.kwargs["task_payload"], Task())
    deliver.assert_called_once_with(
        delivered_task,
        tenant_id="tenant-1",
        principal_id="principal-1",
        status="completed",
        event_id="workflow:workflow-1:1:completed",
    )


@pytest.mark.parametrize(
    "terminal_state",
    [
        TaskState.TASK_STATE_COMPLETED,
        TaskState.TASK_STATE_FAILED,
        TaskState.TASK_STATE_CANCELED,
    ],
)
@pytest.mark.asyncio
async def test_cancel_never_regresses_terminal_task(terminal_state: int) -> None:
    from src.a2a_server.adcp_a2a_server import AdCPRequestHandler

    task = Task(id="task-1", status=TaskStatus(state=terminal_state))
    record = SimpleNamespace(
        workflow_step_id="workflow-1",
        task_payload=json_format.MessageToDict(task, preserving_proto_field_name=True),
    )
    uow = _uow()
    uow.tasks.get_owned_for_update.return_value = record
    handler = AdCPRequestHandler()
    identity = PrincipalFactory.make_identity(
        tenant_id="tenant-1",
        principal_id="principal-1",
        tenant={"tenant_id": "tenant-1"},
        protocol="a2a",
    )
    handler._get_auth_token = MagicMock(return_value="token")
    handler._resolve_a2a_identity = MagicMock(return_value=identity)
    with (
        patch("src.a2a_server.adcp_a2a_server.A2ATaskUoW", return_value=uow),
        pytest.raises(TaskNotCancelableError),
    ):
        await handler.on_cancel_task(CancelTaskRequest(id="task-1"), ServerCallContext())

    uow.tasks.upsert.assert_not_called()


@pytest.mark.asyncio
async def test_workflow_cancel_publishes_through_terminal_outbox() -> None:
    from src.a2a_server.adcp_a2a_server import AdCPRequestHandler

    task = Task(id="task-1", status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED))

    class ExpiringRecord:
        def __init__(self) -> None:
            self.task_payload = json_format.MessageToDict(task, preserving_proto_field_name=True)
            self.detached = False

        @property
        def workflow_step_id(self) -> str:
            if self.detached:
                raise RuntimeError("detached ORM row accessed after UoW exit")
            return "workflow-1"

    record = ExpiringRecord()
    uow = _uow()
    uow.__exit__.side_effect = lambda *_args: setattr(record, "detached", True)
    uow.tasks.get_owned_for_update.return_value = record
    uow.workflows.transition_status.return_value = SimpleNamespace(step_id="workflow-1")
    handler = AdCPRequestHandler()
    identity = PrincipalFactory.make_identity(
        tenant_id="tenant-1",
        principal_id="principal-1",
        tenant={"tenant_id": "tenant-1"},
        protocol="a2a",
    )
    handler._get_auth_token = MagicMock(return_value="token")
    handler._resolve_a2a_identity = MagicMock(return_value=identity)
    with (
        patch("src.a2a_server.adcp_a2a_server.A2ATaskUoW", return_value=uow),
        patch("src.a2a_server.adcp_a2a_server.publish_workflow_notifications", return_value=True) as publish,
        patch.object(handler, "_send_protocol_webhook", new_callable=AsyncMock) as direct_send,
    ):
        result = await handler.on_cancel_task(CancelTaskRequest(id="task-1"), ServerCallContext())

    assert result is not None
    assert result.status.state == TaskState.TASK_STATE_CANCELED
    publish.assert_called_once_with("workflow-1", "canceled", "tenant-1")
    direct_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_standalone_cancel_persists_event_before_publication() -> None:
    from src.a2a_server.adcp_a2a_server import AdCPRequestHandler

    task = Task(id="task-1", status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED))
    record = SimpleNamespace(
        workflow_step_id=None,
        task_payload=json_format.MessageToDict(task, preserving_proto_field_name=True),
    )
    uow = _uow()
    uow.tasks.get_owned_for_update.return_value = record
    uow.tasks.enqueue_notification.return_value = "a2a-task:task-1:canceled"
    handler = AdCPRequestHandler()
    identity = PrincipalFactory.make_identity(
        tenant_id="tenant-1",
        principal_id="principal-1",
        tenant={"tenant_id": "tenant-1"},
        protocol="a2a",
    )
    handler._get_auth_token = MagicMock(return_value="token")
    handler._resolve_a2a_identity = MagicMock(return_value=identity)
    with (
        patch("src.a2a_server.adcp_a2a_server.A2ATaskUoW", return_value=uow),
        patch("src.a2a_server.adcp_a2a_server.publish_task_notification", return_value=False) as publish,
        patch.object(handler, "_send_protocol_webhook", new_callable=AsyncMock) as direct_send,
    ):
        result = await handler.on_cancel_task(CancelTaskRequest(id="task-1"), ServerCallContext())

    assert result is not None
    event = uow.tasks.enqueue_notification.call_args.kwargs
    assert event["task_id"] == "task-1"
    assert event["status"] == "canceled"
    publish.assert_called_once_with("a2a-task:task-1:canceled", "tenant-1")
    direct_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_loses_when_workflow_already_left_cancelable_state() -> None:
    from src.a2a_server.adcp_a2a_server import AdCPRequestHandler

    task = Task(id="task-1", status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED))
    record = SimpleNamespace(
        workflow_step_id="workflow-1",
        task_payload=json_format.MessageToDict(task, preserving_proto_field_name=True),
    )
    uow = _uow()
    uow.tasks.get_owned_for_update.return_value = record
    uow.workflows.transition_status.return_value = None
    handler = AdCPRequestHandler()
    identity = PrincipalFactory.make_identity(
        tenant_id="tenant-1",
        principal_id="principal-1",
        tenant={"tenant_id": "tenant-1"},
        protocol="a2a",
    )
    handler._get_auth_token = MagicMock(return_value="token")
    handler._resolve_a2a_identity = MagicMock(return_value=identity)
    with (
        patch("src.a2a_server.adcp_a2a_server.A2ATaskUoW", return_value=uow),
        pytest.raises(TaskNotCancelableError),
    ):
        await handler.on_cancel_task(CancelTaskRequest(id="task-1"), ServerCallContext())

    uow.tasks.upsert.assert_not_called()
    uow.workflows.update_status.assert_not_called()
