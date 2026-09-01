"""Focused M10 SDK, API-schema, redaction, and projection contracts."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from enterprise.agent_runs.routes import configure_agent_run_service, reset_agent_run_service, router
from enterprise.agent_runs.service import (
    AgentRunAction,
    AgentRunCreateRequest,
    AgentRunDecisionTrace,
    AgentRunDecisionTraceStage,
    AgentRunPage,
    AgentRunPlanStep,
    AgentRunProjection,
    AgentRunState,
    AgentRunSummary,
)
from enterprise.auth.dependencies import get_current_user
from enterprise.auth.schemas import DepartmentRole, UserContext
from enterprise.domains.synthetic_payment.constants import (
    BUSINESS_LINE_ID,
    PAYMENTS_DEPARTMENT_ID,
    TENANT_ID,
)
from enterprise.domains.synthetic_payment.sdk_manifest import build_pack_sdk_manifest
from enterprise.governance.admission import TaskAdmissionBundle
from enterprise.governance.contracts import ActionIntent, DecisionOutcome, ExecutionEffect, PolicyDecision
from enterprise.governance.pack_runtime import PackRuntimeBinding, PackRuntimeRegistry
from skyvern.forge.native_action import NativeActionDisposition, NativeActionResolution
from tests.fixtures.synthetic_payment_agent_runs import build_m10_provider_factory
from tests.fixtures.synthetic_payment_runtime.m6_runtime import SYNTHETIC_RUNTIME_CONTRACT
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


def test_runtime_adapter_exactly_conforms_to_static_manifest_without_sdk_wiring() -> None:
    manifest = build_pack_sdk_manifest()
    registry = PackRuntimeRegistry([SYNTHETIC_RUNTIME_CONTRACT])
    adapter = _adapter()
    registry.register(adapter)

    assert registry.require(pack_id=manifest.pack_id, pack_version=manifest.pack_version) is adapter
    assert registry.public_metadata(pack_id=manifest.pack_id, pack_version=manifest.pack_version).model_dump() == {
        "pack_id": "synthetic.payment",
        "pack_version": "1.0.0",
        "display_name": "Synthetic Payment Reference Pack",
    }
    assert SYNTHETIC_RUNTIME_CONTRACT.pack_id == manifest.pack_id
    assert SYNTHETIC_RUNTIME_CONTRACT.pack_version == manifest.pack_version
    assert SYNTHETIC_RUNTIME_CONTRACT.display_name == manifest.display_name
    assert SYNTHETIC_RUNTIME_CONTRACT.capability_ids == tuple(
        item.capability_id for item in manifest.capabilities
    )
    assert SYNTHETIC_RUNTIME_CONTRACT.manifest_digest == manifest.manifest_digest
    assert adapter.binding.capability_ids == SYNTHETIC_RUNTIME_CONTRACT.capability_ids
    assert manifest.contract_catalog_only is True
    assert manifest.runtime_wiring_eligible is False
    assert "adapter" not in manifest.model_dump(mode="json")

    class ForgedAdapter:
        binding = PackRuntimeBinding(
            pack_id=manifest.pack_id,
            pack_version=manifest.pack_version,
            capability_ids=("synthetic.payment.submit",),
            adapter_id="forged",
        )

        def model_safe_projection(self, authority):
            return authority

        def prepare_run(self, **trusted_inputs):
            return trusted_inputs

        async def admit_run(self, prepared, **trusted_inputs):
            return prepared, trusted_inputs

        async def advance_run(self, prepared, **trusted_inputs):
            return prepared, trusted_inputs

        async def probe_run(self, prepared, **trusted_inputs):
            return prepared, trusted_inputs

    with pytest.raises(ValueError):
        PackRuntimeRegistry([SYNTHETIC_RUNTIME_CONTRACT]).register(ForgedAdapter())


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


def test_public_create_schema_forbids_identity_provider_authority_and_browser_fields() -> None:
    AgentRunCreateRequest(request_id="req-1", intent="Submit", business_inputs=INPUTS)
    for forbidden in (
        "tenant_id",
        "principal_id",
        "provider_mode",
        "adapter_id",
        "grant_ids",
        "permit_id",
        "browser_action",
        "selector",
    ):
        with pytest.raises(ValidationError):
            AgentRunCreateRequest.model_validate(
                {"request_id": "req-1", "intent": "Submit", "business_inputs": INPUTS, forbidden: "forged"}
            )


def test_approval_required_resolution_has_exact_correlation_and_no_permit() -> None:
    intent = ActionIntent(
        intent_id="intent-1",
        task_id="task-1",
        step_id="step-1",
        action_fingerprint="b" * 64,
        observation_id="c" * 64,
        operation="submit",
        effect=ExecutionEffect.EXTERNAL_WRITE,
    )
    decision = PolicyDecision(
        decision_id="decision-1",
        intent_id=intent.intent_id,
        outcome=DecisionOutcome.REQUIRE_APPROVAL,
        risk_level="high",
        required_approver={"department_id": PAYMENTS_DEPARTMENT_ID, "role": "approver"},
        policy_version="synthetic-payment-v1",
    )
    resolution = NativeActionResolution(
        disposition=NativeActionDisposition.APPROVAL_REQUIRED,
        operation="submit",
        binding_digest="d" * 64,
        observation_hash=intent.observation_id,
        action_fingerprint=intent.action_fingerprint,
        approval_intent=intent,
        approval_decision=decision,
    )
    assert resolution.execution_authorization is None
    assert resolution.execution_profile is None

    with pytest.raises(ValidationError):
        NativeActionResolution.model_validate(
            resolution.model_copy(update={"action_fingerprint": "e" * 64}).model_dump()
        )


class _RouteService:
    async def create(self, body, *, user):
        del body, user
        return AgentRunProjection(
                run_id="run_route",
            pack_id="synthetic.payment",
            pack_version="1.0.0",
            pack_display_name="Synthetic Payment Reference Pack",
            provider_mode="recorded",
            state=AgentRunState.AWAITING_APPROVAL,
            legal_actions=(AgentRunAction.APPROVE, AgentRunAction.REJECT),
            plan=(AgentRunPlanStep(sequence=1, role="submit", state="active"),),
            completed_steps=0,
            total_steps=1,
        )

    async def list_runs(self, *, user, cursor=None, limit=20):
        del user, cursor, limit
        return AgentRunPage(
            items=(
                AgentRunSummary(
                    run_id="run_route",
                    pack_id="synthetic.payment",
                    pack_version="1.0.0",
                    pack_display_name="Synthetic Payment Reference Pack",
                    provider_mode="recorded",
                    state=AgentRunState.AWAITING_APPROVAL,
                    completed_steps=0,
                    total_steps=1,
                    created_at=datetime(2026, 7, 31, 1, 0, tzinfo=timezone.utc),
                    modified_at=datetime(2026, 7, 31, 1, 1, tzinfo=timezone.utc),
                ),
            )
        )

    async def decision_trace(self, run_id, *, user):
        del user
        return AgentRunDecisionTrace(
            run_id=run_id,
            stages=tuple(
                AgentRunDecisionTraceStage(stage=stage, status="completed", reason_code="SAFE")
                for stage in ("provider", "validation", "compilation", "admission", "approval", "execution", "recovery")
            ),
        )


def test_http_create_returns_only_redacted_projection_and_rejects_extra_authority() -> None:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_current_user] = _user
    configure_agent_run_service(_RouteService())  # type: ignore[arg-type]
    try:
        client = TestClient(app)
        response = client.post(
            "/api/v1/enterprise/agent-runs/",
            json={"request_id": "req-route", "intent": "Submit", "business_inputs": INPUTS},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["state"] == "AWAITING_APPROVAL"
        assert payload["pack_id"] == "synthetic.payment"
        assert payload["pack_display_name"] == "Synthetic Payment Reference Pack"
        assert payload["provider_mode"] == "recorded"
        encoded = json.dumps(payload, sort_keys=True)
        assert all(value not in encoded for value in INPUTS.values() if isinstance(value, str))

        forbidden = client.post(
            "/api/v1/enterprise/agent-runs/",
            json={
                "request_id": "req-route",
                "intent": "Submit",
                "business_inputs": INPUTS,
                "provider_mode": "live",
            },
        )
        assert forbidden.status_code == 422

        listed = client.get("/api/v1/enterprise/agent-runs/")
        assert listed.status_code == 200
        assert listed.json()["items"][0]["run_id"] == "run_route"
        assert "legal_actions" not in listed.json()["items"][0]
        assert "plan" not in listed.json()["items"][0]

        trace = client.get("/api/v1/enterprise/agent-runs/run_route/decision-trace")
        assert trace.status_code == 200
        assert trace.json()["non_authoritative"] is True
        assert "legal_actions" not in trace.text
        assert [item["stage"] for item in trace.json()["stages"]] == [
            "provider",
            "validation",
            "compilation",
            "admission",
            "approval",
            "execution",
            "recovery",
        ]
    finally:
        reset_agent_run_service()
