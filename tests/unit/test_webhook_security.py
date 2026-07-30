"""Unit tests for webhook security features (SSRF protection and HMAC authentication)."""

import hashlib
import hmac
import json
import time
from types import SimpleNamespace
from unittest.mock import ANY, MagicMock, patch

import pytest
from a2a.types import TaskState, TaskStatus, TaskStatusUpdateEvent
from adcp.types import TaskType

from src.core.webhook_authenticator import WebhookAuthenticator
from src.core.webhook_validator import (
    WEBHOOK_TASK_TYPE_FALLBACK,
    WebhookURLValidator,
    validate_webhook_task_type,
)


@pytest.fixture
def public_dns(monkeypatch):
    """Resolve the documentation host without relying on external DNS."""
    monkeypatch.setattr(
        "src.core.security.url_validator.socket.gethostbyname",
        lambda hostname: "93.184.216.34",
    )


class TestValidateWebhookTaskType:
    """Coercion of untrusted action labels to SDK-accepted TaskType values."""

    @pytest.mark.parametrize("valid", [m.value for m in TaskType])
    def test_valid_tasktype_returned_unchanged(self, valid):
        """Every TaskType enum member passes through verbatim."""
        assert validate_webhook_task_type(valid) == valid

    @pytest.mark.parametrize(
        "invalid",
        # media_buy_delivery is now a valid TaskType member (adcp 6.6 / spec 3.1.1), so it no
        # longer coerces to the fallback — dropped from the invalid-label set.
        ["delivery_report", "unknown", "", "not_a_task"],
    )
    def test_non_tasktype_coerced_to_fallback(self, invalid):
        """Non-members are coerced to the default fallback."""
        assert validate_webhook_task_type(invalid) == WEBHOOK_TASK_TYPE_FALLBACK
        assert WEBHOOK_TASK_TYPE_FALLBACK == "update_media_buy"

    def test_custom_fallback_honored(self):
        """The fallback is overridable for callers with a different default."""
        assert validate_webhook_task_type("bogus", fallback="sync_creatives") == "sync_creatives"

    def test_fallback_must_be_valid_caller_choice(self):
        """A valid label ignores the fallback entirely."""
        assert validate_webhook_task_type("sync_creatives", fallback="update_media_buy") == "sync_creatives"


