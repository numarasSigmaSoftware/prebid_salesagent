"""Validated configuration and signing for the default RFC 9421 webhook profile."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from adcp import sign_webhook
from adcp.signing import private_key_from_jwk

_JWK_ALGORITHMS = {
    "EdDSA": "ed25519",
    "ES256": "ecdsa-p256-sha256",
}


class WebhookSigningConfigurationError(ValueError):
    """The default webhook-signing trust root is absent or internally inconsistent."""


@dataclass(frozen=True)
class WebhookSigningConfig:
    """One parsed source of truth shared by capabilities and delivery."""

    private_key: Any
    key_id: str
    algorithm: str
    jwks_uri: str
    jwks_origin: str
    brand_json_url: str


def _https_url(value: str, *, setting: str) -> tuple[str, str]:
    """Validate a public HTTPS URL and return it plus its scheme/authority origin."""
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise WebhookSigningConfigurationError(f"{setting} must be a valid HTTPS URL") from exc
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise WebhookSigningConfigurationError(
            f"{setting} must be an absolute HTTPS URL without credentials or a fragment"
        )
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    authority = f"{host}:{port}" if port is not None else host
    return value, f"https://{authority}"


def _parse_signing_jwk(encoded_jwk: str) -> tuple[Any, str, str]:
    """Validate private JWK material and derive its public wire attributes."""
    try:
        jwk = json.loads(encoded_jwk)
    except (TypeError, json.JSONDecodeError) as exc:
        raise WebhookSigningConfigurationError("ADCP_WEBHOOK_SIGNING_JWK must be valid JSON") from exc
    if not isinstance(jwk, dict):
        raise WebhookSigningConfigurationError("ADCP_WEBHOOK_SIGNING_JWK must be a JSON object")
    if jwk.get("adcp_use") != "webhook-signing":
        raise WebhookSigningConfigurationError("ADCP_WEBHOOK_SIGNING_JWK must declare adcp_use='webhook-signing'")
    key_id = jwk.get("kid")
    if not isinstance(key_id, str) or not key_id:
        raise WebhookSigningConfigurationError("ADCP_WEBHOOK_SIGNING_JWK must contain a non-empty kid")
    jwk_algorithm = jwk.get("alg")
    algorithm = _JWK_ALGORITHMS.get(jwk_algorithm) if isinstance(jwk_algorithm, str) else None
    if algorithm is None:
        raise WebhookSigningConfigurationError("ADCP_WEBHOOK_SIGNING_JWK alg must be EdDSA or ES256")
    try:
        private_key = private_key_from_jwk(jwk)
    except (KeyError, TypeError, ValueError) as exc:
        raise WebhookSigningConfigurationError(
            "ADCP_WEBHOOK_SIGNING_JWK must contain valid private signing key material"
        ) from exc
    return private_key, key_id, algorithm


def load_webhook_signing_config(*, required: bool = False) -> WebhookSigningConfig | None:
    """Parse and validate the complete default-signing configuration.

    A completely absent configuration means the profile is unsupported. Any
    partial or malformed configuration is an operator error, never a reason to
    advertise support or emit an undiscoverable signature.
    """
    encoded_jwk = os.getenv("ADCP_WEBHOOK_SIGNING_JWK")
    jwks_uri_value = os.getenv("ADCP_WEBHOOK_SIGNING_JWKS_URI")
    brand_json_value = os.getenv("ADCP_BRAND_JSON_URL")
    configured_values = (encoded_jwk, jwks_uri_value, brand_json_value)
    if not any(configured_values):
        if required:
            raise WebhookSigningConfigurationError(
                "Default RFC 9421 webhooks require ADCP_WEBHOOK_SIGNING_JWK, "
                "ADCP_WEBHOOK_SIGNING_JWKS_URI, and ADCP_BRAND_JSON_URL"
            )
        return None
    if not all(configured_values):
        raise WebhookSigningConfigurationError(
            "ADCP_WEBHOOK_SIGNING_JWK, ADCP_WEBHOOK_SIGNING_JWKS_URI, and "
            "ADCP_BRAND_JSON_URL must be configured together"
        )

    assert encoded_jwk is not None
    assert jwks_uri_value is not None
    assert brand_json_value is not None
    private_key, key_id, algorithm = _parse_signing_jwk(encoded_jwk)
    declared_algorithm = os.getenv("ADCP_WEBHOOK_SIGNING_ALGORITHM")
    if declared_algorithm and declared_algorithm != algorithm:
        raise WebhookSigningConfigurationError(
            "ADCP_WEBHOOK_SIGNING_ALGORITHM must match the algorithm derived from ADCP_WEBHOOK_SIGNING_JWK"
        )
    jwks_uri, jwks_origin = _https_url(jwks_uri_value, setting="ADCP_WEBHOOK_SIGNING_JWKS_URI")
    brand_json_url, _ = _https_url(brand_json_value, setting="ADCP_BRAND_JSON_URL")
    return WebhookSigningConfig(
        private_key=private_key,
        key_id=key_id,
        algorithm=algorithm,
        jwks_uri=jwks_uri,
        jwks_origin=jwks_origin,
        brand_json_url=brand_json_url,
    )


def sign_default_webhook_headers(*, url: str, headers: dict[str, str], body: bytes) -> dict[str, str]:
    """Return RFC 9421 headers using the validated, discoverable signing key."""
    config = load_webhook_signing_config(required=True)
    assert config is not None
    signed = sign_webhook(
        method="POST",
        url=url,
        headers=headers,
        body=body,
        private_key=config.private_key,
        key_id=config.key_id,
        alg=config.algorithm,
    )
    return signed.as_dict()
