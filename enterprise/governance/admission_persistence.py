"""Atomic SQLAlchemy persistence for audit-only governed Task admissions."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from enterprise.governance.admission import (
    TaskAdmissionBundle,
    TaskAdmissionReceipt,
    TaskAdmissionRepository,
)
from enterprise.governance.contracts import GovernanceMode
from enterprise.governance.models import GovernanceOutboxModel, GovernedTaskAdmissionModel


class TaskAdmissionConflict(ValueError):
    """A tenant/request idempotency key was reused for different semantics."""


class SqlAlchemyTaskAdmissionRepository(TaskAdmissionRepository):
    """Persist one non-runnable admission aggregate and redacted outbox row."""

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[Any]],
        *,
        allowed_tenant_ids: frozenset[str],
        allowed_capability_ids: frozenset[str],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not allowed_tenant_ids or not allowed_capability_ids:
            raise ValueError("Task admission persistence requires explicit tenant and capability allowlists")
        self._session_factory = session_factory
        self._allowed_tenant_ids = allowed_tenant_ids
        self._allowed_capability_ids = allowed_capability_ids
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def persist_atomic(self, bundle: TaskAdmissionBundle) -> TaskAdmissionReceipt:
        if bundle.task.mode is not GovernanceMode.AUDIT or bundle.contract.mode is not GovernanceMode.AUDIT:
            raise ValueError("Task admission persistence remains audit-only")
        tenant_ids = {
            bundle.task.organization_id,
            bundle.creation_snapshot.organization_id,
            bundle.contract.organization_id,
            bundle.request.tenant_id,
            bundle.audit_record.organization_id,
        }
        if len(tenant_ids) != 1:
            raise ValueError("Task admission tenant bindings are inconsistent")
        if not tenant_ids <= self._allowed_tenant_ids:
            raise ValueError("Task admission tenant is outside the repository allowlist")
        capability_ids = {
            bundle.request.capability_ref,
            bundle.audit_record.capability_id,
            *(grant.capability_id for grant in bundle.grants),
            *(step.capability_id for step in bundle.plan.steps),
            *bundle.contract.allowed_operations,
        }
        if not capability_ids <= self._allowed_capability_ids:
            raise ValueError("Task admission capability is outside the repository allowlist")

        payload = bundle.model_dump(mode="json")
        admission_fingerprint = _admission_fingerprint(bundle)
        bundle_fingerprint = _fingerprint(payload)
        committed_at = self._clock()
        model = GovernedTaskAdmissionModel(
            admission_id=bundle.admission_id,
            organization_id=bundle.task.organization_id,
            request_id=bundle.request.request_id,
            task_id=bundle.task.task_id,
            contract_id=bundle.contract.contract_id,
            bundle_schema_version=bundle.schema_version,
            admission_fingerprint=admission_fingerprint,
            bundle_fingerprint=bundle_fingerprint,
            bundle_payload=payload,
            mode=bundle.task.mode.value,
            committed_at=committed_at,
        )
        outbox = GovernanceOutboxModel(
            outbox_id=f"outbox_{bundle.admission_id}",
            admission_id=bundle.admission_id,
            organization_id=bundle.task.organization_id,
            event_type=bundle.audit_record.event_type,
            payload=bundle.audit_record.model_dump(mode="json"),
            status="pending",
            attempt_count=0,
            created_at=committed_at,
        )

        try:
            async with self._session_factory() as session:
                async with session.begin():
                    existing = await _find_existing(
                        session,
                        organization_id=bundle.task.organization_id,
                        request_id=bundle.request.request_id,
                    )
                    if existing is not None:
                        return _resolve_existing(existing, admission_fingerprint=admission_fingerprint)
                    session.add(model)
                    await session.flush()
                    session.add(outbox)
                    await session.flush()
            return _to_receipt(model, duplicate=False)
        except IntegrityError as exc:
            existing = await self._load_existing(
                organization_id=bundle.task.organization_id,
                request_id=bundle.request.request_id,
            )
            if existing is None:
                raise
            try:
                return _resolve_existing(existing, admission_fingerprint=admission_fingerprint)
            except TaskAdmissionConflict as conflict:
                raise conflict from exc

    async def _load_existing(
        self,
        *,
        organization_id: str,
        request_id: str,
    ) -> GovernedTaskAdmissionModel | None:
        async with self._session_factory() as session:
            return await _find_existing(
                session,
                organization_id=organization_id,
                request_id=request_id,
            )


async def _find_existing(
    session: Any,
    *,
    organization_id: str,
    request_id: str,
) -> GovernedTaskAdmissionModel | None:
    return (
        await session.scalars(
            select(GovernedTaskAdmissionModel)
            .where(
                GovernedTaskAdmissionModel.organization_id == organization_id,
                GovernedTaskAdmissionModel.request_id == request_id,
            )
            .with_for_update()
        )
    ).first()


def _resolve_existing(
    existing: GovernedTaskAdmissionModel,
    *,
    admission_fingerprint: str,
) -> TaskAdmissionReceipt:
    if existing.admission_fingerprint != admission_fingerprint:
        raise TaskAdmissionConflict("Capability request ID was reused with different admission semantics")
    return _to_receipt(existing, duplicate=True)


def _to_receipt(model: GovernedTaskAdmissionModel, *, duplicate: bool) -> TaskAdmissionReceipt:
    return TaskAdmissionReceipt(
        admission_id=model.admission_id,
        task_id=model.task_id,
        contract_id=model.contract_id,
        committed_at=model.committed_at,
        duplicate=duplicate,
    )


def _admission_fingerprint(bundle: TaskAdmissionBundle) -> str:
    payload = bundle.model_dump(mode="json")
    payload["request"].pop("submitted_at", None)
    payload["creation_snapshot"].pop("created_at", None)
    payload["contract"].pop("expires_at", None)
    payload["contract"]["expires_after_creation_us"] = _duration_microseconds(
        bundle.contract.expires_at,
        bundle.creation_snapshot.created_at,
    )
    if isinstance(payload["contract"].get("authorization_snapshot"), dict):
        payload["contract"]["authorization_snapshot"].pop("created_at", None)
    payload["audit_record"].pop("created_at", None)
    payload["contract"]["allowed_operations"] = sorted(bundle.contract.allowed_operations)
    _sort_scope_resources(payload["contract"]["data_scope"])
    payload["request"]["resource_refs"] = sorted(bundle.request.resource_refs)
    _sort_scope_resources(payload["request"]["requested_scope"])
    _sort_scope_resources(payload["plan"]["data_scope"])
    for grant_payload, grant in zip(payload["grants"], bundle.grants, strict=True):
        grant_payload.pop("resolved_at", None)
        grant_payload.pop("not_before", None)
        grant_payload.pop("expires_at", None)
        grant_payload["not_before_offset_us"] = _duration_microseconds(
            grant.not_before or grant.resolved_at,
            grant.resolved_at,
        )
        grant_payload["valid_for_us"] = _duration_microseconds(grant.expires_at, grant.resolved_at)
        grant_payload["allowed_dimensions"] = sorted(dimension.value for dimension in grant.allowed_dimensions)
        _sort_scope_resources(grant_payload["data_scope"])
    for work_order_payload, work_order in zip(payload["work_orders"], bundle.work_orders, strict=True):
        work_order_payload["allowed_operations"] = sorted(work_order.allowed_operations)
        work_order_payload["prohibited_operations"] = sorted(work_order.prohibited_operations)
    return _fingerprint(payload)


def _sort_scope_resources(scope_payload: dict[str, Any]) -> None:
    resource_ids = scope_payload.get("resource_ids")
    if isinstance(resource_ids, list):
        scope_payload["resource_ids"] = sorted(resource_ids)


def _duration_microseconds(end: datetime | None, start: datetime) -> int | None:
    if end is None:
        return None
    duration = end - start
    return ((duration.days * 86_400) + duration.seconds) * 1_000_000 + duration.microseconds


def _fingerprint(value: Any) -> str:
    canonical = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
