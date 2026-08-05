"""Shared helpers for migration integration tests.

Provides common utilities for tests that run Alembic migrations against
isolated PostgreSQL databases:
- parse_postgres_url(): Parse DATABASE_URL into connection components
- run_alembic_upgrade(): Run Alembic upgrade to a specific revision
- run_alembic_downgrade(): Run Alembic downgrade to a specific revision

The shared ``migration_db`` fixture lives in ``conftest.py`` (pytest auto-discovers
fixtures from conftest without explicit imports, avoiding F811 lint errors).
"""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy.engine import Engine


def parse_postgres_url() -> tuple[str, str, str, int] | None:
    """Parse DATABASE_URL into connection components.

    Returns (user, password, host, port) or None if DATABASE_URL is not set
    or does not match the expected PostgreSQL format.
    """
    postgres_url = os.environ.get("DATABASE_URL", "")
    match = re.match(r"postgresql://([^:]+):([^@]+)@([^:]+):(\d+)/(.+)", postgres_url)
    if not match:
        return None
    user, password, host, port_str, _ = match.groups()
    return user, password, host, int(port_str)


def _run_alembic_command(db_url: str, command_fn, target_revision: str) -> None:
    """Run an Alembic command with temporary DATABASE_URL override.

    Temporarily sets DATABASE_URL for alembic/env.py which reads from
    DatabaseConfig.get_connection_string().
    """
    from alembic.config import Config

    old_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = db_url
    try:
        alembic_cfg = Config("alembic.ini")
        command_fn(alembic_cfg, target_revision)
    finally:
        if old_url:
            os.environ["DATABASE_URL"] = old_url
        elif "DATABASE_URL" in os.environ:
            del os.environ["DATABASE_URL"]


def run_alembic_upgrade(db_url: str, target_revision: str) -> None:
    """Run Alembic upgrade to a specific revision."""
    from alembic import command

    _run_alembic_command(db_url, command.upgrade, target_revision)


def run_alembic_downgrade(db_url: str, target_revision: str) -> None:
    """Run Alembic downgrade to a specific revision."""
    from alembic import command

    _run_alembic_command(db_url, command.downgrade, target_revision)


def isolated_postgres_db() -> Iterator[tuple[Engine, str]]:
    """Create an isolated PostgreSQL database, yield (engine, db_url), then drop it.

    Shared provisioning logic for both the module-scoped ``migration_db`` fixture
    and the function-scoped ``migration_db_fresh`` fixture in conftest.py -- both
    need identical create/teardown mechanics, just at different fixture scopes.
    Calls ``pytest.skip()`` directly (rather than returning a sentinel) so either
    fixture can simply ``yield from isolated_postgres_db()``.
    """
    import psycopg2
    from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT
    from sqlalchemy import create_engine

    parsed = parse_postgres_url()
    if not parsed:
        # FAIL in CI, skip only locally. 27 tests across 5 migration files ride this
        # helper, and a skip is indistinguishable from a pass in the aggregate — so a
        # CI job that lost its DATABASE_URL would report green while running none of
        # the migration coverage. Locally, skipping is the right convenience: a
        # developer without Postgres should not be blocked.
        if os.environ.get("CI"):
            pytest.fail(
                "DATABASE_URL is unset or not a PostgreSQL URL, but CI is set. Every test "
                "using migration_db / migration_db_fresh would silently skip and the job "
                "would report green without running any migration coverage."
            )
        pytest.skip("Requires PostgreSQL DATABASE_URL (set CI=1 to make this a failure)")

    user, password, host, port = parsed
    db_name = f"test_migration_{uuid.uuid4().hex[:8]}"

    def _connect_to_postgres_db():
        return psycopg2.connect(host=host, port=port, user=user, password=password, database="postgres")

    conn = _connect_to_postgres_db()
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    cur = conn.cursor()
    cur.execute(f'CREATE DATABASE "{db_name}"')
    cur.close()
    conn.close()

    db_url = f"postgresql://{user}:{password}@{host}:{port}/{db_name}"
    engine = create_engine(db_url, echo=False)

    yield engine, db_url

    engine.dispose()
    try:
        conn = _connect_to_postgres_db()
        conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
        cur = conn.cursor()
        cur.execute(
            f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            f"WHERE datname = '{db_name}' AND pid <> pg_backend_pid()"
        )
        cur.execute(f'DROP DATABASE IF EXISTS "{db_name}"')
        cur.close()
        conn.close()
    except Exception:
        pass
