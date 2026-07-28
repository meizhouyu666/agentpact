"""Persist approval requester identity for separation of duties.

Revision ID: ent_005
Revises: ent_004
Create Date: 2026-07-20
"""

from alembic import op
import sqlalchemy as sa

revision = "ent_005"
down_revision = "ent_004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("approval_requests", sa.Column("requester_user_id", sa.String(), nullable=True))
    op.create_index("ix_approval_requests_requester_user_id", "approval_requests", ["requester_user_id"])


def downgrade() -> None:
    op.drop_index("ix_approval_requests_requester_user_id", table_name="approval_requests")
    op.drop_column("approval_requests", "requester_user_id")
