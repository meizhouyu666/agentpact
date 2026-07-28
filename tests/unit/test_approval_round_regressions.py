"""Regression coverage for repeated and competing approval pauses."""

import asyncio

import pytest

from enterprise.approval.models import ApprovalRequestModel
from enterprise.approval.persistence import decide_approval_request
from enterprise.approval.routing import ApprovalRoute
from enterprise.governance.approval_orchestrator import create_approval_pause
from enterprise.governance.approval_pause_service import begin_reobservation_after_approval
from enterprise.governance.contracts import ActionIntent, DecisionOutcome, ExecutionEffect, PolicyDecision
from enterprise.governance.models import PendingActionModel
from enterprise.governance.pending_action_service import PendingActionError
from skyvern.forge.sdk.db.models import StepModel, TaskModel


class _Result:
    def __init__(self, value):
        self.value = value

    def first(self):
        return self.value


class ApprovalLifecycleSession:
    """Small transactional double that preserves approval-round state."""

    def __init__(self):
        self.task = TaskModel(task_id="task_1", organization_id="org_1", status="running")
        self.step = StepModel(step_id="step_1", task_id="task_1", organization_id="org_1", status="running")
        self.pending_actions: list[PendingActionModel] = []
        self.approvals: list[ApprovalRequestModel] = []

    def add(self, model):
        if isinstance(model, PendingActionModel):
            self.pending_actions.append(model)
        elif isinstance(model, ApprovalRequestModel):
            self.approvals.append(model)

    async def flush(self):
        for number, pending in enumerate(self.pending_actions, start=1):
            if pending.pending_action_id is None:
                pending.pending_action_id = f"pending_{number}"
        for number, approval in enumerate(self.approvals, start=1):
            if approval.approval_id is None:
                approval.approval_id = f"apr_{number}"

    async def scalars(self, statement):
        entity = statement.column_descriptions[0]["entity"]
        if entity is TaskModel:
            return _Result(self.task)
        if entity is StepModel:
            return _Result(self.step)

        parameters = statement.compile().params
        if entity is PendingActionModel:
            pending_action_id = _parameter(parameters, "pending_action_id")
            if pending_action_id is not None:
                return _Result(_by_id(self.pending_actions, "pending_action_id", pending_action_id))
            approval_id = _parameter(parameters, "approval_id")
            if approval_id is not None:
                return _Result(_by_id(self.pending_actions, "approval_id", approval_id))
            return _Result(
                next(
                    (
                        pending
                        for pending in self.pending_actions
                        if pending.status in {"pending", "approved"}
                    ),
                    None,
                )
            )
        if entity is ApprovalRequestModel:
            approval_id = _parameter(parameters, "approval_id")
            return _Result(_by_id(self.approvals, "approval_id", approval_id))
        raise AssertionError(f"Unexpected entity query: {entity}")


def _parameter(parameters, prefix):
    return next((value for key, value in parameters.items() if key.startswith(prefix)), None)


def _by_id(models, attribute, expected):
    return next((model for model in models if getattr(model, attribute) == expected), None)


class FakeAction:
    def model_dump(self, **_kwargs):
        return {"action_type": "click", "element_id": "element_1", "text": "Transfer"}


def _intent(*, observation_id: str) -> ActionIntent:
    return ActionIntent(
        intent_id=f"intent_{observation_id}",
        task_id="task_1",
        step_id="step_1",
        action_fingerprint="action_fp",
        observation_id=observation_id,
        operation="controlled_operation",
        effect=ExecutionEffect.EXTERNAL_WRITE,
    )


def _decision(*, observation_id: str) -> PolicyDecision:
    return PolicyDecision(
        decision_id=f"decision_{observation_id}",
        intent_id=f"intent_{observation_id}",
        outcome=DecisionOutcome.REQUIRE_APPROVAL,
        risk_level="critical",
        reasons=["approval required"],
        policy_version="phase2-v1",
    )


def _pause(session, *, observation_id: str):
    return create_approval_pause(
        db_session=session,
        task_id="task_1",
        step_id="step_1",
        organization_id="org_1",
        contract_id="contract_1",
        source_department_id="dept_1",
        action=FakeAction(),
        intent=_intent(observation_id=observation_id),
        observation_hash=f"hash_{observation_id}",
        decision=_decision(observation_id=observation_id),
        route=ApprovalRoute(requires_approval=True, approver_department_id="dept_approver"),
        requester_user_id="requester_1",
    )


def test_approval_reobservation_then_fresh_approval_round_uses_a_new_pending_action():
    session = ApprovalLifecycleSession()
    first_pause = asyncio.run(_pause(session, observation_id="first"))
    first_approval_id = first_pause.pending_action.approval_id

    asyncio.run(
        decide_approval_request(
            db_session=session,
            approval_id=first_approval_id,
            organization_id="org_1",
            approver_user_id="approver_1",
            approved=True,
            decision_note="approved",
        )
    )
    asyncio.run(
        begin_reobservation_after_approval(
            db_session=session,
            task_id="task_1",
            step_id="step_1",
            organization_id="org_1",
            pending_action_id=first_pause.pending_action.pending_action_id,
            expected_row_version=session.pending_actions[0].row_version,
        )
    )

    # A recovered Agent starts a fresh perception pass before the next pause.
    session.task.status = "running"
    session.step.status = "running"
    second_pause = asyncio.run(_pause(session, observation_id="second"))

    assert [pending.status for pending in session.pending_actions] == ["invalidated", "pending"]
    assert first_pause.pending_action.pending_action_id != second_pause.pending_action.pending_action_id
    assert first_approval_id != second_pause.pending_action.approval_id


def test_competing_pause_request_is_rejected_without_creating_another_approval():
    session = ApprovalLifecycleSession()
    asyncio.run(_pause(session, observation_id="first"))

    with pytest.raises(PendingActionError, match="already has a pending approval"):
        asyncio.run(_pause(session, observation_id="competing"))

    assert len(session.pending_actions) == 1
    assert len(session.approvals) == 1
