"""Add durable lease and reconciliation state for remote media-buy updates.

Revision ID: c1544a2b7d9e
Revises: 9d2f1a7c4b8e
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c1544a2b7d9e"
down_revision: str | Sequence[str] | None = "9d2f1a7c4b8e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("media_buys", sa.Column("update_lease_id", sa.String(length=64), nullable=True))
    op.add_column("media_buys", sa.Column("update_lease_expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("media_buys", sa.Column("update_adapter_invoked_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("media_buys", sa.Column("update_recovery_mode", sa.String(length=20), nullable=True))
    op.add_column("media_buys", sa.Column("update_reconcile_incident_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("media_buys", sa.Column("update_reconcile_incident_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("media_buys", "update_reconcile_incident_reason")
    op.drop_column("media_buys", "update_reconcile_incident_at")
    op.drop_column("media_buys", "update_recovery_mode")
    op.drop_column("media_buys", "update_adapter_invoked_at")
    op.drop_column("media_buys", "update_lease_expires_at")
    op.drop_column("media_buys", "update_lease_id")
