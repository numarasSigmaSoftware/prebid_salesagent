"""Integration behavioral tests for UC-004 delivery service (WebhookDeliveryService, CircuitBreaker).

Migrated from tests/unit/test_delivery_service_behavioral.py to use CircuitBreakerEnv
integration harness. External HTTP, timing, and randomness are mocked; DB
operations for PushNotificationConfig queries are real.

Pure CircuitBreaker state machine tests remain in the unit file.

Each test targets exactly one obligation ID and follows the 6 hard rules.
"""

from __future__ import annotations

import pytest

from src.services.webhook_delivery_service import (
    CircuitState,
)


def _ensure_media_buy(
    tenant,
    principal,
    media_buy_id: str,
    *,
    webhook_url: str | None = None,
    authentication_scheme: str = "Bearer",
    credentials: str = "integration-reporting-token-" + ("x" * 32),
):
    """Create the callback's owning media buy through the shared factory.

    Reporting callbacks are configured exclusively on the media buy. Task
    ``PushNotificationConfig`` rows are intentionally not consulted.
    """
    from tests.factories import MediaBuyFactory

    raw_request = {"packages": [{"package_id": "pkg_001", "product_id": "prod_001"}]}
    if webhook_url is not None:
        raw_request["reporting_webhook"] = {
            "url": webhook_url,
            "reporting_frequency": "daily",
            "authentication": {
                "schemes": [authentication_scheme],
                "credentials": credentials,
            },
        }
    return MediaBuyFactory(
        tenant=tenant,
        principal=principal,
        media_buy_id=media_buy_id,
        raw_request=raw_request,
    )


@pytest.mark.requires_db
def test_delivery_target_claim_is_media_buy_scoped_and_retry_stable(integration_db):
    """Only the originating registration is claimed, with durable correlation."""
    from src.core.database.repositories.uow import PushNotificationConfigUoW
    from tests.factories import (
        MediaBuyFactory,
        PrincipalFactory,
        PushNotificationConfigFactory,
        TenantFactory,
    )
    from tests.harness import CircuitBreakerEnv

    with CircuitBreakerEnv(tenant_id="correlation-tenant", principal_id="correlation-principal"):
        tenant = TenantFactory(tenant_id="correlation-tenant")
        principal = PrincipalFactory(tenant=tenant, principal_id="correlation-principal")
        first_buy = MediaBuyFactory(
            tenant=tenant,
            principal=principal,
            media_buy_id="correlation-buy-1",
        )
        second_buy = MediaBuyFactory(
            tenant=tenant,
            principal=principal,
            media_buy_id="correlation-buy-2",
        )
        first_config = PushNotificationConfigFactory(
            tenant=tenant,
            principal=principal,
            media_buy_id=first_buy.media_buy_id,
            operation_id="operation-1",
            token="token-1",
            application_context={"trace_id": "trace-1"},
            url="https://first.example.com/webhook",
        )
        PushNotificationConfigFactory(
            tenant=tenant,
            principal=principal,
            media_buy_id=second_buy.media_buy_id,
            operation_id="operation-2",
            token="token-2",
            application_context={"trace_id": "trace-2"},
            url="https://second.example.com/webhook",
        )

        with PushNotificationConfigUoW(tenant.tenant_id) as uow:
            assert uow.push_notification_configs is not None
            first_claim = uow.push_notification_configs.claim_delivery_targets(
                principal.principal_id,
                first_buy.media_buy_id,
                "event-1",
            )

        assert len(first_claim) == 1
        assert first_claim[0].url == first_config.url
        assert first_claim[0].operation_id == "operation-1"
        assert first_claim[0].token == "token-1"
        assert first_claim[0].application_context == {"trace_id": "trace-1"}
        assert first_claim[0].sequence_number == 1

        with PushNotificationConfigUoW(tenant.tenant_id) as uow:
            assert uow.push_notification_configs is not None
            retry_claim = uow.push_notification_configs.claim_delivery_targets(
                principal.principal_id,
                first_buy.media_buy_id,
                "event-1",
            )
            next_claim = uow.push_notification_configs.claim_delivery_targets(
                principal.principal_id,
                first_buy.media_buy_id,
                "event-2",
            )

        assert retry_claim[0].sequence_number == 1
        assert next_claim[0].sequence_number == 2


