"""Pre-enforce governance-chain tests with a hard no-execution boundary."""

from __future__ import annotations

import ast
import inspect
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from enterprise.agent.work_orders import BusinessPlan, BusinessPlanStep, ExecutionWorkOrder, RecoveryLevel
from enterprise.governance.analysis import SUPPORTED_TYPED_ACTION_TYPES
from enterprise.governance.capabilities import (
    AccessDisposition,
    CapabilityDataScope,
    CapabilityGrant,
    CapabilityGrantSet,
)
from enterprise.governance.contracts import DecisionOutcome, GovernanceMode, PageReadiness, TaskContract
from enterprise.governance.dry_run import (
    BusinessSemanticResolver,
    CandidateBusinessBinding,
    GovernedDryRunError,
    run_governed_dry_run,
)
from skyvern.webeye.actions.action_types import ActionType

NOW = datetime(2026, 7, 23, 8, 0, tzinfo=timezone.utc)
SECRET = "dry-run-test-key"


class ScenarioAction:
    def __init__(self, payload):
        self.payload = payload
        self.element_id = payload.get("element_id")

    def model_dump(self, **_kwargs):
        return self.payload


def _chain(*, mode=GovernanceMode.AUDIT, contract_expiry=None, grant_expiry=None, allowed=None):
    scope = CapabilityDataScope(
        department_id="dept_1",
        business_line_id="line_1",
        resource_ids={"record_1"},
    )
    grant = CapabilityGrant(
        grant_id="grant_sensitive_1",
        capability_id="records.review",
        capability_version="1",
        principal_id="user_sensitive_1",
        tenant_id="org_sensitive_1",
        data_scope=scope,
        disposition=AccessDisposition.ALLOW_EXECUTE,
        policy_snapshot_version="policy-v1",
        resolved_at=NOW - timedelta(minutes=1),
        expires_at=grant_expiry or NOW + timedelta(minutes=4),
    )
    step = BusinessPlanStep(
        step_id="plan_step_sensitive_1",
        capability_id=grant.capability_id,
        grant_id=grant.grant_id,
        contract_id="contract_sensitive_1",
        inputs={"record_id": "record-A"},
        expected_transition={"from": "open", "to": "reviewed"},
    )
    plan = BusinessPlan(
        plan_id="plan_sensitive_1",
        task_id="task_sensitive_1",
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
        goal="Review a synthetic record",
        allowed_operations={"read", "payment", "delete", "input_text"},
        data_scope=scope.model_dump(mode="json"),
        expires_at=contract_expiry or NOW + timedelta(minutes=10),
        mode=mode,
    )
    work_order = ExecutionWorkOrder(
        work_order_id="work_order_sensitive_1",
        business_plan_step_id=step.step_id,
        task_id=plan.task_id,
        contract_id=plan.contract_id,
        grant_id=step.grant_id,
        navigation_goal="Review only",
        allowed_operations=allowed or {"read", "payment", "delete", "input_text"},
        prohibited_operations={"submit"},
        max_recovery_level=RecoveryLevel.L2,
        result_probe_ref="synthetic.result-probe.v1",
    )
    return contract, CapabilityGrantSet(grants=[grant]), plan, step, work_order


class SyntheticSemanticResolver:
    """Derives facts from current ActionIntent evidence, never from the Plan."""

    def __init__(
        self,
        *,
        observed_inputs=None,
        proposed_transition=None,
        capability_id="records.review",
        confidence=0.95,
    ):
        self.observed_inputs = observed_inputs
        self.proposed_transition = proposed_transition
        self.capability_id = capability_id
        self.confidence = confidence

    def derive(self, *, action_index, action, intent, observation, element, page_html):
        facts = intent.extracted_facts
        return CandidateBusinessBinding(
            action_index=action_index,
            capability_id=self.capability_id,
            observed_inputs=(
                {"record_id": facts.get("record_id")} if self.observed_inputs is None else self.observed_inputs
            ),
            proposed_transition=(
                {"from": facts.get("state"), "to": facts.get("transition_to")}
                if self.proposed_transition is None
                else self.proposed_transition
            ),
            fact_sources={
                "inputs.record_id": "record_id",
                "transition.from": "state",
                "transition.to": "transition_to",
            },
            extractor_ref="synthetic.records.semantic-adapter.v1",
            evidence_refs=["opaque-test-evidence-ref"],
            confidence=self.confidence,
        )


