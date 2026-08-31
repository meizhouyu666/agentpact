"""Persist exact external-write execution checkpoint identity.

Revision ID: ent_008
Revises: ent_007
Create Date: 2026-08-31
"""

import sqlalchemy as sa

from alembic import op

revision = "ent_008"
down_revision = "ent_007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("execution_attempts", sa.Column("permit_id", sa.String(), nullable=True))
    op.add_column("execution_attempts", sa.Column("idempotency_key_digest", sa.String(), nullable=True))
    op.add_column("execution_attempts", sa.Column("execution_effect", sa.String(), nullable=True))
    op.add_column("execution_attempts", sa.Column("result_probe_ref", sa.String(), nullable=True))
    op.create_foreign_key(
        "fk_execution_attempt_permit",
        "execution_attempts",
        "execution_permits",
        ["permit_id"],
        ["permit_id"],
    )
    op.create_unique_constraint("uq_execution_attempt_permit", "execution_attempts", ["permit_id"])
    op.create_check_constraint(
        "ck_execution_attempt_effect",
        "execution_attempts",
        "execution_effect IS NULL OR execution_effect IN ('none', 'read', 'internal_write', 'external_write')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_execution_attempt_effect", "execution_attempts", type_="check")
    op.drop_constraint("uq_execution_attempt_permit", "execution_attempts", type_="unique")
    op.drop_constraint("fk_execution_attempt_permit", "execution_attempts", type_="foreignkey")
    op.drop_column("execution_attempts", "result_probe_ref")
    op.drop_column("execution_attempts", "execution_effect")
    op.drop_column("execution_attempts", "idempotency_key_digest")
    op.drop_column("execution_attempts", "permit_id")
