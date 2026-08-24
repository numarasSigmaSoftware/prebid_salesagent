#!/usr/bin/env python3
"""Backfill historical ``media_buys.confirmed_at`` rows safely.

Run after migration ``2c4e6a7b8d9e`` has added the nullable column. Each
batch runs in its own transaction, so row locks are released before the next
batch. The operation is idempotent and safe to re-run after interruption.
"""

from __future__ import annotations

import argparse

from sqlalchemy import Engine, bindparam, make_url, text

from src.core.database.database_session import get_engine
from src.core.database.models import MEDIA_BUY_UNCONFIRMED_STATUSES

DEFAULT_BATCH_ROWS = 1000
# Each batch takes ``FOR UPDATE`` on every row it selects and holds those locks
# until the batch commits, so the batch size IS the lock-footprint. An operator
# reaching for a "just do it in one pass" value would lock every eligible row in
# a single transaction — the exact failure the batching exists to avoid — so the
# value is bounded rather than merely defaulted.
MAX_BATCH_ROWS = 10_000

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


_ELIGIBLE_COUNT = text(
    """
    SELECT count(*)
    FROM media_buys
    WHERE confirmed_at IS NULL
      AND lower(status) NOT IN :unconfirmed_statuses
    """
).bindparams(bindparam("unconfirmed_statuses", expanding=True))


def backfill_confirmed_at(engine: Engine, *, batch_rows: int = DEFAULT_BATCH_ROWS, dry_run: bool = False) -> int:
    """Backfill eligible historical rows in independently committed batches.

    With ``dry_run`` the eligible rows are COUNTED and nothing is written, so an
    operator can size the work (and confirm they are pointed at the intended
    database) before taking any locks.
    """
    if batch_rows < 1:
        raise ValueError("batch_rows must be at least 1")
    if batch_rows > MAX_BATCH_ROWS:
        raise ValueError(
            f"batch_rows must be at most {MAX_BATCH_ROWS}: each batch holds FOR UPDATE "
            "locks on every row it selects until it commits"
        )

    if dry_run:
        with engine.connect() as connection:
            return int(
                connection.execute(
                    _ELIGIBLE_COUNT,
                    {"unconfirmed_statuses": sorted(MEDIA_BUY_UNCONFIRMED_STATUSES)},
                ).scalar_one()
            )

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


def _describe_target(engine: Engine) -> str:
    """Render the resolved target as ``user@host:port/database`` — never the password.

    The script is run by hand against production, so it must say WHICH database it
    resolved from ``DATABASE_URL`` before it writes. ``URL.render_as_string()``
    masks the password by default; the fields are picked explicitly here so a
    future default change cannot start leaking it.
    """
    url = make_url(str(engine.url))
    host = url.host or "localhost"
    port = f":{url.port}" if url.port else ""
    user = f"{url.username}@" if url.username else ""
    return f"{user}{host}{port}/{url.database}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-rows", type=int, default=DEFAULT_BATCH_ROWS)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Count the eligible rows and write nothing.",
    )
    args = parser.parse_args()
    engine = get_engine()
    print(f"Target: {_describe_target(engine)}")
    if args.dry_run:
        eligible = backfill_confirmed_at(engine, batch_rows=args.batch_rows, dry_run=True)
        print(f"Dry run: {eligible} media buys are eligible for a confirmed_at backfill. Nothing written.")
        return 0
    updated = backfill_confirmed_at(engine, batch_rows=args.batch_rows)
    print(f"Backfilled confirmed_at for {updated} media buys.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
