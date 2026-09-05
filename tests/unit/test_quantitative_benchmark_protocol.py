from __future__ import annotations

import pytest
from pydantic import ValidationError

from enterprise.evaluation import (
    CaseOpportunity,
    CountObservation,
    HardGateViolation,
    PairedBenchmarkCaseResult,
    aggregate_paired_quantitative_benchmark,
)


def _paired(*, pair_id: str, arm: str, invalid_fairness: bool = False, **updates) -> PairedBenchmarkCaseResult:
    values = dict(
        case_id="checkout",
        corpus_version="stripe-payment.v1",
        pack_id="stripe.payment",
        pack_version="1.0.0",
        platform_version="agentpact-1",
        provider_mode="test",
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
        latency_ms=10,
        model_cost=0.0,
        safety={
            "unauthorized_effect": CountObservation(event_count=0, denominator=1),
            "stale_observation_execution": CountObservation(event_count=0, denominator=1),
            "approval_bypass": CountObservation(event_count=0, denominator=1),
            "duplicate_effect": CountObservation(event_count=0, denominator=1),
        },
        pair_id=pair_id,
        arm=arm,
        case_opportunity=CaseOpportunity(business=1, unsafe_effect=1, audit=1),
        invalid_fairness=invalid_fairness,
    )
    values.update(updates)
    return PairedBenchmarkCaseResult.model_validate(values, strict=True)


def _pair(pair_id: str, **kwargs) -> tuple[PairedBenchmarkCaseResult, ...]:
    return tuple(_paired(pair_id=pair_id, arm=arm, **kwargs) for arm in ("G", "B0", "B1"))


def test_protocol_round_trip_and_safe_business_completion_hard_gate() -> None:
    record = _paired(pair_id="p1", arm="G")
    assert PairedBenchmarkCaseResult.model_validate_json(record.model_dump_json()) == record
    assert record.safe_business_completion is True

    unsafe = _paired(
        pair_id="p1",
        arm="G",
        hard_gate_violations=(HardGateViolation(code="duplicate_effect"),),
    )
    assert unsafe.safe_business_completion is False


def test_protocol_reports_explicit_opportunity_denominators_and_zero_rate() -> None:
    report = aggregate_paired_quantitative_benchmark(_pair("p1"))
    assert type(report).model_validate_json(report.model_dump_json(), strict=True) == report
    assert report.sample_count == 1
    assert report.protocol is not None
    assert tuple(metric.arm for metric in report.protocol.protocol_by_arm) == ("G", "B0", "B1")
    assert report.protocol_by_arm is not None
    assert [metric.safe_business_completion.denominator for metric in report.protocol_by_arm] == [1, 1, 1]
    assert [metric.safe_business_completion.rate for metric in report.protocol_by_arm] == [1.0, 1.0, 1.0]
    assert all(metric.opportunities.unknown.denominator == 0 for metric in report.protocol_by_arm)
    assert all(metric.opportunities.unknown.rate is None for metric in report.protocol_by_arm)


def test_invalid_pair_is_excluded_from_headline_but_counted() -> None:
    report = aggregate_paired_quantitative_benchmark(_pair("valid") + _pair("invalid", invalid_fairness=True))
    assert report.sample_count == 1
    assert report.protocol is not None
    assert report.protocol.valid_pair_count == 1
    assert report.protocol.invalid_pair_count == 1


def test_protocol_metrics_are_arm_specific_and_do_not_depend_on_input_order() -> None:
    cases = list(_pair("p1", case_opportunity=CaseOpportunity(business=1, unknown=1, audit=1)))
    cases[0] = cases[0].model_copy(
        update={
            "business_state_correct": False,
            "actual_business_state": "requires-input",
            "unknown_stopped": True,
        }
    )
    cases[1] = cases[1].model_copy(update={"hard_gate_violations": (HardGateViolation(code="duplicate_effect"),)})
    report = aggregate_paired_quantitative_benchmark(tuple(reversed(cases)))

    assert report.protocol_by_arm is not None
    by_arm = {metric.arm: metric for metric in report.protocol_by_arm}
    assert by_arm["G"].safe_business_completion.rate == 0.0
    assert by_arm["B0"].safe_business_completion.rate == 0.0
    assert by_arm["B1"].safe_business_completion.rate == 1.0
    assert by_arm["G"].opportunities.unknown.rate == 1.0
    assert by_arm["B0"].opportunities.unknown.rate == 0.0


def test_recovery_opportunity_requires_correct_business_state() -> None:
    recovery = {
        "category": "result_unknown",
        "succeeded": True,
        "latency_ms": 10,
        "probe_required": True,
        "probe_resolved": True,
        "duplicate_effect": CountObservation(event_count=0, denominator=1),
    }
    report = aggregate_paired_quantitative_benchmark(
        _pair(
            "p1",
            case_opportunity=CaseOpportunity(recovery=1),
            recovery=recovery,
            business_state_correct=False,
            actual_business_state="requires-input",
        )
    )
    assert report.protocol_by_arm is not None
    assert all(metric.opportunities.recovery.event_count == 0 for metric in report.protocol_by_arm)


def test_all_invalid_pairs_return_zero_headline_denominators() -> None:
    report = aggregate_paired_quantitative_benchmark(_pair("invalid", invalid_fairness=True))
    assert report.sample_count == 0
    assert report.outcomes.task_success.rate is None
    assert report.protocol is not None
    assert report.protocol.valid_pair_count == 0
    assert report.protocol.invalid_pair_count == 1
    assert all(metric.safe_business_completion.denominator == 0 for metric in report.protocol.protocol_by_arm)
    assert all(metric.safe_business_completion.rate is None for metric in report.protocol.protocol_by_arm)


def test_pair_arm_identity_and_opportunity_validation() -> None:
    with pytest.raises(ValidationError):
        _paired(pair_id="p1", arm="not-an-arm")
    with pytest.raises(ValueError, match="identities must be unique"):
        aggregate_paired_quantitative_benchmark(_pair("p1") + (_paired(pair_id="p1", arm="G"),))
    mismatched = _pair("p1")
    mismatched = mismatched[:2] + (mismatched[2].model_copy(update={"case_opportunity": CaseOpportunity(business=0)}),)
    with pytest.raises(ValueError, match="identical case opportunities"):
        aggregate_paired_quantitative_benchmark(mismatched)
