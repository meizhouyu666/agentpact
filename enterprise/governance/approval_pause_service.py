"""Atomic native Skyvern task/step transitions for approval pauses."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel
from sqlalchemy import select

from skyvern.forge.sdk.db.models import StepModel, TaskModel
from skyvern.forge.sdk.models import StepStatus
from skyvern.forge.sdk.schemas.tasks import TaskStatus

from .contracts import PendingAction
from .models import PendingActionModel
from .pending_action_service import invalidate_approved_action_for_reobservation


class ApprovalPauseError(ValueError):
    pass


class ApprovalPauseState(BaseModel):
    task_id: str
    step_id: str
    task_status: TaskStatus
    step_status: StepStatus
    pending_action: PendingAction


async def pause_for_approval(
    *,
    db_session: Any,
    task_id: str,
    step_id: str,
    organization_id: str,
    pending_action: PendingAction,
) -> ApprovalPauseState:
    """Atomically move a running task and step into pending approval."""

    _require_matching_pending_action(pending_action, task_id=task_id, step_id=step_id)
    task, step = await _load_task_and_step(
        db_session=db_session,
        task_id=task_id,
        step_id=step_id,
        organization_id=organization_id,
    )
    if task.status != TaskStatus.running.value or step.status != StepStatus.running.value:
        raise ApprovalPauseError("Only running task and step can enter pending approval")

    task.status = TaskStatus.pending_approval.value
    step.status = StepStatus.pending_approval.value
    await db_session.flush()
    return ApprovalPauseState(
        task_id=task_id,
        step_id=step_id,
        task_status=TaskStatus.pending_approval,
        step_status=StepStatus.pending_approval,
        pending_action=pending_action,
    )


async def begin_reobservation_after_approval(
    *,
    db_session: Any,
    task_id: str,
    step_id: str,
    organization_id: str,
    pending_action_id: str,
    expected_row_version: int,
) -> ApprovalPauseState:
    """Consume approval state and transition native state machines to resuming.

    This only makes the task eligible for a scheduler/recovery worker.  The
    worker must acquire the browser, re-scrape, and build a fresh governance
    plan; it must not execute the stored pending-action payload.
    """

    pending_model = (
        await db_session.scalars(
            select(PendingActionModel)
            .where(PendingActionModel.pending_action_id == pending_action_id)
            .with_for_update()
        )
    ).first()
    if pending_model is None or pending_model.status != "approved" or pending_model.row_version != expected_row_version:
        raise ApprovalPauseError("Approved pending action is not available for re-observation")
    if pending_model.task_id != task_id or pending_model.step_id != step_id:
        raise ApprovalPauseError("Pending action does not belong to this task step")
    task, step = await _load_task_and_step(
        db_session=db_session,
        task_id=task_id,
        step_id=step_id,
        organization_id=organization_id,
    )
    if task.status != TaskStatus.pending_approval.value or step.status != StepStatus.pending_approval.value:
        raise ApprovalPauseError("Only pending-approval task and step can resume")

    pending_action = await invalidate_approved_action_for_reobservation(
        db_session=db_session,
        pending_action_id=pending_action_id,
        expected_row_version=expected_row_version,
    )
    task.status = TaskStatus.resuming.value
    step.status = StepStatus.resuming.value
    await db_session.flush()
    return ApprovalPauseState(
        task_id=task_id,
        step_id=step_id,
        task_status=TaskStatus.resuming,
        step_status=StepStatus.resuming,
        pending_action=pending_action,
    )


async def _load_task_and_step(
    *,
    db_session: Any,
    task_id: str,
    step_id: str,
    organization_id: str,
) -> tuple[TaskModel, StepModel]:
    task = (
        await db_session.scalars(
            select(TaskModel)
            .where(TaskModel.task_id == task_id, TaskModel.organization_id == organization_id)
            .with_for_update()
        )
    ).first()
    step = (
        await db_session.scalars(
            select(StepModel)
            .where(
                StepModel.step_id == step_id,
                StepModel.task_id == task_id,
                StepModel.organization_id == organization_id,
            )
            .with_for_update()
        )
    ).first()
    if task is None or step is None:
        raise ApprovalPauseError("Task or step does not exist")
    return task, step


def _require_matching_pending_action(pending_action: PendingAction, *, task_id: str, step_id: str) -> None:
    if pending_action.task_id != task_id or pending_action.step_id != step_id:
        raise ApprovalPauseError("Pending action does not belong to this task step")
    if pending_action.status.value != "pending":
        raise ApprovalPauseError("Pending action is not in a pause-compatible state")
