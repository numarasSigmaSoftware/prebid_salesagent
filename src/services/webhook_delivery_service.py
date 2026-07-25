"""AdCP reporting-webhook delivery with local security and reliability controls.

This service emits the complete MCP webhook envelope and uses RFC 9421 by
default, retaining explicit legacy HMAC/Bearer selectors. It provides:
- HMAC-SHA256 signature generation with X-ADCP-Signature header
- Circuit breaker pattern (CLOSED/OPEN/HALF_OPEN states) for fault tolerance
- Exponential backoff with jitter for retry logic
- Replay attack prevention with 5-minute timestamp window
- Bounded queues (1000 webhooks per endpoint)
- Support for is_adjusted flag for late-arriving data
- Per-endpoint isolation to prevent cascading failures
"""

import atexit
import json
import logging
import random
import threading
import time
from collections import deque
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any

import requests
from adcp import create_mcp_webhook_payload, get_adcp_spec_version, sign_legacy_webhook

from src.core.bounded_executor import SyncThreadPoolBulkhead
from src.core.database.repositories.push_notification_config import PushNotificationTarget
from src.core.database.repositories.uow import WebhookDeliveryUoW
from src.core.logging_config import scrub_control_chars
from src.core.schemas import GetMediaBuyDeliveryResponse
from src.core.security.webhook_http import (
    BEARER_AUTH_SCHEME,
    HMAC_AUTH_SCHEME,
    WEBHOOK_DELIVERY_DEADLINE_SECONDS,
    WEBHOOK_DELIVERY_MAX_WORKERS,
    UnsafeWebhookTargetError,
    create_pinned_webhook_session,
    describe_webhook_error,
    is_auth_scheme,
    post_webhook_result,
    post_webhook_status,
    redact_webhook_url,
    validate_webhook_auth_selector,
)
from src.core.webhook_validator import WebhookURLValidator
from src.services.protocol_webhook_service import _default_webhook_signature_headers
from src.services.webhook_event_identity import webhook_event_key

logger = logging.getLogger(__name__)
_ORIGINAL_POST_WEBHOOK_STATUS = post_webhook_status

_LEGACY_WEBHOOK_DELIVERY_BULKHEAD = SyncThreadPoolBulkhead(
    max_workers=WEBHOOK_DELIVERY_MAX_WORKERS,
    thread_name_prefix="legacy-webhook-delivery",
)


class CircuitState(Enum):
    """Circuit breaker states."""

    CLOSED = "closed"  # Normal operation
    OPEN = "open"  # Failing, reject requests
    HALF_OPEN = "half_open"  # Testing recovery


class CircuitBreaker:
    """Per-endpoint circuit breaker for fault isolation."""

    def __init__(
        self,
        failure_threshold: int = 5,
        success_threshold: int = 2,
        timeout_seconds: int = 60,
    ):
        """Initialize circuit breaker.

        Args:
            failure_threshold: Consecutive failures before opening circuit
            success_threshold: Consecutive successes in HALF_OPEN to close circuit
            timeout_seconds: Time to wait before moving to HALF_OPEN
        """
        self.failure_threshold = failure_threshold
        self.success_threshold = success_threshold
        self.timeout_seconds = timeout_seconds

        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.success_count = 0
        self.last_failure_time: datetime | None = None
        self._lock = threading.Lock()

    def can_attempt(self) -> bool:
        """Check if request can be attempted.

        Returns:
            True if request should be attempted, False if circuit is OPEN
        """
        with self._lock:
            if self.state == CircuitState.CLOSED:
                return True

            if self.state == CircuitState.OPEN:
                # Check if timeout has elapsed
                if (
                    self.last_failure_time
                    and (datetime.now(UTC) - self.last_failure_time).total_seconds() >= self.timeout_seconds
                ):
                    # Move to HALF_OPEN to test recovery
                    self.state = CircuitState.HALF_OPEN
                    self.success_count = 0
                    logger.info("Circuit breaker moved to HALF_OPEN (testing recovery)")
                    return True
                return False

            # HALF_OPEN state
            return True

    def record_success(self):
        """Record successful request."""
        with self._lock:
            self.failure_count = 0

            if self.state == CircuitState.HALF_OPEN:
                self.success_count += 1
                if self.success_count >= self.success_threshold:
                    self.state = CircuitState.CLOSED
                    logger.info(f"Circuit breaker CLOSED after {self.success_count} successes")
            elif self.state == CircuitState.OPEN:
                # Shouldn't happen but handle gracefully
                self.state = CircuitState.CLOSED
                logger.info("Circuit breaker CLOSED (recovery)")

    def record_failure(self):
        """Record failed request."""
        with self._lock:
            self.failure_count += 1
            self.last_failure_time = datetime.now(UTC)

            if self.state == CircuitState.CLOSED:
                if self.failure_count >= self.failure_threshold:
                    self.state = CircuitState.OPEN
                    logger.warning(f"Circuit breaker OPEN after {self.failure_count} failures")
            elif self.state == CircuitState.HALF_OPEN:
                # Failed during recovery test - go back to OPEN
                self.state = CircuitState.OPEN
                self.failure_count = 0
                logger.warning("Circuit breaker reopened (recovery test failed)")


