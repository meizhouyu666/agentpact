"""M6 constrained Planner, compiler, adapter, and redacted trace tests."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from enterprise.agent.constrained_planner import (
    DeterministicPlanner,
    OpenAICompatiblePlanner,
    PlannerOutputError,
    PlannerProviderError,
    parse_planner_proposal,
)
from enterprise.auth.schemas import DepartmentRole, UserContext
from enterprise.domains.synthetic_payment.constants import (
    BUSINESS_LINE_ID,
    CAPABILITY_ID,
    PAYMENTS_DEPARTMENT_ID,
)
from enterprise.domains.synthetic_payment.sdk_manifest import build_pack_sdk_manifest
from enterprise.governance.capabilities import CapabilityDataScope
from enterprise.governance.pack_conformance import evaluate_static_pack_conformance
from tests.fixtures.synthetic_payment_runtime.m6_runtime import (
    M6TraceStage,
    SyntheticM6TrustedContext,
    append_execution_trace,
    bind_compilation_for_execution,
    bind_permit_to_execution,
    build_synthetic_installation,
    compile_synthetic_request,
)

NOW = datetime(2026, 7, 29, 11, 30, tzinfo=timezone.utc)
TENANT = "synthetic-m6-tenant"
REQUEST = "Submit the approved synthetic payment once"
INPUTS = {
    "payment_id": "pay-m6-001",
    "beneficiary_id": "vendor-m6-001",
    "amount": "5000.00",
    "currency": "CNY",
    "reference": "Synthetic M6 invoice",
    "object_version": 1,
}


def _context() -> SyntheticM6TrustedContext:
    return SyntheticM6TrustedContext(
        request_id="request-m6-001",
        task_id="task-m6-001",
        contract_id="contract-m6-001",
        tenant_id=TENANT,
        user=UserContext(
            user_id="operator-m6",
            org_id=TENANT,
            department_roles=[
                DepartmentRole(
                    department_id=PAYMENTS_DEPARTMENT_ID,
                    department_name="Synthetic payments",
                    role="operator",
                )
            ],
            business_line_ids=[BUSINESS_LINE_ID],
        ),
        data_scope=CapabilityDataScope(
            department_id=PAYMENTS_DEPARTMENT_ID,
            business_line_id=BUSINESS_LINE_ID,
            resource_ids={INPUTS["payment_id"]},
        ),
        resolved_at=NOW,
    )


def _installation(*, expires_at=None):
    return build_synthetic_installation(
        tenant_id=TENANT,
        accepted_at=NOW - timedelta(minutes=1),
        expires_at=expires_at or NOW + timedelta(minutes=30),
        contract_digest=build_pack_sdk_manifest().manifest_digest,
    )


def _compile(planner=None, *, installation=None):
    return compile_synthetic_request(
        natural_language_request=REQUEST,
        context=_context(),
        installation=installation or _installation(),
        conformance_report=evaluate_static_pack_conformance(build_pack_sdk_manifest()),
        planner=planner or DeterministicPlanner(INPUTS),
    )


def test_deterministic_planner_compiles_one_valid_trusted_plan_and_work_order():
    first = _compile()
    second = _compile()

    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert first.proposal.capability_id == CAPABILITY_ID
    assert len(first.business_plan.steps) == 1
    assert first.business_plan.task_id == first.task_contract.task_id
    assert first.work_order.business_plan_step_id == first.business_plan.steps[0].step_id
    assert first.work_order.contract_id == first.task_contract.contract_id
    assert first.work_order.grant_id == first.business_plan.steps[0].grant_id
    assert first.work_order.allowed_operations == {"read", "input", "select", "submit"}
    assert "javascript" in first.work_order.prohibited_operations


def test_derived_grant_contract_and_execution_binding_fail_closed_at_installation_expiry():
    installation = _installation(expires_at=NOW + timedelta(seconds=1))
    compiled = _compile(installation=installation)
    grant = compiled.grants.grants[0]

    assert grant.expires_at == installation.expires_at
    assert compiled.task_contract.expires_at == installation.expires_at
    binding = bind_compilation_for_execution(
        compiled,
        observed_business_inputs=INPUTS,
        work_order_id=compiled.work_order.work_order_id,
        now=NOW,
    )
    assert binding.expires_at == installation.expires_at
    assert compiled.grants.executable_grants(now=NOW + timedelta(seconds=1)) == []
    with pytest.raises(ValueError, match="installation is stale"):
        bind_compilation_for_execution(
            compiled,
            observed_business_inputs=INPUTS,
            work_order_id=compiled.work_order.work_order_id,
            now=NOW + timedelta(seconds=1),
        )


def test_execution_binding_rejects_input_work_order_permit_attempt_and_probe_mismatch():
    compiled = _compile()
    with pytest.raises(ValueError, match="Planner proposal"):
        bind_compilation_for_execution(
            compiled,
            observed_business_inputs={**INPUTS, "amount": "7.00"},
            work_order_id=compiled.work_order.work_order_id,
            now=NOW,
        )
    with pytest.raises(ValueError, match="compiled Work Order"):
        bind_compilation_for_execution(
            compiled,
            observed_business_inputs=INPUTS,
            work_order_id="work-order-forged",
            now=NOW,
        )

    binding = bind_compilation_for_execution(
        compiled,
        observed_business_inputs=INPUTS,
        work_order_id=compiled.work_order.work_order_id,
        now=NOW,
    )
    idempotency_key = f"synthetic:{INPUTS['payment_id']}"
    with pytest.raises(ValueError, match="idempotency key"):
        bind_permit_to_execution(
            binding,
            permit_id="permit-m6",
            task_id=compiled.work_order.task_id,
            contract_id=compiled.work_order.contract_id,
            action_fingerprint="fingerprint-m6",
            idempotency_key="synthetic:forged",
            now=NOW,
        )
    permit_binding = bind_permit_to_execution(
        binding,
        permit_id="permit-m6",
        task_id=compiled.work_order.task_id,
        contract_id=compiled.work_order.contract_id,
        action_fingerprint="fingerprint-m6",
        idempotency_key=idempotency_key,
        now=NOW,
    )
    evidence = {
        "result_probe": {"status": "confirmed", "observed_version": 2},
        "facts": INPUTS,
    }
    with pytest.raises(ValueError, match="Attempt idempotency key"):
        append_execution_trace(
            compiled.trace,
            compilation=compiled,
            execution_binding=binding,
            permit_binding=permit_binding,
            attempt_id="attempt-m6",
            attempt_task_id=compiled.work_order.task_id,
            attempt_contract_id=compiled.work_order.contract_id,
            attempt_action_fingerprint=permit_binding.action_fingerprint,
            attempt_idempotency_key="synthetic:forged",
            attempt_state_sequence=("executing", "unknown", "confirmed"),
            result_probe_evidence=evidence,
            final_state="confirmed",
            browser_effect_count=1,
        )
    with pytest.raises(ValueError, match="Planner proposal"):
        append_execution_trace(
            compiled.trace,
            compilation=compiled,
            execution_binding=binding,
            permit_binding=permit_binding,
            attempt_id="attempt-m6",
            attempt_task_id=compiled.work_order.task_id,
            attempt_contract_id=compiled.work_order.contract_id,
            attempt_action_fingerprint=permit_binding.action_fingerprint,
            attempt_idempotency_key=idempotency_key,
            attempt_state_sequence=("executing", "unknown", "confirmed"),
            result_probe_evidence={
                "result_probe": evidence["result_probe"],
                "facts": {**INPUTS, "amount": "7.00"},
            },
            final_state="confirmed",
            browser_effect_count=1,
        )

    trace = append_execution_trace(
        compiled.trace,
        compilation=compiled,
        execution_binding=binding,
        permit_binding=permit_binding,
        attempt_id="attempt-m6",
        attempt_task_id=compiled.work_order.task_id,
        attempt_contract_id=compiled.work_order.contract_id,
        attempt_action_fingerprint=permit_binding.action_fingerprint,
        attempt_idempotency_key=idempotency_key,
        attempt_state_sequence=("executing", "unknown", "confirmed"),
        result_probe_evidence=evidence,
        final_state="confirmed",
        browser_effect_count=1,
    )
    assert [event.stage for event in trace.events[-6:]] == [
        M6TraceStage.EXECUTION_BINDING,
        M6TraceStage.PERMIT,
        M6TraceStage.ATTEMPT,
        M6TraceStage.BROWSER_EFFECT,
        M6TraceStage.RESULT_PROBE,
        M6TraceStage.FINAL_STATE,
    ]


def test_projection_exposes_no_trusted_identity_authority_or_browser_mechanism():
    compiled = _compile()
    serialized = json.dumps([item.model_dump(mode="json") for item in compiled.projection], sort_keys=True)

    assert CAPABILITY_ID in serialized
    for forbidden in (
        "grant_id",
        "tenant_id",
        "principal",
        "policy_version",
        "permit",
        "attempt",
        "adapter_ref",
        "result_probe_ref",
        "browser",
        "locator",
        "coordinate",
        "javascript",
    ):
        assert forbidden not in serialized.lower()


def test_installed_but_non_executable_capability_never_reaches_the_planner():
    context = _context()
    non_executable_user = context.user.model_copy(deep=True)
    non_executable_user.department_roles[0].role = "approver"

    with pytest.raises(ValueError, match="No installed executable Capability"):
        compile_synthetic_request(
            natural_language_request=REQUEST,
            context=context.model_copy(update={"user": non_executable_user}),
            installation=_installation(),
            conformance_report=evaluate_static_pack_conformance(build_pack_sdk_manifest()),
            planner=DeterministicPlanner(INPUTS),
        )


@pytest.mark.parametrize(
    "payload",
    [
        "not-json",
        {"capability_id": CAPABILITY_ID},
        {"capability_id": CAPABILITY_ID, "business_inputs": INPUTS, "tenant_id": TENANT},
        {"capability_id": CAPABILITY_ID, "business_inputs": INPUTS, "grant_id": "forged"},
        {"capability_id": CAPABILITY_ID, "business_inputs": INPUTS, "execution_mechanism": "javascript"},
    ],
)
def test_closed_proposal_rejects_malformed_and_forged_authority(payload):
    with pytest.raises(PlannerOutputError, match="closed proposal schema"):
        parse_planner_proposal(payload)


def test_unprojected_capability_and_invalid_or_extra_business_inputs_fail_before_plan_creation():
    class RawPlanner:
        def __init__(self, payload):
            self.payload = payload

        def propose(self, _planner_input):
            return self.payload

    with pytest.raises(PlannerOutputError, match="outside its projection"):
        _compile(RawPlanner({"capability_id": "synthetic.payment.other", "business_inputs": INPUTS}))
    with pytest.raises(ValueError, match="greater than 0"):
        _compile(RawPlanner({"capability_id": CAPABILITY_ID, "business_inputs": {**INPUTS, "amount": "0"}}))
    with pytest.raises(ValueError, match="unknown fields"):
        _compile(
            RawPlanner(
                {
                    "capability_id": CAPABILITY_ID,
                    "business_inputs": {**INPUTS, "permit_id": "forged"},
                }
            )
        )


def test_redacted_trace_is_deterministic_and_contains_no_business_values_or_browser_content():
    trace = _compile().trace
    serialized = json.dumps(trace.model_dump(mode="json"), sort_keys=True)

    assert [event.stage for event in trace.events] == [
        M6TraceStage.REQUEST,
        M6TraceStage.INSTALLATION,
        M6TraceStage.PROJECTION,
        M6TraceStage.PROPOSAL,
        M6TraceStage.VALIDATION,
        M6TraceStage.TASK_CONTRACT,
        M6TraceStage.BUSINESS_PLAN,
        M6TraceStage.WORK_ORDER,
    ]
    for forbidden in (
        INPUTS["payment_id"],
        INPUTS["beneficiary_id"],
        INPUTS["amount"],
        INPUTS["currency"],
        INPUTS["reference"],
        "<html",
        "screenshot",
        "OPENAI_API_KEY",
    ):
        assert str(forbidden) not in serialized


def test_openai_compatible_adapter_uses_environment_credential_and_shared_parser(monkeypatch):
    captured = {}

    def transport(*, endpoint, api_key, payload):
        captured.update(endpoint=endpoint, api_key=api_key, payload=payload)
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps({"capability_id": CAPABILITY_ID, "business_inputs": INPUTS})
                    }
                }
            ]
        }

    monkeypatch.setenv("M6_TEST_API_KEY", "environment-only-secret")
    planner = OpenAICompatiblePlanner(
        endpoint="https://model.invalid/v1",
        model="mock-model",
        transport=transport,
        api_key_env="M6_TEST_API_KEY",
    )
    compiled = _compile(planner)

    assert compiled.proposal.business_inputs == INPUTS
    assert captured["endpoint"] == "https://model.invalid/v1/chat/completions"
    assert captured["api_key"] == "environment-only-secret"
    assert "environment-only-secret" not in json.dumps(captured["payload"])
    assert "grant_id" not in json.dumps(captured["payload"])


def test_openai_compatible_adapter_fails_closed_on_missing_key_transport_and_response(monkeypatch):
    monkeypatch.delenv("M6_MISSING_API_KEY", raising=False)
    missing = OpenAICompatiblePlanner(
        endpoint="https://model.invalid/v1",
        model="mock-model",
        transport=lambda **_kwargs: {},
        api_key_env="M6_MISSING_API_KEY",
    )
    with pytest.raises(PlannerProviderError, match="not set"):
        _compile(missing)

    monkeypatch.setenv("M6_TEST_API_KEY", "secret")
    transport_failure = OpenAICompatiblePlanner(
        endpoint="https://model.invalid/v1",
        model="mock-model",
        transport=lambda **_kwargs: (_ for _ in ()).throw(OSError("provider down")),
        api_key_env="M6_TEST_API_KEY",
    )
    with pytest.raises(PlannerProviderError, match="transport failed"):
        _compile(transport_failure)

    malformed = OpenAICompatiblePlanner(
        endpoint="https://model.invalid/v1",
        model="mock-model",
        transport=lambda **_kwargs: {"choices": []},
        api_key_env="M6_TEST_API_KEY",
    )
    with pytest.raises(PlannerProviderError, match="response was malformed"):
        _compile(malformed)
