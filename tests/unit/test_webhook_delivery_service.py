"""Unit tests for webhook delivery service.

Tests the thread-safe webhook delivery service that's shared by all adapters.
"""

import json
import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import ANY, MagicMock, patch
from uuid import UUID, uuid4

import pytest
import requests

from src.services.webhook_delivery_service import CircuitState, WebhookDeliveryService


@pytest.fixture
def webhook_service():
    """Create a fresh webhook service for each test."""
    return WebhookDeliveryService()


@pytest.fixture
def mock_db_session(mocker):
    """Mock the reporting-channel UoW and durable random event claims."""
    media_buy = MagicMock(raw_request={})
    media_buys = MagicMock()
    media_buys.get_by_id.return_value = media_buy
    events: dict[str, SimpleNamespace] = {}
    payloads: dict[str, dict] = {}

    def claim_event(**kwargs):
        logical_key = kwargs["logical_event_key"]
        if logical_key not in events:
            events[logical_key] = SimpleNamespace(
                idempotency_key=str(uuid4()),
                sequence_number=len(events) + 1,
                event_payload=None,
            )
        return events[logical_key]

    webhook_delivery_logs = MagicMock()
    webhook_delivery_logs.claim_event.side_effect = claim_event
    webhook_delivery_logs.store_payload_if_absent.side_effect = lambda event_id, payload: payloads.setdefault(
        event_id, payload
    )
    uow = MagicMock(media_buys=media_buys, webhook_delivery_logs=webhook_delivery_logs)
    uow.__enter__.return_value = uow
    uow.__exit__.return_value = None
    mocker.patch("src.services.webhook_delivery_service.WebhookDeliveryUoW", return_value=uow)
    return SimpleNamespace(
        media_buy=media_buy,
        media_buys=media_buys,
        webhook_delivery_logs=webhook_delivery_logs,
        events=events,
        payloads=payloads,
    )


def _callback_config(media_buy_id: str) -> MagicMock:
    config = MagicMock()
    config.url = "https://example.com/webhook"
    config.media_buy_id = media_buy_id
    config.operation_id = "create-operation-123"
    config.token = "callback-token-456"
    config.application_context = {"trace_id": "trace-789", "nested": {"value": 1}}
    config.last_event_key = None
    config.last_event_sequence = 0
    config.authentication_type = None
    config.validation_token = None
    config.webhook_secret = None
    config.authentication_token = None
    config.auth_blocked_at = None
    return config


def _configure_reporting(harness, config: MagicMock) -> None:
    harness.media_buy.raw_request = {
        "reporting_webhook": {
            "url": config.url,
            "token": config.token,
            "authentication": {
                "schemes": [config.authentication_type] if config.authentication_type else [],
                "credentials": config.authentication_token,
            },
            "reporting_frequency": "daily",
        },
        "context": config.application_context,
    }


def test_sequence_number_is_durable_and_retry_stable(webhook_service, mock_db_session):
    """A retry reuses its persisted sequence; a distinct event advances it."""
    media_buy_id = "buy_123"
    start_time = datetime.now(UTC)
    config = _callback_config(media_buy_id)
    _configure_reporting(mock_db_session, config)

    with patch("src.services.webhook_delivery_service.post_webhook_status", return_value=200) as mock_post:
        for impressions in (1000, 1000, 2000):
            webhook_service.send_delivery_webhook(
                media_buy_id=media_buy_id,
                tenant_id="tenant1",
                principal_id="principal1",
                reporting_period_start=start_time,
                reporting_period_end=start_time,
                impressions=impressions,
                spend=100.0,
            )

    envelopes = [json.loads(call.kwargs["body"]) for call in mock_post.call_args_list]
    assert [envelope["result"]["sequence_number"] for envelope in envelopes] == [1, 1, 2]
    assert envelopes[0]["idempotency_key"] == envelopes[1]["idempotency_key"]
    assert envelopes[2]["idempotency_key"] != envelopes[1]["idempotency_key"]
    assert UUID(envelopes[0]["idempotency_key"]).version == 4
    assert len(mock_db_session.events) == 2


