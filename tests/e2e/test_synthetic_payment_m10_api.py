"""Recorded-provider M10 proof through the mounted application composition."""

# ruff: noqa: E402, I001

from __future__ import annotations

import asyncio
import json
from collections import Counter
from unittest.mock import patch

from tests.e2e import m4_synthetic_support as support

import httpx
import pytest
from fastapi import FastAPI, Header
from sqlalchemy import select

from enterprise.agent_runs.routes import reset_agent_run_service
from enterprise.approval.models import ApprovalRequestModel
from enterprise.auth.dependencies import get_current_user
from enterprise.auth.models import BusinessLineModel, DepartmentModel
from enterprise.auth.schemas import DepartmentRole, UserContext
from enterprise.domains.synthetic_payment.constants import BUSINESS_LINE_ID, PAYMENTS_DEPARTMENT_ID
from enterprise.domains.synthetic_payment.agent_run_composition import mount_synthetic_agent_run_api
from enterprise.domains.synthetic_payment.m10_runtime import SyntheticPaymentRuntimeAdapter
from enterprise.governance.models import (
    ExecutionAttemptModel,
    ExecutionPermitModel,
    GovernanceAuditEventModel,
    PendingActionModel,
)
from skyvern.forge.sdk.db.models import OrganizationModel

ORGANIZATION_ID = "org-m10-agent-run-api"
INPUTS = {
    "payment_id": "pay-demo-001",
    "beneficiary_id": "vendor-demo-001",
    "amount": "5000.00",
    "currency": "CNY",
    "reference": "Synthetic invoice 001",
    "object_version": 1,
}


def _user(role: str) -> UserContext:
    return UserContext(
        user_id=f"m10-api-{role}",
        org_id=ORGANIZATION_ID,
        department_roles=[
            DepartmentRole(
                department_id=PAYMENTS_DEPARTMENT_ID,
                department_name="Synthetic payments",
                role=role,
            )
        ],
        business_line_ids=[BUSINESS_LINE_ID],
    )


