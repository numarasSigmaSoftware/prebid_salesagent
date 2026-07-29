"""Integration test for the webhook_delivery_log credential-redaction migration
(168914d7ca05) against a real PostgreSQL.

Rows written before PR #1575 held the complete, unredacted webhook URL (and
could carry the same data via unsanitized exception text in error_message).
This migration must scrub every existing row when it runs -- verifies upgrade
scrubs credential-bearing legacy data, preserves NULL error_message as NULL
(rather than corrupting "no error occurred" into a fake redacted string), and
that downgrade is an honest, explicit failure rather than a silent no-op.
"""

import pytest
from sqlalchemy import text

from tests.integration.migration_helpers import run_alembic_downgrade, run_alembic_upgrade

PRE_REDACTION_REV = "b7c9d2e4f6a8"  # revision immediately before the redaction migration
REDACTION_REV = "168914d7ca05"

LEGACY_CREDENTIALED_URL = "https://buyer:s3cr3t-password@secret.example/hook?token=leaked-legacy-value"
LEGACY_ERROR_MESSAGE = f"HTTP 404: 404 Client Error: Not Found for url: {LEGACY_CREDENTIALED_URL}"
CREDENTIAL_SUBSTRINGS = ("buyer", "s3cr3t-password", "secret.example", "leaked-legacy-value")


def _seed_legacy_rows(engine):
    """Seed tenant/principal/media_buy + three webhook_delivery_log rows:
    one with a credential-bearing URL and error_message (the common case),
    one with a credential-bearing URL but NULL error_message (successful
    delivery attempt logged, later attempt failed with no error captured --
    NULL must stay NULL, not become a fake redacted string), and one whose
    error_message has no credential at all (must still be swept -- the
    migration can't distinguish "safe" legacy text from "unsafe," so it
    redacts unconditionally, matching the blanket non-correlating design)."""
    with engine.connect() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (tenant_id, name, subdomain, created_at, updated_at) "
                "VALUES ('legacy-tenant', 'Legacy Tenant', 'legacy-test', NOW(), NOW())"
            )
        )
        conn.execute(
            text(
                "INSERT INTO principals (tenant_id, principal_id, name, access_token, "
                "platform_mappings, created_at, updated_at) "
                "VALUES ('legacy-tenant', 'legacy-principal', 'Legacy Principal', 'legacy-token', "
                '\'{"mock": {"advertiser_id": "test"}}\'::jsonb, NOW(), NOW())'
            )
        )
        conn.execute(
            text(
                "INSERT INTO media_buys (media_buy_id, tenant_id, principal_id, order_name, "
                "advertiser_name, status, raw_request, start_date, end_date, budget) "
                "VALUES ('legacy-media-buy', 'legacy-tenant', 'legacy-principal', 'Legacy Order', "
                "'Legacy Advertiser', 'active', '{}'::jsonb, CURRENT_DATE, "
                "CURRENT_DATE + interval '30 days', 1000)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO webhook_delivery_log "
                "(id, tenant_id, principal_id, media_buy_id, webhook_url, task_type, status, error_message) "
                "VALUES "
                "('legacy-log-with-error', 'legacy-tenant', 'legacy-principal', 'legacy-media-buy', "
                " :url, 'delivery_report', 'failed', :err), "
                "('legacy-log-null-error', 'legacy-tenant', 'legacy-principal', 'legacy-media-buy', "
                " :url, 'delivery_report', 'success', NULL), "
                "('legacy-log-safe-error', 'legacy-tenant', 'legacy-principal', 'legacy-media-buy', "
                " :url, 'delivery_report', 'failed', 'Connection reset by peer')"
            ),
            {"url": LEGACY_CREDENTIALED_URL, "err": LEGACY_ERROR_MESSAGE},
        )
        conn.commit()


def _fetch_row(engine, log_id):
    with engine.connect() as conn:
        result = conn.execute(
            text("SELECT webhook_url, error_message FROM webhook_delivery_log WHERE id = :id"),
            {"id": log_id},
        )
        row = result.fetchone()
        assert row is not None, f"row {log_id} not found"
        return row


@pytest.mark.requires_db
class TestWebhookDeliveryLogRedactionMigration:
    def test_upgrade_redacts_url_and_error_message(self, migration_db):
        engine, db_url = migration_db

        run_alembic_upgrade(db_url, PRE_REDACTION_REV)
        _seed_legacy_rows(engine)

        # Confirm the legacy data really is unredacted before the migration runs.
        webhook_url, error_message = _fetch_row(engine, "legacy-log-with-error")
        assert webhook_url == LEGACY_CREDENTIALED_URL
        assert error_message == LEGACY_ERROR_MESSAGE

        run_alembic_upgrade(db_url, REDACTION_REV)

        webhook_url, error_message = _fetch_row(engine, "legacy-log-with-error")
        assert webhook_url == "<redacted-by-migration>"
        assert error_message == "<redacted-by-migration>"

    def test_upgrade_preserves_null_error_message(self, migration_db):
        """A row with NO error (NULL error_message) must stay NULL -- turning it
        into a "redacted" string would misrepresent a successful delivery as
        having had a scrubbed error."""
        engine, _ = migration_db
        webhook_url, error_message = _fetch_row(engine, "legacy-log-null-error")
        assert webhook_url == "<redacted-by-migration>"
        assert error_message is None

    def test_upgrade_redacts_error_message_with_no_credential_too(self, migration_db):
        """The migration can't distinguish "this legacy error_message happens to
        be safe" from "this one leaks a credential" -- it redacts every non-null
        value unconditionally, the same blanket design as the runtime fix."""
        engine, _ = migration_db
        _webhook_url, error_message = _fetch_row(engine, "legacy-log-safe-error")
        assert error_message == "<redacted-by-migration>"

    def test_no_legacy_credentials_survive_anywhere_in_the_table(self, migration_db):
        engine, _ = migration_db
        with engine.connect() as conn:
            result = conn.execute(text("SELECT webhook_url, error_message FROM webhook_delivery_log"))
            rows = result.fetchall()
        assert rows, "expected seeded rows to still be present"
        for webhook_url, error_message in rows:
            for substring in CREDENTIAL_SUBSTRINGS:
                assert substring not in (webhook_url or ""), f"{substring!r} survived in webhook_url"
                assert substring not in (error_message or ""), f"{substring!r} survived in error_message"

    def test_downgrade_refuses_rather_than_silently_no_opping(self, migration_db):
        """The original values were destroyed on upgrade and never archived --
        downgrade must fail loudly, not silently leave the redacted state in
        place while claiming to have "reverted" it."""
        _engine, db_url = migration_db

        with pytest.raises(Exception, match="destroys credential data"):
            run_alembic_downgrade(db_url, PRE_REDACTION_REV)