# ---------------------------------------------------------------------------
# UC-004-EXT-G-03
# ---------------------------------------------------------------------------


@pytest.mark.requires_db
class TestCircuitBreakerServiceIntegration:
    """Service-level circuit breaker integration with real DB.

    Covers: UC-004-EXT-G-03
    """

    def test_service_skips_delivery_when_circuit_open(self, integration_db):
        """WebhookDeliveryService skips webhook send when circuit breaker is OPEN.

        Covers: UC-004-EXT-G-03
        """
        from datetime import UTC, datetime

        from tests.factories import (
            PrincipalFactory,
            TenantFactory,
        )
        from tests.harness import CircuitBreakerEnv

        with CircuitBreakerEnv() as env:
            tenant = TenantFactory(tenant_id="t1")
            principal = PrincipalFactory(tenant=tenant, principal_id="p1")
            _ensure_media_buy(
                tenant,
                principal,
                "mb_circuit",
                webhook_url="https://example.com/webhook",
            )

            # Make HTTP fail to trip the circuit breaker
            env.set_http_response(500)
            service = env.get_service()

            start_time = datetime(2025, 6, 1, tzinfo=UTC)
            for _ in range(5):
                service.send_delivery_webhook(
                    media_buy_id="mb_circuit",
                    tenant_id="t1",
                    principal_id="p1",
                    reporting_period_start=start_time,
                    reporting_period_end=start_time,
                    impressions=1000,
                    spend=100.0,
                )

            endpoint_key = "t1:https://example.com/webhook"
            state, _ = service.get_circuit_breaker_state(endpoint_key)
            assert state == CircuitState.OPEN

            # Reset mock to track new calls
            env.mock["client"].return_value.__enter__.return_value.post.reset_mock()

            result = service.send_delivery_webhook(
                media_buy_id="mb_circuit",
                tenant_id="t1",
                principal_id="p1",
                reporting_period_start=start_time,
                reporting_period_end=start_time,
                impressions=1000,
                spend=100.0,
            )

            assert result is False
            env.mock["client"].return_value.__enter__.return_value.post.assert_not_called()


# ---------------------------------------------------------------------------
# UC-004-EXT-G-04
# ---------------------------------------------------------------------------


@pytest.mark.requires_db
class TestCircuitBreakerHalfOpenProbeService:
    """Service-level circuit breaker half-open probe with real DB.

    Covers: UC-004-EXT-G-04
    """

    def test_service_allows_probe_after_circuit_breaker_timeout(self, integration_db):
        """WebhookDeliveryService uses circuit breaker can_attempt() to allow
        half-open probe after timeout expires.

        Covers: UC-004-EXT-G-04
        """
        from datetime import UTC, datetime, timedelta

        from src.services.webhook_delivery_service import CircuitBreaker
        from tests.harness import CircuitBreakerEnv

        with CircuitBreakerEnv() as env:
            service = env.get_service()

            endpoint_key = "t1:https://example.com/webhook"
            cb = CircuitBreaker(failure_threshold=3, success_threshold=2, timeout_seconds=60)
            cb.state = CircuitState.OPEN
            cb.last_failure_time = datetime.now(UTC) - timedelta(seconds=120)

            service._circuit_breakers[endpoint_key] = cb

            assert cb.can_attempt() is True
            assert cb.state == CircuitState.HALF_OPEN


# ---------------------------------------------------------------------------
# UC-004-EXT-G-08
# ---------------------------------------------------------------------------


