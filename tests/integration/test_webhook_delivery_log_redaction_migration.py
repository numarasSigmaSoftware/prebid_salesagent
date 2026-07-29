"""Integration test for the webhook_delivery_log credential-redaction migration
(168914d7ca05) against a real PostgreSQL.

Rows written before PR #1575 held the complete, unredacted webhook URL (and
could carry the same data via unsanitized exception text in error_message).
This migration must scrub every existing row when it runs -- verifies upgrade
scrubs credential-bearing legacy data, preserves NULL error_message as NULL
(rather than corrupting "no error occurred" into a fake redacted string), and
that the CI-mandated upgrade -> downgrade -> upgrade roundtrip succeeds
without un-redacting anything.

The atomic test uses the module-scoped migration_db fixture (fine here: it
seeds, upgrades, and asserts in one method, so there's no cross-test ordering
dependency). The downgrade/roundtrip test uses its OWN function-scoped
migration_db_fresh fixture instead: a migration's data UPDATE fires exactly
once, at its own transition, so on a shared already-migrated database
"upgrade to the revision before mine" is a no-op that would let freshly-seeded
rows silently skip the redaction entirely -- a fresh database sidesteps that
whole class of bug rather than working around it with a `pass` no-op.
"""

import pytest
from sqlalchemy import text

from tests.integration.migration_helpers import run_alembic_downgrade, run_alembic_upgrade

PRE_REDACTION_REV = "b7c9d2e4f6a8"  # revision immediately before the redaction migration
REDACTION_REV = "168914d7ca05"

LEGACY_CREDENTIALED_URL = "https://buyer:s3cr3t-password@secret.example/hook?token=leaked-legacy-value"
LEGACY_ERROR_MESSAGE = f"HTTP 404: 404 Client Error: Not Found for url: {LEGACY_CREDENTIALED_URL}"
CREDENTIAL_SUBSTRINGS = ("buyer", "s3cr3t-password", "secret.example", "leaked-legacy-value")