class TestWebhookURLValidator:
    """Test SSRF protection in webhook URL validation."""

    def test_valid_public_https_url(self, public_dns):
        """Valid public HTTPS URLs should pass."""
        is_valid, error = WebhookURLValidator.validate_webhook_url("https://example.com/webhook")
        assert is_valid
        assert error == ""

    def test_valid_public_http_url(self, public_dns):
        """Valid public HTTP URLs should pass (for testing)."""
        is_valid, error = WebhookURLValidator.validate_webhook_url("http://example.com/webhook")
        assert is_valid
        assert error == ""

    def test_blocks_localhost(self):
        """Should block localhost."""
        is_valid, error = WebhookURLValidator.validate_webhook_url("http://localhost:3000/webhook")
        assert not is_valid
        assert "blocked" in error.lower()

    def test_blocks_127_0_0_1(self):
        """Should block 127.0.0.1."""
        is_valid, error = WebhookURLValidator.validate_webhook_url("http://127.0.0.1:8080/webhook")
        assert not is_valid
        assert "127.0.0.0/8" in error

    def test_blocks_private_network_10(self):
        """Should block 10.0.0.0/8 private network."""
        is_valid, error = WebhookURLValidator.validate_webhook_url("http://10.0.0.5/webhook")
        assert not is_valid
        assert "10.0.0.0/8" in error

    def test_blocks_private_network_192(self):
        """Should block 192.168.0.0/16 private network."""
        is_valid, error = WebhookURLValidator.validate_webhook_url("http://192.168.1.1/webhook")
        assert not is_valid
        assert "192.168.0.0/16" in error

    def test_blocks_private_network_172(self):
        """Should block 172.16.0.0/12 private network."""
        is_valid, error = WebhookURLValidator.validate_webhook_url("http://172.16.0.1/webhook")
        assert not is_valid
        assert "172.16.0.0/12" in error

    def test_blocks_link_local(self):
        """Should block 169.254.0.0/16 link-local (AWS metadata service)."""
        # Use a non-hostname-allowlist IP so the CIDR path is graded (169.254.169.254
        # is also in BLOCKED_HOSTNAMES and short-circuits before network match).
        is_valid, error = WebhookURLValidator.validate_webhook_url("http://169.254.1.1/webhook")
        assert not is_valid
        assert "169.254.0.0/16" in error

    def test_blocks_aws_metadata_hostname(self):
        """Literal metadata IP hostname is blocked by hostname allowlist."""
        is_valid, error = WebhookURLValidator.validate_webhook_url("http://169.254.169.254/latest/meta-data")
        assert not is_valid
        assert "blocked" in error.lower()

    def test_blocks_metadata_hostname(self):
        """Should block cloud metadata hostnames."""
        is_valid, error = WebhookURLValidator.validate_webhook_url("http://metadata.google.internal/webhook")
        assert not is_valid
        assert "blocked" in error.lower()

    def test_requires_http_or_https(self):
        """Should reject non-HTTP protocols."""
        is_valid, error = WebhookURLValidator.validate_webhook_url("ftp://example.com/webhook")
        assert not is_valid
        assert "http" in error.lower()

    def test_requires_hostname(self):
        """Should reject URLs without hostname."""
        is_valid, error = WebhookURLValidator.validate_webhook_url("http:///webhook")
        assert not is_valid
        assert "hostname" in error.lower()

    def test_invalid_url_format(self):
        """Should reject malformed URLs."""
        is_valid, error = WebhookURLValidator.validate_webhook_url("not-a-url")
        assert not is_valid
        assert error != ""

    def test_blocks_embedded_url_credentials(self, public_dns):
        """Authentication belongs in the config, never in a loggable URL."""
        is_valid, error = WebhookURLValidator.validate_webhook_url("https://svc:hunter2@example.com/webhook")
        assert not is_valid
        assert "credentials" in error.lower()

    def test_protocol_test_host_override_is_exact_and_development_only(self, monkeypatch):
        """The Docker callback seam cannot admit arbitrary private destinations."""
        monkeypatch.setenv("ENVIRONMENT", "development")
        monkeypatch.setenv("ADCP_WEBHOOK_TEST_HOST", "tests")
        monkeypatch.setattr(
            "src.core.security.url_validator.socket.gethostbyname",
            lambda hostname: "172.18.0.5",
        )

        assert WebhookURLValidator.validate_protocol_webhook_url("http://tests:8080/webhook") == (True, "")
        is_valid, _error = WebhookURLValidator.validate_protocol_webhook_url("http://internal-service:8080/webhook")
        assert not is_valid
        malformed_is_valid, _error = WebhookURLValidator.validate_protocol_webhook_url(
            "http://tests:not-a-port/webhook"
        )
        assert not malformed_is_valid

    def test_registration_and_protocol_gates_agree_on_the_dev_test_host(self, monkeypatch):
        """Both gates must admit the SAME development-only callback host.

        The E2E capture server is registered through the registration gate and
        delivered to through the protocol gate. When only the protocol gate honored
        the seam, every E2E webhook flow failed at REGISTRATION (VALIDATION_ERROR on
        reporting_webhook.url / push_notification_config.url) and no delivery was
        ever attempted. Covers both configured pairings: "tests" in-network
        (docker-compose.e2e.yml), "host.docker.internal" standalone (e2e/conftest.py).
        """
        monkeypatch.setenv("ENVIRONMENT", "development")
        monkeypatch.setenv("ADCP_TESTING", "true")
        for host in ("tests", "host.docker.internal"):
            monkeypatch.setenv("ADCP_WEBHOOK_TEST_HOST", host)
            url = f"http://{host}:9999/webhook"
            assert WebhookURLValidator.validate_webhook_url_registration(url) == (True, ""), (
                f"registration gate rejected the configured dev callback host {host!r}"
            )
            assert WebhookURLValidator.validate_protocol_webhook_url(url) == (True, ""), (
                f"protocol gate rejected the configured dev callback host {host!r}"
            )

    def test_registration_seam_is_exact_and_never_widens_production(self, monkeypatch):
        """The registration seam admits exactly one host, in development only."""
        monkeypatch.setenv("ADCP_TESTING", "true")
        monkeypatch.setenv("ADCP_WEBHOOK_TEST_HOST", "host.docker.internal")

        monkeypatch.setenv("ENVIRONMENT", "development")
        # A different private host is NOT admitted just because a seam is configured.
        assert not WebhookURLValidator.validate_webhook_url_registration("http://10.0.0.5/webhook")[0]
        # Nor is the seam host when the URL smuggles credentials.
        assert not WebhookURLValidator.validate_webhook_url_registration(
            "http://user:pw@host.docker.internal:9999/webhook"
        )[0]

        # Production ignores the seam even with the env var still set.
        monkeypatch.setenv("ENVIRONMENT", "production")
        assert not WebhookURLValidator.validate_webhook_url_registration("http://host.docker.internal:9999/webhook")[0]

    def test_protocol_callback_requires_https_outside_development(self, monkeypatch, public_dns):
        """Production never sends payloads or legacy credentials over plaintext HTTP."""
        monkeypatch.setenv("ENVIRONMENT", "production")

        is_valid, error = WebhookURLValidator.validate_protocol_webhook_url("http://example.com/webhook")

        assert not is_valid
        assert "https" in error.lower()
        assert WebhookURLValidator.validate_protocol_webhook_url("https://example.com/webhook") == (True, "")

    def test_protocol_public_http_is_allowed_in_development(self, monkeypatch, public_dns):
        """Development may use public HTTP independently of the private-host E2E seam."""
        monkeypatch.setenv("ENVIRONMENT", "development")

        assert WebhookURLValidator.validate_protocol_webhook_url("http://example.com/webhook") == (True, "")

    def test_validate_for_testing_allows_localhost(self):
        """Testing mode should allow localhost when enabled."""
        is_valid, error = WebhookURLValidator.validate_for_testing(
            "http://localhost:3001/webhook", allow_localhost=True
        )
        assert is_valid
        assert error == ""

    def test_validate_for_testing_blocks_private_networks(self):
        """Testing mode should still block private networks even with allow_localhost."""
        is_valid, error = WebhookURLValidator.validate_for_testing("http://192.168.1.1/webhook", allow_localhost=True)
        assert not is_valid

    def test_production_requires_https(self, monkeypatch):
        """Production registration/outbound must reject plain HTTP."""
        monkeypatch.setenv("ENVIRONMENT", "production")
        monkeypatch.delenv("ADCP_TESTING", raising=False)
        is_valid, error = WebhookURLValidator.validate_webhook_url_registration("http://buyer.example.com/hook")
        assert not is_valid
        assert "https" in error.lower()

    def test_production_accepts_https_registration(self, monkeypatch):
        monkeypatch.setenv("ENVIRONMENT", "production")
        monkeypatch.delenv("ADCP_TESTING", raising=False)
        is_valid, error = WebhookURLValidator.validate_webhook_url_registration("https://buyer.example.com/hook")
        assert is_valid
        assert error == ""

    def test_blocks_cgnat_range(self):
        is_valid, error = WebhookURLValidator.validate_webhook_url("http://100.64.1.1/webhook")
        assert not is_valid
        assert "100.64.0.0/10" in error

    def test_blocks_multicast_range(self):
        is_valid, error = WebhookURLValidator.validate_webhook_url("http://224.0.0.1/webhook")
        assert not is_valid
        assert "224.0.0.0/4" in error

    def test_blocks_ipv6_multicast_range(self):
        is_valid, error = WebhookURLValidator.validate_webhook_url("http://[ff02::1]/")
        assert not is_valid
        assert "ff00::/8" in error

    def test_blocks_nat64_well_known_prefix(self):
        is_valid, error = WebhookURLValidator.validate_webhook_url("http://[64:ff9b::a9fe:a9fe]/")
        assert not is_valid
        assert "64:ff9b::/96" in error


