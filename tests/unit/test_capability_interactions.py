"""Unified UI/chat request and constrained Planner interface tests."""

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from enterprise.agent.interactions import (
    CapabilityRequest,
    CapabilityRequestKind,
    EntryMode,
    PlannerOutcome,
    PlannerOutcomeKind,
    build_grant_projection,
    validate_capability_request,
    validate_plan_proposal,
)
from enterprise.agent.work_orders import BusinessPlan, BusinessPlanStep
from enterprise.auth.schemas import DepartmentRole, UserContext
from enterprise.governance.capabilities import (
    AuthorizationDimension,
    CapabilityAccessPolicy,
    CapabilityDataScope,
    CapabilityDefinition,
    CapabilityGrantSet,
    CapabilityRegistry,
    CapabilityResolutionContext,
    CapabilityResolver,
)

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


def _registry() -> CapabilityRegistry:
    policy = CapabilityAccessPolicy(
        role_dimensions={
            "operator": {
                AuthorizationDimension.DISCOVER,
                AuthorizationDimension.READ_RECORD,
                AuthorizationDimension.REQUEST_TRANSITION,
                AuthorizationDimension.EXECUTE_TRANSITION,
                AuthorizationDimension.REQUEST_APPROVAL,
            }
        }
    )
    definitions = [
        CapabilityDefinition(
            capability_id="records.update",
            version="1",
            domain="synthetic",
            display_name="Update record",
            intent_examples=["Update the synthetic record"],
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
            access_policy=policy,
        ),
        CapabilityDefinition(
            capability_id="records.hidden",
            version="1",
            domain="synthetic",
            display_name="Hidden record operation",
            access_policy_ref="policy://synthetic/hidden@1",
            risk_policy_ref="policy://synthetic/risk@1",
            work_order_template_ref="template://synthetic/hidden@1",
            result_probe_ref="probe://synthetic/hidden@1",
            access_policy=policy,
        ),
    ]
    return CapabilityRegistry(definitions)


