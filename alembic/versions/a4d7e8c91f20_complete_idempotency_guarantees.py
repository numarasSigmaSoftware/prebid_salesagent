"""complete idempotency read and downstream reconciliation guarantees

Revision ID: a4d7e8c91f20
Revises: f3a1c92b47de
Create Date: 2026-07-24 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a4d7e8c91f20"
down_revision: str | Sequence[str] | None = "f3a1c92b47de"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Widen principal_id, split admission classes, and add claims.

    principal_id is nullable for BACKWARD COMPATIBILITY, not because current
    code writes a NULL-principal row: an anonymous keyed read now skips
    reservation entirely (see idempotency_replay._read_scope) rather than
    persisting one under a shared (tenant, NULL, NULL) scope. This column stays
    nullable to accommodate rows already written under the prior design;
    see test_complete_idempotency_guarantees_migration.py for the prune this
    enables downstream.
    """
    op.alter_column("idempotency_attempts", "principal_id", existing_type=sa.String(length=50), nullable=True)
    op.add_column(
        "idempotency_attempts",
        sa.Column("operation_class", sa.String(length=8), nullable=False, server_default="write"),
    )
    op.create_check_constraint(
        "ck_idempotency_attempt_operation_class",
        "idempotency_attempts",
        "operation_class IN ('read', 'write')",
    )
    op.create_table(
        "downstream_mutation_claims",
        sa.Column("claim_id", sa.String(length=50), nullable=False),
        sa.Column("tenant_id", sa.String(length=50), nullable=False),
        sa.Column("principal_id", sa.String(length=50), nullable=False),
        sa.Column("account_id", sa.String(length=100), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("operation_key", sa.String(length=255), nullable=False),
        sa.Column("downstream_request_id", sa.String(length=64), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="planned"),
        sa.Column("result_metadata", sa.JSON(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.tenant_id"],
            name="fk_downstream_mutation_claims_tenant",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "principal_id"],
            ["principals.tenant_id", "principals.principal_id"],
            name="fk_downstream_mutation_claims_principal",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "status IN ('planned', 'invoked', 'applied', 'unknown')",
            name="ck_downstream_mutation_claim_status",
        ),
        sa.PrimaryKeyConstraint("claim_id"),
    )
    op.create_index(
        "idx_downstream_mutation_claims_scope",
        "downstream_mutation_claims",
        ["tenant_id", "principal_id", "account_id", "idempotency_key", "provider", "operation_key"],
        unique=True,
        postgresql_nulls_not_distinct=True,
    )
    op.create_index(
        "idx_downstream_mutation_claims_expires_at",
        "downstream_mutation_claims",
        ["expires_at"],
        unique=False,
    )


def downgrade() -> None:
    """Remove reconciliation claims and restore authenticated-only attempts."""
    op.drop_index("idx_downstream_mutation_claims_expires_at", table_name="downstream_mutation_claims")
    op.drop_index("idx_downstream_mutation_claims_scope", table_name="downstream_mutation_claims")
    op.drop_table("downstream_mutation_claims")
    op.execute("DELETE FROM idempotency_attempts WHERE principal_id IS NULL")
    op.drop_constraint(
        "ck_idempotency_attempt_operation_class",
        "idempotency_attempts",
        type_="check",
    )
    op.drop_column("idempotency_attempts", "operation_class")
    op.alter_column("idempotency_attempts", "principal_id", existing_type=sa.String(length=50), nullable=False)
