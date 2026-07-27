"""persist delivery webhook idempotency keys and remove unused scan index

Failed webhook attempts retain their sequence number. Persist the AdCP
idempotency key with each attempt so a later scheduler invocation can retry the
same logical notification with the same key.

The delivery scheduler's repository query filters only by status; it has no
updated_at predicate. Remove the composite index added in the preceding
migration because it cannot support that query.

Revision ID: b7c9d2e4f6a8
Revises: 9f3a1b7c2d4e
Create Date: 2026-07-27 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7c9d2e4f6a8"
down_revision: str | Sequence[str] | None = "9f3a1b7c2d4e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Persist retry keys and remove the index unused by the scheduler query."""
    op.add_column(
        "webhook_delivery_log",
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
    )
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_media_buys_status_updated_at")


def downgrade() -> None:
    """Restore the scan index and remove persisted retry keys."""
    with op.get_context().autocommit_block():
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_media_buys_status_updated_at "
            "ON media_buys (status, updated_at)"
        )
    op.drop_column("webhook_delivery_log", "idempotency_key")