def _run(
    *,
    action=None,
    element=None,
    readiness=PageReadiness.READY,
    readiness_confidence=0.9,
    include_binding=True,
    binding_inputs=None,
    binding_transition=None,
    binding_capability=None,
    binding_confidence=0.95,
    **chain_kwargs,
):
    contract, grants, plan, step, work_order = _chain(**chain_kwargs)
    action = action or ScenarioAction({"action_type": "click", "element_id": "query"})
    element = element or {
        "text": "Query account balance",
        "attributes": {"data-record-id": "record-A", "data-state": "open", "data-transition-to": "reviewed"},
    }
    return run_governed_dry_run(
        task_contract=contract,
        grants=grants,
        business_plan=plan,
        business_plan_step=step,
        work_order=work_order,
        actions=[action],
        page_url="https://synthetic.example/records?token=raw-url-secret",
        page_html="<button>Query account balance</button><span>raw-html-secret</span>",
        element_lookup={str(action.element_id): element},
        semantic_resolver=(
            SyntheticSemanticResolver(
                observed_inputs=binding_inputs,
                proposed_transition=binding_transition,
                capability_id=step.capability_id if binding_capability is None else binding_capability,
                confidence=binding_confidence,
            )
            if include_binding
            else None
        ),
        hmac_secret=SECRET,
        now=NOW,
        readiness=readiness,
        readiness_confidence=readiness_confidence,
    )


def test_audit_dry_run_validates_the_complete_chain_without_execution():
    report = _run()

    assert report.schema_version == "phase2-governed-dry-run-v1"
    assert report.governance_mode == "audit"
    assert report.candidates[0].operation == "read"
    assert report.candidates[0].decision.outcome is DecisionOutcome.ALLOW
    assert report.execution_skipped is True
    assert report.execution_adapter_called is False
    assert report.runtime_wiring_eligible is False
    assert report.candidates[0].business_binding_required is True
    assert report.candidates[0].business_binding_verified is True
    assert report.candidates[0].business_binding_ref.startswith("business-binding:hmac-sha256:")


def test_dry_run_rejects_enforce_contract_and_expired_authorization():
    with pytest.raises(GovernedDryRunError, match="audit-mode"):
        _run(mode=GovernanceMode.ENFORCE)

    with pytest.raises(GovernedDryRunError, match="TaskContract has expired"):
        _run(contract_expiry=NOW)

    with pytest.raises(GovernedDryRunError, match="not an executable"):
        _run(grant_expiry=NOW)


def test_dry_run_rejects_an_action_outside_the_work_order():
    with pytest.raises(GovernedDryRunError, match="outside ExecutionWorkOrder"):
        _run(
            action=ScenarioAction({"action_type": "click", "element_id": "pay"}),
            element={"text": "Confirm transfer", "attributes": {}},
            allowed={"read"},
        )


def test_dry_run_requires_and_validates_domain_pack_business_binding():
    with pytest.raises(GovernedDryRunError, match="require a Domain Pack semantic resolver"):
        _run(include_binding=False)

    with pytest.raises(GovernedDryRunError, match="inputs do not match"):
        _run(binding_inputs={"account": "record_B"})

    with pytest.raises(GovernedDryRunError, match="inputs do not match"):
        _run(
            element={
                "text": "Query account balance",
                "attributes": {
                    "data-record-id": "record-B",
                    "data-state": "open",
                    "data-transition-to": "reviewed",
                },
            }
        )

    with pytest.raises(GovernedDryRunError, match="transition does not match"):
        _run(binding_transition={"from": "open", "to": "record_B"})

    with pytest.raises(GovernedDryRunError, match="capability must match"):
        _run(binding_capability="records.other")

    with pytest.raises(GovernedDryRunError, match="confidence is below"):
        _run(binding_confidence=0.79)


