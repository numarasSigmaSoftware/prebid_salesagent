"""Test-only RFC 9421 webhook signing key material."""

import base64
import json
from functools import cache

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


@cache
def webhook_signing_jwk_json() -> str:
    """Return a private Ed25519 JWK suitable for the SDK webhook signer."""
    key = Ed25519PrivateKey.generate()
    private = key.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    public = key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )

    def encoded(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode()

    return json.dumps(
        {
            "kty": "OKP",
            "crv": "Ed25519",
            "alg": "EdDSA",
            "kid": "test-webhook-key",
            "adcp_use": "webhook-signing",
            "x": encoded(public),
            "d": encoded(private),
        }
    )
