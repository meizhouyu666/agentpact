"""Create Phase 2 governance baseline tables.

Revision ID: ent_002
Revises: ent_001
Create Date: 2026-07-18
"""

from alembic import op
import sqlalchemy as sa

revision = "ent_002"
down_revision = "ent_001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "task_contracts",
        sa.Column("contract_id", sa.String(), primary_key=True),
        sa.Column("task_id", sa.String(), sa.ForeignKey("tasks.task_id"), nullable=False),
        sa.Column("organization_id", sa.String(), sa.ForeignKey("organizations.organization_id"), nullable=False),
        sa.Column("initiator_id", sa.String(), nullable=True),
        sa.Column("service_principal_id", sa.String(), nullable=True),
        sa.Column("department_id", sa.String(), nullable=True),
        sa.Column("business_line_id", sa.String(), nullable=True),
        sa.Column("goal", sa.Text(), nullable=False),
        sa.Column("allowed_operations", sa.JSON(), nullable=False),
        sa.Column("data_scope", sa.JSON(), nullable=False),
        sa.Column("authorization_snapshot", sa.JSON(), nullable=False),
        sa.Column("policy_profile", sa.String(), nullable=False),
        sa.Column("policy_version", sa.String(), nullable=False),
        sa.Column("success_criteria", sa.JSON(), nullable=False),
        sa.Column("mode", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("modified_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("task_id", name="uq_task_contract_task"),
        sa.CheckConstraint("mode IN ('off', 'audit', 'enforce')", name="ck_task_contract_mode"),
        sa.CheckConstraint("version > 0", name="ck_task_contract_version"),
    )
    op.create_index("idx_tc_org_mode", "task_contracts", ["organization_id", "mode"])

    op.create_table(
        "governance_audit_events",
        sa.Column("event_id", sa.String(), primary_key=True),
        sa.Column("task_id", sa.String(), sa.ForeignKey("tasks.task_id"), nullable=False),
        sa.Column("step_id", sa.String(), sa.ForeignKey("steps.step_id"), nullable=True),
        sa.Column("contract_id", sa.String(), sa.ForeignKey("task_contracts.contract_id"), nullable=True),
        sa.Column("organization_id", sa.String(), sa.ForeignKey("organizations.organization_id"), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("mode", sa.String(), nullable=False),
        sa.Column("action_fingerprint", sa.String(), nullable=True),
        sa.Column("observation_hash", sa.String(), nullable=True),
        sa.Column("policy_version", sa.String(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("mode IN ('off', 'audit', 'enforce')", name="ck_governance_event_mode"),
    )
    op.create_index("idx_gae_task_time", "governance_audit_events", ["task_id", "created_at"])
    op.create_index("idx_gae_action_fingerprint", "governance_audit_events", ["action_fingerprint"])

    op.create_table(
        "pending_actions",
        sa.Column("pending_action_id", sa.String(), primary_key=True),
        sa.Column("task_id", sa.String(), sa.ForeignKey("tasks.task_id"), nullable=False),
        sa.Column("step_id", sa.String(), sa.ForeignKey("steps.step_id"), nullable=False),
        sa.Column("contract_id", sa.String(), sa.ForeignKey("task_contracts.contract_id"), nullable=False),
        sa.Column("organization_id", sa.String(), sa.ForeignKey("organizations.organization_id"), nullable=False),
        sa.Column("action_fingerprint", sa.String(), nullable=False),
        sa.Column("observation_hash", sa.String(), nullable=False),
        sa.Column("action_payload", sa.JSON(), nullable=False),
        sa.Column("intent_payload", sa.JSON(), nullable=False),
        sa.Column("decision_payload", sa.JSON(), nullable=False),
        sa.Column("approval_id", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("modified_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("approval_id", name="uq_pending_action_approval"),
        sa.CheckConstraint("status IN ('pending', 'approved', 'rejected', 'expired', 'invalidated')", name="ck_pending_action_status"),
    )
    op.create_index("idx_pa_org_status", "pending_actions", ["organization_id", "status"])
    op.create_index("idx_pa_task_step", "pending_actions", ["task_id", "step_id"])

    op.create_table(
        "execution_attempts",
        sa.Column("attempt_id", sa.String(), primary_key=True),
        sa.Column("task_id", sa.String(), sa.ForeignKey("tasks.task_id"), nullable=False),
        sa.Column("step_id", sa.String(), sa.ForeignKey("steps.step_id"), nullable=False),
        sa.Column("contract_id", sa.String(), sa.ForeignKey("task_contracts.contract_id"), nullable=False),
        sa.Column("pending_action_id", sa.String(), sa.ForeignKey("pending_actions.pending_action_id"), nullable=True),
        sa.Column("action_fingerprint", sa.String(), nullable=False),
        sa.Column("observation_hash", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("idempotency_key", sa.String(), nullable=True),
        sa.Column("result_probe", sa.JSON(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("modified_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("status IN ('authorized', 'executing', 'confirmed', 'unknown', 'failed')", name="ck_execution_attempt_status"),
        sa.UniqueConstraint("task_id", "idempotency_key", name="uq_execution_attempt_idempotency"),
    )
    op.create_index("idx_ea_task_status", "execution_attempts", ["task_id", "status"])


def downgrade() -> None:
    op.drop_index("idx_ea_task_status", table_name="execution_attempts")
    op.drop_table("execution_attempts")
    op.drop_index("idx_pa_task_step", table_name="pending_actions")
    op.drop_index("idx_pa_org_status", table_name="pending_actions")
    op.drop_table("pending_actions")
    op.drop_index("idx_gae_action_fingerprint", table_name="governance_audit_events")
    op.drop_index("idx_gae_task_time", table_name="governance_audit_events")
    op.drop_table("governance_audit_events")
    op.drop_index("idx_tc_org_mode", table_name="task_contracts")
    op.drop_table("task_contracts")
