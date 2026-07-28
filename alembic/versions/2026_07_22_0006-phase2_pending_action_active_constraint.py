"""Enforce one active approval pause per task step.

Revision ID: ent_006
Revises: ent_005
Create Date: 2026-07-22
"""

from alembic import op
import sqlalchemy as sa

revision = "ent_006"
down_revision = "ent_005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Fail before creating the partial unique index. Existing approval history
    # is never changed by a schema migration; an operator must resolve any
    # duplicate active rounds through the documented incident procedure.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM pending_actions
                WHERE status IN ('pending', 'approved')
                GROUP BY task_id, step_id
                HAVING COUNT(*) > 1
            ) THEN
                RAISE EXCEPTION
                    'Cannot create uq_pending_action_active_step: duplicate active PendingActions require manual resolution';
            END IF;
        END
        $$;
        """
    )
    op.create_index(
        "uq_pending_action_active_step",
        "pending_actions",
        ["task_id", "step_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'approved')"),
    )


def downgrade() -> None:
    op.drop_index("uq_pending_action_active_step", table_name="pending_actions")
