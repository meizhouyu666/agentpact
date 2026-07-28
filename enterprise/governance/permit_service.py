"""Persisted, one-time permit primitives for the future enforce path."""

from __future__ import annotations

import hmac
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select

from .contracts import DecisionOutcome, ExecutionEffect, ExecutionPermit, PolicyDecision
from .execution_profiles import (
    CUAExecutionEvidence,
    ExecutionProfile,
    require_allowed_profile,
    require_cua_execution_evidence,
)
from .models import ExecutionPermitModel


class PermitValidationError(ValueError):
    pass


async def issue_permit(
    *,
    db_session: Any,
    task_id: str,
    step_id: str,
    contract_id: str,
    action_fingerprint: str,
    observation_hash: str,
    decision: PolicyDecision,
    effect: ExecutionEffect,
    execution_profile: ExecutionProfile,
    cua_execution_evidence: CUAExecutionEvidence | None = None,
    ttl_seconds: int = 60,
) -> ExecutionPermit:
    if decision.outcome != DecisionOutcome.ALLOW:
        raise PermitValidationError("Only allow decisions can issue execution permits")
    if ttl_seconds <= 0:
        raise PermitValidationError("Execution permit TTL must be positive")
    require_allowed_profile(effect=effect, profile=execution_profile)

    now = datetime.now(timezone.utc)
    require_cua_execution_evidence(
        profile=execution_profile,
        evidence=cua_execution_evidence,
        action_fingerprint=action_fingerprint,
        observation_hash=observation_hash,
        now=now,
    )
    model = ExecutionPermitModel(
        task_id=task_id,
        step_id=step_id,
        contract_id=contract_id,
        action_fingerprint=action_fingerprint,
        observation_hash=observation_hash,
        policy_decision_id=decision.decision_id,
        decision_payload={
            "policy_decision": decision.model_dump(mode="json"),
            "execution_effect": effect.value,
            "execution_profile": execution_profile.model_dump(mode="json"),
            "cua_execution_evidence": (
                cua_execution_evidence.model_dump(mode="json") if cua_execution_evidence else None
            ),
        },
        issued_at=now,
        expires_at=now + timedelta(seconds=ttl_seconds),
    )
    db_session.add(model)
    await db_session.flush()
    return _to_contract(model)


async def consume_permit(
    *,
    db_session: Any,
    permit_id: str,
    action_fingerprint: str,
    observation_hash: str,
    now: datetime | None = None,
) -> ExecutionPermit:
    model = await consume_permit_model(
        db_session=db_session,
        permit_id=permit_id,
        action_fingerprint=action_fingerprint,
        observation_hash=observation_hash,
        now=now,
    )
    return _to_contract(model)


async def consume_permit_model(
    *,
    db_session: Any,
    permit_id: str,
    action_fingerprint: str,
    observation_hash: str,
    now: datetime | None = None,
) -> ExecutionPermitModel:
    """Consume a permit and retain its persistent context for the execution boundary."""

    now = now or datetime.now(timezone.utc)
    model = (
        await db_session.scalars(
            select(ExecutionPermitModel)
            .where(ExecutionPermitModel.permit_id == permit_id)
            .with_for_update()
        )
    ).first()
    if model is None:
        raise PermitValidationError("Execution permit does not exist")
    if model.status != "issued":
        raise PermitValidationError("Execution permit is not available")
    if not (
        hmac.compare_digest(model.action_fingerprint, action_fingerprint)
        and hmac.compare_digest(model.observation_hash, observation_hash)
    ):
        raise PermitValidationError("Execution permit does not match action or observation")
    expiry = model.expires_at.replace(tzinfo=timezone.utc) if model.expires_at.tzinfo is None else model.expires_at
    if now > expiry:
        model.status = "expired"
        await db_session.flush()
        raise PermitValidationError("Execution permit has expired")

    model.status = "consumed"
    model.used_at = now
    await db_session.flush()
    return model


def _to_contract(model: ExecutionPermitModel) -> ExecutionPermit:
    return ExecutionPermit(
        permit_id=model.permit_id,
        task_id=model.task_id,
        step_id=model.step_id,
        action_fingerprint=model.action_fingerprint,
        observation_id=model.observation_hash,
        policy_decision_id=model.policy_decision_id,
        issued_at=model.issued_at,
        expires_at=model.expires_at,
        used_at=model.used_at,
    )
