"""Interface-only atomic governed Task admission tests."""

from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict

from enterprise.agent.interactions import (
    CapabilityRequest,
    CapabilityRequestKind,
    EntryMode,
    build_grant_projection,
)
from enterprise.agent.work_orders import BusinessPlan, BusinessPlanStep, ExecutionWorkOrder, RecoveryLevel
from enterprise.auth.schemas import DepartmentRole, UserContext
from enterprise.governance.admission import (
    GovernedTaskAdmissionService,
    GovernedTaskDraft,
    TaskAdmissionBundle,
    TaskAdmissionReceipt,
)
from enterprise.governance.capabilities import (
    AuthorizationDimension,
    CapabilityAccessPolicy,
    CapabilityDataScope,
    CapabilityDefinition,
    CapabilityRegistry,
    CapabilityResolutionContext,
    CapabilityResolver,
)
from enterprise.governance.contracts import GovernanceMode, TaskContract
from enterprise.governance.creation_snapshot import TaskCreationPath, TrustedTaskCreationSnapshot

NOW = datetime(2026, 7, 25, tzinfo=timezone.utc)
SCOPE = CapabilityDataScope(
    department_id="dept_1",
    business_line_id="line_1",
    resource_ids={"record_1"},
)


class _UpdateInputs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: int


class _InputValidator:
    def validate(self, definition: CapabilityDefinition, value: dict[str, object]) -> None:
        if definition.capability_id != "records.update":
            raise ValueError("unsupported capability schema")
        _UpdateInputs.model_validate(value)


INPUT_VALIDATOR = _InputValidator()


class RecordingAdmissionRepository:
    def __init__(self) -> None:
        self.bundles: list[TaskAdmissionBundle] = []

    async def persist_atomic(self, bundle: TaskAdmissionBundle) -> TaskAdmissionReceipt:
        self.bundles.append(bundle)
        return TaskAdmissionReceipt(
            admission_id=bundle.admission_id,
            task_id=bundle.task.task_id,
            contract_id=bundle.contract.contract_id,
            committed_at=NOW,
        )


