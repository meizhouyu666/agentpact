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
    AgentRunPlanStep,
    AgentRunProjection,
    AgentRunState,
)
from enterprise.auth.dependencies import get_current_user
from enterprise.auth.schemas import DepartmentRole, UserContext
from enterprise.domains.synthetic_payment.constants import (
    BUSINESS_LINE_ID,
    PAYMENTS_DEPARTMENT_ID,
    TENANT_ID,
)
from enterprise.domains.synthetic_payment.m10_runtime import (
    SyntheticPaymentRuntimeAdapter,
    derive_agent_run_id,
)
from enterprise.domains.synthetic_payment.m6_runtime import SYNTHETIC_RUNTIME_CONTRACT
from enterprise.domains.synthetic_payment.sdk_manifest import build_pack_sdk_manifest
from enterprise.governance.contracts import ActionIntent, DecisionOutcome, ExecutionEffect, PolicyDecision
from enterprise.governance.pack_runtime import PackRuntimeBinding, PackRuntimeRegistry
from skyvern.forge.native_action import NativeActionDisposition, NativeActionResolution

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
            run_id="run_m10_route",
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
    finally:
        reset_agent_run_service()
