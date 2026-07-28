"""Synthetic Phase 2.2 benchmark spanning dry-run and closure contracts."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from enterprise.agent.work_orders import BusinessPlan, BusinessPlanStep, ExecutionWorkOrder, RecoveryLevel
from enterprise.governance.audit import observation_hash
from enterprise.governance.capabilities import (
    AccessDisposition,
    CapabilityDataScope,
    CapabilityGrant,
    CapabilityGrantSet,
)
from enterprise.governance.classification import action_fingerprint
from enterprise.governance.contracts import (
    ExecutionAuthorization,
    ExecutionEffect,
    GovernanceMode,
    PageReadiness,
    TaskContract,
)
from enterprise.governance.dry_run import GovernedDryRunError, run_governed_dry_run
from enterprise.governance.execution_guard import ExecutionAuthorizationError, verify_execution_authorization

ROOT = Path(__file__).parents[2]
SCENARIOS = json.loads((ROOT / "tests" / "fixtures" / "pre_enforce_closure_scenarios.json").read_text(encoding="utf-8"))
ENTRYPOINTS = {
    entry["id"]: entry
    for entry in json.loads(
        (ROOT / "tests" / "fixtures" / "execution_entrypoint_inventory.json").read_text(encoding="utf-8")
    )
}
NOW = datetime(2026, 7, 23, 9, 0, tzinfo=timezone.utc)
SECRET = "pre-enforce-benchmark-key"


class BenchmarkAction:
    def __init__(self, payload):
        self.payload = payload
        self.element_id = payload.get("element_id")

    def model_dump(self, **_kwargs):
        return self.payload


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[scenario["id"] for scenario in SCENARIOS])
def test_pre_enforce_closure_benchmark(scenario):
    runner = scenario["runner"]
    if runner == "dry_run":
        _assert_dry_run_scenario(scenario)
    elif runner == "authorization_drift":
        _assert_authorization_drift_scenario(scenario)
    elif runner == "entrypoint_contract":
        _assert_entrypoint_contract_scenario(scenario)
    else:
        pytest.fail(f"Unknown benchmark runner: {runner}")


def test_benchmark_covers_every_required_pre_enforce_family():
    assert {scenario["id"] for scenario in SCENARIOS} == {
        "allow_read",
        "approval_high",
        "external_write_critical",
        "loading_transitioning",
        "action_drift",
        "page_drift",
        "multi_external_write_rejection",
        "cached_speculative_stale_observation",
        "cua_missing_evidence",
        "governed_script_rejection",
        "sdk_direct_route_or_reject",
    }


def _assert_dry_run_scenario(scenario):
    contract, grants, plan, step, work_order = _chain()
    actions = [BenchmarkAction(payload) for payload in scenario["actions"]]
    arguments = dict(
        task_contract=contract,
        grants=grants,
        business_plan=plan,
        business_plan_step=step,
        work_order=work_order,
        actions=actions,
        page_url=f"https://synthetic.example/{scenario['id']}",
        page_html="<main data-synthetic='true'></main>",
        element_lookup=scenario["elements"],
        hmac_secret=SECRET,
        now=NOW,
        readiness=PageReadiness(scenario["readiness"]),
        readiness_confidence=0.9,
    )
    if "expected_error" in scenario:
        with pytest.raises(GovernedDryRunError, match=scenario["expected_error"]):
            run_governed_dry_run(**arguments)
        return

    report = run_governed_dry_run(**arguments)
    candidate = report.candidates[0]
    assert candidate.decision.outcome.value == scenario["expected"]["outcome"]
    assert candidate.decision.risk_level == scenario["expected"]["risk"]
    assert candidate.effect.value == scenario["expected"]["effect"]
    assert report.execution_skipped is True
    assert report.execution_adapter_called is False


def _assert_authorization_drift_scenario(scenario):
    original = BenchmarkAction({"action_type": "click", "element_id": "pay", "text": "100"})
    url = "https://synthetic.example/pay"
    html = "<button>Pay 100</button>"
    current_observation_hash = observation_hash(url=url, html=html, secret=SECRET)
    authorization = ExecutionAuthorization(
        permit_id="synthetic-permit",
        action_fingerprint=action_fingerprint(
            task_id="benchmark-task",
            step_id="benchmark-step",
            action_payload=original.model_dump(),
            observation_hash=current_observation_hash,
            secret=SECRET,
        ),
        observation_hash=current_observation_hash,
        idempotency_key="synthetic:benchmark",
        effect=ExecutionEffect.EXTERNAL_WRITE,
    )
    current_action = original
    current_html = html
    if scenario["drift"] == "action":
        current_action = BenchmarkAction({"action_type": "click", "element_id": "pay", "text": "200"})
    else:
        current_html = "<button>Pay changed beneficiary</button>"

    with pytest.raises(ExecutionAuthorizationError, match=scenario["expected_error"]):
        verify_execution_authorization(
            authorization=authorization,
            task_id="benchmark-task",
            step_id="benchmark-step",
            action=current_action,
            page_url=url,
            page_html=current_html,
            hmac_secret=SECRET,
        )


def _assert_entrypoint_contract_scenario(scenario):
    entry = ENTRYPOINTS[scenario["entrypoint_id"]]

    assert entry["required_disposition"] == scenario["expected_disposition"]
    assert scenario["expected_control"] in entry["required_controls"]
    assert entry["current_status"] == scenario["expected_status"]
    assert entry["enforce_eligible"] is scenario["expected_enforce_eligible"]


def _chain():
    scope = CapabilityDataScope(department_id="dept", business_line_id="line", resource_ids={"synthetic"})
    grant = CapabilityGrant(
        grant_id="benchmark-grant",
        capability_id="synthetic.review",
        capability_version="1",
        principal_id="benchmark-user",
        tenant_id="benchmark-org",
        data_scope=scope,
        disposition=AccessDisposition.ALLOW_EXECUTE,
        policy_snapshot_version="benchmark-policy",
        resolved_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(minutes=5),
    )
    step = BusinessPlanStep(
        step_id="benchmark-step",
        capability_id=grant.capability_id,
        grant_id=grant.grant_id,
        contract_id="benchmark-contract",
    )
    plan = BusinessPlan(
        plan_id="benchmark-plan",
        task_id="benchmark-task",
        contract_id=step.contract_id,
        data_scope=scope,
        steps=[step],
    )
    contract = TaskContract(
        contract_id=step.contract_id,
        task_id=plan.task_id,
        organization_id=grant.tenant_id,
        initiator_id=grant.principal_id,
        department_id=scope.department_id,
        business_line_id=scope.business_line_id,
        goal="Evaluate a synthetic closure scenario",
        allowed_operations={"read", "submit", "payment", "delete"},
        data_scope=scope.model_dump(mode="json"),
        expires_at=NOW + timedelta(minutes=10),
        mode=GovernanceMode.AUDIT,
    )
    work_order = ExecutionWorkOrder(
        work_order_id="benchmark-work-order",
        business_plan_step_id=step.step_id,
        task_id=plan.task_id,
        contract_id=plan.contract_id,
        grant_id=grant.grant_id,
        navigation_goal="Synthetic benchmark only",
        allowed_operations={"read", "submit", "payment", "delete"},
        prohibited_operations=set(),
        max_recovery_level=RecoveryLevel.L2,
        result_probe_ref="synthetic.result-probe.v1",
    )
    return contract, CapabilityGrantSet(grants=[grant]), plan, step, work_order
