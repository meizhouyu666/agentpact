"""Create persistent approval requests for Phase 2 pause and recovery.

Revision ID: ent_004
Revises: ent_003
Create Date: 2026-07-20
"""

from alembic import op
import sqlalchemy as sa

revision = "ent_004"
down_revision = "ent_003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "approval_requests",
        sa.Column("approval_id", sa.String(), primary_key=True),
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("organization_id", sa.String(), sa.ForeignKey("organizations.organization_id"), nullable=False),
        sa.Column("department_id", sa.String(), sa.ForeignKey("departments.department_id"), nullable=False),
        sa.Column("business_line_id", sa.String(), sa.ForeignKey("business_lines.business_line_id"), nullable=True),
        sa.Column("risk_level", sa.String(), nullable=False),
        sa.Column("risk_reason", sa.Text(), nullable=False),
        sa.Column("operation_description", sa.Text(), nullable=True),
        sa.Column("screenshot_path", sa.String(), nullable=True),
        sa.Column("approver_department_id", sa.String(), nullable=False),
        sa.Column("approver_role", sa.String(), nullable=False),
        sa.Column("notify_department_ids", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("requested_at", sa.DateTime(), nullable=False),
        sa.Column("approver_user_id", sa.String(), nullable=True),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False),
        sa.CheckConstraint("status IN ('pending', 'approved', 'rejected', 'timeout')", name="ck_valid_approval_status"),
        sa.CheckConstraint("timeout_seconds > 0", name="ck_positive_timeout"),
    )
    op.create_index("idx_apr_org_status", "approval_requests", ["organization_id", "status"])
    op.create_index("idx_apr_dept_status", "approval_requests", ["approver_department_id", "status"])
    op.create_index("ix_approval_requests_task_id", "approval_requests", ["task_id"])
    op.create_index("ix_approval_requests_requested_at", "approval_requests", ["requested_at"])


def downgrade() -> None:
    op.drop_index("ix_approval_requests_requested_at", table_name="approval_requests")
    op.drop_index("ix_approval_requests_task_id", table_name="approval_requests")
    op.drop_index("idx_apr_dept_status", table_name="approval_requests")
    op.drop_index("idx_apr_org_status", table_name="approval_requests")
    op.drop_table("approval_requests")