def test_dry_run_rejects_plan_facts_preloaded_into_resolver_for_a_different_target():
    with pytest.raises(GovernedDryRunError, match="does not match the current Action target"):
        _run(
            element={
                "text": "Query account balance",
                "attributes": {
                    "data-record-id": "record-B",
                    "data-state": "closed",
                    "data-transition-to": "archived",
                },
            },
            binding_inputs={"record_id": "record-A"},
            binding_transition={"from": "open", "to": "reviewed"},
        )


def test_dry_run_rejects_incomplete_or_unresolvable_fact_source_maps():
    class InvalidSourceResolver(SyntheticSemanticResolver):
        def derive(self, **kwargs):
            binding = super().derive(**kwargs)
            binding.fact_sources = {"inputs.record_id": "missing_record"}
            return binding

    contract, grants, plan, step, work_order = _chain()
    action = ScenarioAction({"action_type": "click", "element_id": "query"})
    arguments = dict(
        task_contract=contract,
        grants=grants,
        business_plan=plan,
        business_plan_step=step,
        work_order=work_order,
        actions=[action],
        page_url="https://synthetic.example/records",
        page_html="<button>Query account balance</button>",
        element_lookup={
            "query": {
                "text": "Query account balance",
                "attributes": {
                    "data-record-id": "record-A",
                    "data-state": "open",
                    "data-transition-to": "reviewed",
                },
            }
        },
        semantic_resolver=InvalidSourceResolver(),
        hmac_secret=SECRET,
        now=NOW,
        readiness=PageReadiness.READY,
        readiness_confidence=0.9,
    )

    with pytest.raises(GovernedDryRunError, match="cover every canonical fact exactly"):
        run_governed_dry_run(**arguments)


def test_dry_run_rejects_contract_scope_drift():
    contract, grants, plan, step, work_order = _chain()
    contract.data_scope["resource_ids"] = ["record_other"]

    with pytest.raises(GovernedDryRunError, match="TaskContract data scope"):
        run_governed_dry_run(
            task_contract=contract,
            grants=grants,
            business_plan=plan,
            business_plan_step=step,
            work_order=work_order,
            actions=[ScenarioAction({"action_type": "click", "element_id": "query"})],
            page_url="https://synthetic.example/records",
            page_html="<button>Query account balance</button>",
            element_lookup={"query": {"text": "Query account balance", "attributes": {}}},
            hmac_secret=SECRET,
            now=NOW,
            readiness=PageReadiness.READY,
        )


def test_dry_run_rejects_two_external_writes_from_one_observation():
    contract, grants, plan, step, work_order = _chain()

    with pytest.raises(GovernedDryRunError, match="multiple external writes"):
        run_governed_dry_run(
            task_contract=contract,
            grants=grants,
            business_plan=plan,
            business_plan_step=step,
            work_order=work_order,
            actions=[
                ScenarioAction({"action_type": "click", "element_id": "pay"}),
                ScenarioAction({"action_type": "click", "element_id": "delete"}),
            ],
            page_url="https://synthetic.example/records",
            page_html="<button>Confirm transfer</button><button>Delete beneficiary</button>",
            element_lookup={
                "pay": {"text": "Confirm transfer", "attributes": {}},
                "delete": {"text": "Delete beneficiary", "attributes": {}},
            },
            hmac_secret=SECRET,
            now=NOW,
            readiness=PageReadiness.READY,
        )


@pytest.mark.parametrize("readiness", [PageReadiness.LOADING, PageReadiness.TRANSITIONING])
def test_dry_run_routes_unsettled_observations_to_human_review(readiness):
    report = _run(readiness=readiness)

    assert report.candidates[0].decision.outcome is DecisionOutcome.NEEDS_HUMAN
    assert report.candidates[0].decision.risk_level == "unknown"


@pytest.mark.parametrize(
    ("readiness", "confidence"),
    [(PageReadiness.UNKNOWN, 0.0), (PageReadiness.READY, 0.59)],
)
def test_dry_run_routes_unknown_or_low_confidence_readiness_to_human(readiness, confidence):
    report = _run(readiness=readiness, readiness_confidence=confidence)

    assert report.candidates[0].decision.outcome is DecisionOutcome.NEEDS_HUMAN
    assert report.candidates[0].decision.risk_level == "unknown"


