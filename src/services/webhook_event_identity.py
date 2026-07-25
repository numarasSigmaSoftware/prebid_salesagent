"""Deterministic identities for retry-stable AdCP webhook events."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def webhook_event_key(
    *,
    tenant_id: str,
    principal_id: str,
    media_buy_id: str,
    notification_type: str,
    event_payload: dict[str, Any],
) -> str:
    """Return the stable identity of one logical delivery-report event."""
    canonical_payload = json.dumps(
        event_payload,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    material = "\0".join(
        (
            tenant_id,
            principal_id,
            media_buy_id,
            notification_type,
            canonical_payload,
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def registration_event_key(event_key: str, *, operation_id: str | None, url: str) -> str:
    """Scope a logical event identity to one callback registration."""
    material = "\0".join((event_key, operation_id or "", url))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()
