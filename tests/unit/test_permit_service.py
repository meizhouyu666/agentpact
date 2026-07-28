"""Unit tests for persisted, one-time Phase 2 execution permits."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from enterprise.governance.contracts import DecisionOutcome, ExecutionEffect, PolicyDecision
from enterprise.governance.execution_profiles import (
    CUAEngine,
    CUAExecutionEvidence,
    ExecutionMechanism,
    ExecutionProfile,
    ExecutionProfileRejected,
)
from enterprise.governance.permit_service import PermitValidationError, consume_permit, issue_permit


class _ScalarResult:
    def __init__(self, model):
        self.model = model

    def first(self):
        return self.model


class FakePermitSession:
    """Minimal async-session double; a real DB covers the row lock in integration tests."""

    def __init__(self):
        self.models = []
        self.flush_count = 0

    def add(self, model):
        self.models.append(model)

    async def flush(self):
        self.flush_count += 1
        for index, model in enumerate(self.models, start=1):
            if model.permit_id is None:
                model.permit_id = f"permit_{index}"
            if model.status is None:
                model.status = "issued"

    async def scalars(self, _statement):
        return _ScalarResult(self.models[0] if self.models else None)


def _allow_decision() -> PolicyDecision:
    return PolicyDecision(
        decision_id="decision_1",
        intent_id="intent_1",
        outcome=DecisionOutcome.ALLOW,
        risk_level="low",
        policy_version="phase2-v1",
    )


def _issue(session: FakePermitSession, **overrides):
    profile = ExecutionProfile(mechanism=ExecutionMechanism.LOCATOR, evidence_refs=["dom:button"])
    values = {
        "db_session": session,
        "task_id": "task_1",
        "step_id": "step_1",
        "contract_id": "contract_1",
        "action_fingerprint": "action_fp",
        "observation_hash": "observation_fp",
        "decision": _allow_decision(),
        "effect": ExecutionEffect.INTERNAL_WRITE,
        "execution_profile": profile,
    }
    values.update(overrides)
    return asyncio.run(issue_permit(**values))


def test_issue_and_consume_permit_once():
    session = FakePermitSession()
    permit = _issue(session)

    assert permit.permit_id == "permit_1"
    assert permit.matches(
        action_fingerprint="action_fp", observation_id="observation_fp", now=permit.issued_at
    )
    assert session.models[0].decision_payload["execution_effect"] == "internal_write"
    assert session.models[0].decision_payload["execution_profile"]["mechanism"] == "locator"

    consumed = asyncio.run(
        consume_permit(
            db_session=session,
            permit_id=permit.permit_id,
            action_fingerprint="action_fp",
            observation_hash="observation_fp",
        )
    )

    assert consumed.used_at is not None
    assert session.models[0].status == "consumed"
    with pytest.raises(PermitValidationError, match="not available"):
        asyncio.run(
            consume_permit(
                db_session=session,
                permit_id=permit.permit_id,
                action_fingerprint="action_fp",
                observation_hash="observation_fp",
            )
        )


def test_permit_rejects_action_or_observation_drift_without_consuming():
    session = FakePermitSession()
    permit = _issue(session)

    with pytest.raises(PermitValidationError, match="does not match"):
        asyncio.run(
            consume_permit(
                db_session=session,
                permit_id=permit.permit_id,
                action_fingerprint="other_action_fp",
                observation_hash="observation_fp",
            )
        )

    assert session.models[0].status == "issued"


def test_expired_permit_is_marked_expired():
    session = FakePermitSession()
    permit = _issue(session, ttl_seconds=1)

    with pytest.raises(PermitValidationError, match="has expired"):
        asyncio.run(
            consume_permit(
                db_session=session,
                permit_id=permit.permit_id,
                action_fingerprint="action_fp",
                observation_hash="observation_fp",
                now=permit.expires_at + timedelta(microseconds=1),
            )
        )

    assert session.models[0].status == "expired"


def test_permit_requires_an_allow_decision_and_positive_ttl():
    session = FakePermitSession()
    denied = _allow_decision().model_copy(update={"outcome": DecisionOutcome.DENY})

    with pytest.raises(PermitValidationError, match="Only allow"):
        _issue(session, decision=denied)
    with pytest.raises(PermitValidationError, match="TTL must be positive"):
        _issue(session, ttl_seconds=0)


def test_permit_rejects_weak_external_write_profile_before_persistence():
    session = FakePermitSession()
    profile = ExecutionProfile(
        mechanism=ExecutionMechanism.COORDINATE,
        fallback_rank=2,
        evidence_refs=["vision:button"],
    )

    with pytest.raises(ExecutionProfileRejected, match="external commit boundary"):
        _issue(
            session,
            effect=ExecutionEffect.EXTERNAL_WRITE,
            execution_profile=profile,
        )
    assert session.models == []


def test_cua_permit_persists_exact_fresh_engine_evidence():
    session = FakePermitSession()
    profile = ExecutionProfile(
        mechanism=ExecutionMechanism.CUA_COORDINATE,
        evidence_refs=["screenshot:current"],
    )
    evidence = CUAExecutionEvidence(
        engine=CUAEngine.OPENAI,
        action_fingerprint="action_fp",
        observation_hash="observation_fp",
        evidence_refs=["screenshot:current"],
        captured_at=datetime.now(timezone.utc),
    )

    _issue(
        session,
        execution_profile=profile,
        cua_execution_evidence=evidence,
    )

    assert session.models[0].decision_payload["cua_execution_evidence"] == evidence.model_dump(mode="json")


def test_cua_permit_rejects_missing_or_detached_engine_evidence_before_persistence():
    profile = ExecutionProfile(
        mechanism=ExecutionMechanism.CUA_COORDINATE,
        evidence_refs=["screenshot:current"],
    )
    session = FakePermitSession()
    with pytest.raises(ExecutionProfileRejected, match="requires fresh engine evidence"):
        _issue(session, execution_profile=profile)
    assert session.models == []

    evidence = CUAExecutionEvidence(
        engine=CUAEngine.ANTHROPIC,
        action_fingerprint="other_action",
        observation_hash="observation_fp",
        evidence_refs=["screenshot:current"],
        captured_at=datetime.now(timezone.utc),
    )
    with pytest.raises(ExecutionProfileRejected, match="does not match"):
        _issue(session, execution_profile=profile, cua_execution_evidence=evidence)
    assert session.models == []