@pytest.mark.requires_db
class TestWebhookFailureNoSyncError:
    """Webhook failure does not produce synchronous error to buyer.

    Covers: UC-004-EXT-G-08
    """

    def test_webhook_failure_does_not_affect_poll_response(self, integration_db):
        """Poll endpoint and webhook delivery are separate code paths.
        A webhook failure cannot propagate to the poll response.

        Covers: UC-004-EXT-G-08
        """
        from datetime import UTC, datetime
        from unittest.mock import patch

        from src.services.webhook_delivery_service import WebhookDeliveryService
        from tests.factories import MediaBuyFactory, PrincipalFactory, TenantFactory
        from tests.harness import DeliveryPollEnv

        # First: simulate webhook failure
        service = WebhookDeliveryService()
        with patch.object(service, "_send_webhook_enhanced", side_effect=Exception("timeout")):
            webhook_result = service.send_delivery_webhook(
                media_buy_id="mb_001",
                tenant_id="t1",
                principal_id="p1",
                reporting_period_start=datetime(2025, 1, 1, tzinfo=UTC),
                reporting_period_end=datetime(2025, 6, 30, tzinfo=UTC),
                impressions=5000,
                spend=250.0,
            )

        assert webhook_result is False

        # Then: poll should still work fine
        with DeliveryPollEnv(tenant_id="t1", principal_id="p1") as env:
            tenant = TenantFactory(tenant_id="t1")
            principal = PrincipalFactory(tenant=tenant, principal_id="p1")
            buy = MediaBuyFactory(tenant=tenant, principal=principal)
            env.set_adapter_response(buy.media_buy_id, impressions=5000, spend=250.0)

            response = env.call_impl(media_buy_ids=[buy.media_buy_id])

        assert len(response.media_buy_deliveries) == 1
        assert response.media_buy_deliveries[0].totals.impressions == 5000.0
        assert response.errors is None


# ---------------------------------------------------------------------------
# UC-004-EXT-G-07 (_send_webhook_enhanced: auth-blocked skip)
# ---------------------------------------------------------------------------


@pytest.mark.requires_db
class TestSendWebhookEnhancedAuthBlockedSkip:
    """Invalid reporting authentication is rejected before delivery.

    Covers: UC-004-EXT-G-07
    """

    def test_auth_blocked_config_skipped_no_http_request(self, integration_db):
        """A malformed persisted reporting credential fails closed without I/O.

        Covers: UC-004-EXT-G-07
        """
        from tests.factories import (
            PrincipalFactory,
            TenantFactory,
        )
        from tests.harness import CircuitBreakerEnv

        with CircuitBreakerEnv(tenant_id="t1", principal_id="p1") as env:
            tenant = TenantFactory(tenant_id="t1")
            principal = PrincipalFactory(tenant=tenant, principal_id="p1")
            _ensure_media_buy(
                tenant,
                principal,
                "mb_001",
                webhook_url="https://blocked.example.com/webhook",
                credentials="too-short",
            )

            env.set_http_response(200)
            service = env.get_service()
            result = service._send_webhook_enhanced(
                tenant_id="t1",
                principal_id="p1",
                media_buy_id="mb_001",
                delivery_payload={"test": "data"},
            )

            assert result is False
            env.mock["client"].return_value.__enter__.return_value.post.assert_not_called()


@pytest.mark.requires_db
class TestPersistedWebhookSsrfRevalidation:
    """Persisted push targets are revalidated immediately before connection."""

    def test_persisted_target_is_revalidated_before_socket_connect(self, integration_db, monkeypatch):
        """A stale row that resolves to metadata IP is refused before urllib3 connects."""
        from urllib3.poolmanager import PoolManager

        from src.core.security import url_validator, webhook_http
        from src.services.webhook_delivery_service import WebhookDeliveryService
        from tests.factories import PrincipalFactory, TenantFactory
        from tests.harness._base import BareIntegrationEnv

        dns_lookups: list[str] = []
        connection_attempts: list[bool] = []

        def resolve_to_metadata(hostname: str) -> list[str]:
            dns_lookups.append(hostname)
            return ["169.254.169.254"]

        def record_connection_attempt(*_args: object, **_kwargs: object) -> None:
            connection_attempts.append(True)
            raise AssertionError("SSRF-invalid target reached the urllib3 connection seam")

        monkeypatch.setattr(webhook_http, "_allow_private_webhook_targets", lambda: False)
        monkeypatch.setattr(url_validator, "_resolve_ips", resolve_to_metadata)
        monkeypatch.setattr(PoolManager, "connection_from_host", record_connection_attempt)

        url = "https://rebind.example/webhook"
        with BareIntegrationEnv(tenant_id="t1", principal_id="p1") as env:
            tenant = TenantFactory(tenant_id="t1")
            principal = PrincipalFactory(tenant=tenant, principal_id="p1")
            _ensure_media_buy(tenant, principal, "mb_001", webhook_url=url)
            env.get_session()

            service = WebhookDeliveryService()
            result = service._send_webhook_enhanced(
                tenant_id="t1",
                principal_id="p1",
                media_buy_id="mb_001",
                delivery_payload={"impressions": 5000},
            )

            assert result is False
            assert dns_lookups == ["rebind.example"]
            assert connection_attempts == []
            state, failure_count = service.get_circuit_breaker_state(url)
            assert state == CircuitState.CLOSED
            assert failure_count == 1


