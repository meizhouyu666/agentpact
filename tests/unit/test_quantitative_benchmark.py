from __future__ import annotations

import pytest
from pydantic import ValidationError

from enterprise.evaluation import (
    CASE_SCHEMA_VERSION,
    REPORT_SCHEMA_VERSION,
    BenchmarkCaseResult,
    CountObservation,
    QuantitativeBenchmarkReport,
    RecoveryObservation,
    SafetyObservations,
    aggregate_quantitative_benchmark,
)


def _count(event_count: int = 0, denominator: int = 1) -> CountObservation:
    return CountObservation(event_count=event_count, denominator=denominator)


def _safety(
    *,
    unauthorized: CountObservation | None = None,
    stale: CountObservation | None = None,
    approval: CountObservation | None = None,
    duplicate: CountObservation | None = None,
) -> SafetyObservations:
    return SafetyObservations(
        unauthorized_effect=unauthorized or _count(denominator=4),
        stale_observation_execution=stale or _count(denominator=2),
        approval_bypass=approval or _count(denominator=1),
        duplicate_effect=duplicate or _count(denominator=1),
    )


def _case(
    case_id: str,
    *,
    pack_id: str,
    pack_version: str,
    platform_version: str,
    provider_mode: str = "recorded",
    expected: str = "COMPLETED",
    actual: str = "COMPLETED",
    task_success: bool = True,
    first_action_hit: bool | None = True,
    incorrect_action: bool = False,
    unknown_stopped: bool | None = None,
    evidence_complete: bool = True,
    action_count: int = 1,
    latency_ms: int = 100,
    model_cost: float = 0.1,
    safety: SafetyObservations | None = None,
    recovery: RecoveryObservation | None = None,
    model_version: str | None = None,
    browser_runtime_version: str | None = None,
) -> BenchmarkCaseResult:
    return BenchmarkCaseResult(
        case_id=case_id,
        corpus_version="platform-contracts.v1",
        pack_id=pack_id,
        pack_version=pack_version,
        platform_version=platform_version,
        provider_mode=provider_mode,
        model_version=model_version,
        browser_runtime_version=browser_runtime_version,
        expected_business_state=expected,
        actual_business_state=actual,
        final_run_state="SUCCEEDED" if task_success else "UNKNOWN",
        task_success=task_success,
        business_state_correct=expected == actual,
        first_action_hit=first_action_hit,
        incorrect_action=incorrect_action,
        unknown_stopped=unknown_stopped,
        evidence_complete=evidence_complete,
        action_count=action_count,
        replan_count=0,
        approval_count=1 if recovery is not None else 0,
        probe_count=1 if recovery is not None and recovery.probe_required else 0,
        latency_ms=latency_ms,
        model_cost=model_cost,
        safety=safety or _safety(),
        recovery=recovery,
    )


def _corpus() -> tuple[BenchmarkCaseResult, ...]:
    result_unknown_success = RecoveryObservation(
        category="result_unknown",
        succeeded=True,
        latency_ms=50,
        probe_required=True,
        probe_resolved=True,
        duplicate_effect=_count(denominator=1),
    )
    result_unknown_failure = RecoveryObservation(
        category="result_unknown",
        succeeded=False,
        latency_ms=100,
        probe_required=True,
        probe_resolved=False,
        duplicate_effect=_count(event_count=1, denominator=1),
    )
    explicit_failure = RecoveryObservation(
        category="explicit_failure",
        succeeded=True,
        latency_ms=25,
        probe_required=False,
        duplicate_effect=_count(denominator=0),
    )
    return (
        _case(
            "read-success",
            pack_id="fake.read",
            pack_version="1.0.0",
            platform_version="agentpact-1",
            action_count=2,
            latency_ms=100,
            model_cost=0.1,
            safety=_safety(approval=_count(denominator=0)),
        ),
        _case(
            "write-probe-confirmed",
            pack_id="fake.write",
            pack_version="2.0.0",
            platform_version="agentpact-1",
            unknown_stopped=True,
            action_count=3,
            latency_ms=200,
            model_cost=0.2,
            recovery=result_unknown_success,
            model_version="gpt-test-1",
            browser_runtime_version="chromium-1",
        ),
        _case(
            "write-probe-unresolved",
            pack_id="fake.write",
            pack_version="3.0.0",
            platform_version="agentpact-2",
            provider_mode="live",
            actual="UNKNOWN",
            task_success=False,
            first_action_hit=False,
            incorrect_action=True,
            unknown_stopped=True,
            evidence_complete=False,
            action_count=2,
            latency_ms=300,
            model_cost=0.3,
            safety=_safety(
                unauthorized=_count(event_count=1, denominator=4),
                stale=_count(event_count=1, denominator=2),
                approval=_count(event_count=1, denominator=1),
                duplicate=_count(event_count=1, denominator=1),
            ),
            recovery=result_unknown_failure,
            model_version="gpt-test-1",
            browser_runtime_version="chromium-2",
        ),
        _case(
            "write-explicit-failure-recovered",
            pack_id="fake.write",
            pack_version="2.0.0",
            platform_version="agentpact-2",
            first_action_hit=None,
            action_count=0,
            latency_ms=400,
            model_cost=0.4,
            safety=_safety(duplicate=_count(denominator=0)),
            recovery=explicit_failure,
            model_version="gpt-test-2",
        ),
    )


