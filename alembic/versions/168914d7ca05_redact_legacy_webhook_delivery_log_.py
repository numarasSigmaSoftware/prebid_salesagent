"""redact legacy webhook delivery log credentials

Historical rows in ``webhook_delivery_log`` were written before the
credential-redaction work in PR #1575: ``webhook_url`` held the complete,
unredacted outbound URL (userinfo, capability-host, path, query, and fragment
credentials all survived verbatim), and ``error_message`` could carry the same
data via an unsanitized ``str(exception)`` -- ``requests.HTTPError`` embeds the
full request URL in its string form.

A keyed digest (matching what the new code computes at runtime) is deliberately
NOT used here: that would require importing application config/service code
into a migration, coupling a migration that must stay replayable forever to
code that will keep changing. A constant, non-correlating placeholder is safer
and simpler -- we cannot recover any operationally useful information from
these rows regardless, so there is nothing lost by not attempting a "real"
digest.

downgrade() does not raise: this migration makes no schema changes to revert.
(An earlier version of this note justified that by CI's Migration Roundtrip job.
That was true when written -- this revision was head -- but b5838b839548 has
since landed on top of it. The job downgrades ONE step from head, i.e. TO this
revision, so it runs b5838's downgrade() and never this one's. The job imposes
no requirement on this function; not raising is simply correct on its own.)

downgrade() also does not merely no-op: it sweeps the table for every row
that isn't already exactly the placeholder, so a row inserted (e.g. via raw
SQL, bypassing the application entirely) after upgrade() ran but before an
operator downgrades still gets redacted -- a print-only downgrade would leave
that window open.

upgrade() and downgrade() share ONE sweep (_redact_rows()): both columns are
trusted ONLY via exact equality to this migration's own placeholder constant
-- never by shape, never by any heuristic about what a value "looks like" --
on every transition (first upgrade, downgrade, re-upgrade, no distinction).
This is the end state of several earlier, narrower designs, each of which
tried to preserve SOME "safe-shaped" value instead of unconditionally
replacing it, and each of which a real credential slipped through:

1. A bare `LIKE '%REDACTED%'` substring check on webhook_url misclassified
   any raw, still-credentialed value that merely happened to CONTAIN the
   word REDACTED somewhere in its own path or query as already-safe.

2. Anchoring error_message only at the trailing end (``... delivering
   webhook to <shape>$``, no leading ``^``) let a credential-bearing PREFIX
   before that marker -- e.g. "token=still-secret delivering webhook to
   REDACTED" -- still pass, since PostgreSQL's ``~`` searches anywhere in
   the string unless the pattern itself starts with ``^``.

3. Fully anchoring error_message at both ends, and applying that check only
   to downgrade() (making upgrade() unconditional instead, on the theory
   that every row present at upgrade time is unsafe by definition), closed
   the prefix bypass but still trusted error_message's bounded *reason*
   grammar. An ordinary-looking real credential (e.g. an alphanumeric API
   token) can land in that exact grammar by coincidence -- not
   hypothetically: "tokenSecret123 delivering webhook to REDACTED" matched
   it. Worse, "every row at upgrade time is unsafe" is only true for the
   FIRST upgrade: on any re-upgrade the fixed runtime may already have
   written a genuinely safe row that downgrade correctly preserved, and
   making upgrade() unconditional destroyed exactly that row. (This
   previously cited CI's roundtrip job as the sequence that forces a
   re-upgrade. It does not: the job downgrades one step from head, which is
   now b5838b839548, so this revision's upgrade() runs exactly once and is
   never re-run. The conditional sweep is still the right design -- it rests
   on the raw-SQL-bypass threat model below, which does not depend on CI --
   but it is not CI that exercises it.)

4. Keeping error_message fully fail-closed (never shape-checked, on any
   transition) but still trusting webhook_url's shape unconditionally --
   reasoning that its safe shapes (see the retired _REDACT_URL_SHAPE
   pattern) all either equal the literal "REDACTED", or embed a literal
   ``<``/``>``, which RFC 3986 excludes from valid URI syntax unencoded, so
   no *validated, application-written* URL could ever collide with it. That
   argument is true as far as it goes, but it answers the wrong question:
   this migration's OWN threat model (see the raw-SQL-bypass reasoning
   above) is not limited to application-validated data. A raw SQL insert is
   not constrained by URL validation at all, and the "safe" shape's key-ID
   and digest slots (``[A-Za-z0-9._-]{1,32}`` and ``[0-9a-f]{32}``
   respectively) are exactly the character classes a real credential
   commonly takes: ``https://<redacted:sk_live_secret:
   0123456789abcdef0123456789abcdef>`` matches the shape in full while
   embedding what could be two real credential fragments. A regex proves
   only that a value has the right SYNTAX -- never that it actually came
   from the trusted runtime. Shape was never a legitimate provenance proof
   for either column; it appeared to work for webhook_url only because a
   counterexample requires deliberately-crafted content, which the
   substring/prefix bugs above didn't need.

A non-cryptographic marker (e.g. a new boolean/timestamp column the runtime
sets) would NOT fix this: raw SQL access that can already write literal
``<``/``>`` into webhook_url can just as easily set a marker column to
whatever value makes a row look trusted. Closing this for good would need
CRYPTOGRAPHIC provenance -- something requiring the runtime's actual secret
key to compute and verify -- which reopens the exact migration/app-code
coupling this migration has rejected from the start (see the keyed-digest
paragraph above). Given that, trusting nothing but this migration's own
exact, constant placeholder string is not a fallback choice; it is the only
option that holds up against the threat model this migration has committed
to since downgrade()'s re-sweep was introduced.

What this gives up: a downgrade or re-upgrade no longer preserves the fixed
runtime's keyed digest or bounded failure classification -- both columns are
replaced with the generic placeholder on every transition, even for rows the
runtime wrote completely correctly. This is judged acceptable: this specific
migration reverts no schema (see the "downgrade() does not raise" paragraph
above), so a real production downgrade-then-reupgrade of JUST this revision,
with genuine traffic in between, is not a realistic operational pattern --
and no automated job exercises this sequence either -- CI's roundtrip
downgrades one step from head (b5838b839548), stopping AT this revision rather
than through it, so this revision's downgrade() never runs there. And even in
the case where a broader,
multi-revision rollback DOES pass through this one while the fixed runtime
keeps running: the cost is a genericized webhook_url/error_message during
that window, a debugging inconvenience -- not a security exposure, and every
other column on the row (id, tenant_id, principal_id, media_buy_id,
task_type, status, timestamps) is untouched and still provides correlation
value.

Operational note: _redact_rows() issues one unbounded UPDATE with no
batching. This is deliberate, not an oversight -- see the "What this gives
up" reasoning above for why the sweep must be unconditional and exact, and
batching would add cursor logic to a file whose entire value is being
trivially auditable.

Its deployment cost is the UPDATE statement's own execution footprint --
whatever WAL it generates and whatever row locks it holds while running,
for however long it takes to touch every qualifying row -- and nothing
mitigates that footprint; it is what it is, for the duration of the
statement. (An earlier version of this note claimed the statement's locks
persist until the whole `alembic upgrade head` invocation commits, and
recommended running this revision as an isolated step to bound that. That
claim was wrong: b5838b839548, the very next migration in this chain,
opens an autocommit_block(), which alembic's own documentation states
unconditionally commits the preceding ambient transaction -- so in this
actual chain, this migration's locks are released as soon as the very
next migration boots, automatically, regardless of what an operator does.
Manually isolating this revision as its own step therefore achieves
nothing beyond what already happens; that advice is intentionally removed
rather than left to mislead.)

Before deploying to an environment with meaningful traffic, check
`SELECT count(*) FROM webhook_delivery_log` -- a large table means a
correspondingly long-running single statement, full stop, with no
operator-side mitigation available today. (Separately: webhook_delivery_log
has no retention/pruning policy anywhere in this codebase, so it grows
unboundedly over time -- that is the actual root cause behind this
migration's cost, and is a more durable fix than anything migration-side;
worth its own follow-up.)

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


def _redact_rows() -> None:
    """Overwrite webhook_url and error_message with the constant placeholder
    for every row that isn't already exactly that placeholder -- no shape
    check for either column, on any transition (first upgrade, downgrade,
    re-upgrade -- shared by upgrade() and downgrade() below, no
    distinction). See the module docstring for the full history of why:
    shape only proves a value's SYNTAX, never that it actually came from the
    trusted runtime, and this migration's own raw-SQL-bypass threat model
    (see "downgrade() also does not merely no-op" in the module docstring)
    means shape can always be forged by whatever wrote the row.

    Skipping rows already EXACTLY equal to the placeholder is not a shape
    heuristic: it is a plain equality comparison against a constant this
    migration itself controls, so it can only recognize output this
    migration already wrote -- it cannot misclassify a real credential as
    safe, and it avoids real, avoidable churn (PostgreSQL creates a new row
    version -- WAL, row lock -- for any UPDATE that touches a row regardless
    of whether the written value differs) on a row that's already done.
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


def upgrade() -> None:
    """Redact every row present at upgrade time -- see _redact_rows() and
    the module docstring for why neither column is shape-checked, on any
    transition."""
    _redact_rows()


def downgrade() -> None:
    """No schema to revert, but re-sweep anyway rather than no-op.

    The original webhook_url/error_message content upgrade() replaced
    contained buyer credentials, was never archived anywhere, and cannot be
    restored -- downgrading past this revision does not (and cannot) un-redact
    anything. But a pure no-op here would leave a real gap: a row inserted
    (e.g. via raw SQL, bypassing the application) after upgrade() ran but
    before an operator downgrades would sail through unredacted, since
    nothing else would ever touch it. Re-running the sweep closes that gap.
    (This previously claimed CI's roundtrip job requires this function not to
    raise. It does not run this function at all -- it downgrades one step from
    head, which is b5838b839548.) See _redact_rows() and the module docstring for
    why downgrade() shares upgrade()'s exact sweep.
    """
    _redact_rows()