# ---------------------------------------------------------------------------
# UC-004-EXT-G-06 (_send_webhook_enhanced: HMAC signing)
# ---------------------------------------------------------------------------


@pytest.mark.requires_db
class TestSendWebhookEnhancedHmacSigning:
    """HMAC-SHA256 signature is added when webhook_secret is configured.

    Covers: UC-004-EXT-G-06
    """

    def test_hmac_signature_header_present_when_secret_configured(self, integration_db):
        """When PushNotificationConfig has a strong webhook_secret (>=32 chars),
        X-AdCP-Signature header is set on the outgoing request.

        Covers: UC-004-EXT-G-06
        """
        from tests.factories import (
            PrincipalFactory,
            TenantFactory,
        )
        from tests.harness import CircuitBreakerEnv

        with CircuitBreakerEnv(tenant_id="t1", principal_id="p1") as env:
            tenant = TenantFactory(tenant_id="t1")
            principal = PrincipalFactory(tenant=tenant, principal_id="p1")
            _ensure_media_buy(
                tenant,
                principal,
                "mb_001",
                webhook_url="https://hmac.example.com/webhook",
                authentication_scheme="HMAC-SHA256",
                credentials="a" * 32,
            )

            env.set_http_response(200)
            service = env.get_service()
            result = service._send_webhook_enhanced(
                tenant_id="t1",
                principal_id="p1",
                media_buy_id="mb_001",
                delivery_payload={"impressions": 5000, "spend": 250.0},
            )

            assert result is True
            post_mock = env.mock["client"].return_value.__enter__.return_value.post
            post_mock.assert_called_once()
            sent_headers = post_mock.call_args.kwargs["headers"]
            assert "X-AdCP-Signature" in sent_headers
            assert sent_headers["X-AdCP-Signature"].startswith("sha256=")

    def test_hmac_signature_valid_reproduces_from_payload(self, integration_db):
        """The HMAC signature can be reproduced using the same secret and payload.

        Covers: UC-004-EXT-G-06
        """
        import hashlib
        import hmac

        from tests.factories import (
            PrincipalFactory,
            TenantFactory,
        )
        from tests.harness import CircuitBreakerEnv

        secret = "b" * 32
        payload = {"media_buy_id": "mb_001", "impressions": 5000}

        with CircuitBreakerEnv(tenant_id="t1", principal_id="p1") as env:
            tenant = TenantFactory(tenant_id="t1")
            principal = PrincipalFactory(tenant=tenant, principal_id="p1")
            _ensure_media_buy(
                tenant,
                principal,
                "mb_001",
                webhook_url="https://hmac-verify.example.com/webhook",
                authentication_scheme="HMAC-SHA256",
                credentials=secret,
            )

            env.set_http_response(200)
            service = env.get_service()
            service._send_webhook_enhanced(
                tenant_id="t1",
                principal_id="p1",
                media_buy_id="mb_001",
                delivery_payload=payload,
            )

            post_mock = env.mock["client"].return_value.__enter__.return_value.post
            sent_headers = post_mock.call_args.kwargs["headers"]
            sent_signature = sent_headers["X-AdCP-Signature"]
            sent_timestamp = sent_headers["X-AdCP-Timestamp"]

            # Reproduce the signature over the exact callback envelope bytes.
            sent_body = post_mock.call_args.kwargs["body"]
            message = sent_timestamp.encode("utf-8") + b"." + sent_body
            expected = "sha256=" + hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()

            assert sent_signature == expected


# ---------------------------------------------------------------------------
# UC-004-ALT-WEBHOOK-PUSH-REPORTING-08 (_send_webhook_enhanced: bearer auth)
# ---------------------------------------------------------------------------


