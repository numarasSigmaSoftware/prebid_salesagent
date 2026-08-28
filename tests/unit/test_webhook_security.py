"""Unit tests for webhook security features (SSRF protection and HMAC authentication)."""

import hashlib
import hmac
import json
import time

import pytest
from adcp.types import TaskType

from src.core.webhook_authenticator import WebhookAuthenticator
from src.core.webhook_validator import (
    WEBHOOK_TASK_TYPE_FALLBACK,
    WebhookURLValidator,
    validate_webhook_task_type,
)
from tests.helpers.webhook_signing import webhook_signing_jwk_json


@pytest.fixture(autouse=True)
def _default_webhook_signing_key(monkeypatch):
    monkeypatch.setenv("ADCP_WEBHOOK_SIGNING_JWK", webhook_signing_jwk_json())
    monkeypatch.setenv("ADCP_WEBHOOK_SIGNING_JWKS_URI", "https://seller.example/.well-known/jwks.json")


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

    def test_valid_public_https_url(self):
        """Valid public HTTPS URLs should pass."""
        from unittest.mock import patch

        with patch("src.core.security.url_validator._resolve_ips", return_value=["93.184.216.34"]):
            is_valid, error = WebhookURLValidator.validate_webhook_url("https://example.com/webhook")
        assert is_valid
        assert error == ""

    def test_valid_public_http_url(self):
        """Valid public HTTP URLs should pass (for testing)."""
        from unittest.mock import patch

        with patch("src.core.security.url_validator._resolve_ips", return_value=["93.184.216.34"]):
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

    def test_callback_rejects_embedded_url_credentials_before_dns(self):
        """Callback auth must come from its config, never URL userinfo."""
        from unittest.mock import patch

        with patch("src.core.security.url_validator._resolve_ips") as resolve:
            is_valid, error = WebhookURLValidator.validate_callback_url(
                "https://url-user:url-password@buyer.example/webhook"
            )

        assert is_valid is False
        assert error == "URL failed SSRF validation"
        resolve.assert_not_called()

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

    def test_outbound_allows_configured_webhook_host_under_testing(self, monkeypatch):
        """ADCP_WEBHOOK_HOST (e.g. a Docker Compose service name resolving to a
        private IP, as set by docker-compose.e2e.yml for the e2e webhook capture
        server) must be allowed at send time under ADCP_TESTING, even though DNS
        resolution puts it in a blocked private range. Regression test for the
        gap where only literal localhost/127.0.0.1/loopback were exempted, so
        every outbound webhook in the e2e suite was silently SSRF-rejected."""
        monkeypatch.setenv("ADCP_TESTING", "true")
        monkeypatch.setenv("ADCP_WEBHOOK_HOST", "tests")
        # "tests" isn't publicly resolvable in a unit-test process, so exercise
        # the hostname-allowlist path directly rather than depending on the
        # e2e Docker network's DNS.
        assert WebhookURLValidator._is_trusted_test_host("http://tests:8080/webhook") is True

    def test_outbound_still_blocks_unconfigured_hostname_under_testing(self, monkeypatch):
        """A hostname that doesn't match ADCP_WEBHOOK_HOST is not exempted just
        because ADCP_TESTING is set -- only the operator-configured test host is."""
        monkeypatch.setenv("ADCP_TESTING", "true")
        monkeypatch.setenv("ADCP_WEBHOOK_HOST", "tests")
        assert WebhookURLValidator._is_trusted_test_host("http://some-other-service:8080/webhook") is False

    def test_outbound_webhook_host_exemption_requires_env_var_set(self, monkeypatch):
        """No ADCP_WEBHOOK_HOST configured -> no hostname exemption beyond localhost."""
        monkeypatch.delenv("ADCP_WEBHOOK_HOST", raising=False)
        assert WebhookURLValidator._is_trusted_test_host("http://tests:8080/webhook") is False

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


