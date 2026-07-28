"""Database-backed pause state for approval-required browser actions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select

from .audit import _redact_mapping, redacted_action_payload
from .contracts import ActionIntent, DecisionOutcome, PendingAction, PendingActionStatus, PolicyDecision
from .models import PendingActionModel


class PendingActionError(ValueError):
    pass


async def create_pending_action(
    *,
    db_session: Any,
    task_id: str,
    step_id: str,
    contract_id: str,
    organization_id: str,
    action: Any,
    intent: ActionIntent,
    observation_hash: str,
    decision: PolicyDecision,
    ttl_seconds: int = 3600,
) -> PendingAction:
    """Persist an approval pause without retaining replayable sensitive inputs."""

    if decision.outcome != DecisionOutcome.REQUIRE_APPROVAL:
        raise PendingActionError("Only approval-required decisions can create pending actions")
    if ttl_seconds <= 0:
        raise PendingActionError("Pending action TTL must be positive")
    if not observation_hash:
        raise PendingActionError("Pending action requires an observation hash")

    existing = (
        await db_session.scalars(
            select(PendingActionModel)
            .where(
                PendingActionModel.task_id == task_id,
                PendingActionModel.step_id == step_id,
                PendingActionModel.status.in_(
                    (PendingActionStatus.PENDING.value, PendingActionStatus.APPROVED.value)
                ),
            )
            .with_for_update()
        )
    ).first()
    if existing is not None:
        raise PendingActionError("Task step already has a pending approval action")

    now = datetime.now(timezone.utc)
    model = PendingActionModel(
        task_id=task_id,
        step_id=step_id,
        contract_id=contract_id,
        organization_id=organization_id,
        action_fingerprint=intent.action_fingerprint,
        observation_hash=observation_hash,
        action_payload=redacted_action_payload(action),
        intent_payload=_redact_mapping(intent.model_dump(mode="json")),
        decision_payload=_redact_mapping(decision.model_dump(mode="json")),
        status=PendingActionStatus.PENDING.value,
        row_version=1,
        expires_at=now + timedelta(seconds=ttl_seconds),
    )
    db_session.add(model)
    await db_session.flush()
    return _to_contract(model)


async def attach_approval(
    *,
    db_session: Any,
    pending_action_id: str,
    approval_id: str,
    expected_row_version: int,
) -> PendingAction:
    """Link exactly one persisted approval request using optimistic concurrency."""

    if not approval_id:
        raise PendingActionError("Approval id is required")
    model = await _get_for_update(db_session=db_session, pending_action_id=pending_action_id)
    await _require_pending(db_session=db_session, model=model, expected_row_version=expected_row_version)
    if model.approval_id is not None:
        raise PendingActionError("Pending action already has an approval request")

    model.approval_id = approval_id
    model.row_version += 1
    await db_session.flush()
    return _to_contract(model)


async def record_approval_decision(
    *,
    db_session: Any,
    pending_action_id: str,
    approval_id: str,
    approved: bool,
    expected_row_version: int,
    now: datetime | None = None,
) -> PendingAction:
    """Persist an approval decision; approval never becomes a direct permit."""

    model = await _get_for_update(db_session=db_session, pending_action_id=pending_action_id)
    await _require_pending(db_session=db_session, model=model, expected_row_version=expected_row_version, now=now)
    if model.approval_id != approval_id:
        raise PendingActionError("Approval decision does not match the pending action")

    model.status = PendingActionStatus.APPROVED.value if approved else PendingActionStatus.REJECTED.value
    model.row_version += 1
    await db_session.flush()
    return _to_contract(model)


async def invalidate_approved_action_for_reobservation(
    *,
    db_session: Any,
    pending_action_id: str,
    expected_row_version: int,
) -> PendingAction:
    """Consume approval state before re-observation so the stored action cannot replay."""

    model = await _get_for_update(db_session=db_session, pending_action_id=pending_action_id)
    if model.status != PendingActionStatus.APPROVED.value:
        raise PendingActionError("Only approved actions can be invalidated for re-observation")
    if model.row_version != expected_row_version:
        raise PendingActionError("Pending action version conflict")

    model.status = PendingActionStatus.INVALIDATED.value
    model.row_version += 1
    await db_session.flush()
    return _to_contract(model)


async def _get_for_update(*, db_session: Any, pending_action_id: str) -> PendingActionModel:
    model = (
        await db_session.scalars(
            select(PendingActionModel)
            .where(PendingActionModel.pending_action_id == pending_action_id)
            .with_for_update()
        )
    ).first()
    if model is None:
        raise PendingActionError("Pending action does not exist")
    return model


async def _require_pending(
    *,
    db_session: Any,
    model: PendingActionModel,
    expected_row_version: int,
    now: datetime | None = None,
) -> None:
    if model.status != PendingActionStatus.PENDING.value:
        raise PendingActionError("Pending action is not available for this transition")
    if model.row_version != expected_row_version:
        raise PendingActionError("Pending action version conflict")
    timestamp = now or datetime.now(timezone.utc)
    expiry = model.expires_at.replace(tzinfo=timezone.utc) if model.expires_at.tzinfo is None else model.expires_at
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    if timestamp > expiry:
        model.status = PendingActionStatus.EXPIRED.value
        model.row_version += 1
        await db_session.flush()
        raise PendingActionError("Pending action has expired")


def _to_contract(model: PendingActionModel) -> PendingAction:
    return PendingAction(
        pending_action_id=model.pending_action_id,
        task_id=model.task_id,
        step_id=model.step_id,
        contract_id=model.contract_id,
        organization_id=model.organization_id,
        action_fingerprint=model.action_fingerprint,
        observation_hash=model.observation_hash,
        status=PendingActionStatus(model.status),
        approval_id=model.approval_id,
        row_version=model.row_version,
        expires_at=model.expires_at,
    )
