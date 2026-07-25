"""Correlation persistence for create_media_buy push callbacks."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from adcp.types import ContextObject

from src.core import context_manager
from src.core.database.repositories.push_notification_config import task_push_config_id
from src.core.tools.media_buy_create import _register_media_buy_push_callback
from tests.unit._push_notification_helpers import make_push_step, session_returning


def test_registration_persists_exact_origin_and_correlation_fields() -> None:
    repository = MagicMock()
    repository.upsert.return_value = (MagicMock(), True)
    uow = MagicMock()
    uow.__enter__.return_value = uow
    uow.__exit__.return_value = None
    uow.push_notification_configs = repository
    callback = {
        "id": "callback-1",
        "url": "https://buyer.example/callback",
        "operation_id": "create-operation-1",
        "token": "callback-token-1",
        "authentication": {
            "schemes": ["Bearer"],
            "credentials": "credential-that-is-at-least-32-chars",
        },
    }
    context = ContextObject(trace_id="trace-1", nested={"source": "buyer"})

    with patch("src.core.database.repositories.MediaBuyUoW", return_value=uow):
        _register_media_buy_push_callback(
            tenant_id="tenant-1",
            principal_id="principal-1",
            media_buy_id="media-buy-1",
            session_id="context-1",
            push_notification_config=callback,
            context=context,
        )

    repository.upsert.assert_called_once_with(
        config_id=task_push_config_id("tenant-1", "principal-1", "context-1", "callback-1"),
        principal_id="principal-1",
        session_id="context-1",
        media_buy_id="media-buy-1",
        url="https://buyer.example/callback",
        operation_id="create-operation-1",
        token="callback-token-1",
        application_context={"trace_id": "trace-1", "nested": {"source": "buyer"}},
        authentication_type="Bearer",
        authentication_token="credential-that-is-at-least-32-chars",
        validation_token=None,
    )


def test_registration_without_callback_is_a_noop() -> None:
    with patch("src.core.database.repositories.MediaBuyUoW") as uow_class:
        _register_media_buy_push_callback(
            tenant_id="tenant-1",
            principal_id="principal-1",
            media_buy_id="media-buy-1",
            session_id="context-1",
            push_notification_config=None,
            context=None,
        )

    uow_class.assert_not_called()


def test_same_status_and_notify_false_never_emit_callbacks() -> None:
    for current_status, new_status, notify in (
        ("completed", "completed", True),
        ("in_progress", "completed", False),
    ):
        step = SimpleNamespace(
            status=current_status,
            completed_at=None,
            response_data={},
            error_message=None,
            transaction_details={},
            comments=[],
            context=SimpleNamespace(tenant_id="tenant-1"),
        )
        result = MagicMock()
        result.first.return_value = step
        session = MagicMock()
        session.scalars.return_value = result
        manager = context_manager.ContextManager()
        manager._session = session
        manager._owns_session = True
        with (
            patch.object(manager, "_send_push_notifications") as send,
            patch.object(manager, "close"),
        ):
            manager.update_workflow_step("step-1", status=new_status, notify=notify)
        send.assert_not_called()


def test_unregistered_task_callback_is_not_emitted() -> None:
    mapping = SimpleNamespace(object_type="media_buy", object_id="mb-1", action="create")
    context = SimpleNamespace(tenant_id="tenant-1", principal_id="principal-1")
    session = session_returning([mapping], context, [])
    service = MagicMock(send_notification=AsyncMock())

    with patch.object(context_manager, "get_protocol_webhook_service", return_value=service):
        context_manager.ContextManager()._send_push_notifications(
            make_push_step("create_media_buy"),
            "completed",
            session,
        )

    service.send_notification.assert_not_called()


def test_a2a_origin_adcp_argument_uses_mcp_envelope_and_status_translation() -> None:
    mapping = SimpleNamespace(object_type="media_buy", object_id="mb-1", action="create")
    context = SimpleNamespace(tenant_id="tenant-1", principal_id="principal-1")
    registered = SimpleNamespace(id="pnc-1")
    session = session_returning([mapping], context, [registered])
    step = make_push_step("create_media_buy")
    step.request_data["protocol"] = "a2a"
    service = MagicMock(send_notification=AsyncMock(return_value=True))

    with patch.object(context_manager, "get_protocol_webhook_service", return_value=service):
        context_manager.ContextManager()._send_push_notifications(
            step,
            "requires_approval",
            session,
        )

    payload = service.send_notification.await_args.kwargs["payload"]
    assert isinstance(payload, dict)
    assert payload["status"] == "input-required"
    assert payload["task_type"] == "create_media_buy"
