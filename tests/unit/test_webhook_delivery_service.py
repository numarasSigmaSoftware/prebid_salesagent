"""Unit tests for webhook delivery service.

Tests the thread-safe webhook delivery service that's shared by all adapters.
"""

import json
import threading
from datetime import UTC, datetime
from unittest.mock import ANY, MagicMock, patch

import pytest

from src.core.security.webhook_http import WEBHOOK_POST_TIMEOUT_SECONDS
from src.services.webhook_delivery_service import CircuitState, WebhookDeliveryService


@pytest.fixture
def webhook_service():
    """Create a fresh webhook service for each test."""
    return WebhookDeliveryService()


@pytest.fixture
def mock_db_session(mocker):
    """Mock database session for SQLAlchemy 2.0 (select() + scalars())."""
    mock_session = MagicMock()

    # Mock SQLAlchemy 2.0 pattern: session.scalars(stmt).all()
    mock_scalars = MagicMock()
    mock_scalars.all.return_value = []  # No webhooks configured by default
    mock_session.scalars.return_value = mock_scalars

    # Mock the database session context manager
    mock_context = MagicMock()
    mock_context.__enter__.return_value = mock_session
    mock_context.__exit__.return_value = None

    mocker.patch("src.core.database.repositories.uow.get_db_session", return_value=mock_context)
    return mock_session


def test_sequence_number_increments(webhook_service, mock_db_session):
    """Test that sequence numbers increment correctly."""
    media_buy_id = "buy_123"
    start_time = datetime.now(UTC)

    # Send 3 webhooks
    for _ in range(3):
        webhook_service.send_delivery_webhook(
            media_buy_id=media_buy_id,
            tenant_id="tenant1",
            principal_id="principal1",
            reporting_period_start=start_time,
            reporting_period_end=start_time,
            impressions=1000,
            spend=100.0,
        )

    # Sequence should be at 3
    with webhook_service._lock:
        assert webhook_service._sequence_numbers[media_buy_id] == 3


