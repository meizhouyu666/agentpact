"""Domain-neutral capability contracts and deterministic authorization resolution.

This module is intentionally outside the browser execution path.  It turns
trusted identity, tenant, scope, and installed-capability inputs into grants
that a future Planner may consume.  It neither discovers capabilities from an
LLM nor authorizes a browser action.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

from enterprise.auth.permission import PermissionLevel
from enterprise.auth.schemas import UserContext


class AccessDisposition(StrEnum):
    """The only outcomes a capability resolver may return."""

    ALLOW = "allow"
    ALLOW_EXECUTE = "allow_execute"
    ALLOW_REQUEST_APPROVAL = "allow_request_approval"
    NEED_CLARIFICATION = "need_clarification"
    DENY = "deny"


class AuthorizationDimension(StrEnum):
    """Independent authorities; no dimension inherits another."""

    DISCOVER = "discover"
    READ_RECORD = "read_record"
    REQUEST_TRANSITION = "request_transition"
    EXECUTE_TRANSITION = "execute_transition"
    REQUEST_APPROVAL = "request_approval"
    ADJUDICATE_APPROVAL = "adjudicate_approval"


class ScopeDimension(StrEnum):
    DEPARTMENT = "department"
    BUSINESS_LINE = "business_line"


class CapabilityAccessPolicy(BaseModel):
    """Explicit role-to-dimension semantics for one capability.

    The legacy permission fields remain serialization-compatible with the
    Phase 2 interface fixtures. They are converted through a semantic role map,
    never through ordinal ``permission >= minimum`` comparison.
    """

    minimum_permission: PermissionLevel = PermissionLevel.OPERATE
    approval_request_minimum_permission: PermissionLevel = PermissionLevel.READ
    allow_request_approval: bool = False
    required_scope_dimensions: set[ScopeDimension] = Field(default_factory=set)
    role_dimensions: dict[str, set[AuthorizationDimension]] | None = None

    def dimensions_for_role(self, role: str) -> set[AuthorizationDimension]:
        if self.role_dimensions is not None:
            return set(self.role_dimensions.get(role, set()))

        dimensions = _legacy_dimensions_for_role(role, self.minimum_permission)
        if self.allow_request_approval and role in _legacy_roles_for_permission(
            self.approval_request_minimum_permission
        ):
            dimensions.add(AuthorizationDimension.REQUEST_APPROVAL)
        return dimensions


class CapabilityDefinition(BaseModel):
    """A registered business capability, without browser implementation details."""

    capability_id: str
    version: str
    domain: str
    display_name: str
    intent_examples: list[str] = Field(default_factory=list)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    state_transition: dict[str, Any] = Field(default_factory=dict)
    access_policy_ref: str
    risk_policy_ref: str
    work_order_template_ref: str
    result_probe_ref: str
    access_policy: CapabilityAccessPolicy = Field(default_factory=CapabilityAccessPolicy)


class CapabilityDataScope(BaseModel):
    """Trusted target scope; a model must never infer or broaden this value."""

    department_id: str | None = None
    business_line_id: str | None = None
    resource_ids: set[str] = Field(default_factory=set)
    attributes: dict[str, Any] = Field(default_factory=dict)


class CapabilityResolutionContext(BaseModel):
    """All resolver inputs come from trusted server-side state."""

    user: UserContext
    tenant_id: str
    data_scope: CapabilityDataScope = Field(default_factory=CapabilityDataScope)
    installed_capability_ids: set[str] = Field(default_factory=set)
    policy_snapshot_version: str
    resolved_at: datetime
    grant_ttl_seconds: int = Field(default=300, gt=0)
    workload_principal_id: str | None = None
    revocation_epoch: str = "0"
    purpose: str = "capability_request"


class CapabilityGrant(BaseModel):
    """A time-bounded authorization result that a future Planner can reference."""

    grant_id: str = Field(default_factory=lambda: f"capgrant_{uuid4().hex}")
    capability_id: str
    capability_version: str
    principal_id: str
    business_principal_id: str | None = None
    workload_principal_id: str | None = None
    tenant_id: str
    data_scope: CapabilityDataScope
    disposition: AccessDisposition
    allowed_dimensions: set[AuthorizationDimension] = Field(default_factory=set)
    policy_snapshot_version: str
    revocation_epoch: str = "0"
    purpose: str = "capability_request"
    resolved_at: datetime
    not_before: datetime | None = None
    expires_at: datetime
    reasons: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def normalize_legacy_bindings(self) -> "CapabilityGrant":
        if self.business_principal_id is None:
            self.business_principal_id = self.principal_id
        if not self.allowed_dimensions:
            if self.disposition is AccessDisposition.ALLOW_EXECUTE:
                self.allowed_dimensions = {AuthorizationDimension.EXECUTE_TRANSITION}
            elif self.disposition is AccessDisposition.ALLOW_REQUEST_APPROVAL:
                self.allowed_dimensions = {AuthorizationDimension.REQUEST_APPROVAL}
        return self

    def is_active_at(self, now: datetime) -> bool:
        """A grant is active only before its exclusive expiry boundary."""

        return (self.not_before or self.resolved_at) <= now < self.expires_at

    def allows(self, dimension: AuthorizationDimension, *, now: datetime) -> bool:
        return self.is_active_at(now) and dimension in self.allowed_dimensions


class CapabilityGrantSet(BaseModel):
    """Resolver result; callers must project out denied and expired grants."""

    grants: list[CapabilityGrant] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_unique_grant_ids(self) -> "CapabilityGrantSet":
        grant_ids = [grant.grant_id for grant in self.grants]
        if len(grant_ids) != len(set(grant_ids)):
            raise ValueError("CapabilityGrantSet grant_id values must be unique")
        return self

    def active_grants(self, *, now: datetime) -> list[CapabilityGrant]:
        return [
            grant
            for grant in self.grants
            if grant.disposition is not AccessDisposition.DENY and grant.is_active_at(now)
        ]

    def executable_grants(self, *, now: datetime) -> list[CapabilityGrant]:
        return [
            grant
            for grant in self.active_grants(now=now)
            if grant.allows(AuthorizationDimension.EXECUTE_TRANSITION, now=now)
        ]

    def require_dimension(
        self,
        *,
        capability_id: str,
        grant_id: str,
        dimension: AuthorizationDimension,
        now: datetime,
    ) -> CapabilityGrant:
        for grant in self.active_grants(now=now):
            if grant.capability_id == capability_id and grant.grant_id == grant_id and grant.allows(dimension, now=now):
                return grant
        raise ValueError(f"CapabilityGrant does not allow {dimension.value}")

    def require_executable(self, *, capability_id: str, grant_id: str, now: datetime) -> CapabilityGrant:
        try:
            return self.require_dimension(
                capability_id=capability_id,
                grant_id=grant_id,
                dimension=AuthorizationDimension.EXECUTE_TRANSITION,
                now=now,
            )
        except ValueError as exc:
            raise ValueError("Planner capability reference is not an executable CapabilityGrant") from exc


class CapabilityRegistry:
    """Trusted registry facade; it is never populated from model output."""

    def __init__(self, definitions: Iterable[CapabilityDefinition]) -> None:
        self._definitions: dict[str, CapabilityDefinition] = {}
        for definition in definitions:
            if definition.capability_id in self._definitions:
                raise ValueError(f"Duplicate capability_id: {definition.capability_id}")
            self._definitions[definition.capability_id] = definition

    def definitions(self) -> list[CapabilityDefinition]:
        return list(self._definitions.values())

    def require(self, capability_id: str) -> CapabilityDefinition:
        try:
            return self._definitions[capability_id]
        except KeyError as exc:
            raise ValueError("Capability is not registered") from exc


class CapabilityResolver:
    """Deterministically derives grants from trusted context and installed definitions."""

    def __init__(self, registry: CapabilityRegistry) -> None:
        self._registry = registry

    def resolve(self, context: CapabilityResolutionContext) -> CapabilityGrantSet:
        return CapabilityGrantSet(
            grants=[self._resolve_definition(definition, context) for definition in self._registry.definitions()]
        )

    @staticmethod
    def _grant(
        definition: CapabilityDefinition,
        context: CapabilityResolutionContext,
        disposition: AccessDisposition,
        reason: str,
    ) -> CapabilityGrant:
        return CapabilityGrant(
            capability_id=definition.capability_id,
            capability_version=definition.version,
            principal_id=context.user.user_id,
            business_principal_id=context.user.user_id,
            workload_principal_id=context.workload_principal_id,
            tenant_id=context.tenant_id,
            data_scope=context.data_scope,
            disposition=disposition,
            allowed_dimensions=set(),
            policy_snapshot_version=context.policy_snapshot_version,
            revocation_epoch=context.revocation_epoch,
            purpose=context.purpose,
            resolved_at=context.resolved_at,
            not_before=context.resolved_at,
            expires_at=context.resolved_at + timedelta(seconds=context.grant_ttl_seconds),
            reasons=[reason],
        )

    def _resolve_definition(
        self,
        definition: CapabilityDefinition,
        context: CapabilityResolutionContext,
    ) -> CapabilityGrant:
        if context.tenant_id != context.user.org_id:
            return self._grant(
                definition, context, AccessDisposition.DENY, "Trusted user tenant does not match request tenant"
            )

        if definition.capability_id not in context.installed_capability_ids:
            return self._grant(
                definition, context, AccessDisposition.DENY, "Capability is not installed for this tenant"
            )

        missing_scope = {
            ScopeDimension.DEPARTMENT: not context.data_scope.department_id,
            ScopeDimension.BUSINESS_LINE: not context.data_scope.business_line_id,
        }
        required_missing = [
            dimension.value
            for dimension in definition.access_policy.required_scope_dimensions
            if missing_scope[dimension]
        ]
        if required_missing:
            return self._grant(
                definition,
                context,
                AccessDisposition.NEED_CLARIFICATION,
                f"Missing required data scope: {', '.join(sorted(required_missing))}",
            )

        policy = definition.access_policy
        dimensions: set[AuthorizationDimension] = set()
        for role in _roles_in_scope(context):
            dimensions.update(policy.dimensions_for_role(role))
        if context.user.has_cross_org_read:
            dimensions.add(AuthorizationDimension.READ_RECORD)
        if context.user.has_cross_org_approve:
            dimensions.add(AuthorizationDimension.ADJUDICATE_APPROVAL)

        if not dimensions:
            return self._grant(definition, context, AccessDisposition.DENY, "No role dimensions satisfy policy")

        if AuthorizationDimension.EXECUTE_TRANSITION in dimensions:
            disposition = AccessDisposition.ALLOW_EXECUTE
        elif AuthorizationDimension.REQUEST_APPROVAL in dimensions:
            disposition = AccessDisposition.ALLOW_REQUEST_APPROVAL
        else:
            disposition = AccessDisposition.ALLOW
        grant = self._grant(definition, context, disposition, "Explicit role dimensions satisfy policy")
        grant.allowed_dimensions = dimensions
        return grant


def _roles_in_scope(context: CapabilityResolutionContext) -> set[str]:
    roles: set[str] = set()
    for department_role in context.user.department_roles:
        same_department = bool(
            context.data_scope.department_id and department_role.department_id == context.data_scope.department_id
        )
        business_line_allowed = not context.data_scope.business_line_id or context.user.has_business_line(
            context.data_scope.business_line_id
        )
        if same_department and business_line_allowed:
            roles.add(department_role.role)
    return roles


def _legacy_dimensions_for_role(
    role: str,
    minimum_permission: PermissionLevel,
) -> set[AuthorizationDimension]:
    base = {
        "viewer": {AuthorizationDimension.DISCOVER, AuthorizationDimension.READ_RECORD},
        "operator": {
            AuthorizationDimension.DISCOVER,
            AuthorizationDimension.READ_RECORD,
            AuthorizationDimension.REQUEST_TRANSITION,
            AuthorizationDimension.EXECUTE_TRANSITION,
        },
        "approver": {
            AuthorizationDimension.DISCOVER,
            AuthorizationDimension.READ_RECORD,
            AuthorizationDimension.ADJUDICATE_APPROVAL,
        },
        "org_admin": {AuthorizationDimension.DISCOVER, AuthorizationDimension.READ_RECORD},
        "super_admin": {AuthorizationDimension.DISCOVER, AuthorizationDimension.READ_RECORD},
    }.get(role, set())
    permitted_for_minimum = {
        PermissionLevel.NONE: set(),
        PermissionLevel.READ: {
            AuthorizationDimension.DISCOVER,
            AuthorizationDimension.READ_RECORD,
        },
        PermissionLevel.OPERATE: {
            AuthorizationDimension.DISCOVER,
            AuthorizationDimension.READ_RECORD,
            AuthorizationDimension.REQUEST_TRANSITION,
            AuthorizationDimension.EXECUTE_TRANSITION,
        },
        PermissionLevel.APPROVE: {
            AuthorizationDimension.DISCOVER,
            AuthorizationDimension.READ_RECORD,
            AuthorizationDimension.ADJUDICATE_APPROVAL,
        },
    }[minimum_permission]
    return set(base & permitted_for_minimum)


def _legacy_roles_for_permission(permission: PermissionLevel) -> set[str]:
    return {
        PermissionLevel.NONE: set(),
        PermissionLevel.READ: {"viewer", "operator", "approver", "org_admin", "super_admin"},
        PermissionLevel.OPERATE: {"operator"},
        PermissionLevel.APPROVE: {"approver"},
    }[permission]
