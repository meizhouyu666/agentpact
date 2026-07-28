"""Database scanner for approved pauses that require fresh browser observation."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from .approval_pause_service import ApprovalPauseError, ApprovalPauseState, begin_reobservation_after_approval
from .contracts import PendingActionStatus
from .models import PendingActionModel


async def prepare_approved_pauses_for_reobservation(
    *,
    db_session: Any,
    organization_id: str | None = None,
    limit: int = 100,
) -> list[ApprovalPauseState]:
    """Claim approved pauses and make them durably discoverable as ``RESUMING``.

    The caller commits the transaction before handing work to a scheduler. If a
    process crashes after commit, the native ``RESUMING`` status remains the
    durable signal for a later scheduler scan; no browser action is replayed.
    """

    if limit <= 0:
        raise ValueError("Recovery scan limit must be positive")

    statement = (
        select(PendingActionModel)
        .where(PendingActionModel.status == PendingActionStatus.APPROVED.value)
        .order_by(PendingActionModel.created_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    if organization_id is not None:
        statement = statement.where(PendingActionModel.organization_id == organization_id)
    pending_actions = (await db_session.scalars(statement)).all()

    resumptions: list[ApprovalPauseState] = []
    for pending_action in pending_actions:
        try:
            resumptions.append(
                await begin_reobservation_after_approval(
                    db_session=db_session,
                    task_id=pending_action.task_id,
                    step_id=pending_action.step_id,
                    organization_id=pending_action.organization_id,
                    pending_action_id=pending_action.pending_action_id,
                    expected_row_version=pending_action.row_version,
                )
            )
        except ApprovalPauseError:
            # A concurrently canceled/completed task is not executable. The
            # transaction owner can audit and resolve it separately; it must
            # never be retried as the stored browser action.
            continue
    return resumptions
