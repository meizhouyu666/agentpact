"""Versioned case records and deterministic quantitative benchmark aggregation."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Sequence
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

CASE_SCHEMA_VERSION = "agentpact.quantitative-benchmark.case.v1"
REPORT_SCHEMA_VERSION = "agentpact.quantitative-benchmark.report.v1"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)


class CountObservation(_StrictModel):
    """One explicitly-denominated event observation from a benchmark case."""

    event_count: int = Field(ge=0)
    denominator: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_count(self) -> Self:
        if self.event_count > self.denominator:
            raise ValueError("event_count cannot exceed its denominator")
        return self


class SafetyObservations(_StrictModel):
    """Safety events that must never be hidden inside an aggregate success rate."""

    unauthorized_effect: CountObservation
    stale_observation_execution: CountObservation
    approval_bypass: CountObservation
    duplicate_effect: CountObservation


class RecoveryObservation(_StrictModel):
    """Recovery evidence for a case where a failure or interruption applied."""

    category: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_]*$")
    succeeded: bool
    latency_ms: int = Field(ge=0)
    probe_required: bool
    probe_resolved: bool | None = None
    duplicate_effect: CountObservation

    @model_validator(mode="after")
    def validate_probe_applicability(self) -> Self:
        if self.probe_required != (self.probe_resolved is not None):
            raise ValueError("probe_resolved must be present exactly when a result probe is required")
        return self


class BenchmarkCaseResult(_StrictModel):
    """Portable per-case result; business state must come from a Pack or independent probe."""

    schema_version: Literal["agentpact.quantitative-benchmark.case.v1"] = CASE_SCHEMA_VERSION
    case_id: str = Field(min_length=1, max_length=256)
    corpus_version: str = Field(min_length=1, max_length=128)
    pack_id: str = Field(min_length=1, max_length=256)
    pack_version: str = Field(min_length=1, max_length=128)
    platform_version: str = Field(min_length=1, max_length=128)
    provider_mode: str = Field(min_length=1, max_length=128)
    model_version: str | None = Field(default=None, min_length=1, max_length=256)
    browser_runtime_version: str | None = Field(default=None, min_length=1, max_length=256)

    expected_business_state: str = Field(min_length=1, max_length=512)
    actual_business_state: str = Field(min_length=1, max_length=512)
    final_run_state: str = Field(min_length=1, max_length=128)
    task_success: bool
    business_state_correct: bool
    first_action_hit: bool | None = None
    incorrect_action: bool
    unknown_stopped: bool | None = None
    evidence_complete: bool

    action_count: int = Field(ge=0)
    replan_count: int = Field(ge=0)
    approval_count: int = Field(ge=0)
    probe_count: int = Field(ge=0)
    latency_ms: int = Field(ge=0)
    model_cost: float = Field(ge=0)

    safety: SafetyObservations
    recovery: RecoveryObservation | None = None

    @model_validator(mode="after")
    def validate_outcome_evidence(self) -> Self:
        # Correctness is asserted by the Pack/probe evaluator; the platform must
        # not impose literal equality on domain-specific state representations.
        if self.first_action_hit is not None and self.action_count == 0:
            raise ValueError("first_action_hit is not applicable when action_count is zero")
        return self


class CountRateMetric(_StrictModel):
    """An aggregate count, its explicit denominator, and the derived rate."""

    event_count: int = Field(ge=0)
    denominator: int = Field(ge=0)
    rate: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def validate_rate(self) -> Self:
        if self.event_count > self.denominator:
            raise ValueError("event_count cannot exceed its denominator")
        expected = None if self.denominator == 0 else self.event_count / self.denominator
        if expected is None:
            if self.rate is not None:
                raise ValueError("a zero denominator must produce a null rate")
        elif self.rate is None or not math.isclose(self.rate, expected, rel_tol=0, abs_tol=1e-12):
            raise ValueError("rate must equal event_count / denominator")
        return self


class LatencyMetrics(_StrictModel):
    sample_count: int = Field(ge=1)
    average_ms: float = Field(ge=0)
    p50_ms: float = Field(ge=0)
    p95_ms: float = Field(ge=0)

    @model_validator(mode="after")
    def validate_quantile_order(self) -> Self:
        if self.p50_ms > self.p95_ms:
            raise ValueError("p50 latency cannot exceed p95 latency")
        return self


class CostMetrics(_StrictModel):
    sample_count: int = Field(ge=1)
    total_model_cost: float = Field(ge=0)
    average_model_cost: float = Field(ge=0)

    @model_validator(mode="after")
    def validate_average(self) -> Self:
        expected = self.total_model_cost / self.sample_count
        if not math.isclose(self.average_model_cost, expected, rel_tol=0, abs_tol=1e-12):
            raise ValueError("average model cost must equal total_model_cost / sample_count")
        return self


class OutcomeMetrics(_StrictModel):
    task_success: CountRateMetric
    business_state_correctness: CountRateMetric
    first_action_hit: CountRateMetric
    incorrect_action: CountRateMetric
    unknown_stop: CountRateMetric
    audit_completeness: CountRateMetric
    total_action_count: int = Field(ge=0)
    average_action_count: float = Field(ge=0)

    @model_validator(mode="after")
    def validate_action_average(self) -> Self:
        denominator = self.task_success.denominator
        if denominator > 0:
            expected = self.total_action_count / denominator
            if not math.isclose(self.average_action_count, expected, rel_tol=0, abs_tol=1e-12):
                raise ValueError("average action count must equal total_action_count / sample denominator")
        return self


class SafetyMetrics(_StrictModel):
    unauthorized_effect: CountRateMetric
    stale_observation_execution: CountRateMetric
    approval_bypass: CountRateMetric
    duplicate_effect: CountRateMetric


class RecoveryCategoryMetrics(_StrictModel):
    category: str
    case_count: int = Field(ge=1)
    success: CountRateMetric
    probe_resolution: CountRateMetric
    duplicate_effect: CountRateMetric
    latency: LatencyMetrics

    @model_validator(mode="after")
    def validate_category_denominators(self) -> Self:
        if self.success.denominator != self.case_count or self.latency.sample_count != self.case_count:
            raise ValueError("recovery category case denominators must match case_count")
        if self.probe_resolution.denominator > self.case_count:
            raise ValueError("recovery category probe denominator cannot exceed case_count")
        return self


class RecoveryMetrics(_StrictModel):
    case_count: int = Field(ge=0)
    success: CountRateMetric
    probe_resolution: CountRateMetric
    duplicate_effect: CountRateMetric
    by_category: tuple[RecoveryCategoryMetrics, ...]

    @model_validator(mode="after")
    def validate_recovery_denominators(self) -> Self:
        if self.success.denominator != self.case_count:
            raise ValueError("recovery success denominator must match case_count")
        if self.probe_resolution.denominator > self.case_count:
            raise ValueError("recovery probe denominator cannot exceed case_count")
        if sum(metric.case_count for metric in self.by_category) != self.case_count:
            raise ValueError("recovery category case counts must sum to case_count")
        if len({metric.category for metric in self.by_category}) != len(self.by_category):
            raise ValueError("recovery categories must be unique")
        totals = (
            (self.success, tuple(metric.success for metric in self.by_category)),
            (self.probe_resolution, tuple(metric.probe_resolution for metric in self.by_category)),
            (self.duplicate_effect, tuple(metric.duplicate_effect for metric in self.by_category)),
        )
        for overall, grouped in totals:
            if overall.event_count != sum(metric.event_count for metric in grouped):
                raise ValueError("recovery aggregate event counts must match category totals")
            if overall.denominator != sum(metric.denominator for metric in grouped):
                raise ValueError("recovery aggregate denominators must match category totals")
        return self


class PackVersionDimension(_StrictModel):
    pack_id: str
    pack_version: str


class BenchmarkDimensions(_StrictModel):
    corpus_versions: tuple[str, ...]
    pack_ids: tuple[str, ...]
    pack_versions: tuple[PackVersionDimension, ...]
    platform_versions: tuple[str, ...]
    provider_modes: tuple[str, ...]
    model_versions: tuple[str, ...]
    browser_runtime_versions: tuple[str, ...]
    distinct_pack_count: int = Field(ge=1)
    distinct_pack_version_count: int = Field(ge=1)
    distinct_platform_version_count: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_dimension_counts(self) -> Self:
        if len(self.corpus_versions) != 1:
            raise ValueError("a benchmark report requires exactly one corpus_version")
        expected = (len(self.pack_ids), len(self.pack_versions), len(self.platform_versions))
        actual = (
            self.distinct_pack_count,
            self.distinct_pack_version_count,
            self.distinct_platform_version_count,
        )
        if actual != expected:
            raise ValueError("distinct dimension counts must match their reported values")
        return self


class QuantitativeBenchmarkReport(_StrictModel):
    schema_version: Literal["agentpact.quantitative-benchmark.report.v1"] = REPORT_SCHEMA_VERSION
    sample_count: int = Field(ge=1)
    dimensions: BenchmarkDimensions
    outcomes: OutcomeMetrics
    safety: SafetyMetrics
    recovery: RecoveryMetrics
    latency: LatencyMetrics
    cost: CostMetrics

    @model_validator(mode="after")
    def validate_report_denominators(self) -> Self:
        corpus_rates = (
            self.outcomes.task_success,
            self.outcomes.business_state_correctness,
            self.outcomes.incorrect_action,
            self.outcomes.audit_completeness,
        )
        if any(metric.denominator != self.sample_count for metric in corpus_rates):
            raise ValueError("corpus outcome denominators must match sample_count")
        if self.outcomes.first_action_hit.denominator > self.sample_count:
            raise ValueError("first-action denominator cannot exceed sample_count")
        if self.outcomes.unknown_stop.denominator > self.sample_count:
            raise ValueError("unknown-stop denominator cannot exceed sample_count")
        if self.latency.sample_count != self.sample_count or self.cost.sample_count != self.sample_count:
            raise ValueError("latency and cost sample counts must match sample_count")
        if self.recovery.case_count > self.sample_count:
            raise ValueError("recovery case_count cannot exceed sample_count")
        return self


def _count_rate(event_count: int, denominator: int) -> CountRateMetric:
    rate = None if denominator == 0 else event_count / denominator
    return CountRateMetric(event_count=event_count, denominator=denominator, rate=rate)


def _boolean_rate(values: Sequence[bool]) -> CountRateMetric:
    return _count_rate(sum(values), len(values))


def _quantile(sorted_values: Sequence[int], percentile: float) -> float:
    """Use deterministic linear interpolation over zero-based sample positions."""

    if len(sorted_values) == 1:
        return float(sorted_values[0])
    position = (len(sorted_values) - 1) * percentile
    lower_index = math.floor(position)
    upper_index = math.ceil(position)
    lower = sorted_values[lower_index]
    upper = sorted_values[upper_index]
    return float(lower + (upper - lower) * (position - lower_index))


def _latency_metrics(values: Sequence[int]) -> LatencyMetrics:
    ordered = tuple(sorted(values))
    return LatencyMetrics(
        sample_count=len(ordered),
        average_ms=math.fsum(ordered) / len(ordered),
        p50_ms=_quantile(ordered, 0.50),
        p95_ms=_quantile(ordered, 0.95),
    )


def _aggregate_count_observations(observations: Sequence[CountObservation]) -> CountRateMetric:
    return _count_rate(
        sum(observation.event_count for observation in observations),
        sum(observation.denominator for observation in observations),
    )


def _recovery_category_metrics(
    category: str,
    observations: Sequence[RecoveryObservation],
) -> RecoveryCategoryMetrics:
    probe_observations = tuple(observation for observation in observations if observation.probe_required)
    return RecoveryCategoryMetrics(
        category=category,
        case_count=len(observations),
        success=_boolean_rate(tuple(observation.succeeded for observation in observations)),
        probe_resolution=_boolean_rate(
            tuple(observation.probe_resolved is True for observation in probe_observations)
        ),
        duplicate_effect=_aggregate_count_observations(
            tuple(observation.duplicate_effect for observation in observations)
        ),
        latency=_latency_metrics(tuple(observation.latency_ms for observation in observations)),
    )


def aggregate_quantitative_benchmark(
    cases: Sequence[BenchmarkCaseResult],
) -> QuantitativeBenchmarkReport:
    """Aggregate a non-empty case corpus without guessing any metric denominator."""

    records = tuple(cases)
    if not records:
        raise ValueError("Quantitative benchmark requires at least one case")

    identities = tuple((record.corpus_version, record.case_id) for record in records)
    if len(set(identities)) != len(identities):
        raise ValueError("Quantitative benchmark case identities must be unique within a corpus")
    corpus_versions = tuple(sorted({record.corpus_version for record in records}))
    if len(corpus_versions) != 1:
        raise ValueError("Quantitative benchmark aggregation requires exactly one corpus_version")

    first_action_values = tuple(
        record.first_action_hit for record in records if record.first_action_hit is not None
    )
    unknown_stop_values = tuple(
        record.unknown_stopped for record in records if record.unknown_stopped is not None
    )
    action_count = sum(record.action_count for record in records)

    recovery_observations = tuple(record.recovery for record in records if record.recovery is not None)
    grouped_recovery: dict[str, list[RecoveryObservation]] = defaultdict(list)
    for observation in recovery_observations:
        grouped_recovery[observation.category].append(observation)
    recovery_probe_observations = tuple(
        observation for observation in recovery_observations if observation.probe_required
    )

    pack_versions = tuple(
        PackVersionDimension(pack_id=pack_id, pack_version=pack_version)
        for pack_id, pack_version in sorted({(record.pack_id, record.pack_version) for record in records})
    )
    pack_ids = tuple(sorted({record.pack_id for record in records}))
    platform_versions = tuple(sorted({record.platform_version for record in records}))

    total_model_cost = math.fsum(record.model_cost for record in records)
    return QuantitativeBenchmarkReport(
        sample_count=len(records),
        dimensions=BenchmarkDimensions(
            corpus_versions=corpus_versions,
            pack_ids=pack_ids,
            pack_versions=pack_versions,
            platform_versions=platform_versions,
            provider_modes=tuple(sorted({record.provider_mode for record in records})),
            model_versions=tuple(sorted({record.model_version for record in records if record.model_version})),
            browser_runtime_versions=tuple(
                sorted(
                    {
                        record.browser_runtime_version
                        for record in records
                        if record.browser_runtime_version
                    }
                )
            ),
            distinct_pack_count=len(pack_ids),
            distinct_pack_version_count=len(pack_versions),
            distinct_platform_version_count=len(platform_versions),
        ),
        outcomes=OutcomeMetrics(
            task_success=_boolean_rate(tuple(record.task_success for record in records)),
            business_state_correctness=_boolean_rate(
                tuple(record.business_state_correct for record in records)
            ),
            first_action_hit=_boolean_rate(first_action_values),
            incorrect_action=_boolean_rate(tuple(record.incorrect_action for record in records)),
            unknown_stop=_boolean_rate(unknown_stop_values),
            audit_completeness=_boolean_rate(tuple(record.evidence_complete for record in records)),
            total_action_count=action_count,
            average_action_count=action_count / len(records),
        ),
        safety=SafetyMetrics(
            unauthorized_effect=_aggregate_count_observations(
                tuple(record.safety.unauthorized_effect for record in records)
            ),
            stale_observation_execution=_aggregate_count_observations(
                tuple(record.safety.stale_observation_execution for record in records)
            ),
            approval_bypass=_aggregate_count_observations(
                tuple(record.safety.approval_bypass for record in records)
            ),
            duplicate_effect=_aggregate_count_observations(
                tuple(record.safety.duplicate_effect for record in records)
            ),
        ),
        recovery=RecoveryMetrics(
            case_count=len(recovery_observations),
            success=_boolean_rate(tuple(observation.succeeded for observation in recovery_observations)),
            probe_resolution=_boolean_rate(
                tuple(observation.probe_resolved is True for observation in recovery_probe_observations)
            ),
            duplicate_effect=_aggregate_count_observations(
                tuple(observation.duplicate_effect for observation in recovery_observations)
            ),
            by_category=tuple(
                _recovery_category_metrics(category, tuple(grouped_recovery[category]))
                for category in sorted(grouped_recovery)
            ),
        ),
        latency=_latency_metrics(tuple(record.latency_ms for record in records)),
        cost=CostMetrics(
            sample_count=len(records),
            total_model_cost=total_model_cost,
            average_model_cost=total_model_cost / len(records),
        ),
    )


__all__ = [
    "CASE_SCHEMA_VERSION",
    "REPORT_SCHEMA_VERSION",
    "BenchmarkCaseResult",
    "CountObservation",
    "QuantitativeBenchmarkReport",
    "RecoveryObservation",
    "SafetyObservations",
    "aggregate_quantitative_benchmark",
]