def _seed_legacy_rows(engine, id_prefix: str) -> None:
    """Ensure the shared tenant/principal/media_buy exist (ON CONFLICT DO
    NOTHING -- safely reused across tests) and seed three webhook_delivery_log
    rows under id_prefix: one with a credential-bearing URL and error_message
    (the common case), one with a credential-bearing URL but NULL
    error_message (a logged attempt with no captured error -- NULL must stay
    NULL, not become a fake redacted string), and one whose error_message has
    no credential at all (must still be swept -- the migration can't
    distinguish "safe" legacy text from "unsafe," so it redacts
    unconditionally, matching the runtime fix's blanket design)."""
    with engine.connect() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (tenant_id, name, subdomain, created_at, updated_at) "
                "VALUES ('legacy-tenant', 'Legacy Tenant', 'legacy-test', NOW(), NOW()) "
                "ON CONFLICT (tenant_id) DO NOTHING"
            )
        )
        conn.execute(
            text(
                "INSERT INTO principals (tenant_id, principal_id, name, access_token, "
                "platform_mappings, created_at, updated_at) "
                "VALUES ('legacy-tenant', 'legacy-principal', 'Legacy Principal', 'legacy-token', "
                '\'{"mock": {"advertiser_id": "test"}}\'::jsonb, NOW(), NOW()) '
                "ON CONFLICT (tenant_id, principal_id) DO NOTHING"
            )
        )
        conn.execute(
            text(
                "INSERT INTO media_buys (media_buy_id, tenant_id, principal_id, order_name, "
                "advertiser_name, status, raw_request, start_date, end_date, budget) "
                "VALUES ('legacy-media-buy', 'legacy-tenant', 'legacy-principal', 'Legacy Order', "
                "'Legacy Advertiser', 'active', '{}'::jsonb, CURRENT_DATE, "
                "CURRENT_DATE + interval '30 days', 1000) "
                "ON CONFLICT (media_buy_id) DO NOTHING"
            )
        )
        conn.execute(
            text(
                "INSERT INTO webhook_delivery_log "
                "(id, tenant_id, principal_id, media_buy_id, webhook_url, task_type, status, error_message) "
                "VALUES "
                "(:id_with_error, 'legacy-tenant', 'legacy-principal', 'legacy-media-buy', "
                " :url, 'delivery_report', 'failed', :err), "
                "(:id_null_error, 'legacy-tenant', 'legacy-principal', 'legacy-media-buy', "
                " :url, 'delivery_report', 'success', NULL), "
                "(:id_safe_error, 'legacy-tenant', 'legacy-principal', 'legacy-media-buy', "
                " :url, 'delivery_report', 'failed', 'Connection reset by peer')"
            ),
            {
                "id_with_error": f"{id_prefix}with-error",
                "id_null_error": f"{id_prefix}null-error",
                "id_safe_error": f"{id_prefix}safe-error",
                "url": LEGACY_CREDENTIALED_URL,
                "err": LEGACY_ERROR_MESSAGE,
            },
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
    def test_upgrade_redacts_legacy_data(self, migration_db):
        """Atomic: seeds and asserts every upgrade outcome in one test, so it
        never depends on state left behind by another test method."""
        engine, db_url = migration_db
        id_prefix = "atomic-"

        run_alembic_upgrade(db_url, PRE_REDACTION_REV)  # no-op if already past this point
        _seed_legacy_rows(engine, id_prefix)

        # Confirm the legacy data really is unredacted before the migration runs.
        webhook_url, error_message = _fetch_row(engine, f"{id_prefix}with-error")
        assert webhook_url == LEGACY_CREDENTIALED_URL
        assert error_message == LEGACY_ERROR_MESSAGE

        run_alembic_upgrade(db_url, REDACTION_REV)

        # webhook_url + error_message redacted for the common case.
        webhook_url, error_message = _fetch_row(engine, f"{id_prefix}with-error")
        assert webhook_url == "<redacted-by-migration>"
        assert error_message == "<redacted-by-migration>"

        # NULL error_message preserved as NULL, not corrupted into a fake string.
        webhook_url, error_message = _fetch_row(engine, f"{id_prefix}null-error")
        assert webhook_url == "<redacted-by-migration>"
        assert error_message is None

        # Non-credentialed error_message still swept unconditionally.
        _webhook_url, error_message = _fetch_row(engine, f"{id_prefix}safe-error")
        assert error_message == "<redacted-by-migration>"

        # No credential substring survives ANYWHERE in the table -- including
        # rows any other test method in this module may have already seeded.
        with engine.connect() as conn:
            result = conn.execute(text("SELECT webhook_url, error_message FROM webhook_delivery_log"))
            rows = result.fetchall()
        assert rows, "expected seeded rows to still be present"
        for row_webhook_url, row_error_message in rows:
            for substring in CREDENTIAL_SUBSTRINGS:
                assert substring not in (row_webhook_url or ""), f"{substring!r} survived in webhook_url"
                assert substring not in (row_error_message or ""), f"{substring!r} survived in error_message"

    def test_downgrade_then_reupgrade_succeeds_and_keeps_placeholders(self, migration_db_fresh):
        """Independently initialized on its OWN fresh database (migration_db_fresh,
        function-scoped) -- never shares state with test_upgrade_redacts_legacy_data
        or with any future test added to this module, even though this one also
        mutates revision state via downgrade.

        The mandatory CI "Migration Roundtrip" job (scripts/ci/migration_roundtrip.py)
        runs upgrade(head) -> downgrade(one step back) -> upgrade(head) on
        every PR. downgrade() MUST succeed here -- not raise -- or that job
        breaks on every future PR, not just this migration's own introduction
        (confirmed: this happened for real in CI before this fix, job 90492416550).
        """
        engine, db_url = migration_db_fresh
        id_prefix = "roundtrip-"

        run_alembic_upgrade(db_url, PRE_REDACTION_REV)
        _seed_legacy_rows(engine, id_prefix)
        run_alembic_upgrade(db_url, REDACTION_REV)

        webhook_url, error_message = _fetch_row(engine, f"{id_prefix}with-error")
        assert webhook_url == "<redacted-by-migration>"
        assert error_message == "<redacted-by-migration>"

        run_alembic_downgrade(db_url, PRE_REDACTION_REV)  # must not raise

        # Downgrade is a structural no-op: the placeholders are never
        # un-redacted (there is nothing to restore them from), so they must
        # still be in place immediately after downgrading.
        webhook_url, error_message = _fetch_row(engine, f"{id_prefix}with-error")
        assert webhook_url == "<redacted-by-migration>"
        assert error_message == "<redacted-by-migration>"

        run_alembic_upgrade(db_url, REDACTION_REV)

        # And placeholders remain after re-upgrading, completing the roundtrip.
        webhook_url, error_message = _fetch_row(engine, f"{id_prefix}with-error")
        assert webhook_url == "<redacted-by-migration>"
        assert error_message == "<redacted-by-migration>"

    def test_downgrade_also_redacts_rows_inserted_after_upgrade(self, migration_db_fresh):
        """A pure no-op downgrade would leave a real gap: a row inserted (e.g.
        via raw SQL, bypassing the application) AFTER upgrade() already ran but
        BEFORE an operator downgrades would sail through unredacted, since
        nothing else would ever touch it. downgrade() re-runs the same sweep
        as upgrade() specifically to close this window -- this test seeds a
        row only after upgrade() has already completed, so it could only be
        redacted by downgrade() itself re-sweeping.

        Seeded ALONGSIDE it: a row already carrying an audit-safe value in the
        shape the fixed runtime actually produces (a keyed digest for
        webhook_url, a bounded failure classification for error_message,
        matching _redact_url_credentials()/_safe_delivery_error_message() in
        protocol_webhook_service.py). The sweep must leave THAT row untouched
        byte-for-byte: it is not raw legacy data, and overwriting it with the
        generic migration placeholder would destroy the keyed correlation and
        diagnostic detail the runtime redaction was designed to preserve --
        exactly what a naive unconditional re-sweep on every downgrade would do.
        """
        engine, db_url = migration_db_fresh
        id_prefix = "post-upgrade-"

        run_alembic_upgrade(db_url, PRE_REDACTION_REV)
        run_alembic_upgrade(db_url, REDACTION_REV)

        # Seeded AFTER upgrade() already ran and redacted whatever existed at
        # that point -- this row was never touched by upgrade().
        _seed_legacy_rows(engine, id_prefix)
        webhook_url, error_message = _fetch_row(engine, f"{id_prefix}with-error")
        assert webhook_url == LEGACY_CREDENTIALED_URL, "sanity check: row must start out unredacted"
        assert error_message == LEGACY_ERROR_MESSAGE

        # ALSO seeded after upgrade(): a row the fixed runtime would produce --
        # already audit-safe, not raw legacy data.
        safe_url = "https://<redacted:v1:3c8408c37e13c649e7279960abb2c3b5>"
        safe_error = f"HTTP 404 error delivering webhook to {safe_url}"
        with engine.connect() as conn:
            conn.execute(
                text(
                    "INSERT INTO webhook_delivery_log "
                    "(id, tenant_id, principal_id, media_buy_id, webhook_url, task_type, status, error_message) "
                    "VALUES (:id, 'legacy-tenant', 'legacy-principal', 'legacy-media-buy', "
                    " :url, 'delivery_report', 'failed', :err)"
                ),
                {"id": f"{id_prefix}already-safe", "url": safe_url, "err": safe_error},
            )
            conn.commit()

        run_alembic_downgrade(db_url, PRE_REDACTION_REV)

        webhook_url, error_message = _fetch_row(engine, f"{id_prefix}with-error")
        assert webhook_url == "<redacted-by-migration>", "downgrade must redact rows inserted after upgrade too"
        assert error_message == "<redacted-by-migration>"

        # The already-safe row must survive byte-for-byte -- not just "still
        # contain no credentials," but literally unchanged, keyed digest intact.
        webhook_url, error_message = _fetch_row(engine, f"{id_prefix}already-safe")
        assert webhook_url == safe_url, "downgrade must not destroy an already-safe keyed digest"
        assert error_message == safe_error, "downgrade must not destroy an already-safe failure classification"
