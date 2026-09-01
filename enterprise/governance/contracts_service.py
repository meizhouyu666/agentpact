"""Persistence helper for trusted creation-time TaskContract snapshots."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from .creation_snapshot import TrustedTaskCreationSnapshot
from .models import TaskContractModel


async def ensure_task_contract(
    *,
    db_session: Any,
    task: Any,
    mode: str,
    creation_snapshot: TrustedTaskCreationSnapshot,
    policy_version: str | None = None,
) -> TaskContractModel:
    """Return a contract created only from a trusted creation snapshot.

    This unconnected persistence helper is deliberately not callable from page
    observation. A future task-creation owner must supply its authenticated
    native, workflow, or template provenance before it can persist a contract.
    """

    if mode != "audit":
        raise ValueError("Task contract persistence is audit-only until enforce is separately approved")
    if task.task_id != creation_snapshot.task_id or task.organization_id != creation_snapshot.organization_id:
        raise ValueError("Task must match the trusted creation snapshot")

    existing = (
        await db_session.scalars(
            select(TaskContractModel).where(TaskContractModel.task_id == task.task_id)
        )
    ).first()
    if existing is not None:
        return existing

    contract = TaskContractModel(
        task_id=task.task_id,
        organization_id=task.organization_id,
        initiator_id=creation_snapshot.initiator_id,
        service_principal_id=creation_snapshot.service_principal_id,
        department_id=creation_snapshot.department_id,
        business_line_id=creation_snapshot.business_line_id,
        goal=task.navigation_goal or task.title or "",
        authorization_snapshot=creation_snapshot.model_dump(mode="json"),
        policy_version=policy_version or creation_snapshot.policy_version,
        version=creation_snapshot.contract_version,
        mode=mode,
    )
    db_session.add(contract)
    await db_session.flush()
    return contract
