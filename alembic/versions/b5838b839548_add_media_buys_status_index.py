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
killed), it can leave an INVALID index behind under the same name --
IF NOT EXISTS only checks the catalog NAME, not pg_index.indisvalid, so a
naive retry would silently skip rebuilding it and "succeed" while leaving
production without a usable index. upgrade() below checks indisvalid and
drops an invalid leftover before retrying CREATE, so a retry after an
interruption self-heals instead of requiring an operator to notice and
intervene manually.

Revision ID: b5838b839548
Revises: 168914d7ca05
Create Date: 2026-07-30 17:32:43.481260

"""

from collections.abc import Sequence

from sqlalchemy import text

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b5838b839548"
down_revision: str | Sequence[str] | None = "168914d7ca05"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX_NAME = "idx_media_buys_status"


def upgrade() -> None:
    """Create the status index declared in the model but never migrated.

    Self-heals a leftover INVALID index from a prior interrupted CONCURRENTLY
    build (see module docstring) -- IF NOT EXISTS alone would silently skip
    rebuilding it since it only checks the catalog name, not indisvalid.

    The lookup resolves through ``to_regclass`` rather than matching
    ``pg_class.relname``, because a bare name match is not schema-scoped: an
    identically-named index in ANY schema satisfies it, while the DROP that
    follows resolves through ``search_path``. Those are different indexes, so
    the check could green-light dropping something it never examined -- or
    report a leftover that the DROP then cannot find. ``to_regclass`` resolves
    exactly as the DROP does, so the check and the action always agree, and it
    returns NULL instead of raising when nothing matches.
    """
    with op.get_context().autocommit_block():
        conn = op.get_bind()
        invalid_leftover = conn.execute(
            text("SELECT 1 FROM pg_index WHERE indexrelid = to_regclass(:name) AND NOT indisvalid"),
            {"name": _INDEX_NAME},
        ).scalar()
        if invalid_leftover:
            # IF EXISTS despite the check above: autocommit_block means these run as
            # separate transactions, so a concurrent migration or an operator can drop
            # the index in between and turn a self-heal into a failed deploy.
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX_NAME}")
        op.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_INDEX_NAME} ON media_buys (status)")


def downgrade() -> None:
    """Drop the status index -- the model still declares it, so a fresh
    Base.metadata.create_all() environment is unaffected by this downgrade.

    No indisvalid self-heal needed here (unlike upgrade()): DROP ... IF EXISTS
    removes whatever catalog entry is present by name regardless of validity,
    so it's already correct against an INVALID leftover with no extra check.
    """
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX_NAME}")
