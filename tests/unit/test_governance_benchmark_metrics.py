"""Task 6 synthetic metric and fault-replay tests."""

from enterprise.governance.benchmark import BenchmarkRecord, replay_fault, summarize
from enterprise.governance.recovery import ExecutionFailureClass, ExecutionFailureEvent, RecoveryLevel


def test_synthetic_metrics_and_unknown_fault_replay_are_explicitly_safe():
    metrics = summarize([BenchmarkRecord(task_success=True, first_action_hit=True, incorrect_action=False, recovery_level="L0", unknown_stopped=False, fallback_used=False, audit_complete=True, latency_ms=10, model_cost=0.01), BenchmarkRecord(task_success=False, first_action_hit=False, incorrect_action=True, recovery_level="L4", unknown_stopped=True, fallback_used=True, audit_complete=True, latency_ms=20, model_cost=0.02)])
    assert metrics.task_success_rate == 0.5
    assert metrics.recovery_distribution == {"L0": 1, "L4": 1}
    decision = replay_fault(ExecutionFailureEvent(task_id="t", step_id="s", failure_class=ExecutionFailureClass.UNKNOWN))
    assert decision.level is RecoveryLevel.L4 and decision.max_attempts == 0
