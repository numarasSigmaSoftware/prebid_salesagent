"""The advertised webhook posture and emitted signature share one validated key."""

import json

import pytest

from src.core.security.webhook_http import (
    BEARER_AUTH_SCHEME,
    HMAC_AUTH_SCHEME,
    redact_webhook_url,
    validate_webhook_auth_selector,
)
from src.core.webhook_signing_config import (
    WebhookSigningConfigurationError,
    load_webhook_signing_config,
)
from src.services.protocol_webhook_service import _default_webhook_signature_headers
from tests.helpers.webhook_signing import webhook_signing_jwk_json


def _configure(monkeypatch) -> dict:
    jwk = json.loads(webhook_signing_jwk_json())
    monkeypatch.setenv("ADCP_WEBHOOK_SIGNING_JWK", json.dumps(jwk))
    monkeypatch.setenv(
        "ADCP_WEBHOOK_SIGNING_JWKS_URI",
        "https://Keys.Example:8443/.well-known/jwks.json",
    )
    monkeypatch.setenv("ADCP_BRAND_JSON_URL", "https://seller.example/.well-known/brand.json")
    return jwk


def test_config_derives_algorithm_and_key_origin_from_validated_jwk(monkeypatch) -> None:
    _configure(monkeypatch)

    config = load_webhook_signing_config(required=True)

    assert config is not None
    assert config.algorithm == "ed25519"
    assert config.jwks_origin == "https://keys.example:8443"
    assert config.jwks_uri.endswith("/.well-known/jwks.json")


def test_partial_default_signing_config_is_not_ready(monkeypatch) -> None:
    monkeypatch.setenv("ADCP_WEBHOOK_SIGNING_JWK", webhook_signing_jwk_json())
    monkeypatch.delenv("ADCP_WEBHOOK_SIGNING_JWKS_URI", raising=False)
    monkeypatch.delenv("ADCP_BRAND_JSON_URL", raising=False)

    with pytest.raises(WebhookSigningConfigurationError, match="configured together"):
        load_webhook_signing_config()
    with pytest.raises(WebhookSigningConfigurationError, match="configured together"):
        validate_webhook_auth_selector(None, None)


def test_declared_algorithm_must_match_jwk(monkeypatch) -> None:
    _configure(monkeypatch)
    monkeypatch.setenv("ADCP_WEBHOOK_SIGNING_ALGORITHM", "ecdsa-p256-sha256")

    with pytest.raises(WebhookSigningConfigurationError, match="must match"):
        load_webhook_signing_config(required=True)


def test_capability_algorithm_and_origin_match_emitted_signature(monkeypatch) -> None:
    _configure(monkeypatch)
    from src.core.tools.capabilities import _webhook_signing_posture

    posture = _webhook_signing_posture()
    signature_input = _default_webhook_signature_headers(
        url="https://buyer.example/webhook",
        headers={"Content-Type": "application/json"},
        body=b"{}",
    )["Signature-Input"]

    assert posture["webhook_signing"].algorithms == ["ed25519"]
    assert str(posture["identity"].key_origins.webhook_signing).rstrip("/") == "https://keys.example:8443"
    assert 'alg="ed25519"' in signature_input


def test_capabilities_do_not_advertise_partial_configuration(monkeypatch) -> None:
    monkeypatch.setenv("ADCP_WEBHOOK_SIGNING_JWK", webhook_signing_jwk_json())
    monkeypatch.delenv("ADCP_WEBHOOK_SIGNING_JWKS_URI", raising=False)
    monkeypatch.delenv("ADCP_BRAND_JSON_URL", raising=False)
    from src.core.tools.capabilities import _webhook_signing_posture

    posture = _webhook_signing_posture()

    assert posture["webhook_signing"].supported is False
    assert posture["identity"] is None


@pytest.mark.parametrize("credentials", [None, "", "short"])
def test_legacy_credentials_enforce_pinned_minimum(credentials) -> None:
    with pytest.raises(ValueError, match="at least 32"):
        validate_webhook_auth_selector(HMAC_AUTH_SCHEME, credentials)


def test_unusable_auth_scheme_is_refused_by_cause() -> None:
    """The two causes stay distinguishable, and both refuse rather than warn.

    #1546 split one warning into these two cases because
    "Bearer is not supported (expected Bearer or Basic)" points an operator at
    the wrong axis when the real problem is a missing token. That fix warned
    and then sent the webhook UNAUTHENTICATED; this path refuses instead, so
    the split is preserved in the refusal messages rather than in log lines —
    and nothing is delivered on a credential the buyer cannot verify.
    """
    # Cause 1: the scheme itself is not one this path can sign or send.
    with pytest.raises(ValueError, match="Unsupported webhook authentication scheme"):
        validate_webhook_auth_selector("Basic", "x" * 32)

    # Cause 2: a recognised scheme, no usable credential — a different message,
    # so an operator is not sent looking at scheme support.
    with pytest.raises(ValueError, match="at least 32"):
        validate_webhook_auth_selector(BEARER_AUTH_SCHEME, None)


def test_callback_url_redaction_drops_userinfo_path_and_query() -> None:
    assert (
        redact_webhook_url("https://user:secret@Buyer.Example:9443/hooks/private?token=secret")
        == "https://buyer.example:9443/<redacted>"
    )
