"""redact legacy webhook delivery log credentials

Historical rows in ``webhook_delivery_log`` were written before the
credential-redaction work in PR #1575: ``webhook_url`` held the complete,
unredacted outbound URL (userinfo, capability-host, path, query, and fragment
credentials all survived verbatim), and ``error_message`` could carry the same
data via an unsanitized ``str(exception)`` -- ``requests.HTTPError`` embeds the
full request URL in its string form.

This migration runs as part of the SAME deployment as the redaction fix, before
the new application code starts serving traffic, so every row that exists when
it FIRST executes was necessarily written by the OLD, unredacted code path.
That guarantee, though, only holds for the FIRST upgrade -- not for any later
call. Alembic offers no way to distinguish "first upgrade ever" from
"re-upgrade after a downgrade" (CI's mandatory roundtrip job forces exactly
that second case on every PR: upgrade -> downgrade -> upgrade), and by the
time of a re-upgrade, the FIXED runtime may have already written genuinely
safe rows that a prior downgrade correctly preserved. Treating "every row is
unsafe" as universally true -- an earlier version of this migration's design
-- silently destroyed that preserved data on re-upgrade. See the two bullet
points below for how upgrade() and downgrade() now handle this without
needing new schema, a marker column, or any application-code coupling.

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

downgrade() also does not merely no-op: it sweeps the table for rows that
don't already look safe, so a row inserted (e.g. via raw SQL, bypassing the
application entirely) after upgrade() ran but before an operator downgrades
still gets redacted -- a print-only downgrade would leave that window open.

upgrade() and downgrade() share ONE sweep (_redact_rows()) again -- an
intermediate version of this migration split them into an unconditional
upgrade() and a shape-scoped downgrade(), reasoning that "every row at
upgrade time is unsafe" meant upgrade() should never trust shape. That
reasoning is only true for the FIRST upgrade (see above): a later re-upgrade
CAN follow a downgrade that correctly preserved a genuinely-safe row, and an
unconditional upgrade() destroyed it -- the exact bug the shape-scoped
downgrade() was introduced to prevent, just reintroduced on the other
transition. The two columns don't get the same treatment, though, because
they don't carry the same risk:

- webhook_url IS shape-checked, on EVERY transition (first upgrade, downgrade,
  re-upgrade -- no distinction). Its safe shapes (see _REDACT_URL_SHAPE below)
  all either equal the literal string "REDACTED" outright, or embed a literal
  ``<`` / ``>``. RFC 3986 excludes both characters from every URI production
  unencoded -- a real webhook URL a buyer configured can only contain them
  percent-encoded (``%3C`` / ``%3E``), never literal. So a row whose
  webhook_url exactly matches this shape cannot be organic legacy or
  application-written data at all; the only way it gets there is this
  migration's own placeholder, the fixed runtime's own redaction output, or a
  raw-SQL bypass that deliberately mimics either -- and in that last case the
  value still carries no real credential, so there is nothing to lose by
  trusting it. That makes the shape check safe to rely on unconditionally,
  including on the very first upgrade.

- error_message is NEVER shape-checked, on ANY transition. It has no
  equivalent unforgeable marker: every "safe" reason format
  _safe_delivery_error_message() (protocol_webhook_service.py) can produce is
  built from ordinary identifier or digit grammar (an HTTP status, a bare
  exception class name -- see this migration's three-attempt history below),
  and an ordinary-looking real credential (e.g. an alphanumeric API token)
  can land in exactly that grammar by coincidence -- not hypothetically:
  "tokenSecret123 delivering webhook to REDACTED" matched every anchored
  pattern this migration tried. Given that, downgrade()
  preserving ANY non-placeholder error_message value is a standing exposure
  that a later re-upgrade is not guaranteed to ever clean up -- a rollback can
  be the terminal state of a deployment, with no future re-upgrade coming.
  The fail-closed choice: error_message is ALWAYS replaced with the constant
  placeholder, on every transition, unless it is already EXACTLY that
  placeholder. This does throw away the bounded failure classification
  ("HTTP 404 error", a bare exception class, etc.) the fixed runtime writes
  for genuinely safe rows too -- but webhook_url's keyed digest alone still
  provides the failure-correlation value the runtime redaction exists for,
  and giving up a classification LABEL is a far smaller cost than a provable
  credential-survival path in a migration whose entire purpose is closing
  exactly that.

This migration's classification logic has changed shape three times before
landing here, each time because a real credential still slipped through:
(1) a bare `LIKE '%REDACTED%'` substring check misclassified any raw,
still-credentialed value that merely happened to CONTAIN the word REDACTED
somewhere in its own path or query as already-safe; (2) anchoring
error_message only at the trailing end (``... delivering webhook to
<shape>$``, no leading ``^``) let a credential-bearing PREFIX before that
marker -- e.g. "token=still-secret delivering webhook to REDACTED" -- still
pass, since PostgreSQL's ``~`` searches anywhere in the string unless the
pattern itself starts with ``^``; (3) fully anchoring error_message at both
ends closed the prefix bypass but still trusted its reason-prefix grammar,
which is exactly what the identifier-shaped-credential collision above
exploited, and applying that same fully-anchored check unconditionally to
upgrade() ALSO destroyed genuinely-safe data on re-upgrade, as covered above.
Removing error_message from the trust boundary entirely -- rather than
attempting a fourth, tighter pattern -- is what finally closes the class of
bug instead of relocating it.

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

# Building blocks anchored to the EXACT shapes _redact_url_credentials()
# (protocol_webhook_service.py) can produce -- never a bare substring check.
# A loose `LIKE '%REDACTED%'` (a real, fixed defect in an earlier version of
# this migration) would misclassify a raw, still-credentialed URL that merely
# happens to contain the word REDACTED somewhere in its OWN path/query --
# e.g. https://hooks.example/REDACTED/deliver?token=still-secret -- as
# already-safe, leaving its real credential unredacted forever. Anchoring to
# the exact shape closes that: the pattern below requires the WHOLE column
# value to match one of the three literal forms _redact_url_credentials()
# can emit -- not just contain a keyword anywhere.
#   scheme: RFC 3986 ALPHA *(ALPHA / DIGIT / "+" / "-" / ".")
#   key_id: constrained by AppConfig.webhook_audit_hmac_key_id's validator to [A-Za-z0-9._-]{1,32}
#   digest: hashlib .hexdigest()[:32] -> exactly 32 lowercase hex characters
# Trusted on EVERY transition (first upgrade, downgrade, re-upgrade) -- see
# the module docstring's "webhook_url IS shape-checked" paragraph for why
# this specific shape (unlike error_message's, see _redact_rows() below)
# cannot collide with organic data: every alternative either equals the bare
# literal "REDACTED", or embeds a literal `<`/`>`, which RFC 3986 excludes
# from valid URI syntax unencoded.
_SCHEME = r"[a-zA-Z][a-zA-Z0-9+.-]*"
_KEY_ID = r"[A-Za-z0-9._-]{1,32}"
_DIGEST = r"[0-9a-f]{32}"
_REDACT_URL_SHAPE = rf"(?:REDACTED|{_SCHEME}://<redacted>|{_SCHEME}://<redacted:{_KEY_ID}:{_DIGEST}>)"
_URL_SAFE_PATTERN = f"^{_REDACT_URL_SHAPE}$"

# error_message has NO equivalent pattern, deliberately -- see _redact_rows()
# below and the module docstring's "error_message is NEVER shape-checked"
# paragraph. This migration went through three anchoring attempts trying to
# recognize a "safe" error_message shape (substring match, then end-anchor
# only, then fully anchored at both ends including a bounded *reason*
# grammar) -- each one still let a real credential through, because every
# "safe" reason format _safe_delivery_error_message() (protocol_webhook_
# service.py) can produce is built from ordinary identifier or digit
# grammar, which an ordinary-looking real credential can match by
# coincidence: "tokenSecret123 delivering webhook to REDACTED" matched the
# final, fully-anchored version of this pattern. Unlike _REDACT_URL_SHAPE
# above, there is no unforgeable marker available here -- so this migration
# does not try to distinguish a "safe" error_message from an unsafe one at
# all; it always replaces error_message with the placeholder.


def _redact_rows() -> None:
    """Overwrite webhook_url and error_message so no row can carry a real
    credential, on EVERY transition (first upgrade, downgrade, re-upgrade --
    shared by upgrade() and downgrade() below, no distinction). The two
    columns are NOT treated the same way; see the module docstring's two
    bullet points for the full reasoning, summarized here:

    - webhook_url is shape-checked (_URL_SAFE_PATTERN, a full regex match
      via a BOUND parameter -- not interpolated into the SQL text -- so a
      literal ``:`` inside the pattern, e.g. from the non-capturing group
      ``(?:...)``, can never be misread as a bind-parameter placeholder):
      preserved if it already exactly matches one of the runtime's
      ``<redacted...>``/``REDACTED`` forms (_redact_url_credentials() in
      protocol_webhook_service.py) or this migration's own placeholder;
      replaced otherwise. Safe to trust unconditionally -- including on the
      very first upgrade, when every row is nominally "unsafe legacy data"
      -- because that shape cannot occur in a real, organic webhook URL at
      all (see the module docstring).

    - error_message is NEVER shape-checked. It is replaced with the
      placeholder whenever it is non-null and not already EXACTLY that
      placeholder -- a plain equality comparison against a constant this
      migration itself controls, never a heuristic about what unknown
      content might mean. There is no format here that is similarly
      impossible to occur organically (see the module docstring's
      "error_message is NEVER shape-checked" paragraph and this migration's
      three-attempt history), so nothing is trusted, full stop.

    A pure no-op on downgrade would leave a real gap: a row inserted (e.g.
    via raw SQL, bypassing the application) after upgrade() ran but before
    an operator downgrades would sail through unredacted, since nothing else
    would ever touch it -- re-running this same sweep on downgrade closes
    that gap. PostgreSQL creates a new row version (WAL, row lock) for any
    UPDATE that touches a row regardless of whether the written value
    differs, so both the webhook_url shape check and the error_message
    exact-placeholder check double as an avoidable-churn guard, not just a
    correctness one: a row already fully redacted is skipped entirely.
    """
    connection = op.get_bind()
    connection.execute(
        text(
            """
            UPDATE webhook_delivery_log
            SET webhook_url = CASE
                    WHEN webhook_url = :placeholder OR webhook_url ~ :url_pattern THEN webhook_url
                    ELSE :placeholder
                END,
                error_message = CASE
                    WHEN error_message IS NULL THEN NULL
                    WHEN error_message = :placeholder THEN error_message
                    ELSE :placeholder
                END
            WHERE NOT (webhook_url = :placeholder OR webhook_url ~ :url_pattern)
               OR (error_message IS NOT NULL AND error_message != :placeholder)
            """
        ),
        {
            "placeholder": _REDACTED_PLACEHOLDER,
            "url_pattern": _URL_SAFE_PATTERN,
        },
    )


def upgrade() -> None:
    """Redact every row present at upgrade time -- see _redact_rows() for
    the per-column rules and the module docstring for why upgrade() and
    downgrade() share this one sweep rather than having different
    semantics."""
    _redact_rows()


def downgrade() -> None:
    """No schema to revert, but re-sweep anyway rather than no-op.

    The original webhook_url/error_message content upgrade() replaced
    contained buyer credentials, was never archived anywhere, and cannot be
    restored -- downgrading past this revision does not (and cannot) un-redact
    anything. But a pure no-op here would leave a real gap: a row inserted
    (e.g. via raw SQL, bypassing the application) after upgrade() ran but
    before an operator downgrades would sail through unredacted, since
    nothing else would ever touch it. Re-running the sweep closes that gap
    and still satisfies CI's mandatory upgrade -> downgrade -> upgrade
    roundtrip (scripts/ci/migration_roundtrip.py), which requires this
    function not to raise. See _redact_rows() and the module docstring for
    why downgrade() shares upgrade()'s exact sweep rather than needing its
    own shape-scoped variant.
    """
    _redact_rows()
