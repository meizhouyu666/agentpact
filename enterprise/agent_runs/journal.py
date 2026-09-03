"""Generic durable Agent Run checkpoint and journal contracts."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from enterprise.agent.work_orders import RecoveryLevel
from enterprise.governance.models import GovernanceAuditEventModel

from .persistence import AgentRunNativeStore

PLAN_APPLICATION_MARKER = "agentpact:agent-run:plan:v1"
LEGACY_PLAN_APPLICATION_MARKERS = frozenset({"agentpact:m8:plan:v1"})
PLAN_JOURNAL_SCHEMA = "agentpact.plan-journal/v1"
PLAN_CHECKPOINT_SCHEMA = "agentpact.plan-checkpoint/v1"
PLAN_EVENT_TYPE_PREFIX = "agent-run.plan."


class GovernedPlanError(RuntimeError):
    """Fail-closed Agent Run coordination or journal error."""


def is_plan_application_marker(value: str | None) -> bool:
    """Accept current and previously persisted Agent Run root markers."""

    return value == PLAN_APPLICATION_MARKER or value in LEGACY_PLAN_APPLICATION_MARKERS


class PlanRunState(StrEnum):
    ACTIVE = "active"
    APPROVAL_REQUIRED = "approval_required"
    REPLAN_REQUIRED = "replan_required"
    PROBE_BLOCKED = "probe_blocked"
    REAUTHORIZATION_REQUIRED = "reauthorization_required"
    COMPLETED = "completed"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    FAILED = "failed"


class PlanStepState(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    COMPLETED = "completed"
    SUPERSEDED = "superseded"
    PROBE_BLOCKED = "probe_blocked"
    FAILED = "failed"


class PlanJournalTransition(StrEnum):
    ADMITTED = "admitted"
    CHILD_ACTIVATED = "child_activated"
    CHILD_COMPLETED = "child_completed"
    REPLAN_REQUIRED = "replan_required"
    PROBE_BLOCKED = "probe_blocked"
    PROBE_RESOLVED = "probe_resolved"
    SUFFIX_SUPERSEDED = "suffix_superseded"
    PLAN_COMPLETED = "plan_completed"
    REAUTHORIZATION_REQUIRED = "reauthorization_required"
    APPROVAL_REQUIRED = "approval_required"
    APPROVAL_RESUMED = "approval_resumed"
    APPROVAL_REJECTED = "approval_rejected"
    RUN_CANCELLED = "run_cancelled"


class GovernedPlanStepRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    business_plan_step_id: str
    step_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    work_order_id: str
    work_order_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    native_task_id: str
    native_step_id: str
    native_contract_id: str
    authority_contract_id: str
    state: PlanStepState
    permit_id: str | None = None
    attempt_id: str | None = None
    probe_ref: str | None = None


class GovernedPlanCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["agentpact.plan-checkpoint/v1"] = PLAN_CHECKPOINT_SCHEMA
    plan_run_id: str
    admission_id: str
    root_task_id: str
    plan_id: str
    plan_version: int = Field(ge=1)
    authority_contract_id: str
    completed_prefix: tuple[GovernedPlanStepRef, ...] = ()
    active_step: GovernedPlanStepRef | None = None
    remaining_suffix: tuple[GovernedPlanStepRef, ...] = ()
    superseded_suffix: tuple[GovernedPlanStepRef, ...] = ()
    state: PlanRunState = PlanRunState.ACTIVE
    replan_count: int = Field(default=0, ge=0)
    max_replans: int = Field(default=2, ge=0, le=2)
    journal_sequence: int = Field(default=0, ge=0)
    journal_digest: str = Field(default="0" * 64, pattern=r"^[0-9a-f]{64}$")


class PlanJournalEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["agentpact.plan-journal/v1"] = PLAN_JOURNAL_SCHEMA
    event_id: str
    plan_run_id: str
    root_task_id: str
    plan_id: str
    plan_version: int
    sequence: int = Field(ge=1)
    previous_event_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    event_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    transition: PlanJournalTransition
    recovery_level: RecoveryLevel | None = None
    reason: str | None = None
    authority_digests: dict[str, str]
    checkpoint: GovernedPlanCheckpoint
    created_at: datetime


def replay_plan_journal(events: list[PlanJournalEvent]) -> GovernedPlanCheckpoint:
    if not events:
        raise GovernedPlanError("Agent Run journal is empty")
    previous = "0" * 64
    terminal = False
    run_identity: tuple[str, str, str] | None = None
    previous_plan_version = 0
    previous_created_at: datetime | None = None
    for expected_sequence, event in enumerate(events, start=1):
        if event.sequence != expected_sequence:
            raise GovernedPlanError("Agent Run journal contains a sequence gap or reorder")
        if expected_sequence == 1 and event.transition is not PlanJournalTransition.ADMITTED:
            raise GovernedPlanError("Agent Run journal does not begin with admission")
        if event.previous_event_digest != previous:
            raise GovernedPlanError("Agent Run journal digest chain is broken")
        if event.event_digest != _digest(event.model_dump(mode="json", exclude={"event_digest"})):
            raise GovernedPlanError("Agent Run journal event digest is corrupt")
        expected_event_id = _stable_id(
            event.plan_run_id,
            f"event-v{event.plan_version}-{event.sequence}-{event.transition.value}",
        )
        legacy_event_id = _legacy_stable_id(
            event.plan_run_id,
            f"event-v{event.plan_version}-{event.sequence}-{event.transition.value}",
        )
        if event.event_id not in {expected_event_id, legacy_event_id}:
            raise GovernedPlanError("Agent Run journal event identity is not deterministic")
        checkpoint = event.checkpoint
        identity = (event.plan_run_id, checkpoint.admission_id, event.root_task_id)
        if run_identity is None:
            run_identity = identity
        elif identity != run_identity:
            raise GovernedPlanError("Agent Run journal root identity changed")
        if (
            checkpoint.plan_run_id != event.plan_run_id
            or checkpoint.root_task_id != event.root_task_id
            or checkpoint.plan_id != event.plan_id
            or checkpoint.plan_version != event.plan_version
            or checkpoint.journal_sequence != event.sequence
            or checkpoint.journal_digest != event.previous_event_digest
        ):
            raise GovernedPlanError("Agent Run journal event and checkpoint identity disagree")
        if event.plan_version < previous_plan_version or event.plan_version > previous_plan_version + 1:
            raise GovernedPlanError("Agent Run journal plan version is reordered or contains a gap")
        if previous_plan_version and event.plan_version > previous_plan_version:
            if event.transition is not PlanJournalTransition.SUFFIX_SUPERSEDED:
                raise GovernedPlanError("Agent Run journal plan version changed outside suffix supersession")
        if previous_created_at is not None and event.created_at < previous_created_at:
            raise GovernedPlanError("Agent Run journal timestamps are reordered")
        _validate_transition_checkpoint(event.transition, checkpoint)
        if terminal:
            raise GovernedPlanError("Agent Run journal contains a transition after terminal state")
        terminal = event.transition in {
            PlanJournalTransition.PLAN_COMPLETED,
            PlanJournalTransition.REAUTHORIZATION_REQUIRED,
            PlanJournalTransition.APPROVAL_REJECTED,
            PlanJournalTransition.RUN_CANCELLED,
        }
        previous = event.event_digest
        previous_plan_version = event.plan_version
        previous_created_at = event.created_at
    return events[-1].checkpoint.model_copy(
        update={"journal_sequence": events[-1].sequence, "journal_digest": events[-1].event_digest}
    )


async def append_agent_run_transition(
    session: Any,
    *,
    native_store: AgentRunNativeStore,
    organization_id: str,
    checkpoint: GovernedPlanCheckpoint,
    transition: PlanJournalTransition,
    authority_digests: dict[str, str],
    operation_key: str,
    created_at: datetime,
) -> GovernedPlanCheckpoint:
    """Append one approval/cancellation transition in the caller transaction."""

    transition = PlanJournalTransition(transition)
    if transition not in {
        PlanJournalTransition.APPROVAL_REQUIRED,
        PlanJournalTransition.APPROVAL_RESUMED,
        PlanJournalTransition.APPROVAL_REJECTED,
        PlanJournalTransition.RUN_CANCELLED,
    }:
        raise GovernedPlanError("Agent Run append accepts only orchestration-owned transitions")
    root = await native_store.get_root(
        session,
        run_id=checkpoint.root_task_id,
        organization_id=organization_id,
        lock=True,
    )
    if root is None or not is_plan_application_marker(root.application):
        raise GovernedPlanError("Agent Run transition root Task is missing or untrusted")
    events = await _load_events(session, checkpoint.root_task_id)
    restored = replay_plan_journal(events)
    if restored.journal_sequence != checkpoint.journal_sequence or restored.journal_digest != checkpoint.journal_digest:
        if restored.journal_sequence == checkpoint.journal_sequence + 1:
            committed = events[-1]
            expected_checkpoint = checkpoint.model_copy(
                update={"journal_sequence": committed.sequence, "journal_digest": checkpoint.journal_digest}
            )
            if (
                committed.transition is transition
                and committed.reason == operation_key
                and committed.authority_digests == authority_digests
                and committed.checkpoint == expected_checkpoint
            ):
                return restored
        raise GovernedPlanError("Agent Run stale, conflicting, or one-sided transition detected")
    try:
        await native_store.verify_checkpoint_native_state(
            session,
            checkpoint,
            transition=transition,
            organization_id=organization_id,
        )
    except ValueError as exc:
        raise GovernedPlanError(str(exc)) from exc
    event = _event(
        checkpoint=checkpoint,
        transition=transition,
        authority_digests=authority_digests,
        reason=operation_key,
        created_at=created_at,
    )
    session.add(_event_model(event, root.organization_id))
    await session.flush()
    return checkpoint.model_copy(update={"journal_sequence": event.sequence, "journal_digest": event.event_digest})


def _event(
    *,
    checkpoint: GovernedPlanCheckpoint,
    transition: PlanJournalTransition,
    authority_digests: dict[str, str],
    created_at: datetime,
    recovery_level: RecoveryLevel | None = None,
    reason: str | None = None,
) -> PlanJournalEvent:
    sequence = checkpoint.journal_sequence + 1
    previous = checkpoint.journal_digest
    checkpoint_payload = checkpoint.model_copy(update={"journal_sequence": sequence, "journal_digest": previous})
    values = {
        "schema_version": PLAN_JOURNAL_SCHEMA,
        "event_id": _stable_id(
            checkpoint.plan_run_id,
            f"event-v{checkpoint.plan_version}-{sequence}-{transition.value}",
        ),
        "plan_run_id": checkpoint.plan_run_id,
        "root_task_id": checkpoint.root_task_id,
        "plan_id": checkpoint.plan_id,
        "plan_version": checkpoint.plan_version,
        "sequence": sequence,
        "previous_event_digest": previous,
        "transition": transition,
        "recovery_level": recovery_level,
        "reason": reason,
        "authority_digests": authority_digests,
        "checkpoint": checkpoint_payload,
        "created_at": created_at,
    }
    unsigned = PlanJournalEvent(event_digest="0" * 64, **values)
    return unsigned.model_copy(
        update={"event_digest": _digest(unsigned.model_dump(mode="json", exclude={"event_digest"}))}
    )


async def _load_events(session: Any, root_task_id: str) -> list[PlanJournalEvent]:
    models = list(
        (
            await session.scalars(
                select(GovernanceAuditEventModel).where(
                    GovernanceAuditEventModel.task_id == root_task_id,
                )
            )
        ).all()
    )
    events: list[PlanJournalEvent] = []
    for item in models:
        payload = item.payload
        if not isinstance(payload, dict) or payload.get("schema_version") != PLAN_JOURNAL_SCHEMA:
            continue
        events.append(PlanJournalEvent.model_validate(payload))
    return sorted(events, key=lambda item: item.sequence)


def _validate_transition_checkpoint(
    transition: PlanJournalTransition,
    checkpoint: GovernedPlanCheckpoint,
) -> None:
    expected_states = {
        PlanJournalTransition.ADMITTED: {PlanRunState.ACTIVE},
        PlanJournalTransition.CHILD_ACTIVATED: {PlanRunState.ACTIVE},
        PlanJournalTransition.CHILD_COMPLETED: {PlanRunState.ACTIVE},
        PlanJournalTransition.REPLAN_REQUIRED: {PlanRunState.REPLAN_REQUIRED},
        PlanJournalTransition.PROBE_BLOCKED: {PlanRunState.PROBE_BLOCKED},
        PlanJournalTransition.PROBE_RESOLVED: {PlanRunState.ACTIVE, PlanRunState.COMPLETED},
        PlanJournalTransition.SUFFIX_SUPERSEDED: {PlanRunState.ACTIVE},
        PlanJournalTransition.PLAN_COMPLETED: {PlanRunState.COMPLETED},
        PlanJournalTransition.REAUTHORIZATION_REQUIRED: {PlanRunState.REAUTHORIZATION_REQUIRED},
        PlanJournalTransition.APPROVAL_REQUIRED: {PlanRunState.APPROVAL_REQUIRED},
        PlanJournalTransition.APPROVAL_RESUMED: {PlanRunState.ACTIVE},
        PlanJournalTransition.APPROVAL_REJECTED: {PlanRunState.REJECTED},
        PlanJournalTransition.RUN_CANCELLED: {PlanRunState.CANCELLED},
    }
    if checkpoint.state not in expected_states[transition]:
        raise GovernedPlanError("Agent Run journal transition disagrees with checkpoint state")
    if checkpoint.state is PlanRunState.COMPLETED and checkpoint.active_step is not None:
        raise GovernedPlanError("Completed Agent Run retains an active child")
    if checkpoint.state in {
        PlanRunState.ACTIVE,
        PlanRunState.APPROVAL_REQUIRED,
        PlanRunState.REPLAN_REQUIRED,
        PlanRunState.PROBE_BLOCKED,
    } and checkpoint.active_step is None:
        raise GovernedPlanError("Nonterminal Agent Run checkpoint is missing its active child")


class AgentRunJournal:
    """Generic journal façade bound to an AgentPact native persistence store."""

    def __init__(self, native_store: AgentRunNativeStore) -> None:
        self.native_store = native_store

    async def load_events(self, session: Any, root_task_id: str) -> list[PlanJournalEvent]:
        return await _load_events(session, root_task_id)

    def replay(self, events: list[PlanJournalEvent]) -> GovernedPlanCheckpoint:
        return replay_plan_journal(events)

    async def append(
        self,
        session: Any,
        *,
        organization_id: str,
        checkpoint: GovernedPlanCheckpoint,
        transition: PlanJournalTransition,
        authority_digests: dict[str, str],
        operation_key: str,
        created_at: datetime,
    ) -> GovernedPlanCheckpoint:
        return await append_agent_run_transition(
            session,
            native_store=self.native_store,
            organization_id=organization_id,
            checkpoint=checkpoint,
            transition=transition,
            authority_digests=authority_digests,
            operation_key=operation_key,
            created_at=created_at,
        )


def _event_model(event: PlanJournalEvent, organization_id: str) -> GovernanceAuditEventModel:
    return GovernanceAuditEventModel(
        event_id=event.event_id,
        task_id=event.root_task_id,
        step_id=None,
        contract_id=event.checkpoint.authority_contract_id,
        organization_id=organization_id,
        event_type=f"{PLAN_EVENT_TYPE_PREFIX}{event.transition.value}",
        mode="audit",
        action_fingerprint=None,
        observation_hash=None,
        policy_version=None,
        payload=event.model_dump(mode="json"),
        created_at=event.created_at,
    )


def _digest(value: Any) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    canonical = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _stable_id(seed: str, kind: str) -> str:
    return "agent_run_" + hashlib.sha256(f"agentpact-agent-run|{seed}|{kind}".encode("utf-8")).hexdigest()


def _legacy_stable_id(seed: str, kind: str) -> str:
    legacy_namespace = "agentpact-" + "m8"
    return "m8_" + hashlib.sha256(f"{legacy_namespace}|{seed}|{kind}".encode("utf-8")).hexdigest()


# Compatibility names used by the existing M8 implementation while ownership moves here.
_replay = replay_plan_journal
append_m10_transition = append_agent_run_transition
