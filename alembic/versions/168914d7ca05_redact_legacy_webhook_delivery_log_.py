"""redact legacy webhook delivery log credentials

Historical rows in ``webhook_delivery_log`` were written before the
credential-redaction work in PR #1575: ``webhook_url`` held the complete,
unredacted outbound URL (userinfo, capability-host, path, query, and fragment
credentials all survived verbatim), and ``error_message`` could carry the same
data via an unsanitized ``str(exception)`` -- ``requests.HTTPError`` embeds the
full request URL in its string form.

This migration runs as part of the SAME deployment as the redaction fix, before
the new application code starts serving traffic, so every row that exists when
it executes was necessarily written by the OLD, unredacted code path -- there is
no ambiguity to resolve between "legacy" and "already safe" rows.

A keyed digest (matching what the new code computes at runtime) is deliberately
NOT used here: that would require importing application config/service code
into a migration, coupling a migration that must stay replayable forever to
code that will keep changing. A constant, non-correlating placeholder is safer
and simpler -- we cannot recover any operationally useful information from
these rows regardless, so there is nothing lost by not attempting a "real"
digest.

downgrade() does not raise: this migration makes no schema changes to revert,
and CI's mandatory Migration Roundtrip job (scripts/ci/migration_roundtrip.py)
runs upgrade(head) -> downgrade(one step back) -> upgrade(head) on every PR,
so downgrade() failing here would break that job on every future PR, not just
this one.

downgrade() also does not merely no-op: it re-runs the same sweep as
upgrade(), so a row inserted (e.g. via raw SQL, bypassing the application
entirely) after upgrade() ran but before an operator downgrades still gets
redacted -- a print-only downgrade would leave that window open.

The sweep is NOT unconditional, though: it only touches rows whose
webhook_url or error_message does not already look like an audit-safe value
(the runtime's ``<redacted...>``/``REDACTED`` shapes -- see
_redact_url_credentials() in protocol_webhook_service.py -- or this
migration's own placeholder, which itself matches that shape). An earlier
version of this migration swept every row unconditionally on every
upgrade/downgrade/re-upgrade, which would blow away the KEYED, correlatable
digest and bounded failure classification the runtime redaction produces for
rows written normally after this migration first ran -- replacing genuinely
useful audit data with the generic non-correlating placeholder for no reason.
It also is not a "no-op" in the way an earlier version of this docstring
claimed: PostgreSQL creates a new row version (and writes WAL, and takes a row
lock) for any UPDATE that touches a row, regardless of whether the written
value differs from the old one -- so re-writing every row on every downgrade
was real, unnecessary churn, not a free operation. Scoping the WHERE clause to
rows that actually need it avoids both problems.

Revision ID: 168914d7ca05
Revises: b7c9d2e4f6a8
Create Date: 2026-07-29 02:26:40.333322

"""

from collections.abc import Sequence

from sqlalchemy import text

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "168914d7ca05"
down_revision: str | Sequence[str] | None = "b7c9d2e4f6a8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_REDACTED_PLACEHOLDER = "<redacted-by-migration>"


def _already_safe(column: str) -> str:
    """SQL boolean expression: does *column* already look like an audit-safe
    value, rather than a raw legacy one that still needs redacting?

    Every shape _redact_url_credentials() (protocol_webhook_service.py) can
    produce -- f"{scheme}://<redacted:{key_id}:{digest}>",
    f"{scheme}://<redacted>", or the bare "REDACTED" fallback -- contains one
    of these two substrings. _safe_delivery_error_message() always embeds
    that same output, so this one pair of patterns recognizes an already-safe
    value in EITHER column. This migration's own placeholder also matches (it
    contains "<redacted"), so an already-migration-redacted row is correctly
    recognized as needing no further work too. A real webhook URL cannot
    contain a literal, un-percent-encoded "<" -- it isn't a valid URI
    character -- so this can't collide with genuine buyer-supplied content.
    """
    return f"({column} LIKE '%<redacted%' OR {column} LIKE '%REDACTED%')"


def _redact_all_rows() -> None:
    """Overwrite webhook_url and non-null error_message with a constant,
    non-correlating placeholder -- but ONLY for rows that don't already look
    like an audit-safe value (see _already_safe() above).

    Shared by upgrade() and downgrade(): both revision states want the SAME
    invariant held (no raw credentials in these two columns), so both sweep
    the table the same way. Scoping to genuinely-unsafe rows matters for two
    reasons: (1) a row written normally by the fixed runtime after this
    migration first ran already carries a keyed, correlatable digest and a
    bounded failure classification -- overwriting it with the generic
    placeholder would destroy real audit value for no reason; (2) PostgreSQL
    creates a new row version (WAL, row lock) for any UPDATE that touches a
    row regardless of whether the written value differs, so re-writing rows
    that don't need it is real, avoidable churn -- not the "no-op" an earlier
    version of this migration claimed.
    """
    url_is_safe = _already_safe("webhook_url")
    error_is_safe = _already_safe("error_message")
    connection = op.get_bind()
    connection.execute(
        text(
            f"""
            UPDATE webhook_delivery_log
            SET webhook_url = CASE WHEN {url_is_safe} THEN webhook_url ELSE :placeholder END,
                error_message = CASE
                    WHEN error_message IS NULL THEN NULL
                    WHEN {error_is_safe} THEN error_message
                    ELSE :placeholder
                END
            WHERE NOT {url_is_safe}
               OR (error_message IS NOT NULL AND NOT {error_is_safe})
            """
        ),
        {"placeholder": _REDACTED_PLACEHOLDER},
    )


def upgrade() -> None:
    """Redact every not-already-safe row present at upgrade time. At the
    point this migration first runs, every row was written by the old code
    path -- there is no ambiguity to resolve between "legacy" and "already
    safe" rows here, but _redact_all_rows() is still safety-scoped identically
    to downgrade() so both share one code path."""
    _redact_all_rows()


def downgrade() -> None:
    """No schema to revert, but re-sweep anyway rather than no-op.

    The original webhook_url/error_message content upgrade() replaced
    contained buyer credentials, was never archived anywhere, and cannot be
    restored -- downgrading past this revision does not (and cannot) un-redact
    anything. But a pure no-op here would leave a real gap: a row inserted
    (e.g. via raw SQL, bypassing the application) after upgrade() ran but
    before an operator downgrades would sail through unredacted, since
    nothing else would ever touch it. Re-running the same sweep closes that
    gap and still satisfies CI's mandatory upgrade -> downgrade -> upgrade
    roundtrip (scripts/ci/migration_roundtrip.py), which requires this
    function not to raise.
    """
    _redact_all_rows()