class TestCallbackRejectionHintClassification:
    """The buyer-facing HTTPS hint fires only on the actual scheme rejection.

    The hint was previously keyed on a substring match against the rejection
    detail, so any detail containing "https" (e.g. the hostname echo of an
    unresolvable ``https-portal.invalid``) was misclassified as a scheme error.
    It now keys on the validator's exact scheme-error prefix.
    """

    def test_http_scheme_rejection_gets_https_hint(self):
        from src.core.webhook_validator import _HTTPS_REQUIRED_MESSAGE, _validate_callback_url_with_policy

        is_valid, message = _validate_callback_url_with_policy("http://example.com/webhook", allow_private=False)
        assert not is_valid
        assert message == _HTTPS_REQUIRED_MESSAGE

    def test_unresolvable_host_named_https_gets_generic_rejection(self):
        from unittest.mock import patch

        from src.core.webhook_validator import (
            _GENERIC_CALLBACK_REJECTION,
            _HTTPS_REQUIRED_MESSAGE,
            _validate_callback_url_with_policy,
        )

        with patch("src.core.security.url_validator._resolve_ips", side_effect=OSError("no dns")):
            is_valid, message = _validate_callback_url_with_policy(
                "https://https-portal.invalid/webhook", allow_private=False
            )
        assert not is_valid
        assert message == _GENERIC_CALLBACK_REJECTION
        assert message != _HTTPS_REQUIRED_MESSAGE


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


