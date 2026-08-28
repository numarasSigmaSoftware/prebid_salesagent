"""
Delivery Webhook Scheduler

Sends daily delivery reports via webhooks for media buys that have configured reporting_webhook.
This runs as a background task and sends reports when GAM data is fresh (after 4 AM PT daily).
"""

import asyncio
import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Any

from adcp import create_mcp_webhook_payload
from adcp.types import GeneratedTaskStatus as AdcpTaskStatus
from adcp.types.generated_poc.media_buy.get_media_buy_delivery_response import (
    NotificationType,
)  # TODO: no stable alias — response-level NotificationType differs from top-level

from src.core.database.database_session import get_db_session, get_independent_db_session
from src.core.database.models import PersistedMediaBuyStatus
from src.core.database.models import PushNotificationConfig as DBPushNotificationConfig
from src.core.database.repositories import MediaBuyRepository, ProductRepository
from src.core.database.repositories.webhook_delivery_log import WebhookDeliveryLogRepository
from src.core.enum_helpers import enum_value
from src.core.logging_config import scrub_control_chars
from src.core.schemas import GetMediaBuyDeliveryRequest, GetMediaBuyDeliveryResponse
from src.core.security.webhook_http import describe_webhook_error
from src.core.tools.media_buy_delivery import _get_media_buy_delivery_impl
from src.core.utils import utc_flight_start
from src.services.protocol_webhook_service import get_protocol_webhook_service
from src.services.webhook_event_identity import webhook_event_key

logger = logging.getLogger(__name__)

# 1 hour because AdCP protocol has frequency options hourly, daily and monthly
# Configurable via env var for testing
SLEEP_INTERVAL_SECONDS = int(os.getenv("DELIVERY_WEBHOOK_INTERVAL") or "3600")
_TERMINAL_MEDIA_BUY_STATUSES = frozenset({"completed", "canceled", "rejected", "failed"})


def _reporting_notification_type(media_buy_status: str, unavailable_count: int) -> NotificationType:
    """Classify scheduled, delayed, and one-shot terminal reporting events."""
    if unavailable_count:
        return NotificationType.delayed
    if media_buy_status in _TERMINAL_MEDIA_BUY_STATUSES:
        return NotificationType.final
    return NotificationType.scheduled


def _reporting_event_identity(
    notification_type: NotificationType,
    media_buy_status: str,
    reporting_period: dict[str, Any],
) -> dict[str, Any]:
    """Return the stable semantic identity for one reporting event."""
    if notification_type == NotificationType.final:
        return {"terminal_status": media_buy_status}
    return {"reporting_period": reporting_period}


def _expected_delay_minutes(session: Any, media_buy: Any) -> int:
    """Return the strictest declared product delay for this media buy."""
    packages = (media_buy.raw_request or {}).get("packages") or []
    product_ids = {
        str(package["product_id"]) for package in packages if isinstance(package, dict) and package.get("product_id")
    }
    if not product_ids:
        return 0
    products = ProductRepository(session, media_buy.tenant_id).list_by_ids(sorted(product_ids))
    return max(
        (int((product.reporting_capabilities or {}).get("expected_delay_minutes") or 0) for product in products),
        default=0,
    )


def _reporting_delivery_request(
    media_buy_id: str,
    *,
    is_terminal: bool,
    start_date: Any,
    end_date: Any,
) -> GetMediaBuyDeliveryRequest:
    """Build the exact scheduled-period or terminal-lifetime delivery query."""
    return GetMediaBuyDeliveryRequest(
        media_buy_ids=[media_buy_id],
        status_filter=None,
        start_date=None if is_terminal else start_date.strftime("%Y-%m-%d"),
        end_date=None if is_terminal else end_date.strftime("%Y-%m-%d"),
        context=None,
    )


def _unavailable_delivery_ids(delivery_response: Any, media_buy_id: str) -> set[str]:
    """Count delayed/failed rows and request-level errors without enum ambiguity."""
    unavailable = {
        str(delivery.media_buy_id)
        for delivery in delivery_response.media_buy_deliveries
        if enum_value(delivery.status) in {"reporting_delayed", "failed"}
    }
    if delivery_response.errors:
        unavailable.add(media_buy_id)
    return unavailable


def _reporting_callback_config(
    media_buy: Any,
    reporting_webhook: dict[str, Any],
    webhook_url: str,
) -> DBPushNotificationConfig:
    """Build the reporting channel from reporting_webhook, never task PNC."""
    authentication = reporting_webhook.get("authentication") or {}
    schemes = authentication.get("schemes") or []
    return DBPushNotificationConfig(
        id=f"temp_{media_buy.media_buy_id}",
        tenant_id=media_buy.tenant_id,
        principal_id=media_buy.principal_id,
        media_buy_id=media_buy.media_buy_id,
        url=webhook_url,
        authentication_type=schemes[0] if schemes else None,
        authentication_token=authentication.get("credentials"),
        is_active=True,
    )


