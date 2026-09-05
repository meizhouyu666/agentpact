"""Skyvern-backed implementation of the Agent Run native store contract.

This is the only Agent Run persistence integration that knows about Skyvern's
Task/Step ORM models and status enums.  The rest of the Agent Run core uses
the snapshots defined in :mod:`enterprise.agent_runs.persistence`.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, or_, select

from enterprise.agent_runs.persistence import (
    AgentRunNativePair,
    AgentRunPauseSnapshot,
    AgentRunNativeStore,
    AgentRunStepSnapshot,
    AgentRunStepStatus,
    AgentRunTaskSnapshot,
    AgentRunTaskStatus,
)
from enterprise.agent_runs.pause_signal import RunPauseSignal
from enterprise.governance.contracts import ExecutionAttemptStatus
from enterprise.governance.models import (
    ExecutionAttemptModel,
    ExecutionPermitModel,
    GovernedTaskAdmissionModel,
    GovernanceAuditEventModel,
)
from skyvern.forge.sdk.db.models import StepModel, TaskModel
from skyvern.forge.sdk.models import StepStatus
from skyvern.forge.sdk.schemas.tasks import TaskStatus


class SkyvernAgentRunStore(AgentRunNativeStore):
    """Translate Agent Run native operations to Skyvern's current schema."""

    async def get_root(
        self,
        session: Any,
        *,
        run_id: str,
        organization_id: str,
        lock: bool = False,
    ) -> AgentRunTaskSnapshot | None:
        statement = select(TaskModel).where(
            TaskModel.task_id == run_id,
            TaskModel.organization_id == organization_id,
        )
        if lock:
            statement = statement.with_for_update()
        model = (await session.scalars(statement)).first()
        return None if model is None else self._task_snapshot(model)

    async def list_roots(
        self,
        session: Any,
        *,
        organization_id: str,
        boundary: tuple[datetime, str] | None = None,
        limit: int = 21,
    ) -> tuple[AgentRunTaskSnapshot, ...]:
        statement = (
            select(TaskModel)
            .join(
                GovernedTaskAdmissionModel,
                and_(
                    GovernedTaskAdmissionModel.task_id == TaskModel.task_id,
                    GovernedTaskAdmissionModel.organization_id == TaskModel.organization_id,
                ),
            )
            .where(
                TaskModel.organization_id == organization_id,
                TaskModel.task_id.like("run_%"),
            )
        )
        if boundary is not None:
            created_at, run_id = boundary
            statement = statement.where(
                or_(
                    TaskModel.created_at < created_at,
                    and_(TaskModel.created_at == created_at, TaskModel.task_id < run_id),
                )
            )
        models = list(
            (
                await session.scalars(
                    statement.order_by(TaskModel.created_at.desc(), TaskModel.task_id.desc()).limit(limit)
                )
            ).all()
        )
        return tuple(self._task_snapshot(model) for model in models)

    async def get_native_pair(
        self,
        session: Any,
        *,
        task_id: str,
        step_id: str,
        organization_id: str,
    ) -> AgentRunNativePair:
        task_model = (
            await session.scalars(
                select(TaskModel).where(
                    TaskModel.task_id == task_id,
                    TaskModel.organization_id == organization_id,
                )
            )
        ).first()
        step_model = (
            await session.scalars(
                select(StepModel).where(
                    StepModel.step_id == step_id,
                    StepModel.task_id == task_id,
                    StepModel.organization_id == organization_id,
                )
            )
        ).first()
        if (task_model is None) != (step_model is None):
            raise ValueError("Agent Run native task/step pair is inconsistent")
        return (
            None if task_model is None else self._task_snapshot(task_model),
            None if step_model is None else self._step_snapshot(step_model),
        )

    async def cancel_native_pair(
        self,
        session: Any,
        *,
        task_id: str,
        step_id: str,
        organization_id: str,
    ) -> bool:
        task_model = (
            await session.scalars(
                select(TaskModel).where(
                    TaskModel.task_id == task_id,
                    TaskModel.organization_id == organization_id,
                )
            )
        ).first()
        step_model = (
            await session.scalars(
                select(StepModel).where(
                    StepModel.step_id == step_id,
                    StepModel.task_id == task_id,
                    StepModel.organization_id == organization_id,
                )
            )
        ).first()
        if task_model is None or step_model is None:
            return False
        task_model.status = TaskStatus.canceled.value
        step_model.status = StepStatus.canceled.value
        await session.flush()
        return True

    async def verify_checkpoint_native_state(
        self,
        session: Any,
        checkpoint: Any,
        *,
        transition: Any,
        organization_id: str,
    ) -> None:
        await _verify_checkpoint_native_state(
            self,
            session,
            checkpoint,
            transition=transition,
            organization_id=organization_id,
        )

    async def save_pause_signal(
        self,
        session: Any,
        signal: RunPauseSignal,
        *,
        checkpoint_digest: str,
        organization_id: str,
        modified_at: datetime,
    ) -> None:
        payload = {
            "schema_version": "agentpact.run-pause/v1",
            "run_id": signal.run_id,
            "checkpoint_digest": checkpoint_digest,
            "signal": signal.model_dump(mode="json"),
        }
        event_id = "agent_run_pause_" + hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        existing = (
            await session.scalars(
                select(GovernanceAuditEventModel)
                .where(
                    GovernanceAuditEventModel.task_id == signal.run_id,
                    GovernanceAuditEventModel.organization_id == organization_id,
                    GovernanceAuditEventModel.event_type == "agent-run.pause",
                )
                .order_by(GovernanceAuditEventModel.created_at.desc())
            )
        ).first()
        if existing is not None:
            if existing.payload != payload:
                raise ValueError("Conflicting pause signal already exists")
            return
        session.add(
            GovernanceAuditEventModel(
                event_id=event_id,
                task_id=signal.run_id,
                step_id=signal.step_id,
                contract_id=None,
                organization_id=organization_id,
                event_type="agent-run.pause",
                mode="audit",
                payload=payload,
                created_at=modified_at,
            )
        )
        await session.flush()

    async def get_pause_signal(
        self,
        session: Any,
        *,
        run_id: str,
        organization_id: str,
    ) -> AgentRunPauseSnapshot | None:
        rows = list(
            await session.scalars(
                select(GovernanceAuditEventModel)
                .where(
                    GovernanceAuditEventModel.task_id == run_id,
                    GovernanceAuditEventModel.organization_id == organization_id,
                    GovernanceAuditEventModel.event_type.in_(("agent-run.pause", "agent-run.pause.resolved")),
                )
                .order_by(GovernanceAuditEventModel.created_at.asc(), GovernanceAuditEventModel.event_id.asc())
            )
        )
        candidate: GovernanceAuditEventModel | None = None
        for row in rows:
            payload = row.payload
            if row.event_type == "agent-run.pause":
                if not isinstance(payload, dict) or payload.get("schema_version") != "agentpact.run-pause/v1":
                    raise ValueError("Invalid persisted Agent Run pause signal")
                candidate = row
            elif candidate is not None and isinstance(payload, dict):
                if payload.get("pause_event_id") == candidate.event_id:
                    candidate = None
        if candidate is None:
            return None
        payload = candidate.payload
        assert isinstance(payload, dict)
        return AgentRunPauseSnapshot(
            signal=RunPauseSignal.model_validate(payload.get("signal")),
            checkpoint_digest=str(payload.get("checkpoint_digest", "")),
            modified_at=candidate.created_at,
        )

    async def clear_pause_signal(
        self,
        session: Any,
        *,
        run_id: str,
        organization_id: str,
    ) -> None:
        pauses = list(
            (
                await session.scalars(
                    select(GovernanceAuditEventModel).where(
                        GovernanceAuditEventModel.task_id == run_id,
                        GovernanceAuditEventModel.organization_id == organization_id,
                        GovernanceAuditEventModel.event_type == "agent-run.pause",
                    ).order_by(GovernanceAuditEventModel.created_at.desc(), GovernanceAuditEventModel.event_id.desc())
                )
            ).all()
        )
        if not pauses:
            return
        pause = pauses[0]
        resolution_payload = {
            "schema_version": "agentpact.run-pause-resolution/v1",
            "run_id": run_id,
            "pause_event_id": pause.event_id,
            "status": "cleared",
        }
        event_id = "agent_run_pause_resolution_" + hashlib.sha256(
            json.dumps(resolution_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        existing = await session.get(GovernanceAuditEventModel, event_id)
        if existing is None:
            session.add(
                GovernanceAuditEventModel(
                    event_id=event_id,
                    task_id=run_id,
                    step_id=pause.step_id,
                    contract_id=None,
                    organization_id=organization_id,
                    event_type="agent-run.pause.resolved",
                    mode="audit",
                    payload=resolution_payload,
                    created_at=pause.created_at + timedelta(microseconds=1),
                )
            )
            await session.flush()

    @staticmethod
    def _task_snapshot(model: Any) -> AgentRunTaskSnapshot:
        return AgentRunTaskSnapshot(
            task_id=model.task_id,
            organization_id=model.organization_id,
            status=_task_status(model.status),
            application=getattr(model, "application", None),
            created_at=model.created_at,
            modified_at=model.modified_at,
        )

    @staticmethod
    def _step_snapshot(model: Any) -> AgentRunStepSnapshot:
        return AgentRunStepSnapshot(
            step_id=model.step_id,
            task_id=model.task_id,
            organization_id=model.organization_id,
            status=_step_status(model.status),
        )


def _task_status(value: str | TaskStatus) -> AgentRunTaskStatus:
    try:
        return AgentRunTaskStatus(value)
    except ValueError:
        return AgentRunTaskStatus.UNKNOWN


def _step_status(value: str | StepStatus) -> AgentRunStepStatus:
    try:
        return AgentRunStepStatus(value)
    except ValueError:
        return AgentRunStepStatus.UNKNOWN


async def _verify_attempt_identity(
    session: Any,
    item: Any,
    *,
    expected_status: ExecutionAttemptStatus,
    organization_id: str,
) -> None:
    if item.attempt_id is None and item.permit_id is None:
        return
    if item.attempt_id is None or item.permit_id is None:
        raise ValueError("Effect checkpoint has partial Permit/Attempt identity")
    attempt = (
        await session.scalars(
            select(ExecutionAttemptModel)
            .join(TaskModel, TaskModel.task_id == ExecutionAttemptModel.task_id)
            .where(
                ExecutionAttemptModel.attempt_id == item.attempt_id,
                TaskModel.organization_id == organization_id,
            )
        )
    ).first()
    permit = (
        await session.scalars(
            select(ExecutionPermitModel)
            .join(TaskModel, TaskModel.task_id == ExecutionPermitModel.task_id)
            .where(
                ExecutionPermitModel.permit_id == item.permit_id,
                TaskModel.organization_id == organization_id,
            )
        )
    ).first()
    if (
        attempt is None
        or permit is None
        or attempt.task_id != item.native_task_id
        or attempt.step_id != item.native_step_id
        or attempt.contract_id != item.native_contract_id
        or attempt.status != expected_status.value
        or permit.task_id != item.native_task_id
        or permit.step_id != item.native_step_id
        or permit.contract_id != item.native_contract_id
        or permit.status != "consumed"
    ):
        raise ValueError("Checkpoint disagrees with authoritative Permit/Attempt state")


async def _verify_checkpoint_native_state(
    store: SkyvernAgentRunStore,
    session: Any,
    checkpoint: Any,
    *,
    transition: Any,
    organization_id: str,
) -> None:
    for item in checkpoint.completed_prefix:
        task, step = await store.get_native_pair(
            session,
            task_id=item.native_task_id,
            step_id=item.native_step_id,
            organization_id=organization_id,
        )
        if (
            task is None
            or step is None
            or task.status != AgentRunTaskStatus.COMPLETED
            or step.status != AgentRunStepStatus.COMPLETED
            or step.step_id != item.native_step_id
        ):
            raise ValueError("Completed prefix disagrees with native Task/Step state")
        await _verify_attempt_identity(
            session,
            item,
            expected_status=ExecutionAttemptStatus.CONFIRMED,
            organization_id=organization_id,
        )
    for item in checkpoint.remaining_suffix:
        task, step = await store.get_native_pair(
            session,
            task_id=item.native_task_id,
            step_id=item.native_step_id,
            organization_id=organization_id,
        )
        if task is not None or step is not None:
            raise ValueError("Non-current suffix child became runnable")
    for item in checkpoint.superseded_suffix:
        task, step = await store.get_native_pair(
            session,
            task_id=item.native_task_id,
            step_id=item.native_step_id,
            organization_id=organization_id,
        )
        if task is not None and task.status != AgentRunTaskStatus.CANCELED:
            raise ValueError("Superseded native Task remains runnable")
        if step is not None and step.status != AgentRunStepStatus.CANCELED:
            raise ValueError("Superseded native Step remains runnable")
    active = checkpoint.active_step
    if active is None:
        return
    task, step = await store.get_native_pair(
        session,
        task_id=active.native_task_id,
        step_id=active.native_step_id,
        organization_id=organization_id,
    )
    if task is None or step is None:
        raise ValueError("Active native Task/Step state is partial or missing")
    if step.step_id != active.native_step_id:
        raise ValueError("Active native Step identity disagrees with checkpoint")
    transition_value = getattr(transition, "value", transition)
    if transition_value == "probe_blocked":
        if (
            task.status != AgentRunTaskStatus.PENDING_RESULT_PROBE
            or step.status != AgentRunStepStatus.PENDING_RESULT_PROBE
            or not active.attempt_id
            or not active.permit_id
        ):
            raise ValueError("Probe-blocked checkpoint lacks exact native state")
        await _verify_attempt_identity(
            session,
            active,
            expected_status=ExecutionAttemptStatus.UNKNOWN,
            organization_id=organization_id,
        )
    elif transition_value == "approval_required":
        if task.status != AgentRunTaskStatus.PENDING_APPROVAL or step.status != AgentRunStepStatus.PENDING_APPROVAL:
            raise ValueError("Approval checkpoint lacks exact pending native state")
    elif transition_value == "approval_resumed":
        if task.status != AgentRunTaskStatus.RESUMING or step.status != AgentRunStepStatus.RESUMING:
            raise ValueError("Resume checkpoint lacks exact resuming native state")
    elif transition_value in {"approval_rejected", "run_cancelled"}:
        if task.status != AgentRunTaskStatus.CANCELED or step.status != AgentRunStepStatus.CANCELED:
            raise ValueError("Terminal non-effect checkpoint lacks exact cancelled native state")


__all__ = ["SkyvernAgentRunStore"]
