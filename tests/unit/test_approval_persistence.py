"""Tests for database approval decisions linked to governance pauses."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from enterprise.approval.models import ApprovalRequestModel, ApprovalStatus
from enterprise.approval.persistence import ApprovalPersistenceError, decide_approval_request
from enterprise.governance.models import PendingActionModel


class _Result:
    def __init__(self, value):
        self.value = value

    def first(self):
        return self.value


class FakeSession:
    def __init__(self, approval, pending):
        self.approval = approval
        self.pending = pending

    async def scalars(self, statement):
        entity = statement.column_descriptions[0]["entity"]
        if entity is ApprovalRequestModel:
            return _Result(self.approval)
        if entity is PendingActionModel:
            return _Result(self.pending)
        raise AssertionError(f"Unexpected entity query: {entity}")

    async def flush(self):
        pass


def _session(approval_status="pending"):
    approval = ApprovalRequestModel(
        approval_id="apr_1",
        task_id="task_1",
        organization_id="org_1",
        department_id="dept_1",
        requester_user_id="user_requester",
        risk_level="critical",
        risk_reason="payment",
        approver_department_id="dept_approver",
        approver_role="approver",
        status=approval_status,
        timeout_seconds=3600,
    )
    pending = PendingActionModel(
        pending_action_id="pending_1",
        task_id="task_1",
        step_id="step_1",
        contract_id="contract_1",
        organization_id="org_1",
        action_fingerprint="action_fp",
        observation_hash="obs_hash",
        action_payload={},
        intent_payload={},
        decision_payload={},
        approval_id="apr_1",
        status="pending",
        row_version=2,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    return FakeSession(approval, pending)


def test_persisted_approval_decision_updates_linked_pending_action():
    session = _session()
    approval = asyncio.run(
        decide_approval_request(
            db_session=session,
            approval_id="apr_1",
            organization_id="org_1",
            approver_user_id="user_approver",
            approved=True,
            decision_note="verified",
        )
    )

    assert approval.status == ApprovalStatus.APPROVED.value
    assert session.pending.status == "approved"
    assert session.pending.row_version == 3


def test_persisted_approval_decision_rejects_duplicate_or_cross_org_calls():
    with pytest.raises(ApprovalPersistenceError, match="another organization"):
        asyncio.run(
            decide_approval_request(
                db_session=_session(),
                approval_id="apr_1",
                organization_id="org_other",
                approver_user_id="user_approver",
                approved=False,
                decision_note="",
            )
        )
    with pytest.raises(ApprovalPersistenceError, match="not pending"):
        asyncio.run(
            decide_approval_request(
                db_session=_session(approval_status="approved"),
                approval_id="apr_1",
                organization_id="org_1",
                approver_user_id="user_approver",
                approved=False,
                decision_note="",
            )
        )


def test_persisted_approval_decision_rejects_requester_self_approval():
    with pytest.raises(ApprovalPersistenceError, match="cannot approve their own"):
        asyncio.run(
            decide_approval_request(
                db_session=_session(),
                approval_id="apr_1",
                organization_id="org_1",
                approver_user_id="user_requester",
                approved=True,
                decision_note="",
            )
        )
