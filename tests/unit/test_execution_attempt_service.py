"""Tests for the crash-aware execution-attempt state machine."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from enterprise.governance.contracts import (
    DecisionOutcome,
    ExecutionAttemptStatus,
    ExecutionEffect,
    PolicyDecision,
)
from enterprise.governance.execution_attempt_service import (
    ExecutionAttemptError,
    ExecutionAttemptRecoveryRequired,
    authorize_execution_attempt,
    confirm_execution_attempt,
    mark_execution_attempt_executing,
    mark_execution_attempt_unknown,
    resolve_unknown_execution_attempt,
)
from enterprise.governance.execution_profiles import (
    CUAEngine,
    CUAExecutionEvidence,
    ExecutionMechanism,
    ExecutionProfile,
)
from enterprise.governance.models import ExecutionAttemptModel, ExecutionPermitModel
from enterprise.governance.permit_service import issue_permit

PROFILE = ExecutionProfile(mechanism=ExecutionMechanism.LOCATOR, evidence_refs=["dom:button"])


class _Result:
    def __init__(self, value):
        self.value = value

    def first(self):
        return self.value


class FakeSession:
    def __init__(self):
        self.permits = []
        self.attempts = []
        self.flush_count = 0

    def add(self, model):
        if isinstance(model, ExecutionPermitModel):
            self.permits.append(model)
        elif isinstance(model, ExecutionAttemptModel):
            self.attempts.append(model)

    async def flush(self):
        self.flush_count += 1
        for index, model in enumerate(self.permits, start=1):
            if model.permit_id is None:
                model.permit_id = f"permit_{index}"
            if model.status is None:
                model.status = "issued"
        for index, model in enumerate(self.attempts, start=1):
            if model.attempt_id is None:
                model.attempt_id = f"attempt_{index}"
            if model.status is None:
                model.status = "authorized"

    async def scalars(self, statement):
        entity = statement.column_descriptions[0]["entity"]
        if entity is ExecutionPermitModel:
            return _Result(self.permits[0] if self.permits else None)
        if entity is ExecutionAttemptModel:
            return _Result(self.attempts[0] if self.attempts else None)
        raise AssertionError(f"Unexpected entity query: {entity}")


def _decision() -> PolicyDecision:
    return PolicyDecision(
        decision_id="decision_1",
        intent_id="intent_1",
        outcome=DecisionOutcome.ALLOW,
        risk_level="low",
        policy_version="phase2-v1",
    )


def _issue(
    session: FakeSession,
    *,
    execution_profile: ExecutionProfile = PROFILE,
    cua_execution_evidence: CUAExecutionEvidence | None = None,
):
    return asyncio.run(
        issue_permit(
            db_session=session,
            task_id="task_1",
            step_id="step_1",
            contract_id="contract_1",
            action_fingerprint="action_fp",
            observation_hash="observation_fp",
            decision=_decision(),
            effect=ExecutionEffect.INTERNAL_WRITE,
            execution_profile=execution_profile,
            cua_execution_evidence=cua_execution_evidence,
        )
    )


def _authorize(
    session: FakeSession,
    permit_id: str,
    idempotency_key: str = "payment:request_1",
    *,
    effect: ExecutionEffect = ExecutionEffect.INTERNAL_WRITE,
    execution_profile: ExecutionProfile = PROFILE,
    cua_execution_evidence: CUAExecutionEvidence | None = None,
    now: datetime | None = None,
):
    return asyncio.run(
        authorize_execution_attempt(
            db_session=session,
            permit_id=permit_id,
            action_fingerprint="action_fp",
            observation_hash="observation_fp",
            idempotency_key=idempotency_key,
            effect=effect,
            execution_profile=execution_profile,
            cua_execution_evidence=cua_execution_evidence,
            now=now,
        )
    )


def test_authorized_attempt_consumes_permit_before_browser_execution():
    session = FakeSession()
    permit = _issue(session)

    attempt = _authorize(session, permit.permit_id)

    assert attempt.status == ExecutionAttemptStatus.AUTHORIZED
    assert session.permits[0].status == "consumed"
    assert attempt.contract_id == "contract_1"


def test_attempt_rejects_effect_or_profile_downgrade_without_consuming_permit():
    session = FakeSession()
    permit = _issue(session)

    with pytest.raises(ExecutionAttemptError, match="authorized effect and profile"):
        _authorize(session, permit.permit_id, effect=ExecutionEffect.NONE)
    assert session.permits[0].status == "issued"

    weaker_profile = ExecutionProfile(
        mechanism=ExecutionMechanism.LABEL,
        fallback_rank=1,
        evidence_refs=["dom:button"],
    )
    with pytest.raises(ExecutionAttemptError, match="authorized effect and profile"):
        _authorize(session, permit.permit_id, execution_profile=weaker_profile)
    assert session.permits[0].status == "issued"


def test_execution_attempt_can_be_confirmed_only_after_executing():
    session = FakeSession()
    attempt = _authorize(session, _issue(session).permit_id)

    executing = asyncio.run(mark_execution_attempt_executing(db_session=session, attempt_id=attempt.attempt_id))
    confirmed = asyncio.run(
        confirm_execution_attempt(
            db_session=session,
            attempt_id=attempt.attempt_id,
            result_probe={"receipt_id": "receipt_1"},
        )
    )

    assert executing.status == ExecutionAttemptStatus.EXECUTING
    assert executing.started_at is not None
    assert confirmed.status == ExecutionAttemptStatus.CONFIRMED
    assert confirmed.completed_at is not None
    assert confirmed.result_probe == {"receipt_id": "receipt_1"}


def test_unknown_execution_cannot_be_auto_confirmed_or_replayed():
    session = FakeSession()
    attempt = _authorize(session, _issue(session).permit_id)
    asyncio.run(mark_execution_attempt_executing(db_session=session, attempt_id=attempt.attempt_id))
    unknown = asyncio.run(
        mark_execution_attempt_unknown(
            db_session=session,
            attempt_id=attempt.attempt_id,
            error_message="browser disconnected after submit",
        )
    )

    assert unknown.status == ExecutionAttemptStatus.UNKNOWN
    with pytest.raises(ExecutionAttemptError, match="cannot transition"):
        asyncio.run(confirm_execution_attempt(db_session=session, attempt_id=attempt.attempt_id))
    with pytest.raises(ExecutionAttemptRecoveryRequired, match="recovery probe"):
        _authorize(session, _issue(session).permit_id)


def test_result_probe_can_resolve_unknown_attempt():
    session = FakeSession()
    attempt = _authorize(session, _issue(session).permit_id)
    asyncio.run(mark_execution_attempt_executing(db_session=session, attempt_id=attempt.attempt_id))
    asyncio.run(
        mark_execution_attempt_unknown(
            db_session=session,
            attempt_id=attempt.attempt_id,
            error_message="response lost",
        )
    )

    resolved = asyncio.run(
        resolve_unknown_execution_attempt(
            db_session=session,
            attempt_id=attempt.attempt_id,
            confirmed=True,
            result_probe={"payment_reference": "ref_1"},
        )
    )

    assert resolved.status == ExecutionAttemptStatus.CONFIRMED
    assert resolved.result_probe == {"payment_reference": "ref_1"}


def test_duplicate_idempotency_key_does_not_consume_second_permit():
    session = FakeSession()
    _authorize(session, _issue(session).permit_id)
    second_permit = _issue(session)

    with pytest.raises(ExecutionAttemptRecoveryRequired):
        _authorize(session, second_permit.permit_id)

    assert session.permits[1].status == "issued"


def test_execution_attempt_requires_an_idempotency_key():
    session = FakeSession()

    with pytest.raises(ExecutionAttemptError, match="requires an idempotency key"):
        _authorize(session, _issue(session).permit_id, idempotency_key="")


def test_execution_attempt_requires_an_existing_permit():
    session = FakeSession()

    with pytest.raises(ExecutionAttemptError, match="permit does not exist"):
        _authorize(session, "permit_missing")


def test_cua_attempt_rejects_substituted_or_stale_evidence_before_consuming_permit():
    now = datetime.now(timezone.utc)
    profile = ExecutionProfile(
        mechanism=ExecutionMechanism.CUA_COORDINATE,
        evidence_refs=["screenshot:current"],
    )
    evidence = CUAExecutionEvidence(
        engine=CUAEngine.UI_TARS,
        action_fingerprint="action_fp",
        observation_hash="observation_fp",
        evidence_refs=["screenshot:current"],
        captured_at=now,
    )
    session = FakeSession()
    permit = _issue(session, execution_profile=profile, cua_execution_evidence=evidence)
    substituted = evidence.model_copy(update={"engine": CUAEngine.OPENAI})

    with pytest.raises(ExecutionAttemptError, match="authorized CUA engine evidence"):
        _authorize(
            session,
            permit.permit_id,
            execution_profile=profile,
            cua_execution_evidence=substituted,
            now=now,
        )
    assert session.permits[0].status == "issued"

    with pytest.raises(ExecutionAttemptError, match="stale"):
        _authorize(
            session,
            permit.permit_id,
            execution_profile=profile,
            cua_execution_evidence=evidence,
            now=now + timedelta(seconds=31),
        )
    assert session.permits[0].status == "issued"
