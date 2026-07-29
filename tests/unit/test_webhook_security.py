"""Unit tests for webhook security features (SSRF protection and HMAC authentication)."""

import hashlib
import hmac
import json
import logging
import time
from unittest.mock import MagicMock, patch

import pytest
from adcp.types import TaskType

from src.core.webhook_authenticator import WebhookAuthenticator
from src.core.webhook_validator import (
    WEBHOOK_TASK_TYPE_FALLBACK,
    WebhookURLValidator,
    validate_webhook_task_type,
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

    @pytest.fixture(autouse=True)
    def _stable_dns(self, monkeypatch):
        def _resolve(hostname: str) -> str:
            if hostname == "example.com":
                return "93.184.216.34"
            return hostname

        monkeypatch.setattr("src.core.security.url_validator.socket.gethostbyname", _resolve)

    def test_valid_public_https_url(self):
        """Valid public HTTPS URLs should pass."""
        is_valid, error = WebhookURLValidator.validate_webhook_url("https://example.com/webhook")
        assert is_valid
        assert error == ""

    def test_valid_public_http_url(self):
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
        assert "loopback" in error.lower() or "private" in error.lower() or "internal" in error.lower()

    def test_blocks_private_network_10(self):
        """Should block 10.0.0.0/8 private network."""
        is_valid, error = WebhookURLValidator.validate_webhook_url("http://10.0.0.5/webhook")
        assert not is_valid
        assert "private" in error.lower() or "internal" in error.lower()

    def test_blocks_private_network_192(self):
        """Should block 192.168.0.0/16 private network."""
        is_valid, error = WebhookURLValidator.validate_webhook_url("http://192.168.1.1/webhook")
        assert not is_valid
        assert "private" in error.lower() or "internal" in error.lower()

    def test_blocks_private_network_172(self):
        """Should block 172.16.0.0/12 private network."""
        is_valid, error = WebhookURLValidator.validate_webhook_url("http://172.16.0.1/webhook")
        assert not is_valid
        assert "private" in error.lower() or "internal" in error.lower()

    def test_blocks_link_local(self):
        """Should block 169.254.0.0/16 link-local (AWS metadata service)."""
        is_valid, error = WebhookURLValidator.validate_webhook_url("http://169.254.169.254/latest/meta-data")
        assert not is_valid
        assert "link" in error.lower() or "private" in error.lower() or "blocked" in error.lower()

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


class TestRedactUrlForLogging:
    """``_redact_url_for_logging`` strips userinfo credentials before a URL reaches a log line."""

    def _redact(self, url: str) -> str:
        from src.services.protocol_webhook_service import _redact_url_for_logging

        return _redact_url_for_logging(url)

    def test_strips_username_and_password(self):
        redacted = self._redact("https://buyer:s3cr3t@example.com/hook")
        assert "buyer" not in redacted
        assert "s3cr3t" not in redacted
        assert redacted == "https://REDACTED@example.com/hook"

    def test_preserves_port_alongside_redaction(self):
        redacted = self._redact("https://buyer:s3cr3t@example.com:8443/hook")
        assert "s3cr3t" not in redacted
        assert redacted == "https://REDACTED@example.com:8443/hook"

    def test_username_only_is_also_redacted(self):
        """Even a bare username (no password) is credential material worth hiding."""
        redacted = self._redact("https://buyer@example.com/hook")
        assert "buyer" not in redacted
        assert redacted == "https://REDACTED@example.com/hook"

    def test_url_without_userinfo_is_returned_unchanged(self):
        url = "https://example.com/hook?token=abc"
        assert self._redact(url) == url

    def test_malformed_url_falls_back_to_a_safe_placeholder(self):
        """A parse failure must never smuggle the raw (possibly credentialed) string through.

        ``urlparse`` itself is lenient and rarely raises, but a userinfo-bearing URL with
        a non-numeric port raises ValueError lazily, from the ``.port`` property access --
        exactly the case where fail-open (returning the string unchanged) would leak
        the credentials this function exists to hide.
        """
        assert self._redact("http://user:pass@example.com:notaport/hook") == "REDACTED"

    def test_does_not_mutate_the_input_string(self):
        """Confidence check: redaction must be a pure function, not an in-place rewrite --
        the caller still needs the ORIGINAL url for the real outbound request."""
        original = "https://buyer:s3cr3t@example.com/hook"
        self._redact(original)
        assert original == "https://buyer:s3cr3t@example.com/hook"


class TestCredentialsNeverReachTheLog:
    """The two ``protocol_webhook_service`` log sites that emit a webhook URL must never
    emit the userinfo credentials embedded in it -- even though ``authentication_token``
    is already (correctly) excluded, the URL itself can carry the same class of secret.
    """

    @pytest.mark.asyncio
    async def test_retry_send_log_line_omits_url_credentials(self, caplog):
        from src.services.protocol_webhook_service import ProtocolWebhookService

        service = ProtocolWebhookService()
        credentialed_url = "https://buyer:s3cr3t-password@example.com/hook"
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "OK"
        mock_response.raise_for_status = lambda: None

        with (
            patch.object(service, "_write_delivery_log"),
            patch.object(service._session, "post", return_value=mock_response) as mock_post,
            caplog.at_level(logging.INFO, logger="src.services.protocol_webhook_service"),
        ):
            await service._send_with_retry_and_logging(
                url=credentialed_url,
                payload={"task_id": "media-buy-1"},
                headers={},
                metadata={
                    "task_type": "media_buy_delivery",
                    "tenant_id": "tenant-1",
                    "principal_id": "principal-1",
                    "media_buy_id": "media-buy-1",
                },
            )

        assert "s3cr3t-password" not in caplog.text, f"credential leaked into the log: {caplog.text}"
        # The real outbound request must still receive the FULL, unredacted URL --
        # redaction is a log-output concern only, never a functional rewrite.
        mock_post.assert_called_once()
        assert mock_post.call_args.args[0] == credentialed_url

    @pytest.mark.asyncio
    async def test_sanitized_config_log_line_omits_url_credentials(self, caplog):
        from src.core.database.models import PushNotificationConfig
        from src.services.protocol_webhook_service import ProtocolWebhookService

        service = ProtocolWebhookService()
        credentialed_url = "https://buyer:s3cr3t-password@example.com/hook"
        config = PushNotificationConfig(
            url=credentialed_url,
            authentication_type=None,
            authentication_token=None,
        )
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "OK"
        mock_response.raise_for_status = lambda: None

        with (
            patch.object(service, "_write_delivery_log"),
            patch.object(service._session, "post", return_value=mock_response) as mock_post,
            caplog.at_level(logging.INFO, logger="src.services.protocol_webhook_service"),
        ):
            await service.send_notification(
                push_notification_config=config,
                payload={"task_id": "media-buy-1"},
                metadata={
                    "task_type": "media_buy_delivery",
                    "tenant_id": "tenant-1",
                    "principal_id": "principal-1",
                    "media_buy_id": "media-buy-1",
                },
            )

        assert "s3cr3t-password" not in caplog.text, f"credential leaked into the log: {caplog.text}"
        # The real outbound request must still receive the FULL, unredacted URL.
        mock_post.assert_called_once()
        assert mock_post.call_args.args[0] == credentialed_url
