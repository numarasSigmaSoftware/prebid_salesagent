"""Unit tests for webhook security features (SSRF protection and HMAC authentication)."""

import hashlib
import hmac
import json
import logging
import time
from unittest.mock import ANY, MagicMock, patch

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


class TestRedactUrlCredentials:
    """``_redact_url_credentials`` strips userinfo AND every query-parameter VALUE
    (names kept) before a URL reaches a log line or durable storage.

    Query values are redacted UNCONDITIONALLY rather than against a denylist of
    known credential-parameter names: an allow/deny-list is inherently incomplete
    against an open set of provider conventions (``client_secret``, ``sig``,
    ``X-Amz-Signature``, ...), and this URL is a buyer-configured webhook
    endpoint, not an AdCP request whose exact query values the seller needs.
    """

    def _redact(self, url: str) -> str:
        from src.services.protocol_webhook_service import _redact_url_credentials

        return _redact_url_credentials(url)

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

    def test_url_with_no_query_string_at_all_is_returned_unchanged(self):
        url = "https://example.com/hook"
        assert self._redact(url) == url

    def test_every_query_param_value_is_redacted_regardless_of_name(self):
        """Non-credential-sounding names are redacted too -- there is no allowlist
        of 'safe' names, since the seller has no way to distinguish a buyer's
        opaque webhook-routing token from a genuinely harmless value."""
        redacted = self._redact("https://example.com/hook?media_buy_id=mb_1&format=json")
        assert "mb_1" not in redacted
        assert "json" not in redacted
        assert redacted == "https://example.com/hook?media_buy_id=REDACTED&format=REDACTED"

    @pytest.mark.parametrize(
        "param_name",
        [
            "token",
            "client_secret",
            "auth_token",
            "sig",
            "X-Amz-Credential",
            "X-Amz-Signature",
            "some_provider_specific_credential_name",
        ],
    )
    def test_arbitrary_provider_specific_param_names_are_redacted(self, param_name):
        """The exact names a review found missing from the old denylist -- and an
        invented one, proving this isn't just a longer denylist in disguise."""
        redacted = self._redact(f"https://example.com/hook?{param_name}=leaked-value")
        assert "leaked-value" not in redacted
        assert param_name in redacted, "the parameter NAME must survive for debugging shape"

    def test_parameter_names_survive_only_values_are_redacted(self):
        redacted = self._redact("https://example.com/hook?media_buy_id=mb_1")
        assert "media_buy_id=REDACTED" in redacted

    def test_userinfo_and_query_credentials_both_redacted_together(self):
        redacted = self._redact("https://buyer:s3cr3t@example.com/hook?token=also-leaked")
        assert "buyer" not in redacted
        assert "s3cr3t" not in redacted
        assert "also-leaked" not in redacted

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
        assert "buyer" not in caplog.text, f"username leaked into the log: {caplog.text}"
        # The real outbound request must still receive the FULL, unredacted URL --
        # redaction is a log-output concern only, never a functional rewrite. Count
        # and the url arg checked atomically (not a separate call_args read).
        mock_post.assert_called_once_with(credentialed_url, headers=ANY, timeout=ANY, json=ANY)

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
        assert "buyer" not in caplog.text, f"username leaked into the log: {caplog.text}"
        # The real outbound request must still receive the FULL, unredacted URL.
        mock_post.assert_called_once_with(credentialed_url, headers=ANY, timeout=ANY, json=ANY)

    @pytest.mark.asyncio
    async def test_retry_send_log_line_omits_a_bare_username(self, caplog):
        """Username-only URLs (no password) are credential material too -- a fix that only
        stripped 'user:pass@' and left a bare 'user@' behind would pass every other test here."""
        from src.services.protocol_webhook_service import ProtocolWebhookService

        service = ProtocolWebhookService()
        username_only_url = "https://buyer-identity@example.com/hook"
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
                url=username_only_url,
                payload={"task_id": "media-buy-1"},
                headers={},
                metadata={
                    "task_type": "media_buy_delivery",
                    "tenant_id": "tenant-1",
                    "principal_id": "principal-1",
                    "media_buy_id": "media-buy-1",
                },
            )

        assert "buyer-identity" not in caplog.text, f"bare username leaked into the log: {caplog.text}"
        mock_post.assert_called_once_with(username_only_url, headers=ANY, timeout=ANY, json=ANY)

    @pytest.mark.asyncio
    async def test_retry_send_log_line_omits_query_credentials(self, caplog):
        """Every prior case in this class used a userinfo-only URL, so a regression that
        replaced _redact_url_credentials with a userinfo-only implementation at THIS log
        site would have passed all of them. No userinfo here -- query-only."""
        from src.services.protocol_webhook_service import ProtocolWebhookService

        service = ProtocolWebhookService()
        query_credentialed_url = "https://example.com/hook?token=query-leak-retry"
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
                url=query_credentialed_url,
                payload={"task_id": "media-buy-1"},
                headers={},
                metadata={
                    "task_type": "media_buy_delivery",
                    "tenant_id": "tenant-1",
                    "principal_id": "principal-1",
                    "media_buy_id": "media-buy-1",
                },
            )

        assert "query-leak-retry" not in caplog.text, f"query credential leaked into the log: {caplog.text}"
        mock_post.assert_called_once_with(query_credentialed_url, headers=ANY, timeout=ANY, json=ANY)

    @pytest.mark.asyncio
    async def test_sanitized_config_log_line_omits_query_credentials(self, caplog):
        """Same regression class as above, for the OTHER log site (send_notification's
        safe_config line, which reads push_notification_config.url directly)."""
        from src.core.database.models import PushNotificationConfig
        from src.services.protocol_webhook_service import ProtocolWebhookService

        service = ProtocolWebhookService()
        query_credentialed_url = "https://example.com/hook?token=query-leak-config"
        config = PushNotificationConfig(
            url=query_credentialed_url,
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

        assert "query-leak-config" not in caplog.text, f"query credential leaked into the log: {caplog.text}"
        mock_post.assert_called_once_with(query_credentialed_url, headers=ANY, timeout=ANY, json=ANY)


class TestDurableDeliveryLogRedactsCredentials:
    """WebhookDeliveryLog.webhook_url is persisted, not just logged -- durable storage is a
    stronger exposure than a log line (DB access, backups, replicas). _delivery_log_context
    is the single builder for that persisted value, so it is the point that must redact.
    """

    def test_context_webhook_url_omits_userinfo(self):
        from src.services.protocol_webhook_service import _delivery_log_context

        context = _delivery_log_context(
            log_id="log-1",
            url="https://buyer:s3cr3t-password@example.com/hook",
            task_type="media_buy_delivery",
            tenant_id="tenant-1",
            principal_id="principal-1",
            media_buy_id="media-buy-1",
            idempotency_key=None,
            sequence_number=1,
            notification_type="scheduled",
            payload_size_bytes=100,
        )
        assert context is not None
        assert "buyer" not in context.webhook_url
        assert "s3cr3t-password" not in context.webhook_url
        assert context.webhook_url == "https://REDACTED@example.com/hook"

    def test_context_webhook_url_omits_sensitive_query_param_values(self):
        from src.services.protocol_webhook_service import _delivery_log_context

        context = _delivery_log_context(
            log_id="log-1",
            url="https://example.com/hook?token=leaked-in-db",
            task_type="media_buy_delivery",
            tenant_id="tenant-1",
            principal_id="principal-1",
            media_buy_id="media-buy-1",
            idempotency_key=None,
            sequence_number=1,
            notification_type="scheduled",
            payload_size_bytes=100,
        )
        assert context is not None
        assert "leaked-in-db" not in context.webhook_url

    def test_write_delivery_log_forwards_the_redacted_url_to_the_repository(self):
        """Exercises _write_delivery_log FOR REAL (only get_db_session and DeliveryRepository
        are mocked, one layer below it) and inspects the value that reaches
        DeliveryRepository.create_log(webhook_url=...) -- the actual persistence boundary.

        The earlier version of this test mocked _write_delivery_log itself and only checked
        the already-redacted _DeliveryLogContext passed INTO it, so a regression that stopped
        _write_delivery_log from forwarding context.webhook_url to create_log (e.g. a typo'd
        keyword, or reading from a stale local instead of the context) would not have been
        caught. This drives _write_delivery_log's real body.
        """
        from src.services.protocol_webhook_service import ProtocolWebhookService, _delivery_log_context

        service = ProtocolWebhookService()
        context = _delivery_log_context(
            log_id="log-1",
            url="https://buyer:s3cr3t-password@example.com/hook?token=also-leaked",
            task_type="media_buy_delivery",
            tenant_id="tenant-1",
            principal_id="principal-1",
            media_buy_id="media-buy-1",
            idempotency_key=None,
            sequence_number=1,
            notification_type="scheduled",
            payload_size_bytes=100,
        )
        assert context is not None

        mock_session = MagicMock()
        mock_session_cm = MagicMock()
        mock_session_cm.__enter__.return_value = mock_session
        mock_session_cm.__exit__.return_value = None
        mock_repo_instance = MagicMock()

        with (
            patch("src.services.protocol_webhook_service.get_db_session", return_value=mock_session_cm),
            patch(
                "src.services.protocol_webhook_service.DeliveryRepository", return_value=mock_repo_instance
            ) as mock_repo_class,
        ):
            service._write_delivery_log(context=context, status="pending", attempt_count=0)

        mock_repo_class.assert_called_once_with(mock_session, "tenant-1")
        # Count and the forwarded webhook_url checked atomically in one call --
        # every other kwarg is asserted too so a swapped/dropped field is also caught.
        assert "buyer" not in context.webhook_url
        assert "s3cr3t-password" not in context.webhook_url
        assert "also-leaked" not in context.webhook_url
        mock_repo_instance.create_log.assert_called_once_with(
            log_id="log-1",
            principal_id="principal-1",
            media_buy_id="media-buy-1",
            webhook_url=context.webhook_url,
            task_type="media_buy_delivery",
            status="pending",
            idempotency_key=None,
            sequence_number=1,
            notification_type="scheduled",
            attempt_count=0,
            http_status_code=None,
            error_message=None,
            payload_size_bytes=100,
            response_time_ms=None,
            completed_at=None,
            next_retry_at=None,
        )