@pytest.mark.requires_db
class TestSendWebhookEnhancedBearerAuth:
    """Bearer token authentication is set when configured on PushNotificationConfig.

    Covers: UC-004-ALT-WEBHOOK-PUSH-REPORTING-08
    """

    @pytest.mark.parametrize(
        "configured_scheme",
        ["Bearer", "bearer"],
        ids=["adcp-spelling", "legacy-lowercase-row"],
    )
    def test_bearer_token_sent_in_authorization_header(self, integration_db, configured_scheme):
        """A configured Bearer scheme puts 'Bearer <token>' on the wire.

        ``core/push_notification_config.json`` (v3.1.1) enumerates the scheme as
        ``Bearer``, and the A2A/REST intake stores ``authentication.scheme``
        verbatim — so the CAPITALIZED spelling is what a conformant config
        actually carries. Pinning only the lowercase spelling let an
        exact-match comparison ship that delivered every conformant Bearer
        webhook with no Authorization header at all. The legacy row is kept as
        a second case because pre-existing rows hold it.

        Covers: UC-004-ALT-WEBHOOK-PUSH-REPORTING-08
        """
        from tests.factories import (
            PrincipalFactory,
            TenantFactory,
        )
        from tests.harness import CircuitBreakerEnv

        bearer_token = "my-secret-token-" + ("x" * 32)
        with CircuitBreakerEnv(tenant_id="t1", principal_id="p1") as env:
            tenant = TenantFactory(tenant_id="t1")
            principal = PrincipalFactory(tenant=tenant, principal_id="p1")
            _ensure_media_buy(
                tenant,
                principal,
                "mb_001",
                webhook_url="https://bearer.example.com/webhook",
                authentication_scheme=configured_scheme,
                credentials=bearer_token,
            )

            env.set_http_response(200)
            service = env.get_service()
            result = service._send_webhook_enhanced(
                tenant_id="t1",
                principal_id="p1",
                media_buy_id="mb_001",
                delivery_payload={"impressions": 5000},
            )

            assert result is True
            post_mock = env.mock["client"].return_value.__enter__.return_value.post
            post_mock.assert_called_once()
            sent_headers = post_mock.call_args.kwargs["headers"]
            assert sent_headers["Authorization"] == f"Bearer {bearer_token}"


# ---------------------------------------------------------------------------
# UC-004-ALT-WEBHOOK-PUSH-REPORTING-01 (_send_webhook_enhanced: happy path)
# ---------------------------------------------------------------------------


@pytest.mark.requires_db
class TestSendWebhookEnhancedHappyPath:
    """Happy path: _send_webhook_enhanced delivers to configured endpoint.

    Covers: UC-004-ALT-WEBHOOK-PUSH-REPORTING-01
    """

    def test_happy_path_delivers_payload_to_configured_endpoint(self, integration_db):
        """With a working endpoint and valid config, _send_webhook_enhanced returns True
        and sends the payload to the configured URL.

        Covers: UC-004-ALT-WEBHOOK-PUSH-REPORTING-01
        """
        import json
        from unittest.mock import ANY
        from uuid import UUID

        from tests.factories import (
            PrincipalFactory,
            TenantFactory,
        )
        from tests.harness import CircuitBreakerEnv

        with CircuitBreakerEnv(tenant_id="t1", principal_id="p1") as env:
            tenant = TenantFactory(tenant_id="t1")
            principal = PrincipalFactory(tenant=tenant, principal_id="p1")
            _ensure_media_buy(
                tenant,
                principal,
                "mb_001",
                webhook_url="https://happy.example.com/webhook",
            )

            env.set_http_response(200)
            service = env.get_service()
            payload = {"adcp_version": "2.3", "impressions": 5000, "spend": 250.0}
            result = service._send_webhook_enhanced(
                tenant_id="t1",
                principal_id="p1",
                media_buy_id="mb_001",
                delivery_payload=payload,
            )

            assert result is True
            post_mock = env.mock["client"].return_value.__enter__.return_value.post
            post_mock.assert_called_once_with(
                ANY,
                "https://happy.example.com/webhook",
                body=ANY,
                headers=ANY,
                timeout=10.0,
            )
            sent = json.loads(post_mock.call_args.kwargs["body"])
            assert sent == {
                "idempotency_key": sent["idempotency_key"],
                "operation_id": f"reporting:{sent['idempotency_key']}",
                "task_id": "delivery:mb_001",
                "task_type": "media_buy_delivery",
                "status": "completed",
                "timestamp": sent["timestamp"],
                "result": {**payload, "sequence_number": 1},
            }
            assert str(UUID(sent["idempotency_key"])) == sent["idempotency_key"]
            assert sent["timestamp"].endswith("Z")

    def test_no_configs_returns_false(self, integration_db):
        """When no PushNotificationConfig exists, _send_webhook_enhanced returns False.

        Covers: UC-004-ALT-WEBHOOK-PUSH-REPORTING-01
        """
        from tests.factories import (
            PrincipalFactory,
            TenantFactory,
        )
        from tests.harness import CircuitBreakerEnv

        with CircuitBreakerEnv(tenant_id="t1", principal_id="p1") as env:
            TenantFactory(tenant_id="t1")
            PrincipalFactory(tenant_id="t1", principal_id="p1")

            service = env.get_service()
            result = service._send_webhook_enhanced(
                tenant_id="t1",
                principal_id="p1",
                media_buy_id="mb_001",
                delivery_payload={"test": "data"},
            )

            assert result is False
            env.mock["client"].return_value.__enter__.return_value.post.assert_not_called()