class TestWebhookAuthenticator:
    """Test HMAC-SHA256 webhook authentication."""

    def test_sign_payload(self):
        """Should generate signature with timestamp."""
        payload = {"event": "test", "data": "value"}
        secret = "test_secret_key"

        headers = WebhookAuthenticator.sign_payload(payload, secret)

        assert "X-Webhook-Signature" in headers
        assert "X-Webhook-Timestamp" in headers
        assert headers["X-Webhook-Signature"].startswith("sha256=")
        assert headers["X-Webhook-Timestamp"].isdigit()

    def test_sign_payload_deterministic(self):
        """Same payload and secret should generate different signatures (due to timestamp)."""
        payload = {"event": "test"}
        secret = "secret"

        headers1 = WebhookAuthenticator.sign_payload(payload, secret)
        time.sleep(1.1)  # Delay to ensure different timestamp (at least 1 second)
        headers2 = WebhookAuthenticator.sign_payload(payload, secret)

        # Timestamps should be different
        assert headers1["X-Webhook-Timestamp"] != headers2["X-Webhook-Timestamp"]
        # Signatures should be different (timestamp is part of signed message)
        assert headers1["X-Webhook-Signature"] != headers2["X-Webhook-Signature"]

    def test_sign_payload_with_different_secrets(self):
        """Different secrets should produce different signatures."""
        payload = {"event": "test"}

        headers1 = WebhookAuthenticator.sign_payload(payload, "secret1")
        headers2 = WebhookAuthenticator.sign_payload(payload, "secret2")

        assert headers1["X-Webhook-Signature"] != headers2["X-Webhook-Signature"]

    def test_verify_signature_valid(self):
        """Should verify valid signature."""
        payload = {"event": "test", "data": "value"}
        secret = "test_secret"

        # Create signature
        payload_str = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        timestamp = str(int(time.time()))
        signed_payload = f"{timestamp}.{payload_str}"
        signature = (
            "sha256=" + hmac.new(secret.encode("utf-8"), signed_payload.encode("utf-8"), hashlib.sha256).hexdigest()
        )

        # Verify
        is_valid = WebhookAuthenticator.verify_signature(payload_str, signature, timestamp, secret)
        assert is_valid

    def test_verify_signature_invalid_secret(self):
        """Should reject signature with wrong secret."""
        payload = {"event": "test"}
        payload_str = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        timestamp = str(int(time.time()))

        # Sign with one secret
        signed_payload = f"{timestamp}.{payload_str}"
        signature = "sha256=" + hmac.new(b"secret1", signed_payload.encode("utf-8"), hashlib.sha256).hexdigest()

        # Verify with different secret
        is_valid = WebhookAuthenticator.verify_signature(payload_str, signature, timestamp, "secret2")
        assert not is_valid

    def test_verify_signature_replay_protection(self):
        """Should reject old timestamps (replay attack prevention)."""
        payload = {"event": "test"}
        payload_str = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        secret = "test_secret"

        # Create signature with old timestamp (10 minutes ago)
        old_timestamp = str(int(time.time()) - 600)
        signed_payload = f"{old_timestamp}.{payload_str}"
        signature = (
            "sha256=" + hmac.new(secret.encode("utf-8"), signed_payload.encode("utf-8"), hashlib.sha256).hexdigest()
        )

        # Should reject (default tolerance is 300 seconds / 5 minutes)
        is_valid = WebhookAuthenticator.verify_signature(payload_str, signature, old_timestamp, secret)
        assert not is_valid

    def test_verify_signature_custom_tolerance(self):
        """Should accept old timestamps if tolerance allows."""
        payload = {"event": "test"}
        payload_str = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        secret = "test_secret"

        # Create signature with timestamp 10 minutes ago
        old_timestamp = str(int(time.time()) - 600)
        signed_payload = f"{old_timestamp}.{payload_str}"
        signature = (
            "sha256=" + hmac.new(secret.encode("utf-8"), signed_payload.encode("utf-8"), hashlib.sha256).hexdigest()
        )

        # Should accept with large tolerance
        is_valid = WebhookAuthenticator.verify_signature(
            payload_str, signature, old_timestamp, secret, tolerance_seconds=3600
        )
        assert is_valid

    def test_roundtrip_sign_and_verify(self):
        """Should successfully sign and verify."""
        payload = {"event": "creative_approved", "creative_id": "cr_123", "status": "active"}
        secret = "super_secret_key_12345"

        # Sign
        headers = WebhookAuthenticator.sign_payload(payload, secret)
        payload_str = json.dumps(payload, separators=(",", ":"), sort_keys=True)

        # Verify
        is_valid = WebhookAuthenticator.verify_signature(
            payload_str, headers["X-Webhook-Signature"], headers["X-Webhook-Timestamp"], secret
        )
        assert is_valid

    def test_signature_without_sha256_prefix(self):
        """Should handle signatures without sha256= prefix."""
        payload = {"event": "test"}
        payload_str = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        secret = "test_secret"
        timestamp = str(int(time.time()))

        # Create signature without prefix
        signed_payload = f"{timestamp}.{payload_str}"
        signature = hmac.new(secret.encode("utf-8"), signed_payload.encode("utf-8"), hashlib.sha256).hexdigest()

        # Should still verify
        is_valid = WebhookAuthenticator.verify_signature(payload_str, signature, timestamp, secret)
        assert is_valid

    def test_tampered_payload(self):
        """Should reject tampered payload."""
        payload = {"event": "test", "amount": 100}
        secret = "test_secret"

        # Sign original payload
        headers = WebhookAuthenticator.sign_payload(payload, secret)

        # Tamper with payload
        tampered_payload = {"event": "test", "amount": 999999}
        tampered_str = json.dumps(tampered_payload, separators=(",", ":"), sort_keys=True)

        # Should reject
        is_valid = WebhookAuthenticator.verify_signature(
            tampered_str, headers["X-Webhook-Signature"], headers["X-Webhook-Timestamp"], secret
        )
        assert not is_valid


