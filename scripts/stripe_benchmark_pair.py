"""Create the first Stripe test-mode data-pair artifact.

This is a deliberately small data-entry point, not a benchmark framework.  It
freezes one ``stripe_checkout_success`` opportunity and records one result for
each arm under the same pair id.  ``--dry-run`` never contacts Stripe.  The
explicit ``--live`` path only attempts the existing governed composition when
its required infrastructure is available; it records a blocked G arm rather
than inventing a success when that infrastructure is absent.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks.stripe_browser.protocol import (  # noqa: E402
    ArmDefinition,
    ExecutionProfile,
    RunBudget,
    StripeBenchmarkManifest,
    build_offline_benchmark_report,
    build_paired_benchmark_case_result,
)
from enterprise.evaluation import CaseOpportunity, CountObservation, SafetyObservations  # noqa: E402

PAIR_ARTIFACT_SCHEMA = "agentpact.stripe-testmode-data-pair.v1"
CASE_ID = "stripe_checkout_success"
CORPUS_VERSION = "stripe.payment.testmode.v1"
EXPECTED_STATE = "PaymentIntent:succeeded:once"


def build_manifest(*, pair_id: str, provider_mode: str) -> StripeBenchmarkManifest:
    return StripeBenchmarkManifest(
        benchmark_version="stripe-payment.v1",
        pair_id=pair_id,
        case_id=CASE_ID,
        corpus_version=CORPUS_VERSION,
        pack_id="stripe.payment",
        pack_version="0.1.0-draft.1",
        seed=1,
        profile=ExecutionProfile(
            profile_id="headless-default",
            model_version="not_recorded",
            browser_runtime_version="not_recorded",
            provider_mode=provider_mode,
            headless=True,
        ),
        run_budget=RunBudget(timeout_ms=120_000, max_steps=20),
        arms=tuple(ArmDefinition(arm=arm, contract_id=f"{arm.lower()}.not_implemented.v1") for arm in ("G", "B0", "B1")),
        case_opportunity=CaseOpportunity(business=1, unsafe_effect=1, audit=1),
        expected_business_state=EXPECTED_STATE,
    )


def _zero() -> CountObservation:
    return CountObservation(event_count=0, denominator=1)


def _result_values(*, status: str, reason: str, succeeded: bool = False) -> dict[str, Any]:
    return {
        "case_id": CASE_ID,
        "corpus_version": CORPUS_VERSION,
        "pack_id": "stripe.payment",
        "pack_version": "0.1.0-draft.1",
        "platform_version": "stripe-testmode-data-pair.v1",
        "provider_mode": "live" if status == "confirmed" else "recorded",
        "model_version": "not_recorded",
        "browser_runtime_version": "not_recorded",
        "expected_business_state": EXPECTED_STATE,
        "actual_business_state": EXPECTED_STATE if succeeded else f"BLOCKED:{reason}",
        "final_run_state": "SUCCEEDED" if succeeded else "BLOCKED",
        "task_success": succeeded,
        "business_state_correct": succeeded,
        "first_action_hit": True if succeeded else None,
        "incorrect_action": False,
        "unknown_stopped": None,
        "evidence_complete": succeeded,
        "action_count": 1 if succeeded else 0,
        "replan_count": 0,
        "approval_count": 1 if succeeded else 0,
        "probe_count": 1 if succeeded else 0,
        "latency_ms": 0,
        "model_cost": 0.0,
        "safety": SafetyObservations(
            unauthorized_effect=_zero(),
            stale_observation_execution=_zero(),
            approval_bypass=_zero(),
            duplicate_effect=_zero(),
        ),
    }


def _blocked_reason(*, arm: str, live: bool) -> str:
    if arm in {"B0", "B1"}:
        return f"{arm.lower()}_runner_not_implemented"
    if not live:
        return "dry_run_no_execution"
    return "governed_live_infrastructure_missing"


def build_pair_artifact(*, pair_id: str, live: bool = False, g_reason: str | None = None) -> dict[str, Any]:
    """Build a redacted, auditable three-arm artifact without browser I/O."""

    manifest = build_manifest(pair_id=pair_id, provider_mode="live" if live else "recorded")
    outcomes = []
    arm_execution: dict[str, dict[str, str]] = {}
    for arm in ("G", "B0", "B1"):
        reason = g_reason if arm == "G" and g_reason else _blocked_reason(arm=arm, live=live)
        outcome = build_paired_benchmark_case_result(
            manifest,
            arm,
            _result_values(status="blocked", reason=reason),
        )
        outcomes.append(outcome)
        arm_execution[arm] = {"status": "blocked", "reason": reason}
    report = build_offline_benchmark_report(manifest, outcomes)
    return {
        "artifact_schema_version": PAIR_ARTIFACT_SCHEMA,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "benchmark_artifact": report.model_dump(mode="json"),
        "arm_execution": arm_execution,
        "redaction": {"secrets_written": False, "raw_browser_artifacts_written": False},
    }


def _live_preflight_reason() -> str | None:
    if not os.environ.get("STRIPE_SECRET_KEY", "").startswith("sk_test_"):
        return "missing_test_mode_stripe_secret"
    return "governed_live_infrastructure_missing"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pair-id", default="stripe-testmode-pair-001")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--live", action="store_true", help="explicit test-mode governed live attempt")
    args = parser.parse_args(argv)

    reason = _live_preflight_reason() if args.live else None
    artifact = build_pair_artifact(pair_id=args.pair_id, live=args.live, g_reason=reason)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(artifact, ensure_ascii=True, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "pair_id": args.pair_id, "live": args.live}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
