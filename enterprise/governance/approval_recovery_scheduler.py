"""Optional background scheduler for durable Phase 2 approval recovery."""

from __future__ import annotations

import asyncio

import structlog

from skyvern.config import settings
from skyvern.forge import app

from .approval_recovery_service import prepare_approved_pauses_for_reobservation
from .resume_execution_service import discover_resuming_tasks, execute_resuming_task

LOG = structlog.get_logger()

_recovery_task: asyncio.Task | None = None


async def run_approval_recovery_cycle() -> int:
    """Persist recovery transitions, then start fresh Agent perception for each task."""

    async with app.DATABASE.Session() as session:
        await prepare_approved_pauses_for_reobservation(
            db_session=session,
            limit=settings.GOVERNANCE_RECOVERY_BATCH_SIZE,
        )
        await session.commit()

    async with app.DATABASE.Session() as session:
        resumptions = await discover_resuming_tasks(
            db_session=session,
            limit=settings.GOVERNANCE_RECOVERY_BATCH_SIZE,
        )

    if not settings.ENABLE_GOVERNANCE_RECOVERY_EXECUTION:
        LOG.info(
            "Approval recovery execution is disabled; tasks remain durably resuming",
            resumptions=len(resumptions),
        )
        return 0

    started = 0
    for resumption in resumptions:
        try:
            await execute_resuming_task(
                task_id=resumption.task_id,
                step_id=resumption.step_id,
                organization_id=resumption.organization_id,
            )
            started += 1
        except Exception:
            # The step remains RESUMING until Agent itself starts. A later
            # cycle can safely retry this pre-browser handoff.
            LOG.exception("Approval recovery execution handoff failed", **resumption.__dict__)
    return started


async def approval_recovery_scheduler() -> None:
    """Run recovery once on startup, then on a bounded periodic cadence."""

    LOG.info(
        "Approval recovery scheduler started",
        interval_seconds=settings.GOVERNANCE_RECOVERY_INTERVAL_SECONDS,
    )
    while True:
        try:
            await run_approval_recovery_cycle()
            await asyncio.sleep(settings.GOVERNANCE_RECOVERY_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            LOG.info("Approval recovery scheduler cancelled")
            break
        except Exception:
            LOG.exception("Approval recovery cycle failed")
            await asyncio.sleep(settings.GOVERNANCE_RECOVERY_INTERVAL_SECONDS)


def start_approval_recovery_scheduler() -> asyncio.Task | None:
    global _recovery_task
    if not settings.ENABLE_GOVERNANCE_RECOVERY_SCHEDULER:
        return None
    if _recovery_task is not None and not _recovery_task.done():
        return _recovery_task
    _recovery_task = asyncio.create_task(approval_recovery_scheduler())
    return _recovery_task


async def stop_approval_recovery_scheduler() -> None:
    global _recovery_task
    if _recovery_task is not None and not _recovery_task.done():
        _recovery_task.cancel()
        try:
            await _recovery_task
        except asyncio.CancelledError:
            pass
    _recovery_task = None