def _supports_reporting_frequency(raw_frequency: str, *, force: bool, media_buy_id: str) -> bool:
    """Reject unsupported scheduled frequencies while preserving forced runs."""
    if force or raw_frequency == "daily":
        return True
    logger.warning(
        "Skipping reporting webhook with frequency '%s' for media buy %s – "
        "only 'daily' frequency is supported for delivery webhooks at this time",
        # raw_frequency comes straight off the buyer's reporting_webhook config,
        # so it is buyer-controlled text like every other value in this call.
        scrub_control_chars(raw_frequency),
        scrub_control_chars(media_buy_id),
    )
    return False


def _reporting_period_dates(
    media_buy: Any,
    *,
    is_terminal: bool,
    force: bool,
    session: Any,
) -> tuple[Any, Any] | None:
    """Resolve a report's campaign-lifetime or prior-day period when ready."""
    today = datetime.now(UTC).date()
    if not is_terminal and media_buy.start_date and media_buy.start_date > today:
        logger.info(
            "Skipping delivery report for media buy %s before its flight starts on %s",
            scrub_control_chars(media_buy.media_buy_id),
            media_buy.start_date,
        )
        return None
    if is_terminal:
        return media_buy.start_date or (today - timedelta(days=1)), media_buy.end_date or today

    start_date = today - timedelta(days=1)
    available_at = utc_flight_start(today) + timedelta(minutes=_expected_delay_minutes(session, media_buy))
    if not force and datetime.now(UTC) < available_at:
        logger.info(
            "Reporting period for media buy %s is not expected to be available until %s",
            scrub_control_chars(media_buy.media_buy_id),
            available_at,
        )
        return None
    return start_date, today


