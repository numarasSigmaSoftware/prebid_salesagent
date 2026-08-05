"""
Protocol-level webhook delivery service for A2A/MCP push notifications.

This service handles protocol-level push notifications (operation status updates)
as distinct from application-level webhooks (scheduled reporting delivery).

Protocol-level webhooks are configured via:
- A2A: MessageSendConfiguration.pushNotificationConfig
- MCP: (future) protocol wrapper extension

Application-level webhooks are configured via:
- AdCP: CreateMediaBuyRequest.reporting_webhook
"""

import asyncio
import json
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from urllib.parse import urlparse, urlunparse
from uuid import uuid4

import requests
from a2a.types import Task, TaskStatusUpdateEvent
from adcp import extract_webhook_result_data, sign_legacy_webhook
from adcp.types import McpWebhookPayload
from google.protobuf.json_format import MessageToDict

from src.core.audit_logger import get_audit_logger
from src.core.database.database_session import get_db_session
from src.core.database.models import DELIVERY_TASK_TYPE, PushNotificationConfig
from src.core.database.repositories.delivery import DeliveryRepository
from src.core.lifecycle import register_shutdown
from src.core.webhook_validator import redact_webhook_url_for_audit, reject_unsafe_outbound_webhook_url

logger = logging.getLogger(__name__)

_LOGGABLE_DELIVERY_TASK_TYPES = frozenset({"delivery_report", DELIVERY_TASK_TYPE})


class DeliveryLogPersistenceError(RuntimeError):
    """Raised when durable webhook retry state cannot be persisted."""


@dataclass(frozen=True)
class _DeliveryLogContext:
    """Invariant fields shared by every state of one delivery attempt."""

    log_id: str
    tenant_id: str
    principal_id: str
    media_buy_id: str
    webhook_url: str
    task_type: str
    idempotency_key: str | None
    sequence_number: int
    notification_type: str | None
    payload_size_bytes: int


def _delivery_log_context(
    *,
    log_id: str,
    url: str,
    task_type: str | None,
    tenant_id: str | None,
    principal_id: str | None,
    media_buy_id: str | None,
    idempotency_key: str | None,
    sequence_number: int,
    notification_type: str | None,
    payload_size_bytes: int,
) -> _DeliveryLogContext | None:
    """Build durable delivery state only for fully identified report webhooks.

    ``webhook_url`` is redacted before it enters this dataclass: the value is
    persisted verbatim to ``WebhookDeliveryLog.webhook_url`` by ``_write_delivery_log``
    and never read back to dial a real request, so this is the single point that
    controls what a buyer-supplied credential-bearing URL leaves in durable storage.
    """
    if task_type not in _LOGGABLE_DELIVERY_TASK_TYPES or not tenant_id or not principal_id or not media_buy_id:
        return None
    return _DeliveryLogContext(
        log_id=log_id,
        tenant_id=tenant_id,
        principal_id=principal_id,
        media_buy_id=media_buy_id,
        webhook_url=redact_webhook_url_for_audit(url),
        task_type=task_type,
        idempotency_key=idempotency_key,
        sequence_number=sequence_number,
        notification_type=notification_type,
        payload_size_bytes=payload_size_bytes,
    )


# FIXME(gh-#1299): behaviour-identical backport of adcp 5.4.0
# ``adcp.to_wire_dict`` + ``_normalize_a2a_task_state_to_v03`` (adcp #602).
# salesagent is pinned to adcp 4.3.0, which predates that public seam.
# Delete this block and call ``adcp.to_wire_dict()`` directly once salesagent
# bumps adcp to the version that ships it.
def _normalize_message_role(message: dict[str, Any]) -> None:
    """Rewrite a2a-sdk 1.0 ``ROLE_*`` to the A2A 0.3 lowercase wire form."""
    role = message.get("role")
    if isinstance(role, str) and role.startswith("ROLE_"):
        message["role"] = role[len("ROLE_") :].lower()


