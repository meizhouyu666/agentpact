"""Tests for durable discovery of approved approval pauses."""

import asyncio
from datetime import datetime, timedelta, timezone

from enterprise.governance.approval_recovery_service import prepare_approved_pauses_for_reobservation
from enterprise.governance.models import PendingActionModel
from skyvern.forge.sdk.db.models import StepModel, TaskModel


class _Result:
    def __init__(self, value):
        self.value = value

    def first(self):
        return self.value

    def all(self):
        return self.value if isinstance(self.value, list) else [self.value]


class FakeSession:
    def __init__(self):
        self.task = TaskModel(task_id="task_1", organization_id="org_1", status="pending_approval")
        self.step = StepModel(step_id="step_1", task_id="task_1", organization_id="org_1", status="pending_approval")
        self.pending = PendingActionModel(
            pending_action_id="pending_1",
            task_id="task_1",
            step_id="step_1",
            contract_id="contract_1",
            organization_id="org_1",
            action_fingerprint="action_fp",
            observation_hash="obs_hash",
            action_payload={},
            intent_payload={},
            decision_payload={},
            approval_id="apr_1",
            status="approved",
            row_version=2,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        self.pending_queries = 0

    async def scalars(self, statement):
        entity = statement.column_descriptions[0]["entity"]
        if entity is PendingActionModel:
            self.pending_queries += 1
            return _Result([self.pending] if self.pending_queries == 1 else self.pending)
        if entity is TaskModel:
            return _Result(self.task)
        if entity is StepModel:
            return _Result(self.step)
        raise AssertionError(f"Unexpected entity query: {entity}")

    async def flush(self):
        pass


def test_recovery_marks_approved_pause_resuming_and_invalidates_old_action():
    session = FakeSession()
    resumptions = asyncio.run(prepare_approved_pauses_for_reobservation(db_session=session, organization_id="org_1"))

    assert len(resumptions) == 1
    assert resumptions[0].task_status.value == "resuming"
    assert session.task.status == "resuming"
    assert session.step.status == "resuming"
    assert session.pending.status == "invalidated"


def test_recovery_rejects_invalid_scan_limit():
    try:
        asyncio.run(prepare_approved_pauses_for_reobservation(db_session=FakeSession(), limit=0))
    except ValueError as exc:
        assert "limit" in str(exc)
    else:
        raise AssertionError("Expected invalid limit to raise")
