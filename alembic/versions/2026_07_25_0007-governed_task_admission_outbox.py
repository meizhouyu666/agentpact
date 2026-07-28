"""Add audit-only governed Task admission aggregate and outbox.

Revision ID: ent_007
Revises: ent_006
Create Date: 2026-07-25
"""

import sqlalchemy as sa

from alembic import op

revision = "ent_007"
down_revision = "ent_006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "governed_task_admissions",
        sa.Column("admission_id", sa.String(), primary_key=True),
        sa.Column("organization_id", sa.String(), nullable=False),
        sa.Column("request_id", sa.String(), nullable=False),
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("contract_id", sa.String(), nullable=False),
        sa.Column("bundle_schema_version", sa.String(), nullable=False),
        sa.Column("admission_fingerprint", sa.String(), nullable=False),
        sa.Column("bundle_fingerprint", sa.String(), nullable=False),
        sa.Column("bundle_payload", sa.JSON(), nullable=False),
        sa.Column("mode", sa.String(), nullable=False),
        sa.Column("committed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("mode = 'audit'", name="ck_governed_task_admission_audit_only"),
        sa.UniqueConstraint("organization_id", "request_id", name="uq_gta_org_request"),
        sa.UniqueConstraint("organization_id", "task_id", name="uq_gta_org_task"),
    )
    op.create_index(
        "ix_governed_task_admissions_organization_id",
        "governed_task_admissions",
        ["organization_id"],
    )
    op.create_index(
        "idx_gta_org_committed",
        "governed_task_admissions",
        ["organization_id", "committed_at"],
    )

    op.create_table(
        "governance_outbox",
        sa.Column("outbox_id", sa.String(), primary_key=True),
        sa.Column(
            "admission_id",
            sa.String(),
            sa.ForeignKey("governed_task_admissions.admission_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("organization_id", sa.String(), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'published')",
            name="ck_governance_outbox_status",
        ),
        sa.CheckConstraint("attempt_count >= 0", name="ck_governance_outbox_attempt_count"),
        sa.UniqueConstraint(
            "admission_id",
            "event_type",
            name="uq_governance_outbox_admission_event",
        ),
    )
    op.create_index(
        "ix_governance_outbox_organization_id",
        "governance_outbox",
        ["organization_id"],
    )
    op.create_index(
        "idx_governance_outbox_pending",
        "governance_outbox",
        ["status", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_governance_outbox_pending", table_name="governance_outbox")
    op.drop_index("ix_governance_outbox_organization_id", table_name="governance_outbox")
    op.drop_table("governance_outbox")
    op.drop_index("idx_gta_org_committed", table_name="governed_task_admissions")
    op.drop_index("ix_governed_task_admissions_organization_id", table_name="governed_task_admissions")
    op.drop_table("governed_task_admissions")