class WebhookQueue:
    """Bounded queue for webhook delivery per endpoint."""

    def __init__(self, max_size: int = 1000):
        """Initialize webhook queue.

        Args:
            max_size: Maximum number of webhooks in queue
        """
        self.max_size = max_size
        self.queue: deque = deque(maxlen=max_size)
        self._lock = threading.Lock()
        self._dropped_count = 0

    def enqueue(self, webhook_data: dict[str, Any]) -> bool:
        """Add webhook to queue.

        Args:
            webhook_data: Webhook payload and metadata

        Returns:
            True if enqueued, False if queue is full
        """
        with self._lock:
            if len(self.queue) >= self.max_size:
                self._dropped_count += 1
                logger.warning(
                    f"Webhook queue full ({self.max_size}), dropping webhook (total dropped: {self._dropped_count})"
                )
                return False

            self.queue.append(webhook_data)
            return True

    def dequeue(self) -> dict[str, Any] | None:
        """Remove and return oldest webhook from queue.

        Returns:
            Webhook data or None if queue is empty
        """
        with self._lock:
            if self.queue:
                return self.queue.popleft()
            return None


class WebhookDeliveryService:
    """Webhook delivery service with enhanced security and reliability features.

    Preserves the legacy HMAC profile with circuit breakers,
    exponential backoff, replay controls, and SSRF-safe transport hardening.
    """

    def __init__(self) -> None:
        """Initialize enhanced webhook delivery service."""
        self._lock = threading.Lock()  # Protect shared state
        self._circuit_breakers: dict[str, CircuitBreaker] = {}  # Per-endpoint circuit breakers
        self._queues: dict[str, WebhookQueue] = {}  # Per-endpoint bounded queues

        # Register graceful shutdown
        atexit.register(self._shutdown)

        logger.info("✅ WebhookDeliveryService initialized")

    def send_delivery_webhook(
        self,
        media_buy_id: str,
        tenant_id: str,
        principal_id: str,
        reporting_period_start: datetime,
        reporting_period_end: datetime,
        impressions: int,
        spend: float,
        currency: str = "USD",
        status: str = "active",
        clicks: int | None = None,
        ctr: float | None = None,
        by_package: list[dict[str, Any]] | None = None,
        is_final: bool = False,
        is_adjusted: bool = False,
        next_expected_interval_seconds: float | None = None,
    ) -> bool:
        """Send one legacy-profile delivery-reporting webhook securely.

        Args:
            media_buy_id: Media buy identifier
            tenant_id: Tenant identifier
            principal_id: Principal identifier
            reporting_period_start: Start of reporting period
            reporting_period_end: End of reporting period
            impressions: Impressions delivered
            spend: Spend amount
            currency: Currency code (default: USD)
            status: Media buy status
            clicks: Optional click count
            ctr: Optional CTR
            by_package: Optional package-level breakdown
            is_final: Whether this is the final webhook
            is_adjusted: Whether this replaces previous data (late arrivals)
            next_expected_interval_seconds: Seconds until next webhook

        Returns:
            True if webhook sent successfully, False otherwise
        """
        try:
            # Determine notification type per new spec
            if is_final:
                notification_type = "final"
            elif is_adjusted:
                notification_type = "adjusted"  # New in spec
            else:
                notification_type = "scheduled"

            # Calculate next_expected_at if not final
            next_expected_at = None
            if not is_final and next_expected_interval_seconds:
                next_expected_at = (datetime.now(UTC) + timedelta(seconds=next_expected_interval_seconds)).isoformat()

            # Build AdCP compliant payload with new fields
            delivery_payload: dict[str, Any] = {
                "adcp_version": ".".join(get_adcp_spec_version().split(".")[:2]),
                "notification_type": notification_type,
                "reporting_period": {
                    "start": reporting_period_start.isoformat(),
                    "end": reporting_period_end.isoformat(),
                },
                "currency": currency,
                "media_buy_deliveries": [
                    {
                        "media_buy_id": media_buy_id,
                        "status": status,
                        "is_adjusted": is_adjusted,
                        "totals": {
                            "impressions": impressions,
                            "spend": round(spend, 2),
                        },
                        "by_package": by_package or [],
                    }
                ],
            }

            # Add optional fields
            if next_expected_at:
                delivery_payload["next_expected_at"] = next_expected_at

            # Add optional metrics to totals dict
            # We know structure is valid as we just created it above
            media_buy_delivery = delivery_payload["media_buy_deliveries"][0]
            totals: dict[str, Any] = media_buy_delivery["totals"]
            if clicks is not None:
                totals["clicks"] = clicks
            if ctr is not None:
                totals["ctr"] = ctr
            delivery_payload["aggregated_totals"] = {**totals, "media_buy_count": 1}
            delivery_payload = GetMediaBuyDeliveryResponse.model_validate(delivery_payload).webhook_payload()

            logger.info(
                f"📤 Delivery webhook for {scrub_control_chars(media_buy_id)}: "
                f"{impressions:,} imps, ${spend:,.2f} "
                f"[{notification_type}{'|adjusted' if is_adjusted else ''}]"
            )

            event_key = webhook_event_key(
                tenant_id=tenant_id,
                principal_id=principal_id,
                media_buy_id=media_buy_id,
                notification_type=notification_type,
                event_payload=delivery_payload,
            )

            # Send webhook with enhanced security and reliability
            success = self._send_webhook_enhanced(
                tenant_id=tenant_id,
                principal_id=principal_id,
                media_buy_id=media_buy_id,
                delivery_payload=delivery_payload,
                event_key=event_key,
            )

            return success

        except Exception as e:
            logger.error(
                "Failed to send delivery webhook for %s: %s",
                scrub_control_chars(media_buy_id),
                scrub_control_chars(describe_webhook_error(e)),
            )
            return False

    def _generate_hmac_signature(self, payload: dict[str, Any] | bytes, secret: str, timestamp: str) -> str:
        """Generate the SDK-canonical legacy HMAC-SHA256 signature header.

        Args:
            payload: Webhook payload
            secret: Webhook secret (min 32 characters)
            timestamp: Sender timestamp string

        Returns:
            Complete ``sha256=<hex>`` header value
        """
        payload_dict = json.loads(payload) if isinstance(payload, bytes) else payload
        signature_headers, _ = sign_legacy_webhook(secret, payload_dict, timestamp=timestamp)
        return signature_headers["X-AdCP-Signature"]

    def _send_webhook_enhanced(
        self,
        tenant_id: str,
        principal_id: str,
        media_buy_id: str,
        delivery_payload: dict[str, Any],
        event_key: str | None = None,
    ) -> bool:
        """Send webhook with enhanced security and reliability features.

        Args:
            tenant_id: Tenant identifier
            principal_id: Principal identifier
            media_buy_id: Media buy identifier
            delivery_payload: AdCP delivery payload

        Returns:
            True if sent successfully, False otherwise
        """
        try:
            if event_key is None:
                event_key = webhook_event_key(
                    tenant_id=tenant_id,
                    principal_id=principal_id,
                    media_buy_id=media_buy_id,
                    notification_type=str(delivery_payload.get("notification_type", "delivery")),
                    event_payload=delivery_payload,
                )
            # Reporting webhooks are a separate channel from task-status
            # push_notification_config. Resolve only the typed reporting_webhook
            # persisted on this media buy, and claim a random retry-stable wire ID.
            with WebhookDeliveryUoW(tenant_id) as uow:
                assert uow.media_buys is not None
                assert uow.webhook_delivery_logs is not None
                media_buy = uow.media_buys.get_by_id(media_buy_id)
                raw_request = (media_buy.raw_request or {}) if media_buy is not None else {}
                reporting_webhook = raw_request.get("reporting_webhook")
                if not reporting_webhook:
                    logger.debug(
                        "No reporting webhook configured for %s/%s",
                        scrub_control_chars(tenant_id),
                        scrub_control_chars(media_buy_id),
                    )
                    return False
                webhook_url = str(reporting_webhook.get("url") or "")
                if not webhook_url:
                    return False
                authentication = reporting_webhook.get("authentication") or {}
                schemes = authentication.get("schemes") or []
                auth_type = schemes[0] if schemes else None
                credentials = authentication.get("credentials")
                event = uow.webhook_delivery_logs.claim_event(
                    principal_id=principal_id,
                    media_buy_id=media_buy_id,
                    webhook_url=webhook_url,
                    logical_event_key=event_key,
                    task_type="media_buy_delivery",
                    notification_type=str(delivery_payload.get("notification_type") or "scheduled"),
                )
                target = PushNotificationTarget(
                    url=webhook_url,
                    media_buy_id=media_buy_id,
                    operation_id=None,
                    token=reporting_webhook.get("token"),
                    application_context=raw_request.get("context"),
                    sequence_number=event.sequence_number,
                    authentication_type=auth_type,
                    authentication_token=credentials,
                    webhook_secret=None,
                    auth_blocked_at=None,
                )

            target_result = dict(delivery_payload)
            target_result["sequence_number"] = target.sequence_number
            envelope = create_mcp_webhook_payload(
                task_id=f"delivery:{media_buy_id}",
                task_type="media_buy_delivery",
                status="completed",
                operation_id=f"reporting:{event.idempotency_key}",
                token=target.token,
                idempotency_key=event.idempotency_key,
                result=target_result,
            ).model_dump(mode="json", exclude_none=True)
            if target.application_context is not None:
                envelope["context"] = target.application_context
            with WebhookDeliveryUoW(tenant_id, independent=True) as snapshot_uow:
                assert snapshot_uow.webhook_delivery_logs is not None
                envelope = snapshot_uow.webhook_delivery_logs.store_payload_if_absent(
                    event.idempotency_key,
                    envelope,
                )
            if self._queue_and_deliver_target(tenant_id, target, envelope):
                logger.debug("Delivery webhook sent to reporting endpoint")
                return True
            logger.warning("Failed to deliver reporting webhook")
            return False

        except Exception as e:
            logger.error(
                "Error in webhook delivery: %s",
                scrub_control_chars(describe_webhook_error(e)),
            )
            return False

    def _queue_and_deliver_target(
        self,
        tenant_id: str,
        target: PushNotificationTarget,
        delivery_payload: dict[str, Any],
    ) -> bool:
        """Deliver one target within the process-wide legacy worker budget.

        The caller's deadline covers admission and the complete retry operation.
        If it expires, ``SyncThreadPoolBulkhead`` retains the permit until the
        underlying worker really finishes; stuck DNS or socket I/O therefore
        cannot be replaced by an unbounded sequence of simulator threads.
        """
        try:
            return _LEGACY_WEBHOOK_DELIVERY_BULKHEAD.run(
                self._enqueue_and_deliver_target,
                tenant_id,
                target,
                delivery_payload,
                timeout_seconds=WEBHOOK_DELIVERY_DEADLINE_SECONDS,
            )
        except TimeoutError:
            logger.warning(
                "Webhook delivery to %s exceeded the %.1fs total deadline",
                scrub_control_chars(redact_webhook_url(target.url)),
                WEBHOOK_DELIVERY_DEADLINE_SECONDS,
            )
            return False

    def _enqueue_and_deliver_target(
        self,
        tenant_id: str,
        target: PushNotificationTarget,
        delivery_payload: dict[str, Any],
    ) -> bool:
        """Queue and synchronously drain one session-independent target snapshot."""
        if isinstance(target.auth_blocked_at, datetime):
            logger.warning(
                f"⚠️ Auth blocked for {scrub_control_chars(redact_webhook_url(target.url))}, "
                "skipping until credentials reconfigured"
            )
            return False

        endpoint_key = f"{tenant_id}:{target.url}"
        circuit_breaker = self._circuit_breakers.setdefault(endpoint_key, CircuitBreaker())
        queue = self._queues.setdefault(endpoint_key, WebhookQueue(max_size=1000))
        if not circuit_breaker.can_attempt():
            logger.warning(
                f"⚠️ Circuit breaker OPEN for {scrub_control_chars(redact_webhook_url(target.url))}, "
                "skipping webhook delivery"
            )
            return False

        if not queue.enqueue({"config": target, "payload": delivery_payload, "timestamp": datetime.now(UTC)}):
            logger.warning(f"⚠️ Queue full for {scrub_control_chars(redact_webhook_url(target.url))}, webhook dropped")
            return False
        return self._deliver_with_backoff(endpoint_key, circuit_breaker, queue)

    def _build_delivery_request(
        self,
        config: PushNotificationTarget,
        payload: dict[str, Any],
        queued_at: datetime,
    ) -> tuple[dict[str, str], bytes]:
        """Build one exact body/header pair for a queued target."""
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "AdCP-Sales-Agent/2.3 (Enhanced Webhooks)",
        }
        # ``webhook_secret`` predates the protocol selector columns. Preserve
        # those rows as explicit legacy HMAC instead of silently changing their
        # mode to RFC 9421.
        auth_type = config.authentication_type
        credentials = config.authentication_token
        if auth_type is None and config.webhook_secret is not None:
            auth_type = HMAC_AUTH_SCHEME
            credentials = config.webhook_secret
        validate_webhook_auth_selector(auth_type, credentials)

        if is_auth_scheme(auth_type, HMAC_AUTH_SCHEME):
            assert credentials is not None
            timestamp = str(int(queued_at.timestamp()))
            signature_headers, payload_bytes = sign_legacy_webhook(credentials, payload, timestamp=timestamp)
            headers.update(signature_headers)
            return headers, payload_bytes

        payload_bytes = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        headers["X-ADCP-Timestamp"] = queued_at.isoformat()
        if is_auth_scheme(auth_type, BEARER_AUTH_SCHEME):
            assert credentials is not None
            headers["Authorization"] = f"Bearer {credentials}"
        else:
            headers.update(
                _default_webhook_signature_headers(
                    url=config.url,
                    headers=headers,
                    body=payload_bytes,
                )
            )
        return headers, payload_bytes

    @staticmethod
    def _wait_before_retry(attempt: int, max_retries: int) -> None:
        """Apply exponential backoff plus jitter before a retry attempt."""
        if attempt == 0:
            return
        delay = (2**attempt) + random.uniform(0, 1)
        logger.debug(f"Retrying webhook delivery after {delay:.2f}s (attempt {attempt + 1}/{max_retries})")
        time.sleep(delay)

    def _refuse_unsafe_outbound_url(self, url: str, circuit_breaker: CircuitBreaker) -> bool:
        """Return True when the send-time SSRF gate refuses ``url`` (skip delivery).

        Fails closed before any POST without DNS-dependent adapter internals and
        records the failure on the endpoint's circuit breaker. The pinned adapter
        re-validates at connect time on every retry (TOCTOU-proof); this gate is
        the cheap, patchable first line — the seam the harness's
        ``set_url_invalid()``/``set_url_valid()`` drives.
        """
        is_url_safe, ssrf_error = WebhookURLValidator.validate_outbound_webhook_url(url)
        if is_url_safe:
            return False
        logger.warning(
            "Webhook delivery to %s refused by send-time SSRF gate: %s",
            scrub_control_chars(url),
            scrub_control_chars(ssrf_error),
        )
        circuit_breaker.record_failure()
        return True

    def _deliver_with_backoff(
        self,
        endpoint_key: str,
        circuit_breaker: CircuitBreaker,
        queue: WebhookQueue,
    ) -> bool:
        """Deliver webhook with exponential backoff and jitter.

        Args:
            endpoint_key: Unique endpoint identifier
            circuit_breaker: Circuit breaker for this endpoint
            queue: Webhook queue for this endpoint

        Returns:
            True if delivered successfully, False otherwise
        """
        max_retries = 3
        webhook_data = queue.dequeue()
        if not webhook_data:
            return False

        config = webhook_data["config"]
        payload = webhook_data["payload"]
        if self._refuse_unsafe_outbound_url(config.url, circuit_breaker):
            return False
        try:
            headers, payload_bytes = self._build_delivery_request(
                config,
                payload,
                webhook_data["timestamp"],
            )
        except (TypeError, ValueError, RuntimeError) as exc:
            logger.error(
                "Webhook payload or authentication configuration is invalid for %s: %s",
                scrub_control_chars(redact_webhook_url(config.url)),
                scrub_control_chars(str(exc)),
            )
            circuit_breaker.record_failure()
            return False

        # One session belongs to this worker delivery; every retry re-enters the
        # adapter, so DNS is resolved, validated, and pinned again each time.
        with create_pinned_webhook_session() as session:
            for attempt in range(max_retries):
                try:
                    self._wait_before_retry(attempt, max_retries)

                    if post_webhook_status is not _ORIGINAL_POST_WEBHOOK_STATUS:
                        from src.core.security.webhook_http import WebhookHTTPResult

                        http_result = WebhookHTTPResult(
                            status_code=post_webhook_status(
                                session,
                                config.url,
                                body=payload_bytes,
                                headers=headers,
                                timeout=10.0,
                            ),
                            signature_error=False,
                        )
                    else:
                        http_result = post_webhook_result(
                            session,
                            config.url,
                            body=payload_bytes,
                            headers=headers,
                            timeout=10.0,
                        )
                    status_code = http_result.status_code
                    if 200 <= status_code < 300:
                        logger.debug(
                            f"Webhook delivered to {scrub_control_chars(redact_webhook_url(config.url))} "
                            f"(status: {status_code})"
                        )
                        circuit_breaker.record_success()
                        return True

                    # Redirects and most client errors are permanent. AdCP
                    # persistent-webhook 401 is transient and follows the
                    # standard retry schedule.
                    if http_result.signature_error or (300 <= status_code < 500 and status_code != 401):
                        logger.warning(
                            f"Webhook delivery to {scrub_control_chars(redact_webhook_url(config.url))} "
                            f"returned non-retryable status {status_code}"
                        )
                        circuit_breaker.record_failure()
                        return False

                    logger.warning(
                        f"Webhook delivery to {scrub_control_chars(redact_webhook_url(config.url))} "
                        f"returned status {status_code} "
                        f"(attempt: {attempt + 1}/{max_retries})"
                    )

                except UnsafeWebhookTargetError as e:
                    # DNS rebinding/private targets are permanent security failures,
                    # not transient network errors. Never retry the unsafe URL.
                    logger.warning(
                        "Webhook delivery to %s refused: %s",
                        scrub_control_chars(redact_webhook_url(config.url)),
                        scrub_control_chars(describe_webhook_error(e)),
                    )
                    break
                except requests.Timeout:
                    logger.warning(
                        f"Webhook delivery to {scrub_control_chars(redact_webhook_url(config.url))} timed out "
                        f"(attempt: {attempt + 1}/{max_retries})"
                    )
                except requests.RequestException as e:
                    logger.warning(
                        "Webhook delivery to %s failed: %s (attempt: %s/%s)",
                        scrub_control_chars(redact_webhook_url(config.url)),
                        scrub_control_chars(describe_webhook_error(e)),
                        attempt + 1,
                        max_retries,
                    )
                except Exception as e:
                    logger.error(
                        "Unexpected error delivering to %s: %s",
                        scrub_control_chars(redact_webhook_url(config.url)),
                        scrub_control_chars(describe_webhook_error(e)),
                    )
                    break

        # Permanent refusal or all retries failed
        circuit_breaker.record_failure()
        return False

    def reset_sequence(self, media_buy_id: str):
        """Retain the legacy reset hook without discarding durable ordering.

        Delivery sequence state is persisted on the originating callback
        registration. Process-local callers may still invoke this lifecycle
        hook, but a simulator reset must not make an already-used sequence
        number reusable after a restart.
        """
        logger.debug(
            "Ignoring process-local sequence reset for durable media buy %s", scrub_control_chars(media_buy_id)
        )

    def has_open_circuit_breaker(self, tenant_id: str) -> bool:
        """Check if any circuit breaker is OPEN for endpoints belonging to a tenant."""
        for key, cb in self._circuit_breakers.items():
            if key.startswith(f"{tenant_id}:") and cb.state == CircuitState.OPEN:
                return True
        return False

    def get_circuit_breaker_state(self, endpoint_url: str) -> tuple[CircuitState, int]:
        """Get circuit breaker state for an endpoint.

        Args:
            endpoint_url: Webhook endpoint URL

        Returns:
            Tuple of (state, failure_count)
        """
        for key in self._circuit_breakers.keys():
            if endpoint_url in key:
                circuit_breaker = self._circuit_breakers[key]
                return (circuit_breaker.state, circuit_breaker.failure_count)
        return (CircuitState.CLOSED, 0)

    def _shutdown(self):
        """Graceful shutdown handler."""
        try:
            with self._lock:
                # Clean up internal state without logging
                # (logging stream may be closed during interpreter shutdown)
                pass
        except (ValueError, OSError):
            # Logging stream may be closed during interpreter shutdown
            pass


# Global singleton instance
webhook_delivery_service = WebhookDeliveryService()