def test_quantitative_report_aggregates_outcomes_safety_recovery_and_dimensions() -> None:
    report = aggregate_quantitative_benchmark(_corpus())

    assert QuantitativeBenchmarkReport.model_validate_json(report.model_dump_json()) == report
    assert report.schema_version == REPORT_SCHEMA_VERSION
    assert report.sample_count == 4
    assert report.outcomes.task_success.model_dump() == {
        "event_count": 3,
        "denominator": 4,
        "rate": 0.75,
    }
    assert report.outcomes.business_state_correctness.rate == 0.75
    assert report.outcomes.first_action_hit.model_dump() == {
        "event_count": 2,
        "denominator": 3,
        "rate": 2 / 3,
    }
    assert report.outcomes.incorrect_action.rate == 0.25
    assert report.outcomes.unknown_stop.model_dump() == {
        "event_count": 2,
        "denominator": 2,
        "rate": 1.0,
    }
    assert report.outcomes.audit_completeness.rate == 0.75
    assert report.outcomes.total_action_count == 7
    assert report.outcomes.average_action_count == 1.75

    assert report.safety.unauthorized_effect.model_dump() == {
        "event_count": 1,
        "denominator": 16,
        "rate": 1 / 16,
    }
    assert report.safety.stale_observation_execution.rate == 1 / 8
    assert report.safety.approval_bypass.model_dump() == {
        "event_count": 1,
        "denominator": 3,
        "rate": 1 / 3,
    }
    assert report.safety.duplicate_effect.model_dump() == {
        "event_count": 1,
        "denominator": 3,
        "rate": 1 / 3,
    }

    assert report.latency.model_dump() == {
        "sample_count": 4,
        "average_ms": 250.0,
        "p50_ms": 250.0,
        "p95_ms": 385.0,
    }
    assert report.cost.total_model_cost == pytest.approx(1.0)
    assert report.cost.average_model_cost == pytest.approx(0.25)

    assert report.recovery.case_count == 3
    assert report.recovery.success.model_dump() == {
        "event_count": 2,
        "denominator": 3,
        "rate": 2 / 3,
    }
    assert report.recovery.probe_resolution.model_dump() == {
        "event_count": 1,
        "denominator": 2,
        "rate": 0.5,
    }
    assert report.recovery.duplicate_effect.rate == 0.5
    assert tuple(metric.category for metric in report.recovery.by_category) == (
        "explicit_failure",
        "result_unknown",
    )
    explicit, unknown = report.recovery.by_category
    assert explicit.probe_resolution.model_dump() == {
        "event_count": 0,
        "denominator": 0,
        "rate": None,
    }
    assert unknown.success.rate == 0.5
    assert unknown.probe_resolution.rate == 0.5
    assert unknown.latency.p50_ms == 75.0
    assert unknown.latency.p95_ms == 97.5

    dimensions = report.dimensions
    assert dimensions.corpus_versions == ("platform-contracts.v1",)
    assert dimensions.pack_ids == ("fake.read", "fake.write")
    assert tuple((item.pack_id, item.pack_version) for item in dimensions.pack_versions) == (
        ("fake.read", "1.0.0"),
        ("fake.write", "2.0.0"),
        ("fake.write", "3.0.0"),
    )
    assert dimensions.platform_versions == ("agentpact-1", "agentpact-2")
    assert dimensions.provider_modes == ("live", "recorded")
    assert dimensions.model_versions == ("gpt-test-1", "gpt-test-2")
    assert dimensions.browser_runtime_versions == ("chromium-1", "chromium-2")
    assert dimensions.distinct_pack_count == 2
    assert dimensions.distinct_pack_version_count == 3
    assert dimensions.distinct_platform_version_count == 2


