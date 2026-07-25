"""Round-trip coverage for anonymous reads and downstream mutation claims."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import inspect, text

from tests.integration.migration_helpers import run_alembic_downgrade, run_alembic_upgrade

PRE_RESERVATION_REV = "823974a5553e"
RESERVATION_REV = "f3a1c92b47de"
COMPLETE_GUARANTEES_REV = "a4d7e8c91f20"

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]


def _seed_scope_and_attempts(engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (tenant_id, name, subdomain, created_at, updated_at) "
                "VALUES ('idem_migration_t', 'Idempotency Migration', 'idem-migration', NOW(), NOW())"
            )
        )
        conn.execute(
            text(
                "INSERT INTO principals "
                "(principal_id, tenant_id, name, platform_mappings, access_token, created_at) "
                "VALUES ('idem_migration_p', 'idem_migration_t', 'Principal', '{}', "
                "'idem_migration_token', NOW())"
            )
        )
        for attempt_id, status, response in (
            ("completed-attempt", "completed", {"status": "completed", "response": {"ok": True}}),
            ("in-flight-attempt", "in_flight", None),
        ):
            conn.execute(
                text(
                    "INSERT INTO idempotency_attempts "
                    "(attempt_id, tenant_id, principal_id, account_id, tool_name, idempotency_key, "
                    "status, response_envelope, payload_hash, expires_at, created_at) "
                    "VALUES (:attempt_id, 'idem_migration_t', 'idem_migration_p', NULL, "
                    "'update_media_buy', :key, :status, CAST(:response AS jsonb), :payload_hash, "
                    "NOW() + INTERVAL '1 day', NOW())"
                ),
                {
                    "attempt_id": attempt_id,
                    "key": f"{attempt_id}-key",
                    "status": status,
                    "response": json.dumps(response) if response is not None else None,
                    "payload_hash": f"{attempt_id}-hash",
                },
            )


def test_upgrade_downgrade_upgrade_preserves_documented_state(migration_db) -> None:
    engine, db_url = migration_db
    run_alembic_upgrade(db_url, RESERVATION_REV)
    _seed_scope_and_attempts(engine)

    run_alembic_upgrade(db_url, COMPLETE_GUARANTEES_REV)
    with engine.begin() as conn:
        assert (
            conn.execute(
                text("SELECT operation_class FROM idempotency_attempts WHERE attempt_id='completed-attempt'")
            ).scalar_one()
            == "write"
        )
        conn.execute(
            text(
                "INSERT INTO idempotency_attempts "
                "(attempt_id, tenant_id, principal_id, account_id, tool_name, operation_class, "
                "idempotency_key, status, response_envelope, payload_hash, expires_at, created_at) "
                "VALUES ('anonymous-read', 'idem_migration_t', NULL, NULL, 'get_products', 'read', "
                "'anonymous-read-key', 'completed', CAST(:response AS jsonb), 'anonymous-hash', "
                "NOW() + INTERVAL '1 day', NOW())"
            ),
            {"response": json.dumps({"status": "completed", "response": {"products": []}})},
        )
        conn.execute(
            text(
                "INSERT INTO downstream_mutation_claims "
                "(claim_id, tenant_id, principal_id, account_id, idempotency_key, provider, "
                "operation_key, downstream_request_id, request_hash, status, result_metadata, "
                "expires_at, created_at, updated_at) "
                "VALUES ('claim-1', 'idem_migration_t', 'idem_migration_p', NULL, "
                "'completed-attempt-key', 'mock', 'campaign:pause_media_buy', "
                "'downstream-request', 'claim-hash', 'applied', CAST(:result AS jsonb), "
                "NOW() + INTERVAL '1 day', NOW(), NOW())"
            ),
            {"result": json.dumps({"response": {"media_buy_id": "mb-1"}})},
        )

    run_alembic_downgrade(db_url, PRE_RESERVATION_REV)
    with engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT COUNT(*) FROM idempotency_attempts WHERE attempt_id='completed-attempt'")
            ).scalar_one()
            == 1
        )
        assert (
            conn.execute(
                text("SELECT COUNT(*) FROM idempotency_attempts WHERE attempt_id='in-flight-attempt'")
            ).scalar_one()
            == 0
        )
        assert (
            conn.execute(
                text("SELECT COUNT(*) FROM idempotency_attempts WHERE attempt_id='anonymous-read'")
            ).scalar_one()
            == 0
        )
    assert "downstream_mutation_claims" not in inspect(engine).get_table_names()

    run_alembic_upgrade(db_url, COMPLETE_GUARANTEES_REV)
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT status, operation_class, response_envelope "
                "FROM idempotency_attempts WHERE attempt_id='completed-attempt'"
            )
        ).one()
        assert row.status == "completed"
        assert row.operation_class == "write"
        assert row.response_envelope["response"] == {"ok": True}
    assert "downstream_mutation_claims" in inspect(engine).get_table_names()
