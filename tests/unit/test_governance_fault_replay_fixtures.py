"""Replay a static technical-failure corpus without browser side effects."""

import json
from pathlib import Path

import pytest

from enterprise.evaluation.benchmark import replay_fault
from enterprise.governance.contracts import ExecutionAttemptStatus
from enterprise.governance.recovery import ExecutionFailureClass, ExecutionFailureEvent

SCENARIOS = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "governance_fault_replay.json").read_text(encoding="utf-8")
)


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[scenario["id"] for scenario in SCENARIOS])
def test_fault_replay_fixture_has_only_declared_recovery_decisions(scenario):
    decision = replay_fault(
        ExecutionFailureEvent(
            task_id="synthetic_task",
            step_id=scenario["id"],
            failure_class=ExecutionFailureClass(scenario["failure_class"]),
            attempt_status=(
                ExecutionAttemptStatus(scenario["attempt_status"])
                if scenario.get("attempt_status")
                else None
            ),
            contract_scope_unchanged=scenario.get("contract_scope_unchanged", True),
        )
    )

    assert decision.level.value == scenario["expected"]["level"]
    assert decision.action == scenario["expected"]["action"]
    assert decision.max_attempts == scenario["expected"]["max_attempts"]
    assert decision.requires_reauthorization is scenario["expected"].get("requires_reauthorization", False)
    assert decision.requires_result_probe is scenario["expected"].get("requires_result_probe", False)


def test_fault_replay_corpus_covers_every_failure_class_and_unknown_attempt_override():
    assert {scenario["failure_class"] for scenario in SCENARIOS} == {
        failure_class.value for failure_class in ExecutionFailureClass
    }
    assert any(scenario.get("attempt_status") == "unknown" for scenario in SCENARIOS)
