"""Regression coverage for v0.3 task-not-found JSON-RPC compatibility."""

import json
from unittest.mock import MagicMock

import pytest
from a2a.compat.v0_3 import types as types_v03
from a2a.types import TaskNotFoundError

from src.a2a_server.v03_compat import AdCPJSONRPC03Adapter, AdCPJsonRpcDispatcher


class _MissingTaskHandler:
    async def on_get_task(self, params, context):
        raise TaskNotFoundError(message=f"Task not found: {params.id}", data={"task_id": params.id})

    async def on_cancel_task(self, params, context):
        raise TaskNotFoundError(message=f"Task not found: {params.id}", data={"task_id": params.id})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("request_cls", "method"),
    [(types_v03.GetTaskRequest, "tasks/get"), (types_v03.CancelTaskRequest, "tasks/cancel")],
)
async def test_task_not_found_preserves_v03_error_code_and_data(request_cls, method):
    adapter = AdCPJSONRPC03Adapter(_MissingTaskHandler())
    request = request_cls.model_validate(
        {"jsonrpc": "2.0", "id": 7, "method": method, "params": {"id": "missing-task"}}
    )

    response = await adapter._process_non_streaming_request(7, request, MagicMock())

    payload = json.loads(response.body)
    assert payload["id"] == 7
    assert payload["error"]["code"] == -32001
    assert payload["error"]["data"] == {"task_id": "missing-task"}


def test_dispatcher_uses_application_v03_adapter() -> None:
    dispatcher = AdCPJsonRpcDispatcher(_MissingTaskHandler(), enable_v0_3_compat=True)

    assert isinstance(dispatcher._v03_adapter, AdCPJSONRPC03Adapter)
