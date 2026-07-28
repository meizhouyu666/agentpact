"""Tests for atomic construction of an approval pause."""

import asyncio
from datetime import datetime, timedelta, timezone

from enterprise.approval.models import ApprovalRequestModel
from enterprise.approval.routing import ApprovalRoute
from enterprise.governance.approval_orchestrator import create_approval_pause
from enterprise.governance.contracts import ActionIntent, DecisionOutcome, ExecutionEffect, PolicyDecision
from enterprise.governance.models import PendingActionModel
from skyvern.forge.sdk.db.models import StepModel, TaskModel


class _Result:
    def __init__(self, value):
        self.value = value

    def first(self):
        return self.value


class FakeSession:
    def __init__(self):
        self.task = TaskModel(task_id="task_1", organization_id="org_1", status="running")
        self.step = StepModel(step_id="step_1", task_id="task_1", organization_id="org_1", status="running")
        self.pending = None
        self.approval = None

    def add(self, model):
        if isinstance(model, PendingActionModel):
            self.pending = model
        if isinstance(model, ApprovalRequestModel):
            self.approval = model

    async def flush(self):
        if self.pending is not None and self.pending.pending_action_id is None:
            self.pending.pending_action_id = "pending_1"
        if self.approval is not None and self.approval.approval_id is None:
            self.approval.approval_id = "apr_1"

    async def scalars(self, statement):
        entity = statement.column_descriptions[0]["entity"]
        values = {
            PendingActionModel: self.pending,
            TaskModel: self.task,
            StepModel: self.step,
        }
        return _Result(values[entity])


class FakeAction:
    def model_dump(self, **_kwargs):
        return {"action_type": "click", "element_id": "element_1", "text": "Transfer 100"}


def test_orchestrator_creates_linked_records_and_pauses_native_state():
    session = FakeSession()
    intent = ActionIntent(
        intent_id="intent_1",
        task_id="task_1",
        step_id="step_1",
        action_fingerprint="action_fp",
        observation_id="obs_1",
        operation="payment",
        effect=ExecutionEffect.EXTERNAL_WRITE,
    )
    decision = PolicyDecision(
        decision_id="decision_1",
        intent_id="intent_1",
        outcome=DecisionOutcome.REQUIRE_APPROVAL,
        risk_level="critical",
        reasons=["payment requires approval"],
        policy_version="phase2-v1",
    )

    state = asyncio.run(
        create_approval_pause(
            db_session=session,
            task_id="task_1",
            step_id="step_1",
            organization_id="org_1",
            contract_id="contract_1",
            source_department_id="dept_1",
            action=FakeAction(),
            intent=intent,
            observation_hash="obs_hash",
            decision=decision,
            route=ApprovalRoute(requires_approval=True, approver_department_id="dept_approver"),
        )
    )

    assert session.pending.approval_id == session.approval.approval_id
    assert session.pending.status == "pending"
    assert session.task.status == "pending_approval"
    assert session.step.status == "pending_approval"
    assert state.pending_action.approval_id == session.approval.approval_id