def test_quantitative_case_schema_and_denominators_fail_closed() -> None:
    assert _corpus()[0].schema_version == CASE_SCHEMA_VERSION

    with pytest.raises(ValueError, match="at least one case"):
        aggregate_quantitative_benchmark(())

    first = _corpus()[0]
    no_recovery = aggregate_quantitative_benchmark((first,))
    assert no_recovery.recovery.case_count == 0
    assert no_recovery.recovery.success.model_dump() == {
        "event_count": 0,
        "denominator": 0,
        "rate": None,
    }

    domain_evaluated = BenchmarkCaseResult.model_validate(
        {
            **first.model_dump(),
            "expected_business_state": "invoice-is-paid",
            "actual_business_state": "invoice.status=paid",
            "business_state_correct": True,
        },
        strict=True,
    )
    assert domain_evaluated.business_state_correct is True

    with pytest.raises(ValueError, match="identities must be unique"):
        aggregate_quantitative_benchmark((first, first))

    different_corpus = first.model_copy(
        update={"case_id": "read-success-v2", "corpus_version": "platform-contracts.v2"}
    )
    with pytest.raises(ValueError, match="exactly one corpus_version"):
        aggregate_quantitative_benchmark((first, different_corpus))

    with pytest.raises(ValidationError, match="event_count cannot exceed"):
        CountObservation(event_count=2, denominator=1)

    with pytest.raises(ValidationError, match="probe_resolved must be present"):
        RecoveryObservation(
            category="result_unknown",
            succeeded=False,
            latency_ms=10,
            probe_required=True,
            duplicate_effect=_count(denominator=0),
        )

    with pytest.raises(ValidationError, match="int_type"):
        BenchmarkCaseResult.model_validate(
            {
                **first.model_dump(),
                "action_count": "2",
            },
            strict=True,
        )

    with pytest.raises(ValidationError, match="literal_error"):
        BenchmarkCaseResult.model_validate(
            {
                **first.model_dump(),
                "schema_version": "agentpact.quantitative-benchmark.case.v2",
            },
            strict=True,
        )

    valid_report = aggregate_quantitative_benchmark(_corpus())
    invalid_cost = valid_report.model_dump()
    invalid_cost["cost"]["average_model_cost"] = 999.0
    with pytest.raises(ValidationError, match="average model cost"):
        type(valid_report).model_validate(invalid_cost, strict=True)

    invalid_latency = valid_report.model_dump()
    invalid_latency["latency"]["p50_ms"] = 500.0
    with pytest.raises(ValidationError, match="p50 latency"):
        type(valid_report).model_validate(invalid_latency, strict=True)

    invalid_actions = valid_report.model_dump()
    invalid_actions["outcomes"]["average_action_count"] = 999.0
    with pytest.raises(ValidationError, match="average action count"):
        type(valid_report).model_validate(invalid_actions, strict=True)

    invalid_corpus = valid_report.model_dump()
    invalid_corpus["dimensions"]["corpus_versions"] = ("platform-contracts.v1", "v2")
    with pytest.raises(ValidationError, match="exactly one corpus_version"):
        type(valid_report).model_validate(invalid_corpus, strict=True)

    invalid_probe_denominator = valid_report.model_dump()
    invalid_probe_denominator["recovery"]["probe_resolution"] = {
        "event_count": 0,
        "denominator": 99,
        "rate": 0.0,
    }
    with pytest.raises(ValidationError, match="probe denominator"):
        type(valid_report).model_validate(invalid_probe_denominator, strict=True)

    invalid_category_probe_denominator = valid_report.model_dump()
    invalid_category_probe_denominator["recovery"]["by_category"][0]["probe_resolution"] = {
        "event_count": 0,
        "denominator": 2,
        "rate": 0.0,
    }
    with pytest.raises(ValidationError, match="category probe denominator"):
        type(valid_report).model_validate(invalid_category_probe_denominator, strict=True)