def _normalize_a2a_task_state_to_v03(payload: dict[str, Any]) -> None:
    """Rewrite a2a-sdk 1.0 ``TASK_STATE_*`` / ``ROLE_*`` enums to A2A 0.3
    lowercase wire strings in-place. Buyer receivers parse the 0.3 shape
    (``"state": "completed"``); the 1.0 protobuf JSON emitter produces
    ``"state": "TASK_STATE_COMPLETED"`` by default.
    """
    status = payload.get("status")
    if isinstance(status, dict):
        state = status.get("state")
        if isinstance(state, str) and state.startswith("TASK_STATE_"):
            # Spec uses hyphens for multi-word states (e.g. "auth-required").
            status["state"] = state[len("TASK_STATE_") :].lower().replace("_", "-")
        message = status.get("message")
        if isinstance(message, dict):
            _normalize_message_role(message)
    history = payload.get("history")
    if isinstance(history, list):
        for entry in history:
            if isinstance(entry, dict):
                _normalize_message_role(entry)
    if "role" in payload:
        _normalize_message_role(payload)


def _to_wire_dict(payload: Any) -> dict[str, Any]:
    """Serialize any AdCP webhook payload to a JSON-ready dict.

    Behaviour-identical backport of adcp 5.4.0 ``adcp.to_wire_dict``:

    * a2a ``Task`` / ``TaskStatusUpdateEvent`` (protobuf, a2a-sdk 1.0+) ->
      ``MessageToDict(preserving_proto_field_name=False)`` so JSON keys are
      the A2A wire camelCase (``id``, ``contextId``, ``taskId``), then enum
      values normalized from the 1.0 form (``TASK_STATE_COMPLETED``,
      ``ROLE_AGENT``) to the 0.3-spec lowercase form (``completed``,
      ``agent``).
    * Any Pydantic model (``McpWebhookPayload`` ...) ->
      ``model_dump(mode="json", exclude_none=True)``.
    * ``Mapping`` -> coerced to ``dict`` (legacy hand-built passthrough).
    """
    if isinstance(payload, (Task, TaskStatusUpdateEvent)):
        data: dict[str, Any] = MessageToDict(payload, preserving_proto_field_name=False)
        _normalize_a2a_task_state_to_v03(data)
        return data
    if hasattr(payload, "model_dump"):
        return cast(dict[str, Any], payload.model_dump(mode="json", exclude_none=True))
    if isinstance(payload, Mapping):
        return dict(payload)
    raise TypeError(
        f"Unsupported webhook payload type {type(payload).__name__}: expected "
        "a2a Task / TaskStatusUpdateEvent (protobuf), an AdCP Pydantic model "
        "(e.g. McpWebhookPayload), or a Mapping[str, Any]."
    )


def _normalize_localhost_for_docker(url: str) -> str:
    """Replace localhost host with host.docker.internal while preserving userinfo and port."""
    try:
        parsed = urlparse(url)
        if parsed.hostname and parsed.hostname.lower() == "localhost":
            userinfo = ""
            if parsed.username:
                userinfo = parsed.username
                if parsed.password:
                    userinfo += f":{parsed.password}"
                userinfo += "@"
            port = f":{parsed.port}" if parsed.port else ""
            new_netloc = f"{userinfo}host.docker.internal{port}"
            return urlunparse(parsed._replace(netloc=new_netloc))
    except Exception:
        logger.debug("Docker URL rewrite failed, using original URL", exc_info=True)
    return url


def _safe_delivery_error_message(url: str, *, reason: str) -> str:
    """Build a bounded, non-leaking error_message for a failed webhook delivery.

    NEVER embed ``str(exception)`` or an f-string ``{exception}`` for a failure
    that touched the network/URL layer (``requests.HTTPError``,
    ``requests.RequestException``, or any bare ``Exception`` raised from within
    the same try block): ``requests`` exceptions routinely embed the complete
    request URL in their string form -- ``HTTPError.__str__()`` includes it
    verbatim, userinfo and all; ``ConnectionError``'s message includes
    host+path+query -- which would leak exactly the credentials
    ``redact_webhook_url_for_audit()`` exists to keep out of logs and
    ``WebhookDeliveryLog``. *reason* must be bounded, already-safe text (an
    exception type name, an HTTP status phrase) -- never raw exception text.
    """
    return f"{reason} delivering webhook to {redact_webhook_url_for_audit(url)}"


