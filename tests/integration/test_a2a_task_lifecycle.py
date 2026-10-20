"""The in-memory A2A task registry discards candidates rejected by skill routing.

This is an internal resource-lifetime contract: the JSON-RPC refusal returns no task
ID, so a buyer-facing BDD scenario cannot observe the orphaned registry entry.
Requests use the real HTTP route, with no patched dispatch or fabricated errors.
The unchanged refusal code is also covered by T-PREDISPATCH-a2a-unknown-skill.
"""

import pytest
from a2a.types import Task, TaskState
from google.protobuf import json_format

from tests.harness._base import BareIntegrationEnv, _a2a_message_send_body

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]


def _send_skill(client, skill):
    return client.post(
        "/a2a",
        headers={"A2A-Version": "1.0"},
        json=_a2a_message_send_body(skill, {}),
    )


def test_unknown_skill_discards_only_its_unreturned_task(integration_db):
    from src.app import _request_handler

    initial_ids = set(_request_handler.tasks)
    try:
        with BareIntegrationEnv() as env:
            client = env.get_rest_client()
            # A real validation refusal returns a FAILED Task. It must remain
            # registered even when subsequent requests fail at the protocol layer.
            response = _send_skill(client, "create_media_buy")
            assert response.status_code == 200
            returned_task = json_format.ParseDict(response.json()["result"]["task"], Task())
            assert returned_task.status.state == TaskState.TASK_STATE_FAILED
            assert _request_handler.tasks[returned_task.id] == returned_task
            retained_ids = initial_ids | {returned_task.id}

            for _ in range(2):
                response = _send_skill(client, "nonexistent_skill")
                assert response.status_code == 200
                body = response.json()
                assert body["error"]["code"] == -32601
                assert "result" not in body
                assert set(_request_handler.tasks) == retained_ids
                assert _request_handler.tasks[returned_task.id] == returned_task
    finally:
        # The application handler is process-scoped; remove only this test's entries.
        for task_id in set(_request_handler.tasks) - initial_ids:
            _request_handler.tasks.pop(task_id)
