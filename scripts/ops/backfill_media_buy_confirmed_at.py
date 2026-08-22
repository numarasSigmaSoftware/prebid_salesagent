#!/usr/bin/env python3
"""Backfill historical ``media_buys.confirmed_at`` rows safely.

Run after migration ``2c4e6a7b8d9e`` has added the nullable column. Each
batch runs in its own transaction, so row locks are released before the next
batch. The operation is idempotent and safe to re-run after interruption.
"""

from __future__ import annotations

import argparse

from sqlalchemy import Engine, bindparam, text

from src.core.database.database_session import get_engine
from src.core.database.models import MEDIA_BUY_UNCONFIRMED_STATUSES

DEFAULT_BATCH_ROWS = 1000

# Eligibility derives SOLELY from MEDIA_BUY_UNCONFIRMED_STATUSES — the single
# source of truth for "the seller has committed" (src/core/database/models.py) —
# so a historical row receives exactly the confirmed_at the runtime stamp
# (MediaBuyRepository._stamp_confirmation_if_needed) would give an identical NEW
# row. This query previously carried an extra
# ``OR (lower(status) = 'draft' AND approved_at IS NOT NULL)`` arm that
# CONTRADICTED that set: ``draft`` is listed there as not-yet-committed, and the
# admin approve path still writes status=draft WITH approved_at while creatives
# are outstanding — so the runtime leaves such a row unstamped while the backfill
# stamped it, and history disagreed with every new row of the same shape.
# lower() keeps the comparison case-insensitive, matching
# is_media_buy_seller_confirmed.
_BACKFILL_BATCH = text(
    """
    WITH batch AS (
        SELECT media_buy_id
        FROM media_buys
        WHERE confirmed_at IS NULL
          AND lower(status) NOT IN :unconfirmed_statuses
        ORDER BY media_buy_id
        FOR UPDATE
        LIMIT :batch_rows
    )
    UPDATE media_buys AS mb
    SET confirmed_at = COALESCE(mb.approved_at, mb.created_at, now())
    FROM batch
    WHERE mb.media_buy_id = batch.media_buy_id
    """
).bindparams(bindparam("unconfirmed_statuses", expanding=True))


def backfill_confirmed_at(engine: Engine, *, batch_rows: int = DEFAULT_BATCH_ROWS) -> int:
    """Backfill eligible historical rows in independently committed batches."""
    if batch_rows < 1:
        raise ValueError("batch_rows must be at least 1")

    updated = 0
    while True:
        # ``engine.begin`` commits this one batch before the loop can select the
        # next, preventing an Alembic-style transaction from retaining every lock.
        with engine.begin() as connection:
            result = connection.execute(
                _BACKFILL_BATCH,
                {
                    "batch_rows": batch_rows,
                    "unconfirmed_statuses": sorted(MEDIA_BUY_UNCONFIRMED_STATUSES),
                },
            )
            batch_updated = result.rowcount
        if batch_updated == 0:
            return updated
        updated += batch_updated


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-rows", type=int, default=DEFAULT_BATCH_ROWS)
    args = parser.parse_args()
    updated = backfill_confirmed_at(get_engine(), batch_rows=args.batch_rows)
    print(f"Backfilled confirmed_at for {updated} media buys.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
