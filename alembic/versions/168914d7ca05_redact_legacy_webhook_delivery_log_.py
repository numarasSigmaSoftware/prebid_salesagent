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
no ambiguity to resolve between "legacy" and "already safe" rows. upgrade()
takes this guarantee at face value and redacts unconditionally (see
_redact_unconditionally() below) -- this is why upgrade() and downgrade() are
NOT the same operation, unlike an earlier version of this migration.

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

upgrade() and downgrade() deliberately do NOT share one code path (an earlier
version of this migration had them share a single shape-scoped sweep).
upgrade() redacts EVERY row unconditionally (_redact_unconditionally());
downgrade() only touches rows whose webhook_url or error_message does not
already exactly match an audit-safe shape (_redact_if_unsafe(), matching one
of the runtime's ``<redacted...>``/``REDACTED`` forms -- see
_redact_url_credentials() in protocol_webhook_service.py -- or this
migration's own placeholder). Each half of that split closes a real,
previously-shipped bug that the OTHER half's behavior would reintroduce:

- A shape-scoped check is REQUIRED for downgrade(): rows written normally by
  the fixed runtime after this migration first ran already carry a keyed,
  correlatable digest and a bounded failure classification, and an
  unconditional re-sweep on every downgrade/re-upgrade would destroy that
  real audit value for no reason, replacing it with the generic
  non-correlating placeholder. (An earlier version of this migration swept
  unconditionally on every call and had exactly this bug.) Re-writing every
  touched row is also not a "no-op": PostgreSQL creates a new row version
  (and writes WAL, and takes a row lock) for any UPDATE that touches a row,
  regardless of whether the written value differs from the old one -- so an
  unconditional sweep on every downgrade was real, unnecessary churn too,
  not just a correctness bug.

- A shape-scoped check is WRONG for upgrade(): at upgrade() time there is no
  "already safe" row to preserve in the first place (see above), so
  shape-checking there can only ever produce a false negative -- skipping a
  genuinely-unsafe legacy row because it happens to LOOK safe. Concretely: a
  raw legacy error_message reading "tokenSecret123 delivering webhook to
  REDACTED" is string-for-string indistinguishable, BY SHAPE, from the
  genuinely-safe bare-exception-class-name reason format ("ConnectionError
  delivering webhook to REDACTED") -- both are just an identifier
  (letters/digits/underscore, starting with a letter or underscore) followed
  by the same literal marker text. "tokenSecret123" is an entirely ordinary
  shape for a real API token or session key, so this is not a contrived
  case. (An earlier version of this migration shared its shape-scoped sweep
  with upgrade() and had exactly this bug -- two independent review passes
  over the fully-anchored predicate below both reproduced it.)

The "already safe" check downgrade() still needs matches the EXACT anchored
shape -- ``^...$`` for webhook_url; for error_message, both the bounded
*reason* prefix AND the URL suffix, ALSO anchored at both ends -- never a
bare substring test and never an anchor at only one end. Three earlier
versions of this migration got the classification wrong in three different
ways: one used `LIKE '%REDACTED%'`, misclassifying any raw, still-credentialed
value that merely happened to CONTAIN the word REDACTED somewhere in its own
path or query as already-safe. The next anchored error_message only at the
end (``... delivering webhook to <shape>$`` with no leading ``^``), which let
a credential-bearing PREFIX before that marker -- e.g. "token=still-secret
delivering webhook to REDACTED" -- still pass, since PostgreSQL's ``~``
searches anywhere in the string unless the pattern itself starts with ``^``.
The next fully anchored the shape but applied it to upgrade() too, which is
the identifier-shaped-credential bug described above. All three mistakes led
to the same outcome: the row was skipped, and its real credential survived
unredacted. Restricting the shape check to ONLY downgrade() -- the one
transition where an "already safe" row can genuinely exist -- closes the
class of bug for upgrade() entirely; downgrade() keeps a narrower, accepted
residual risk documented on _redact_if_unsafe() below.

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
# value (webhook_url) or the tail of it (error_message, after the
# " delivering webhook to " marker) to match one of the three literal forms
# _redact_url_credentials() can emit -- not just contain a keyword anywhere.
#   scheme: RFC 3986 ALPHA *(ALPHA / DIGIT / "+" / "-" / ".")
#   key_id: constrained by AppConfig.webhook_audit_hmac_key_id's validator to [A-Za-z0-9._-]{1,32}
#   digest: hashlib .hexdigest()[:32] -> exactly 32 lowercase hex characters
# Consulted ONLY by _redact_if_unsafe() (downgrade()) -- see the module
# docstring for why _redact_unconditionally() (upgrade()) never checks shape.
_SCHEME = r"[a-zA-Z][a-zA-Z0-9+.-]*"
_KEY_ID = r"[A-Za-z0-9._-]{1,32}"
_DIGEST = r"[0-9a-f]{32}"
_REDACT_URL_SHAPE = rf"(?:REDACTED|{_SCHEME}://<redacted>|{_SCHEME}://<redacted:{_KEY_ID}:{_DIGEST}>)"

# The *reason* prefix _safe_delivery_error_message() (protocol_webhook_service.py)
# is called with, at its three call sites -- nothing else is ever passed:
#   f"HTTP {status_code} error"                  (status_code is a real HTTP
#                                                   status: always exactly 3
#                                                   digits, or the literal
#                                                   None -- see the
#                                                   `e.response is not None`
#                                                   comment at that call site)
#   type(e).__name__                             (a Python exception class name)
#   f"Unexpected error ({type(e).__name__})"
# _HTTP_STATUS is deliberately exactly 3 digits, not `[0-9]+`: an unbounded
# digit run would also recognize a numeric-only credential (e.g. a PIN or
# numeric API key) wrapped in "HTTP <it> error" as an already-safe shape.
#
# _EXCEPTION_CLASS_NAME has NO equivalent tightening available: a real Python
# exception class name and an identifier-shaped credential (e.g. an
# alphanumeric API token, "tokenSecret123") are the same grammar --
# letters/digits/underscore, starting with a letter or underscore -- and
# there is no way to tell them apart by shape alone. Enumerating real
# exception class names would require importing application code into this
# migration, the exact coupling the module docstring already rejects for the
# digest computation. This is why _redact_unconditionally() (upgrade()) does
# not use _REASON_SHAPE at all: upgrade() faces this exact ambiguity on every
# ordinary legacy row with no bypass required, so it cannot safely rely on
# shape. _redact_if_unsafe() (downgrade()) still has to accept this as a
# narrow, accepted residual: it only matters for a row inserted via raw SQL,
# bypassing the application, whose error_message also happens to match this
# one specific grammar.
_EXCEPTION_CLASS_NAME = r"[A-Za-z_][A-Za-z0-9_]*"
_HTTP_STATUS = r"(?:[0-9]{3}|None)"
_REASON_SHAPE = rf"(?:HTTP {_HTTP_STATUS} error|{_EXCEPTION_CLASS_NAME}|Unexpected error \({_EXCEPTION_CLASS_NAME}\))"

# webhook_url must be the shape in full. error_message must be the shape in
# full TOO: the bounded reason, then " delivering webhook to ", then the URL
# shape, and NOTHING else before or after -- anchored at BOTH ends (^...$).
# An earlier version of this migration anchored only the trailing "$", which
# let error_message = "<anything>" + " delivering webhook to " + <safe URL>
# through: PostgreSQL's `~` searches anywhere in the string unless the
# pattern itself starts with `^`, so a credential-bearing prefix before that
# marker (e.g. "token=still-secret delivering webhook to REDACTED") was
# wrongly classified as already-safe and would have survived untouched
# forever. Anchoring the reason too closes the PREFIX class of bug -- but,
# per the _EXCEPTION_CLASS_NAME note above, does NOT make these patterns safe
# to use against rows that are guaranteed unsafe (upgrade()'s rows): it only
# narrows "already safe" to the three bounded forms production actually
# emits, which still overlaps with an identifier-shaped credential.
_URL_SAFE_PATTERN = f"^{_REDACT_URL_SHAPE}$"
_ERROR_SAFE_PATTERN = rf"^{_REASON_SHAPE} delivering webhook to {_REDACT_URL_SHAPE}$"


def _redact_unconditionally() -> None:
    """Overwrite webhook_url and non-null error_message with the constant
    placeholder for EVERY row -- no shape check. upgrade() is the ONLY
    caller: every row present when it runs was necessarily written by the
    OLD, unredacted code path (see the module docstring's opening
    paragraphs), so there is no "already safe" row to preserve at that
    point, and nothing is gained by asking whether a row's current shape
    LOOKS safe.

    That question is unanswerable from shape alone, and an earlier version
    of this migration asking it anyway -- by sharing this sweep with
    downgrade()'s shape-scoped predicate -- was a real, shipped bug: a raw
    legacy error_message reading "tokenSecret123 delivering webhook to
    REDACTED" is string-for-string indistinguishable, BY SHAPE, from the
    genuinely-safe bare-exception-class-name reason format. A shape-based
    predicate applied at upgrade() time let that row survive, un-redacted,
    forever. See the _EXCEPTION_CLASS_NAME comment above for why that
    ambiguity cannot be closed by a tighter regex.

    Skipping rows already EXACTLY equal to the placeholder (relevant on a
    re-upgrade after a downgrade, e.g. CI's mandatory roundtrip) is not that
    same mistake: it is a plain equality comparison against a constant this
    migration itself controls, never a heuristic about what unknown legacy
    content might mean -- it can only recognize output this migration
    already wrote, so it cannot misclassify a real credential as safe.
    """
    connection = op.get_bind()
    connection.execute(
        text(
            """
            UPDATE webhook_delivery_log
            SET webhook_url = :placeholder,
                error_message = CASE WHEN error_message IS NULL THEN NULL ELSE :placeholder END
            WHERE webhook_url != :placeholder
               OR (error_message IS NOT NULL AND error_message != :placeholder)
            """
        ),
        {"placeholder": _REDACTED_PLACEHOLDER},
    )


def _redact_if_unsafe() -> None:
    """Overwrite webhook_url and non-null error_message with a constant,
    non-correlating placeholder -- but ONLY for rows that don't already
    exactly match an audit-safe shape (see _URL_SAFE_PATTERN /
    _ERROR_SAFE_PATTERN above; both compared as a full regex match, never a
    substring test, via a BOUND parameter -- not interpolated into the SQL
    text -- so a literal ``:`` inside the pattern, e.g. from the non-capturing
    group ``(?:...)``, can never be misread as a bind-parameter placeholder).

    downgrade() is the ONLY caller. Unlike upgrade(), downgrade() can
    genuinely encounter BOTH kinds of row: one written normally by the fixed
    runtime after upgrade() first ran -- already carrying a keyed,
    correlatable digest and a bounded failure classification, which must
    survive byte-for-byte -- and one written via raw SQL bypassing the
    application entirely after upgrade() ran, which must still be caught.
    Shape is the only signal available to tell these apart at this point, so
    (unlike upgrade()) downgrade() has to use it. PostgreSQL creates a new
    row version (WAL, row lock) for any UPDATE that touches a row regardless
    of whether the written value differs, so scoping to genuinely-unsafe
    rows also avoids real, avoidable churn on every downgrade.

    Accepted residual: per the _EXCEPTION_CLASS_NAME comment above, a raw
    SQL-inserted row whose error_message happens to read
    "<identifier-shaped-credential> delivering webhook to <safe-url-shape>"
    is still classified as already-safe here. Closing that too would require
    an exhaustive allowlist of real exception class names sourced from
    application code -- the exact migration/app coupling the module
    docstring already rejects for the digest computation, for the same
    reason. This residual is judged acceptable because it requires BOTH a
    raw-SQL bypass of the application AND a value that happens to match this
    one narrow grammar -- unlike upgrade(), which faced the identical
    ambiguity on every ordinary legacy row already sitting in the table,
    with no bypass required at all.
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
                    WHEN error_message = :placeholder OR error_message ~ :error_pattern THEN error_message
                    ELSE :placeholder
                END
            WHERE NOT (webhook_url = :placeholder OR webhook_url ~ :url_pattern)
               OR (
                    error_message IS NOT NULL
                    AND NOT (error_message = :placeholder OR error_message ~ :error_pattern)
                  )
            """
        ),
        {
            "placeholder": _REDACTED_PLACEHOLDER,
            "url_pattern": _URL_SAFE_PATTERN,
            "error_pattern": _ERROR_SAFE_PATTERN,
        },
    )


def upgrade() -> None:
    """Redact every row present at upgrade time, unconditionally. At the
    point this migration first runs, every row was written by the old code
    path -- there is no ambiguity to resolve between "legacy" and "already
    safe" rows here (see the module docstring), so upgrade() does NOT use
    the shape-based safety check downgrade() needs -- see
    _redact_unconditionally()."""
    _redact_unconditionally()


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
    function not to raise. Unlike upgrade(), downgrade() uses the
    shape-scoped sweep (_redact_if_unsafe()) since it can genuinely
    encounter rows already written safely by the fixed runtime -- see
    _redact_if_unsafe() and the module docstring for why the two functions
    are not the same operation.
    """
    _redact_if_unsafe()
