"""Tests for atomic native task/step approval-pause state transitions."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from enterprise.governance.approval_pause_service import (
    ApprovalPauseError,
    begin_reobservation_after_approval,
    pause_for_approval,
)
from enterprise.governance.contracts import PendingAction, PendingActionStatus
from enterprise.governance.models import PendingActionModel
from skyvern.forge.sdk.db.models import StepModel, TaskModel
from skyvern.forge.sdk.models import StepStatus
from skyvern.forge.sdk.schemas.tasks import TaskStatus


class _Result:
    def __init__(self, value):
        self.value = value

    def first(self):
        return self.value


class FakeSession:
    def __init__(self, task, step, pending):
        self.task = task
        self.step = step
        self.pending = pending
        self.flush_count = 0

    async def scalars(self, statement):
        entity = statement.column_descriptions[0]["entity"]
        if entity is TaskModel:
            return _Result(self.task)
        if entity is StepModel:
            return _Result(self.step)
        if entity is PendingActionModel:
            return _Result(self.pending)
        raise AssertionError(f"Unexpected entity query: {entity}")

    async def flush(self):
        self.flush_count += 1


def _pending_contract(status=PendingActionStatus.PENDING, row_version=1):
    return PendingAction(
        pending_action_id="pending_1",
        task_id="task_1",
        step_id="step_1",
        contract_id="contract_1",
        organization_id="org_1",
        action_fingerprint="action_fp",
        observation_hash="obs_hash",
        status=status,
        approval_id="apr_1",
        row_version=row_version,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )


def _session(task_status="running", step_status="running", pending_status="approved", row_version=2):
    task = TaskModel(task_id="task_1", organization_id="org_1", status=task_status)
    step = StepModel(step_id="step_1", task_id="task_1", organization_id="org_1", status=step_status)
    pending = PendingActionModel(
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
        status=pending_status,
        row_version=row_version,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    return FakeSession(task, step, pending)


def test_pause_moves_task_and_step_together():
    session = _session(pending_status="pending", row_version=1)
    state = asyncio.run(
        pause_for_approval(
            db_session=session,
            task_id="task_1",
            step_id="step_1",
            organization_id="org_1",
            pending_action=_pending_contract(),
        )
    )

    assert state.task_status == TaskStatus.pending_approval
    assert state.step_status == StepStatus.pending_approval
    assert session.task.status == "pending_approval"
    assert session.step.status == "pending_approval"


def test_approved_pause_moves_to_resuming_and_invalidates_old_action():
    session = _session(task_status="pending_approval", step_status="pending_approval")
    state = asyncio.run(
        begin_reobservation_after_approval(
            db_session=session,
            task_id="task_1",
            step_id="step_1",
            organization_id="org_1",
            pending_action_id="pending_1",
            expected_row_version=2,
        )
    )

    assert state.task_status == TaskStatus.resuming
    assert state.step_status == StepStatus.resuming
    assert state.pending_action.status == PendingActionStatus.INVALIDATED
    assert session.task.status == "resuming"
    assert session.step.status == "resuming"


def test_pause_rejects_non_running_native_state():
    session = _session(task_status="completed", pending_status="pending", row_version=1)

    with pytest.raises(ApprovalPauseError, match="Only running"):
        asyncio.run(
            pause_for_approval(
                db_session=session,
                task_id="task_1",
                step_id="step_1",
                organization_id="org_1",
                pending_action=_pending_contract(),
        )
    )


def test_pause_rejects_an_invalidated_action_from_a_prior_approval_round():
    session = _session(pending_status="invalidated", row_version=4)

    with pytest.raises(ApprovalPauseError, match="pause-compatible"):
        asyncio.run(
            pause_for_approval(
                db_session=session,
                task_id="task_1",
                step_id="step_1",
                organization_id="org_1",
                pending_action=_pending_contract(status=PendingActionStatus.INVALIDATED, row_version=4),
            )
        )