# ---------------------------------------------------------------------------
# UC-004-EXT-G-01 (_deliver_with_backoff: successful delivery)
# ---------------------------------------------------------------------------


@pytest.mark.requires_db
class TestDeliverWithBackoffSuccess:
    """Successful pinned HTTP delivery records success on circuit breaker.

    Covers: UC-004-EXT-G-01
    """

    def test_successful_delivery_returns_true_records_success(self, integration_db):
        """The pinned POST returns 200 -> _deliver_with_backoff returns True and
        circuit breaker records success.

        Covers: UC-004-EXT-G-01
        """
        from tests.factories import (
            PrincipalFactory,
            TenantFactory,
        )
        from tests.harness import CircuitBreakerEnv

        with CircuitBreakerEnv(tenant_id="t1", principal_id="p1") as env:
            tenant = TenantFactory(tenant_id="t1")
            principal = PrincipalFactory(tenant=tenant, principal_id="p1")
            _ensure_media_buy(
                tenant,
                principal,
                "mb_001",
                webhook_url="https://success.example.com/webhook",
            )

            env.set_http_response(200)
            service = env.get_service()
            result = service._send_webhook_enhanced(
                tenant_id="t1",
                principal_id="p1",
                media_buy_id="mb_001",
                delivery_payload={"impressions": 5000},
            )

            assert result is True

            # Circuit breaker should remain CLOSED (success recorded)
            endpoint_key = "t1:https://success.example.com/webhook"
            state, failure_count = service.get_circuit_breaker_state(endpoint_key)
            assert state == CircuitState.CLOSED
            assert failure_count == 0


# ---------------------------------------------------------------------------
# UC-004-EXT-G-01 (_deliver_with_backoff: retry on 500)
# ---------------------------------------------------------------------------


@pytest.mark.requires_db
class TestDeliverWithBackoffRetry:
    """A 500 response retries with backoff and records circuit-breaker failure.

    Covers: UC-004-EXT-G-01
    """

    def test_500_triggers_retries_and_records_failure(self, integration_db):
        """The pinned POST returns 500 on all attempts -> delivery retries
        max_retries times, then circuit breaker records failure.

        Covers: UC-004-EXT-G-01
        """
        from tests.factories import (
            PrincipalFactory,
            TenantFactory,
        )
        from tests.harness import CircuitBreakerEnv

        with CircuitBreakerEnv(tenant_id="t1", principal_id="p1") as env:
            tenant = TenantFactory(tenant_id="t1")
            principal = PrincipalFactory(tenant=tenant, principal_id="p1")
            _ensure_media_buy(
                tenant,
                principal,
                "mb_001",
                webhook_url="https://failing.example.com/webhook",
            )

            env.set_http_response(500)
            service = env.get_service()
            result = service._send_webhook_enhanced(
                tenant_id="t1",
                principal_id="p1",
                media_buy_id="mb_001",
                delivery_payload={"impressions": 5000},
            )

            assert result is False

            # The pinned POST seam should have been called 3 times (max_retries=3)
            post_mock = env.mock["client"].return_value.__enter__.return_value.post
            assert post_mock.call_count == 3

            # sleep should have been called for backoff (attempts 1 and 2, not before attempt 0)
            assert env.mock["sleep"].call_count == 2

            # Circuit breaker should record failure
            endpoint_key = "t1:https://failing.example.com/webhook"
            state, failure_count = service.get_circuit_breaker_state(endpoint_key)
            assert failure_count == 1