class TestProtocolWebhookDeliverySecurity:
    """The actual protocol sender enforces URL safety at the connection seam."""

    @staticmethod
    def _event() -> TaskStatusUpdateEvent:
        return TaskStatusUpdateEvent(
            task_id="task_security",
            context_id="ctx_security",
            status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED),
        )

    @pytest.mark.asyncio
    async def test_private_destination_never_reaches_http_client(self):
        from src.services.protocol_webhook_service import ProtocolWebhookService

        service = ProtocolWebhookService()
        service._session.post = MagicMock()
        config = SimpleNamespace(
            url="http://127.0.0.1:8999/internal",
            authentication_type=None,
            authentication_token=None,
        )

        sent = await service.send_notification(config, self._event(), {"task_type": "create_media_buy"})

        assert sent is False
        service._session.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_delivery_disables_redirect_following(self):
        from src.services.protocol_webhook_service import ProtocolWebhookService

        service = ProtocolWebhookService()
        response = MagicMock(status_code=204)
        service._session.post = MagicMock(return_value=response)
        config = SimpleNamespace(
            url="https://buyer.example/webhook",
            authentication_type=None,
            authentication_token=None,
        )

        with patch.object(WebhookURLValidator, "validate_protocol_webhook_url", return_value=(True, "")):
            sent = await service.send_notification(config, self._event(), {"task_type": "create_media_buy"})

        assert sent is True
        service._session.post.assert_called_once_with(
            "https://buyer.example/webhook",
            json=ANY,
            headers=ANY,
            timeout=10.0,
            allow_redirects=False,
        )

    @pytest.mark.asyncio
    async def test_redirect_response_is_not_reported_as_success(self):
        from src.services.protocol_webhook_service import ProtocolWebhookService

        service = ProtocolWebhookService()
        response = MagicMock(status_code=302)
        service._session.post = MagicMock(return_value=response)
        config = SimpleNamespace(
            url="https://buyer.example/webhook",
            authentication_type=None,
            authentication_token=None,
        )

        with patch.object(WebhookURLValidator, "validate_protocol_webhook_url", return_value=(True, "")):
            sent = await service.send_notification(config, self._event(), {"task_type": "create_media_buy"})

        assert sent is False
        service._session.post.assert_called_once_with(
            "https://buyer.example/webhook",
            json=ANY,
            headers=ANY,
            timeout=10.0,
            allow_redirects=False,
        )
