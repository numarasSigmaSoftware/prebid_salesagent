"""add media_buys.last_periodic_webhook_claimed_at (best-effort periodic-webhook claim)

Serializes a buy's PERIODIC (non-final) delivery webhooks across concurrent
scheduler/manual workers -- e.g. multiple replicas under horizontal scaling,
each running their own DeliveryWebhookScheduler against the same database.
The existing 24h dedup check (_should_skip_send) is read-only, so two workers
can both read "no recent send" and both POST, each with a different
idempotency_key (so the buyer's own idempotency dedup does not catch it). The
delivery scheduler atomically claims the periodic send via a conditional
UPDATE on this column before the outbound POST, mirroring
final_webhook_claimed_at's design; a stale claim (crashed worker, older than
the lease) self-heals on a later attempt. NULL until a periodic send is
claimed.

Revision ID: db2e99013cb8
Revises: 168914d7ca05
Create Date: 2026-08-10 16:34:14.709497

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "db2e99013cb8"
down_revision: str | Sequence[str] | None = "168914d7ca05"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable periodic-webhook claim timestamp."""
    op.add_column(
        "media_buys",
        sa.Column("last_periodic_webhook_claimed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """Drop the periodic-webhook claim timestamp."""
    op.drop_column("media_buys", "last_periodic_webhook_claimed_at")
