from __future__ import annotations

import pytest
from pydantic import ValidationError

from enterprise.evaluation import (
    ArmDefinition,
    BenchmarkCaseResult,
    CaseOpportunity,
    CountObservation,
    ExecutionProfile,
    InMemoryResultSink,
    RunBudget,
    SafetyObservations,
    StripeBenchmarkManifest,
    StripeBenchmarkRunnerConfig,
    compose_stripe_benchmark_runner,
    inject_stripe_test_secret,
    validate_stripe_test_secret,
)


def _manifest() -> StripeBenchmarkManifest:
    return StripeBenchmarkManifest(
        benchmark_version="stripe-payment.v1",
        pair_id="pair-1",
        case_id="checkout",
        corpus_version="stripe.payment.testmode.v1",
        pack_id="stripe.payment",
        pack_version="0.1.0-draft.1",
        seed=1,
        profile=ExecutionProfile(
            profile_id="headless-default",
            model_version="recorded",
            browser_runtime_version="protocol-only",
            provider_mode="recorded",
        ),
        run_budget=RunBudget(timeout_ms=1000, max_steps=1),
        arms=tuple(ArmDefinition(arm=arm, contract_id=f"{arm}.v1") for arm in ("G", "B0", "B1")),
        case_opportunity=CaseOpportunity(business=1, audit=1),
        expected_business_state="succeeded-once",
    )


def _outcome() -> BenchmarkCaseResult:
    zero = CountObservation(event_count=0, denominator=1)
    return BenchmarkCaseResult(
        case_id="checkout",
        corpus_version="stripe.payment.testmode.v1",
        pack_id="stripe.payment",
        pack_version="0.1.0-draft.1",
        platform_version="test",
        provider_mode="recorded",
        model_version="recorded",
        browser_runtime_version="protocol-only",
        expected_business_state="succeeded-once",
        actual_business_state="succeeded-once",
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
        latency_ms=1,
        model_cost=0.0,
        safety=SafetyObservations(
            unauthorized_effect=zero,
            stale_observation_execution=zero,
            approval_bypass=zero,
            duplicate_effect=zero,
        ),
    )


def test_config_defaults_headless_and_rejects_production_or_enforce() -> None:
    config = StripeBenchmarkRunnerConfig()
    assert config.headless is True
    assert config.arm == "G"
    assert config.production_eligible is False
    with pytest.raises(ValidationError):
        StripeBenchmarkRunnerConfig(enforce=True)
    with pytest.raises(ValidationError):
        StripeBenchmarkRunnerConfig(stripe_mode="live")
    with pytest.raises(ValidationError):
        StripeBenchmarkRunnerConfig(production_eligible=True)


def test_arm_isolation_and_config_round_trip() -> None:
    config = StripeBenchmarkRunnerConfig(arm="B1")
    assert StripeBenchmarkRunnerConfig.model_validate(config.model_dump(), strict=True) == config
    sink = InMemoryResultSink()
    result = compose_stripe_benchmark_runner(config, result_sink=sink).record(_manifest(), _outcome())
    assert result.arm == "B1"
    assert next(iter(sink.results.values()))["arm"] == "B1"


def test_secret_boundary_accepts_only_test_key_and_never_persists_it(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "sk_test_runner_secret"
    monkeypatch.setenv("STRIPE_TEST_KEY", secret)
    config = StripeBenchmarkRunnerConfig(secret_env_key="STRIPE_TEST_KEY")
    assert inject_stripe_test_secret(config) == secret
    assert validate_stripe_test_secret(secret) == secret
    with pytest.raises(ValueError):
        validate_stripe_test_secret("sk_live_production")
    sink = InMemoryResultSink(secrets=(secret,))
    sink.put_result(compose_stripe_benchmark_runner(result_sink=sink).record(_manifest(), _outcome()))
    assert secret not in str(sink.results)


def test_recorded_runner_does_not_implement_execution() -> None:
    with pytest.raises(NotImplementedError):
        compose_stripe_benchmark_runner().run(_manifest())
