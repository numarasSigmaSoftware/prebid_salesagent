"""Durable native A2A task transitions and push delivery."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from a2a.types import Artifact, Part, Task, TaskState, TaskStatus
from adcp import create_a2a_webhook_payload
from adcp.types import GeneratedTaskStatus
from google.protobuf import json_format, struct_pb2

from src.core.database.database_session import get_independent_db_session
from src.core.database.models import PushNotificationConfig
from src.core.database.repositories import A2ATaskRepository, A2ATaskUoW
from src.core.logging_config import scrub_control_chars
from src.core.security.webhook_http import describe_webhook_error
from src.core.webhook_validator import require_valid_callback_config_urls, validated_callback_url_scope
from src.services.protocol_webhook_service import get_protocol_webhook_service

logger = logging.getLogger(__name__)

_FINAL_STATUSES = frozenset({"completed", "failed", "canceled", "rejected"})


def _dict_to_value(value: dict[str, Any]) -> struct_pb2.Value:
    result = struct_pb2.Value()
    json_format.ParseDict(value, result)
    return result


async def send_native_task_webhooks(
    task: Task,
    *,
    tenant_id: str,
    principal_id: str,
    status: str,
    event_id: str | None = None,
) -> bool:
    """Fan out one native A2A event, isolating each registered destination."""
    # Callback tasks can inherit the request's ContextVars when scheduled from
    # an after-commit hook. Always take an independent session so the child
    # cannot reuse or leak the parent request's scoped Session.
    with A2ATaskUoW(tenant_id, independent=True) as uow:
        assert uow.push_notification_configs is not None
        rows = uow.push_notification_configs.list_active_for_task(
            principal_id=principal_id,
            task_id=task.id,
        )
        registrations = [(row.id, row.url, row.authentication_type, row.authentication_token) for row in rows]
    if not registrations:
        return True

    if status in _FINAL_STATUSES:
        payload: Any = task
    else:
        try:
            status_enum = GeneratedTaskStatus(status)
        except ValueError:
            status_enum = GeneratedTaskStatus.working
        payload = create_a2a_webhook_payload(
            task_id=task.id,
            status=status_enum,
            context_id=task.context_id or "",
            result={},
        )
    if event_id is not None:
        payload.metadata.update({"idempotency_key": event_id})
    metadata_dict = json_format.MessageToDict(task.metadata) if task.metadata.ByteSize() else {}
    skills = metadata_dict.get("skills_requested") or []
    service = get_protocol_webhook_service()
    all_succeeded = True
    for config_id, url, auth_type, auth_token in registrations:
        try:
            async with validated_callback_url_scope(push_notification_config={"url": url}):
                require_valid_callback_config_urls(push_notification_config={"url": url})
            sent = await service.send_notification(
                push_notification_config=PushNotificationConfig(
                    id=config_id,
                    tenant_id=tenant_id,
                    principal_id=principal_id,
                    session_id=task.id,
                    url=url,
                    authentication_type=auth_type,
                    authentication_token=auth_token,
                    is_active=True,
                ),
                payload=payload,
                metadata={
                    "task_type": skills[0] if skills else "unknown",
                    "tenant_id": tenant_id,
                    "principal_id": principal_id,
                    "event_id": f"{event_id or f'a2a-task:{task.id}:{status}'}:{config_id}",
                },
            )
            all_succeeded = all_succeeded and sent
        except Exception as exc:
            all_succeeded = False
            logger.warning(
                "A2A callback %s for task %s failed: %s",
                config_id,
                task.id,
                scrub_control_chars(describe_webhook_error(exc)),
            )
    return all_succeeded


def _run_task_delivery(
    task: Task,
    *,
    tenant_id: str,
    principal_id: str,
    status: str,
    event_id: str | None = None,
) -> bool:
    async def _send_result() -> bool:
        return await send_native_task_webhooks(
            task,
            tenant_id=tenant_id,
            principal_id=principal_id,
            status="input-required" if status == "requires_approval" else status,
            event_id=event_id,
        )

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_send_result())
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="a2a-task-notification") as executor:
        return executor.submit(asyncio.run, _send_result()).result()


def publish_task_notification(event_id: str, tenant_id: str) -> bool:
    """Claim, deliver, and acknowledge one durable native task event."""
    with A2ATaskUoW(tenant_id, independent=True) as uow:
        assert uow.tasks is not None
        claimed = uow.tasks.claim_notification_publication(event_id)
        if claimed is None:
            return False
        if claimed.already_delivered:
            return True
        assert claimed.claim_token is not None

    try:
        task = json_format.ParseDict(claimed.task_payload, Task())
        succeeded = _run_task_delivery(
            task,
            tenant_id=tenant_id,
            principal_id=claimed.principal_id,
            status=claimed.status,
            event_id=event_id,
        )
    except Exception:
        logger.warning("Native A2A task notification %s failed", event_id, exc_info=True)
        succeeded = False

    with A2ATaskUoW(tenant_id, independent=True) as uow:
        assert uow.tasks is not None
        if succeeded:
            acknowledged = uow.tasks.mark_notification_published(event_id, claim_token=claimed.claim_token)
            if not acknowledged:
                logger.warning("Lost native A2A notification claim before ACK for %s", event_id)
            return acknowledged
        else:
            released = uow.tasks.release_notification_claim(event_id, claim_token=claimed.claim_token)
            if not released:
                logger.warning("Lost native A2A notification claim before release for %s", event_id)
    return False


def publish_pending_task_notifications() -> int:
    """Drain all scheduler-visible native task notification events."""
    with get_independent_db_session() as session:
        pending = A2ATaskRepository.list_pending_notifications(session)
    published = 0
    for item in pending:
        try:
            if publish_task_notification(item.event_id, item.tenant_id):
                published += 1
        except Exception:
            logger.warning(
                "Failed to publish pending A2A task notification for event_id=%s",
                item.event_id,
                exc_info=True,
            )
    return published


def publish_workflow_task_transition(
    workflow_step_id: str,
    status: str,
    tenant_id: str,
    *,
    event_id: str,
    response_data: dict | None,
) -> bool:
    """Update and publish the native task correlated with a committed workflow."""
    with A2ATaskUoW(tenant_id, independent=True) as uow:
        assert uow.tasks is not None
        record = uow.tasks.get_by_workflow_step_for_update(workflow_step_id)
        if record is None:
            return True
        # Terminal native Task states are monotonic. This lock/guard pairs with
        # tasks/cancel's row lock so a stale admin or workflow publisher cannot
        # resurrect a canceled/completed/rejected task or emit a second terminal
        # callback after a concurrent transition wins.
        previous_status = record.status
        if previous_status in _FINAL_STATUSES and previous_status != status:
            return False
        principal_id = record.principal_id
        task = json_format.ParseDict(record.task_payload, Task())
        if status == "completed":
            task_state = TaskState.TASK_STATE_COMPLETED
        elif status == "failed":
            task_state = TaskState.TASK_STATE_FAILED
        elif status == "rejected":
            task_state = TaskState.TASK_STATE_REJECTED
        elif status == "canceled":
            task_state = TaskState.TASK_STATE_CANCELED
        else:
            task_state = TaskState.TASK_STATE_INPUT_REQUIRED
        task.status.CopyFrom(TaskStatus(state=task_state))

        assert uow.workflows is not None
        if status in _FINAL_STATUSES:
            del task.artifacts[:]
            task.artifacts.append(
                Artifact(
                    artifact_id="workflow_result_1",
                    name="processing_error" if status in {"failed", "rejected"} else "result",
                    parts=[Part(data=_dict_to_value(response_data or {}))],
                )
            )
        task_payload = json_format.MessageToDict(task, preserving_proto_field_name=True)
        if record.status not in _FINAL_STATUSES:
            uow.tasks.upsert(
                task_id=task.id,
                principal_id=principal_id,
                context_id=task.context_id or None,
                workflow_step_id=workflow_step_id,
                status=status,
                task_payload=task_payload,
            )
    return _run_task_delivery(
        task,
        tenant_id=tenant_id,
        principal_id=principal_id,
        status=status,
        event_id=event_id,
    )
