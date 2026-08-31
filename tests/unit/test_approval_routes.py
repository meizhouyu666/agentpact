"""Focused tests for the persisted approval queue API."""

from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from enterprise.approval import routes as approval_routes
from enterprise.approval.models import ApprovalRequestModel
from enterprise.approval.routes import DecisionRequest, approve_request, list_pending_approvals, reject_request
from enterprise.auth.schemas import DepartmentRole, UserContext
from enterprise.governance.models import PendingActionModel


class _Result:
    def __init__(self, values):
        self.values = values

    def all(self):
        return list(self.values)

    def first(self):
        return self.values[0] if self.values else None


class _Session:
    def __init__(self, approvals):
        self.approvals = list(approvals)
        self.committed = False
        self.rolled_back = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def scalars(self, statement):
        entity = statement.column_descriptions[0]["entity"]
        if entity is ApprovalRequestModel:
            return _Result(self.approvals)
        if entity is PendingActionModel:
            return _Result([])
        raise AssertionError(f"Unexpected query entity: {entity}")

    async def flush(self):
        return None

    async def commit(self):
        self.committed = True

    async def rollback(self):
        self.rolled_back = True


class _Database:
    def __init__(self, session):
        self._session = session

    def Session(self):
        return self._session


def _user(*, user_id="approver_1", org_id="org_1", department_id="dept_approver"):
    return UserContext(
        user_id=user_id,
        org_id=org_id,
        department_roles=[
            DepartmentRole(department_id=department_id, department_name="Approvers", role="approver")
        ],
        business_line_ids=[],
    )


def _approval(**overrides):
    values = {
        "approval_id": "apr_1",
        "task_id": "task_1",
        "organization_id": "org_1",
        "department_id": "dept_source",
        "business_line_id": None,
        "requester_user_id": "requester_1",
        "risk_level": "high",
        "risk_reason": "Domain policy requires approval",
        "operation_description": "Submit payment",
        "screenshot_path": None,
        "approver_department_id": "dept_approver",
        "approver_role": "approver",
        "notify_department_ids": None,
        "status": "pending",
        "requested_at": datetime(2026, 3, 7, 10, 0, 0),
        "timeout_seconds": 3600,
    }
    values.update(overrides)
    return ApprovalRequestModel(**values)


@pytest.mark.asyncio
async def test_list_pending_approvals_filters_by_approver_department(monkeypatch):
    visible = _approval()
    hidden = _approval(approval_id="apr_2", approver_department_id="dept_other")
    monkeypatch.setattr(approval_routes, "app", SimpleNamespace(DATABASE=_Database(_Session([visible, hidden]))))

    result = await list_pending_approvals(_user())

    assert [item.approval_id for item in result] == ["apr_1"]


@pytest.mark.asyncio
async def test_approve_persists_decision(monkeypatch):
    approval = _approval()
    session = _Session([approval])
    monkeypatch.setattr(approval_routes, "app", SimpleNamespace(DATABASE=_Database(session)))

    result = await approve_request("apr_1", DecisionRequest(note="Reviewed"), _user())

    assert result.status == "approved"
    assert approval.approver_user_id == "approver_1"
    assert approval.decision_note == "Reviewed"
    assert session.committed is True


@pytest.mark.asyncio
async def test_reject_persists_decision(monkeypatch):
    approval = _approval()
    session = _Session([approval])
    monkeypatch.setattr(approval_routes, "app", SimpleNamespace(DATABASE=_Database(session)))

    result = await reject_request("apr_1", DecisionRequest(note="Denied"), _user())

    assert result.status == "rejected"
    assert approval.decision_note == "Denied"


@pytest.mark.asyncio
async def test_decision_rejects_wrong_organization(monkeypatch):
    session = _Session([_approval(organization_id="org_other")])
    monkeypatch.setattr(approval_routes, "app", SimpleNamespace(DATABASE=_Database(session)))

    with pytest.raises(HTTPException) as exc_info:
        await approve_request("apr_1", DecisionRequest(), _user())

    assert exc_info.value.status_code == 403
    assert session.committed is False


@pytest.mark.asyncio
async def test_requester_cannot_approve_own_request(monkeypatch):
    session = _Session([_approval(requester_user_id="approver_1")])
    monkeypatch.setattr(approval_routes, "app", SimpleNamespace(DATABASE=_Database(session)))

    with pytest.raises(HTTPException) as exc_info:
        await approve_request("apr_1", DecisionRequest(), _user())

    assert exc_info.value.status_code == 409
    assert session.rolled_back is True
