"""Integration test for the webhook_delivery_log credential-redaction migration
(168914d7ca05) against a real PostgreSQL.

Rows written before PR #1575 held the complete, unredacted webhook URL (and
could carry the same data via unsanitized exception text in error_message).
This migration must scrub every existing row when it runs -- verifies upgrade
scrubs credential-bearing legacy data, preserves NULL error_message as NULL
(rather than corrupting "no error occurred" into a fake redacted string), and
that the CI-mandated upgrade -> downgrade -> upgrade roundtrip succeeds
without un-redacting anything.

upgrade() and downgrade() share ONE implementation (see the migration's
module docstring): both are safe to trust for webhook_url (its safe shape
cannot occur in organic data, on any transition), and neither ever trusts
error_message's shape (no format there is similarly unforgeable, so it is
always redacted, fail-closed). Tests below cover both entrypoints
independently -- proving each one delegates identically, not just one of
them -- plus a full upgrade -> fixed-runtime-write -> downgrade -> re-upgrade
lifecycle test, since no single-transition test can prove a later re-upgrade
doesn't destroy what an earlier downgrade preserved.

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

LEGACY_CREDENTIALED_URL = "https://buyer:s3cr3t-password@secret.example/hook?token=leaked-legacy-value"
LEGACY_ERROR_MESSAGE = f"HTTP 404: 404 Client Error: Not Found for url: {LEGACY_CREDENTIALED_URL}"
CREDENTIAL_SUBSTRINGS = ("buyer", "s3cr3t-password", "secret.example", "leaked-legacy-value")

# A safe-shaped webhook_url, matching what _redact_url_credentials()
# (protocol_webhook_service.py) actually produces for a keyed digest --
# shared by every test below that needs a webhook_url its scenario does NOT
# intend to exercise (only error_message is under test).
_SAFE_KEYED_URL = "https://<redacted:v1:3c8408c37e13c649e7279960abb2c3b5>"


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
    all (must still be swept -- the migration can't distinguish "safe" legacy
    text from "unsafe," so it redacts unconditionally, matching the runtime
    fix's blanket design)."""
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
    preservation case below needs -- only the values under test differ
    between call sites, so each test constructs its own scenario values and
    passes them here rather than open-coding the same INSERT."""
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


def _legitimate_shaped_cases(id_prefix: str) -> dict[str, str]:
    """The four bounded error_message shapes _safe_delivery_error_message()
    (protocol_webhook_service.py) can produce, at its three call sites -- an
    "HTTP <status> error" (including the literal "HTTP None error", when
    e.response is None), a bare exception class name, and "Unexpected error
    (<class>)". Shared by every test proving error_message is ALWAYS
    redacted regardless of how legitimate its shape looks -- only id_prefix
    (and therefore which test/transition is exercising them) differs between
    call sites."""
    return {
        f"{id_prefix}http-status": f"HTTP 404 error delivering webhook to {_SAFE_KEYED_URL}",
        f"{id_prefix}http-none": f"HTTP None error delivering webhook to {_SAFE_KEYED_URL}",
        f"{id_prefix}exception-class": f"ConnectionError delivering webhook to {_SAFE_KEYED_URL}",
        f"{id_prefix}unexpected-error": f"Unexpected error (RuntimeError) delivering webhook to {_SAFE_KEYED_URL}",
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

    def test_upgrade_redacts_raw_values_that_merely_contain_the_safe_keywords(self, migration_db_fresh):
        """A raw, still-credentialed value can legitimately CONTAIN the words
        "REDACTED" or "<redacted" as ordinary content -- e.g. a buyer's own
        path segment, or a coincidental fragment of query-string text -- without
        being an actual safe/redacted value in our shape. A substring check
        (`LIKE '%REDACTED%'`, a real defect an earlier version of this
        migration had) would misclassify these as already-safe and skip them,
        leaving their real credentials unredacted forever.

        Three further collision cases below (credential_prefix, identifier_
        shaped, numeric_status) target error_message specifically -- and pin
        that upgrade() redacts it regardless, since error_message is NEVER
        shape-checked at all (see _redact_rows() in the migration): even
        paired with a webhook_url that IS genuinely safe-shaped ('REDACTED',
        preserved -- see the assertions below), every one of these
        error_message values must still be replaced. credential_prefix and
        numeric_status would ALSO fail an anchored shape check on their own
        merits (a credential-bearing prefix; an unbounded digit run
        masquerading as an HTTP status); identifier_shaped would NOT --
        "tokenSecret123" is grammatically identical to a bare exception class
        name, and no regex can tell them apart (see the migration's module
        docstring) -- which is exactly why error_message redaction here does
        not, and cannot, depend on shape at all.

        Uses migration_db_fresh (function-scoped), not the shared module-scoped
        migration_db: this migration's UPDATE fires exactly once, at its own
        upgrade() transition, so on a DB another test already pushed past
        REDACTION_REV, "upgrade to REDACTION_REV" is a no-op and these
        collision rows -- seeded after that point -- would never actually be
        swept by upgrade() at all (the exact class of bug the
        migration_db_fresh fixture exists to sidestep -- see its docstring).
        """
        engine, db_url = migration_db_fresh
        id_prefix = "collision-"

        run_alembic_upgrade(db_url, PRE_REDACTION_REV)
        _ensure_fk_parents(engine)

        collision_cases = {
            f"{id_prefix}url-bare-redacted": (
                "https://hooks.example/REDACTED/deliver?token=still-secret",
                None,
            ),
            f"{id_prefix}url-angle-redacted": (
                "https://hooks.example/<redacted>-not-really/deliver?token=still-secret-2",
                None,
            ),
            f"{id_prefix}error-mid-string": (
                "https://example.com/hook",
                "some raw error message containing REDACTED in the middle, not at the end",
            ),
            f"{id_prefix}error-wrong-tail": (
                "https://example.com/hook",
                "delivering webhook to REDACTED but then more text follows REDACTED",
            ),
        }
        for log_id, (url, err) in collision_cases.items():
            _insert_row(engine, log_id, url, err)

        # webhook_url is a genuinely safe-SHAPED exact value in each case
        # below ('REDACTED') -- preserved by upgrade() (see the assertions
        # below), unlike collision_cases' webhook_urls above, which are NOT
        # the exact safe shape (just contain a substring of it) and so still
        # get redacted. error_message is what's under test here.
        error_shape_collision_cases = {
            # A credential-bearing PREFIX before the safe-looking tail.
            f"{id_prefix}error-credential-prefix": "token=still-secret delivering webhook to REDACTED",
            # A plain identifier -- same grammar as a bare exception class
            # name; no shape check could ever tell these apart.
            f"{id_prefix}error-identifier-shaped": "tokenSecret123 delivering webhook to REDACTED",
            # An unbounded digit run is not a real 3-digit HTTP status.
            f"{id_prefix}error-numeric-status": "HTTP 40412345678 error delivering webhook to REDACTED",
        }
        for log_id, err in error_shape_collision_cases.items():
            _insert_row(engine, log_id, "REDACTED", err)

        run_alembic_upgrade(db_url, REDACTION_REV)

        for log_id, (original_url, original_err) in collision_cases.items():
            webhook_url, error_message = _fetch_row(engine, log_id)
            assert webhook_url == "<redacted-by-migration>", (
                f"{log_id}: collision value {original_url!r} survived redaction"
            )
            if original_err is not None:
                assert error_message == "<redacted-by-migration>", (
                    f"{log_id}: collision error_message {original_err!r} survived redaction"
                )

        for log_id, original_err in error_shape_collision_cases.items():
            webhook_url, error_message = _fetch_row(engine, log_id)
            assert webhook_url == "REDACTED", f"{log_id}: a genuinely-safe webhook_url must not be touched"
            assert error_message == "<redacted-by-migration>", (
                f"{log_id}: credential-bearing error_message survived redaction: {original_err!r}"
            )

    def test_upgrade_preserves_safe_shaped_url_but_always_redacts_error_message(self, migration_db_fresh):
        """error_message is NEVER shape-checked (see _redact_rows() in the
        migration): upgrade() redacts it unconditionally, even when it is
        one of the exact bounded formats _legitimate_shaped_cases() produces
        (matching what _safe_delivery_error_message() in
        protocol_webhook_service.py legitimately emits), and even when
        paired with a webhook_url that genuinely IS safe-shaped -- and
        therefore preserved, not redacted (see the assertions below). A
        credential can be grammatically identical to a bare exception class
        name (see test_upgrade_redacts_raw_values_that_merely_contain_the_
        safe_keywords above), so error_message shape is never trusted,
        regardless of how legitimate it looks.

        webhook_url, unlike error_message, IS preserved here: it is already
        in the exact shape the fixed runtime would produce, and that shape
        cannot occur in real legacy data (see the migration's module
        docstring for why) -- so there is nothing to redact.

        (downgrade()'s side of this same invariant, via the OTHER Alembic
        entrypoint, is covered separately by
        test_downgrade_preserves_safe_shaped_url_but_always_redacts_error_message
        below -- upgrade() and downgrade() share one implementation now, so
        both tests are expected to make identical assertions; having both
        pins that structurally instead of by inspection.)
        """
        engine, db_url = migration_db_fresh
        id_prefix = "legit-reason-"

        run_alembic_upgrade(db_url, PRE_REDACTION_REV)
        _ensure_fk_parents(engine)

        legitimate_shaped_cases = _legitimate_shaped_cases(id_prefix)
        for log_id, err in legitimate_shaped_cases.items():
            _insert_row(engine, log_id, _SAFE_KEYED_URL, err)

        run_alembic_upgrade(db_url, REDACTION_REV)

        for log_id, original_err in legitimate_shaped_cases.items():
            webhook_url, error_message = _fetch_row(engine, log_id)
            assert webhook_url == _SAFE_KEYED_URL, f"{log_id}: already-safe-shaped webhook_url must not be touched"
            assert error_message == "<redacted-by-migration>", (
                f"{log_id}: error_message must always be redacted, even a legitimate-looking one: {original_err!r}"
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

        This test only ever re-upgrades rows that were ALREADY the
        placeholder by the time downgrade ran -- nothing here was preserved
        by downgrade, so there is nothing for a re-upgrade to destroy. See
        test_full_lifecycle_reupgrade_does_not_destroy_what_downgrade_preserved
        below for the sequence that DOES seed genuinely-safe data before
        downgrading and re-upgrading.
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
        nothing else would ever touch it. downgrade() re-sweeps the table
        specifically to close this window -- this test seeds rows only after
        upgrade() has already completed, so they could only be redacted by
        downgrade() itself re-sweeping.

        Seeded ALONGSIDE the redact-me rows: one row already carrying a
        genuinely safe-shaped webhook_url (a keyed digest, matching
        _redact_url_credentials() in protocol_webhook_service.py). The sweep
        must leave webhook_url untouched byte-for-byte: it is not raw legacy
        data, and overwriting it with the generic migration placeholder would
        destroy the keyed correlation the runtime redaction was designed to
        preserve -- exactly what a naive unconditional re-sweep would do.
        error_message, though, is NEVER preserved -- not even here, even
        though this row's error_message IS one of the exact bounded formats
        the fixed runtime legitimately produces: error_message has no
        equivalent unforgeable shape (see the migration's module docstring),
        so it is always redacted regardless of how safe it looks.

        Also seeded: three more error_message-only collisions (a
        credential-bearing PREFIX before the safe-looking tail, an
        identifier-shaped credential grammatically identical to a bare
        exception class name, and a numeric credential embedded in a fake
        "HTTP <it> error") -- all three must be caught on downgrade too, even
        though their own webhook_url ('REDACTED') is genuinely safe and must
        be left alone.
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

        # ALSO seeded after upgrade(): a row the fixed runtime would produce
        # -- webhook_url is already audit-safe; error_message is a
        # legitimate bounded classification, but still gets redacted
        # regardless (see docstring above).
        safe_error = f"HTTP 404 error delivering webhook to {_SAFE_KEYED_URL}"
        _insert_row(engine, f"{id_prefix}already-safe", _SAFE_KEYED_URL, safe_error)

        already_safe_url_cases = {
            f"{id_prefix}error-credential-prefix": "token=still-secret delivering webhook to REDACTED",
            f"{id_prefix}error-identifier-shaped": "tokenSecret123 delivering webhook to REDACTED",
            f"{id_prefix}error-numeric-status": "HTTP 40412345678 error delivering webhook to REDACTED",
        }
        for log_id, err in already_safe_url_cases.items():
            _insert_row(engine, log_id, "REDACTED", err)

        run_alembic_downgrade(db_url, PRE_REDACTION_REV)

        webhook_url, error_message = _fetch_row(engine, f"{id_prefix}with-error")
        assert webhook_url == "<redacted-by-migration>", "downgrade must redact rows inserted after upgrade too"
        assert error_message == "<redacted-by-migration>"

        # webhook_url survives byte-for-byte; error_message is ALWAYS
        # redacted, even for this genuinely-legitimate classification.
        webhook_url, error_message = _fetch_row(engine, f"{id_prefix}already-safe")
        assert webhook_url == _SAFE_KEYED_URL, "downgrade must not destroy an already-safe keyed digest"
        assert error_message == "<redacted-by-migration>", (
            f"error_message must always be redacted, even a legitimate one: {safe_error!r}"
        )

        for log_id, original_err in already_safe_url_cases.items():
            webhook_url, error_message = _fetch_row(engine, log_id)
            assert webhook_url == "REDACTED", f"{log_id}: a genuinely-safe webhook_url must not be touched"
            assert error_message == "<redacted-by-migration>", (
                f"{log_id}: credential-bearing error_message survived downgrade: {original_err!r}"
            )

    def test_downgrade_preserves_safe_shaped_url_but_always_redacts_error_message(self, migration_db_fresh):
        """downgrade()'s side of the SAME invariant
        test_upgrade_preserves_safe_shaped_url_but_always_redacts_error_message
        pins via upgrade() -- upgrade() and downgrade() share one
        implementation (_redact_rows()) now, so both are expected to make
        identical assertions; having both pins that structurally instead of
        by inspection.

        Seeds the four bounded *reason* formats AFTER upgrade() has already
        run, simulating the fixed application writing them for real, then
        confirms downgrade() preserves webhook_url (genuinely safe-shaped)
        while ALWAYS redacting error_message -- even though every one of
        these values is exactly what the fixed runtime legitimately
        produces. A fix that tried to special-case "legitimate" error_message
        shapes here would reopen the identifier-shaped-credential exposure
        (see test_downgrade_fails_closed_on_identifier_shaped_credential
        below): there is no shape distinguishing a real bounded
        classification from a credential that happens to look like one.
        """
        engine, db_url = migration_db_fresh
        id_prefix = "downgrade-legit-reason-"

        run_alembic_upgrade(db_url, PRE_REDACTION_REV)
        run_alembic_upgrade(db_url, REDACTION_REV)
        _ensure_fk_parents(engine)

        legitimate_shaped_cases = _legitimate_shaped_cases(id_prefix)
        for log_id, err in legitimate_shaped_cases.items():
            _insert_row(engine, log_id, _SAFE_KEYED_URL, err)

        run_alembic_downgrade(db_url, PRE_REDACTION_REV)

        for log_id, original_err in legitimate_shaped_cases.items():
            webhook_url, error_message = _fetch_row(engine, log_id)
            assert webhook_url == _SAFE_KEYED_URL, f"{log_id}: already-safe-shaped webhook_url must not be touched"
            assert error_message == "<redacted-by-migration>", (
                f"{log_id}: error_message must always be redacted, even a legitimate-looking one: {original_err!r}"
            )

    def test_downgrade_fails_closed_on_identifier_shaped_credential(self, migration_db_fresh):
        """A rollback can be the terminal state of a deployment -- there is
        no guarantee a later re-upgrade ever runs to clean anything up -- so
        downgrade() cannot afford to preserve a value it cannot prove is
        safe. An identifier-shaped credential (e.g. "tokenSecret123", an
        entirely ordinary shape for a real API token or session key) is
        grammatically identical to a genuinely-safe bare exception class
        name; an earlier version of this migration treated that ambiguity as
        an "accepted residual" and deliberately preserved it on downgrade.
        That was the wrong call for a migration whose whole purpose is
        guaranteeing no credential survives: this test replaces the old
        assertion that the credential survived with the fail-closed
        assertion that it does not -- error_message is NEVER shape-checked
        (see _redact_rows() in the migration), so this collision is caught
        the same way every other error_message value is, without needing to
        recognize it specially.
        """
        engine, db_url = migration_db_fresh
        id_prefix = "downgrade-residual-"

        run_alembic_upgrade(db_url, PRE_REDACTION_REV)
        run_alembic_upgrade(db_url, REDACTION_REV)
        _ensure_fk_parents(engine)

        identifier_shaped_error = "tokenSecret123 delivering webhook to REDACTED"
        _insert_row(engine, f"{id_prefix}identifier-shaped", "REDACTED", identifier_shaped_error)

        run_alembic_downgrade(db_url, PRE_REDACTION_REV)

        webhook_url, error_message = _fetch_row(engine, f"{id_prefix}identifier-shaped")
        assert webhook_url == "REDACTED", "a genuinely-safe webhook_url must not be touched"
        assert error_message == "<redacted-by-migration>", (
            f"identifier-shaped credential must not survive downgrade: {identifier_shaped_error!r}"
        )

    def test_full_lifecycle_reupgrade_does_not_destroy_what_downgrade_preserved(self, migration_db_fresh):
        """The complete lifecycle: initial upgrade (every row guaranteed
        unsafe legacy data) -> fixed runtime writes a genuinely-safe row ->
        downgrade -> re-upgrade. This is the exact sequence CI's mandatory
        Migration Roundtrip job (scripts/ci/migration_roundtrip.py) exercises
        on every PR (upgrade -> downgrade -> upgrade) once real application
        data exists in between -- and the sequence no prior test in this
        module covered end-to-end:
        test_downgrade_then_reupgrade_succeeds_and_keeps_placeholders only
        ever re-upgrades rows that were ALREADY the placeholder (nothing to
        preserve, nothing to destroy), and
        test_downgrade_preserves_safe_shaped_url_but_always_redacts_error_message
        never re-upgrades at all. An earlier version of this migration made
        upgrade() unconditional on every call (not just the first), which
        passed both of those tests individually while still destroying a
        webhook_url downgrade had JUST preserved, the moment a re-upgrade
        followed -- this test is what would have caught that.

        webhook_url must survive ALL FOUR steps unchanged: its safe shape
        (see the migration's module docstring for why it is unforgeable by
        organic or raw-SQL-bypass data alike) is trusted on every transition,
        not just downgrade -- so a re-upgrade preserves it exactly like
        downgrade did.

        error_message must become the placeholder starting at the very first
        downgrade (it is never preserved, even though this specific value is
        one of the exact legitimate formats the fixed runtime produces -- see
        test_downgrade_preserves_safe_shaped_url_but_always_redacts_error_message)
        and stay that way through re-upgrade.
        """
        engine, db_url = migration_db_fresh
        id_prefix = "full-lifecycle-"

        # Step 1: initial upgrade against legacy (guaranteed-unsafe) data.
        run_alembic_upgrade(db_url, PRE_REDACTION_REV)
        _seed_legacy_rows(engine, id_prefix)
        run_alembic_upgrade(db_url, REDACTION_REV)
        webhook_url, error_message = _fetch_row(engine, f"{id_prefix}with-error")
        assert webhook_url == "<redacted-by-migration>"
        assert error_message == "<redacted-by-migration>"

        # Step 2: fixed runtime writes a genuinely-safe row (simulated here
        # via a direct insert using the exact shape _redact_url_credentials()
        # / _safe_delivery_error_message() produce).
        safe_error = f"HTTP 404 error delivering webhook to {_SAFE_KEYED_URL}"
        row_id = f"{id_prefix}fixed-runtime-write"
        _insert_row(engine, row_id, _SAFE_KEYED_URL, safe_error)

        # Step 3: downgrade. webhook_url survives; error_message does NOT --
        # fail-closed, even though this specific value is genuinely safe.
        run_alembic_downgrade(db_url, PRE_REDACTION_REV)
        webhook_url, error_message = _fetch_row(engine, row_id)
        assert webhook_url == _SAFE_KEYED_URL, "downgrade must preserve the safe-shaped webhook_url"
        assert error_message == "<redacted-by-migration>", (
            "downgrade must always redact error_message, even a safe-looking one"
        )

        # Step 4: re-upgrade. webhook_url must STILL survive -- this is the
        # exact regression this test exists to catch: an earlier version of
        # this migration made upgrade() unconditional on every call, which
        # destroyed exactly this row on exactly this step.
        run_alembic_upgrade(db_url, REDACTION_REV)
        webhook_url, error_message = _fetch_row(engine, row_id)
        assert webhook_url == _SAFE_KEYED_URL, "re-upgrade must not destroy a webhook_url downgrade already preserved"
        assert error_message == "<redacted-by-migration>", "error_message must remain redacted after re-upgrade"
