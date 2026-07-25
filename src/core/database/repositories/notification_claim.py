"""Shared token-fenced notification claim transitions."""

from datetime import UTC, datetime
from typing import Any


def finalize_notification_claim(
    event: Any | None,
    claim_token: str,
    *,
    published: bool,
) -> bool:
    """Apply one ACK or release only when the caller still owns the claim."""
    if event is None or event.claim_token != claim_token:
        return False
    event.delivered_at = datetime.now(UTC) if published else None
    event.claimed_at = None
    event.claim_token = None
    return True
