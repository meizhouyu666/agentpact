"""Capability-bound planning contracts for the future Skyvern Work Order adapter.

These models define business orchestration only.  They do not import or invoke
ForgeAgent, ActionHandler, Playwright, or any browser-facing code.
"""

from __future__ import annotations

import json
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from enterprise.governance.capabilities import CapabilityDataScope, CapabilityGrantSet


class RecoveryLevel(StrEnum):
    L0 = "L0"
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"
    L4 = "L4"


class ReplanReason(StrEnum):
    BUSINESS_STATE_CHANGED = "business_state_changed"
    INPUT_CLARIFICATION = "input_clarification"
    POLICY_OR_PERMISSION_CHANGED = "policy_or_permission_changed"
    EXECUTION_FAILURE = "execution_failure"


class BusinessPlanStep(BaseModel):
    """A capability-bound business state transition, never a browser action."""

    model_config = ConfigDict(extra="forbid")

    step_id: str = Field(default_factory=lambda: f"bpstep_{uuid4().hex}")
    capability_id: str
    capability_version: str | None = None
    grant_id: str
    contract_id: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    expected_transition: dict[str, Any] = Field(default_factory=dict)
    success_criteria: list[str] = Field(default_factory=list)


class BusinessPlan(BaseModel):
    """A versioned plan whose steps can reference only executable grants."""

    model_config = ConfigDict(extra="forbid")

    plan_id: str = Field(default_factory=lambda: f"businessplan_{uuid4().hex}")
    request_id: str | None = None
    task_id: str
    contract_id: str
    data_scope: CapabilityDataScope
    version: int = Field(default=1, ge=1)
    replan_reason: ReplanReason | None = None
    steps: list[BusinessPlanStep] = Field(default_factory=list)


class ExecutionWorkOrder(BaseModel):
    """Bounded page-execution request for the future single Skyvern executor."""

    work_order_id: str = Field(default_factory=lambda: f"workorder_{uuid4().hex}")
    business_plan_step_id: str
    task_id: str
    contract_id: str
    grant_id: str
    navigation_goal: str
    allowed_operations: set[str] = Field(default_factory=set)
    prohibited_operations: set[str] = Field(default_factory=set)
    success_criteria: list[str] = Field(default_factory=list)
    required_evidence: list[str] = Field(default_factory=list)
    max_recovery_level: RecoveryLevel
    result_probe_ref: str

    def validate_boundaries(self) -> None:
        overlap = self.allowed_operations & self.prohibited_operations
        if overlap:
            raise ValueError(f"Work Order operations cannot be both allowed and prohibited: {sorted(overlap)}")
        if not self.result_probe_ref:
            raise ValueError("ExecutionWorkOrder requires a result_probe_ref")


class ReplanAssessment(BaseModel):
    """Conservative result of comparing a proposed plan with its predecessor."""

    requires_reauthorization: bool
    invalidated_contract_ids: set[str] = Field(default_factory=set)
    invalidated_grant_ids: set[str] = Field(default_factory=set)
    reasons: list[str] = Field(default_factory=list)


class SkyvernWorkOrderAdapter(Protocol):
    """Adapter boundary; implementation may prepare, never directly execute, a Work Order."""

    async def prepare(self, work_order: ExecutionWorkOrder) -> None:
        """Hand a validated Work Order to a future Skyvern integration point."""


def validate_business_plan(plan: BusinessPlan, grants: CapabilityGrantSet, *, now: datetime) -> None:
    """Reject any plan reference outside the deterministic executable-grant set."""

    for step in plan.steps:
        if step.contract_id != plan.contract_id:
            raise ValueError("BusinessPlanStep contract_id must match the BusinessPlan contract_id")
        grants.require_executable(capability_id=step.capability_id, grant_id=step.grant_id, now=now)


def validate_work_order(
    work_order: ExecutionWorkOrder,
    plan: BusinessPlan,
    step: BusinessPlanStep,
    grants: CapabilityGrantSet,
    *,
    now: datetime,
) -> None:
    """Ensure a Work Order remains within its originating plan, step, contract, and grant."""

    if work_order.task_id != plan.task_id:
        raise ValueError("Work Order task_id must match the originating BusinessPlan task_id")
    if work_order.business_plan_step_id != step.step_id:
        raise ValueError("Work Order must reference its originating BusinessPlanStep")
    if step not in plan.steps:
        raise ValueError("Work Order step must belong to the originating BusinessPlan")
    if work_order.contract_id != plan.contract_id:
        raise ValueError("Work Order contract_id must match the originating BusinessPlan contract_id")
    if work_order.contract_id != step.contract_id:
        raise ValueError("Work Order contract_id must match the BusinessPlanStep contract_id")
    if work_order.grant_id != step.grant_id:
        raise ValueError("Work Order grant_id must match the BusinessPlanStep grant_id")
    grants.require_executable(capability_id=step.capability_id, grant_id=step.grant_id, now=now)
    work_order.validate_boundaries()


def assess_replan(previous: BusinessPlan, proposed: BusinessPlan) -> ReplanAssessment:
    """Invalidate old grants/contracts when a replan broadens capability or data scope."""

    reasons: list[str] = []
    contract_ids: set[str] = set()
    grant_ids: set[str] = set()
    if previous.contract_id != proposed.contract_id:
        reasons.append("Task contract changed")
        contract_ids.add(previous.contract_id)

    if _scope_expands(previous.data_scope, proposed.data_scope):
        reasons.append("Data scope expanded or changed")
        contract_ids.add(previous.contract_id)

    previous_capabilities = {step.capability_id for step in previous.steps}
    proposed_capabilities = {step.capability_id for step in proposed.steps}
    if not proposed_capabilities.issubset(previous_capabilities):
        reasons.append("Proposed plan references a new capability")
        contract_ids.add(previous.contract_id)

    if _business_inputs_changed(previous.steps, proposed.steps):
        reasons.append("Business inputs or expected transition changed")
        contract_ids.add(previous.contract_id)

    if reasons:
        grant_ids.update(step.grant_id for step in previous.steps)

    return ReplanAssessment(
        requires_reauthorization=bool(reasons),
        invalidated_contract_ids=contract_ids,
        invalidated_grant_ids=grant_ids,
        reasons=reasons,
    )


def _scope_expands(previous: CapabilityDataScope, proposed: CapabilityDataScope) -> bool:
    if previous.department_id != proposed.department_id:
        return True
    if previous.business_line_id != proposed.business_line_id:
        return True
    if not proposed.resource_ids.issubset(previous.resource_ids):
        return True
    return any(previous.attributes.get(key) != value for key, value in proposed.attributes.items())


def _business_inputs_changed(previous: list[BusinessPlanStep], proposed: list[BusinessPlanStep]) -> bool:
    """Compare canonical structured plan facts, not only capability and scope."""

    previous_by_step = {step.step_id: step for step in previous}
    if set(previous_by_step) != {step.step_id for step in proposed}:
        return True
    for proposed_step in proposed:
        previous_step = previous_by_step[proposed_step.step_id]
        if previous_step.capability_id != proposed_step.capability_id:
            return True
        if _canonical_json(previous_step.inputs) != _canonical_json(proposed_step.inputs):
            return True
        if _canonical_json(previous_step.expected_transition) != _canonical_json(proposed_step.expected_transition):
            return True
    return False


def _canonical_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