def _inputs():
    definition = CapabilityDefinition(
        capability_id="records.update",
        version="1",
        domain="synthetic",
        display_name="Update record",
        input_schema={
            "type": "object",
            "properties": {"value": {"type": "integer"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        access_policy_ref="policy://synthetic/update@1",
        risk_policy_ref="policy://synthetic/risk@1",
        work_order_template_ref="template://synthetic/update@1",
        result_probe_ref="probe://synthetic/update@1",
        access_policy=CapabilityAccessPolicy(
            role_dimensions={
                "operator": {
                    AuthorizationDimension.DISCOVER,
                    AuthorizationDimension.REQUEST_TRANSITION,
                    AuthorizationDimension.EXECUTE_TRANSITION,
                }
            }
        ),
    )
    registry = CapabilityRegistry([definition])
    user = UserContext(
        user_id="user_1",
        org_id="org_1",
        department_roles=[DepartmentRole(department_id="dept_1", department_name="Ops", role="operator")],
        business_line_ids=["line_1"],
    )
    grants = CapabilityResolver(registry).resolve(
        CapabilityResolutionContext(
            user=user,
            tenant_id="org_1",
            data_scope=SCOPE,
            installed_capability_ids={"records.update"},
            policy_snapshot_version="policy-v1",
            resolved_at=NOW,
            workload_principal_id="service_1",
            revocation_epoch="epoch-1",
        )
    )
    projection = build_grant_projection(grants=grants, registry=registry, now=NOW)
    grant = grants.grants[0]
    request = CapabilityRequest(
        request_id="request_1",
        submitted_at=NOW,
        entry_mode=EntryMode.UI,
        principal_ref="user_1",
        session_ref="session_1",
        tenant_id="org_1",
        requested_scope=SCOPE,
        capability_ref="records.update",
        capability_version="1",
        request_kind=CapabilityRequestKind.TRANSITION,
        typed_inputs={"value": 42},
        resource_refs={"record_1"},
        grant_ref=grant.grant_id,
        contract_versions={"policy": "policy-v1", "task_contract": "1"},
    )
    task = GovernedTaskDraft(
        task_id="task_1",
        organization_id="org_1",
        goal="Update one synthetic record",
    )
    snapshot = TrustedTaskCreationSnapshot(
        task_id=task.task_id,
        organization_id=task.organization_id,
        creation_path=TaskCreationPath.NATIVE,
        initiator_id="user_1",
        service_principal_id="service_1",
        department_id="dept_1",
        business_line_id="line_1",
        authorization_snapshot={"revocation_epoch": "epoch-1"},
        policy_version="policy-v1",
        contract_version=1,
        created_at=NOW,
        request_id=request.request_id,
    )
    contract = TaskContract(
        contract_id="contract_1",
        task_id=task.task_id,
        organization_id=task.organization_id,
        initiator_id="user_1",
        service_principal_id="service_1",
        department_id="dept_1",
        business_line_id="line_1",
        goal=task.goal,
        allowed_operations={"records.update"},
        data_scope=SCOPE.model_dump(mode="json"),
        authorization_snapshot=snapshot.model_dump(mode="json"),
        policy_version="policy-v1",
        version=1,
        mode=GovernanceMode.AUDIT,
    )
    step = BusinessPlanStep(
        step_id="plan_step_1",
        capability_id="records.update",
        capability_version="1",
        grant_id=grant.grant_id,
        contract_id=contract.contract_id,
        inputs=request.typed_inputs,
    )
    plan = BusinessPlan(
        plan_id="plan_1",
        request_id=request.request_id,
        task_id=task.task_id,
        contract_id=contract.contract_id,
        data_scope=SCOPE,
        steps=[step],
    )
    work_order = ExecutionWorkOrder(
        work_order_id="work_order_1",
        business_plan_step_id=step.step_id,
        task_id=task.task_id,
        contract_id=contract.contract_id,
        grant_id=grant.grant_id,
        navigation_goal="Prepare the synthetic record update",
        allowed_operations={"read", "input"},
        prohibited_operations={"submit"},
        max_recovery_level=RecoveryLevel.L1,
        result_probe_ref="probe://synthetic/update@1",
    )
    return {
        "task": task,
        "creation_snapshot": snapshot,
        "contract": contract,
        "request": request,
        "projection": projection,
        "grants": grants,
        "registry": registry,
        "plan": plan,
        "work_orders": (work_order,),
        "now": NOW,
        "input_validator": INPUT_VALIDATOR,
    }


@pytest.mark.asyncio
async def test_admission_persists_one_complete_bundle_through_one_atomic_repository_call():
    repository = RecordingAdmissionRepository()
    service = GovernedTaskAdmissionService(repository)
    inputs = _inputs()

    receipt = await service.admit(**inputs)
    repeated_bundle = service.prepare(**inputs)

    assert len(repository.bundles) == 1
    bundle = repository.bundles[0]
    assert receipt.admission_id == bundle.admission_id
    assert repeated_bundle.admission_id == bundle.admission_id
    assert [grant.grant_id for grant in bundle.grants] == [bundle.request.grant_ref]
    assert bundle.task.task_id == bundle.creation_snapshot.task_id == bundle.contract.task_id
    assert bundle.audit_record.request_id == bundle.request.request_id
    assert bundle.audit_record.mode is GovernanceMode.AUDIT
    assert "typed_inputs" not in bundle.audit_record.model_dump(mode="json")


def test_admission_rejects_enforce_without_calling_repository():
    repository = RecordingAdmissionRepository()
    service = GovernedTaskAdmissionService(repository)
    inputs = _inputs()
    inputs["task"] = inputs["task"].model_copy(update={"mode": GovernanceMode.ENFORCE})
    inputs["contract"] = inputs["contract"].model_copy(update={"mode": GovernanceMode.ENFORCE})

    with pytest.raises(ValueError, match="audit-only"):
        service.prepare(**inputs)

    assert repository.bundles == []


def test_admission_rejects_mismatched_identity_revocation_and_work_order_sets():
    service = GovernedTaskAdmissionService(RecordingAdmissionRepository())
    inputs = _inputs()
    inputs["creation_snapshot"] = inputs["creation_snapshot"].model_copy(
        update={"authorization_snapshot": {"revocation_epoch": "stale-epoch"}}
    )
    inputs["contract"] = inputs["contract"].model_copy(
        update={"authorization_snapshot": inputs["creation_snapshot"].model_dump(mode="json")}
    )
    with pytest.raises(ValueError, match="revocation epoch"):
        service.prepare(**inputs)

    inputs = _inputs()
    inputs["work_orders"] = ()
    with pytest.raises(ValueError, match="exactly one Work Order"):
        service.prepare(**inputs)


def test_admission_rejects_contract_or_result_probe_substitution():
    service = GovernedTaskAdmissionService(RecordingAdmissionRepository())
    inputs = _inputs()
    second_definition = CapabilityDefinition(
        capability_id="records.archive",
        version="1",
        domain="synthetic",
        display_name="Archive record",
        access_policy_ref="policy://synthetic/archive@1",
        risk_policy_ref="policy://synthetic/risk@1",
        work_order_template_ref="template://synthetic/archive@1",
        result_probe_ref="probe://synthetic/archive@1",
    )
    inputs["registry"] = CapabilityRegistry([inputs["registry"].require("records.update"), second_definition])
    second_grant = (
        inputs["grants"]
        .grants[0]
        .model_copy(
            update={
                "grant_id": "grant_archive",
                "capability_id": second_definition.capability_id,
                "capability_version": second_definition.version,
            }
        )
    )
    inputs["grants"] = inputs["grants"].model_copy(update={"grants": [*inputs["grants"].grants, second_grant]})
    inputs["projection"] = build_grant_projection(
        grants=inputs["grants"],
        registry=inputs["registry"],
        now=NOW,
    )
    second_step = BusinessPlanStep(
        step_id="plan_step_2",
        capability_id=second_definition.capability_id,
        capability_version=second_definition.version,
        grant_id=second_grant.grant_id,
        contract_id=inputs["contract"].contract_id,
    )
    inputs["plan"] = inputs["plan"].model_copy(update={"steps": [*inputs["plan"].steps, second_step]})
    inputs["work_orders"] = (
        *inputs["work_orders"],
        ExecutionWorkOrder(
            work_order_id="work_order_2",
            business_plan_step_id=second_step.step_id,
            task_id=inputs["task"].task_id,
            contract_id=inputs["contract"].contract_id,
            grant_id=second_grant.grant_id,
            navigation_goal="Prepare the synthetic archive",
            allowed_operations={"read"},
            max_recovery_level=RecoveryLevel.L1,
            result_probe_ref=second_definition.result_probe_ref,
        ),
    )
    with pytest.raises(ValueError, match="every BusinessPlan capability"):
        service.prepare(**inputs)

    inputs["contract"] = inputs["contract"].model_copy(
        update={"allowed_operations": {"records.update", "records.archive"}}
    )
    inputs["grants"] = inputs["grants"].model_copy(
        update={
            "grants": [
                grant.model_copy(update={"policy_snapshot_version": "stale-policy"})
                if grant.grant_id == second_grant.grant_id
                else grant
                for grant in inputs["grants"].grants
            ]
        }
    )
    with pytest.raises(ValueError, match="Every CapabilityGrant policy version"):
        service.prepare(**inputs)

    inputs = _inputs()
    inputs["work_orders"] = (inputs["work_orders"][0].model_copy(update={"result_probe_ref": "probe://substituted@1"}),)
    with pytest.raises(ValueError, match="result probe"):
        service.prepare(**inputs)


def test_admission_id_is_stable_for_the_same_tenant_and_request_even_if_task_content_conflicts():
    service = GovernedTaskAdmissionService(RecordingAdmissionRepository())
    original = _inputs()
    first = service.prepare(**original)

    conflicting = _inputs()
    conflicting["task"] = conflicting["task"].model_copy(update={"task_id": "task_conflict"})
    conflicting["creation_snapshot"] = conflicting["creation_snapshot"].model_copy(update={"task_id": "task_conflict"})
    conflicting["contract"] = conflicting["contract"].model_copy(
        update={
            "task_id": "task_conflict",
            "authorization_snapshot": conflicting["creation_snapshot"].model_dump(mode="json"),
        }
    )
    conflicting["plan"] = conflicting["plan"].model_copy(update={"task_id": "task_conflict"})
    conflicting["work_orders"] = (conflicting["work_orders"][0].model_copy(update={"task_id": "task_conflict"}),)

    second = service.prepare(**conflicting)

    assert second.admission_id == first.admission_id
    assert second.task.task_id != first.task.task_id


def test_admission_module_has_no_live_skyvern_or_browser_import():
    source = (Path(__file__).parents[2] / "enterprise" / "governance" / "admission.py").read_text(encoding="utf-8")

    assert "import skyvern" not in source
    assert "Playwright" not in source
    assert "ActionHandler" not in source