def test_same_reporting_event_reuses_exact_first_wire_payload(webhook_service, mock_db_session):
    media_buy_id = "buy_retry"
    instant = datetime.now(UTC)
    config = _callback_config(media_buy_id)
    _configure_reporting(mock_db_session, config)

    with patch("src.services.webhook_delivery_service.post_webhook_status", return_value=200) as mock_post:
        webhook_service.send_delivery_webhook(
            media_buy_id=media_buy_id,
            tenant_id="tenant1",
            principal_id="principal1",
            reporting_period_start=instant,
            reporting_period_end=instant,
            impressions=1000,
            spend=100.0,
        )
        mock_db_session.media_buy.raw_request["context"] = {"trace_id": "changed"}
        mock_db_session.media_buy.raw_request["reporting_webhook"]["token"] = "changed-token-credential-456"
        webhook_service.send_delivery_webhook(
            media_buy_id=media_buy_id,
            tenant_id="tenant1",
            principal_id="principal1",
            reporting_period_start=instant,
            reporting_period_end=instant,
            impressions=1000,
            spend=100.0,
        )

    first, retry = [json.loads(call.kwargs["body"]) for call in mock_post.call_args_list]
    assert retry == first


def test_delivery_uses_only_media_buy_bound_repository_claim(webhook_service, mock_db_session):
    """The service scopes callback lookup to the originating media buy."""
    start_time = datetime.now(UTC)
    _configure_reporting(mock_db_session, _callback_config("buy_origin"))

    with patch.object(webhook_service, "_queue_and_deliver_target", return_value=1):
        webhook_service.send_delivery_webhook(
            media_buy_id="buy_origin",
            tenant_id="tenant1",
            principal_id="principal1",
            reporting_period_start=start_time,
            reporting_period_end=start_time,
            impressions=1000,
            spend=100.0,
        )

    mock_db_session.media_buys.get_by_id.assert_called_once_with("buy_origin")
    claim = mock_db_session.webhook_delivery_logs.claim_event.call_args.kwargs
    assert claim["principal_id"] == "principal1"
    assert claim["media_buy_id"] == "buy_origin"


def test_adcp_payload_structure(webhook_service, mock_db_session):
    """Test that payload follows AdCP V2.3 structure with enhanced security (PR #86)."""
    media_buy_id = "buy_adcp"
    start_time = datetime.now(UTC)

    # Mock the shared pinned POST seam to capture the exact wire body.
    with patch("src.services.webhook_delivery_service.post_webhook_status", return_value=200) as mock_post:
        mock_config = _callback_config(media_buy_id)

        # Update mock to return config for SQLAlchemy 2.0
        _configure_reporting(mock_db_session, mock_config)

        # Send webhook
        webhook_service.send_delivery_webhook(
            media_buy_id=media_buy_id,
            tenant_id="tenant1",
            principal_id="principal1",
            reporting_period_start=start_time,
            reporting_period_end=start_time,
            impressions=5000,
            spend=500.0,
            clicks=50,
            ctr=0.01,
            is_final=False,
            next_expected_interval_seconds=60.0,
        )

        mock_post.assert_called_once_with(
            ANY,
            "https://example.com/webhook",
            body=ANY,
            headers=ANY,
            timeout=10.0,
        )
        call_args = mock_post.call_args

        # Check new payload structure (PR #86 - no wrapper, direct payload)
        # Version should match what's reported by the adcp library
        from adcp import get_adcp_spec_version

        envelope = json.loads(call_args.kwargs["body"])
        assert envelope["task_type"] == "media_buy_delivery"
        assert envelope["operation_id"].startswith("reporting:")
        assert envelope["token"] == "callback-token-456"
        assert envelope["context"] == {"trace_id": "trace-789", "nested": {"value": 1}}
        payload = envelope["result"]
        assert payload["adcp_version"] == ".".join(get_adcp_spec_version().split(".")[:2])
        assert payload["notification_type"] == "scheduled"
        assert "aggregated_totals" not in payload
        assert payload["sequence_number"] == 1
        assert "reporting_period" in payload
        assert payload["reporting_period"]["start"] == start_time.isoformat().replace("+00:00", "Z")
        assert "media_buy_deliveries" in payload
        assert len(payload["media_buy_deliveries"]) == 1

        # Check delivery data
        delivery = payload["media_buy_deliveries"][0]
        assert delivery["is_adjusted"] is False
        assert delivery["media_buy_id"] == media_buy_id
        assert delivery["status"] == "active"
        assert delivery["totals"]["impressions"] == 5000
        assert delivery["totals"]["spend"] == 500.0
        assert delivery["totals"]["clicks"] == 50
        assert delivery["totals"]["ctr"] == 0.01


