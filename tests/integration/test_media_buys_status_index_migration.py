"""Integration test for the media_buys.status index migration (b5838b839548)
against a real PostgreSQL.

CREATE INDEX CONCURRENTLY IF NOT EXISTS is not fully idempotent: IF NOT EXISTS
only checks whether the catalog NAME already exists, never pg_index.indisvalid.
A prior build interrupted mid-way (process killed, lock timeout, statement
timeout) can leave an INVALID index under the target name -- a plain
(non-unique) index build cannot fail on data content alone, only on external
interruption, so this is a real production risk, not a hypothetical. Without
a self-heal check, a naive retry would see the name already present, skip
rebuilding, and "succeed" while leaving production without a usable index --
exactly the model<->DB drift this migration exists to close. upgrade()
checks indisvalid and drops an invalid leftover before retrying CREATE; see
the migration's own module docstring.

A real interrupted build (process killed, lock timeout) is not reproduced
via timing tricks -- racing a statement_timeout against CONCURRENTLY's build
phases proved flaky on a freshly created, contention-free test database
(nothing else holds an older snapshot to wait on, so the build can complete
before an aggressive timeout fires). Instead the seed helper builds a normal,
valid index and then flips pg_index.indisvalid directly: this reproduces the
exact CATALOG STATE an interrupted build leaves behind, deterministically,
regardless of what caused it. Only the test's OWN seeding needs the
superuser catalog-write access this requires (the local test DB role has
it); the migration code under test only ever SELECTs from pg_index (a
catalog view any role can read) and issues ordinary CONCURRENTLY DDL on a
table it owns -- no elevated privilege of its own.
"""

import pytest
from sqlalchemy import text

from tests.integration.migration_helpers import run_alembic_downgrade, run_alembic_upgrade

PRE_INDEX_REV = "168914d7ca05"  # revision immediately before this index migration
INDEX_REV = "b5838b839548"
INDEX_NAME = "idx_media_buys_status"


def _index_validity(engine) -> bool | None:
    """Return indisvalid for INDEX_NAME, or None if no catalog entry exists at all."""
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT indisvalid FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid WHERE c.relname = :name"),
            {"name": INDEX_NAME},
        ).fetchone()
    return None if row is None else bool(row[0])


def _create_valid_index(engine) -> None:
    """Build a normal, valid index under INDEX_NAME with no alembic
    involvement at all -- the shared primitive every seed helper below
    needs, so a test can plant a pre-existing index without going through
    a real upgrade() call (which would advance alembic's tracked revision
    and make a later "upgrade to the same target" a no-op -- see
    test_upgrade_is_idempotent_against_an_already_valid_index)."""
    with engine.connect() as conn:
        conn = conn.execution_options(isolation_level="AUTOCOMMIT")
        conn.execute(text(f"CREATE INDEX CONCURRENTLY {INDEX_NAME} ON media_buys (status)"))


def _corrupt_existing_index_to_invalid(engine) -> None:
    """Flip an ALREADY-CREATED index's indisvalid to false in place, with no
    CREATE of its own -- for scenarios where alembic's tracked revision must
    already be past INDEX_REV (so downgrade() actually runs, rather than
    being a no-op against a database alembic believes is already at the
    target revision). Asserts the corruption took effect.
    """
    with engine.connect() as conn:
        conn = conn.execution_options(isolation_level="AUTOCOMMIT")
        conn.execute(
            text(
                "UPDATE pg_index SET indisvalid = false "
                "WHERE indexrelid = (SELECT oid FROM pg_class WHERE relname = :name)"
            ),
            {"name": INDEX_NAME},
        )
    assert _index_validity(engine) is False, "corruption helper failed to invalidate the index"


def _seed_valid_index(engine) -> None:
    """Build a normal, valid index under INDEX_NAME with alembic's tracked
    revision left at PRE_INDEX_REV -- for testing upgrade()'s IF NOT EXISTS
    behavior against a pre-existing valid index, without a real upgrade()
    call reaching alembic's revision tracking first (see
    test_upgrade_is_idempotent_against_an_already_valid_index for why that
    matters). Asserts the seed actually produced a valid index.
    """
    _create_valid_index(engine)
    assert _index_validity(engine) is True, "seed helper failed to produce a valid index"


