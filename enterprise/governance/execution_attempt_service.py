"""Crash-aware state machine for permit-authorized browser side effects.

The public ActionHandler integration will use this service immediately around a
browser call.  It deliberately never retries an existing attempt: a process
crash after a remote commit can be indistinguishable from a failed response.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from .contracts import ExecutionAttempt, ExecutionAttemptStatus, ExecutionEffect
from .execution_profiles import (
    CUAExecutionEvidence,
    ExecutionProfile,
    ExecutionProfileRejected,
    require_cua_execution_evidence,
)
from .models import ExecutionAttemptModel, ExecutionPermitModel
from .permit_service import consume_permit_model


class ExecutionAttemptError(ValueError):
    pass


class ExecutionAttemptRecoveryRequired(ExecutionAttemptError):
    """An idempotency key has prior state and must be probed by a recovery flow."""


async def authorize_execution_attempt(
    *,
    db_session: Any,
    permit_id: str,
    action_fingerprint: str,
    observation_hash: str,
    idempotency_key: str,
    effect: ExecutionEffect,
    execution_profile: ExecutionProfile,
    cua_execution_evidence: CUAExecutionEvidence | None = None,
    now: datetime | None = None,
) -> ExecutionAttempt:
    """Consume a permit and durably register a not-yet-executing attempt.

    Callers must commit this state before transitioning the attempt to
    ``executing`` and invoking Playwright.  A pre-existing key is never
    replayed automatically, regardless of its recorded status.
    """

    if not idempotency_key:
        raise ExecutionAttemptError("Execution attempt requires an idempotency key")

    permit = (
        await db_session.scalars(
            select(ExecutionPermitModel)
            .where(ExecutionPermitModel.permit_id == permit_id)
            .with_for_update()
        )
    ).first()
    if permit is None:
        raise ExecutionAttemptError("Execution permit does not exist")
    _verify_permit_execution_context(
        permit=permit,
        effect=effect,
        execution_profile=execution_profile,
        cua_execution_evidence=cua_execution_evidence,
        action_fingerprint=action_fingerprint,
        observation_hash=observation_hash,
        now=now,
    )

    existing = (
        await db_session.scalars(
            select(ExecutionAttemptModel)
            .where(
                ExecutionAttemptModel.task_id == permit.task_id,
                ExecutionAttemptModel.idempotency_key == idempotency_key,
            )
            .with_for_update()
        )
    ).first()
    if existing is not None:
        raise ExecutionAttemptRecoveryRequired(
            f"Execution attempt already exists with status {existing.status}; recovery probe is required"
        )

    permit = await consume_permit_model(
        db_session=db_session,
        permit_id=permit_id,
        action_fingerprint=action_fingerprint,
        observation_hash=observation_hash,
        now=now,
    )
    model = ExecutionAttemptModel(
        task_id=permit.task_id,
        step_id=permit.step_id,
        contract_id=permit.contract_id,
        action_fingerprint=permit.action_fingerprint,
        observation_hash=permit.observation_hash,
        status=ExecutionAttemptStatus.AUTHORIZED.value,
        idempotency_key=idempotency_key,
    )
    db_session.add(model)
    await db_session.flush()
    return _to_contract(model)


def _verify_permit_execution_context(
    *,
    permit: ExecutionPermitModel,
    effect: ExecutionEffect,
    execution_profile: ExecutionProfile,
    cua_execution_evidence: CUAExecutionEvidence | None,
    action_fingerprint: str,
    observation_hash: str,
    now: datetime | None,
) -> None:
    """Reject a downgraded effect or substituted fallback profile."""

    payload = permit.decision_payload
    if not isinstance(payload, dict):
        raise ExecutionAttemptError("Execution permit is missing its governed execution context")
    try:
        permitted_effect = ExecutionEffect(payload["execution_effect"])
        permitted_profile = ExecutionProfile.model_validate(payload["execution_profile"])
        persisted_cua_evidence = (
            CUAExecutionEvidence.model_validate(payload["cua_execution_evidence"])
            if payload.get("cua_execution_evidence") is not None
            else None
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ExecutionAttemptError("Execution permit is missing its governed execution context") from exc
    if permitted_effect is not effect or permitted_profile != execution_profile:
        raise ExecutionAttemptError("Execution permit does not match the authorized effect and profile")
    if persisted_cua_evidence != cua_execution_evidence:
        raise ExecutionAttemptError("Execution permit does not match the authorized CUA engine evidence")
    try:
        require_cua_execution_evidence(
            profile=execution_profile,
            evidence=cua_execution_evidence,
            action_fingerprint=action_fingerprint,
            observation_hash=observation_hash,
            now=now,
        )
    except ExecutionProfileRejected as exc:
        raise ExecutionAttemptError(str(exc)) from exc


async def mark_execution_attempt_executing(
    *,
    db_session: Any,
    attempt_id: str,
    now: datetime | None = None,
) -> ExecutionAttempt:
    """Persist the point immediately before a browser side effect begins."""

    return await _transition_attempt(
        db_session=db_session,
        attempt_id=attempt_id,
        from_status=ExecutionAttemptStatus.AUTHORIZED,
        to_status=ExecutionAttemptStatus.EXECUTING,
        now=now,
    )


async def confirm_execution_attempt(
    *,
    db_session: Any,
    attempt_id: str,
    result_probe: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> ExecutionAttempt:
    """Mark a completed browser side effect as independently confirmed."""

    return await _transition_attempt(
        db_session=db_session,
        attempt_id=attempt_id,
        from_status=ExecutionAttemptStatus.EXECUTING,
        to_status=ExecutionAttemptStatus.CONFIRMED,
        result_probe=result_probe,
        now=now,
    )


async def fail_execution_attempt(
    *,
    db_session: Any,
    attempt_id: str,
    error_message: str,
    result_probe: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> ExecutionAttempt:
    """Mark a definitely-not-executed action as failed."""

    return await _transition_attempt(
        db_session=db_session,
        attempt_id=attempt_id,
        from_status=ExecutionAttemptStatus.EXECUTING,
        to_status=ExecutionAttemptStatus.FAILED,
        error_message=error_message,
        result_probe=result_probe,
        now=now,
    )


async def mark_execution_attempt_unknown(
    *,
    db_session: Any,
    attempt_id: str,
    error_message: str,
    result_probe: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> ExecutionAttempt:
    """Record an ambiguous outcome; only a business result probe may resolve it."""

    return await _transition_attempt(
        db_session=db_session,
        attempt_id=attempt_id,
        from_status=ExecutionAttemptStatus.EXECUTING,
        to_status=ExecutionAttemptStatus.UNKNOWN,
        error_message=error_message,
        result_probe=result_probe,
        now=now,
    )


async def resolve_unknown_execution_attempt(
    *,
    db_session: Any,
    attempt_id: str,
    confirmed: bool,
    result_probe: dict[str, Any],
    now: datetime | None = None,
) -> ExecutionAttempt:
    """Resolve an ambiguous attempt only after a business-level result probe."""

    if not result_probe:
        raise ExecutionAttemptError("Resolving an unknown attempt requires result-probe evidence")
    return await _transition_attempt(
        db_session=db_session,
        attempt_id=attempt_id,
        from_status=ExecutionAttemptStatus.UNKNOWN,
        to_status=ExecutionAttemptStatus.CONFIRMED if confirmed else ExecutionAttemptStatus.FAILED,
        result_probe=result_probe,
        now=now,
    )


async def _transition_attempt(
    *,
    db_session: Any,
    attempt_id: str,
    from_status: ExecutionAttemptStatus,
    to_status: ExecutionAttemptStatus,
    now: datetime | None = None,
    result_probe: dict[str, Any] | None = None,
    error_message: str | None = None,
) -> ExecutionAttempt:
    model = (
        await db_session.scalars(
            select(ExecutionAttemptModel)
            .where(ExecutionAttemptModel.attempt_id == attempt_id)
            .with_for_update()
        )
    ).first()
    if model is None:
        raise ExecutionAttemptError("Execution attempt does not exist")
    if model.status != from_status.value:
        raise ExecutionAttemptError(
            f"Execution attempt cannot transition from {model.status} to {to_status.value}"
        )

    timestamp = now or datetime.now(timezone.utc)
    model.status = to_status.value
    if to_status == ExecutionAttemptStatus.EXECUTING:
        model.started_at = timestamp
    else:
        model.completed_at = timestamp
        model.result_probe = result_probe
        model.error_message = error_message
    await db_session.flush()
    return _to_contract(model)


def _to_contract(model: ExecutionAttemptModel) -> ExecutionAttempt:
    return ExecutionAttempt(
        attempt_id=model.attempt_id,
        task_id=model.task_id,
        step_id=model.step_id,
        contract_id=model.contract_id,
        action_fingerprint=model.action_fingerprint,
        observation_hash=model.observation_hash,
        idempotency_key=model.idempotency_key,
        status=ExecutionAttemptStatus(model.status),
        started_at=model.started_at,
        completed_at=model.completed_at,
        result_probe=model.result_probe,
        error_message=model.error_message,
    )
