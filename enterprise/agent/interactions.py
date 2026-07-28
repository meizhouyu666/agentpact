"""Unified UI/chat request and constrained planning interfaces.

These contracts are intentionally disconnected from routes, the free-form
Planner prototype, Skyvern, and browser execution.
"""

from __future__ import annotations

import json
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from enterprise.agent.work_orders import BusinessPlan, validate_business_plan
from enterprise.governance.capabilities import (
    AuthorizationDimension,
    CapabilityDataScope,
    CapabilityDefinition,
    CapabilityGrant,
    CapabilityGrantSet,
    CapabilityRegistry,
)


class CapabilityInputValidator(Protocol):
    """Trusted validator for a registered capability's typed input schema."""

    def validate(self, definition: CapabilityDefinition, value: dict[str, Any]) -> None:
        """Raise ValueError when value does not satisfy the registered schema."""


class EntryMode(StrEnum):
    UI = "ui"
    CHAT = "chat"


class CapabilityRequestKind(StrEnum):
    DISCOVER = "discover"
    READ = "read"
    TRANSITION = "transition"
    APPROVAL_REQUEST = "approval_request"
    APPROVAL_ADJUDICATION = "approval_adjudication"


_REQUEST_DIMENSIONS = {
    CapabilityRequestKind.DISCOVER: AuthorizationDimension.DISCOVER,
    CapabilityRequestKind.READ: AuthorizationDimension.READ_RECORD,
    CapabilityRequestKind.TRANSITION: AuthorizationDimension.REQUEST_TRANSITION,
    CapabilityRequestKind.APPROVAL_REQUEST: AuthorizationDimension.REQUEST_APPROVAL,
    CapabilityRequestKind.APPROVAL_ADJUDICATION: AuthorizationDimension.ADJUDICATE_APPROVAL,
}


class CapabilityRequest(BaseModel):
    """One strict request shape shared by UI and natural-language entry."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str = Field(min_length=1)
    submitted_at: datetime
    entry_mode: EntryMode
    principal_ref: str = Field(min_length=1)
    session_ref: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    requested_scope: CapabilityDataScope
    capability_ref: str = Field(min_length=1)
    capability_version: str = Field(min_length=1)
    request_kind: CapabilityRequestKind
    typed_inputs: dict[str, Any] = Field(default_factory=dict)
    resource_refs: set[str] = Field(default_factory=set)
    approval_round_ref: str | None = None
    user_intent_summary: str = ""
    grant_ref: str = Field(min_length=1)
    contract_versions: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_approval_binding(self) -> "CapabilityRequest":
        is_approval_interaction = self.request_kind in {
            CapabilityRequestKind.APPROVAL_REQUEST,
            CapabilityRequestKind.APPROVAL_ADJUDICATION,
        }
        if is_approval_interaction and not self.approval_round_ref:
            raise ValueError("Approval interactions require approval_round_ref")
        if not is_approval_interaction and self.approval_round_ref is not None:
            raise ValueError("approval_round_ref is valid only for approval interactions")
        return self


class GrantProjectionEntry(BaseModel):
    """Disclosure-safe Planner/UI view of one active Grant."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    grant_id: str
    capability_id: str
    capability_version: str
    display_name: str
    intent_examples: tuple[str, ...] = ()
    input_schema: dict[str, Any] = Field(default_factory=dict)
    data_scope: CapabilityDataScope
    allowed_request_kinds: set[CapabilityRequestKind]
    expires_at: datetime