class TestPinnedOutboundClient:
    """The outbound webhook POST is connection-pinned, redirect-disabled, 2xx-only (#1512 SSRF).

    Pinning lives in ``_PinningHTTPAdapter``, mounted on the service's long-lived pooled
    session. Tests exercise the real production seam (``get_connection_with_tls_context``),
    mocking only DNS resolution and pool creation.
    """

    # These tests grade POST mechanics (pinning, redirects, Host header, retry
    # semantics) on ``buyer.example.com``, which does not resolve — see the
    # shared fixture in tests/unit/conftest.py.
    pytestmark = pytest.mark.usefixtures("pass_send_time_ssrf_gate")

    def test_pinning_adapter_pins_socket_to_validated_ip_keeping_hostname_sni(self):
        """The socket connects to the validated IP while SNI + cert stay bound to the hostname."""
        from unittest.mock import MagicMock, patch

        import requests

        from src.core.security import webhook_http

        adapter = webhook_http.PinningHTTPAdapter()
        request = requests.Request("POST", "https://buyer.example.com:8443/webhook").prepare()

        captured: dict = {}

        def _fake_connection_from_host(**kwargs):
            captured.update(kwargs)
            return MagicMock()

        with (
            patch.object(webhook_http, "resolve_and_validate_target", return_value=("93.184.216.34", "")),
            patch.object(adapter.poolmanager, "connection_from_host", _fake_connection_from_host),
        ):
            adapter.get_connection_with_tls_context(request, verify=True)

        # Socket connects to the validated IP, not a hostname re-resolved at connect time...
        assert captured["host"] == "93.184.216.34"
        assert captured["port"] == 8443
        # ...while SNI and certificate verification stay bound to the ORIGINAL hostname.
        assert captured["pool_kwargs"]["server_hostname"] == "buyer.example.com"
        assert captured["pool_kwargs"]["assert_hostname"] == "buyer.example.com"

    def test_pinning_adapter_rejects_ssrf_url(self):
        """An SSRF-invalid target raises before any connection is created."""
        from unittest.mock import patch

        import requests

        from src.core.security import webhook_http

        adapter = webhook_http.PinningHTTPAdapter()
        request = requests.Request("POST", "https://evil.example.com/webhook").prepare()

        with patch.object(webhook_http, "resolve_and_validate_target", return_value=(None, "blocked internal target")):
            with pytest.raises(requests.RequestException, match="SSRF"):
                adapter.get_connection_with_tls_context(request, verify=True)

    def test_pinning_adapter_rejects_embedded_url_credentials_before_dns(self):
        """A stale persisted URL cannot replace configured Bearer auth with URL Basic auth."""
        from unittest.mock import patch

        import requests

        from src.core.security import webhook_http

        adapter = webhook_http.PinningHTTPAdapter()
        request = requests.Request(
            "POST",
            "https://url-user:url-password@buyer.example.com/webhook",
            headers={"Authorization": "Bearer configured-token"},
        ).prepare()

        # requests has already replaced the configured Bearer header with URL Basic
        # auth while preparing this request. The adapter must fail closed before DNS.
        assert request.headers["Authorization"].startswith("Basic ")
        with patch.object(webhook_http, "resolve_and_validate_target") as resolve:
            with pytest.raises(webhook_http.UnsafeWebhookTargetError, match="embedded credentials"):
                adapter.get_connection_with_tls_context(request, verify=True)
        resolve.assert_not_called()

    def test_session_ignores_environment_proxies_and_netrc(self):
        """trust_env=False: no HTTP(S)_PROXY / NO_PROXY / ~/.netrc injection on egress."""
        from src.services.protocol_webhook_service import ProtocolWebhookService

        service = ProtocolWebhookService()
        assert service._session.trust_env is False

    def test_pinning_adapter_refuses_proxied_target(self):
        """A configured proxy would defeat host-pinning — refuse to deliver, do not unpin."""
        from unittest.mock import patch

        import requests

        from src.core.security import webhook_http

        adapter = webhook_http.PinningHTTPAdapter()
        request = requests.Request("POST", "https://buyer.example.com/webhook").prepare()

        with (
            patch.object(webhook_http, "resolve_and_validate_target", return_value=("93.184.216.34", "")),
            patch.object(webhook_http, "select_proxy", return_value="http://proxy.internal:3128"),
        ):
            with pytest.raises(requests.RequestException, match="proxy"):
                adapter.get_connection_with_tls_context(request, verify=True)

    def test_post_streams_and_closes_response_body(self):
        """Only the status code is consumed: the POST streams and the body is closed."""
        import asyncio
        from unittest.mock import MagicMock, patch

        from src.core.database.models import PushNotificationConfig
        from src.services.protocol_webhook_service import ProtocolWebhookService

        config = PushNotificationConfig(
            id="pnc-stream",
            tenant_id="t",
            principal_id="p",
            url="https://buyer.example.com/webhook",
            authentication_type=None,
            authentication_token=None,
        )
        ok_resp = MagicMock()
        ok_resp.status_code = 200
        captured: dict = {}

        def _fake_post(self, url, **kwargs):  # noqa: ANN001 - test stub
            captured["kwargs"] = kwargs
            return ok_resp

        with patch("requests.sessions.Session.post", _fake_post):
            asyncio.run(
                ProtocolWebhookService().send_notification(
                    config, {"status": "completed"}, metadata={"task_type": "create_media_buy"}
                )
            )

        assert captured["kwargs"]["stream"] is True
        ok_resp.close.assert_called_once_with()

    def test_post_disables_redirects_and_preserves_host_header(self):
        """Delivery POSTs disable redirects and set Host to the original netloc (vhost routing)."""
        import asyncio
        from unittest.mock import MagicMock, patch

        from src.core.database.models import PushNotificationConfig
        from src.services.protocol_webhook_service import ProtocolWebhookService

        config = PushNotificationConfig(
            id="pnc-ok",
            tenant_id="t",
            principal_id="p",
            url="https://buyer.example.com/webhook",
            authentication_type=None,
            authentication_token=None,
        )
        ok_resp = MagicMock()
        ok_resp.status_code = 200
        captured: dict = {}

        def _fake_post(self, url, **kwargs):  # noqa: ANN001 - test stub
            captured["url"] = url
            captured["kwargs"] = kwargs
            return ok_resp

        with patch("requests.sessions.Session.post", _fake_post):
            asyncio.run(
                ProtocolWebhookService().send_notification(
                    config, {"status": "completed"}, metadata={"task_type": "create_media_buy"}
                )
            )

        assert captured["url"] == "https://buyer.example.com/webhook"
        assert captured["kwargs"]["allow_redirects"] is False
        assert captured["kwargs"]["headers"]["Host"] == "buyer.example.com"

    def test_send_notification_treats_3xx_as_failed_delivery(self):
        """A refused redirect is permanent and must be attempted exactly once."""
        import asyncio
        from unittest.mock import AsyncMock, MagicMock, patch

        from src.core.database.models import PushNotificationConfig
        from src.services.protocol_webhook_service import ProtocolWebhookService

        config = PushNotificationConfig(
            id="pnc-3xx",
            tenant_id="t",
            principal_id="p",
            url="https://buyer.example.com/webhook",
            authentication_type=None,
            authentication_token=None,
        )
        redirect_resp = MagicMock()
        redirect_resp.status_code = 302

        with (
            patch("requests.sessions.Session.post", return_value=redirect_resp) as mock_post,
            patch("src.services.protocol_webhook_service.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        ):
            delivered = asyncio.run(
                ProtocolWebhookService().send_notification(
                    config, {"status": "completed"}, metadata={"task_type": "create_media_buy"}
                )
            )

        assert delivered is False, "a 3xx response must be treated as a failed delivery"
        assert mock_post.call_count == 1
        call = mock_post.call_args
        assert call.args == ("https://buyer.example.com/webhook",)
        assert call.kwargs["data"] == b'{"status":"completed"}'
        assert call.kwargs["headers"]["Host"] == "buyer.example.com"
        assert {"Signature", "Signature-Input", "Content-Digest"} <= call.kwargs["headers"].keys()
        mock_sleep.assert_not_awaited()

    def test_send_notification_does_not_retry_unsafe_target(self):
        """An SSRF/pinning policy refusal is permanent, not a network retry."""
        import asyncio
        from unittest.mock import AsyncMock, patch

        from src.core.database.models import PushNotificationConfig
        from src.core.security.webhook_http import UnsafeWebhookTargetError
        from src.services.protocol_webhook_service import ProtocolWebhookService

        config = PushNotificationConfig(
            id="pnc-unsafe",
            tenant_id="t",
            principal_id="p",
            url="https://buyer.example.com/webhook",
            authentication_type=None,
            authentication_token=None,
        )
        service = ProtocolWebhookService()

        with (
            patch(
                "src.services.protocol_webhook_service.post_webhook_status_async",
                new_callable=AsyncMock,
                side_effect=UnsafeWebhookTargetError("unsafe target"),
            ) as mock_post,
            patch("src.services.protocol_webhook_service.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        ):
            delivered = asyncio.run(
                service.send_notification(config, {"status": "completed"}, metadata={"task_type": "create_media_buy"})
            )

        assert delivered is False
        mock_post.assert_awaited_once()
        args = mock_post.await_args
        assert args.args == (service._session, "https://buyer.example.com/webhook")
        assert args.kwargs["body"] == b'{"status":"completed"}'
        assert args.kwargs["timeout"] == 10.0
        assert args.kwargs["headers"]["Content-Type"] == "application/json"
        assert args.kwargs["headers"]["User-Agent"] == "AdCP-Sales-Agent/1.0"
        assert {"Signature", "Signature-Input", "Content-Digest"} <= args.kwargs["headers"].keys()
        mock_sleep.assert_not_awaited()


class TestPinningAdapterIsActuallyMounted:
    """The pinning adapter is REACHED by a real send, not merely defined.

    Every other test in this file constructs ``PinningHTTPAdapter()`` directly
    and grades its behavior. None of them grade that
    ``create_pinned_webhook_session`` mounts it — so deleting the two
    ``session.mount(...)`` lines left `tests/unit -k "webhook or ssrf or
    delivery"` at 658 passed while silently restoring unpinned, re-resolving
    egress on both delivery services.

    No mocks: the assertions go through requests' own adapter registry and its
    real ``Session.send()`` dispatch.
    """

    def test_both_schemes_resolve_to_the_pinning_adapter(self):
        from src.core.security.webhook_http import PinningHTTPAdapter, create_pinned_webhook_session

        session = create_pinned_webhook_session()

        assert isinstance(session.get_adapter("https://buyer.example.com/cb"), PinningHTTPAdapter)
        assert isinstance(session.get_adapter("http://buyer.example.com/cb"), PinningHTTPAdapter)

    def test_proxy_env_is_ignored_so_pinning_cannot_be_bypassed(self):
        from src.core.security.webhook_http import create_pinned_webhook_session

        assert create_pinned_webhook_session().trust_env is False

    def test_a_real_send_reaches_the_adapter_and_is_refused(self):
        """Drives requests' real dispatch, not the adapter in isolation.

        ``.invalid`` is reserved by RFC 6761 and never resolves, so an
        UNMOUNTED session fails with a DNS/ConnectionError instead — which is
        exactly the discrimination this test needs, and it needs no network
        either way. Embedded credentials are the adapter's FIRST check, so the
        refusal happens before any resolution attempt.
        """
        import pytest as _pytest
        import requests

        from src.core.security.webhook_http import UnsafeWebhookTargetError, create_pinned_webhook_session

        session = create_pinned_webhook_session()

        with _pytest.raises(UnsafeWebhookTargetError):
            session.post("https://user:pw@nonexistent.invalid/cb", data=b"{}", timeout=5)

        # And prove the discrimination is real: a plain session reaches the
        # network layer instead of the guard.
        with _pytest.raises(requests.exceptions.RequestException) as plain:
            requests.Session().post("https://user:pw@nonexistent.invalid/cb", data=b"{}", timeout=5)
        assert not isinstance(plain.value, UnsafeWebhookTargetError)


class TestPrivateTargetOptInIsSingleSourced:
    """Send-time gate and connect-time adapter read ONE predicate.

    They used to read two: ``validate_outbound_webhook_url`` keyed on
    ``ADCP_TESTING`` while ``PinningHTTPAdapter`` keyed on
    ``ADCP_ALLOW_PRIVATE_WEBHOOKS``. Setting either alone made them contradict
    each other in one direction or the other — a send the gate approved and
    the socket refused, or a target the gate refused that the adapter would
    have dialled. Neither direction was detectable from one gate's tests.
    """

    _LOOPBACK = "http://127.0.0.1:9999/webhook"

    @staticmethod
    def _clear(monkeypatch):
        for var in ("ADCP_TESTING", "ADCP_ALLOW_PRIVATE_WEBHOOKS", "ENVIRONMENT", "PRODUCTION", "FLY_APP_NAME"):
            monkeypatch.delenv(var, raising=False)

    def _both_gates_allow_private(self) -> tuple[bool, bool]:
        """(send-time verdict, connect-time verdict) for the same target."""
        from src.core.security import webhook_http
        from src.core.webhook_validator import WebhookURLValidator

        send_ok, _ = WebhookURLValidator.validate_outbound_webhook_url(self._LOOPBACK)
        connect_allows_private = webhook_http._allow_private_webhook_targets()
        return send_ok, connect_allows_private

    def test_the_dedicated_flag_opens_both_gates(self, monkeypatch):
        self._clear(monkeypatch)
        monkeypatch.setenv("ADCP_ALLOW_PRIVATE_WEBHOOKS", "true")

        send_ok, connect_ok = self._both_gates_allow_private()

        assert send_ok is True and connect_ok is True, f"gates disagree: send={send_ok} connect={connect_ok}"

    def test_no_flag_closes_both_gates(self, monkeypatch):
        self._clear(monkeypatch)

        send_ok, connect_ok = self._both_gates_allow_private()

        assert send_ok is False and connect_ok is False, f"gates disagree: send={send_ok} connect={connect_ok}"

    def test_adcp_testing_alone_does_not_open_either_gate(self, monkeypatch):
        """The exact divergence that existed: ADCP_TESTING opened only the send gate.

        A staging deployment serving real buyers can carry ADCP_TESTING, which
        is why ``_allow_private_webhook_targets``'s docstring records that
        gating on "not production" was too broad and this dedicated flag
        replaced it. Honouring ADCP_TESTING here would re-open exactly that.
        """
        self._clear(monkeypatch)
        monkeypatch.setenv("ADCP_TESTING", "true")

        send_ok, connect_ok = self._both_gates_allow_private()

        assert send_ok is False and connect_ok is False, (
            f"ADCP_TESTING alone must not unlock private egress: send={send_ok} connect={connect_ok}"
        )


class TestDeliveryDeadlineCoversTheRetryBudget:
    """The deadline must bound the retry loop, not cancel it.

    `WEBHOOK_DELIVERY_DEADLINE_SECONDS` was a hand-picked 12.0 wrapping a loop
    that can legitimately run 36-38s (3 x 10s POST plus 2-3s and 4-5s backoff).
    The second attempt alone exceeded it, so the TimeoutError branch was the
    normal outcome for any slow endpoint and the retries never happened. All
    four callers discard the returned bool, so the only signal was a log line.

    Nothing pinned the relation — the sole existing reference monkeypatches the
    constant to 0.05.
    """

    def test_the_deadline_is_at_least_the_worst_case_retry_budget(self):
        from src.core.security import webhook_http

        assert webhook_http.WEBHOOK_DELIVERY_DEADLINE_SECONDS >= webhook_http._worst_case_delivery_seconds(), (
            "the deadline is shorter than the retry loop it wraps, so retries are cancelled, not capped"
        )

    def test_the_deadline_moves_when_the_REAL_backoff_moves(self):
        """The derivation must read the same function the loop sleeps on.

        This is the oracle the first version lacked. The service hardcoded
        `(2**attempt) + random.uniform(0, 1)` while the deadline re-expressed
        the identical formula, so widening the real backoff to
        `3**attempt + uniform(0, 5)` — 52s against a 38s deadline, reinstating
        the original defect — left 92 tests passing. Two homes for one number
        cannot be pinned by asserting the number.
        """
        from unittest.mock import patch

        from src.core.security import webhook_http

        baseline = webhook_http._worst_case_delivery_seconds()

        def _wider(attempt: int, *, jitter: float | None = None) -> float:
            return 0.0 if attempt == 0 else (3**attempt) + (5.0 if jitter is not None else 0.0)

        with patch.object(webhook_http, "webhook_retry_delay_seconds", _wider):
            widened = webhook_http._worst_case_delivery_seconds()

        assert widened > baseline, (
            "widening the retry delay did not move the derived budget — the deadline is "
            "computing its own copy of the backoff instead of reading the one that sleeps"
        )

    def test_the_service_sleeps_the_shared_delay(self):
        """The other half: the loop must CALL the shared definition."""
        from unittest.mock import patch

        from src.services.webhook_delivery_service import WebhookDeliveryService

        with (
            patch("src.services.webhook_delivery_service.webhook_retry_delay_seconds", return_value=0.0) as delay,
            patch("src.services.webhook_delivery_service.time.sleep") as slept,
        ):
            WebhookDeliveryService._wait_before_retry(2, 3)

        delay.assert_called_once_with(2)
        slept.assert_not_called()  # a 0.0 delay must not sleep

    def test_the_deadline_includes_an_admission_term(self):
        """A queued target must not be punished for queuing.

        The bulkhead starts the clock BEFORE granting a permit and there are
        only WEBHOOK_DELIVERY_MAX_WORKERS of them, so an execution-only budget
        cancels the 5th concurrent target's retries — the same defect, moved
        from the retry loop to the queue. `_enqueue_and_deliver_target`'s
        docstring claims the deadline covers admission.
        """
        from src.core.security import webhook_http

        assert webhook_http.WEBHOOK_DELIVERY_DEADLINE_SECONDS > webhook_http._worst_case_delivery_seconds(), (
            "the deadline equals the execution budget, so it leaves nothing for admission"
        )

    def test_the_worst_case_matches_the_documented_arithmetic(self):
        """Pins the derivation itself: 3 POSTs plus backoff before attempts 1 and 2.

        That the SERVICE actually uses these constants is graded behaviorally
        by tests/unit/test_webhook_delivery_service.py::test_adcp_payload_structure,
        which asserts the real POST call carries WEBHOOK_POST_TIMEOUT_SECONDS.
        """
        from src.core.security import webhook_http

        expected = 3 * webhook_http.WEBHOOK_POST_TIMEOUT_SECONDS + (2 + 1) + (4 + 1)
        assert webhook_http._worst_case_delivery_seconds() == expected

    def test_a_single_attempt_fits_inside_the_deadline(self):
        """The floor the old value violated: one POST timing out must not
        already blow the total budget."""
        from src.core.security import webhook_http

        assert webhook_http.WEBHOOK_POST_TIMEOUT_SECONDS < webhook_http.WEBHOOK_DELIVERY_DEADLINE_SECONDS


class TestWebhookUrlForLogIsTotal:
    """The helper documented as total must not raise, on any input.

    Folding `scrub_control_chars` into `webhook_url_for_log` put a `urlparse`
    call behind a docstring promising "never raw" — and `urlparse` RAISES on
    some malformed input rather than degrading: `urlparse("https://[fe80::1")`
    is `ValueError: Invalid IPv6 URL`. The helper is installed at fourteen
    sites in the delivery service, several inside `except` handlers where the
    `scrub_control_chars` it replaced was total by construction. A raise there
    replaces the delivery failure being logged with an unrelated one.

    Reachability is bounded — registration rejects such URLs — but a helper
    called from exception handlers has to be total by construction, not by
    the good behaviour of its callers.
    """

    @pytest.mark.parametrize(
        "url",
        [
            "https://[fe80::1",  # the ValueError case
            "https://[",
            "https://[]",
            None,
            "",
            "   ",
            "not-a-url",
            "://missing-scheme",
            "https://h/cb\r\nFAKE",
        ],
    )
    def test_never_raises(self, url):
        from src.core.webhook_validator import webhook_url_for_log

        result = webhook_url_for_log(url)

        assert isinstance(result, str) and result, f"{url!r} produced {result!r}"

    def test_malformed_input_degrades_to_the_placeholder(self):
        from src.core.webhook_validator import UNPARSEABLE_WEBHOOK_URL_FOR_LOG, webhook_url_for_log

        assert webhook_url_for_log("https://[fe80::1") == UNPARSEABLE_WEBHOOK_URL_FOR_LOG

    def test_well_formed_ipv6_still_parses(self):
        """The fix must not turn valid IPv6 into the placeholder."""
        from src.core.webhook_validator import webhook_url_for_log

        assert webhook_url_for_log("http://[::1]:8080/cb") == "http://::1/cb"
