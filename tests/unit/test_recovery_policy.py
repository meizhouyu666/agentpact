"""Task 3 recovery policy tests."""

from enterprise.governance.contracts import ExecutionAttemptStatus
from enterprise.governance.recovery import (
    ExecutionFailureClass,
    ExecutionFailureEvent,
    RecoveryLevel,
    decide_recovery,
)


def _event(failure_class, **kwargs):
    return ExecutionFailureEvent(task_id="task_1", step_id="step_1", failure_class=failure_class, **kwargs)


def test_technical_failure_stays_at_l0_and_never_requests_business_replan():
    decision = decide_recovery(_event(ExecutionFailureClass.TECHNICAL_TRANSIENT))

    assert decision.level is RecoveryLevel.L0
    assert decision.action == "retry_same_action"
    assert not decision.requires_reauthorization


def test_unknown_never_retries_and_requires_result_probe():
    decision = decide_recovery(
        _event(ExecutionFailureClass.TECHNICAL_TRANSIENT, attempt_status=ExecutionAttemptStatus.UNKNOWN)
    )

    assert decision.level is RecoveryLevel.L4
    assert decision.max_attempts == 0
    assert decision.requires_result_probe


def test_l3_is_limited_to_existing_contract_scope():
    within_scope = decide_recovery(_event(ExecutionFailureClass.BUSINESS_STATE_MISMATCH))
    expanded_scope = decide_recovery(
        _event(ExecutionFailureClass.BUSINESS_STATE_MISMATCH, contract_scope_unchanged=False)
    )

    assert within_scope.level is RecoveryLevel.L3
    assert not within_scope.requires_reauthorization
    assert expanded_scope.level is RecoveryLevel.L4
    assert expanded_scope.requires_reauthorization
