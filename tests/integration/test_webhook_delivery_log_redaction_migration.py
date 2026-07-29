"""Integration test for the webhook_delivery_log credential-redaction migration
(168914d7ca05) against a real PostgreSQL.

Rows written before PR #1575 held the complete, unredacted webhook URL (and
could carry the same data via unsanitized exception text in error_message).
This migration must scrub every existing row when it runs -- verifies upgrade
scrubs credential-bearing legacy data, preserves NULL error_message as NULL
(rather than corrupting "no error occurred" into a fake redacted string), and
that the CI-mandated upgrade -> downgrade -> upgrade roundtrip succeeds
without un-redacting anything.

upgrade() and downgrade() are NOT the same operation (see the migration's
module docstring): upgrade() redacts unconditionally, since every row at
upgrade time is guaranteed legacy/unsafe, while downgrade() uses a
shape-based safety check, since it can genuinely encounter rows the fixed
runtime already wrote safely. Tests below are grouped accordingly --
upgrade-focused tests first, then downgrade-focused tests.

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

        Three further collision cases below (credential_prefix_*,
        identifier_shaped_*, numeric_status_*) cover DIFFERENT failure modes
        from the substring collisions above -- not a substring anywhere in
        the value, but the value's OWN shape colliding with what an
        anchored-both-ends predicate accepts as safe:

        - credential_prefix: a credential-bearing PREFIX before the safe
          marker (only an end-anchor, no leading "^", would miss this).
        - identifier_shaped: a plain-identifier credential (e.g.
          "tokenSecret123", an ordinary shape for a real API token) is
          grammatically indistinguishable from a bare exception class name
          like "ConnectionError" -- anchoring both ends does NOT resolve
          this, since the credential matches _EXCEPTION_CLASS_NAME exactly.
        - numeric_status: an unbounded digit run embedded in "HTTP <it>
          error" is grammatically indistinguishable from a real 3-digit HTTP
          status once _HTTP_STATUS is left as `[0-9]+` instead of exactly 3
          digits.

        No fully-anchored REGEX can resolve identifier_shaped -- a real
        exception class name and a credential that happens to look like one
        are the same grammar (see the _EXCEPTION_CLASS_NAME comment in the
        migration). This is exactly why upgrade() no longer consults shape
        at all (_redact_unconditionally()): these three cases pin THAT
        structural fix. upgrade() must still catch and redact every one of
        them -- not because a smarter regex classifies them correctly, but
        because upgrade() no longer asks the question in the first place.

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
        # below ('REDACTED') -- the collision is entirely in error_message.
        # Kept as a separate dict from collision_cases above purely for
        # documentation (each key names a distinct collision class); the
        # post-upgrade expectation is the SAME as collision_cases, though:
        # upgrade() no longer checks shape at all, so even a webhook_url
        # that already reads 'REDACTED' gets overwritten too. There is no
        # real legacy scenario where a pre-fix webhook_url would already
        # equal that exact string -- the old code never wrote it -- so
        # nothing meaningful is lost by upgrade() not special-casing it.
        error_shape_collision_cases = {
            # A fix that only anchors the trailing "$" (not a leading "^"
            # too) lets "<anything> delivering webhook to REDACTED" through,
            # since PostgreSQL's `~` searches anywhere in the string unless
            # the pattern itself starts with `^`.
            f"{id_prefix}error-credential-prefix": "token=still-secret delivering webhook to REDACTED",
            # A plain identifier -- matches _EXCEPTION_CLASS_NAME exactly,
            # same grammar as a bare exception class name.
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
            assert webhook_url == "<redacted-by-migration>", (
                f"{log_id}: upgrade() must redact webhook_url unconditionally, even a safe-shaped one"
            )
            assert error_message == "<redacted-by-migration>", (
                f"{log_id}: credential-bearing error_message survived redaction: {original_err!r}"
            )

    def test_upgrade_redacts_even_safe_shaped_legacy_data(self, migration_db_fresh):
        """upgrade() no longer checks shape at all (_redact_unconditionally()):
        every row present when it runs was necessarily written by the OLD,
        unredacted code path (see the migration's module docstring), so
        there is no "already safe" legacy row to preserve, and a shape-based
        check applied at upgrade() time can only produce a false negative.

        Seeds rows using the exact three bounded *reason* formats
        _safe_delivery_error_message() (protocol_webhook_service.py) can
        produce -- an "HTTP <status> error" (including the literal "HTTP
        None error"), a bare exception class name, and "Unexpected error
        (<class>)" -- paired with a genuinely-safe-shaped webhook_url, and
        confirms upgrade() redacts all of them anyway. An earlier version of
        this migration applied its shape check to upgrade() too, and would
        have left every one of these rows completely unredacted --
        including the bare-exception-class-name row, which is grammatically
        identical to a raw, credential-bearing legacy value like
        "tokenSecret123 delivering webhook to REDACTED" (see
        test_upgrade_redacts_raw_values_that_merely_contain_the_safe_keywords
        above).

        (downgrade()'s COMPLEMENTARY obligation -- that it must NOT redact
        these same three formats when they are written by the fixed runtime
        AFTER upgrade() already ran -- is covered separately by
        test_downgrade_preserves_all_three_legitimate_reason_formats below.)
        """
        engine, db_url = migration_db_fresh
        id_prefix = "legit-reason-"

        run_alembic_upgrade(db_url, PRE_REDACTION_REV)
        _ensure_fk_parents(engine)

        legitimate_shaped_cases = {
            f"{id_prefix}http-status": f"HTTP 404 error delivering webhook to {_SAFE_KEYED_URL}",
            f"{id_prefix}http-none": f"HTTP None error delivering webhook to {_SAFE_KEYED_URL}",
            f"{id_prefix}exception-class": f"ConnectionError delivering webhook to {_SAFE_KEYED_URL}",
            f"{id_prefix}unexpected-error": f"Unexpected error (RuntimeError) delivering webhook to {_SAFE_KEYED_URL}",
        }
        for log_id, err in legitimate_shaped_cases.items():
            _insert_row(engine, log_id, _SAFE_KEYED_URL, err)

        run_alembic_upgrade(db_url, REDACTION_REV)

        for log_id, original_err in legitimate_shaped_cases.items():
            webhook_url, error_message = _fetch_row(engine, log_id)
            assert webhook_url == "<redacted-by-migration>", (
                f"{log_id}: safe-SHAPED legacy webhook_url must still be redacted at upgrade time"
            )
            assert error_message == "<redacted-by-migration>", (
                f"{log_id}: safe-SHAPED legacy error_message must still be redacted at upgrade time: {original_err!r}"
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

        Seeded ALONGSIDE the redact-me rows: one row already carrying an
        audit-safe value in the shape the fixed runtime actually produces (a
        keyed digest for webhook_url, a bounded failure classification for
        error_message, matching _redact_url_credentials()/
        _safe_delivery_error_message() in protocol_webhook_service.py). The
        sweep must leave THAT row untouched byte-for-byte: it is not raw
        legacy data, and overwriting it with the generic migration
        placeholder would destroy the keyed correlation and diagnostic detail
        the runtime redaction was designed to preserve -- exactly what a
        naive unconditional re-sweep on every downgrade would do.

        Also seeded: a credential-bearing PREFIX before the safe-looking tail
        (an end-anchor-only predicate would miss this), and a numeric
        credential exploiting an unbounded-digit-run _HTTP_STATUS (an
        unbounded `[0-9]+` would misclassify this as a real HTTP status) --
        both must still be caught on downgrade too, even though their own
        webhook_url ('REDACTED') is genuinely safe and must be left alone.
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
        safe_error = f"HTTP 404 error delivering webhook to {_SAFE_KEYED_URL}"
        _insert_row(engine, f"{id_prefix}already-safe", _SAFE_KEYED_URL, safe_error)

        already_safe_url_cases = {
            f"{id_prefix}error-credential-prefix": "token=still-secret delivering webhook to REDACTED",
            f"{id_prefix}error-numeric-status": "HTTP 40412345678 error delivering webhook to REDACTED",
        }
        for log_id, err in already_safe_url_cases.items():
            _insert_row(engine, log_id, "REDACTED", err)

        run_alembic_downgrade(db_url, PRE_REDACTION_REV)

        webhook_url, error_message = _fetch_row(engine, f"{id_prefix}with-error")
        assert webhook_url == "<redacted-by-migration>", "downgrade must redact rows inserted after upgrade too"
        assert error_message == "<redacted-by-migration>"

        # The already-safe row must survive byte-for-byte -- not just "still
        # contain no credentials," but literally unchanged, keyed digest intact.
        webhook_url, error_message = _fetch_row(engine, f"{id_prefix}already-safe")
        assert webhook_url == _SAFE_KEYED_URL, "downgrade must not destroy an already-safe keyed digest"
        assert error_message == safe_error, "downgrade must not destroy an already-safe failure classification"

        for log_id, original_err in already_safe_url_cases.items():
            webhook_url, error_message = _fetch_row(engine, log_id)
            assert webhook_url == "REDACTED", f"{log_id}: a genuinely-safe webhook_url must not be touched"
            assert error_message == "<redacted-by-migration>", (
                f"{log_id}: credential-bearing error_message survived downgrade: {original_err!r}"
            )

    def test_downgrade_preserves_all_three_legitimate_reason_formats(self, migration_db_fresh):
        """downgrade() keeps the shape-based check upgrade() no longer uses
        (_redact_if_unsafe()), because -- unlike upgrade() -- it can
        genuinely encounter rows the FIXED runtime already wrote safely.

        Seeds all three bounded *reason* formats _safe_delivery_error_message()
        (protocol_webhook_service.py) can produce -- an "HTTP <status> error"
        (including the literal "HTTP None error"), a bare exception class
        name, and "Unexpected error (<class>)" -- AFTER upgrade() has already
        run, simulating the fixed application writing them for real, then
        confirms downgrade() leaves every one of them untouched byte-for-byte.
        A fix that over-tightens the anchored pattern (e.g. forgetting the
        "None" status alternative, or misspelling the "Unexpected error (...)"
        shape) would start needlessly destroying real, legitimate production
        audit data -- exactly the over-broad-redaction defect an earlier
        round of this migration already fixed once.

        (upgrade()'s COMPLEMENTARY obligation -- that it must redact these
        same three formats when they appear as LEGACY data, before upgrade()
        has run -- is covered separately by
        test_upgrade_redacts_even_safe_shaped_legacy_data above.)
        """
        engine, db_url = migration_db_fresh
        id_prefix = "downgrade-legit-reason-"

        run_alembic_upgrade(db_url, PRE_REDACTION_REV)
        run_alembic_upgrade(db_url, REDACTION_REV)
        _ensure_fk_parents(engine)

        legitimate_shaped_cases = {
            f"{id_prefix}http-status": f"HTTP 404 error delivering webhook to {_SAFE_KEYED_URL}",
            f"{id_prefix}http-none": f"HTTP None error delivering webhook to {_SAFE_KEYED_URL}",
            f"{id_prefix}exception-class": f"ConnectionError delivering webhook to {_SAFE_KEYED_URL}",
            f"{id_prefix}unexpected-error": f"Unexpected error (RuntimeError) delivering webhook to {_SAFE_KEYED_URL}",
        }
        for log_id, err in legitimate_shaped_cases.items():
            _insert_row(engine, log_id, _SAFE_KEYED_URL, err)

        run_alembic_downgrade(db_url, PRE_REDACTION_REV)

        for log_id, original_err in legitimate_shaped_cases.items():
            webhook_url, error_message = _fetch_row(engine, log_id)
            assert webhook_url == _SAFE_KEYED_URL, f"{log_id}: already-safe webhook_url must not be touched"
            assert error_message == original_err, (
                f"{log_id}: legitimate reason format wrongly redacted by downgrade: "
                f"{original_err!r} -> {error_message!r}"
            )

    def test_downgrade_accepts_identifier_shaped_credential_as_safe_by_design(self, migration_db_fresh):
        """Pins the ACCEPTED residual documented on _redact_if_unsafe() and
        the _EXCEPTION_CLASS_NAME comment in the migration: unlike upgrade()
        (test_upgrade_redacts_raw_values_that_merely_contain_the_safe_keywords
        above), downgrade() still cannot distinguish an identifier-shaped
        credential (e.g. "tokenSecret123", an ordinary shape for a real API
        token) from a genuinely-safe bare exception class name -- both match
        the same grammar. Closing this for downgrade() too would require an
        exhaustive allowlist of real exception class names sourced from
        application code, which the migration's module docstring already
        rejects for the digest computation, for the same app/migration-
        coupling reason.

        This is a DELIBERATE, narrow, documented trade-off, not a silent gap:
        it is only reachable via a raw-SQL bypass of the application -- the
        same threat model downgrade()'s re-sweep exists for in the first
        place -- combined with a value that happens to match this one narrow
        grammar. This test exists so a future change to _EXCEPTION_CLASS_NAME
        or _redact_if_unsafe() that flips this behavior is a deliberate,
        reviewed decision -- update this test (and the referenced docs)
        alongside it, don't just delete the assertion because it started
        failing.
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
        assert webhook_url == "REDACTED"
        assert error_message == identifier_shaped_error, (
            "accepted residual regressed: downgrade() now redacts an identifier-shaped reason -- "
            "update this test (and the _EXCEPTION_CLASS_NAME / _redact_if_unsafe() docs) to reflect "
            "the new, narrower behavior instead of just deleting the assertion"
        )