def _seed_invalid_leftover_index(engine) -> None:
    """Build a normal, valid index under INDEX_NAME, then flip indisvalid to
    false directly -- reproducing the catalog state a real interrupted
    CONCURRENTLY build leaves behind (see module docstring for why this is
    seeded directly rather than via a real interrupted build). Asserts the
    seed actually produced that state before any caller trusts it.
    """
    _create_valid_index(engine)
    _corrupt_existing_index_to_invalid(engine)


@pytest.mark.requires_db
class TestMediaBuysStatusIndexMigration:
    def test_upgrade_creates_valid_index_from_scratch(self, migration_db_fresh):
        """Baseline: no leftover, upgrade() creates a fresh, valid index."""
        engine, db_url = migration_db_fresh
        run_alembic_upgrade(db_url, PRE_INDEX_REV)
        assert _index_validity(engine) is None, "sanity check: index must not exist yet"

        run_alembic_upgrade(db_url, INDEX_REV)

        assert _index_validity(engine) is True, "upgrade() must leave a valid index"

    def test_upgrade_is_idempotent_against_an_already_valid_index(self, migration_db_fresh):
        """upgrade()'s IF NOT EXISTS must not raise when the index already
        exists and is valid -- e.g. a redeploy re-applying the same head
        after another process already created it, or a prior partial run
        that got as far as CREATE but not alembic's own version stamp.

        Seeds the index directly at PRE_INDEX_REV rather than by calling
        run_alembic_upgrade(db_url, INDEX_REV) twice: alembic's own version
        tracking treats "upgrade to a revision already applied" as a no-op
        and never re-invokes upgrade() at all, so calling it twice would
        pass this test regardless of whether IF NOT EXISTS does anything --
        confirmed by removing IF NOT EXISTS from the migration and seeing
        the two-calls form of this test stay green. Seeding the index
        directly, then upgrading for the FIRST time, actually exercises
        upgrade()'s own guard against a pre-existing valid index.
        """
        engine, db_url = migration_db_fresh
        run_alembic_upgrade(db_url, PRE_INDEX_REV)
        _seed_valid_index(engine)

        run_alembic_upgrade(db_url, INDEX_REV)  # must not raise against the pre-existing valid index

        assert _index_validity(engine) is True

    def test_upgrade_self_heals_an_invalid_leftover_index(self, migration_db_fresh):
        """The regression test for the self-heal fix: an interrupted prior
        build leaves an INVALID index under the target name. Without the
        indisvalid check, IF NOT EXISTS alone would see the name already
        present and silently skip rebuilding it."""
        engine, db_url = migration_db_fresh
        run_alembic_upgrade(db_url, PRE_INDEX_REV)
        _seed_invalid_leftover_index(engine)

        run_alembic_upgrade(db_url, INDEX_REV)

        assert _index_validity(engine) is True, "upgrade() must self-heal an invalid leftover into a valid index"

    def test_downgrade_drops_the_index_regardless_of_validity(self, migration_db_fresh):
        """downgrade() needs no indisvalid check of its own: DROP ... IF EXISTS
        removes the catalog entry by name regardless of validity, so an
        invalid leftover is already handled correctly with no extra logic.

        Advances alembic to INDEX_REV via a real upgrade() first (creating a
        valid index) before corrupting it -- otherwise alembic's tracked
        revision never actually moves past PRE_INDEX_REV, and "downgrade to
        PRE_INDEX_REV" is a no-op against a database already there, never
        invoking b5838b839548's downgrade() at all.
        """
        engine, db_url = migration_db_fresh
        run_alembic_upgrade(db_url, INDEX_REV)
        _corrupt_existing_index_to_invalid(engine)

        run_alembic_downgrade(db_url, PRE_INDEX_REV)

        assert _index_validity(engine) is None, "downgrade() must remove the index entirely, valid or not"

    def test_migration_roundtrip_ends_with_a_valid_index(self, migration_db_fresh):
        """upgrade -> downgrade -> upgrade (the sequence CI's mandatory
        Migration Roundtrip job runs on every PR) must end with a valid
        index, pinned here at this migration's own boundary."""
        engine, db_url = migration_db_fresh
        run_alembic_upgrade(db_url, INDEX_REV)
        assert _index_validity(engine) is True

        run_alembic_downgrade(db_url, PRE_INDEX_REV)
        assert _index_validity(engine) is None

        run_alembic_upgrade(db_url, INDEX_REV)
        assert _index_validity(engine) is True
