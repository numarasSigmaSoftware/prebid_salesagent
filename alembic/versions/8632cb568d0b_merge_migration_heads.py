"""Merge migration heads.

Reconciles the two heads produced by merging upstream/main's media-buy-status
and confirmed_at/revision migration chain (ending at `9b2d4f6c1a37`) with this
branch's own `b5c8f1d20a37` (task and webhook-reliability tables). No schema
changes — purely a branch join.

Revision ID: 8632cb568d0b
Revises: 9b2d4f6c1a37, b5c8f1d20a37
"""

revision = "8632cb568d0b"
down_revision = ("9b2d4f6c1a37", "b5c8f1d20a37")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """No-op: this migration only joins the two branch heads."""
    pass


def downgrade() -> None:
    """No-op: this migration only joins the two branch heads."""
    pass