class DeliveryWebhookScheduler:
    """Scheduler for sending delivery reports via webhooks."""

    def __init__(self) -> None:
        self.webhook_service = get_protocol_webhook_service()
        self.is_running = False
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        """Start the scheduler background task."""
        async with self._lock:
            if self.is_running:
                logger.warning("Delivery webhook scheduler is already running")
                return

            self.is_running = True
            self._task = asyncio.create_task(self._run_scheduler())
            logger.info("Delivery webhook scheduler started")

    async def stop(self) -> None:
        """Stop the scheduler background task."""
        async with self._lock:
            if not self.is_running:
                return

            self.is_running = False
            if self._task:
                self._task.cancel()
                try:
                    await self._task
                except asyncio.CancelledError:
                    pass
            logger.info("Delivery webhook scheduler stopped")

    async def _run_scheduler(self) -> None:
        """Main scheduler loop - runs on a fixed hourly cadence.

        Sends immediately on startup (duplicate check prevents re-sending if
        already sent in last 24 hours), then continues on hourly cadence.
        """
        while self.is_running:
            try:
                await self._send_reports()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error in delivery webhook scheduler: %s", scrub_control_chars(describe_webhook_error(e)))
            finally:
                # Wait before next batch
                await asyncio.sleep(SLEEP_INTERVAL_SECONDS)

    async def _send_reports(self) -> None:
        """Send reports for all active media buys with configured webhooks."""
        logger.info("Starting scheduled delivery report webhook batch")

        try:
            with get_db_session() as session:
                # Find all active media buys (cross-tenant scheduler query)
                media_buys = MediaBuyRepository.get_all_by_statuses(
                    session,
                    {
                        PersistedMediaBuyStatus.ACTIVE,
                        PersistedMediaBuyStatus.APPROVED,
                        PersistedMediaBuyStatus.COMPLETED,
                        PersistedMediaBuyStatus.CANCELED,
                        PersistedMediaBuyStatus.REJECTED,
                        PersistedMediaBuyStatus.FAILED,
                    },
                )
                report_jobs: list[tuple[Any, dict[str, Any]]] = []
                for media_buy in media_buys:
                    raw_request = media_buy.raw_request or {}
                    reporting_webhook = raw_request.get("reporting_webhook")
                    if reporting_webhook:
                        session.expunge(media_buy)
                        report_jobs.append((media_buy, reporting_webhook))

            reports_sent = 0
            errors = 0

            for media_buy, reporting_webhook in report_jobs:
                try:
                    # The independent session is closed by the callee before
                    # outbound I/O, so no connection spans the await.
                    with get_independent_db_session() as session:
                        sent = await self._send_report_for_media_buy(media_buy, reporting_webhook, session)
                    if sent:
                        reports_sent += 1
                    else:
                        errors += 1

                except Exception as e:
                    logger.error(
                        f"Error sending report for media buy {scrub_control_chars(media_buy.media_buy_id)}: "
                        f"{scrub_control_chars(describe_webhook_error(e))}",
                    )
                    errors += 1

            logger.info(f"Daily delivery report batch complete: {reports_sent} sent, {errors} errors")

        except Exception as e:
            logger.error(
                "Error in daily delivery report batch: %s",
                scrub_control_chars(describe_webhook_error(e)),
            )

    async def trigger_report_for_media_buy_by_id(self, media_buy_id: str, tenant_id: str) -> bool:
        """Manually trigger a delivery report for a single media buy by ID.

        This method manages its own database session to avoid detached instance errors.

        Args:
            media_buy_id: The media buy ID
            tenant_id: The tenant ID

        Returns:
            bool: True if report was triggered successfully, False otherwise
        """
        try:
            with get_db_session() as session:
                repo = MediaBuyRepository(session, tenant_id)
                media_buy = repo.get_by_id(media_buy_id)

                if not media_buy:
                    logger.warning(f"Cannot trigger report: Media buy {scrub_control_chars(media_buy_id)} not found")
                    return False

                raw_request = media_buy.raw_request or {}
                reporting_webhook = raw_request.get("reporting_webhook")

                if not reporting_webhook:
                    logger.warning(
                        f"Cannot trigger report: No reporting_webhook configured for "
                        f"{scrub_control_chars(media_buy_id)}"
                    )
                    return False

                session.expunge(media_buy)

            # Force sending even if already sent today (for testing). The
            # callee returns the independent connection before it awaits I/O.
            with get_independent_db_session() as session:
                return await self._send_report_for_media_buy(media_buy, reporting_webhook, session, force=True)
        except Exception as e:
            logger.error(
                f"Error manually triggering report for {scrub_control_chars(media_buy_id)}: "
                f"{scrub_control_chars(describe_webhook_error(e))}",
            )
            return False

    async def _send_report_for_media_buy(
        self, media_buy: Any, reporting_webhook: dict, session: Any, force: bool = False
    ) -> bool:
        """Send a delivery report for a single media buy.

        Args:
            media_buy: MediaBuy database model
            reporting_webhook: Webhook configuration dict
            session: Database session
            force: If True, bypass frequency checks and duplicate checks
        """
        media_buy_id_for_log = str(media_buy.media_buy_id)
        try:
            # Determine reporting frequency from AdCP config (hourly, daily, monthly)
            raw_freq = str(reporting_webhook.get("reporting_frequency") or "daily").lower()

            if not _supports_reporting_frequency(raw_freq, force=force, media_buy_id=media_buy_id_for_log):
                return False

            media_buy_status = str(media_buy.status)
            is_terminal = media_buy_status in _TERMINAL_MEDIA_BUY_STATUSES

            # Scheduled daily reports cover the previous UTC day. A terminal
            # report covers the campaign lifetime and has a stable logical key,
            # so a later scheduler run can never produce a second final event.
            reporting_dates = _reporting_period_dates(
                media_buy,
                is_terminal=is_terminal,
                force=force,
                session=session,
            )
            if reporting_dates is None:
                return False
            start_date_obj, end_date_obj = reporting_dates

            # Fetch delivery metrics
            # Create a ResolvedIdentity for the delivery call
            from src.core.resolved_identity import ResolvedIdentity

            identity = ResolvedIdentity(
                principal_id=media_buy.principal_id,
                tenant_id=media_buy.tenant_id,
                tenant={"tenant_id": media_buy.tenant_id},
                protocol="rest",
            )

            # Include active + completed statuses: the scheduler already filters
            # by DB status (active/approved) at query time, so the delivery impl
            # should include ended campaigns (dynamic status=completed) rather
            # than filtering them out and reporting "not found" errors.
            # We exclude "pending_start" (ready) to avoid returning delivery
            # data for future-dated campaigns that haven't started yet.

            req = _reporting_delivery_request(
                media_buy.media_buy_id,
                is_terminal=is_terminal,
                start_date=start_date_obj,
                end_date=end_date_obj,
            )

            delivery_response = _get_media_buy_delivery_impl(req, identity)

            if not isinstance(delivery_response, GetMediaBuyDeliveryResponse):
                logger.warning(
                    f"`Couldn't get media_delivery` for {scrub_control_chars(media_buy.media_buy_id)}. "
                    f"Result is {scrub_control_chars(delivery_response.model_dump())}"
                )
                return False

            unavailable_ids = _unavailable_delivery_ids(delivery_response, str(media_buy.media_buy_id))
            unavailable_count = len(unavailable_ids)
            notification_type = _reporting_notification_type(media_buy_status, unavailable_count)

            # Calculate next_expected_at for daily frequency: start of next day (UTC)
            next_day = datetime.now(UTC).date() + timedelta(days=1)
            next_expected_at = None if notification_type == NotificationType.final else utc_flight_start(next_day)

            reporting_period = delivery_response.reporting_period.model_dump(mode="json")
            identity_payload = _reporting_event_identity(
                notification_type,
                media_buy_status,
                reporting_period,
            )
            logical_event_key = webhook_event_key(
                tenant_id=media_buy.tenant_id,
                principal_id=media_buy.principal_id,
                media_buy_id=media_buy.media_buy_id,
                notification_type=notification_type.value,
                event_payload=identity_payload,
            )
            webhook_url = reporting_webhook.get("url")
            if not webhook_url:
                logger.warning(f"No webhook URL configured for media buy {scrub_control_chars(media_buy.media_buy_id)}")
                return False
            event_repository = WebhookDeliveryLogRepository(session, media_buy.tenant_id)
            event = event_repository.claim_event(
                principal_id=media_buy.principal_id,
                media_buy_id=media_buy.media_buy_id,
                webhook_url=str(webhook_url),
                logical_event_key=logical_event_key,
                task_type="media_buy_delivery",
                notification_type=notification_type.value,
            )
            if not force and event.status == "success":
                session.commit()
                logger.info(
                    "Skipping daily delivery webhook for media buy %s and reporting period %s – already sent",
                    scrub_control_chars(media_buy.media_buy_id),
                    scrub_control_chars(logical_event_key),
                )
                return False
            sequence_number = event.sequence_number

            # Set webhook-specific metadata directly on the response model
            # These fields are defined on the library's GetMediaBuyDeliveryResponse
            delivery_response.notification_type = notification_type
            delivery_response.sequence_number = sequence_number
            delivery_response.next_expected_at = next_expected_at
            delivery_response.partial_data = bool(unavailable_count)
            delivery_response.unavailable_count = unavailable_count

            push_notification_config = _reporting_callback_config(
                media_buy,
                reporting_webhook,
                str(webhook_url),
            )

            # Keep the internal log classifier and wire task type identical so
            # correlation, deduplication, and receiver dispatch describe the same
            # normative media-buy-delivery event.
            metadata = {
                "task_type": "media_buy_delivery",
                "tenant_id": media_buy.tenant_id,
                "principal_id": media_buy.principal_id,
                "media_buy_id": media_buy.media_buy_id,
                "event_id": event.idempotency_key,
                "logical_event_key": logical_event_key,
                "sequence_number": sequence_number,
            }

            media_buy_delivery_payload = create_mcp_webhook_payload(
                task_id=f"delivery:{media_buy.media_buy_id}",
                task_type="media_buy_delivery",
                result=delivery_response.webhook_payload(requested_metrics=reporting_webhook.get("requested_metrics")),
                status=AdcpTaskStatus.completed,
                operation_id=f"delivery:{media_buy.media_buy_id}:{reporting_period['end']}",
                idempotency_key=event.idempotency_key,
                token=reporting_webhook.get("token"),
            )
            payload_dict = media_buy_delivery_payload.model_dump(mode="json", exclude_none=True)
            application_context = (media_buy.raw_request or {}).get("context")
            if application_context is not None:
                payload_dict["context"] = application_context
            payload_dict = event_repository.store_payload_if_absent(event.idempotency_key, payload_dict)
            session.commit()

            # Return the connection before outbound I/O. The surrounding
            # independent-session context will perform idempotent final cleanup.
            session.close()
            sent = await self.webhook_service.send_notification(
                push_notification_config=push_notification_config,
                payload=payload_dict,
                metadata=metadata,
            )
            if not sent:
                raise RuntimeError("Reporting webhook delivery returned false")
            logger.info(f"Sent delivery report webhook for media buy {scrub_control_chars(media_buy_id_for_log)}")
            return True

        except Exception as e:
            logger.error(
                f"Error sending delivery report for media buy {scrub_control_chars(media_buy_id_for_log)}: "
                f"{scrub_control_chars(describe_webhook_error(e))}",
            )
            raise


# Global scheduler instance
_scheduler: DeliveryWebhookScheduler | None = None


def get_delivery_webhook_scheduler() -> DeliveryWebhookScheduler:
    """Get or create global scheduler instance."""
    global _scheduler
    if _scheduler is None:
        _scheduler = DeliveryWebhookScheduler()
    return _scheduler


async def start_delivery_webhook_scheduler():
    """Start the delivery webhook scheduler (called at application startup)."""
    scheduler = get_delivery_webhook_scheduler()
    await scheduler.start()


async def stop_delivery_webhook_scheduler():
    """Stop the delivery webhook scheduler (called at application shutdown)."""
    scheduler = get_delivery_webhook_scheduler()
    await scheduler.stop()