def _grants(registry: CapabilityRegistry) -> CapabilityGrantSet:
    user = UserContext(
        user_id="user_1",
        org_id="org_1",
        department_roles=[DepartmentRole(department_id="dept_1", department_name="Ops", role="operator")],
        business_line_ids=["line_1"],
    )
    return CapabilityResolver(registry).resolve(
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


def _request(entry_mode: EntryMode = EntryMode.UI, **updates) -> CapabilityRequest:
    values = {
        "request_id": "request_1",
        "submitted_at": NOW,
        "entry_mode": entry_mode,
        "principal_ref": "user_1",
        "session_ref": "session_1",
        "tenant_id": "org_1",
        "requested_scope": SCOPE,
        "capability_ref": "records.update",
        "capability_version": "1",
        "request_kind": CapabilityRequestKind.TRANSITION,
        "typed_inputs": {"value": 42},
        "resource_refs": {"record_1"},
        "grant_ref": updates.pop("grant_ref"),
        "contract_versions": {"policy": "policy-v1", "task_contract": "1"},
    }
    values.update(updates)
    return CapabilityRequest(**values)


def test_projection_hides_denied_capabilities_and_policy_reasons():
    registry = _registry()
    grants = _grants(registry)
    projection = build_grant_projection(grants=grants, registry=registry, now=NOW)

    assert [entry.capability_id for entry in projection.entries] == ["records.update"]
    serialized = projection.model_dump(mode="json")
    assert "reasons" not in str(serialized)
    assert "access_policy_ref" not in str(serialized)


def test_ui_and_chat_use_the_same_normalized_request_contract():
    registry = _registry()
    grants = _grants(registry)
    projection = build_grant_projection(grants=grants, registry=registry, now=NOW)
    grant_id = projection.entries[0].grant_id

    ui = _request(grant_ref=grant_id)
    chat = _request(EntryMode.CHAT, grant_ref=grant_id)
    ui_payload = ui.model_dump(exclude={"entry_mode"})
    chat_payload = chat.model_dump(exclude={"entry_mode"})

    assert ui_payload == chat_payload
    validate_capability_request(ui, projection=projection, registry=registry, now=NOW, input_validator=INPUT_VALIDATOR)
    validate_capability_request(
        chat, projection=projection, registry=registry, now=NOW, input_validator=INPUT_VALIDATOR
    )


def test_request_rejects_unknown_fields_scope_expansion_and_invalid_inputs():
    registry = _registry()
    grants = _grants(registry)
    projection = build_grant_projection(grants=grants, registry=registry, now=NOW)
    grant_id = projection.entries[0].grant_id

    with pytest.raises(ValidationError, match="extra_forbidden"):
        CapabilityRequest(**_request(grant_ref=grant_id).model_dump(), invented_authority=True)

    expanded = _request(
        grant_ref=grant_id,
        requested_scope=SCOPE.model_copy(update={"resource_ids": {"record_1", "record_2"}}),
    )
    with pytest.raises(ValueError, match="scope exceeds"):
        validate_capability_request(
            expanded,
            projection=projection,
            registry=registry,
            now=NOW,
            input_validator=INPUT_VALIDATOR,
        )

    invalid = _request(grant_ref=grant_id, typed_inputs={"value": "not-an-integer"})
    with pytest.raises(ValueError, match="registered schema"):
        validate_capability_request(
            invalid,
            projection=projection,
            registry=registry,
            now=NOW,
            input_validator=INPUT_VALIDATOR,
        )


def test_plan_models_reject_unknown_fields_and_future_request_time_fails_closed():
    registry = _registry()
    grants = _grants(registry)
    projection = build_grant_projection(grants=grants, registry=registry, now=NOW)
    grant_id = projection.entries[0].grant_id

    with pytest.raises(ValidationError, match="extra_forbidden"):
        BusinessPlan(
            task_id="task_1",
            contract_id="contract_1",
            data_scope=SCOPE,
            invented_authority=True,
        )

    future_request = _request(grant_ref=grant_id, submitted_at=NOW + timedelta(seconds=1))
    with pytest.raises(ValueError, match="cannot be in the future"):
        validate_capability_request(
            future_request,
            projection=projection,
            registry=registry,
            now=NOW,
            input_validator=INPUT_VALIDATOR,
        )


def test_expired_projection_entry_cannot_authorize_a_request():
    registry = _registry()
    grants = _grants(registry)
    projection = build_grant_projection(grants=grants, registry=registry, now=NOW)
    request = _request(grant_ref=projection.entries[0].grant_id)

    with pytest.raises(ValueError, match="outside the active Grant projection"):
        validate_capability_request(
            request,
            projection=projection,
            registry=registry,
            now=NOW + timedelta(minutes=5),
            input_validator=INPUT_VALIDATOR,
        )


def test_plan_proposal_must_bind_request_version_scope_and_projected_grant():
    registry = _registry()
    grants = _grants(registry)
    projection = build_grant_projection(grants=grants, registry=registry, now=NOW)
    grant_id = projection.entries[0].grant_id
    request = _request(grant_ref=grant_id)
    plan = BusinessPlan(
        request_id=request.request_id,
        task_id="task_1",
        contract_id="contract_1",
        data_scope=SCOPE,
        steps=[
            BusinessPlanStep(
                capability_id=request.capability_ref,
                capability_version=request.capability_version,
                grant_id=grant_id,
                contract_id="contract_1",
                inputs=request.typed_inputs,
            )
        ],
    )

    validate_plan_proposal(
        plan,
        request=request,
        projection=projection,
        grants=grants,
        registry=registry,
        now=NOW,
        input_validator=INPUT_VALIDATOR,
    )

    wrong_version = plan.model_copy(deep=True)
    wrong_version.steps[0].capability_version = "2"
    with pytest.raises(ValueError, match="version"):
        validate_plan_proposal(
            wrong_version,
            request=request,
            projection=projection,
            grants=grants,
            registry=registry,
            now=NOW,
            input_validator=INPUT_VALIDATOR,
        )

    substituted_inputs = plan.model_copy(deep=True)
    substituted_inputs.steps[0].inputs = {"value": 7}
    with pytest.raises(ValueError, match="inputs must match"):
        validate_plan_proposal(
            substituted_inputs,
            request=request,
            projection=projection,
            grants=grants,
            registry=registry,
            now=NOW,
            input_validator=INPUT_VALIDATOR,
        )

    omitted_resource = plan.model_copy(
        update={"data_scope": SCOPE.model_copy(update={"resource_ids": set()})},
        deep=True,
    )
    with pytest.raises(ValueError, match="omits a requested resource"):
        validate_plan_proposal(
            omitted_resource,
            request=request,
            projection=projection,
            grants=grants,
            registry=registry,
            now=NOW,
            input_validator=INPUT_VALIDATOR,
        )


def test_plan_rejects_duplicate_steps_and_a_grant_set_forged_behind_a_projection():
    registry = _registry()
    grants = _grants(registry)
    projection = build_grant_projection(grants=grants, registry=registry, now=NOW)
    grant_id = projection.entries[0].grant_id
    request = _request(grant_ref=grant_id)
    step = BusinessPlanStep(
        step_id="step_1",
        capability_id=request.capability_ref,
        capability_version=request.capability_version,
        grant_id=grant_id,
        contract_id="contract_1",
        inputs=request.typed_inputs,
    )
    plan = BusinessPlan(
        request_id=request.request_id,
        task_id="task_1",
        contract_id="contract_1",
        data_scope=SCOPE,
        steps=[step],
    )

    duplicate = plan.model_copy(update={"steps": [step, step.model_copy()]}, deep=True)
    with pytest.raises(ValueError, match="step_id values must be unique"):
        validate_plan_proposal(
            duplicate,
            request=request,
            projection=projection,
            grants=grants,
            registry=registry,
            now=NOW,
            input_validator=INPUT_VALIDATOR,
        )

    forged = CapabilityGrantSet(
        grants=[
            grant.model_copy(update={"tenant_id": "org_2"}) if grant.grant_id == grant_id else grant
            for grant in grants.grants
        ]
    )
    with pytest.raises(ValueError, match="identity binding"):
        validate_plan_proposal(
            plan,
            request=request,
            projection=projection,
            grants=forged,
            registry=registry,
            now=NOW,
            input_validator=INPUT_VALIDATOR,
        )


def test_planner_outcome_cannot_attach_a_plan_to_a_refusal():
    with pytest.raises(ValidationError, match="Only PLAN_PROPOSAL"):
        PlannerOutcome(
            outcome=PlannerOutcomeKind.NO_MATCH,
            plan=BusinessPlan(task_id="task_1", contract_id="contract_1", data_scope=SCOPE),
        )
