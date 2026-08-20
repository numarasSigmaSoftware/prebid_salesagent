"""Migration and operational backfill sanity for ``media_buys.confirmed_at``.

The Alembic revision only adds the nullable column. The separately-run
operational job backfills historical rows in independently committed batches,
which keeps migration locks bounded on a production backlog.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import event, text

from tests.integration.migration_helpers import (
    column_exists,
    run_alembic_downgrade,
    run_alembic_upgrade,
    seed_tenant,
)

# Migration under test and its parent
CONFIRMED_AT_REV = "2c4e6a7b8d9e"
PRE_REV = "1497aa06013c"

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

_APPROVED = datetime(2026, 1, 5, tzinfo=UTC)
_CREATED = datetime(2026, 1, 1, tzinfo=UTC)
_TEST_TENANT_IDS = ("t_conf_schema", "t_conf_backfill")


def _clear_seed_rows(engine) -> None:
    """Reset this module's seed data in the shared migration-test database."""
    tenant_ids = list(_TEST_TENANT_IDS)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM media_buys WHERE tenant_id = ANY(:tenant_ids)"), {"tenant_ids": tenant_ids})
        conn.execute(text("DELETE FROM principals WHERE tenant_id = ANY(:tenant_ids)"), {"tenant_ids": tenant_ids})
        conn.execute(text("DELETE FROM tenants WHERE tenant_id = ANY(:tenant_ids)"), {"tenant_ids": tenant_ids})


def _insert_media_buy(
    engine,
    media_buy_id: str,
    *,
    tenant_id: str,
    status: str,
    approved_at: str | None,
    created_at: str | None,
) -> None:
    with engine.connect() as conn:
        conn.execute(
            text(
                "INSERT INTO media_buys (media_buy_id, tenant_id, principal_id, order_name, advertiser_name, "
                "budget, currency, start_date, end_date, status, approved_at, raw_request, created_at, updated_at) "
                "VALUES (:id, :tenant_id, :principal_id, :name, 'Adv', 100.00, 'USD', "
                "'2026-01-01', '2026-02-01', :status, :approved_at, '{}', "
                f"{'now()' if created_at is None else ':created_at'}, NOW())"
            ),
            {
                "id": media_buy_id,
                "tenant_id": tenant_id,
                "principal_id": f"p_{tenant_id}",
                "name": f"Order {media_buy_id}",
                "status": status,
                "approved_at": approved_at,
                **({"created_at": created_at} if created_at is not None else {}),
            },
        )
        conn.commit()


def _seed_pre_migration_media_buys(engine, tenant_id: str) -> None:
    """Insert tenant/principal once, plus one row per backfill class."""
    seed_tenant(engine, tenant_id, subdomain=f"{tenant_id}-migration-test")
    with engine.connect() as conn:
        conn.execute(
            text(
                "INSERT INTO principals (tenant_id, principal_id, name, platform_mappings, access_token) "
                "VALUES (:tenant_id, :principal_id, 'Confirmed-At Principal', '{}', :access_token)"
            ),
            {
                "tenant_id": tenant_id,
                "principal_id": f"p_{tenant_id}",
                "access_token": f"tok_{tenant_id}",
            },
        )
        conn.commit()

    # Unconfirmed status, no approval -> stays NULL.
    _insert_media_buy(
        engine,
        f"{tenant_id}_unconfirmed",
        tenant_id=tenant_id,
        status="pending_approval",
        approved_at=None,
        created_at=_CREATED,
    )
    # Confirmed status, approved_at set -> approved_at wins over created_at.
    _insert_media_buy(
        engine,
        f"{tenant_id}_confirmed_approved",
        tenant_id=tenant_id,
        status="active",
        approved_at=_APPROVED,
        created_at=_CREATED,
    )
    # Confirmed status, no approval (synchronous auto-approve path) -> created_at.
    _insert_media_buy(
        engine,
        f"{tenant_id}_confirmed_sync",
        tenant_id=tenant_id,
        status="ACTIVE",
        approved_at=None,
        created_at=_CREATED,
    )
    # Historical draft+approved_at hold for a creative-blocked buy.
    _insert_media_buy(
        engine,
        f"{tenant_id}_draft_approved",
        tenant_id=tenant_id,
        status="draft",
        approved_at=_APPROVED,
        created_at=_CREATED,
    )
    # Confirmed status, neither instant recorded -> the termination floor (now()), not NULL forever.
    _insert_media_buy(
        engine,
        f"{tenant_id}_confirmed_no_instants",
        tenant_id=tenant_id,
        status="active",
        approved_at=None,
        created_at=None,
    )


def test_upgrade_leaves_historical_rows_for_operational_backfill_and_downgrade_drops_column(migration_db):
    engine, db_url = migration_db
    tenant_id = "t_conf_schema"
    run_alembic_upgrade(db_url, PRE_REV)
    _clear_seed_rows(engine)
    assert not column_exists(engine, "media_buys", "confirmed_at")

    _seed_pre_migration_media_buys(engine, tenant_id)

    run_alembic_upgrade(db_url, CONFIRMED_AT_REV)

    with engine.connect() as conn:
        rows = dict(
            conn.execute(
                text("SELECT media_buy_id, confirmed_at FROM media_buys WHERE tenant_id = :tenant_id"),
                {"tenant_id": tenant_id},
            ).all()
        )

    assert set(rows.values()) == {None}, "schema migration must not hold a historical-row rewrite transaction"

    run_alembic_downgrade(db_url, PRE_REV)
    assert not column_exists(engine, "media_buys", "confirmed_at")
    with engine.connect() as conn:
        count = conn.execute(
            text("SELECT COUNT(*) FROM media_buys WHERE tenant_id = :tenant_id"), {"tenant_id": tenant_id}
        ).scalar_one()
    assert count == 5


def test_operational_backfill_uses_multiple_committed_batches(migration_db):
    """The backfill retains historical semantics without extending migration locks."""
    from scripts.ops.backfill_media_buy_confirmed_at import backfill_confirmed_at

    engine, db_url = migration_db
    tenant_id = "t_conf_backfill"
    run_alembic_upgrade(db_url, PRE_REV)
    _clear_seed_rows(engine)
    _seed_pre_migration_media_buys(engine, tenant_id)
    run_alembic_upgrade(db_url, CONFIRMED_AT_REV)

    # Four eligible rows (of the five seeded) prove the loop advances past a
    # single-row batch and confirm eligibility is case-insensitive like the
    # model's canonical rule (MEDIA_BUY_UNCONFIRMED_STATUSES).
    commits: list[object] = []
    on_commit = commits.append
    event.listen(engine, "commit", on_commit)
    try:
        assert backfill_confirmed_at(engine, batch_rows=1) == 4
    finally:
        event.remove(engine, "commit", on_commit)
    assert len(commits) == 5, "each one-row batch must commit independently, plus the final empty probe"
    assert backfill_confirmed_at(engine, batch_rows=1) == 0, "the job must be idempotent"

    with engine.connect() as conn:
        rows = dict(
            conn.execute(
                text("SELECT media_buy_id, confirmed_at FROM media_buys WHERE tenant_id = :tenant_id"),
                {"tenant_id": tenant_id},
            ).all()
        )

    assert rows[f"{tenant_id}_unconfirmed"] is None
    assert rows[f"{tenant_id}_confirmed_approved"] == _APPROVED
    assert rows[f"{tenant_id}_confirmed_sync"] == _CREATED
    assert rows[f"{tenant_id}_draft_approved"] == _APPROVED
    assert rows[f"{tenant_id}_confirmed_no_instants"] is not None

    run_alembic_downgrade(db_url, PRE_REV)
