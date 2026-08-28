"""add media_buys.revision

Persisted monotonic optimistic-concurrency counter for media buys
(AdCP 3.1.1 `revision` response field). A persisted counter — bumped by the
repository on every successful mutation — is the only way to guarantee
strict monotonicity: anything derived from timestamps collides when two
updates land within the clock resolution.

Existing rows are backfilled to 1 via the server default: revision was
never emitted before this migration, so starting every buy at revision 1
is the correct baseline.

Cherry-picked from PR #1544, re-parented onto this branch's head
(823974a5553e) instead of a164b85bab9e — the single-head invariant is
enforced by tests/unit/test_architecture_single_migration_head.py.

Revision ID: 1497aa06013c
Revises: 823974a5553e
Create Date: 2026-07-03 13:40:00.837250

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "1497aa06013c"
down_revision: str | Sequence[str] | None = "823974a5553e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add media_buys.revision, backfilling existing rows to 1.

    IF NOT EXISTS: this branch's history merges (at 8632cb568d0b) with
    b5c8f1d20a37, which also idempotently ensures this column for its own
    isolated-history callers (predates this merge). A full upgrade-to-head
    applies both parents before the merge point, so whichever runs second
    must be a no-op rather than colliding.
    """
    op.execute("ALTER TABLE media_buys ADD COLUMN IF NOT EXISTS revision INTEGER NOT NULL DEFAULT 1")


def downgrade() -> None:
    """Drop media_buys.revision.

    IF EXISTS, mirroring the idempotent add above and b5c8f1d20a37's own
    idempotent drop -- a full downgrade past both merge parents must not
    have the second one to run fail on an already-dropped column.
    """
    op.execute("ALTER TABLE media_buys DROP COLUMN IF EXISTS revision")