# ---------------------------------------------------------------------------
# UC-004-EXT-G-01 (_deliver_with_backoff: timeout handling)
# ---------------------------------------------------------------------------


@pytest.mark.requires_db
class TestDeliverWithBackoffTimeout:
    """A requests timeout retries with backoff and records failure.

    Covers: UC-004-EXT-G-01
    """

    def test_timeout_triggers_retries_and_records_failure(self, integration_db):
        """requests raises Timeout on all attempts -> retries exhaust,
        circuit breaker records failure.

        Covers: UC-004-EXT-G-01
        """
        import requests

        from tests.factories import (
            PrincipalFactory,
            TenantFactory,
        )
        from tests.harness import CircuitBreakerEnv

        with CircuitBreakerEnv(tenant_id="t1", principal_id="p1") as env:
            tenant = TenantFactory(tenant_id="t1")
            principal = PrincipalFactory(tenant=tenant, principal_id="p1")
            _ensure_media_buy(
                tenant,
                principal,
                "mb_001",
                webhook_url="https://timeout.example.com/webhook",
            )

            env.mock["client"].return_value.__enter__.return_value.post.side_effect = requests.Timeout(
                "Connection timed out"
            )

            service = env.get_service()
            result = service._send_webhook_enhanced(
                tenant_id="t1",
                principal_id="p1",
                media_buy_id="mb_001",
                delivery_payload={"impressions": 5000},
            )

            assert result is False

            # Should have retried 3 times
            post_mock = env.mock["client"].return_value.__enter__.return_value.post
            assert post_mock.call_count == 3

            # Circuit breaker should record failure
            endpoint_key = "t1:https://timeout.example.com/webhook"
            state, failure_count = service.get_circuit_breaker_state(endpoint_key)
            assert failure_count == 1


# ---------------------------------------------------------------------------
# Coverage: is_adjusted notification type (line 239)
# ---------------------------------------------------------------------------


@pytest.mark.requires_db
class TestIsAdjustedNotificationType:
    """send_delivery_webhook with is_adjusted=True sets notification_type='adjusted'.

    Covers: line 239 of webhook_delivery_service.py
    """

    def test_is_adjusted_sets_notification_type_adjusted(self, integration_db):
        """When is_adjusted=True, the payload notification_type is 'adjusted'.

        Covers: webhook_delivery_service.py line 239
        """
        import json
        from datetime import UTC, datetime

        from tests.factories import (
            PrincipalFactory,
            TenantFactory,
        )
        from tests.harness import CircuitBreakerEnv

        with CircuitBreakerEnv(tenant_id="t1", principal_id="p1") as env:
            tenant = TenantFactory(tenant_id="t1")
            principal = PrincipalFactory(tenant=tenant, principal_id="p1")
            _ensure_media_buy(
                tenant,
                principal,
                "mb_adj",
                webhook_url="https://adjusted.example.com/webhook",
            )

            env.set_http_response(200)
            service = env.get_service()
            result = service.send_delivery_webhook(
                media_buy_id="mb_adj",
                tenant_id="t1",
                principal_id="p1",
                reporting_period_start=datetime(2025, 6, 1, tzinfo=UTC),
                reporting_period_end=datetime(2025, 6, 30, tzinfo=UTC),
                impressions=1000,
                spend=50.0,
                is_adjusted=True,
            )

            assert result is True
            post_mock = env.mock["client"].return_value.__enter__.return_value.post
            sent_payload = json.loads(post_mock.call_args.kwargs["body"])
            assert sent_payload["result"]["notification_type"] == "adjusted"
            assert sent_payload["result"]["media_buy_deliveries"][0]["is_adjusted"] is True
            assert sent_payload["result"]["sequence_number"] == 1


