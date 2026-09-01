"""Audit-only governed Task admission boundary.

Callers provide trusted task, authorization, and Pack contracts. The
repository protocol deliberately owns one atomic transaction; the service
never persists a partial or runnable Skyvern Task.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal, Protocol
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field

from enterprise.agent.constrained_planner import PlannerObservation
from enterprise.agent.interactions import (
    CapabilityInputValidator,
    CapabilityRequest,
    GrantSetProjection,
    validate_capability_request,
    validate_plan_proposal,
)
from enterprise.agent.work_orders import BusinessPlan, ExecutionWorkOrder, validate_work_order
from enterprise.governance.capabilities import CapabilityGrant, CapabilityGrantSet, CapabilityRegistry
from enterprise.governance.contracts import GovernanceMode, TaskContract
from enterprise.governance.creation_snapshot import TrustedTaskCreationSnapshot
from enterprise.governance.pack_runtime import PackRuntimeBinding


class GovernedTaskDraft(BaseModel):
    """Minimal task row input owned by a future task-creation adapter."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str = Field(min_length=1)
    organization_id: str = Field(min_length=1)
    goal: str = Field(min_length=1)
    mode: GovernanceMode = GovernanceMode.AUDIT


class AdmissionAuditRecord(BaseModel):
    """Redacted admission evidence; it carries no raw business inputs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_type: str = "governed_task_admitted"
    admission_id: str
    request_id: str
    task_id: str
    organization_id: str
    contract_id: str
    plan_id: str
    grant_id: str
    capability_id: str
    capability_version: str
    policy_version: str
    revocation_epoch: str
    mode: GovernanceMode
    created_at: datetime


class TaskAdmissionBundle(BaseModel):
    """All records a repository must persist in one transaction."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "phase2-task-admission-bundle-v1"
    provider_mode: Literal["recorded", "live"] = "recorded"
    runtime_binding: PackRuntimeBinding | None = None
    planner_observation: PlannerObservation | None = None
    admission_id: str
    task: GovernedTaskDraft
    creation_snapshot: TrustedTaskCreationSnapshot
    contract: TaskContract
    request: CapabilityRequest
    grants: tuple[CapabilityGrant, ...]
    plan: BusinessPlan
    work_orders: tuple[ExecutionWorkOrder, ...]
    audit_record: AdmissionAuditRecord


def canonical_task_admission_payload(bundle: TaskAdmissionBundle) -> dict[str, Any]:
    """Return the stable JSON representation used by persistence and bindings."""

    payload = bundle.model_dump(mode="json")
    payload["contract"]["allowed_operations"] = sorted(bundle.contract.allowed_operations)
    _sort_scope_resources(payload["contract"]["data_scope"])
    payload["request"]["resource_refs"] = sorted(bundle.request.resource_refs)
    _sort_scope_resources(payload["request"]["requested_scope"])
    _sort_scope_resources(payload["plan"]["data_scope"])
    for grant_payload, grant in zip(payload["grants"], bundle.grants, strict=True):
        grant_payload["allowed_dimensions"] = sorted(dimension.value for dimension in grant.allowed_dimensions)
        _sort_scope_resources(grant_payload["data_scope"])
    for work_order_payload, work_order in zip(payload["work_orders"], bundle.work_orders, strict=True):
        work_order_payload["allowed_operations"] = sorted(work_order.allowed_operations)
        work_order_payload["prohibited_operations"] = sorted(work_order.prohibited_operations)
    return payload


def _sort_scope_resources(scope_payload: dict[str, Any]) -> None:
    resource_ids = scope_payload.get("resource_ids")
    if isinstance(resource_ids, list):
        scope_payload["resource_ids"] = sorted(resource_ids)


