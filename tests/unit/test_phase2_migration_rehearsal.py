"""Static rehearsal of the additive Phase 2 migration chain; no database is touched."""

import re
from pathlib import Path

MIGRATIONS = (
    ("2026_07_18_0002-phase2_governance_baseline.py", "ent_002", "ent_001"),
    ("2026_07_19_0003-phase2_execution_permits.py", "ent_003", "ent_002"),
    ("2026_07_20_0004-phase2_persistent_approvals.py", "ent_004", "ent_003"),
    ("2026_07_20_0005-phase2_approval_requester.py", "ent_005", "ent_004"),
    ("2026_07_22_0006-phase2_pending_action_active_constraint.py", "ent_006", "ent_005"),
    ("2026_07_25_0007-governed_task_admission_outbox.py", "ent_007", "ent_006"),
)


def test_phase2_migrations_form_a_linear_additive_rehearsal_chain():
    directory = Path(__file__).parents[2] / "alembic" / "versions"

    for filename, revision, predecessor in MIGRATIONS:
        source = (directory / filename).read_text(encoding="utf-8")
        assert re.search(rf'^revision = "{revision}"$', source, flags=re.MULTILINE)
        assert re.search(rf'^down_revision = "{predecessor}"$', source, flags=re.MULTILINE)
        assert "def upgrade()" in source
        assert "def downgrade()" in source


def test_phase2_upgrade_sections_do_not_drop_existing_schema_objects():
    directory = Path(__file__).parents[2] / "alembic" / "versions"

    for filename, _revision, _predecessor in MIGRATIONS:
        source = (directory / filename).read_text(encoding="utf-8")
        upgrade_source = source.split("def upgrade()", maxsplit=1)[1].split("def downgrade()", maxsplit=1)[0]
        assert "op.drop_table" not in upgrade_source
        assert "op.drop_column" not in upgrade_source
        assert "op.drop_index" not in upgrade_source


def test_ent006_preflight_precedes_index_creation_and_never_repairs_rows():
    source = (
        Path(__file__).parents[2]
        / "alembic"
        / "versions"
        / "2026_07_22_0006-phase2_pending_action_active_constraint.py"
    ).read_text(encoding="utf-8")
    upgrade_source = source.split("def upgrade()", maxsplit=1)[1].split("def downgrade()", maxsplit=1)[0]

    assert upgrade_source.index("op.execute(") < upgrade_source.index("op.create_index(")
    assert "HAVING COUNT(*) > 1" in upgrade_source
    assert "RAISE EXCEPTION" in upgrade_source
    assert "UPDATE PENDING_ACTIONS" not in upgrade_source.upper()
    assert "DELETE FROM PENDING_ACTIONS" not in upgrade_source.upper()


def test_pending_action_runbook_preserves_manual_authority_and_rehearsal_gate():
    runbook = (
        Path(__file__).parents[2] / "docs" / "phase-2" / "pending-action-migration-runbook.md"
    ).read_text(encoding="utf-8")

    assert "authorized service owner decides" in runbook
    assert "Do not infer the decision from timestamps or action payloads" in runbook
    assert "retain the rows for audit history" in runbook
    assert "explicitly authorized deployment activity" in runbook
    assert "neither connects to a database nor runs Alembic" in runbook
