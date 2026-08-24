"""Schema-probe and seed helpers shared by migration tests.

Lives in ``tests/helpers/`` rather than beside the Alembic runners in
``tests/integration/migration_helpers.py`` because this is the directory the
repository-pattern guard (``tests/unit/test_architecture_repository_pattern.py``)
already scans — shared test substrate placed outside a guard's scan set is a
silent escape hatch, and moving the code is narrower than widening the guard's
glob. It is also where this PR already put ``read_back_media_buy``.

The Alembic upgrade/downgrade runners and ``parse_postgres_url`` stay in
``tests/integration/migration_helpers.py``: they drive Alembic rather than touch
rows, and several integration modules import them from there.
"""

from __future__ import annotations

from sqlalchemy import text


def column_exists(engine, table: str, column: str) -> bool:
    """Whether ``table.column`` exists (information_schema probe).

    Shared by migration tests that assert a column is added/dropped, replacing
    per-test hand-rolled copies of the same query.
    """
    return get_column_info(engine, table, column) is not None


def get_column_info(engine, table: str, column: str) -> tuple[str, str | None] | None:
    """``(is_nullable, column_default)`` for ``table.column``, or None if absent."""
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT is_nullable, column_default FROM information_schema.columns "
                "WHERE table_name = :t AND column_name = :c"
            ),
            {"t": table, "c": column},
        ).first()
    return (row[0], row[1]) if row else None


def seed_tenant(engine, tenant_id: str, *, subdomain: str) -> None:
    """Insert a minimal tenant row on a migration-managed schema.

    Raw SQL deliberately: a migration test runs against an OLD schema revision,
    where the current ORM models do not necessarily match the columns that exist,
    so the repository layer cannot be used here. Each test still seeds its own
    domain-specific child rows.
    """
    with engine.connect() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (tenant_id, name, subdomain, created_at, updated_at) "
                "VALUES (:tid, :name, :sub, NOW(), NOW())"
            ),
            {"tid": tenant_id, "name": f"{tenant_id} tenant", "sub": subdomain},
        )
        conn.commit()
