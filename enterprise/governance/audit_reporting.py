"""Read-only replay and completeness reporting for audit candidate events."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select

from .audit import AuditCandidatePayload
from .models import GovernanceAuditEventModel


class AuditReplayEvent(BaseModel):
    event_id: str
    task_id: str
    step_id: str | None
    organization_id: str
    action_fingerprint: str | None
    observation_hash: str | None
    created_at: datetime | None
    payload: AuditCandidatePayload


class AuditReplayPage(BaseModel):
    """Replayable audit records plus opaque identifiers for invalid history."""

    events: list[AuditReplayEvent] = Field(default_factory=list)
    invalid_event_ids: list[str] = Field(default_factory=list)

    @property
    def invalid_payload_count(self) -> int:
        return len(self.invalid_event_ids)

    @property
    def replayable_event_count(self) -> int:
        return len(self.events)

    @property
    def scanned_event_count(self) -> int:
        return self.replayable_event_count + self.invalid_payload_count


class AuditCompletenessMetrics(BaseModel):
    total_events: int
    replayable_events: int
    invalid_payload_events: int
    write_failure_events: int
    distinct_observations: int

    @property
    def replay_completeness_rate(self) -> float:
        observed_events = self.total_events + self.write_failure_events
        return self.replayable_events / observed_events if observed_events else 0.0


async def list_audit_replay_events(
    *,
    db_session: Any,
    organization_id: str,
    task_id: str | None = None,
    step_id: str | None = None,
    limit: int = 100,
) -> AuditReplayPage:
    """Read replayable records without failing on invalid historical payloads."""

    if limit <= 0 or limit > 1000:
        raise ValueError("Audit replay limit must be between 1 and 1000")
    statement = (
        select(GovernanceAuditEventModel)
        .where(
            GovernanceAuditEventModel.organization_id == organization_id,
            GovernanceAuditEventModel.event_type == "action_candidate",
            GovernanceAuditEventModel.mode == "audit",
        )
        .order_by(GovernanceAuditEventModel.created_at, GovernanceAuditEventModel.event_id)
        .limit(limit)
    )
    if task_id is not None:
        statement = statement.where(GovernanceAuditEventModel.task_id == task_id)
    if step_id is not None:
        statement = statement.where(GovernanceAuditEventModel.step_id == step_id)
    models = (await db_session.scalars(statement)).all()
    events: list[AuditReplayEvent] = []
    invalid_event_ids: list[str] = []
    for model in models:
        try:
            events.append(_to_replay_event(model))
        except ValueError:
            invalid_event_ids.append(model.event_id)
    return AuditReplayPage(events=events, invalid_event_ids=invalid_event_ids)


def summarize_audit_completeness(
    models: list[GovernanceAuditEventModel],
    *,
    write_failure_events: int = 0,
) -> AuditCompletenessMetrics:
    """Aggregate replay validity and externally counted write failures.

    Write failures come from the non-blocking audit-hook log aggregation; this
    function does not write, retry, or infer an execution outcome.
    """

    if write_failure_events < 0:
        raise ValueError("write_failure_events cannot be negative")
    replayable = 0
    observations: set[str] = set()
    for model in models:
        try:
            payload = _validated_payload(model)
        except ValueError:
            continue
        replayable += 1
        observations.add(payload.evidence_refs.observation_hash)
    return AuditCompletenessMetrics(
        total_events=len(models),
        replayable_events=replayable,
        invalid_payload_events=len(models) - replayable,
        write_failure_events=write_failure_events,
        distinct_observations=len(observations),
    )


def _to_replay_event(model: GovernanceAuditEventModel) -> AuditReplayEvent:
    payload = _validated_payload(model)
    return AuditReplayEvent(
        event_id=model.event_id,
        task_id=model.task_id,
        step_id=model.step_id,
        organization_id=model.organization_id,
        action_fingerprint=model.action_fingerprint,
        observation_hash=model.observation_hash,
        created_at=model.created_at,
        payload=payload,
    )


def _validated_payload(model: GovernanceAuditEventModel) -> AuditCandidatePayload:
    payload = AuditCandidatePayload.model_validate(model.payload)
    if not model.action_fingerprint:
        raise ValueError("Audit event is missing its action fingerprint")
    if not model.observation_hash:
        raise ValueError("Audit event is missing its observation hash")
    if model.observation_hash != payload.evidence_refs.observation_hash:
        raise ValueError("Audit event observation hash does not match its payload")
    return payload
