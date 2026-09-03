"""Focused Synthetic M10 planning, redaction, and restoration contracts."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from enterprise.agent_runs.journal import GovernedPlanCheckpoint, GovernedPlanStepRef, PlanRunState, PlanStepState
from enterprise.auth.schemas import DepartmentRole, UserContext
from enterprise.domains.synthetic_payment.constants import (
    BUSINESS_LINE_ID,
    PAYMENTS_DEPARTMENT_ID,
    TENANT_ID,
)
from enterprise.governance.admission import TaskAdmissionBundle
from tests.fixtures.synthetic_payment_agent_runs import build_m10_provider_factory
from tests.fixtures.synthetic_payment_runtime.m10_runtime import (
    M10PlanningError,
    SyntheticPaymentRuntimeAdapter,
    derive_agent_run_id,
)

INPUTS = {
    "payment_id": "m10-secret-payment",
    "beneficiary_id": "m10-secret-beneficiary",
    "amount": "5000.00",
    "currency": "CNY",
    "reference": "m10-secret-reference",
    "object_version": 1,
}


def _user(role: str = "operator", user_id: str = "m10-operator") -> UserContext:
    return UserContext(
        user_id=user_id,
        org_id=TENANT_ID,
        department_roles=[
            DepartmentRole(
                department_id=PAYMENTS_DEPARTMENT_ID,
                department_name="Synthetic payments",
                role=role,
            )
        ],
        business_line_ids=[BUSINESS_LINE_ID],
    )


def _adapter() -> SyntheticPaymentRuntimeAdapter:
    def no_session():
        raise AssertionError("Preparation must not touch persistence")

    class NoopDriver:
        async def execute(self, **trusted_inputs):
            raise AssertionError(trusted_inputs)

        async def probe(self, **trusted_inputs):
            raise AssertionError(trusted_inputs)

    return SyntheticPaymentRuntimeAdapter(no_session, driver=NoopDriver())


@pytest.mark.asyncio
async def test_failed_advance_reason_preserves_checkpoint_state_for_diagnostics() -> None:
    checkpoint = GovernedPlanCheckpoint(
        plan_run_id="run-m10-diagnostic",
        admission_id="admission-m10-diagnostic",
        root_task_id="run-m10-diagnostic",
        plan_id="plan-m10-diagnostic",
        plan_version=1,
        authority_contract_id="contract-m10-diagnostic",
        active_step=GovernedPlanStepRef(
            business_plan_step_id="step-m10-diagnostic",
            step_digest="a" * 64,
            work_order_id="work-order-m10-diagnostic",
            work_order_digest="b" * 64,
            native_task_id="native-task-m10-diagnostic",
            native_step_id="native-step-m10-diagnostic",
            native_contract_id="native-contract-m10-diagnostic",
            authority_contract_id="contract-m10-diagnostic",
            state=PlanStepState.ACTIVE,
        ),
        state=PlanRunState.APPROVAL_REQUIRED,
    )

    result = await _adapter()._advance_result(checkpoint)

    assert result.status.value == "FAILED"
    assert result.reason_code == "PACK_ADVANCE_FAILED_APPROVAL_REQUIRED"


def test_preparation_is_deterministic_and_model_boundary_uses_only_intent_token() -> None:
    adapter = _adapter()
    now = datetime(2026, 7, 30, 4, 0, tzinfo=timezone.utc)
    prepared = adapter.prepare_run(
        user=_user(),
        tenant_id=TENANT_ID,
        request_id="m10-request-001",
        intent_digest="a" * 64,
        business_inputs=INPUTS,
        target_url="http://127.0.0.1:18080",
        now=now,
    )
    repeated = adapter.prepare_run(
        user=_user(),
        tenant_id=TENANT_ID,
        request_id="m10-request-001",
        intent_digest="a" * 64,
        business_inputs=INPUTS,
        target_url="http://127.0.0.1:18080",
        now=now,
    )

    assert prepared == repeated
    assert prepared.run_id == derive_agent_run_id(tenant_id=TENANT_ID, request_id="m10-request-001")
    safe_trace = json.dumps(prepared.compilation.authority.trace.model_dump(mode="json"), sort_keys=True)
    for value in INPUTS.values():
        if isinstance(value, str):
            assert value not in safe_trace
    projection = adapter.model_safe_projection(prepared.compilation.authority)
    serialized_projection = projection.model_dump_json()
    for value in INPUTS.values():
        if isinstance(value, str):
            assert value not in serialized_projection


def test_live_provider_composition_is_explicit_model_safe_and_persisted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("M11_TEST_PROVIDER_KEY", "credential-canary")
    requests: list[dict[str, object]] = []

    def transport(*, endpoint: str, api_key: str, payload: dict[str, object]) -> object:
        assert endpoint == "https://provider.invalid/v1/chat/completions"
        assert api_key == "credential-canary"
        requests.append(payload)
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "capability_id": "synthetic.payment.submit",
                                "input_slots": list(INPUTS),
                                "step_roles": ["precheck", "submit", "confirm"],
                            }
                        )
                    }
                }
            ]
        }

    adapter = SyntheticPaymentRuntimeAdapter(
        lambda: None,  # type: ignore[arg-type]
        driver=object(),  # type: ignore[arg-type]
        provider_mode="live",
        provider_factory=build_m10_provider_factory(
            "live",
            endpoint="https://provider.invalid/v1",
            model="m11-model",
            api_key_env="M11_TEST_PROVIDER_KEY",
            transport=transport,
        ),
    )
    prepared = adapter.prepare_run(
        user=_user(),
        tenant_id=TENANT_ID,
        request_id="m11-live-001",
        intent_digest="f" * 64,
        business_inputs=INPUTS,
        target_url="http://127.0.0.1:18080",
        now=datetime(2026, 7, 31, 1, 0, tzinfo=timezone.utc),
    )

    assert prepared.admission_bundle.provider_mode == "live"
    assert len(requests) == 1
    encoded = json.dumps(requests, sort_keys=True)
    for canary in (
        *(value for value in INPUTS.values() if isinstance(value, str)),
        TENANT_ID,
        _user().user_id,
        "credential-canary",
    ):
        assert str(canary) not in encoded


def test_live_provider_has_no_incomplete_configuration_or_failure_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("M11_TEST_PROVIDER_KEY", raising=False)
    with pytest.raises(ValueError, match="configuration is incomplete"):
        build_m10_provider_factory(
            "live",
            endpoint="https://provider.invalid/v1",
            model="m11-model",
            api_key_env="M11_TEST_PROVIDER_KEY",
        )

    monkeypatch.setenv("M11_TEST_PROVIDER_KEY", "test-key")

    def failing_transport(**_kwargs: object) -> object:
        raise RuntimeError("provider unavailable")

    adapter = SyntheticPaymentRuntimeAdapter(
        lambda: None,  # type: ignore[arg-type]
        driver=object(),  # type: ignore[arg-type]
        provider_mode="live",
        provider_factory=build_m10_provider_factory(
            "live",
            endpoint="https://provider.invalid/v1",
            model="m11-model",
            api_key_env="M11_TEST_PROVIDER_KEY",
            transport=failing_transport,
        ),
    )
    with pytest.raises(M10PlanningError, match="PLANNER_PROVIDER_FAILURE"):
        adapter.prepare_run(
            user=_user(),
            tenant_id=TENANT_ID,
            request_id="m11-live-failure",
            intent_digest="e" * 64,
            business_inputs=INPUTS,
            target_url="http://127.0.0.1:18080",
            now=datetime(2026, 7, 31, 1, 0, tzinfo=timezone.utc),
        )


def test_old_admission_defaults_recorded_and_restore_never_calls_provider() -> None:
    prepared = _adapter().prepare_run(
        user=_user(),
        tenant_id=TENANT_ID,
        request_id="m11-restore-001",
        intent_digest="d" * 64,
        business_inputs=INPUTS,
        target_url="http://127.0.0.1:18080",
        now=datetime(2026, 7, 31, 1, 0, tzinfo=timezone.utc),
    )
    legacy = prepared.admission_bundle.model_dump(mode="json")
    legacy.pop("provider_mode")
    legacy.pop("planner_observation")
    bundle = TaskAdmissionBundle.model_validate(legacy)
    assert bundle.provider_mode == "recorded"
    assert bundle.planner_observation is None

    def forbidden_provider(_planner_input: object) -> object:
        raise AssertionError("restoration must not invoke a provider")

    restorer = SyntheticPaymentRuntimeAdapter(
        lambda: None,  # type: ignore[arg-type]
        driver=object(),  # type: ignore[arg-type]
        provider_mode="live",
        provider_factory=forbidden_provider,  # type: ignore[arg-type]
    )
    assert restorer.restore_run(bundle, target_url=prepared.target_url).admission_bundle == bundle


def test_structurally_repaired_plan_uses_the_same_trusted_compiler() -> None:
    def repaired_provider(planner_input):
        from tests.fixtures.synthetic_payment_runtime.m9_runtime import RecordedM9Provider

        return RecordedM9Provider(
            [
                "not-json",
                {
                    "capability_id": "synthetic.payment.submit",
                    "input_slots": [item.name for item in planner_input.input_slots],
                    "step_roles": ["precheck", "submit", "confirm"],
                },
            ]
        )

    adapter = SyntheticPaymentRuntimeAdapter(
        lambda: None,  # type: ignore[arg-type]
        driver=object(),  # type: ignore[arg-type]
        provider_factory=repaired_provider,
    )
    prepared = adapter.prepare_run(
        user=_user(),
        tenant_id=TENANT_ID,
        request_id="m12-repaired-plan",
        intent_digest="9" * 64,
        business_inputs=INPUTS,
        target_url="http://127.0.0.1:18080",
        now=datetime(2026, 8, 2, tzinfo=timezone.utc),
    )
    observation = prepared.admission_bundle.planner_observation
    assert observation is not None
    assert observation.disposition == "repaired"
    assert observation.provider_calls == 2
    assert observation.repair_count == 1
    assert len(prepared.compilation.work_orders) == 3
