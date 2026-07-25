"""Add the seller confirmation instant for media buys.

Historical rows are backfilled by ``scripts/ops/backfill_media_buy_confirmed_at.py``.
The operational job commits each bounded batch independently; Alembic runs this
migration in one transaction and must not perform a large data rewrite here.
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
