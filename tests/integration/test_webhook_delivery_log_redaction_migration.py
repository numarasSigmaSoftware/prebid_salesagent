"""Integration test for the webhook_delivery_log credential-redaction migration
(168914d7ca05) against a real PostgreSQL.

Rows written before PR #1575 held the complete, unredacted webhook URL (and
could carry the same data via unsanitized exception text in error_message).
This migration must scrub every existing row when it runs -- verifies upgrade
scrubs credential-bearing legacy data, preserves NULL error_message as NULL
(rather than corrupting "no error occurred" into a fake redacted string), and
that the CI-mandated upgrade -> downgrade -> upgrade roundtrip succeeds
without un-redacting anything.

upgrade() and downgrade() share ONE sweep (see the migration's module
docstring): webhook_url and error_message are BOTH trusted only via exact
equality to this migration's own placeholder constant -- never by shape --
on every transition. Earlier versions of this migration tried to preserve
various "safe-shaped" values instead (a keyed URL digest, a bounded error
reason), and a real credential slipped through each one -- shape proves
syntax, never provenance. Tests below cover: the basic sweep, a regression
suite pinning every one of those historical collisions (so none can silently
reappear), and a full upgrade -> fixed-runtime-write -> downgrade ->
re-upgrade lifecycle test, since no single-transition test can prove nothing
survives a later re-upgrade either.

The atomic test uses the module-scoped migration_db fixture (fine here: it
seeds, upgrades, and asserts in one method, so there's no cross-test ordering
dependency). Every other test uses its OWN function-scoped migration_db_fresh
fixture instead: a migration's data UPDATE fires exactly once, at its own
transition, so on a shared already-migrated database "upgrade to the revision
before mine" is a no-op that would let freshly-seeded rows silently skip the
redaction entirely -- a fresh database sidesteps that whole class of bug
rather than working around it with a `pass` no-op.
"""

import pytest
from sqlalchemy import text

from tests.integration.migration_helpers import run_alembic_downgrade, run_alembic_upgrade

PRE_REDACTION_REV = "b7c9d2e4f6a8"  # revision immediately before the redaction migration
REDACTION_REV = "168914d7ca05"
PLACEHOLDER = "<redacted-by-migration>"

LEGACY_CREDENTIALED_URL = "https://buyer:s3cr3t-password@secret.example/hook?token=leaked-legacy-value"
LEGACY_ERROR_MESSAGE = f"HTTP 404: 404 Client Error: Not Found for url: {LEGACY_CREDENTIALED_URL}"
CREDENTIAL_SUBSTRINGS = ("buyer", "s3cr3t-password", "secret.example", "leaked-legacy-value")

# A well-formed webhook_url in the exact shape _redact_url_credentials()
# (protocol_webhook_service.py) actually produces for a keyed digest. No
# longer "safe" in the sense of being preserved -- nothing is, any more --
# but still useful as a realistic example of genuine runtime output, for
# tests proving even well-formed values don't get special treatment.
_RUNTIME_KEYED_URL = "https://<redacted:v1:3c8408c37e13c649e7279960abb2c3b5>"


def _ensure_fk_parents(engine) -> None:
    """Ensure the shared tenant/principal/media_buy exist (ON CONFLICT DO
    NOTHING -- safely reused across tests and safely re-callable on an
    already-seeded database)."""
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
        conn.commit()


def _seed_legacy_rows(engine, id_prefix: str) -> None:
    """Ensure the FK parents exist, then seed three webhook_delivery_log rows
    under id_prefix: one with a credential-bearing URL and error_message (the
    common case), one with a credential-bearing URL but NULL error_message (a
    logged attempt with no captured error -- NULL must stay NULL, not become a
    fake redacted string), and one whose error_message has no credential at
    all (must still be swept -- everything is redacted unconditionally,
    matching the runtime fix's blanket design)."""
    _ensure_fk_parents(engine)
    with engine.connect() as conn:
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


def _insert_row(engine, log_id: str, url: str, error_message: str | None) -> None:
    """Insert a single webhook_delivery_log row with an explicit
    webhook_url/error_message pair. The shared primitive every collision/
    lifecycle case below needs -- only the values under test differ between
    call sites, so each test constructs its own scenario values and passes
    them here rather than open-coding the same INSERT."""
    with engine.connect() as conn:
        conn.execute(
            text(
                "INSERT INTO webhook_delivery_log "
                "(id, tenant_id, principal_id, media_buy_id, webhook_url, task_type, status, error_message) "
                "VALUES (:id, 'legacy-tenant', 'legacy-principal', 'legacy-media-buy', "
                " :url, 'delivery_report', 'failed', :err)"
            ),
            {"id": log_id, "url": url, "err": error_message},
        )
        conn.commit()