class GrantSetProjection(BaseModel):
    """Only active, non-denied capabilities visible to one principal."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    principal_id: str
    workload_principal_id: str | None = None
    tenant_id: str
    revocation_epoch: str
    generated_at: datetime
    entries: tuple[GrantProjectionEntry, ...] = ()

    @model_validator(mode="after")
    def validate_unique_grant_ids(self) -> "GrantSetProjection":
        grant_ids = [entry.grant_id for entry in self.entries]
        if len(grant_ids) != len(set(grant_ids)):
            raise ValueError("GrantSetProjection grant_id values must be unique")
        return self

    def require_entry(
        self,
        *,
        capability_id: str,
        grant_id: str,
        now: datetime,
    ) -> GrantProjectionEntry:
        for entry in self.entries:
            if entry.capability_id == capability_id and entry.grant_id == grant_id and now < entry.expires_at:
                return entry
        raise ValueError("Capability request is outside the active Grant projection")


class PlannerOutcomeKind(StrEnum):
    CLARIFICATION_REQUIRED = "clarification_required"
    PLAN_PROPOSAL = "plan_proposal"
    NO_MATCH = "no_match"
    UNSAFE_OR_UNSUPPORTED = "unsafe_or_unsupported"


class PlannerOutcome(BaseModel):
    """Structured Planner result; explanatory text never grants authority."""

    model_config = ConfigDict(extra="forbid")

    outcome: PlannerOutcomeKind
    plan: BusinessPlan | None = None
    missing_fields: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_payload(self) -> "PlannerOutcome":
        if self.outcome is PlannerOutcomeKind.PLAN_PROPOSAL and self.plan is None:
            raise ValueError("PLAN_PROPOSAL requires a BusinessPlan")
        if self.outcome is not PlannerOutcomeKind.PLAN_PROPOSAL and self.plan is not None:
            raise ValueError("Only PLAN_PROPOSAL may contain a BusinessPlan")
        if self.outcome is PlannerOutcomeKind.CLARIFICATION_REQUIRED and not self.missing_fields:
            raise ValueError("CLARIFICATION_REQUIRED requires missing_fields")
        return self


def build_grant_projection(
    *,
    grants: CapabilityGrantSet,
    registry: CapabilityRegistry,
    now: datetime,
) -> GrantSetProjection:
    """Remove denied/expired Grants and all policy/reason internals."""

    active = grants.active_grants(now=now)
    if not active:
        raise ValueError("No active Capability Grants are available for projection")
    principal_ids = {grant.business_principal_id or grant.principal_id for grant in active}
    workload_ids = {grant.workload_principal_id for grant in active}
    tenant_ids = {grant.tenant_id for grant in active}
    epochs = {grant.revocation_epoch for grant in active}
    if any(len(values) != 1 for values in (principal_ids, workload_ids, tenant_ids, epochs)):
        raise ValueError("Grant projection cannot combine principals, tenants, or revocation epochs")

    entries: list[GrantProjectionEntry] = []
    for grant in active:
        definition = registry.require(grant.capability_id)
        if definition.version != grant.capability_version:
            raise ValueError("Grant capability version does not match the registry")
        request_kinds = {
            request_kind
            for request_kind, dimension in _REQUEST_DIMENSIONS.items()
            if dimension in grant.allowed_dimensions
        }
        if not request_kinds:
            continue
        entries.append(
            GrantProjectionEntry(
                grant_id=grant.grant_id,
                capability_id=grant.capability_id,
                capability_version=grant.capability_version,
                display_name=definition.display_name,
                intent_examples=tuple(definition.intent_examples),
                input_schema=definition.input_schema,
                data_scope=grant.data_scope,
                allowed_request_kinds=request_kinds,
                expires_at=grant.expires_at,
            )
        )
    return GrantSetProjection(
        principal_id=next(iter(principal_ids)),
        workload_principal_id=next(iter(workload_ids)),
        tenant_id=next(iter(tenant_ids)),
        revocation_epoch=next(iter(epochs)),
        generated_at=now,
        entries=tuple(sorted(entries, key=lambda entry: (entry.capability_id, entry.grant_id))),
    )


def validate_capability_request(
    request: CapabilityRequest,
    *,
    projection: GrantSetProjection,
    registry: CapabilityRegistry,
    now: datetime,
    input_validator: CapabilityInputValidator | None = None,
) -> GrantProjectionEntry:
    """Fail closed when UI/chat output exceeds its supplied projection."""

    if request.submitted_at > now:
        raise ValueError("Capability request submission time cannot be in the future")
    if projection.generated_at > now:
        raise ValueError("Grant projection generation time cannot be in the future")
    if request.principal_ref != projection.principal_id or request.tenant_id != projection.tenant_id:
        raise ValueError("Capability request principal or tenant does not match the Grant projection")
    entry = projection.require_entry(
        capability_id=request.capability_ref,
        grant_id=request.grant_ref,
        now=now,
    )
    if request.capability_version != entry.capability_version:
        raise ValueError("Capability request version does not match the Grant projection")
    if request.request_kind not in entry.allowed_request_kinds:
        raise ValueError("Capability request kind is not allowed by the Grant")
    if _scope_expands(entry.data_scope, request.requested_scope):
        raise ValueError("Capability request scope exceeds the Grant")
    if entry.data_scope.resource_ids and not request.resource_refs.issubset(entry.data_scope.resource_ids):
        raise ValueError("Capability request references a resource outside the Grant")
    if request.requested_scope.resource_ids and not request.resource_refs.issubset(
        request.requested_scope.resource_ids
    ):
        raise ValueError("Capability request resource refs exceed its requested scope")

    definition = registry.require(request.capability_ref)
    if definition.version != request.capability_version:
        raise ValueError("Capability request version does not match the registry")
    _validate_inputs(definition, request.typed_inputs, input_validator)
    return entry


def validate_plan_proposal(
    plan: BusinessPlan,
    *,
    request: CapabilityRequest,
    projection: GrantSetProjection,
    grants: CapabilityGrantSet,
    registry: CapabilityRegistry,
    now: datetime,
    input_validator: CapabilityInputValidator | None = None,
) -> None:
    """Validate a model proposal without granting it execution authority."""

    if request.request_kind is not CapabilityRequestKind.TRANSITION:
        raise ValueError("Only a transition request may produce a BusinessPlan")
    validate_capability_request(
        request,
        projection=projection,
        registry=registry,
        now=now,
        input_validator=input_validator,
    )
    if not plan.steps:
        raise ValueError("BusinessPlan must contain at least one step")
    if plan.request_id != request.request_id:
        raise ValueError("BusinessPlan request_id must match the CapabilityRequest")
    step_ids = [step.step_id for step in plan.steps]
    if len(step_ids) != len(set(step_ids)):
        raise ValueError("BusinessPlan step_id values must be unique")
    first_step = plan.steps[0]
    if first_step.capability_id != request.capability_ref or first_step.grant_id != request.grant_ref:
        raise ValueError("BusinessPlan first step must represent the requested capability")
    if _canonical_json(first_step.inputs) != _canonical_json(request.typed_inputs):
        raise ValueError("BusinessPlan first step inputs must match the CapabilityRequest")
    if _scope_expands(request.requested_scope, plan.data_scope):
        raise ValueError("BusinessPlan scope exceeds the CapabilityRequest")
    if request.resource_refs and not request.resource_refs.issubset(plan.data_scope.resource_ids):
        raise ValueError("BusinessPlan scope omits a requested resource")

    projection_entries = {(entry.capability_id, entry.grant_id): entry for entry in projection.entries}
    for step in plan.steps:
        entry = projection_entries.get((step.capability_id, step.grant_id))
        if entry is None:
            raise ValueError("BusinessPlan step is outside the Grant projection")
        if step.capability_version != entry.capability_version:
            raise ValueError("BusinessPlan step capability version does not match its Grant")
        grant = _require_grant(grants, step.grant_id)
        _validate_grant_projection_binding(grant=grant, entry=entry, projection=projection)
        grants.require_dimension(
            capability_id=step.capability_id,
            grant_id=step.grant_id,
            dimension=AuthorizationDimension.REQUEST_TRANSITION,
            now=now,
        )
        if _scope_expands(grant.data_scope, plan.data_scope):
            raise ValueError("BusinessPlan scope exceeds a referenced Grant")
        definition = registry.require(step.capability_id)
        if definition.version != step.capability_version:
            raise ValueError("BusinessPlan step capability version does not match the registry")
        _validate_inputs(definition, step.inputs, input_validator)
    validate_business_plan(plan, grants, now=now)


def _require_grant(grants: CapabilityGrantSet, grant_id: str) -> CapabilityGrant:
    for grant in grants.grants:
        if grant.grant_id == grant_id:
            return grant
    raise ValueError("BusinessPlan references an unknown Grant")


def _validate_inputs(
    definition: CapabilityDefinition,
    value: dict[str, Any],
    input_validator: CapabilityInputValidator | None,
) -> None:
    if not definition.input_schema:
        if value:
            raise ValueError("Capability has no registered input schema")
        return
    if input_validator is None:
        raise ValueError("Capability input schema requires a trusted validator")
    try:
        input_validator.validate(definition, value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Capability inputs do not match the registered schema: {exc}") from exc


def _scope_expands(allowed: CapabilityDataScope, requested: CapabilityDataScope) -> bool:
    if allowed.department_id != requested.department_id:
        return True
    if allowed.business_line_id != requested.business_line_id:
        return True
    if allowed.resource_ids and not requested.resource_ids.issubset(allowed.resource_ids):
        return True
    return any(allowed.attributes.get(key) != value for key, value in requested.attributes.items())


def _validate_grant_projection_binding(
    *,
    grant: CapabilityGrant,
    entry: GrantProjectionEntry,
    projection: GrantSetProjection,
) -> None:
    if (
        (grant.business_principal_id or grant.principal_id) != projection.principal_id
        or grant.workload_principal_id != projection.workload_principal_id
        or grant.tenant_id != projection.tenant_id
        or grant.revocation_epoch != projection.revocation_epoch
    ):
        raise ValueError("CapabilityGrant identity binding does not match the Grant projection")
    if (
        grant.capability_id != entry.capability_id
        or grant.capability_version != entry.capability_version
        or grant.data_scope != entry.data_scope
        or grant.expires_at != entry.expires_at
    ):
        raise ValueError("CapabilityGrant content does not match the Grant projection")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
