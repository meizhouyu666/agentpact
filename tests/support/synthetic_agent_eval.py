"""Test-only Synthetic planner evaluation harness."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from enterprise.agent.constrained_planner import OpenAICompatiblePlanner, PlannerTransport
from enterprise.auth.schemas import DepartmentRole, UserContext
from enterprise.domains.synthetic_payment.constants import (
    BUSINESS_LINE_ID,
    PAYMENTS_DEPARTMENT_ID,
    TENANT_ID,
)
from enterprise.domains.synthetic_payment.m9_runtime import (
    AgentEvalReport,
    M9StepRole,
    OpenAICompatibleM9Provider,
    build_m9_plan_input,
    build_m9_replan_input,
    load_agent_eval_cases,
    redact_replan_evidence,
    run_agent_eval,
)
from enterprise.domains.synthetic_payment.m10_runtime import SyntheticPaymentRuntimeAdapter

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
RECORDED_CASES = FIXTURES / "m9_agent_eval_cases.json"
LIVE_CASES = FIXTURES / "m12_live_eval_cases.json"


def _context() -> tuple[object, object, object, object]:
    def no_session() -> object:
        raise AssertionError("Synthetic evaluation must not touch persistence")

    class NoEffectDriver:
        async def execute(self, **_trusted_inputs: object) -> object:
            raise AssertionError("Synthetic evaluation must not execute a business effect")

        async def probe(self, **_trusted_inputs: object) -> object:
            raise AssertionError("Synthetic evaluation must not use a browser Probe")

    user = UserContext(
        user_id="m12-eval-operator",
        org_id=TENANT_ID,
        department_roles=[
            DepartmentRole(
                department_id=PAYMENTS_DEPARTMENT_ID,
                department_name="Synthetic payments",
                role="operator",
            )
        ],
        business_line_ids=[BUSINESS_LINE_ID],
    )
    adapter = SyntheticPaymentRuntimeAdapter(no_session, driver=NoEffectDriver())  # type: ignore[arg-type]
    prepared = adapter.prepare_run(
        user=user,
        tenant_id=TENANT_ID,
        request_id="m12-eval-context",
        intent_digest="1" * 64,
        business_inputs={
            "payment_id": "trusted-eval-payment",
            "beneficiary_id": "trusted-eval-beneficiary",
            "amount": "1.00",
            "currency": "CNY",
            "reference": "trusted-eval-reference",
            "object_version": 1,
        },
        target_url="http://127.0.0.1.invalid",
        now=datetime(2026, 8, 2, tzinfo=timezone.utc),
    )
    authority = prepared.compilation.authority
    plan_input = build_m9_plan_input(authority, intent_summary="Evaluate one model-safe synthetic plan")
    replan_input = build_m9_replan_input(
        prepared.compilation,
        completed_prefix_length=1,
        remaining_replans=1,
        evidence_tokens=(
            redact_replan_evidence(
                mismatch_code="BUSINESS_STATE_CHANGED",
                step_role=M9StepRole.SUBMIT,
                raw_evidence="m12-eval-redacted",
            ),
        ),
    )
    return authority, prepared.compilation, plan_input, replan_input


def evaluate(
    mode: Literal["recorded", "live"],
    *,
    transport: PlannerTransport | None = None,
    endpoint: str | None = None,
    model: str | None = None,
    api_key_env: str = "OPENAI_COMPATIBLE_API_KEY",
) -> AgentEvalReport:
    authority, previous, plan_input, replan_input = _context()
    if mode == "recorded":
        cases = load_agent_eval_cases(RECORDED_CASES)
        return run_agent_eval(
            cases,
            plan_input=plan_input,
            replan_input=replan_input,
            authority=authority,  # type: ignore[arg-type]
            previous=previous,  # type: ignore[arg-type]
        )
    if not endpoint or not model or not os.environ.get(api_key_env):
        raise ValueError("Live evaluation configuration is incomplete")
    cases = load_agent_eval_cases(LIVE_CASES)
    planner = OpenAICompatiblePlanner(
        endpoint=endpoint,
        model=model,
        api_key_env=api_key_env,
        transport=transport,
    )
    return run_agent_eval(
        cases,
        plan_input=plan_input,
        replan_input=replan_input,
        authority=authority,  # type: ignore[arg-type]
        previous=previous,  # type: ignore[arg-type]
        provider_factory=lambda _case: OpenAICompatibleM9Provider(planner),
        provider_mode="live",
    )


def canonical_json(report: AgentEvalReport) -> bytes:
    return (
        json.dumps(report.model_dump(mode="json"), ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
