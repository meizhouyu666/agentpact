"""Schema regression tests for approval-pause concurrency guarantees."""

from pathlib import Path

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex

from enterprise.governance.models import PendingActionModel


def test_pending_action_schema_allows_history_but_has_one_active_approval_per_step():
    active_index = next(
        index for index in PendingActionModel.__table__.indexes if index.name == "uq_pending_action_active_step"
    )

    ddl = str(CreateIndex(active_index).compile(dialect=postgresql.dialect()))

    assert active_index.unique is True
    assert "UNIQUE INDEX uq_pending_action_active_step" in ddl
    assert "WHERE status IN ('pending', 'approved')" in ddl
    assert all(constraint.name != "uq_pending_action_step_status" for constraint in PendingActionModel.__table__.constraints)


def test_pending_action_migration_creates_the_same_active_only_constraint():
    migration = (
        Path(__file__).parents[2]
        / "alembic"
        / "versions"
        / "2026_07_22_0006-phase2_pending_action_active_constraint.py"
    ).read_text(encoding="utf-8")

    assert 'down_revision = "ent_005"' in migration
    assert '"uq_pending_action_active_step"' in migration
    assert "postgresql_where=sa.text(\"status IN ('pending', 'approved')\")" in migration
    assert "DO $$" in migration
    assert "HAVING COUNT(*) > 1" in migration
    assert migration.index("DO $$") < migration.index("op.create_index")
