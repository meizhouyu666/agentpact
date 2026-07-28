"""Transactional approval decisions for Phase 2 governed pauses."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select

from enterprise.governance.models import PendingActionModel
from enterprise.governance.pending_action_service import record_approval_decision

from .models import ApprovalRequestModel, ApprovalStatus


class ApprovalPersistenceError(ValueError):
    pass


async def decide_approval_request(
    *,
    db_session: Any,
    approval_id: str,
    organization_id: str,
    approver_user_id: str,
    approved: bool,
    decision_note: str,
    now: datetime | None = None,
) -> ApprovalRequestModel:
    """Persist an approval decision and linked pending-action state atomically."""

    approval = (
        await db_session.scalars(
            select(ApprovalRequestModel)
            .where(ApprovalRequestModel.approval_id == approval_id)
            .with_for_update()
        )
    ).first()
    if approval is None:
        raise ApprovalPersistenceError("Approval request does not exist")
    if approval.organization_id != organization_id:
        raise ApprovalPersistenceError("Approval request belongs to another organization")
    if approval.status != ApprovalStatus.PENDING.value:
        raise ApprovalPersistenceError("Approval request is not pending")
    if approval.requester_user_id is not None and approval.requester_user_id == approver_user_id:
        raise ApprovalPersistenceError("Requester cannot approve their own request")

    pending_action = (
        await db_session.scalars(
            select(PendingActionModel)
            .where(PendingActionModel.approval_id == approval_id)
            .with_for_update()
        )
    ).first()
    timestamp = now or datetime.utcnow()
    approval.status = ApprovalStatus.APPROVED.value if approved else ApprovalStatus.REJECTED.value
    approval.approver_user_id = approver_user_id
    approval.decision_note = decision_note
    approval.decided_at = timestamp
    if pending_action is not None:
        await record_approval_decision(
            db_session=db_session,
            pending_action_id=pending_action.pending_action_id,
            approval_id=approval_id,
            approved=approved,
            expected_row_version=pending_action.row_version,
            now=timestamp,
        )
    await db_session.flush()
    return approval
