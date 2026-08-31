"""Tests for constructing persisted approval requests."""

from enterprise.approval.models import DEFAULT_TIMEOUTS, ApprovalStatus
from enterprise.approval.requests import build_approval_request
from enterprise.approval.routing import ApprovalRoute


def test_build_approval_request_uses_route_and_identity_snapshot():
    request = build_approval_request(
        task_id="task_1",
        org_id="org_1",
        department_id="dept_source",
        business_line_id="line_1",
        requester_user_id="user_1",
        risk_level="critical",
        risk_reason="Domain policy requires approval",
        route=ApprovalRoute(
            requires_approval=True,
            approver_department_id="dept_approver",
            notify_department_ids=["dept_risk", "dept_audit"],
        ),
    )

    assert request.approval_id.startswith("apr_")
    assert request.status == ApprovalStatus.PENDING.value
    assert request.requester_user_id == "user_1"
    assert request.approver_department_id == "dept_approver"
    assert request.notify_department_ids == "dept_risk,dept_audit"
    assert request.timeout_seconds == DEFAULT_TIMEOUTS["critical"]


def test_build_approval_request_honors_timeout_override_and_department_fallback():
    request = build_approval_request(
        task_id="task_1",
        org_id="org_1",
        department_id="dept_source",
        risk_level="custom",
        risk_reason="Explicit policy decision",
        route=ApprovalRoute(requires_approval=True),
        timeout_override=120,
    )

    assert request.approver_department_id == "dept_source"
    assert request.timeout_seconds == 120
