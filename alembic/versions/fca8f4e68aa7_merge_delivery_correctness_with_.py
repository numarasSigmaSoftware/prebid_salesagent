"""merge delivery-correctness with upstream status normalization

Revision ID: fca8f4e68aa7
Revises: b5838b839548, 9b2d4f6c1a37
Create Date: 2026-08-31 13:44:53.769530

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "fca8f4e68aa7"
down_revision: Union[str, Sequence[str], None] = ("b5838b839548", "9b2d4f6c1a37")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
