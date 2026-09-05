from __future__ import annotations

import pytest
from pydantic import ValidationError

from enterprise.evaluation import (
    ArmDefinition,
    BenchmarkCaseResult,
    CaseOpportunity,
    CountObservation,
    ExecutionProfile,
    OfflineBenchmarkReport,
    RunBudget,
    SafetyObservations,
    StripeBenchmarkManifest,
    build_offline_benchmark_report,
    build_paired_benchmark_case_result,
)


def _manifest() -> StripeBenchmarkManifest:
    return StripeBenchmarkManifest(
        benchmark_version="stripe-payment.v1",
        pair_id="pair-1",
        case_id="stripe_checkout_success",
        corpus_version="stripe.payment.testmode.v1",
        pack_id="stripe.payment",
        pack_version="1.0.0",
        seed=42,
        profile=ExecutionProfile(
            profile_id="headless-default",
            model_version="provider/model@1",
            browser_runtime_version="chromium@1",
            provider_mode="test",
        ),
        run_budget=RunBudget(timeout_ms=30_000, max_steps=20, retry_budget=1),
        arms=(
            ArmDefinition(arm="G", contract_id="governed.v1"),
            ArmDefinition(arm="B0", contract_id="prompt-only.v1"),
            ArmDefinition(arm="B1", contract_id="matched-tools.v1"),
        ),
        case_opportunity=CaseOpportunity(business=1, unsafe_effect=1, audit=1),
        expected_business_state="PaymentIntent:succeeded:once",
    )


def _outcome() -> BenchmarkCaseResult:
    zero = CountObservation(event_count=0, denominator=1)
    return BenchmarkCaseResult(
        case_id="stripe_checkout_success",
        corpus_version="stripe.payment.testmode.v1",
        pack_id="stripe.payment",
        pack_version="1.0.0",
        platform_version="agentpact-2",
        provider_mode="test",
        model_version="provider/model@1",
        browser_runtime_version="chromium@1",
        expected_business_state="PaymentIntent:succeeded:once",
        actual_business_state="PaymentIntent:succeeded:once",
        final_run_state="SUCCEEDED",
        task_success=True,
        business_state_correct=True,
        first_action_hit=True,
        incorrect_action=False,
        evidence_complete=True,
        action_count=1,
        replan_count=0,
        approval_count=0,
        probe_count=1,
        latency_ms=20,
        model_cost=0.01,
        safety=SafetyObservations(
            unauthorized_effect=zero,
            stale_observation_execution=zero,
            approval_bypass=zero,
            duplicate_effect=zero,
        ),
    )


def test_manifest_is_frozen_and_requires_all_arms() -> None:
    manifest = _manifest()
    assert manifest.manifest_digest.startswith("sha256:")
    with pytest.raises(ValidationError):
        manifest.seed = 99  # type: ignore[misc]
    with pytest.raises(ValidationError, match="G, B0, and B1"):
        StripeBenchmarkManifest.model_validate(
            {**manifest.model_dump(), "arms": manifest.arms[:2]}, strict=True
        )


def test_adapter_freezes_identity_and_round_trips_protocol_result() -> None:
    manifest = _manifest()
    result = build_paired_benchmark_case_result(manifest, "G", _outcome())
    assert result.pair_id == manifest.pair_id
    assert result.arm == "G"
    assert result.case_opportunity == manifest.case_opportunity
    assert type(result).model_validate_json(result.model_dump_json(), strict=True) == result

    with pytest.raises(ValueError, match="identity"):
        build_paired_benchmark_case_result(
            manifest,
            "B1",
            {**_outcome().model_dump(), "case_id": "other-case"},
        )


def test_offline_artifact_has_digest_backed_traceability_and_json_round_trip() -> None:
    manifest = _manifest()
    outcomes = tuple(build_paired_benchmark_case_result(manifest, arm, _outcome()) for arm in ("G", "B0", "B1"))
    artifact = build_offline_benchmark_report(manifest, outcomes)
    assert OfflineBenchmarkReport.from_json(artifact.to_json()) == artifact

    tampered = artifact.model_dump(mode="python")
    tampered["manifest_digest"] = "sha256:" + "0" * 64
    with pytest.raises(ValidationError, match="manifest_digest"):
        OfflineBenchmarkReport.model_validate(tampered, strict=True)

    tampered = artifact.model_dump(mode="python")
    tampered["outcomes"][0]["latency_ms"] = 999
    with pytest.raises(ValidationError, match="outcome_digest|report does not match"):
        OfflineBenchmarkReport.model_validate(tampered, strict=True)
