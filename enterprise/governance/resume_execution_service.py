"""Claim and execute a native ``RESUMING`` task through fresh Agent perception."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from skyvern.forge import app
from skyvern.forge.sdk.db.models import StepModel, TaskModel
from skyvern.forge.sdk.models import StepStatus
from skyvern.forge.sdk.schemas.tasks import TaskStatus
from skyvern.schemas.runs import RunEngine, RunType


class ResumeExecutionError(ValueError):
    pass


@dataclass(frozen=True)
class ResumingTask:
    task_id: str
    step_id: str
    organization_id: str


async def discover_resuming_tasks(*, db_session: Any, limit: int = 100) -> list[ResumingTask]:
    """Find durable recovery work, including a crash before Agent starts."""

    if limit <= 0:
        raise ValueError("Resume discovery limit must be positive")
    steps = (
        await db_session.scalars(
            select(StepModel)
            .where(StepModel.status == StepStatus.resuming.value)
            .order_by(StepModel.created_at)
            .limit(limit)
        )
    ).all()
    work: list[ResumingTask] = []
    for step in steps:
        task = (
            await db_session.scalars(
                select(TaskModel).where(
                    TaskModel.task_id == step.task_id,
                    TaskModel.organization_id == step.organization_id,
                )
            )
        ).first()
        if task is not None and task.status in {TaskStatus.resuming.value, TaskStatus.running.value}:
            work.append(
                ResumingTask(task_id=step.task_id, step_id=step.step_id, organization_id=step.organization_id)
            )
    return work


async def claim_resuming_task_for_execution(
    *,
    db_session: Any,
    task_id: str,
    step_id: str,
    organization_id: str,
) -> None:
    """Claim a pre-execution recovery boundary without running a browser action.

    If a worker crashes after this commits, the task is `running` but the step
    remains `resuming`; a recovery scan can safely claim it again. Once Agent
    starts, it changes the step to `running`, after which normal execution-
    attempt recovery rules apply.
    """

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
        raise ResumeExecutionError("Task or step does not exist")
    if step.status != StepStatus.resuming.value:
        raise ResumeExecutionError("Only a resuming step may enter fresh Agent execution")
    if task.status not in {TaskStatus.resuming.value, TaskStatus.running.value}:
        raise ResumeExecutionError("Task is not eligible for approval recovery")

    task.status = TaskStatus.running.value
    await db_session.flush()


async def execute_resuming_task(
    *,
    task_id: str,
    step_id: str,
    organization_id: str,
) -> None:
    """Run a recovered step; the Agent starts with a new scrape and prompt."""

    async with app.DATABASE.Session() as session:
        await claim_resuming_task_for_execution(
            db_session=session,
            task_id=task_id,
            step_id=step_id,
            organization_id=organization_id,
        )
        await session.commit()

    organization = await app.DATABASE.get_organization(organization_id)
    task = await app.DATABASE.get_task(task_id=task_id, organization_id=organization_id)
    step = await app.DATABASE.get_step(step_id=step_id, organization_id=organization_id)
    if organization is None or task is None or step is None:
        raise ResumeExecutionError("Task recovery records disappeared after claim")

    engine = await _engine_for_task(task_id=task_id, organization_id=organization_id)
    await app.agent.execute_step(
        organization=organization,
        task=task,
        step=step,
        api_key=None,
        close_browser_on_completion=task.browser_session_id is None and not task.browser_address,
        browser_session_id=task.browser_session_id,
        engine=engine,
    )


async def _engine_for_task(*, task_id: str, organization_id: str) -> RunEngine:
    run = await app.DATABASE.get_run(run_id=task_id, organization_id=organization_id)
    if run and run.task_run_type == RunType.openai_cua:
        return RunEngine.openai_cua
    if run and run.task_run_type == RunType.anthropic_cua:
        return RunEngine.anthropic_cua
    if run and run.task_run_type == RunType.ui_tars:
        return RunEngine.ui_tars
    return RunEngine.skyvern_v1
