"""add media_buys status index

media_buys.status has been indexed in the ORM model (idx_media_buys_status,
models.py __table_args__) since the column was introduced, but no migration
has ever created it -- this closes that model<->DB drift, already diagnosed
in b7c9d2e4f6a8's own docstring ("media_buys carries no status index at all
... so the scheduler's status scan is unindexed in both CI and production").

Not primarily a performance fix: status is low-cardinality enough that
PostgreSQL may reasonably prefer a sequential scan on a modest table anyway.
The justification is closing the drift between the declared model and the
actual schema.

Runs CREATE/DROP INDEX CONCURRENTLY in an autocommit block, matching
a164b85bab9e's precedent on this same table -- CONCURRENTLY cannot run
inside a transaction, and this repo's alembic/env.py does not set
transaction_per_migration. IF NOT EXISTS / IF EXISTS are load-bearing, not
defensive filler: since the index is already declared in
MediaBuy.__table_args__, any environment bootstrapped via
Base.metadata.create_all() (e.g. fresh test databases) already has it, and
omitting the guard would make this migration fail there.

If CREATE INDEX CONCURRENTLY fails partway through (e.g. the process is
killed), it can leave an INVALID index behind under the same name -- in that
case IF NOT EXISTS will silently skip creating it on retry, and an operator
must DROP INDEX idx_media_buys_status manually before re-running.

Revision ID: b5838b839548
Revises: 168914d7ca05
Create Date: 2026-07-30 17:32:43.481260

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b5838b839548"
down_revision: str | Sequence[str] | None = "168914d7ca05"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the status index declared in the model but never migrated."""
    with op.get_context().autocommit_block():
        op.execute("CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_media_buys_status ON media_buys (status)")


def downgrade() -> None:
    """Drop the status index -- the model still declares it, so a fresh
    Base.metadata.create_all() environment is unaffected by this downgrade."""
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_media_buys_status")
