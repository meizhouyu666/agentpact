"""Stripe M6 constrained Planner, compiler, binding, trace, and probe tests.

Deterministic and network-free: the Planner is ``DeterministicPlanner`` and the
probe is ``RecordedStripeProbe``. The live Stripe API is never contacted.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from enterprise.agent.constrained_planner import (
    DeterministicPlanner,
    PlannerOutputError,
    parse_planner_proposal,
)
from enterprise.auth.schemas import DepartmentRole, UserContext
from enterprise.domains.stripe_payment.constants import (
    BUSINESS_LINE_ID,
    CAPABILITY_ID,
    PAYMENTS_DEPARTMENT_ID,
)
from enterprise.domains.stripe_payment.m6_runtime import (
    STRIPE_RUNTIME_CONTRACT,
    M6TraceStage,
    StripeM6TrustedContext,
    append_execution_trace,
    bind_compilation_for_execution,
    bind_permit_to_execution,
    build_stripe_conformance_attestation,
    build_stripe_installation,
    compile_stripe_request,
    probe_submission_outcome,
    require_confirmed_outcome,
)
from enterprise.domains.stripe_payment.result_probe import (
    RecordedStripeProbe,
    StripePaymentIntentRead,
)
from enterprise.domains.stripe_payment.sdk_manifest import build_pack_sdk_manifest
from enterprise.governance.capabilities import CapabilityDataScope
from enterprise.governance.pack_conformance import ConformanceStatus, evaluate_static_pack_conformance

NOW = datetime(2026, 7, 29, 11, 30, tzinfo=timezone.utc)
TENANT = "stripe-m6-tenant"
REQUEST = "Submit the approved test-mode payment once"
INPUTS = {
    "payment_intent_id": "pi_m6_001",
    "customer_id": "cus_m6_001",
    "amount_minor": 5000,
    "currency": "usd",
    "description": "Stripe M6 test payment",
    "object_version": 1,
}
IDEMPOTENCY_KEY = f"stripe:{INPUTS['payment_intent_id']}"


def _context() -> StripeM6TrustedContext:
    return StripeM6TrustedContext(
        request_id="request-stripe-m6-001",
        task_id="task-stripe-m6-001",
        contract_id="contract-stripe-m6-001",
        tenant_id=TENANT,
        user=UserContext(
            user_id="operator-stripe-m6",
            org_id=TENANT,
            department_roles=[
                DepartmentRole(
                    department_id=PAYMENTS_DEPARTMENT_ID,
                    department_name="Stripe payments",
                    role="operator",
                )
            ],
            business_line_ids=[BUSINESS_LINE_ID],
        ),
        data_scope=CapabilityDataScope(
            department_id=PAYMENTS_DEPARTMENT_ID,
            business_line_id=BUSINESS_LINE_ID,
            resource_ids={INPUTS["payment_intent_id"]},
        ),
        resolved_at=NOW,
    )


def _installation(*, expires_at: datetime | None = None):
    return build_stripe_installation(
        tenant_id=TENANT,
        accepted_at=NOW - timedelta(minutes=1),
        expires_at=expires_at or NOW + timedelta(minutes=30),
        contract_digest=build_pack_sdk_manifest().manifest_digest,
    )


def _compile(planner=None, *, installation=None):
    return compile_stripe_request(
        natural_language_request=REQUEST,
        context=_context(),
        installation=installation or _installation(),
        conformance_report=evaluate_static_pack_conformance(build_pack_sdk_manifest()),
        planner=planner or DeterministicPlanner(INPUTS),
    )


def _confirmed_probe() -> RecordedStripeProbe:
    return RecordedStripeProbe(
        {
            INPUTS["payment_intent_id"]: StripePaymentIntentRead(
                payment_intent_id="pi_test_001",
                status="succeeded",
                amount_minor=INPUTS["amount_minor"],
                currency=INPUTS["currency"],
            )
        }
    )


def test_runtime_contract_and_fixed_attestation_match_offline_manifest():
    manifest = build_pack_sdk_manifest()
    attestation = build_stripe_conformance_attestation()

    assert STRIPE_RUNTIME_CONTRACT.model_dump() == {
        "pack_id": "stripe.payment",
        "pack_version": "0.1.0-draft.1",
        "display_name": "Stripe Payment (Test Mode) Domain Pack",
        "capability_ids": ("stripe.payment.read", "stripe.payment.submit"),
        "adapter_id": "stripe.payment.agent-run-runtime.v1",
        "manifest_digest": manifest.manifest_digest,
    }
    assert attestation.status is ConformanceStatus.PASS
    assert attestation.candidate_pack_id == manifest.pack_id
    assert attestation.manifest_digest == manifest.manifest_digest
    assert not attestation.checks
    assert not attestation.violations


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
    assert first.work_order.result_probe_ref == "stripe.payment.submit.result-probe.v1"


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
            observed_business_inputs={**INPUTS, "amount_minor": 7000},
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
    with pytest.raises(ValueError, match="idempotency key"):
        bind_permit_to_execution(
            binding,
            permit_id="permit-stripe-m6",
            task_id=compiled.work_order.task_id,
            contract_id=compiled.work_order.contract_id,
            action_fingerprint="fingerprint-stripe-m6",
            idempotency_key="stripe:forged",
            now=NOW,
        )
    permit_binding = bind_permit_to_execution(
        binding,
        permit_id="permit-stripe-m6",
        task_id=compiled.work_order.task_id,
        contract_id=compiled.work_order.contract_id,
        action_fingerprint="fingerprint-stripe-m6",
        idempotency_key=IDEMPOTENCY_KEY,
        now=NOW,
    )

    evidence, final_state = probe_submission_outcome(
        probe=_confirmed_probe(),
        observed_business_inputs=INPUTS,
        idempotency_key=IDEMPOTENCY_KEY,
    )
    assert final_state == "confirmed"
    require_confirmed_outcome(final_state)

    with pytest.raises(ValueError, match="Attempt idempotency key"):
        append_execution_trace(
            compiled.trace,
            compilation=compiled,
            execution_binding=binding,
            permit_binding=permit_binding,
            attempt_id="attempt-stripe-m6",
            attempt_task_id=compiled.work_order.task_id,
            attempt_contract_id=compiled.work_order.contract_id,
            attempt_action_fingerprint=permit_binding.action_fingerprint,
            attempt_idempotency_key="stripe:forged",
            attempt_state_sequence=("executing", "unknown", "confirmed"),
            result_probe_evidence=evidence,
            final_state=final_state,
            browser_effect_count=1,
        )

    trace = append_execution_trace(
        compiled.trace,
        compilation=compiled,
        execution_binding=binding,
        permit_binding=permit_binding,
        attempt_id="attempt-stripe-m6",
        attempt_task_id=compiled.work_order.task_id,
        attempt_contract_id=compiled.work_order.contract_id,
        attempt_action_fingerprint=permit_binding.action_fingerprint,
        attempt_idempotency_key=IDEMPOTENCY_KEY,
        attempt_state_sequence=("executing", "unknown", "confirmed"),
        result_probe_evidence=evidence,
        final_state=final_state,
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


def test_unconfirmed_probe_outcome_is_forbidden_to_proceed():
    pending_probe = RecordedStripeProbe(
        {
            INPUTS["payment_intent_id"]: StripePaymentIntentRead(
                payment_intent_id="pi_test_001",
                status="processing",
                amount_minor=INPUTS["amount_minor"],
                currency=INPUTS["currency"],
            )
        }
    )
    evidence, final_state = probe_submission_outcome(
        probe=pending_probe,
        observed_business_inputs=INPUTS,
        idempotency_key=IDEMPOTENCY_KEY,
    )

    assert evidence["result_probe"]["status"] == "unknown"
    assert final_state == "unknown"
    with pytest.raises(ValueError, match="outcome is not confirmed"):
        require_confirmed_outcome(final_state)


def test_canceled_probe_outcome_is_not_confirmed():
    canceled_probe = RecordedStripeProbe(
        {
            INPUTS["payment_intent_id"]: StripePaymentIntentRead(
                payment_intent_id="pi_test_001",
                status="canceled",
                amount_minor=INPUTS["amount_minor"],
                currency=INPUTS["currency"],
                failure_code="payment_method_unavailable",
            )
        }
    )
    _evidence, final_state = probe_submission_outcome(
        probe=canceled_probe,
        observed_business_inputs=INPUTS,
        idempotency_key=IDEMPOTENCY_KEY,
    )
    assert final_state == "not_confirmed"


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
        compile_stripe_request(
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
        _compile(RawPlanner({"capability_id": "stripe.payment.other", "business_inputs": INPUTS}))
    with pytest.raises(ValueError, match="greater than 0"):
        _compile(RawPlanner({"capability_id": CAPABILITY_ID, "business_inputs": {**INPUTS, "amount_minor": 0}}))
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
        INPUTS["payment_intent_id"],
        INPUTS["customer_id"],
        INPUTS["description"],
        "<html",
        "screenshot",
        "STRIPE_SECRET_KEY",
    ):
        assert str(forbidden) not in serialized


def test_probe_evidence_does_not_leak_idempotency_key_or_stripe_key():
    evidence, _final_state = probe_submission_outcome(
        probe=_confirmed_probe(),
        observed_business_inputs=INPUTS,
        idempotency_key=IDEMPOTENCY_KEY,
    )
    serialized = json.dumps(evidence, sort_keys=True)

    assert "stripe:pi_m6_001" not in serialized
    assert "STRIPE_SECRET_KEY" not in serialized
    assert "sk_test" not in serialized
