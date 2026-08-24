"""Add the seller confirmation instant for media buys.

Historical rows are backfilled by ``scripts/ops/backfill_media_buy_confirmed_at.py``.
The operational job commits each bounded batch independently; Alembic runs this
migration in one transaction and must not perform a large data rewrite here.

REQUIRED DEPLOY ORDERING: migrate -> backfill -> serve.

This migration adds the column NULLABLE, so between it and the operator running
the backfill every already-running buy serializes ``status: "active"`` together
with ``confirmed_at: null``. The pinned
``dist/schemas/3.1.1/media-buy/get-media-buys-response.json``
``properties.media_buys.items.allOf[0]`` forbids exactly that pair: its ``if``
matches an item whose ``confirmed_at`` is present-and-null, and its ``then``
asserts ``not {status: "active"}`` (and no ``committed_metrics`` on any package)
— an unconfirmed buy is provisional and must not advertise itself as running. So
a deployment that starts serving get_media_buys in that window emits
schema-invalid items for every historical active buy.

Run the backfill BEFORE the new code serves reads:

    uv run python scripts/ops/migrate.py
    uv run python scripts/ops/backfill_media_buy_confirmed_at.py --dry-run   # inspect
    uv run python scripts/ops/backfill_media_buy_confirmed_at.py
    # only now roll out / restart the application

The backfill is idempotent and safe to re-run after an interruption, so an
operator who is unsure whether it completed should simply run it again.

The split is deliberate: doing the rewrite inside ``upgrade()`` would hold a lock
on every eligible row for the whole migration transaction. Do not "fix" the
ordering hazard by moving the backfill here.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "2c4e6a7b8d9e"
down_revision: str | Sequence[str] | None = "1497aa06013c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("media_buys", sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("media_buys", "confirmed_at")