def test_thread_safety(webhook_service, mock_db_session):
    """Test that service is thread-safe with concurrent calls."""
    media_buy_id = "buy_concurrent"
    start_time = datetime.now(UTC)
    num_threads = 10

    def send_webhook():
        webhook_service.send_delivery_webhook(
            media_buy_id=media_buy_id,
            tenant_id="tenant1",
            principal_id="principal1",
            reporting_period_start=start_time,
            reporting_period_end=start_time,
            impressions=1000,
            spend=100.0,
        )

    # Send webhooks from multiple threads
    threads = [threading.Thread(target=send_webhook) for _ in range(num_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Should have exactly num_threads webhooks sent
    with webhook_service._lock:
        assert webhook_service._sequence_numbers[media_buy_id] == num_threads


def test_adcp_payload_structure(webhook_service, mock_db_session):
    """Test that payload follows AdCP V2.3 structure with enhanced security (PR #86)."""
    media_buy_id = "buy_adcp"
    start_time = datetime.now(UTC)

    # Mock the shared pinned POST seam to capture the exact wire body.
    with patch("src.services.webhook_delivery_service.post_webhook_status", return_value=200) as mock_post:
        # Mock webhook config
        mock_config = MagicMock()
        mock_config.url = "https://example.com/webhook"
        mock_config.authentication_type = None
        mock_config.validation_token = None
        mock_config.webhook_secret = None  # No HMAC for this test
        mock_config.authentication_token = None
        mock_config.auth_blocked_at = None

        # Update mock to return config for SQLAlchemy 2.0
        mock_db_session.scalars.return_value.all.return_value = [mock_config]

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
            timeout=WEBHOOK_POST_TIMEOUT_SECONDS,
        )
        call_args = mock_post.call_args

        # Check new payload structure (PR #86 - no wrapper, direct payload)
        # The stamped version is RELEASE precision (MAJOR.MINOR) per
        # core/version-envelope.json — NOT the SDK's patch-precision spec pin
        # ("3.1.1"), which this agent's own inbound _RELEASE_PIN_RE rejects.
        # Asserted against the advertisement + the real parser rather than
        # against wire_adcp_version(), so both sides cannot drift together.
        from src.core.adcp_version import _parse_release_pin, supported_adcp_versions

        payload = json.loads(call_args.kwargs["body"])
        assert _parse_release_pin(payload["adcp_version"]) is not None, (
            f"outbound adcp_version {payload['adcp_version']!r} is not release precision"
        )
        assert payload["adcp_version"] in supported_adcp_versions()
        assert payload["notification_type"] == "scheduled"
        assert payload["is_adjusted"] is False  # NEW in PR #86
        assert payload["sequence_number"] == 1
        assert "reporting_period" in payload
        assert payload["reporting_period"]["start"] == start_time.isoformat()
        assert "media_buy_deliveries" in payload
        assert len(payload["media_buy_deliveries"]) == 1

        # Check delivery data
        delivery = payload["media_buy_deliveries"][0]
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
        mock_config = MagicMock()
        mock_config.url = "https://example.com/webhook"
        mock_config.authentication_type = None
        mock_config.validation_token = None
        mock_config.webhook_secret = None
        mock_config.authentication_token = None
        mock_config.auth_blocked_at = None
        mock_db_session.scalars.return_value.all.return_value = [mock_config]

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
        payload = json.loads(mock_post.call_args.kwargs["body"])
        assert payload["notification_type"] == "final"
        assert payload["is_adjusted"] is False
        assert "next_expected_at" not in payload


def test_reset_sequence(webhook_service, mock_db_session):
    """Test that reset_sequence clears sequence numbers (PR #86)."""
    media_buy_id = "buy_reset"
    start_time = datetime.now(UTC)

    # Send 3 webhooks
    for _ in range(3):
        webhook_service.send_delivery_webhook(
            media_buy_id=media_buy_id,
            tenant_id="tenant1",
            principal_id="principal1",
            reporting_period_start=start_time,
            reporting_period_end=start_time,
            impressions=1000,
            spend=100.0,
        )

    # Reset
    webhook_service.reset_sequence(media_buy_id)

    # Verify sequence number cleared (PR #86: failure tracking is per-endpoint via circuit breakers)
    with webhook_service._lock:
        assert media_buy_id not in webhook_service._sequence_numbers


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

        mock_config = MagicMock()
        mock_config.url = "https://example.com/webhook"
        mock_config.authentication_type = None
        mock_config.validation_token = None
        mock_config.webhook_secret = None
        mock_config.authentication_token = None
        mock_config.auth_blocked_at = None
        mock_db_session.scalars.return_value.all.return_value = [mock_config]

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
        mock_config = MagicMock()
        mock_config.url = "https://example.com/webhook"
        # The AdCP scheme spelling (core/push_notification_config.json v3.1.1).
        mock_config.authentication_type = "Bearer"
        mock_config.authentication_token = "secret_token"
        mock_config.validation_token = "validation_token"
        mock_config.webhook_secret = None
        mock_config.auth_blocked_at = None
        mock_db_session.scalars.return_value.all.return_value = [mock_config]

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
        assert headers["Authorization"] == "Bearer secret_token"
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


class TestBuildDeliveryHeadersRecognisedSchemeNoToken:
    """A recognised-but-tokenless scheme must not be logged as unsupported.

    `_build_delivery_headers`'s elif fires whenever the scheme-match ifs
    failed for ANY reason — including a Bearer row with no token — so it used
    to log "scheme Bearer is not supported on the delivery path (expected
    HMAC-SHA256 or Bearer)", naming Bearer in both halves of the sentence and
    pointing at scheme support when the real problem is a missing credential.
    """

    def test_bearer_with_no_token_names_the_token_not_the_scheme(self, caplog):
        import logging

        from src.core.database.repositories.push_notification_config import (
            PushNotificationTarget,
        )

        config = PushNotificationTarget(
            url="https://example.com/webhook",
            authentication_type="Bearer",
            authentication_token=None,
            webhook_secret=None,
            auth_blocked_at=None,
        )
        service = WebhookDeliveryService()
        with caplog.at_level(logging.WARNING, logger="src.services.webhook_delivery_service"):
            headers = service._build_delivery_headers(config, b"{}", "2026-01-01T00:00:00Z")

        assert "Authorization" not in headers
        message = " ".join(r.getMessage() for r in caplog.records)
        assert "configured with no token" in message, f"wrong axis: {message!r}"
        assert "is not supported" not in message, f"a recognised scheme must not be called unsupported: {message!r}"

    def test_unrecognised_scheme_still_names_it_unsupported(self, caplog):
        import logging

        from src.core.database.repositories.push_notification_config import (
            PushNotificationTarget,
        )

        config = PushNotificationTarget(
            url="https://example.com/webhook",
            authentication_type="Digest",
            authentication_token="irrelevant",
            webhook_secret=None,
            auth_blocked_at=None,
        )
        service = WebhookDeliveryService()
        with caplog.at_level(logging.WARNING, logger="src.services.webhook_delivery_service"):
            headers = service._build_delivery_headers(config, b"{}", "2026-01-01T00:00:00Z")

        assert "Authorization" not in headers
        message = " ".join(r.getMessage() for r in caplog.records)
        assert "is not supported" in message, f"an unrecognised scheme must say so: {message!r}"
        assert "configured with no token" not in message, f"wrong axis: {message!r}"
