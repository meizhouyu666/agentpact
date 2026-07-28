"""Create persisted Phase 2 execution permits.

Revision ID: ent_003
Revises: ent_002
Create Date: 2026-07-19
"""

from alembic import op
import sqlalchemy as sa

revision = "ent_003"
down_revision = "ent_002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "execution_permits",
        sa.Column("permit_id", sa.String(), primary_key=True),
        sa.Column("task_id", sa.String(), sa.ForeignKey("tasks.task_id"), nullable=False),
        sa.Column("step_id", sa.String(), sa.ForeignKey("steps.step_id"), nullable=False),
        sa.Column("contract_id", sa.String(), sa.ForeignKey("task_contracts.contract_id"), nullable=False),
        sa.Column("action_fingerprint", sa.String(), nullable=False),
        sa.Column("observation_hash", sa.String(), nullable=False),
        sa.Column("policy_decision_id", sa.String(), nullable=False),
        sa.Column("decision_payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("issued_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("used_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("status IN ('issued', 'consumed', 'revoked', 'expired')", name="ck_execution_permit_status"),
    )
    op.create_index("idx_permit_task_status", "execution_permits", ["task_id", "status"])


def downgrade() -> None:
    op.drop_index("idx_permit_task_status", table_name="execution_permits")
    op.drop_table("execution_permits")
