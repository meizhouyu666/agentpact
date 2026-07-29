"""Task 2 planning and Work Order boundary tests."""

from datetime import datetime, timezone

import pytest

from enterprise.agent.work_orders import (
    BusinessPlan,
    BusinessPlanStep,
    ExecutionWorkOrder,
    RecoveryLevel,
    ReplanReason,
    assess_replan,
    assess_suffix_replan,
    validate_business_plan,
    validate_work_order,
)
from enterprise.auth.schemas import DepartmentRole, UserContext
from enterprise.governance.capabilities import (
    CapabilityDataScope,
    CapabilityDefinition,
    CapabilityRegistry,
    CapabilityResolutionContext,
    CapabilityResolver,
)


def _grants():
    definition = CapabilityDefinition(
        capability_id="records.update",
        version="1",
        domain="synthetic",
        display_name="Update record",
        access_policy_ref="synthetic.access.v1",
        risk_policy_ref="synthetic.risk.v1",
        work_order_template_ref="synthetic.work-order.v1",
        result_probe_ref="synthetic.result-probe.v1",
    )
    user = UserContext(
        user_id="user_1",
        org_id="org_1",
        department_roles=[DepartmentRole(department_id="dept_1", department_name="Operations", role="operator")],
        business_line_ids=["line_1"],
    )
    context = CapabilityResolutionContext(
        user=user,
        tenant_id="org_1",
        data_scope=CapabilityDataScope(department_id="dept_1", business_line_id="line_1", resource_ids={"r1"}),
        installed_capability_ids={"records.update"},
        policy_snapshot_version="policy-v1",
        resolved_at=datetime(2026, 7, 21, tzinfo=timezone.utc),
    )
    return CapabilityResolver(CapabilityRegistry([definition])).resolve(context)


def _plan(grant_id: str, *, scope: CapabilityDataScope | None = None) -> BusinessPlan:
    return BusinessPlan(
        task_id="task_1",
        contract_id="contract_1",
        data_scope=scope or CapabilityDataScope(department_id="dept_1", business_line_id="line_1", resource_ids={"r1"}),
        steps=[
            BusinessPlanStep(
                step_id="step_1",
                capability_id="records.update",
                grant_id=grant_id,
                contract_id="contract_1",
                expected_transition={"from": "draft", "to": "updated"},
                success_criteria=["synthetic result probe confirms update"],
            )
        ],
    )


def test_business_plan_requires_an_executable_grant():
    grants = _grants()
    plan = _plan(grants.grants[0].grant_id)

    now = grants.grants[0].resolved_at
    validate_business_plan(plan, grants, now=now)

    plan.steps[0].grant_id = "capgrant_missing"
    with pytest.raises(ValueError, match="not an executable"):
        validate_business_plan(plan, grants, now=now)


def test_work_order_binds_plan_step_contract_grant_and_execution_constraints():
    grants = _grants()
    plan = _plan(grants.grants[0].grant_id)
    step = plan.steps[0]
    work_order = ExecutionWorkOrder(
        business_plan_step_id=step.step_id,
        task_id="task_1",
        contract_id=step.contract_id,
        grant_id=step.grant_id,
        navigation_goal="Update the synthetic record within the declared capability",
        allowed_operations={"read", "input"},
        prohibited_operations={"submit"},
        success_criteria=step.success_criteria,
        required_evidence=["dom_target", "result_probe"],
        max_recovery_level=RecoveryLevel.L2,
        result_probe_ref="synthetic.result-probe.v1",
    )

    now = grants.grants[0].resolved_at
    validate_work_order(work_order, plan, step, grants, now=now)

    work_order.prohibited_operations.add("input")
    with pytest.raises(ValueError, match="both allowed and prohibited"):
        validate_work_order(work_order, plan, step, grants, now=now)

    work_order.prohibited_operations.remove("input")
    work_order.task_id = "task_other"
    with pytest.raises(ValueError, match="task_id"):
        validate_work_order(work_order, plan, step, grants, now=now)


def test_replan_scope_or_capability_expansion_invalidates_predecessor_authorization():
    grants = _grants()
    previous = _plan(grants.grants[0].grant_id)
    proposed = BusinessPlan(
        task_id=previous.task_id,
        contract_id=previous.contract_id,
        data_scope=CapabilityDataScope(department_id="dept_1", business_line_id="line_1", resource_ids={"r1", "r2"}),
        version=2,
        replan_reason=ReplanReason.BUSINESS_STATE_CHANGED,
        steps=previous.steps,
    )

    assessment = assess_replan(previous, proposed)

    assert assessment.requires_reauthorization
    assert assessment.invalidated_contract_ids == {"contract_1"}
    assert assessment.invalidated_grant_ids == {grants.grants[0].grant_id}


def test_replan_within_existing_scope_does_not_invalidate_authorization():
    grants = _grants()
    previous = _plan(grants.grants[0].grant_id)
    proposed = previous.model_copy(update={"version": 2, "replan_reason": ReplanReason.EXECUTION_FAILURE})

    assessment = assess_replan(previous, proposed)

    assert not assessment.requires_reauthorization


def test_replan_changing_business_inputs_or_expected_transition_requires_reauthorization():
    grants = _grants()
    previous = _plan(grants.grants[0].grant_id)
    proposed = previous.model_copy(deep=True)
    proposed.steps[0].inputs = {"amount": "200", "beneficiary": "vendor_2"}
    proposed.steps[0].expected_transition = {"from": "draft", "to": "submitted", "object_version": "v2"}

    assessment = assess_replan(previous, proposed)

    assert assessment.requires_reauthorization
    assert "Business inputs or expected transition changed" in assessment.reasons


def test_root_plan_can_bind_a_distinct_native_task_and_contract():
    grants = _grants()
    plan = _plan(grants.grants[0].grant_id)
    step = plan.steps[0]
    work_order = ExecutionWorkOrder(
        business_plan_step_id=step.step_id,
        task_id="native_task_1",
        contract_id="native_contract_1",
        plan_task_id=plan.task_id,
        authority_contract_id=plan.contract_id,
        grant_id=step.grant_id,
        navigation_goal="Execute one child under the root authority snapshot",
        allowed_operations={"read"},
        max_recovery_level=RecoveryLevel.L3,
        result_probe_ref="synthetic.result-probe.v1",
    )

    validate_work_order(work_order, plan, step, grants, now=grants.grants[0].resolved_at)
    with pytest.raises(ValueError, match="plan_task_id"):
        validate_work_order(
            work_order.model_copy(update={"plan_task_id": "root_other"}),
            plan,
            step,
            grants,
            now=grants.grants[0].resolved_at,
        )


def test_suffix_replan_preserves_prefix_and_allows_new_suffix_identity_only():
    grants = _grants()
    previous = _plan(grants.grants[0].grant_id)
    previous.steps.append(previous.steps[0].model_copy(update={"step_id": "step_2"}, deep=True))
    proposed = previous.model_copy(deep=True)
    proposed.version = 2
    proposed.replan_reason = ReplanReason.BUSINESS_STATE_CHANGED
    proposed.steps[1].step_id = "step_2_replanned"

    accepted = assess_suffix_replan(previous, proposed, completed_prefix_length=1)
    assert not accepted.requires_reauthorization
    assert accepted.accepted_suffix_step_ids == ("step_2_replanned",)

    proposed.steps[0].inputs = {"resource": "changed"}
    rejected = assess_suffix_replan(previous, proposed, completed_prefix_length=1)
    assert rejected.requires_reauthorization
    assert "Completed prefix changed" in rejected.reasons
