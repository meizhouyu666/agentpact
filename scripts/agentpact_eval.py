"""M12 deterministic recorded and explicit planning-only live evaluation CLI."""

# ruff: noqa: E402, I001

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from enterprise.agent.constrained_planner import OpenAICompatiblePlanner, PlannerTransport
from enterprise.auth.schemas import DepartmentRole, UserContext
from enterprise.domains.synthetic_payment.constants import (
    BUSINESS_LINE_ID,
    PAYMENTS_DEPARTMENT_ID,
    TENANT_ID,
)
from tests.fixtures.synthetic_payment_runtime.m10_runtime import SyntheticPaymentRuntimeAdapter
from tests.fixtures.synthetic_payment_runtime.m9_runtime import (
    AgentEvalReport,
    M9StepRole,
    OpenAICompatibleM9Provider,
    build_m9_plan_input,
    build_m9_replan_input,
    load_agent_eval_cases,
    redact_replan_evidence,
    run_agent_eval,
)

RECORDED_CASES = ROOT / "tests" / "fixtures" / "m9_agent_eval_cases.json"
LIVE_CASES = ROOT / "tests" / "fixtures" / "m12_live_eval_cases.json"
ARTIFACT_DIR = ROOT / "artifacts" / "m12"


def _context() -> tuple[object, object, object, object]:
    def no_session() -> object:
        raise AssertionError("M12 evaluation is planning-only and must not touch persistence")

    class NoEffectDriver:
        async def execute(self, **_trusted_inputs: object) -> object:
            raise AssertionError("M12 evaluation must not execute a business effect")

        async def probe(self, **_trusted_inputs: object) -> object:
            raise AssertionError("M12 evaluation must not use a browser Probe")

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


def markdown(report: AgentEvalReport) -> bytes:
    rows = [
        "# AgentPact M12 Agent evaluation",
        "",
        f"Digest: `{report.report_digest}`",
        "",
        "| Case | Kind | Expected | Actual | Compile | Calls | Repairs | Pass |",
        "|---|---|---|---|---|---:|---:|---|",
    ]
    rows.extend(
        f"| {item.case_id} | {item.kind} | {item.expected_disposition.value} | "
        f"{item.actual_disposition.value} | {item.trusted_compile_result} | {item.provider_calls} | "
        f"{item.repair_count} | {'yes' if item.passed else 'no'} |"
        for item in report.cases
    )
    rows.extend(("", "## Limitations", ""))
    rows.extend(f"- {item}" for item in report.limitations)
    return ("\n".join(rows) + "\n").encode("utf-8")


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def write_artifacts(report: AgentEvalReport, *, mode: Literal["recorded", "live"]) -> tuple[Path, Path]:
    json_path = ARTIFACT_DIR / f"agent-eval-{mode}.json"
    markdown_path = ARTIFACT_DIR / f"agent-eval-{mode}.md"
    _atomic_write(json_path, canonical_json(report))
    _atomic_write(markdown_path, markdown(report))
    return json_path, markdown_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("recorded", "live"))
    args = parser.parse_args()
    if args.mode == "recorded":
        report = evaluate("recorded")
    else:
        report = evaluate(
            "live",
            endpoint=os.environ.get("OPENAI_COMPATIBLE_API_BASE"),
            model=os.environ.get("OPENAI_COMPATIBLE_MODEL_NAME"),
        )
    paths = write_artifacts(report, mode=args.mode)
    print(json.dumps({"digest": report.report_digest, "artifacts": [str(item) for item in paths]}, sort_keys=True))
    return 0 if report.passed_case_count == report.case_count else 1


if __name__ == "__main__":
    raise SystemExit(main())
