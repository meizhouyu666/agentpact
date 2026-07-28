"""Synthetic-only metrics and fault replay helpers; not compliance evidence."""

from collections import Counter

from pydantic import BaseModel, Field

from .recovery import ExecutionFailureEvent, RecoveryDecision, decide_recovery


class BenchmarkRecord(BaseModel):
    task_success: bool
    first_action_hit: bool
    incorrect_action: bool
    recovery_level: str
    unknown_stopped: bool
    fallback_used: bool
    audit_complete: bool
    latency_ms: int = Field(ge=0)
    model_cost: float = Field(ge=0)


class BenchmarkMetrics(BaseModel):
    task_success_rate: float
    first_action_hit_rate: float
    incorrect_action_rate: float
    recovery_distribution: dict[str, int]
    unknown_stop_rate: float
    fallback_rate: float
    audit_completeness_rate: float
    average_latency_ms: float
    total_model_cost: float


def summarize(records: list[BenchmarkRecord]) -> BenchmarkMetrics:
    if not records:
        raise ValueError("Synthetic benchmark requires at least one record")
    count = len(records)
    return BenchmarkMetrics(
        task_success_rate=sum(record.task_success for record in records) / count,
        first_action_hit_rate=sum(record.first_action_hit for record in records) / count,
        incorrect_action_rate=sum(record.incorrect_action for record in records) / count,
        recovery_distribution=dict(Counter(record.recovery_level for record in records)),
        unknown_stop_rate=sum(record.unknown_stopped for record in records) / count,
        fallback_rate=sum(record.fallback_used for record in records) / count,
        audit_completeness_rate=sum(record.audit_complete for record in records) / count,
        average_latency_ms=sum(record.latency_ms for record in records) / count,
        total_model_cost=sum(record.model_cost for record in records),
    )


def replay_fault(event: ExecutionFailureEvent) -> RecoveryDecision:
    return decide_recovery(event)
