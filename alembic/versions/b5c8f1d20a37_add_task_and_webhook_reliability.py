"""add task and webhook reliability state

Revision ID: b5c8f1d20a37
Revises: a4d7e8c91f20
Create Date: 2026-07-25 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from src.core.database.json_type import JSONType

revision: str = "b5c8f1d20a37"
down_revision: str | Sequence[str] | None = "a4d7e8c91f20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _batch_backfill_logical_event_key(*, batch_size: int = 1000) -> None:
    """Backfill ``logical_event_key`` in bounded chunks.

    ``webhook_delivery_log`` is a hot, populated production table; a single
    unbatched ``UPDATE ... WHERE logical_event_key IS NULL`` would scan and
    lock the whole table in one statement. ``alembic/env.py`` runs this
    migration's entire ``upgrade()`` inside ONE transaction
    (``context.begin_transaction()``), so chunking does NOT bound the total
    lock HOLD duration — locks acquired by earlier chunks are still held when
    later chunks run, same as an unbatched UPDATE, and all release together at
    commit. What chunking DOES bound is each individual UPDATE statement's
    row-scan/lock-acquisition footprint, so no single statement holds the
    full-table scan open at once.
    """
    bind = op.get_bind()
    while True:
        result = bind.execute(
            sa.text(
                "UPDATE webhook_delivery_log SET logical_event_key = id "
                "WHERE id IN ("
                "SELECT id FROM webhook_delivery_log WHERE logical_event_key IS NULL LIMIT :batch_size"
                ")"
            ),
            {"batch_size": batch_size},
        )
        if result.rowcount == 0:
            break


def upgrade() -> None:
    """Add durable task, callback, revision, and expanded claim state."""
    op.add_column("webhook_delivery_log", sa.Column("logical_event_key", sa.String(length=64), nullable=True))
    _batch_backfill_logical_event_key()
    op.add_column("webhook_delivery_log", sa.Column("event_payload", JSONType, nullable=True))

    op.add_column("push_notification_configs", sa.Column("media_buy_id", sa.String(length=100), nullable=True))
    op.add_column("push_notification_configs", sa.Column("operation_id", sa.String(length=255), nullable=True))
    op.add_column("push_notification_configs", sa.Column("token", sa.Text(), nullable=True))
    op.add_column("push_notification_configs", sa.Column("application_context", JSONType, nullable=True))
    op.add_column("push_notification_configs", sa.Column("last_event_key", sa.String(length=64), nullable=True))
    op.add_column(
        "push_notification_configs",
        sa.Column("last_event_sequence", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_foreign_key(
        "fk_push_notification_configs_media_buy",
        "push_notification_configs",
        "media_buys",
        ["media_buy_id"],
        ["media_buy_id"],
        ondelete="CASCADE",
    )

    # Both indexes are brand-new (no prior name to protect), so a plain
    # CREATE INDEX CONCURRENTLY IF NOT EXISTS suffices -- no build-under-a-
    # temp-name-then-rename dance is needed (unlike
    # a164b85bab9e_widen_media_buys_idempotency_backstop_.py, which swaps an
    # EXISTING index name and so must avoid a coverage gap). CONCURRENTLY
    # cannot run inside a transaction alongside other DDL, so both builds are
    # grouped into one autocommit_block(); the add_column/FK calls above and
    # the DDL below run in the normal transactional mode on either side of it.
    with op.get_context().autocommit_block():
        op.execute(
            "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uq_webhook_log_logical_event "
            "ON webhook_delivery_log (tenant_id, principal_id, media_buy_id, webhook_url, logical_event_key)"
        )
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_push_notification_configs_media_buy "
            "ON push_notification_configs (tenant_id, principal_id, media_buy_id)"
        )

    # IF NOT EXISTS, not a plain add_column: this migration's other parent
    # chain (merged at 8632cb568d0b) also adds media_buys.revision, in
    # 1497aa06013c. A full upgrade-to-head applies both parents before the
    # merge point, so a plain add_column here would collide with whichever
    # parent's chain ran first. Idempotent either way: this branch's own
    # isolated history (e.g. tests upgrading only to this revision, which
    # predates the merge and never touches 1497aa06013c) still gets the
    # column; the merged-history path is a no-op here.
    op.execute("ALTER TABLE media_buys ADD COLUMN IF NOT EXISTS revision INTEGER NOT NULL DEFAULT 1")
    op.add_column("workflow_steps", sa.Column("processing_started_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("workflow_steps", sa.Column("notifications_published_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("workflow_steps", sa.Column("notification_claimed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("workflow_steps", sa.Column("notification_claim_token", sa.String(length=36), nullable=True))
    op.add_column(
        "workflow_steps",
        sa.Column("notification_sequence", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_unique_constraint("uq_contexts_tenant_context", "contexts", ["tenant_id", "context_id"])
    op.create_unique_constraint("uq_workflow_steps_context_step", "workflow_steps", ["context_id", "step_id"])
    op.execute(
        "UPDATE workflow_steps SET notifications_published_at = NOW() "
        "WHERE status IN ('completed', 'failed', 'rejected', 'canceled')"
    )

    op.drop_constraint(
        "ck_downstream_mutation_claim_status",
        "downstream_mutation_claims",
        type_="check",
    )
    op.create_check_constraint(
        "ck_downstream_mutation_claim_status",
        "downstream_mutation_claims",
        "status IN ('planned', 'invoked', 'applied', 'not_applied', 'unknown', 'expired')",
    )

    op.create_table(
        "a2a_tasks",
        sa.Column("task_id", sa.String(length=100), nullable=False),
        sa.Column("tenant_id", sa.String(length=50), nullable=False),
        sa.Column("principal_id", sa.String(length=50), nullable=False),
        sa.Column("context_id", sa.String(length=100), nullable=True),
        sa.Column("workflow_step_id", sa.String(length=100), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("task_payload", JSONType, nullable=False),
        sa.Column("notification_sequence", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_notification_status", sa.String(length=32), nullable=True),
        sa.Column("last_notification_event_id", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "principal_id"],
            ["principals.tenant_id", "principals.principal_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["workflow_step_id"], ["workflow_steps.step_id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("task_id"),
        sa.UniqueConstraint("tenant_id", "task_id", name="uq_a2a_tasks_tenant_task"),
    )
    op.create_index("idx_a2a_tasks_owner", "a2a_tasks", ["tenant_id", "principal_id"])
    op.create_index("uq_a2a_tasks_workflow_step", "a2a_tasks", ["workflow_step_id"], unique=True)
    op.create_table(
        "a2a_task_notification_events",
        sa.Column("event_id", sa.String(length=255), nullable=False),
        sa.Column("tenant_id", sa.String(length=50), nullable=False),
        sa.Column("task_id", sa.String(length=100), nullable=False),
        sa.Column("principal_id", sa.String(length=50), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("task_payload", JSONType, nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_token", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "principal_id"],
            ["principals.tenant_id", "principals.principal_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "task_id"],
            ["a2a_tasks.tenant_id", "a2a_tasks.task_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("event_id"),
    )
    op.create_index(
        "idx_a2a_task_notifications_pending",
        "a2a_task_notification_events",
        ["tenant_id", "delivered_at", "created_at"],
    )
    op.create_table(
        "workflow_notification_events",
        sa.Column("event_id", sa.String(length=255), nullable=False),
        sa.Column("tenant_id", sa.String(length=50), nullable=False),
        sa.Column("context_id", sa.String(length=100), nullable=False),
        sa.Column("step_id", sa.String(length=100), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("response_data", JSONType, nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_token", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["tenant_id", "context_id"],
            ["contexts.tenant_id", "contexts.context_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["context_id", "step_id"],
            ["workflow_steps.context_id", "workflow_steps.step_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("event_id"),
    )
    op.create_index(
        "idx_workflow_notifications_pending",
        "workflow_notification_events",
        ["tenant_id", "step_id", "delivered_at", "sequence"],
    )


def downgrade() -> None:
    """Remove task and webhook reliability state."""
    op.drop_index("idx_workflow_notifications_pending", table_name="workflow_notification_events")
    op.drop_table("workflow_notification_events")
    op.drop_index("idx_a2a_task_notifications_pending", table_name="a2a_task_notification_events")
    op.drop_table("a2a_task_notification_events")
    op.drop_index("uq_a2a_tasks_workflow_step", table_name="a2a_tasks")
    op.drop_index("idx_a2a_tasks_owner", table_name="a2a_tasks")
    op.drop_table("a2a_tasks")

    op.drop_constraint(
        "ck_downstream_mutation_claim_status",
        "downstream_mutation_claims",
        type_="check",
    )
    op.execute("UPDATE downstream_mutation_claims SET status = 'unknown' WHERE status IN ('not_applied', 'expired')")
    op.create_check_constraint(
        "ck_downstream_mutation_claim_status",
        "downstream_mutation_claims",
        "status IN ('planned', 'invoked', 'applied', 'unknown')",
    )

    op.drop_column("workflow_steps", "notification_claim_token")
    op.drop_column("workflow_steps", "notification_claimed_at")
    op.drop_column("workflow_steps", "notifications_published_at")
    op.drop_constraint("uq_workflow_steps_context_step", "workflow_steps", type_="unique")
    op.drop_constraint("uq_contexts_tenant_context", "contexts", type_="unique")
    op.drop_column("workflow_steps", "notification_sequence")
    op.drop_column("workflow_steps", "processing_started_at")
    # IF EXISTS, mirroring the idempotent add above: this branch's own
    # downgrade must not assume it owns the column when the other parent
    # chain (1497aa06013c) may have added it instead.
    op.execute("ALTER TABLE media_buys DROP COLUMN IF EXISTS revision")

    # Mirror upgrade()'s grouping: both DROP INDEX CONCURRENTLY statements
    # together in one autocommit_block(), since CONCURRENTLY cannot run
    # inside a transaction alongside other DDL. The FK/column drops that
    # follow run in the normal transactional mode after the block exits.
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_push_notification_configs_media_buy")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS uq_webhook_log_logical_event")

    op.drop_constraint(
        "fk_push_notification_configs_media_buy",
        "push_notification_configs",
        type_="foreignkey",
    )
    op.drop_column("push_notification_configs", "last_event_sequence")
    op.drop_column("push_notification_configs", "last_event_key")
    op.drop_column("push_notification_configs", "application_context")
    op.drop_column("push_notification_configs", "token")
    op.drop_column("push_notification_configs", "operation_id")
    op.drop_column("push_notification_configs", "media_buy_id")

    op.drop_column("webhook_delivery_log", "event_payload")
    op.drop_column("webhook_delivery_log", "logical_event_key")