@pytest.mark.e2e
def test_m10_recorded_api_uses_boot_driver_reobserves_permits_and_probes_once() -> None:
    with support.isolated_m4_environment() as environment:
        database = support.M4Database(environment.database_url)
        proposal_calls: list[str] = []
        original_prepare = SyntheticPaymentRuntimeAdapter.prepare_run

        def counted_prepare(self: SyntheticPaymentRuntimeAdapter, request=None, **trusted_inputs: object):
            request_id = request.request_id if request is not None else trusted_inputs["request_id"]
            proposal_calls.append(str(request_id))
            return original_prepare(self, request, **trusted_inputs)

        async def scenario() -> dict[str, object]:
            try:
                async with database.Session() as session:
                    session.add(OrganizationModel(organization_id=ORGANIZATION_ID, organization_name="M10 API E2E"))
                    await session.flush()
                    session.add(
                        DepartmentModel(
                            department_id=PAYMENTS_DEPARTMENT_ID,
                            organization_id=ORGANIZATION_ID,
                            department_name="Synthetic payments",
                            department_code="synthetic-payments",
                        )
                    )
                    session.add(
                        BusinessLineModel(
                            business_line_id=BUSINESS_LINE_ID,
                            organization_id=ORGANIZATION_ID,
                            line_name="Synthetic payments",
                            line_code="synthetic-payments",
                        )
                    )
                    await session.commit()

                async with support.real_chromium(environment.console_url, environment.cleanup) as browser:
                    with support.configured_forge_boundary(database, browser.state):
                        application = FastAPI()
                        mount_synthetic_agent_run_api(
                            application,
                            session_factory=database.Session,
                            target_url=environment.console_url,
                            hmac_secret=support.HMAC_SECRET,
                            provider_mode="recorded",
                        )

                        async def current_user(x_test_role: str = Header(default="operator")) -> UserContext:
                            return _user("approver" if x_test_role == "approver" else "operator")

                        application.dependency_overrides[get_current_user] = current_user
                        transport = httpx.ASGITransport(app=application)
                        async with httpx.AsyncClient(transport=transport, base_url="http://m10.local") as client:
                            request = {
                                "request_id": "m10-api-e2e-001",
                                "intent": "Submit the approved synthetic payment",
                                "business_inputs": INPUTS,
                            }
                            concurrent_create = await asyncio.gather(
                                client.post("/api/v1/enterprise/agent-runs/", json=request),
                                client.post("/api/v1/enterprise/agent-runs/", json=request),
                            )
                            assert [item.status_code for item in concurrent_create] == [200, 200]
                            assert concurrent_create[0].json() == concurrent_create[1].json()
                            created = concurrent_create[0]
                            assert created.status_code == 200, created.text
                            assert created.json()["state"] == "AWAITING_APPROVAL"
                            assert created.json()["legal_actions"] == ["approve", "reject"]
                            assert created.json()["pack_id"] == "synthetic.payment"
                            assert created.json()["pack_display_name"] == "Synthetic Payment Reference Pack"
                            assert created.json()["provider_mode"] == "recorded"
                            assert all(
                                value not in json.dumps(created.json(), sort_keys=True)
                                for value in INPUTS.values()
                                if isinstance(value, str)
                            )

                            repeated_create = await client.post("/api/v1/enterprise/agent-runs/", json=request)
                            assert repeated_create.json() == created.json()
                            conflicting = await client.post(
                                "/api/v1/enterprise/agent-runs/",
                                json={**request, "intent": "A different use of the same request id"},
                            )
                            assert conflicting.status_code == 409
                            assert conflicting.json() == {"detail": {"code": "IDEMPOTENCY_CONFLICT"}}
                            listed = await client.get("/api/v1/enterprise/agent-runs/?limit=20")
                            assert listed.status_code == 200, listed.text
                            assert listed.json()["items"][0]["run_id"] == created.json()["run_id"]
                            assert listed.json()["items"][0]["provider_mode"] == "recorded"
                            assert "legal_actions" not in listed.json()["items"][0]
                            assert "plan" not in listed.json()["items"][0]
                            async with database.Session() as session:
                                assert len(list((await session.scalars(select(ApprovalRequestModel))).all())) == 1
                                assert len(list((await session.scalars(select(ExecutionPermitModel))).all())) == 0
                                assert len(list((await session.scalars(select(ExecutionAttemptModel))).all())) == 0

                            run_id = created.json()["run_id"]
                            initial_trace = await client.get(
                                f"/api/v1/enterprise/agent-runs/{run_id}/decision-trace"
                            )
                            assert initial_trace.status_code == 200, initial_trace.text
                            assert initial_trace.json()["non_authoritative"] is True
                            assert "legal_actions" not in initial_trace.text
                            initial_stages = {item["stage"]: item for item in initial_trace.json()["stages"]}
                            assert initial_stages["provider"]["status"] == "completed"
                            assert initial_stages["provider"]["provider_calls"] == 1
                            assert initial_stages["approval"]["status"] == "active"
                            restart_application = FastAPI()
                            mount_synthetic_agent_run_api(
                                restart_application,
                                session_factory=database.Session,
                                target_url=environment.console_url,
                                hmac_secret=support.HMAC_SECRET,
                                provider_mode="recorded",
                            )
                            restarted = await client.get(f"/api/v1/enterprise/agent-runs/{run_id}")
                            assert restarted.status_code == 200, restarted.text
                            assert restarted.json() == created.json()

                            invalid_request = {
                                **request,
                                "request_id": "m11-api-lock-release",
                                "business_inputs": {**INPUTS, "amount": "not-a-decimal"},
                            }
                            for _ in range(2):
                                invalid = await client.post("/api/v1/enterprise/agent-runs/", json=invalid_request)
                                assert invalid.status_code == 422
                                assert invalid.json() == {"detail": {"code": "PLANNER_REJECTED"}}

                            approved = await client.post(
                                f"/api/v1/enterprise/agent-runs/{run_id}/approve",
                                headers={"x-test-role": "approver"},
                                json={"operation_key": "m10-api-approve-001"},
                            )
                            assert approved.status_code == 200, approved.text
                            assert approved.json()["state"] == "UNKNOWN"
                            assert approved.json()["legal_actions"] == ["probe"]
                            unknown_trace = await client.get(
                                f"/api/v1/enterprise/agent-runs/{run_id}/decision-trace"
                            )
                            unknown_stages = {item["stage"]: item for item in unknown_trace.json()["stages"]}
                            assert unknown_stages["approval"]["status"] == "completed"
                            assert unknown_stages["execution"]["status"] == "blocked"
                            assert unknown_stages["recovery"]["status"] == "blocked"

                            probed = await client.post(
                                f"/api/v1/enterprise/agent-runs/{run_id}/probe",
                                json={"operation_key": "m10-api-probe-001"},
                            )
                            assert probed.status_code == 200, probed.text
                            assert probed.json()["state"] == "SUCCEEDED"
                            repeated_probe = await client.post(
                                f"/api/v1/enterprise/agent-runs/{run_id}/probe",
                                json={"operation_key": "m10-api-probe-001"},
                            )
                            assert repeated_probe.json() == probed.json()
                            completed_trace = await client.get(
                                f"/api/v1/enterprise/agent-runs/{run_id}/decision-trace"
                            )
                            completed_stages = {
                                item["stage"]: item for item in completed_trace.json()["stages"]
                            }
                            assert completed_stages["execution"]["status"] == "completed"
                            assert completed_stages["recovery"]["status"] == "completed"

                            report = await client.get(f"/api/v1/enterprise/agent-runs/{run_id}/report")
                            assert report.status_code == 200, report.text
                            assert report.json()["schema_version"] == "agentpact-agent-run-report/v2"
                            assert report.json()["projection"]["provider_mode"] == "recorded"
                            assert report.json()["decision_trace"] == completed_trace.json()
                            assert all(
                                value not in json.dumps(report.json(), sort_keys=True)
                                for value in INPUTS.values()
                                if isinstance(value, str)
                            )

                    async with database.Session() as session:
                        permits = list((await session.scalars(select(ExecutionPermitModel))).all())
                        attempts = list((await session.scalars(select(ExecutionAttemptModel))).all())
                        pending = list((await session.scalars(select(PendingActionModel))).all())
                        browser_events = list(
                            (
                                await session.scalars(
                                    select(GovernanceAuditEventModel).where(
                                        GovernanceAuditEventModel.event_type.like("browser.loop.%")
                                    )
                                )
                            ).all()
                        )
                    assert Counter(event.event_type for event in browser_events) == {
                        "browser.loop.observation": 2,
                        "browser.loop.decision": 2,
                        "browser.loop.verification": 2,
                        "browser.loop.terminal": 2,
                    }
                    assert len({event.task_id for event in browser_events}) == 2
                    assert all(event.action_fingerprint is None for event in browser_events)
                    return {
                        "fresh_observation": pending[0].observation_hash != permits[0].observation_hash,
                        "effect_count": len(attempts),
                        "permit_statuses": [item.status for item in permits],
                        "attempt_statuses": [item.status for item in attempts],
                        "pending_statuses": [item.status for item in pending],
                    }
            finally:
                reset_agent_run_service()
                await database.engine.dispose()

        with patch.object(SyntheticPaymentRuntimeAdapter, "prepare_run", counted_prepare):
            evidence = asyncio.run(scenario())
        assert Counter(proposal_calls) == {
            "m10-api-e2e-001": 1,
            "m11-api-lock-release": 2,
        }
        assert evidence == {
            "fresh_observation": True,
            "effect_count": 1,
            "permit_statuses": ["consumed"],
            "attempt_statuses": ["confirmed"],
            "pending_statuses": ["invalidated"],
        }