def test_dry_run_preserves_contract_scope_denial_as_reviewable_policy_evidence():
    contract, grants, plan, step, work_order = _chain()
    contract.allowed_operations = {"read"}
    action = ScenarioAction({"action_type": "click", "element_id": "pay"})

    report = run_governed_dry_run(
        task_contract=contract,
        grants=grants,
        business_plan=plan,
        business_plan_step=step,
        work_order=work_order,
        actions=[action],
        page_url="https://synthetic.example/pay",
        page_html="<button>Confirm transfer</button>",
        element_lookup={
            "pay": {
                "text": "Confirm transfer",
                "attributes": {
                    "data-record-id": "record-A",
                    "data-state": "open",
                    "data-transition-to": "reviewed",
                },
            }
        },
        semantic_resolver=SyntheticSemanticResolver(),
        hmac_secret=SECRET,
        now=NOW,
        readiness=PageReadiness.READY,
    )

    assert report.candidates[0].decision.outcome is DecisionOutcome.DENY
    assert "outside the task contract" in report.candidates[0].decision.reasons[0]


def test_dry_run_rejects_unknown_action_type_without_echoing_it():
    contract, grants, plan, step, work_order = _chain()
    secret_action_type = "secret-action-type-987"

    with pytest.raises(GovernedDryRunError) as exc_info:
        run_governed_dry_run(
            task_contract=contract,
            grants=grants,
            business_plan=plan,
            business_plan_step=step,
            work_order=work_order,
            actions=[ScenarioAction({"action_type": secret_action_type, "element_id": "unknown"})],
            page_url="https://synthetic.example/records",
            page_html="<button>Unknown</button>",
            element_lookup={"unknown": {"text": "Unknown", "attributes": {}}},
            hmac_secret=SECRET,
            now=NOW,
            readiness=PageReadiness.READY,
            readiness_confidence=0.9,
        )

    assert "unsupported typed Action" in str(exc_info.value)
    assert secret_action_type not in str(exc_info.value)


def test_dry_run_report_contains_only_opaque_references_and_redacted_evidence():
    report_json = _run(
        action=ScenarioAction({"action_type": "input_text", "element_id": "account", "text": "raw-action-secret"}),
        element={
            "text": "",
            "attributes": {
                "aria-label": "Account raw-dom-secret",
                "data-record-id": "record-A",
                "data-state": "open",
                "data-transition-to": "reviewed",
            },
        },
    ).model_dump_json()

    for raw_value in (
        "contract_sensitive_1",
        "grant_sensitive_1",
        "plan_sensitive_1",
        "plan_step_sensitive_1",
        "work_order_sensitive_1",
        "raw-url-secret",
        "raw-html-secret",
        "raw-action-secret",
        "raw-dom-secret",
        "account-raw-secret",
        "synthetic.records.semantic-adapter.v1",
        "opaque-test-evidence-ref",
    ):
        assert raw_value not in report_json
    assert "hmac-sha256" in report_json


def test_dry_run_module_has_no_browser_executor_import_or_callback_boundary():
    module_path = Path(__file__).parents[2] / "enterprise" / "governance" / "dry_run.py"
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    imported_modules = {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)} | {
        alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names
    }

    assert not any(module.startswith(("skyvern", "playwright")) for module in imported_modules)
    assert "execution_adapter" not in inspect.signature(run_governed_dry_run).parameters


def test_semantic_resolver_contract_cannot_receive_plan_or_authorization_inputs():
    resolver_parameters = set(inspect.signature(BusinessSemanticResolver.derive).parameters)

    assert {
        "business_plan",
        "business_plan_step",
        "work_order",
        "grants",
        "task_contract",
    }.isdisjoint(resolver_parameters)


def test_offline_typed_action_allowlist_tracks_skyvern_action_protocol():
    assert SUPPORTED_TYPED_ACTION_TYPES == {action_type.value for action_type in ActionType}