class TaskAdmissionReceipt(BaseModel):
    """Repository acknowledgement after the atomic transaction commits."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    admission_id: str
    task_id: str
    contract_id: str
    committed_at: datetime
    duplicate: bool = False


class TaskAdmissionRepository(Protocol):
    async def persist_atomic(self, bundle: TaskAdmissionBundle) -> TaskAdmissionReceipt:
        """Persist the bundle atomically and idempotently by tenant/request ID.

        A semantic duplicate must return the prior authoritative receipt.
        Reuse of the same tenant/request ID with different admission semantics
        must fail as a conflict.
        """


class TaskAdmissionRecoveryRequired(RuntimeError):
    """The caller must reconcile by admission ID and must not submit a new request."""


class GovernedTaskAdmissionService:
    """Validate and hand one complete audit-only bundle to its transaction owner."""

    def __init__(self, repository: TaskAdmissionRepository) -> None:
        self._repository = repository

    def prepare(
        self,
        *,
        task: GovernedTaskDraft,
        creation_snapshot: TrustedTaskCreationSnapshot,
        contract: TaskContract,
        request: CapabilityRequest,
        projection: GrantSetProjection,
        grants: CapabilityGrantSet,
        registry: CapabilityRegistry,
        plan: BusinessPlan,
        work_orders: tuple[ExecutionWorkOrder, ...],
        now: datetime,
        input_validator: CapabilityInputValidator | None = None,
    ) -> TaskAdmissionBundle:
        if task.mode is not GovernanceMode.AUDIT or contract.mode is not GovernanceMode.AUDIT:
            raise ValueError("Governed Task admission remains audit-only; enforce is not authorized")
        if task.task_id != creation_snapshot.task_id or task.organization_id != creation_snapshot.organization_id:
            raise ValueError("Task does not match its trusted creation snapshot")
        if contract.task_id != task.task_id or contract.organization_id != task.organization_id:
            raise ValueError("TaskContract does not match the Task")
        if request.tenant_id != task.organization_id:
            raise ValueError("CapabilityRequest tenant must match the Task")
        if contract.goal != task.goal:
            raise ValueError("TaskContract goal must match the Task draft")
        if plan.task_id != task.task_id or plan.contract_id != contract.contract_id:
            raise ValueError("BusinessPlan does not match the TaskContract")
        if creation_snapshot.request_id != request.request_id:
            raise ValueError("Creation snapshot request_id must match the CapabilityRequest")
        if creation_snapshot.initiator_id != request.principal_ref:
            raise ValueError("Creation snapshot initiator must match the CapabilityRequest principal")
        if creation_snapshot.service_principal_id != projection.workload_principal_id:
            raise ValueError("Creation snapshot workload identity must match the Grant projection")
        if contract.initiator_id != request.principal_ref:
            raise ValueError("TaskContract initiator must match the CapabilityRequest principal")
        if contract.service_principal_id != projection.workload_principal_id:
            raise ValueError("TaskContract workload identity must match the Grant projection")
        if contract.policy_version != creation_snapshot.policy_version:
            raise ValueError("TaskContract policy version must match the trusted snapshot")
        if contract.authorization_snapshot != creation_snapshot.model_dump(mode="json"):
            raise ValueError("TaskContract authorization snapshot must match the trusted snapshot")
        if contract.expires_at is not None and now >= contract.expires_at:
            raise ValueError("TaskContract is expired")
        if request.contract_versions.get("policy") != contract.policy_version:
            raise ValueError("CapabilityRequest policy version must match the TaskContract")
        if request.contract_versions.get("task_contract") != str(contract.version):
            raise ValueError("CapabilityRequest contract version must match the TaskContract")
        if contract.data_scope != request.requested_scope.model_dump(mode="json"):
            raise ValueError("TaskContract data scope must match the CapabilityRequest")

        entry = validate_capability_request(
            request,
            projection=projection,
            registry=registry,
            now=now,
            input_validator=input_validator,
        )
        grant = _require_grant(grants, request.grant_ref)
        if grant.capability_id != request.capability_ref or grant.capability_version != request.capability_version:
            raise ValueError("CapabilityRequest does not match its Grant")
        if grant.policy_snapshot_version != contract.policy_version:
            raise ValueError("Grant policy version must match the TaskContract")
        if creation_snapshot.authorization_snapshot.get("revocation_epoch") != grant.revocation_epoch:
            raise ValueError("Trusted snapshot revocation epoch must match the Grant")
        if request.capability_ref not in contract.allowed_operations:
            raise ValueError("TaskContract does not allow the requested capability")

        validate_plan_proposal(
            plan,
            request=request,
            projection=projection,
            grants=grants,
            registry=registry,
            now=now,
            input_validator=input_validator,
        )
        if any(step.capability_id not in contract.allowed_operations for step in plan.steps):
            raise ValueError("TaskContract must allow every BusinessPlan capability")
        work_orders_by_step = {work_order.business_plan_step_id: work_order for work_order in work_orders}
        work_order_ids = {work_order.work_order_id for work_order in work_orders}
        if len(work_order_ids) != len(work_orders):
            raise ValueError("ExecutionWorkOrder work_order_id values must be unique")
        if len(work_orders_by_step) != len(work_orders) or set(work_orders_by_step) != {
            step.step_id for step in plan.steps
        }:
            raise ValueError("Admission requires exactly one Work Order for every BusinessPlan step")
        for step in plan.steps:
            work_order = work_orders_by_step[step.step_id]
            validate_work_order(work_order, plan, step, grants, now=now)
            if work_order.result_probe_ref != registry.require(step.capability_id).result_probe_ref:
                raise ValueError("ExecutionWorkOrder result probe must match the capability registry")

        referenced_grant_ids = {step.grant_id for step in plan.steps}
        referenced_grants = tuple(
            sorted(
                (candidate for candidate in grants.grants if candidate.grant_id in referenced_grant_ids),
                key=lambda candidate: candidate.grant_id,
            )
        )
        if {candidate.grant_id for candidate in referenced_grants} != referenced_grant_ids:
            raise ValueError("BusinessPlan references a Grant missing from the admission bundle")
        if any(candidate.policy_snapshot_version != contract.policy_version for candidate in referenced_grants):
            raise ValueError("Every CapabilityGrant policy version must match the TaskContract")

        admission_key = json.dumps(
            [task.organization_id, request.request_id],
            ensure_ascii=True,
            separators=(",", ":"),
        )
        admission_id = f"admission_{uuid5(NAMESPACE_URL, admission_key).hex}"
        audit_record = AdmissionAuditRecord(
            admission_id=admission_id,
            request_id=request.request_id,
            task_id=task.task_id,
            organization_id=task.organization_id,
            contract_id=contract.contract_id,
            plan_id=plan.plan_id,
            grant_id=entry.grant_id,
            capability_id=entry.capability_id,
            capability_version=entry.capability_version,
            policy_version=contract.policy_version,
            revocation_epoch=grant.revocation_epoch,
            mode=task.mode,
            created_at=now,
        )
        return TaskAdmissionBundle(
            admission_id=admission_id,
            task=task,
            creation_snapshot=creation_snapshot,
            contract=contract,
            request=request,
            grants=referenced_grants,
            plan=plan,
            work_orders=work_orders,
            audit_record=audit_record,
        )

    async def admit(self, **kwargs: Any) -> TaskAdmissionReceipt:
        bundle = self.prepare(**kwargs)
        receipt = await self._repository.persist_atomic(bundle)
        if (
            receipt.admission_id != bundle.admission_id
            or receipt.task_id != bundle.task.task_id
            or receipt.contract_id != bundle.contract.contract_id
        ):
            raise TaskAdmissionRecoveryRequired(
                f"Task admission receipt mismatch; reconcile {bundle.admission_id} before retry"
            )
        return receipt


def _require_grant(grants: CapabilityGrantSet, grant_id: str) -> CapabilityGrant:
    for grant in grants.grants:
        if grant.grant_id == grant_id:
            return grant
    raise ValueError("CapabilityRequest references an unknown Grant")
