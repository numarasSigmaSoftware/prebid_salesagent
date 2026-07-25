"""Round-trip coverage for anonymous reads and downstream mutation claims."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import inspect, text

from tests.integration.migration_helpers import run_alembic_downgrade, run_alembic_upgrade

PRE_RESERVATION_REV = "823974a5553e"
RESERVATION_REV = "f3a1c92b47de"
COMPLETE_GUARANTEES_REV = "b5c8f1d20a37"

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
        conn.execute(
            text(
                "INSERT INTO media_buys "
                "(media_buy_id, tenant_id, principal_id, order_name, advertiser_name, "
                "start_date, end_date, status, raw_request) "
                "VALUES ('idem-migration-buy', 'idem_migration_t', 'idem_migration_p', "
                "'Migration Order', 'Migration Advertiser', CURRENT_DATE, "
                "CURRENT_DATE + 1, 'active', '{}')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO push_notification_configs "
                "(id, tenant_id, principal_id, url, is_active) "
                "VALUES ('idem-migration-callback', 'idem_migration_t', "
                "'idem_migration_p', 'https://buyer.example/callback', TRUE)"
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
        assert (
            conn.execute(text("SELECT revision FROM media_buys WHERE media_buy_id='idem-migration-buy'")).scalar_one()
            == 1
        )
        callback = conn.execute(
            text(
                "SELECT media_buy_id, operation_id, token, application_context, "
                "last_event_key, last_event_sequence "
                "FROM push_notification_configs WHERE id='idem-migration-callback'"
            )
        ).one()
        assert callback.media_buy_id is None
        assert callback.operation_id is None
        assert callback.token is None
        assert callback.application_context is None
        assert callback.last_event_key is None
        assert callback.last_event_sequence == 0
        conn.execute(
            text(
                "UPDATE push_notification_configs SET "
                "media_buy_id='idem-migration-buy', operation_id='create-op', "
                "token='callback-token', application_context=CAST(:context AS jsonb), "
                "last_event_key='event-1', last_event_sequence=1 "
                "WHERE id='idem-migration-callback'"
            ),
            {"context": json.dumps({"trace_id": "migration-trace"})},
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
        anonymous = conn.execute(
            text(
                "SELECT principal_id, operation_class, response_envelope "
                "FROM idempotency_attempts WHERE attempt_id='anonymous-read'"
            )
        ).one()
        assert anonymous.principal_id is None
        assert anonymous.operation_class == "read"
        assert anonymous.response_envelope["response"] == {"products": []}
        claim = conn.execute(
            text(
                "SELECT status, downstream_request_id, request_hash, result_metadata "
                "FROM downstream_mutation_claims WHERE claim_id='claim-1'"
            )
        ).one()
        assert claim.status == "applied"
        assert claim.downstream_request_id == "downstream-request"
        assert claim.request_hash == "claim-hash"
        assert claim.result_metadata == {"response": {"media_buy_id": "mb-1"}}

    columns = {column["name"]: column for column in inspect(engine).get_columns("downstream_mutation_claims")}
    assert str(columns["result_metadata"]["type"]) == "JSONB"
    task_columns = {column["name"]: column for column in inspect(engine).get_columns("a2a_tasks")}
    assert str(task_columns["task_payload"]["type"]) == "JSONB"
    notification_columns = {
        column["name"]: column for column in inspect(engine).get_columns("a2a_task_notification_events")
    }
    assert str(notification_columns["task_payload"]["type"]) == "JSONB"
    workflow_notification_columns = {
        column["name"]: column for column in inspect(engine).get_columns("workflow_notification_events")
    }
    assert str(workflow_notification_columns["response_data"]["type"]) == "JSONB"
    webhook_log_columns = {column["name"]: column for column in inspect(engine).get_columns("webhook_delivery_log")}
    assert str(webhook_log_columns["event_payload"]["type"]) == "JSONB"
    assert "uq_a2a_tasks_workflow_step" in {index["name"] for index in inspect(engine).get_indexes("a2a_tasks")}
    callback_columns = {column["name"]: column for column in inspect(engine).get_columns("push_notification_configs")}
    assert str(callback_columns["application_context"]["type"]) == "JSONB"
    workflow_columns = {column["name"] for column in inspect(engine).get_columns("workflow_steps")}
    assert {
        "processing_started_at",
        "notifications_published_at",
        "notification_claimed_at",
        "notification_claim_token",
        "notification_sequence",
    } <= workflow_columns
    assert "idx_push_notification_configs_media_buy" in {
        index["name"] for index in inspect(engine).get_indexes("push_notification_configs")
    }

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
    assert "a2a_tasks" not in inspect(engine).get_table_names()
    assert "a2a_task_notification_events" not in inspect(engine).get_table_names()
    assert "workflow_notification_events" not in inspect(engine).get_table_names()
    assert "event_payload" not in {column["name"] for column in inspect(engine).get_columns("webhook_delivery_log")}
    assert "revision" not in {column["name"] for column in inspect(engine).get_columns("media_buys")}
    downgraded_workflow_columns = {column["name"] for column in inspect(engine).get_columns("workflow_steps")}
    assert {
        "processing_started_at",
        "notifications_published_at",
        "notification_claimed_at",
        "notification_claim_token",
        "notification_sequence",
    }.isdisjoint(downgraded_workflow_columns)
    downgraded_callback_columns = {
        column["name"] for column in inspect(engine).get_columns("push_notification_configs")
    }
    assert {
        "media_buy_id",
        "operation_id",
        "token",
        "application_context",
        "last_event_key",
        "last_event_sequence",
    }.isdisjoint(downgraded_callback_columns)

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
        assert (
            conn.execute(text("SELECT revision FROM media_buys WHERE media_buy_id='idem-migration-buy'")).scalar_one()
            == 1
        )
        callback = conn.execute(
            text(
                "SELECT media_buy_id, operation_id, token, application_context, "
                "last_event_key, last_event_sequence "
                "FROM push_notification_configs WHERE id='idem-migration-callback'"
            )
        ).one()
        assert callback.media_buy_id is None
        assert callback.operation_id is None
        assert callback.token is None
        assert callback.application_context is None
        assert callback.last_event_key is None
        assert callback.last_event_sequence == 0
    assert "downstream_mutation_claims" in inspect(engine).get_table_names()
    assert "a2a_tasks" in inspect(engine).get_table_names()
    assert "event_payload" in {column["name"] for column in inspect(engine).get_columns("webhook_delivery_log")}