def test_final_notification_type(webhook_service, mock_db_session):
    """Test that is_final sets notification_type to 'final' (PR #86)."""
    media_buy_id = "buy_final"
    start_time = datetime.now(UTC)

    with patch("src.services.webhook_delivery_service.post_webhook_status", return_value=200) as mock_post:
        mock_config = _callback_config(media_buy_id)
        _configure_reporting(mock_db_session, mock_config)

        # Send final webhook
        webhook_service.send_delivery_webhook(
            media_buy_id=media_buy_id,
            tenant_id="tenant1",
            principal_id="principal1",
            reporting_period_start=start_time,
            reporting_period_end=start_time,
            impressions=10000,
            spend=1000.0,
            status="completed",
            is_final=True,
        )

        # Check notification_type (direct payload structure in PR #86)
        payload = json.loads(mock_post.call_args.kwargs["body"])["result"]
        assert payload["notification_type"] == "final"
        assert payload["media_buy_deliveries"][0]["is_adjusted"] is False
        assert payload.get("next_expected_at") is None


def test_reset_sequence_does_not_discard_durable_state(webhook_service, mock_db_session):
    """Legacy lifecycle resets cannot make persisted sequence numbers reusable."""
    config = _callback_config("buy_reset")
    config.last_event_key = "persisted-event"
    config.last_event_sequence = 7
    webhook_service.reset_sequence("buy_reset")
    assert config.last_event_key == "persisted-event"
    assert config.last_event_sequence == 7


@patch("src.services.webhook_delivery_service.time.sleep")
def test_failure_tracking(mock_sleep, webhook_service, mock_db_session):
    """Test that failures are tracked correctly with circuit breaker (PR #86)."""
    media_buy_id = "buy_fail"
    start_time = datetime.now(UTC)

    with patch("src.services.webhook_delivery_service.post_webhook_status") as mock_post:
        mock_post.side_effect = [
            200,
            500,
            500,
            500,
        ]

        mock_config = _callback_config(media_buy_id)
        _configure_reporting(mock_db_session, mock_config)

        # First webhook - success
        result1 = webhook_service.send_delivery_webhook(
            media_buy_id=media_buy_id,
            tenant_id="tenant1",
            principal_id="principal1",
            reporting_period_start=start_time,
            reporting_period_end=start_time,
            impressions=1000,
            spend=100.0,
        )
        assert result1 is True

        # Check circuit breaker state after success (should be CLOSED)
        endpoint_key = "tenant1:https://example.com/webhook"
        state, failures = webhook_service.get_circuit_breaker_state(endpoint_key)
        assert state == CircuitState.CLOSED
        assert failures == 0

        # Second webhook - failure (will retry 3 times)
        result2 = webhook_service.send_delivery_webhook(
            media_buy_id=media_buy_id,
            tenant_id="tenant1",
            principal_id="principal1",
            reporting_period_start=start_time,
            reporting_period_end=start_time,
            impressions=2000,
            spend=200.0,
        )
        assert result2 is False

        # Check circuit breaker recorded the failure
        state, failures = webhook_service.get_circuit_breaker_state(endpoint_key)
        assert state == CircuitState.CLOSED  # Still closed (threshold is 5)
        assert failures == 1


def test_authentication_headers(webhook_service, mock_db_session):
    """Test that authentication headers are set correctly (PR #86)."""
    media_buy_id = "buy_auth"
    start_time = datetime.now(UTC)

    with patch("src.services.webhook_delivery_service.post_webhook_status", return_value=200) as mock_post:
        # Test bearer auth
        mock_config = _callback_config(media_buy_id)
        # The AdCP scheme spelling (core/push_notification_config.json v3.1.1).
        mock_config.authentication_type = "Bearer"
        mock_config.authentication_token = "secret_token_that_is_at_least_32_chars"
        mock_config.validation_token = "validation_token"
        _configure_reporting(mock_db_session, mock_config)

        webhook_service.send_delivery_webhook(
            media_buy_id=media_buy_id,
            tenant_id="tenant1",
            principal_id="principal1",
            reporting_period_start=start_time,
            reporting_period_end=start_time,
            impressions=1000,
            spend=100.0,
        )

        # Verify headers (PR #86 added X-ADCP-Timestamp, no longer uses X-Webhook-Token)
        call_args = mock_post.call_args
        headers = call_args.kwargs["headers"]
        assert headers["Authorization"] == "Bearer secret_token_that_is_at_least_32_chars"
        assert "X-ADCP-Timestamp" in headers  # NEW in PR #86


