"""Synthetic financial workflow benchmark for Phase 2.1b."""

import json
from pathlib import Path

import pytest

from enterprise.governance.analysis import analyze_action, build_observation, evaluate_audit_policy
from enterprise.governance.contracts import PageReadiness


class ScenarioAction:
    def __init__(self, payload):
        self.payload = payload

    def model_dump(self, **_kwargs):
        return self.payload


SCENARIOS = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "governance_scenarios.json").read_text(encoding="utf-8")
)


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[scenario["id"] for scenario in SCENARIOS])
def test_synthetic_financial_governance_benchmark(scenario):
    observation = build_observation(
        task_id="synthetic_task",
        step_id=scenario["id"],
        url=scenario["url"],
        html=scenario["html"],
        readiness=PageReadiness(scenario["readiness"]),
    )
    intent = analyze_action(
        task_id="synthetic_task",
        step_id=scenario["id"],
        action=ScenarioAction(scenario["action"]),
        observation=observation,
        element=scenario["element"],
        hmac_secret="test-audit-key",
    )
    decision = evaluate_audit_policy(intent, observation=observation)
    expected = scenario["expected"]

    assert intent.operation == expected["operation"]
    assert intent.effect.value == expected["effect"]
    assert decision.outcome.value == expected["outcome"]
    assert decision.risk_level == expected["risk"]


def test_benchmark_includes_all_required_scenario_families():
    ids = {scenario["id"] for scenario in SCENARIOS}

    assert {
        "query_balance",
        "download_customer_statement",
        "submit_loan_application",
        "approve_loan",
        "confirm_transfer",
        "delete_beneficiary",
        "password_input",
        "loading_transfer_confirmation",
    } <= ids
