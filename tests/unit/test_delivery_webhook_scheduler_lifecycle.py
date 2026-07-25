"""Scheduled reporting has exact period, delayed, and terminal identities."""

from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

from adcp.types import MediaBuyDeliveryStatus
from adcp.types.generated_poc.media_buy.get_media_buy_delivery_response import NotificationType

from src.core.database.repositories.webhook_delivery_log import WebhookDeliveryLogRepository
from src.services.delivery_webhook_scheduler import (
    _reporting_delivery_request,
    _reporting_event_identity,
    _reporting_notification_type,
    _unavailable_delivery_ids,
)


def test_reporting_notification_lifecycle() -> None:
    assert _reporting_notification_type("active", 0) == NotificationType.scheduled
    assert _reporting_notification_type("active", 2) == NotificationType.delayed
    assert _reporting_notification_type("completed", 0) == NotificationType.final
    assert _reporting_notification_type("canceled", 0) == NotificationType.final
    assert _reporting_notification_type("rejected", 0) == NotificationType.final


def test_scheduled_and_delayed_events_are_exact_period_scoped() -> None:
    first_period = {"start": "2026-07-23T00:00:00Z", "end": "2026-07-24T00:00:00Z"}
    next_period = {"start": "2026-07-24T00:00:00Z", "end": "2026-07-25T00:00:00Z"}

    assert _reporting_event_identity(NotificationType.scheduled, "active", first_period) != (
        _reporting_event_identity(NotificationType.scheduled, "active", next_period)
    )
    assert _reporting_event_identity(NotificationType.delayed, "active", first_period) == {
        "reporting_period": first_period
    }


def test_final_event_identity_is_stable_when_scheduler_time_drifts() -> None:
    first_observation = {"start": "2026-07-01T00:00:00Z", "end": "2026-07-24T00:01:00Z"}
    later_observation = {"start": "2026-07-01T00:00:00Z", "end": "2026-07-24T01:01:00Z"}

    assert _reporting_event_identity(NotificationType.final, "completed", first_observation) == (
        _reporting_event_identity(NotificationType.final, "completed", later_observation)
    )


def test_terminal_query_is_lifetime_while_scheduled_query_is_exact_period() -> None:
    scheduled = _reporting_delivery_request(
        "buy-1",
        is_terminal=False,
        start_date=date(2026, 7, 24),
        end_date=date(2026, 7, 25),
    )
    terminal = _reporting_delivery_request(
        "buy-1",
        is_terminal=True,
        start_date=date(2026, 7, 24),
        end_date=date(2026, 7, 25),
    )

    assert scheduled.start_date == "2026-07-24"
    assert scheduled.end_date == "2026-07-25"
    assert terminal.start_date is None
    assert terminal.end_date is None
    assert terminal.status_filter is None


def test_reporting_event_payload_is_first_copy_wins() -> None:
    """An ambiguous retry reuses the exact first payload bytes/operation id."""
    event = SimpleNamespace(event_payload=None)
    session = MagicMock()
    session.scalars.return_value.one.return_value = event
    repository = WebhookDeliveryLogRepository(session, "tenant-1")
    first = {
        "idempotency_key": "event-1",
        "operation_id": "delivery:buy-1:2026-07-24T00:00:00Z",
        "result": {"impressions": 10},
    }
    later_recomputed = {
        "idempotency_key": "event-1",
        "operation_id": "delivery:buy-1:2026-07-24T01:00:00Z",
        "result": {"impressions": 12},
    }

    assert repository.store_payload_if_absent("event-1", first) == first
    assert repository.store_payload_if_absent("event-1", later_recomputed) == first


def test_delayed_enum_row_sets_unavailable_identity() -> None:
    response = SimpleNamespace(
        media_buy_deliveries=[
            SimpleNamespace(
                media_buy_id="buy-1",
                status=MediaBuyDeliveryStatus.reporting_delayed,
            )
        ],
        errors=[],
    )

    assert _unavailable_delivery_ids(response, "buy-1") == {"buy-1"}
