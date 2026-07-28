"""Task 1 capability contracts and deterministic authorization tests."""

from datetime import datetime, timedelta, timezone

import pytest

from enterprise.auth.schemas import DepartmentRole, UserContext
from enterprise.governance.capabilities import (
    AccessDisposition,
    AuthorizationDimension,
    CapabilityAccessPolicy,
    CapabilityDataScope,
    CapabilityDefinition,
    CapabilityGrant,
    CapabilityGrantSet,
    CapabilityRegistry,
    CapabilityResolutionContext,
    CapabilityResolver,
    ScopeDimension,
)


def _user(role: str = "operator", organization_id: str = "org_1") -> UserContext:
    return UserContext(
        user_id="user_1",
        org_id=organization_id,
        department_roles=[DepartmentRole(department_id="dept_1", department_name="Operations", role=role)],
        business_line_ids=["line_1"],
    )


def _definition(**overrides) -> CapabilityDefinition:
    values = {
        "capability_id": "records.update",
        "version": "1",
        "domain": "synthetic",
        "display_name": "Update a record",
        "access_policy_ref": "synthetic.records.update.access.v1",
        "risk_policy_ref": "synthetic.records.update.risk.v1",
        "work_order_template_ref": "synthetic.records.update.work-order.v1",
        "result_probe_ref": "synthetic.records.update.result-probe.v1",
    }
    values.update(overrides)
    return CapabilityDefinition(**values)


def _context(**overrides) -> CapabilityResolutionContext:
    values = {
        "user": _user(),
        "tenant_id": "org_1",
        "data_scope": CapabilityDataScope(department_id="dept_1", business_line_id="line_1"),
        "installed_capability_ids": {"records.update"},
        "policy_snapshot_version": "policy-v1",
        "resolved_at": datetime(2026, 7, 21, tzinfo=timezone.utc),
    }
    values.update(overrides)
    return CapabilityResolutionContext(**values)


def test_resolver_grants_execution_only_for_installed_capability_with_sufficient_scope():
    grants = CapabilityResolver(CapabilityRegistry([_definition()])).resolve(_context())

    grant = grants.require_executable(
        capability_id="records.update",
        grant_id=grants.grants[0].grant_id,
        now=grants.grants[0].resolved_at,
    )
    assert grant.disposition is AccessDisposition.ALLOW_EXECUTE
    assert grant.principal_id == "user_1"
    assert grant.tenant_id == "org_1"
    assert grant.policy_snapshot_version == "policy-v1"


def test_resolver_returns_approval_request_without_exposing_an_executable_grant():
    definition = _definition(
        access_policy=CapabilityAccessPolicy(
            allow_request_approval=True,
            required_scope_dimensions={ScopeDimension.DEPARTMENT},
        )
    )
    context = _context(user=_user(role="viewer"))
    grants = CapabilityResolver(CapabilityRegistry([definition])).resolve(context)

    assert grants.grants[0].disposition is AccessDisposition.ALLOW_REQUEST_APPROVAL
    assert grants.executable_grants(now=grants.grants[0].resolved_at) == []
    with pytest.raises(ValueError, match="not an executable"):
        grants.require_executable(
            capability_id="records.update",
            grant_id=grants.grants[0].grant_id,
            now=grants.grants[0].resolved_at,
        )


def test_resolver_needs_clarification_before_permission_evaluation_when_required_scope_is_missing():
    definition = _definition(
        access_policy=CapabilityAccessPolicy(required_scope_dimensions={ScopeDimension.DEPARTMENT})
    )
    context = _context(data_scope=CapabilityDataScope())

    grant = CapabilityResolver(CapabilityRegistry([definition])).resolve(context).grants[0]

    assert grant.disposition is AccessDisposition.NEED_CLARIFICATION
    assert "department" in grant.reasons[0]


def test_resolver_denies_tenant_mismatch_and_uninstalled_capabilities():
    definition = _definition()
    resolver = CapabilityResolver(CapabilityRegistry([definition]))

    tenant_mismatch = resolver.resolve(_context(tenant_id="org_2")).grants[0]
    assert tenant_mismatch.disposition is AccessDisposition.DENY

    uninstalled = resolver.resolve(_context(installed_capability_ids=set())).grants[0]
    assert uninstalled.disposition is AccessDisposition.DENY


