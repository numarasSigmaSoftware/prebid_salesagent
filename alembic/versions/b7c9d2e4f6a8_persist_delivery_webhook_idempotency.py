"""persist delivery webhook idempotency keys

Failed webhook attempts retain their sequence number. Persist the AdCP
idempotency key with each attempt so a later scheduler invocation can retry the
same logical notification with the same key.

No index accompanies this change. An earlier draft of this branch added a
composite ``(status, updated_at)`` index on ``media_buys`` and dropped it again
one revision later, because the delivery scheduler's repository query filters
only by status and has no ``updated_at`` predicate. Both revisions were squashed
away before merge rather than making every deployment pay two concurrent index
builds for no net schema change. Note that ``media_buys`` carries no status
index at all: ``models.py`` declares ``idx_media_buys_status`` in
``__table_args__``, but no migration creates it, so the scheduler's status scan
is unindexed in both CI and production.

Revision ID: b7c9d2e4f6a8
Revises: d3f8a1c4b592
Create Date: 2026-07-27 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7c9d2e4f6a8"
down_revision: str | Sequence[str] | None = "d3f8a1c4b592"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Persist retry keys so a later invocation reuses the same idempotency key."""
    op.add_column(
        "webhook_delivery_log",
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
    )


def downgrade() -> None:
    """Remove persisted retry keys."""
    op.drop_column("webhook_delivery_log", "idempotency_key")
