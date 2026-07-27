"""add media_buys status/updated_at delivery-scan index

Supports the delivery webhook scheduler's hourly query for recently completed
media buys. The existing status-only index still supports the unbounded serving
arm; this composite index lets PostgreSQL time-bound the completed arm.

Revision ID: 9f3a1b7c2d4e
Revises: d3f8a1c4b592
Create Date: 2026-07-27 00:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9f3a1b7c2d4e"
down_revision: str | Sequence[str] | None = "d3f8a1c4b592"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the supporting index without blocking writes to media_buys."""
    with op.get_context().autocommit_block():
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_media_buys_status_updated_at "
            "ON media_buys (status, updated_at)"
        )


def downgrade() -> None:
    """Drop the supporting index without blocking writes to media_buys."""
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_media_buys_status_updated_at")
