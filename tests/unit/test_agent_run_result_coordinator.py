from __future__ import annotations

import hashlib

import pytest

from enterprise.agent_runs.coordinator import AgentRunResultCoordinator
from enterprise.agent_runs.journal import (
    GovernedPlanCheckpoint,
    GovernedPlanError,
    GovernedPlanStepRef,
    PlanJournalTransition,
    PlanRunState,
    PlanStepState,
)
from enterprise.governance.pack_runtime import (
    ExecutionCheckpoint,
    PackAdvanceResult,
    PackAdvanceStatus,
    PackProbeResult,
    PackProbeStatus,
)


def _checkpoint() -> GovernedPlanCheckpoint:
    return GovernedPlanCheckpoint(
        plan_run_id="run-coordinator",
        admission_id="admission-coordinator",
        root_task_id="run-coordinator",
        plan_id="plan-coordinator",
        plan_version=1,
        authority_contract_id="contract-coordinator",
        active_step=GovernedPlanStepRef(
            business_plan_step_id="business-step",
            step_digest="a" * 64,
            work_order_id="work-order",
            work_order_digest="b" * 64,
            native_task_id="native-task",
            native_step_id="native-step",
            native_contract_id="native-contract",
            authority_contract_id="contract-coordinator",
            state=PlanStepState.ACTIVE,
        ),
    )


def _checkpoint_with_suffix() -> GovernedPlanCheckpoint:
    first = _checkpoint()
    return first.model_copy(
        update={
            "remaining_suffix": (
                GovernedPlanStepRef(
                    business_plan_step_id="business-step-2",
                    step_digest="e" * 64,
                    work_order_id="work-order-2",
                    work_order_digest="f" * 64,
                    native_task_id="native-task-2",
                    native_step_id="native-step-2",
                    native_contract_id="native-contract-2",
                    authority_contract_id="contract-coordinator",
                    state=PlanStepState.PENDING,
                ),
            )
        }
    )


def _execution() -> ExecutionCheckpoint:
    return ExecutionCheckpoint(
        permit_id="permit-1",
        attempt_id="attempt-1",
        task_id="native-task",
        step_id="native-step",
        action_fingerprint="c" * 64,
        observation_hash="d" * 64,
        idempotency_key_digest=hashlib.sha256(b"key").hexdigest(),
        execution_effect="external_write",
        result_probe_ref="stripe.payment.submit.result-probe.v1",
        attempt_status="unknown",
    )


def test_pending_advance_becomes_probe_blocked_with_exact_checkpoint():
    updated, transition = AgentRunResultCoordinator.advance(
        _checkpoint(),
        PackAdvanceResult(
            status=PackAdvanceStatus.PENDING_RESULT_PROBE,
            run_id="run-coordinator",
            step_id="native-step",
            reason_code="RESULT_UNCERTAIN",
            execution_checkpoint=_execution(),
        ),
    ) or (None, None)
    assert transition is PlanJournalTransition.PROBE_BLOCKED
    assert updated.state is PlanRunState.PROBE_BLOCKED
    assert updated.active_step is not None
    assert updated.active_step.attempt_id == "attempt-1"


def test_completed_advance_and_confirmed_probe_complete_the_generic_plan():
    completed, transition = AgentRunResultCoordinator.advance(
        _checkpoint(),
        PackAdvanceResult(status=PackAdvanceStatus.COMPLETED, run_id="run-coordinator"),
    ) or (None, None)
    assert transition is PlanJournalTransition.PLAN_COMPLETED
    assert completed.state is PlanRunState.COMPLETED
    assert completed.active_step is None

    blocked = AgentRunResultCoordinator.advance(
        _checkpoint(),
        PackAdvanceResult(
            status=PackAdvanceStatus.PENDING_RESULT_PROBE,
            run_id="run-coordinator",
            step_id="native-step",
            reason_code="RESULT_UNCERTAIN",
            execution_checkpoint=_execution(),
        ),
    )[0]
    resolved, transition = AgentRunResultCoordinator.probe(
        blocked,
        PackProbeResult(
            status=PackProbeStatus.CONFIRMED,
            checkpoint=_execution(),
            reason_code="BUSINESS_RESULT_CONFIRMED",
        ),
    ) or (None, None)
    assert transition is PlanJournalTransition.PLAN_COMPLETED
    assert resolved.state is PlanRunState.COMPLETED


def test_confirmed_probe_advances_the_remaining_suffix():
    blocked = AgentRunResultCoordinator.advance(
        _checkpoint_with_suffix(),
        PackAdvanceResult(
            status=PackAdvanceStatus.PENDING_RESULT_PROBE,
            run_id="run-coordinator",
            step_id="native-step",
            reason_code="RESULT_UNCERTAIN",
            execution_checkpoint=_execution(),
        ),
    )[0]
    resolved, transition = AgentRunResultCoordinator.probe(
        blocked,
        PackProbeResult(status=PackProbeStatus.CONFIRMED, checkpoint=_execution(), reason_code="CONFIRMED"),
    ) or (None, None)
    assert transition is PlanJournalTransition.PROBE_RESOLVED
    assert resolved.state is PlanRunState.ACTIVE
    assert resolved.active_step is not None
    assert resolved.active_step.native_task_id == "native-task-2"
    assert len(resolved.completed_prefix) == 1


def test_coordinator_rejects_substituted_execution_checkpoint():
    execution = _execution().model_copy(update={"task_id": "native-other"})
    with pytest.raises(GovernedPlanError, match="does not match"):
        AgentRunResultCoordinator.advance(
            _checkpoint(),
            PackAdvanceResult(
                status=PackAdvanceStatus.PENDING_RESULT_PROBE,
                run_id="run-coordinator",
                step_id="native-step",
                reason_code="RESULT_UNCERTAIN",
                execution_checkpoint=execution,
            ),
        )