def test_no_webhooks_configured(webhook_service, mock_db_session):
    """Test behavior when no webhooks are configured."""
    media_buy_id = "buy_no_config"
    start_time = datetime.now(UTC)

    # No webhooks configured (default mock behavior)
    result = webhook_service.send_delivery_webhook(
        media_buy_id=media_buy_id,
        tenant_id="tenant1",
        principal_id="principal1",
        reporting_period_start=start_time,
        reporting_period_end=start_time,
        impressions=1000,
        spend=100.0,
    )

    # Should return False but not error
    assert result is False


def test_deliver_rejects_metadata_url_without_post(webhook_service, mock_db_session, monkeypatch):
    """Send-time SSRF gate must refuse cloud-metadata URLs before any POST.

    Drives the REAL gate (a literal metadata IP needs no DNS): the delivery
    worker must skip the POST entirely and record the failure on the
    endpoint's circuit breaker.
    """
    monkeypatch.delenv("ADCP_TESTING", raising=False)
    start_time = datetime.now(UTC)

    mock_config = MagicMock()
    mock_config.url = "http://169.254.169.254/latest/meta-data/"
    mock_config.authentication_type = None
    mock_config.authentication_token = None
    mock_config.validation_token = None
    mock_config.webhook_secret = None
    mock_db_session.scalars.return_value.all.return_value = [mock_config]

    with patch("src.services.webhook_delivery_service.post_webhook_status") as mock_post:
        result = webhook_service.send_delivery_webhook(
            media_buy_id="buy_ssrf",
            tenant_id="tenant1",
            principal_id="principal1",
            reporting_period_start=start_time,
            reporting_period_end=start_time,
            impressions=1000,
            spend=100.0,
        )

    assert result is False
    mock_post.assert_not_called()
    endpoint_key = f"tenant1:{mock_config.url}"
    breaker = webhook_service._circuit_breakers[endpoint_key]
    assert breaker.failure_count == 1


def test_deliver_disables_redirects(webhook_service, mock_db_session):
    """The outbound delivery POST must never follow redirects (open-redirect SSRF)."""
    start_time = datetime.now(UTC)

    mock_config = MagicMock()
    mock_config.url = "https://example.com/webhook"
    mock_config.authentication_type = None
    mock_config.authentication_token = None
    mock_config.validation_token = None
    mock_config.webhook_secret = None
    mock_db_session.scalars.return_value.all.return_value = [mock_config]

    captured: dict = {}
    ok_resp = MagicMock()
    ok_resp.status_code = 200

    def _fake_post(self, url, **kwargs):  # noqa: ANN001 - test stub
        captured["kwargs"] = kwargs
        return ok_resp

    with (
        patch(
            "src.core.webhook_validator.WebhookURLValidator.validate_outbound_webhook_url",
            return_value=(True, ""),
        ),
        patch("requests.sessions.Session.post", _fake_post),
    ):
        webhook_service.send_delivery_webhook(
            media_buy_id="buy_redir",
            tenant_id="tenant1",
            principal_id="principal1",
            reporting_period_start=start_time,
            reporting_period_end=start_time,
            impressions=1000,
            spend=100.0,
        )

    assert captured["kwargs"]["allow_redirects"] is False


def test_network_error_logs_never_expose_webhook_query_secrets(
    webhook_service,
    mock_db_session,
    caplog,
):
    """Request exceptions may embed their URL; logs retain only the error type."""
    secret = "buyer-query-secret-that-must-not-leak"
    config = _callback_config("buy_secret_log")
    config.url = f"https://example.com/webhook?token={secret}"
    _configure_reporting(mock_db_session, config)
    start_time = datetime.now(UTC)

    with (
        patch(
            "src.services.webhook_delivery_service.post_webhook_status",
            side_effect=requests.ConnectionError(f"failed URL {config.url}"),
        ),
        caplog.at_level(logging.WARNING),
    ):
        result = webhook_service.send_delivery_webhook(
            media_buy_id="buy_secret_log",
            tenant_id="tenant1",
            principal_id="principal1",
            reporting_period_start=start_time,
            reporting_period_end=start_time,
            impressions=1000,
            spend=100.0,
        )

    assert result is False
    assert secret not in caplog.text
    assert config.url not in caplog.text
    assert "ConnectionError" in caplog.text
