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

_BACKFILL_BATCH = text(
    """
    WITH batch AS (
        SELECT media_buy_id
        FROM media_buys
        WHERE confirmed_at IS NULL
          AND (
            lower(status) NOT IN :unconfirmed_statuses
            OR (lower(status) = 'draft' AND approved_at IS NOT NULL)
          )
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
