"""Construction helpers for persisted approval requests."""

from .models import DEFAULT_TIMEOUTS, ApprovalRequestModel, ApprovalStatus, generate_approval_id
from .routing import ApprovalRoute


def build_approval_request(
    *,
    task_id: str,
    org_id: str,
    department_id: str,
    risk_level: str,
    risk_reason: str,
    route: ApprovalRoute,
    requester_user_id: str | None = None,
    business_line_id: str | None = None,
    operation_description: str | None = None,
    screenshot_path: str | None = None,
    timeout_override: int | None = None,
) -> ApprovalRequestModel:
    """Build, but do not persist, an approval request."""

    timeout = timeout_override or DEFAULT_TIMEOUTS.get(risk_level, 3600)
    notify_departments = ",".join(route.notify_department_ids) if route.notify_department_ids else None
    return ApprovalRequestModel(
        approval_id=generate_approval_id(),
        task_id=task_id,
        organization_id=org_id,
        department_id=department_id,
        business_line_id=business_line_id,
        requester_user_id=requester_user_id,
        risk_level=risk_level,
        risk_reason=risk_reason,
        operation_description=operation_description,
        screenshot_path=screenshot_path,
        approver_department_id=route.approver_department_id or department_id,
        approver_role=route.approver_role,
        notify_department_ids=notify_departments,
        status=ApprovalStatus.PENDING.value,
        timeout_seconds=timeout,
    )
