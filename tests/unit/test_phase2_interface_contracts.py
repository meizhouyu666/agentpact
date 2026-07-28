"""Offline P2 contract tests: expiry, trusted creation, and version envelopes."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from enterprise.agent.interactions import (
    CapabilityRequest,
    CapabilityRequestKind,
    EntryMode,
    GrantProjectionEntry,
    GrantSetProjection,
)
from enterprise.agent.work_orders import (
    BusinessPlan,
    BusinessPlanStep,
    ExecutionWorkOrder,
    RecoveryLevel,
)
from enterprise.governance.capabilities import (
    AccessDisposition,
    CapabilityDataScope,
    CapabilityGrant,
)
from enterprise.governance.contracts import ObservationContext, TaskContract
from enterprise.governance.creation_snapshot import (
    TaskCreationPath,
    TrustedTaskCreationSnapshot,
)
from enterprise.governance.recovery import (
    ExecutionFailureClass,
    ExecutionFailureEvent,
    decide_recovery,
)
from enterprise.governance.versioning import (
    GovernanceArtifactKind,
    requires_invalidation,
    serialize_governance_artifact,
)

NOW = datetime(2026, 7, 22, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("path", "provenance"),
    [
        (TaskCreationPath.NATIVE, {"request_id": "request_1"}),
        (
            TaskCreationPath.WORKFLOW,
            {"workflow_id": "workflow_1", "workflow_run_id": "workflow_run_1"},
        ),
        (
            TaskCreationPath.TEMPLATE,
            {
                "template_id": "template_1",
                "template_version": "3",
                "template_run_id": "template_run_1",
            },
        ),
        (
            TaskCreationPath.SDK_API,
            {"request_id": "request_1", "caller_id": "sdk_client_1"},
        ),
        (
            TaskCreationPath.DIRECT_INTERNAL,
            {
                "request_id": "request_1",
                "caller_id": "internal_scheduler_1",
                "service_principal_id": "service_scheduler_1",
            },
        ),
    ],
)
def test_each_trusted_task_creation_path_requires_its_own_provenance(path, provenance):
    snapshot = TrustedTaskCreationSnapshot(
        task_id="task_1",
        organization_id="org_1",
        creation_path=path,
        initiator_id="user_1",
        authorization_snapshot={"role_snapshot_id": "rbac_1"},
        policy_version="policy-v1",
        contract_version=1,
        created_at=NOW,
        **provenance,
    )

    assert snapshot.creation_path is path
    assert snapshot.authorization_snapshot["role_snapshot_id"] == "rbac_1"


@pytest.mark.parametrize(
    ("path", "provenance", "message"),
    [
        (TaskCreationPath.NATIVE, {}, "request_id"),
        (TaskCreationPath.WORKFLOW, {"workflow_id": "workflow_1"}, "workflow_id"),
        (TaskCreationPath.TEMPLATE, {"template_id": "template_1"}, "template_id"),
        (TaskCreationPath.SDK_API, {"request_id": "request_1"}, "caller_id"),
        (
            TaskCreationPath.DIRECT_INTERNAL,
            {"request_id": "request_1", "caller_id": "internal_scheduler_1"},
            "service_principal_id",
        ),
    ],
)
def test_task_creation_snapshot_rejects_missing_trusted_provenance(path, provenance, message):
    with pytest.raises(ValueError, match=message):
        TrustedTaskCreationSnapshot(
            task_id="task_1",
            organization_id="org_1",
            creation_path=path,
            initiator_id="user_1",
            policy_version="policy-v1",
            contract_version=1,
            created_at=NOW,
            **provenance,
        )


def test_creation_snapshot_contract_never_uses_agent_observation_as_its_source():
    source = (Path(__file__).parents[2] / "enterprise" / "governance" / "contracts_service.py").read_text(
        encoding="utf-8"
    )

    assert "agent_first_observation" not in source
    assert "get_tenant_context" not in source
    assert "TaskExtensionModel" not in source


def _grant() -> CapabilityGrant:
    return CapabilityGrant(
        grant_id="grant_1",
        capability_id="records.update",
        capability_version="1",
        principal_id="user_1",
        tenant_id="org_1",
        data_scope=CapabilityDataScope(department_id="dept_1"),
        disposition=AccessDisposition.ALLOW_EXECUTE,
        policy_snapshot_version="policy-v1",
        resolved_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
    )


def test_versioned_serialization_covers_each_future_interface_and_detects_invalidation():
    contract = TaskContract(
        contract_id="contract_1",
        task_id="task_1",
        organization_id="org_1",
        goal="Synthetic interface test",
        version=1,
    )
    grant = _grant()
    request = CapabilityRequest(
        request_id="request_1",
        submitted_at=NOW,
        entry_mode=EntryMode.UI,
        principal_ref=grant.principal_id,
        session_ref="session_1",
        tenant_id=grant.tenant_id,
        requested_scope=grant.data_scope,
        capability_ref=grant.capability_id,
        capability_version=grant.capability_version,
        request_kind=CapabilityRequestKind.TRANSITION,
        grant_ref=grant.grant_id,
    )
    projection = GrantSetProjection(
        principal_id=grant.principal_id,
        tenant_id=grant.tenant_id,
        revocation_epoch=grant.revocation_epoch,
        generated_at=NOW,
        entries=(
            GrantProjectionEntry(
                grant_id=grant.grant_id,
                capability_id=grant.capability_id,
                capability_version=grant.capability_version,
                display_name="Update a record",
                data_scope=grant.data_scope,
                allowed_request_kinds={CapabilityRequestKind.TRANSITION},
                expires_at=grant.expires_at,
            ),
        ),
    )
    step = BusinessPlanStep(
        step_id="step_1",
        capability_id=grant.capability_id,
        grant_id=grant.grant_id,
        contract_id=contract.contract_id,
    )
    plan = BusinessPlan(
        plan_id="plan_1",
        task_id=contract.task_id,
        contract_id=contract.contract_id,
        data_scope=grant.data_scope,
        steps=[step],
    )
    work_order = ExecutionWorkOrder(
        work_order_id="work_order_1",
        business_plan_step_id=step.step_id,
        task_id=contract.task_id,
        contract_id=contract.contract_id,
        grant_id=grant.grant_id,
        navigation_goal="Synthetic interface test",
        max_recovery_level=RecoveryLevel.L1,
        result_probe_ref="synthetic.probe.v1",
    )
    observation = ObservationContext(
        observation_id="observation_1",
        task_id=contract.task_id,
        step_id=step.step_id,
        page_url="https://synthetic.example",
        snapshot_hash="observation-hmac",
        captured_at=NOW,
    )
    recovery = decide_recovery(
        ExecutionFailureEvent(
            task_id=contract.task_id,
            step_id=step.step_id,
            failure_class=ExecutionFailureClass.PAGE_LOCATOR,
        )
    )
    artifacts = {
        GovernanceArtifactKind.CONTRACT: contract,
        GovernanceArtifactKind.GRANT: grant,
        GovernanceArtifactKind.CAPABILITY_REQUEST: request,
        GovernanceArtifactKind.GRANT_PROJECTION: projection,
        GovernanceArtifactKind.BUSINESS_PLAN: plan,
        GovernanceArtifactKind.WORK_ORDER: work_order,
        GovernanceArtifactKind.OBSERVATION: observation,
        GovernanceArtifactKind.RECOVERY_DECISION: recovery,
    }

    envelopes = {kind: serialize_governance_artifact(kind=kind, value=value) for kind, value in artifacts.items()}

    assert set(envelopes) == set(GovernanceArtifactKind)
    assert all(envelope.schema_version.endswith("-v1") for envelope in envelopes.values())
    assert all(envelope.invalidation_keys for envelope in envelopes.values())
    assert not requires_invalidation(envelopes[GovernanceArtifactKind.GRANT], envelopes[GovernanceArtifactKind.GRANT])

    expired_grant = grant.model_copy(update={"expires_at": grant.expires_at + timedelta(minutes=5)})
    changed = serialize_governance_artifact(kind=GovernanceArtifactKind.GRANT, value=expired_grant)
    assert requires_invalidation(envelopes[GovernanceArtifactKind.GRANT], changed)

    revoked_grant = grant.model_copy(update={"revocation_epoch": "epoch-2"})
    revoked = serialize_governance_artifact(kind=GovernanceArtifactKind.GRANT, value=revoked_grant)
    assert requires_invalidation(envelopes[GovernanceArtifactKind.GRANT], revoked)
