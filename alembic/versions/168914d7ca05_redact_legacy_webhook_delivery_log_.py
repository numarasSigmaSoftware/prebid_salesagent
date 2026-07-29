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

downgrade() is a structural no-op, not a raise: this migration makes no schema
changes to revert, and CI's mandatory Migration Roundtrip job
(scripts/ci/migration_roundtrip.py) runs upgrade(head) -> downgrade(one step
back) -> upgrade(head) on every PR, so downgrade() failing here would break
that job on every future PR, not just this one. The placeholder values it
wrote on upgrade are intentionally left in place on downgrade -- the original
credential-bearing data was never archived and genuinely cannot be restored,
but "cannot restore the data" and "must not let the job fail" are different
concerns; only the schema-reversibility question belongs in downgrade().

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


def upgrade() -> None:
    """Overwrite every existing webhook_url and non-null error_message with a
    constant, non-correlating placeholder -- credentials in these columns
    predate the redaction fix and cannot be recovered or safely re-derived."""
    connection = op.get_bind()
    connection.execute(
        text(
            """
            UPDATE webhook_delivery_log
            SET webhook_url = :placeholder,
                error_message = CASE WHEN error_message IS NOT NULL THEN :placeholder ELSE NULL END
            """
        ),
        {"placeholder": _REDACTED_PLACEHOLDER},
    )


def downgrade() -> None:
    """No schema to revert -- the redacted placeholder values stay in place.

    The original webhook_url/error_message content they replaced contained
    buyer credentials, was never archived anywhere, and cannot be restored.
    Downgrading past this revision does not un-redact anything; it only moves
    the alembic_version bookkeeping backward, matching what CI's mandatory
    upgrade -> downgrade -> upgrade roundtrip expects to be able to do.
    """
    print("168914d7ca05 downgrade: no schema change to revert; redacted placeholder values remain in place.")
