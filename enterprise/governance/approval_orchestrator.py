"""Single-transaction creation of a governed approval pause."""

from __future__ import annotations

from typing import Any

from enterprise.approval.pubsub import build_approval_request
from enterprise.approval.routing import ApprovalRoute

from .approval_pause_service import ApprovalPauseState, pause_for_approval
from .contracts import ActionIntent, PolicyDecision
from .pending_action_service import attach_approval, create_pending_action


async def create_approval_pause(
    *,
    db_session: Any,
    task_id: str,
    step_id: str,
    organization_id: str,
    contract_id: str,
    source_department_id: str,
    action: Any,
    intent: ActionIntent,
    observation_hash: str,
    decision: PolicyDecision,
    route: ApprovalRoute,
    requester_user_id: str | None = None,
    business_line_id: str | None = None,
    screenshot_path: str | None = None,
    ttl_seconds: int = 3600,
) -> ApprovalPauseState:
    """Persist the complete approval pause in the caller's database transaction.

    The caller must commit only after this returns. Redis notification, if any,
    happens after commit and is deliberately outside this function.
    """

    pending_action = await create_pending_action(
        db_session=db_session,
        task_id=task_id,
        step_id=step_id,
        contract_id=contract_id,
        organization_id=organization_id,
        action=action,
        intent=intent,
        observation_hash=observation_hash,
        decision=decision,
        ttl_seconds=ttl_seconds,
    )
    approval = build_approval_request(
        task_id=task_id,
        org_id=organization_id,
        department_id=source_department_id,
        risk_level=decision.risk_level,
        risk_reason="; ".join(decision.reasons) or "Governance policy requires approval",
        route=route,
        requester_user_id=requester_user_id,
        business_line_id=business_line_id,
        operation_description=intent.operation,
        screenshot_path=screenshot_path,
        timeout_override=ttl_seconds,
    )
    db_session.add(approval)
    await db_session.flush()
    pending_action = await attach_approval(
        db_session=db_session,
        pending_action_id=pending_action.pending_action_id,
        approval_id=approval.approval_id,
        expected_row_version=pending_action.row_version,
    )
    return await pause_for_approval(
        db_session=db_session,
        task_id=task_id,
        step_id=step_id,
        organization_id=organization_id,
        pending_action=pending_action,
    )
