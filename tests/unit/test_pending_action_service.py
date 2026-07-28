"""Tests for database-backed Phase 2 approval pauses."""

import asyncio
from datetime import timedelta

import pytest

from enterprise.governance.contracts import (
    ActionIntent,
    DecisionOutcome,
    ExecutionEffect,
    PendingActionStatus,
    PolicyDecision,
)
from enterprise.governance.models import PendingActionModel
from enterprise.governance.pending_action_service import (
    PendingActionError,
    attach_approval,
    create_pending_action,
    invalidate_approved_action_for_reobservation,
    record_approval_decision,
)


class _Result:
    def __init__(self, value):
        self.value = value

    def first(self):
        return self.value


class FakeSession:
    def __init__(self):
        self.pending_actions = []

    def add(self, model):
        if isinstance(model, PendingActionModel):
            self.pending_actions.append(model)

    async def flush(self):
        for index, model in enumerate(self.pending_actions, start=1):
            if model.pending_action_id is None:
                model.pending_action_id = f"pending_{index}"
            if model.status is None:
                model.status = "pending"

    async def scalars(self, _statement):
        return _Result(self.pending_actions[0] if self.pending_actions else None)


class FakeAction:
    def model_dump(self, **_kwargs):
        return {"action_type": "click", "element_id": "element_1", "text": "13800138000"}


def _intent():
    return ActionIntent(
        intent_id="intent_1",
        task_id="task_1",
        step_id="step_1",
        action_fingerprint="action_fp",
        observation_id="obs_1",
        operation="payment",
        effect=ExecutionEffect.EXTERNAL_WRITE,
        target={"text": "Pay 100"},
    )


def _decision(outcome=DecisionOutcome.REQUIRE_APPROVAL):
    return PolicyDecision(
        decision_id="decision_1",
        intent_id="intent_1",
        outcome=outcome,
        risk_level="critical",
        policy_version="phase2-v1",
    )


def _create(session, **kwargs):
    values = {
        "db_session": session,
        "task_id": "task_1",
        "step_id": "step_1",
        "contract_id": "contract_1",
        "organization_id": "org_1",
        "action": FakeAction(),
        "intent": _intent(),
        "observation_hash": "observation_hash_1",
        "decision": _decision(),
    }
    values.update(kwargs)
    return asyncio.run(create_pending_action(**values))


def test_pending_action_is_redacted_and_requires_approval():
    session = FakeSession()
    pending = _create(session)

    assert pending.status == PendingActionStatus.PENDING
    assert session.pending_actions[0].action_payload["text"] == "[REDACTED_PII]"
    with pytest.raises(PendingActionError, match="Only approval-required"):
        _create(session, decision=_decision(DecisionOutcome.ALLOW))


def test_pending_action_approval_then_reobservation_invalidates_old_action():
    session = FakeSession()
    pending = _create(session)
    attached = asyncio.run(
        attach_approval(
            db_session=session,
            pending_action_id=pending.pending_action_id,
            approval_id="apr_1",
            expected_row_version=pending.row_version,
        )
    )
    approved = asyncio.run(
        record_approval_decision(
            db_session=session,
            pending_action_id=pending.pending_action_id,
            approval_id="apr_1",
            approved=True,
            expected_row_version=attached.row_version,
        )
    )
    invalidated = asyncio.run(
        invalidate_approved_action_for_reobservation(
            db_session=session,
            pending_action_id=pending.pending_action_id,
            expected_row_version=approved.row_version,
        )
    )

    assert approved.status == PendingActionStatus.APPROVED
    assert invalidated.status == PendingActionStatus.INVALIDATED
    assert invalidated.row_version == 4


def test_pending_action_rejects_stale_or_mismatched_approval_decisions():
    session = FakeSession()
    pending = _create(session)
    attached = asyncio.run(
        attach_approval(
            db_session=session,
            pending_action_id=pending.pending_action_id,
            approval_id="apr_1",
            expected_row_version=pending.row_version,
        )
    )

    with pytest.raises(PendingActionError, match="version conflict"):
        asyncio.run(
            record_approval_decision(
                db_session=session,
                pending_action_id=pending.pending_action_id,
                approval_id="apr_1",
                approved=True,
                expected_row_version=pending.row_version,
            )
        )
    with pytest.raises(PendingActionError, match="does not match"):
        asyncio.run(
            record_approval_decision(
                db_session=session,
                pending_action_id=pending.pending_action_id,
                approval_id="apr_other",
                approved=True,
                expected_row_version=attached.row_version,
            )
        )


def test_expired_pending_action_is_not_decidable():
    session = FakeSession()
    pending = _create(session, ttl_seconds=1)
    attached = asyncio.run(
        attach_approval(
            db_session=session,
            pending_action_id=pending.pending_action_id,
            approval_id="apr_1",
            expected_row_version=pending.row_version,
        )
    )

    with pytest.raises(PendingActionError, match="has expired"):
        asyncio.run(
            record_approval_decision(
                db_session=session,
                pending_action_id=pending.pending_action_id,
                approval_id="apr_1",
                approved=True,
                expected_row_version=attached.row_version,
                now=session.pending_actions[0].expires_at + timedelta(microseconds=1),
            )
        )
    assert session.pending_actions[0].status == PendingActionStatus.EXPIRED.value


def test_pending_action_rejects_a_second_round_while_the_first_is_approved():
    session = FakeSession()
    pending = _create(session)
    attached = asyncio.run(
        attach_approval(
            db_session=session,
            pending_action_id=pending.pending_action_id,
            approval_id="apr_1",
            expected_row_version=pending.row_version,
        )
    )
    asyncio.run(
        record_approval_decision(
            db_session=session,
            pending_action_id=pending.pending_action_id,
            approval_id="apr_1",
            approved=True,
            expected_row_version=attached.row_version,
        )
    )

    with pytest.raises(PendingActionError, match="already has a pending approval"):
        _create(session)