class ProtocolWebhookService:
    """
    Service for sending protocol-level push notifications to clients.

    Supports authentication schemes:
    - HMAC-SHA256: Signs payload with shared secret
    - Bearer: Sends credentials as Bearer token
    - None: No authentication
    """

    def __init__(self):
        self._session = requests.Session()

    async def send_notification(
        self,
        push_notification_config: PushNotificationConfig,
        payload: Task | TaskStatusUpdateEvent | McpWebhookPayload,
        metadata: dict[str, Any],
    ) -> bool:
        """
        Send a protocol-level push notification to the configured webhook.

        Args:
            push_notification_config: Push notification configuration from protocol layer
            payload: For A2A it can be Task or TaskStatusUpdateEvent types for MCP it wil be McpWebhookPayload.
                Use create_a2a_webhook_payload or create_mcp_webhook_payload from adcp's official python client to get the payload for particular task and status
            metadata: Contains app specific metadata's such as task_type, tenant_id, principal_id

        Returns:
            True if notification sent successfully, False otherwise
        """
        if not push_notification_config or not push_notification_config.url:
            # TODO: @yusuf - Double check logging actually works for Task, TaskStatusUpdateEvent and McpWebhookPayload types
            logger.debug(
                f"No webhook URL configured in the push notification. Here's payload: {payload}, skipping notification"
            )
            return False

        # SSRF gate on the configured URL *before* docker localhost rewrite.
        # Under ADCP_TESTING, localhost/loopback is allowed for capture servers;
        # production uses the full DNS-backed check (HTTPS required).
        rejected, _error_msg = reject_unsafe_outbound_webhook_url(
            push_notification_config.url,
            log=logger,
            kind="Protocol",
            # Same redaction the success path and the durable log use: on this
            # path the hostname can itself be the credential, so the rejection
            # branch must not log it in the clear.
            sanitize=redact_webhook_url_for_audit,
        )
        if rejected:
            return False

        url = _normalize_localhost_for_docker(push_notification_config.url)

        # Prepare headers
        headers = {"Content-Type": "application/json", "User-Agent": "AdCP-Sales-Agent/1.0"}

        # Log sanitized config (exclude sensitive authentication_token). The URL
        # itself may carry userinfo credentials, so it is redacted too — "sanitized"
        # must not leave a different credential in the log line.
        safe_config = {
            "url": (
                redact_webhook_url_for_audit(push_notification_config.url)
                if hasattr(push_notification_config, "url") and push_notification_config.url
                else None
            ),
            "authentication_type": (
                push_notification_config.authentication_type
                if hasattr(push_notification_config, "authentication_type")
                else None
            ),
            # DO NOT log authentication_token - security risk
        }
        logger.info(f"push_notification_config (sanitized): {safe_config}")

        # Serialize payload to dict at the delivery boundary (for HMAC signing
        # and JSON send). Single seam: a2a protobuf -> camelCase + A2A 0.3
        # lowercase enum values; Pydantic -> model_dump; Mapping -> dict.
        payload_dict: dict[str, Any] = _to_wire_dict(payload)

        body_bytes: bytes | None = None

        # Apply authentication based on schemes
        if (
            push_notification_config.authentication_type == "HMAC-SHA256"
            and push_notification_config.authentication_token
        ):
            # The legacy signature covers the exact bytes sent on the wire.
            headers, body_bytes = sign_legacy_webhook(
                push_notification_config.authentication_token,
                payload_dict,
                timestamp=str(int(time.time())),
                headers=headers,
            )

        elif push_notification_config.authentication_type == "Bearer" and push_notification_config.authentication_token:
            # Use Bearer token authentication
            headers["Authorization"] = f"Bearer {push_notification_config.authentication_token}"

        # Send notification with retry logic and logging
        return await self._send_with_retry_and_logging(
            url=url,
            payload=payload_dict,
            headers=headers,
            metadata=metadata,
            body_bytes=body_bytes,
        )

    @staticmethod
    def _write_delivery_log(
        *,
        context: _DeliveryLogContext | None,
        status: str,
        attempt_count: int = 1,
        http_status_code: int | None = None,
        error_message: str | None = None,
        response_time_ms: int | None = None,
        completed_at: datetime | None = None,
        next_retry_at: datetime | None = None,
    ) -> None:
        """Persist one delivery state transition, failing closed on storage errors."""
        if context is None:
            return
        try:
            with get_db_session() as session:
                repo = DeliveryRepository(session, context.tenant_id)
                repo.create_log(
                    log_id=context.log_id,
                    principal_id=context.principal_id,
                    media_buy_id=context.media_buy_id,
                    webhook_url=context.webhook_url,
                    task_type=context.task_type,
                    status=status,
                    idempotency_key=context.idempotency_key,
                    sequence_number=context.sequence_number,
                    notification_type=context.notification_type,
                    attempt_count=attempt_count,
                    http_status_code=http_status_code,
                    error_message=error_message,
                    payload_size_bytes=context.payload_size_bytes,
                    response_time_ms=response_time_ms,
                    completed_at=completed_at,
                    next_retry_at=next_retry_at,
                )
                session.commit()
        except Exception as exc:
            logger.error("Failed to persist webhook delivery state %s: %s", status, exc, exc_info=True)
            raise DeliveryLogPersistenceError(
                f"Could not persist webhook delivery state '{status}' for {context.media_buy_id}"
            ) from exc

    async def _send_with_retry_and_logging(
        self,
        url: str,
        payload: dict[str, Any],
        headers: dict,
        metadata: dict[str, Any],
        body_bytes: bytes | None = None,
        max_attempts: int = 3,
    ) -> bool:
        """Send webhook with exponential backoff retry logic, logging, and audit trail."""
        # Calculate payload size for metrics
        payload_size_bytes = len(body_bytes) if body_bytes is not None else len(json.dumps(payload).encode("utf-8"))

        task_type = metadata["task_type"] if "task_type" in metadata else None
        tenant_id = metadata["tenant_id"] if "tenant_id" in metadata else None
        principal_id = metadata["principal_id"] if "principal_id" in metadata else None
        media_buy_id = metadata["media_buy_id"] if "media_buy_id" in metadata else None

        # TODO: Fix type annotation discrepancy in adcp library - extract_webhook_result_data
        # returns dict at runtime but is typed as AdcpAsyncResponseData | None
        result = cast(dict[str, Any] | None, extract_webhook_result_data(payload))
        # After serialization, payload is always a dict - extract task_id accordingly.
        # A2A Task uses 'id'; A2A TaskStatusUpdateEvent uses camelCase 'taskId' (proto
        # json_name wire contract); MCP uses snake_case 'task_id'.
        task_id = payload.get("id") or payload.get("taskId") or payload.get("task_id") or ""

        # If we are delivering media buy delivery report
        notification_type_from_result = result.get("notification_type") if result is not None else None
        sequence_number_from_result = result.get("sequence_number") if result is not None else None
        notification_type = notification_type_from_result
        sequence_number = sequence_number_from_result if isinstance(sequence_number_from_result, int) else 1
        idempotency_key_from_payload = payload.get("idempotency_key")
        idempotency_key = idempotency_key_from_payload if isinstance(idempotency_key_from_payload, str) else None

        # Create webhook delivery log entry
        log_id = str(uuid4())
        start_time = time.time()
        delivery_log_context = _delivery_log_context(
            log_id=log_id,
            url=url,
            task_type=task_type,
            tenant_id=tenant_id,
            principal_id=principal_id,
            media_buy_id=media_buy_id,
            idempotency_key=idempotency_key,
            sequence_number=sequence_number,
            notification_type=notification_type,
            payload_size_bytes=payload_size_bytes,
        )

        # Reserve the event identity before network I/O. If this write fails,
        # do not send: otherwise a receiver could accept an event whose retry
        # key the sender cannot recover after a restart.
        self._write_delivery_log(
            context=delivery_log_context,
            status="pending",
            attempt_count=0,
        )

        # Log to audit system (start)
        audit_logger = None
        if tenant_id:
            audit_logger = get_audit_logger("webhook", tenant_id)
            audit_logger.log_info(f"Sending {task_type} webhook for task {task_id} (sequence #{sequence_number})")

        # Computed once, not per attempt: the redaction runs urlparse + get_config
        # + an HMAC-SHA256, and `url` does not change across retries. Inlined in
        # the log call below, all of that re-ran on every attempt, eagerly, even
        # when the INFO line was never emitted.
        redacted_url = redact_webhook_url_for_audit(url)

        for attempt in range(max_attempts):
            try:
                logger.info(
                    "Sending webhook for task %s to %s (attempt %s/%s)",
                    task_id,
                    redacted_url,
                    attempt + 1,
                    max_attempts,
                )

                def _post() -> requests.Response:
                    # Never follow redirects: a 302 to metadata/private IPs would
                    # bypass the pre-POST SSRF check (open-redirect SSRF).
                    request_body: dict[str, Any] = {"data": body_bytes} if body_bytes is not None else {"json": payload}
                    return self._session.post(url, headers=headers, timeout=10.0, allow_redirects=False, **request_body)

                response = await asyncio.to_thread(_post)
                response.raise_for_status()

                # Calculate response time
                response_time_ms = int((time.time() - start_time) * 1000)

                # raise_for_status() only raises for 4xx/5xx -- a 3xx we deliberately
                # did not follow (allow_redirects=False, above) passes right through it
                # (and so, in principle, would a stray 1xx -- hence "non-2xx" below, not
                # "redirect": this branch does not assume which non-2xx shape it got).
                # Left unchecked, that response would fall into the success path below
                # and record a delivery that never actually happened. Treated as a
                # permanent failure, same as 4xx: retrying against the same URL would
                # just get the same response again, and for the 3xx case specifically,
                # we've already decided not to trust it (that's the whole point of not
                # following it).
                if not (200 <= response.status_code < 300):
                    error_message = _safe_delivery_error_message(url, reason=f"HTTP {response.status_code} response")
                    logger.error(
                        f"Webhook failed for task {task_id} with non-2xx response {response.status_code} - not retrying"
                    )

                    self._write_delivery_log(
                        context=delivery_log_context,
                        status="failed",
                        attempt_count=attempt + 1,
                        http_status_code=response.status_code,
                        error_message=error_message,
                        response_time_ms=response_time_ms,
                        completed_at=datetime.now(UTC),
                    )

                    if audit_logger:
                        audit_logger.log_warning(
                            f"{task_type} webhook failed with non-2xx response {response.status_code}"
                        )

                    return False

                logger.info("Successfully sent webhook for task %s (status: %s)", task_id, response.status_code)

                self._write_delivery_log(
                    context=delivery_log_context,
                    status="success",
                    attempt_count=attempt + 1,
                    http_status_code=response.status_code,
                    response_time_ms=response_time_ms,
                    completed_at=datetime.now(UTC),
                )

                # Log to audit system (success)
                if audit_logger:
                    audit_logger.log_success(
                        f"{task_type} webhook delivered successfully (sequence #{sequence_number}, "
                        f"{response_time_ms}ms, {payload_size_bytes} bytes)"
                    )

                return True

            except DeliveryLogPersistenceError:
                raise
            except requests.HTTPError as e:
                # `if e.response else None` (not `is not None`) is the bug this replaced:
                # requests.Response.__bool__() returns self.ok, which is False for EVERY
                # 4xx/5xx response -- exactly the only case raise_for_status() ever raises
                # HTTPError for. So the old check made status_code always None, which
                # defeated the "don't retry 4xx" branch below and always persisted a NULL
                # http_status_code, regardless of the real status.
                status_code = e.response.status_code if e.response is not None else None
                response_time_ms = int((time.time() - start_time) * 1000)
                error_message = _safe_delivery_error_message(url, reason=f"HTTP {status_code} error")

                # Don't retry on 4xx errors (client errors - permanent failures)
                if status_code and 400 <= status_code < 500:
                    logger.error(f"Webhook failed for task {task_id} with client error {status_code} - not retrying")

                    self._write_delivery_log(
                        context=delivery_log_context,
                        status="failed",
                        attempt_count=attempt + 1,
                        http_status_code=status_code,
                        error_message=error_message,
                        response_time_ms=response_time_ms,
                        completed_at=datetime.now(UTC),
                    )

                    # Log to audit system (failure)
                    if audit_logger:
                        audit_logger.log_warning(f"{task_type} webhook failed with client error {status_code}")

                    return False

                # Retry on 5xx errors (server errors - transient)
                if attempt < max_attempts - 1:
                    wait_seconds = min(2**attempt, 60)  # Exponential backoff, max 60 seconds
                    logger.warning(
                        f"Webhook failed for task {task_id}: HTTP {status_code}. "
                        f"Retrying in {wait_seconds}s (attempt {attempt + 1}/{max_attempts})"
                    )

                    next_retry = datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=wait_seconds)
                    self._write_delivery_log(
                        context=delivery_log_context,
                        status="retrying",
                        attempt_count=attempt + 1,
                        http_status_code=status_code,
                        error_message=error_message,
                        response_time_ms=response_time_ms,
                        next_retry_at=next_retry,
                    )

                    await asyncio.sleep(wait_seconds)
                else:
                    logger.error(f"Webhook failed for task {task_id} after {max_attempts} attempts: HTTP {status_code}")

                    self._write_delivery_log(
                        context=delivery_log_context,
                        status="failed",
                        attempt_count=max_attempts,
                        http_status_code=status_code,
                        error_message=error_message,
                        response_time_ms=response_time_ms,
                        completed_at=datetime.now(UTC),
                    )

                    # Log to audit system (failure after all retries)
                    if audit_logger:
                        audit_logger.log_warning(f"{task_type} webhook failed after {max_attempts} attempts")

                    return False

            except requests.RequestException as e:
                response_time_ms = int((time.time() - start_time) * 1000)
                error_message = _safe_delivery_error_message(url, reason=type(e).__name__)

                # Network errors - retry
                if attempt < max_attempts - 1:
                    wait_seconds = min(2**attempt, 60)
                    logger.warning(
                        f"Webhook network error for task {task_id}: {type(e).__name__}. "
                        f"Retrying in {wait_seconds}s (attempt {attempt + 1}/{max_attempts})"
                    )
                    next_retry = datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=wait_seconds)
                    self._write_delivery_log(
                        context=delivery_log_context,
                        status="retrying",
                        attempt_count=attempt + 1,
                        error_message=error_message,
                        response_time_ms=response_time_ms,
                        next_retry_at=next_retry,
                    )
                    await asyncio.sleep(wait_seconds)
                else:
                    logger.error(
                        "Webhook failed for task %s after %s attempts: %s", task_id, max_attempts, error_message
                    )

                    self._write_delivery_log(
                        context=delivery_log_context,
                        status="failed",
                        attempt_count=max_attempts,
                        error_message=error_message,
                        response_time_ms=response_time_ms,
                        completed_at=datetime.now(UTC),
                    )

                    # Log to audit system (network failure)
                    if audit_logger:
                        audit_logger.log_warning(f"{task_type} webhook failed with network error: {type(e).__name__}")

                    return False

            except Exception as e:
                error_message = _safe_delivery_error_message(url, reason=f"Unexpected error ({type(e).__name__})")
                logger.error("Unexpected error sending webhook for task %s: %s", task_id, error_message)

                self._write_delivery_log(
                    context=delivery_log_context,
                    status="failed",
                    attempt_count=attempt + 1,
                    error_message=error_message,
                    completed_at=datetime.now(UTC),
                )

                # Audit sink, matching every sibling terminal path in this method. This
                # was the only failure branch that wrote a delivery-log row but no audit
                # entry — and it is the branch for UNEXPECTED errors, so an operator
                # watching the audit stream saw a clean run precisely when something
                # unanticipated broke. The message is already the safe, redacted form.
                if audit_logger:
                    audit_logger.log_warning(f"{task_type} webhook failed: {error_message}")

                return False

        # Should never reach here
        return False

    async def close(self):
        """Close HTTP client."""
        self._session.close()


# Global service instance
_webhook_service: ProtocolWebhookService | None = None


def get_protocol_webhook_service() -> ProtocolWebhookService:
    """Get or create global webhook service instance.

    On first construction, self-registers ``close`` with the shutdown
    registry so the long-lived ``requests.Session`` connection pool is
    released on FastAPI lifespan shutdown — the service owns its own
    lifecycle.
    """
    global _webhook_service
    if _webhook_service is None:
        _webhook_service = ProtocolWebhookService()
        register_shutdown(_webhook_service.close)
    return _webhook_service


def get_webhook_service_or_none() -> ProtocolWebhookService | None:
    """Return the current singleton instance, or None if never constructed.

    Distinct from :func:`get_protocol_webhook_service`: this does NOT trigger
    construction. Use it from shutdown hooks where you only want to close an
    *existing* instance, not create one (and its long-lived ``requests.Session``
    connection pool) just to immediately close it.

    Resolving the singleton through this function call is location-independent:
    it reads the live module global at call time, so callers may import it at
    module top-level without the lazy-import tripwire that a direct
    ``from ... import _webhook_service`` would introduce (a hoisted private
    import binds the initial ``None`` forever).
    """
    return _webhook_service
