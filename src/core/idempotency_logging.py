"""Safe observability helpers for security-sensitive idempotency keys."""

from __future__ import annotations


def redact_idempotency_key(key: str | None) -> str:
    """Return the permitted short prefix, never the full replay credential."""
    if not key:
        return "<absent>"
    return f"{key[:8]}…"