def test_capability_grant_round_trips_through_json_for_persistent_storage():
    grant = CapabilityResolver(CapabilityRegistry([_definition()])).resolve(_context()).grants[0]

    restored = CapabilityGrant.model_validate(grant.model_dump(mode="json"))

    assert restored == grant
    assert restored.data_scope.department_id == "dept_1"


def test_capability_grant_is_not_executable_at_or_after_its_exclusive_expiry():
    grants = CapabilityResolver(CapabilityRegistry([_definition()])).resolve(_context(grant_ttl_seconds=60))
    grant = grants.grants[0]

    assert grant.is_active_at(grant.resolved_at + timedelta(seconds=59))
    assert not grant.is_active_at(grant.expires_at)
    with pytest.raises(ValueError, match="not an executable"):
        grants.require_executable(
            capability_id=grant.capability_id,
            grant_id=grant.grant_id,
            now=grant.expires_at,
        )


def test_registry_rejects_duplicate_capability_ids():
    with pytest.raises(ValueError, match="Duplicate capability_id"):
        CapabilityRegistry([_definition(), _definition(version="2")])


def test_grant_set_rejects_duplicate_grant_ids():
    grant = CapabilityResolver(CapabilityRegistry([_definition()])).resolve(_context()).grants[0]

    with pytest.raises(ValueError, match="grant_id values must be unique"):
        CapabilityGrantSet(grants=[grant, grant.model_copy()])


def test_approver_dimension_never_inherits_transition_execution():
    definition = _definition(
        access_policy=CapabilityAccessPolicy(
            role_dimensions={
                "operator": {
                    AuthorizationDimension.REQUEST_TRANSITION,
                    AuthorizationDimension.EXECUTE_TRANSITION,
                },
                "approver": {AuthorizationDimension.ADJUDICATE_APPROVAL},
            }
        )
    )
    grants = CapabilityResolver(CapabilityRegistry([definition])).resolve(_context(user=_user(role="approver")))
    grant = grants.grants[0]

    assert grant.disposition is AccessDisposition.ALLOW
    assert grant.allowed_dimensions == {AuthorizationDimension.ADJUDICATE_APPROVAL}
    grants.require_dimension(
        capability_id=grant.capability_id,
        grant_id=grant.grant_id,
        dimension=AuthorizationDimension.ADJUDICATE_APPROVAL,
        now=grant.resolved_at,
    )
    with pytest.raises(ValueError, match="not an executable"):
        grants.require_executable(
            capability_id=grant.capability_id,
            grant_id=grant.grant_id,
            now=grant.resolved_at,
        )


@pytest.mark.parametrize("role", ["org_admin", "super_admin"])
def test_administrator_role_has_no_implicit_payment_execution(role):
    grants = CapabilityResolver(CapabilityRegistry([_definition()])).resolve(_context(user=_user(role=role)))

    assert AuthorizationDimension.EXECUTE_TRANSITION not in grants.grants[0].allowed_dimensions
    assert grants.executable_grants(now=grants.grants[0].resolved_at) == []


def test_grant_binds_business_and_workload_identities_and_revocation_epoch():
    grant = (
        CapabilityResolver(CapabilityRegistry([_definition()]))
        .resolve(_context(workload_principal_id="service_skyvern_1", revocation_epoch="epoch-7"))
        .grants[0]
    )

    assert grant.business_principal_id == "user_1"
    assert grant.workload_principal_id == "service_skyvern_1"
    assert grant.revocation_epoch == "epoch-7"
    assert grant.not_before == grant.resolved_at


def test_explicit_empty_role_dimension_map_denies_instead_of_using_legacy_compatibility():
    definition = _definition(access_policy=CapabilityAccessPolicy(role_dimensions={}))

    grant = CapabilityResolver(CapabilityRegistry([definition])).resolve(_context()).grants[0]

    assert grant.disposition is AccessDisposition.DENY
    assert grant.allowed_dimensions == set()


def test_business_line_membership_cannot_reuse_a_role_from_another_department():
    user = UserContext(
        user_id="user_1",
        org_id="org_1",
        department_roles=[DepartmentRole(department_id="dept_other", department_name="Other", role="operator")],
        business_line_ids=["line_1"],
    )

    grant = CapabilityResolver(CapabilityRegistry([_definition()])).resolve(_context(user=user)).grants[0]

    assert grant.disposition is AccessDisposition.DENY
    assert grant.allowed_dimensions == set()
