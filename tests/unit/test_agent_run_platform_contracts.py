"""Pack-neutral Agent Run schema, approval, and HTTP contracts."""

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
from enterprise.governance.contracts import ActionIntent, DecisionOutcome, ExecutionEffect, PolicyDecision
from skyvern.forge.native_action import NativeActionDisposition, NativeActionResolution
from tests.fixtures.fake_domain_pack import (
    FAKE_BUSINESS_INPUTS,
    FAKE_PACK_DISPLAY_NAME,
    FAKE_PACK_ID,
    FAKE_PACK_VERSION,
)

FAKE_TENANT_ID = "tenant-fake-domain"
FAKE_DEPARTMENT_ID = "department-fake-operations"
FAKE_BUSINESS_LINE_ID = "business-line-fake-domain"


def _user() -> UserContext:
    return UserContext(
        user_id="fake-domain-operator",
        org_id=FAKE_TENANT_ID,
        department_roles=[
            DepartmentRole(
                department_id=FAKE_DEPARTMENT_ID,
                department_name="Fake domain operations",
                role="operator",
            )
        ],
        business_line_ids=[FAKE_BUSINESS_LINE_ID],
    )


def test_public_create_schema_forbids_identity_provider_authority_and_browser_fields() -> None:
    AgentRunCreateRequest(
        request_id="req-1",
        intent="Execute",
        business_inputs=FAKE_BUSINESS_INPUTS,
        pack_id=FAKE_PACK_ID,
        pack_version=FAKE_PACK_VERSION,
    )
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
                {
                    "request_id": "req-1",
                    "intent": "Execute",
                    "business_inputs": FAKE_BUSINESS_INPUTS,
                    forbidden: "forged",
                }
            )


def test_approval_required_resolution_has_exact_correlation_and_no_permit() -> None:
    intent = ActionIntent(
        intent_id="intent-1",
        task_id="task-1",
        step_id="step-1",
        action_fingerprint="b" * 64,
        observation_id="c" * 64,
        operation="execute",
        effect=ExecutionEffect.EXTERNAL_WRITE,
    )
    decision = PolicyDecision(
        decision_id="decision-1",
        intent_id=intent.intent_id,
        outcome=DecisionOutcome.REQUIRE_APPROVAL,
        risk_level="high",
        required_approver={"department_id": FAKE_DEPARTMENT_ID, "role": "approver"},
        policy_version="fake-domain-policy-v1",
    )
    resolution = NativeActionResolution(
        disposition=NativeActionDisposition.APPROVAL_REQUIRED,
        operation="execute",
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
            run_id="run_fake_route",
            pack_id=FAKE_PACK_ID,
            pack_version=FAKE_PACK_VERSION,
            pack_display_name=FAKE_PACK_DISPLAY_NAME,
            provider_mode="recorded",
            state=AgentRunState.AWAITING_APPROVAL,
            legal_actions=(AgentRunAction.APPROVE, AgentRunAction.REJECT),
            plan=(AgentRunPlanStep(sequence=1, role="execute", state="active"),),
            completed_steps=0,
            total_steps=1,
        )

    async def list_runs(self, *, user, cursor=None, limit=20):
        del user, cursor, limit
        return AgentRunPage(
            items=(
                AgentRunSummary(
                    run_id="run_fake_route",
                    pack_id=FAKE_PACK_ID,
                    pack_version=FAKE_PACK_VERSION,
                    pack_display_name=FAKE_PACK_DISPLAY_NAME,
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
            json={
                "request_id": "req-route",
                "intent": "Execute",
                "business_inputs": FAKE_BUSINESS_INPUTS,
                "pack_id": FAKE_PACK_ID,
                "pack_version": FAKE_PACK_VERSION,
            },
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["state"] == "AWAITING_APPROVAL"
        assert payload["pack_id"] == FAKE_PACK_ID
        assert payload["pack_display_name"] == FAKE_PACK_DISPLAY_NAME
        assert payload["provider_mode"] == "recorded"
        encoded = json.dumps(payload, sort_keys=True)
        assert all(value not in encoded for value in FAKE_BUSINESS_INPUTS.values() if isinstance(value, str))

        forbidden = client.post(
            "/api/v1/enterprise/agent-runs/",
            json={
                "request_id": "req-route",
                "intent": "Execute",
                "business_inputs": FAKE_BUSINESS_INPUTS,
                "provider_mode": "live",
            },
        )
        assert forbidden.status_code == 422

        listed = client.get("/api/v1/enterprise/agent-runs/")
        assert listed.status_code == 200
        assert listed.json()["items"][0]["run_id"] == "run_fake_route"
        assert "legal_actions" not in listed.json()["items"][0]
        assert "plan" not in listed.json()["items"][0]

        trace = client.get("/api/v1/enterprise/agent-runs/run_fake_route/decision-trace")
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
