"""Wire-level task-correlation tests for creative-completion webhooks."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.admin.blueprints.creatives import _call_webhook_for_creative_status


def _creative_webhook_uow(*, protocol: str, external_task_id: str | None) -> MagicMock:
    """Build the read-only UoW view consumed by the creative webhook helper."""
    request_data = {
        "protocol": protocol,
        "push_notification_config": {
            "url": "https://callbacks.example.test/creative-status",
            "authentication": {"schemes": ["Bearer"], "credentials": "test-credential"},
        },
    }
    if external_task_id is not None:
        request_data["external_task_id"] = external_task_id

    mapping = SimpleNamespace(step_id="step_internal_1", object_type="creative", object_id="creative_1")
    step = SimpleNamespace(
        step_id="step_internal_1",
        tool_name="sync_creatives",
        request_data=request_data,
        context_id="ctx_creative_1",
        context=SimpleNamespace(tenant_id="tenant_creative_1", principal_id="principal_creative_1"),
    )
    creative = SimpleNamespace(
        creative_id="creative_1",
        status="approved",
        data={},
    )

    uow = MagicMock()
    uow.workflows.get_latest_mapping_for_object.return_value = mapping
    uow.workflows.get_step_by_id.return_value = step
    uow.workflows.get_mappings_for_step.return_value = [mapping]
    uow.creatives.admin_get_by_ids.return_value = [creative]

    manager = MagicMock()
    manager.__enter__.return_value = uow
    return manager


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["a2a", "mcp"])
@pytest.mark.parametrize(
    ("external_task_id", "expected_task_id"),
    [
        ("task_buyer_1", "task_buyer_1"),
        (None, "step_internal_1"),
    ],
)
async def test_creative_completion_webhook_uses_buyer_task_id_or_step_fallback(
    protocol: str, external_task_id: str | None, expected_task_id: str
) -> None:
    """Both protocol payloads correlate to the buyer task id, with the legacy step-id fallback."""
    uow_manager = _creative_webhook_uow(protocol=protocol, external_task_id=external_task_id)
    service = MagicMock()
    service.send_notification = AsyncMock()

    with patch("src.admin.blueprints.creatives.AdminCreativeUoW", return_value=uow_manager):
        with patch("src.admin.blueprints.creatives.get_protocol_webhook_service", return_value=service):
            delivered = await _call_webhook_for_creative_status("creative_1", "tenant_creative_1")

    assert delivered is True
    service.send_notification.assert_awaited_once()
    payload = service.send_notification.await_args.kwargs["payload"]
    task_id = getattr(payload, "id", None) or getattr(payload, "task_id", None)
    assert task_id == expected_task_id
