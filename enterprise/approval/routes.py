"""Database-backed approval queue API."""

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from enterprise.auth.dependencies import require_approver
from enterprise.auth.schemas import UserContext
from skyvern.forge import app

from .models import ApprovalRequestModel, ApprovalStatus
from .persistence import ApprovalPersistenceError, decide_approval_request

router = APIRouter(prefix="/enterprise/approvals", tags=["approvals"])


class ApprovalResponseSchema(BaseModel):
    approval_id: str
    task_id: str
    organization_id: str
    department_id: str
    business_line_id: str | None
    risk_level: str
    risk_reason: str
    operation_description: str | None
    screenshot_path: str | None
    approver_department_id: str
    status: str
    requested_at: str
    timeout_seconds: int


class DecisionRequest(BaseModel):
    note: str = Field(default="", max_length=2000)


class DecisionResponse(BaseModel):
    approval_id: str
    status: str
    decided_at: str
    message: str


def _user_can_approve(user: UserContext, approval_dept_id: str) -> bool:
    if user.is_org_admin or user.has_cross_org_approve:
        return True
    return user.get_role_in_department(approval_dept_id) in ("approver", "org_admin", "super_admin")


@router.get("/pending", response_model=list[ApprovalResponseSchema])
async def list_pending_approvals(
    user: UserContext = Depends(require_approver),
) -> list[ApprovalResponseSchema]:
    """List persisted pending requests that the current user can act on."""

    async with app.DATABASE.Session() as session:
        approvals = (
            await session.scalars(
                select(ApprovalRequestModel).where(
                    ApprovalRequestModel.status == ApprovalStatus.PENDING.value,
                    ApprovalRequestModel.organization_id == user.org_id,
                )
            )
        ).all()
    return [_approval_response(item) for item in approvals if _user_can_approve(user, item.approver_department_id)]


@router.post("/{approval_id}/approve", response_model=DecisionResponse)
async def approve_request(
    approval_id: str,
    body: DecisionRequest = DecisionRequest(),
    user: UserContext = Depends(require_approver),
) -> DecisionResponse:
    return await _decide_persisted_request(approval_id=approval_id, body=body, user=user, approved=True)


@router.post("/{approval_id}/reject", response_model=DecisionResponse)
async def reject_request(
    approval_id: str,
    body: DecisionRequest = DecisionRequest(),
    user: UserContext = Depends(require_approver),
) -> DecisionResponse:
    return await _decide_persisted_request(approval_id=approval_id, body=body, user=user, approved=False)


def _approval_response(approval: ApprovalRequestModel) -> ApprovalResponseSchema:
    return ApprovalResponseSchema(
        approval_id=approval.approval_id,
        task_id=approval.task_id,
        organization_id=approval.organization_id,
        department_id=approval.department_id,
        business_line_id=approval.business_line_id,
        risk_level=approval.risk_level,
        risk_reason=approval.risk_reason,
        operation_description=approval.operation_description,
        screenshot_path=approval.screenshot_path,
        approver_department_id=approval.approver_department_id,
        status=approval.status,
        requested_at=approval.requested_at.isoformat(),
        timeout_seconds=approval.timeout_seconds,
    )


async def _decide_persisted_request(
    *,
    approval_id: str,
    body: DecisionRequest,
    user: UserContext,
    approved: bool,
) -> DecisionResponse:
    async with app.DATABASE.Session() as session:
        approval = (
            await session.scalars(select(ApprovalRequestModel).where(ApprovalRequestModel.approval_id == approval_id))
        ).first()
        if approval is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Approval request {approval_id} not found",
            )
        if approval.organization_id != user.org_id or not _user_can_approve(user, approval.approver_department_id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You are not authorized for this approval",
            )
        try:
            approval = await decide_approval_request(
                db_session=session,
                approval_id=approval_id,
                organization_id=user.org_id,
                approver_user_id=user.user_id,
                approved=approved,
                decision_note=body.note,
            )
            await session.commit()
        except ApprovalPersistenceError as exc:
            await session.rollback()
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    return DecisionResponse(
        approval_id=approval_id,
        status=approval.status,
        decided_at=approval.decided_at.isoformat(),
        message="Approval granted" if approved else "Approval rejected",
    )
