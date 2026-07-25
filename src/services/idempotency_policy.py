"""Idempotency cache admission policy — thresholds and retry_after derivation.

The policy layer over :class:`IdempotencyAttemptRepository`: the repository
answers the two scope questions (how many inserts in the trailing window, how
many active rows — plus their oldest timestamps); this module owns the
thresholds, the ``retry_after`` math, and the decision to reject. Data access
stays in the repository; policy changes never touch SQL.
"""

from __future__ import annotations

import math
import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.core.database.repositories.idempotency_attempt import IdempotencyAttemptRepository

# Storage-abuse ceiling: active (non-expired) cached successes per
# (tenant, principal, account) scope. Each keyed create stores one row for the
# replay TTL, so a buyer minting fresh keys is bounded to this many creates per
# window; the probe rejects the excess as RATE_LIMITED with retry_after set to
# when the oldest row expires. Env-tunable; looked up at call time so tests can patch it.
MAX_ACTIVE_ATTEMPTS_PER_SCOPE = int(os.getenv("IDEMPOTENCY_MAX_ACTIVE_ATTEMPTS_PER_SCOPE") or "1000")

# Insert-RATE limit per (tenant, principal, account) scope — the spec's MUST is
# a rate limit on cache inserts (the row count above is the derived storage
# bound). The window/ceiling follow the spec's SHOULD-level burst numbers
# (300 inserts per 10s). Env-tunable; looked up at call time so tests can patch them.
INSERT_RATE_WINDOW = timedelta(seconds=int(os.getenv("IDEMPOTENCY_INSERT_RATE_WINDOW_SECONDS") or "10"))
MAX_INSERTS_PER_WINDOW = int(os.getenv("IDEMPOTENCY_MAX_INSERTS_PER_WINDOW") or "300")


def _operation_limits(operation_class: str) -> tuple[int, int, timedelta]:
    """Resolve independently tunable read/write limits with legacy fallbacks."""
    prefix = f"IDEMPOTENCY_{operation_class.upper()}"
    active = int(os.getenv(f"{prefix}_MAX_ACTIVE_ATTEMPTS_PER_SCOPE") or MAX_ACTIVE_ATTEMPTS_PER_SCOPE)
    inserts = int(os.getenv(f"{prefix}_MAX_INSERTS_PER_WINDOW") or MAX_INSERTS_PER_WINDOW)
    window = timedelta(
        seconds=int(os.getenv(f"{prefix}_INSERT_RATE_WINDOW_SECONDS") or int(INSERT_RATE_WINDOW.total_seconds()))
    )
    return active, inserts, window


# The spec Error model bounds retry_after to [1, 3600] seconds (clients clamp
# anyway); never emit more even when the oldest row expires further out. A spec
# constant, not an operational knob — deliberately not env-tunable.
_RETRY_AFTER_MAX = 3600


def _clamp_retry_after(seconds: float) -> int:
    """Clamp a raw retry_after to the spec Error model's [1, _RETRY_AFTER_MAX] bound.

    The single home for the floor/ceiling both rejection branches share; callers
    layer any context-specific cap (e.g. the insert-rate window) on top.
    """
    return min(max(1, math.ceil(seconds)), _RETRY_AFTER_MAX)


def enforce_insert_ceiling(
    attempts: IdempotencyAttemptRepository,
    *,
    principal_id: str | None,
    account_id: str | None = None,
    ceiling: int | None = None,
    rate_ceiling: int | None = None,
    now: datetime | None = None,
    operation_class: str = "write",
) -> None:
    """Raise ``RATE_LIMITED`` when the scope has no room for another cached success.

    Called by the idempotency probe on a cache MISS, before any execution —
    a fresh key would insert a new row. Two bounds, both on the spec's
    (tenant, principal, account) scope (no tool dimension):

    - **insert rate** (the spec's MUST): at most :data:`MAX_INSERTS_PER_WINDOW`
      rows created within the trailing :data:`INSERT_RATE_WINDOW`;
      ``retry_after`` is when the oldest in-window insert leaves the window.
    - **active row count** (the derived storage bound): at most
      :data:`MAX_ACTIVE_ATTEMPTS_PER_SCOPE` non-expired rows; ``retry_after``
      is when the oldest active row expires.

    Replays and conflicts are not rate-limited — they insert nothing.
    ``retry_after`` is clamped to the spec Error model's [1, 3600] bound.
    """
    from src.core.exceptions import AdCPRateLimitError

    current = now or datetime.now(UTC)
    # Serialize the scope until the surrounding reservation transaction
    # commits. Without this lock, concurrent distinct keys can all observe
    # limit-1 and exceed both ceilings.
    attempts.lock_admission_scope(
        principal_id=principal_id,
        account_id=account_id,
        operation_class=operation_class,
    )

    # Insert-rate bound: rows CREATED inside the trailing window, expired or not.
    default_active, default_inserts, rate_window = _operation_limits(operation_class)
    rate_limit = rate_ceiling if rate_ceiling is not None else default_inserts
    window_start = current - rate_window
    recent, oldest_in_window = attempts.count_inserts_since(
        principal_id=principal_id,
        account_id=account_id,
        since=window_start,
        operation_class=operation_class,
    )
    if recent >= rate_limit:
        window_seconds = math.ceil(rate_window.total_seconds())
        raw_wait = window_seconds - (current - oldest_in_window).total_seconds() if oldest_in_window else 1
        # The wait can never logically exceed the window itself; the bound
        # also absorbs DB-vs-app clock skew on created_at (server_default).
        raise AdCPRateLimitError(
            "idempotency cache insert rate exceeded for this account — retry shortly",
            retry_after=min(_clamp_retry_after(raw_wait), window_seconds),
        )

    # Storage bound: ACTIVE (non-expired) rows.
    limit = ceiling if ceiling is not None else default_active
    active, oldest_expiry = attempts.count_active(
        principal_id=principal_id,
        account_id=account_id,
        now=current,
        operation_class=operation_class,
    )
    if active < limit:
        return

    raw_wait = (oldest_expiry - current).total_seconds() if oldest_expiry else 1
    raise AdCPRateLimitError(
        "too many active idempotency keys for this account — retry after the oldest replay window expires",
        retry_after=_clamp_retry_after(raw_wait),
    )