# ---------------------------------------------------------------------------
# Coverage: queue full drops webhook (lines 408-409)
# ---------------------------------------------------------------------------


@pytest.mark.requires_db
class TestQueueFullDropsWebhook:
    """When webhook queue is full, _send_webhook_enhanced drops the webhook.

    Covers: lines 408-409 of webhook_delivery_service.py
    """

    def test_queue_full_skips_delivery(self, integration_db):
        """When the per-endpoint queue is at max capacity, enqueue fails
        and delivery is skipped for that endpoint.

        Covers: webhook_delivery_service.py lines 408-409
        """
        from src.services.webhook_delivery_service import WebhookQueue
        from tests.factories import (
            PrincipalFactory,
            TenantFactory,
        )
        from tests.harness import CircuitBreakerEnv

        with CircuitBreakerEnv(tenant_id="t1", principal_id="p1") as env:
            tenant = TenantFactory(tenant_id="t1")
            principal = PrincipalFactory(tenant=tenant, principal_id="p1")
            _ensure_media_buy(
                tenant,
                principal,
                "mb_full",
                webhook_url="https://full-queue.example.com/webhook",
            )

            env.set_http_response(200)
            service = env.get_service()

            # Pre-populate the queue to capacity (use small max_size)
            endpoint_key = "t1:https://full-queue.example.com/webhook"
            small_queue = WebhookQueue(max_size=1)
            small_queue.enqueue({"dummy": "data"})  # Fill it
            service._queues[endpoint_key] = small_queue

            result = service._send_webhook_enhanced(
                tenant_id="t1",
                principal_id="p1",
                media_buy_id="mb_full",
                delivery_payload={"test": "data"},
            )

            assert result is False


# ---------------------------------------------------------------------------
# Coverage: weak webhook secret warning (line 463)
# ---------------------------------------------------------------------------


@pytest.mark.requires_db
class TestWeakSecretNoSignature:
    """Weak webhook secrets fail closed before any outbound request.

    Covers: line 463 of webhook_delivery_service.py
    """

    def test_weak_secret_is_rejected_before_delivery(self, integration_db):
        """A too-short HMAC secret cannot silently downgrade authentication.

        Covers: webhook_delivery_service.py line 463
        """
        from tests.factories import (
            PrincipalFactory,
            TenantFactory,
        )
        from tests.harness import CircuitBreakerEnv

        with CircuitBreakerEnv(tenant_id="t1", principal_id="p1") as env:
            tenant = TenantFactory(tenant_id="t1")
            principal = PrincipalFactory(tenant=tenant, principal_id="p1")
            _ensure_media_buy(
                tenant,
                principal,
                "mb_weak",
                webhook_url="https://weak-secret.example.com/webhook",
                authentication_scheme="HMAC-SHA256",
                credentials="tooshort",
            )

            env.set_http_response(200)
            service = env.get_service()
            result = service._send_webhook_enhanced(
                tenant_id="t1",
                principal_id="p1",
                media_buy_id="mb_weak",
                delivery_payload={"test": "data"},
            )

            assert result is False
            post_mock = env.mock["client"].return_value.__enter__.return_value.post
            post_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Coverage: empty dequeue returns False (line 447)
# ---------------------------------------------------------------------------


@pytest.mark.requires_db
class TestEmptyDequeueReturnsFalse:
    """_deliver_with_backoff returns False when queue is empty.

    Covers: line 447 of webhook_delivery_service.py
    """

    def test_deliver_with_backoff_empty_queue(self, integration_db):
        """Calling _deliver_with_backoff with an empty queue returns False.

        Covers: webhook_delivery_service.py line 447
        """
        from src.services.webhook_delivery_service import CircuitBreaker, WebhookQueue
        from tests.harness import CircuitBreakerEnv

        with CircuitBreakerEnv() as env:
            service = env.get_service()
            cb = CircuitBreaker()
            empty_queue = WebhookQueue()

            result = service._deliver_with_backoff("t1:https://empty.example.com", cb, empty_queue)
            assert result is False