def _legitimate_error_messages(id_prefix: str) -> dict[str, str]:
    """The four bounded error_message shapes _safe_delivery_error_message()
    (protocol_webhook_service.py) can produce, at its three call sites -- an
    "HTTP <status> error" (including the literal "HTTP None error", when
    e.response is None), a bare exception class name, and "Unexpected error
    (<class>)". Shared by every test proving these get redacted like
    everything else, despite being genuine, legitimate runtime output --
    only id_prefix (and therefore which test is exercising them) differs
    between call sites."""
    return {
        f"{id_prefix}http-status": f"HTTP 404 error delivering webhook to {_RUNTIME_KEYED_URL}",
        f"{id_prefix}http-none": f"HTTP None error delivering webhook to {_RUNTIME_KEYED_URL}",
        f"{id_prefix}exception-class": f"ConnectionError delivering webhook to {_RUNTIME_KEYED_URL}",
        f"{id_prefix}unexpected-error": f"Unexpected error (RuntimeError) delivering webhook to {_RUNTIME_KEYED_URL}",
    }


def _historical_collision_cases(id_prefix: str) -> dict[str, tuple[str, str | None]]:
    """One case per historical bug documented in the migration's module
    docstring -- each maps a log_id to (webhook_url, error_message). Every
    one of these was, at some point, wrongly classified as "already safe"
    by an earlier, narrower version of this migration and left unredacted;
    this migration no longer tries to classify anything, so all of them
    must simply be swept like every other non-placeholder value. Shared by
    the upgrade-side and downgrade-side sweeps below so both prove the same
    historical bugs stay closed via both entrypoints."""
    return {
        # Bug 1: `LIKE '%REDACTED%'` treated any URL merely CONTAINING the
        # word REDACTED as already-safe.
        f"{id_prefix}substring-bare-redacted": (
            "https://hooks.example/REDACTED/deliver?token=still-secret",
            None,
        ),
        f"{id_prefix}substring-angle-redacted": (
            "https://hooks.example/<redacted>-not-really/deliver?token=still-secret-2",
            None,
        ),
        # Bug 2: anchoring error_message only at the trailing "$" let a
        # credential-bearing PREFIX before the safe-looking tail through.
        f"{id_prefix}error-prefix-bypass": (
            "REDACTED",
            "token=still-secret delivering webhook to REDACTED",
        ),
        # Bug 3: a plain identifier is grammatically identical to a bare
        # exception class name -- no anchored regex can tell them apart.
        f"{id_prefix}error-identifier-shaped": (
            "REDACTED",
            "tokenSecret123 delivering webhook to REDACTED",
        ),
        # Bug 4a: a real secret occupying the key-ID slot
        # ([A-Za-z0-9._-]{1,32}) of the retired "safe" URL shape. This is
        # the reviewer's exact reported example.
        f"{id_prefix}url-key-id-slot-secret": (
            "https://<redacted:sk_live_secret:0123456789abcdef0123456789abcdef>",
            None,
        ),
        # Bug 4b: a real secret occupying the digest slot ([0-9a-f]{32}) of
        # the same retired shape, with an otherwise ordinary key-ID -- an
        # MD5-shaped or hex-encoded API token/session key fits this
        # perfectly, and no regex can distinguish it from a genuine keyed
        # digest.
        f"{id_prefix}url-digest-slot-secret": (
            "https://<redacted:v1:deadbeefcafebabe0123456789abcdef>",
            None,
        ),
    }


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
        assert webhook_url == PLACEHOLDER
        assert error_message == PLACEHOLDER

        # NULL error_message preserved as NULL, not corrupted into a fake string.
        webhook_url, error_message = _fetch_row(engine, f"{id_prefix}null-error")
        assert webhook_url == PLACEHOLDER
        assert error_message is None

        # Non-credentialed error_message still swept unconditionally.
        _webhook_url, error_message = _fetch_row(engine, f"{id_prefix}safe-error")
        assert error_message == PLACEHOLDER

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

    def test_upgrade_redacts_historical_collision_values(self, migration_db_fresh):
        """Regression suite for every collision an earlier, narrower version
        of this migration was fooled by (see _historical_collision_cases()
        and the migration's module docstring for the full history of each).
        None of them get special treatment any more -- webhook_url and
        error_message are both replaced unless already EXACTLY the
        placeholder -- so this test's assertions are simple, but its VALUE
        is pinning that none of these old bugs can silently reappear if a
        future change reintroduces any shape-based trust.

        Uses migration_db_fresh (function-scoped), not the shared module-scoped
        migration_db: this migration's UPDATE fires exactly once, at its own
        upgrade() transition, so on a DB another test already pushed past
        REDACTION_REV, "upgrade to REDACTION_REV" is a no-op and these
        collision rows -- seeded after that point -- would never actually be
        swept by upgrade() at all (the exact class of bug the
        migration_db_fresh fixture exists to sidestep -- see its docstring).
        """
        engine, db_url = migration_db_fresh
        id_prefix = "upgrade-collision-"

        run_alembic_upgrade(db_url, PRE_REDACTION_REV)
        _ensure_fk_parents(engine)

        collision_cases = _historical_collision_cases(id_prefix)
        for log_id, (url, err) in collision_cases.items():
            _insert_row(engine, log_id, url, err)

        run_alembic_upgrade(db_url, REDACTION_REV)

        for log_id, (original_url, original_err) in collision_cases.items():
            webhook_url, error_message = _fetch_row(engine, log_id)
            assert webhook_url == PLACEHOLDER, f"{log_id}: collision value {original_url!r} survived redaction"
            if original_err is not None:
                assert error_message == PLACEHOLDER, (
                    f"{log_id}: collision error_message {original_err!r} survived redaction"
                )

    def test_upgrade_redacts_well_formed_runtime_shaped_values_too(self, migration_db_fresh):
        """Nothing is spared, not even values that are genuinely well-formed
        -- exactly what the fixed runtime would itself produce -- if they
        appear as LEGACY data (present before upgrade() runs). Seeds a
        webhook_url in the exact keyed-digest shape
        _redact_url_credentials() (protocol_webhook_service.py) produces,
        paired with each of the four legitimate error_message reason
        formats _safe_delivery_error_message() can produce
        (_legitimate_error_messages()), and confirms upgrade() redacts all
        of it anyway. There is no "already safe" legacy row to preserve at
        upgrade time in the first place (every row present was necessarily
        written by the OLD, unredacted code path -- see the migration's
        module docstring), so a value merely LOOKING like trusted output
        gets no special treatment.
        """
        engine, db_url = migration_db_fresh
        id_prefix = "legit-shaped-"

        run_alembic_upgrade(db_url, PRE_REDACTION_REV)
        _ensure_fk_parents(engine)

        legitimate_cases = _legitimate_error_messages(id_prefix)
        for log_id, err in legitimate_cases.items():
            _insert_row(engine, log_id, _RUNTIME_KEYED_URL, err)

        run_alembic_upgrade(db_url, REDACTION_REV)

        for log_id, original_err in legitimate_cases.items():
            webhook_url, error_message = _fetch_row(engine, log_id)
            assert webhook_url == PLACEHOLDER, f"{log_id}: well-formed webhook_url must still be redacted"
            assert error_message == PLACEHOLDER, (
                f"{log_id}: well-formed error_message must still be redacted: {original_err!r}"
            )

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
        assert webhook_url == PLACEHOLDER
        assert error_message == PLACEHOLDER

        run_alembic_downgrade(db_url, PRE_REDACTION_REV)  # must not raise

        # Downgrade is a structural no-op: the placeholders are never
        # un-redacted (there is nothing to restore them from), so they must
        # still be in place immediately after downgrading.
        webhook_url, error_message = _fetch_row(engine, f"{id_prefix}with-error")
        assert webhook_url == PLACEHOLDER
        assert error_message == PLACEHOLDER

        run_alembic_upgrade(db_url, REDACTION_REV)

        # And placeholders remain after re-upgrading, completing the roundtrip.
        webhook_url, error_message = _fetch_row(engine, f"{id_prefix}with-error")
        assert webhook_url == PLACEHOLDER
        assert error_message == PLACEHOLDER

    def test_downgrade_redacts_rows_inserted_after_upgrade(self, migration_db_fresh):
        """A pure no-op downgrade would leave a real gap: a row inserted (e.g.
        via raw SQL, bypassing the application) AFTER upgrade() already ran but
        BEFORE an operator downgrades would sail through unredacted, since
        nothing else would ever touch it. downgrade() re-sweeps the table
        specifically to close this window -- this test seeds every historical
        collision case (_historical_collision_cases()) only after upgrade()
        has already completed, so they could only be redacted by downgrade()
        itself re-sweeping. This is the SAME threat model (raw-SQL bypass)
        that makes shape-based trust unsound in the first place -- see the
        migration's module docstring -- so downgrade() closing it via
        unconditional redaction, not classification, is the point.
        """
        engine, db_url = migration_db_fresh
        id_prefix = "post-upgrade-"

        run_alembic_upgrade(db_url, PRE_REDACTION_REV)
        run_alembic_upgrade(db_url, REDACTION_REV)
        _ensure_fk_parents(engine)

        # Seeded AFTER upgrade() already ran and redacted whatever existed at
        # that point -- these rows were never touched by upgrade().
        collision_cases = _historical_collision_cases(id_prefix)
        for log_id, (url, err) in collision_cases.items():
            _insert_row(engine, log_id, url, err)
        webhook_url, _error_message = _fetch_row(engine, f"{id_prefix}substring-bare-redacted")
        assert webhook_url != PLACEHOLDER, "sanity check: row must start out unredacted"

        run_alembic_downgrade(db_url, PRE_REDACTION_REV)

        for log_id, (original_url, original_err) in collision_cases.items():
            webhook_url, error_message = _fetch_row(engine, log_id)
            assert webhook_url == PLACEHOLDER, f"{log_id}: collision value {original_url!r} survived downgrade"
            if original_err is not None:
                assert error_message == PLACEHOLDER, (
                    f"{log_id}: collision error_message {original_err!r} survived downgrade"
                )

    def test_full_lifecycle_never_preserves_anything_but_the_placeholder(self, migration_db_fresh):
        """The complete lifecycle: initial upgrade (every row guaranteed
        legacy/unsafe) -> fixed runtime writes a genuinely well-formed row
        -> downgrade -> re-upgrade. This is the exact sequence CI's
        mandatory Migration Roundtrip job (scripts/ci/migration_roundtrip.py)
        exercises on every PR (upgrade -> downgrade -> upgrade) once real
        application data exists in between -- and no single-transition test
        in this module proves a later re-upgrade doesn't destroy (or, in an
        earlier version of this migration, incorrectly PRESERVE) data a
        prior downgrade touched.

        Both webhook_url (a genuine keyed digest) and error_message (a
        genuine bounded reason) must become the placeholder starting at the
        very first downgrade and stay that way through re-upgrade -- neither
        column gets any special treatment, at any step, no matter how
        legitimate the value actually is.
        """
        engine, db_url = migration_db_fresh
        id_prefix = "full-lifecycle-"

        # Step 1: initial upgrade against legacy (guaranteed-unsafe) data.
        run_alembic_upgrade(db_url, PRE_REDACTION_REV)
        _seed_legacy_rows(engine, id_prefix)
        run_alembic_upgrade(db_url, REDACTION_REV)
        webhook_url, error_message = _fetch_row(engine, f"{id_prefix}with-error")
        assert webhook_url == PLACEHOLDER
        assert error_message == PLACEHOLDER

        # Step 2: fixed runtime writes a genuinely well-formed row (simulated
        # here via a direct insert using the exact shape
        # _redact_url_credentials() / _safe_delivery_error_message() produce).
        well_formed_error = f"HTTP 404 error delivering webhook to {_RUNTIME_KEYED_URL}"
        row_id = f"{id_prefix}fixed-runtime-write"
        _insert_row(engine, row_id, _RUNTIME_KEYED_URL, well_formed_error)

        # Step 3: downgrade. Both columns are replaced with the placeholder,
        # even though this row is genuinely well-formed runtime output.
        run_alembic_downgrade(db_url, PRE_REDACTION_REV)
        webhook_url, error_message = _fetch_row(engine, row_id)
        assert webhook_url == PLACEHOLDER, "downgrade must redact webhook_url even when it is well-formed"
        assert error_message == PLACEHOLDER, "downgrade must redact error_message even when it is well-formed"

        # Step 4: re-upgrade. Still the placeholder -- nothing to destroy
        # because nothing was ever preserved.
        run_alembic_upgrade(db_url, REDACTION_REV)
        webhook_url, error_message = _fetch_row(engine, row_id)
        assert webhook_url == PLACEHOLDER, "re-upgrade must leave webhook_url as the placeholder"
        assert error_message == PLACEHOLDER, "re-upgrade must leave error_message as the placeholder"
