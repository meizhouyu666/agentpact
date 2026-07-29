"""Focused M7 native publication, resolver, recovery, and trace tests."""

# ruff: noqa: E402, F401, I001

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from tests.e2e import m4_synthetic_support as _m4_runtime_shims

from enterprise.agent.constrained_planner import DeterministicPlanner
from enterprise.agent.interactions import (
    CapabilityRequest,
    CapabilityRequestKind,
    EntryMode,
)
from enterprise.auth.schemas import DepartmentRole, UserContext
from enterprise.domains.synthetic_payment.constants import (
    BUSINESS_LINE_ID,
    CAPABILITY_ID,
    PACK_VERSION,
    PAYMENTS_DEPARTMENT_ID,
    POLICY_VERSION,
)
from enterprise.domains.synthetic_payment.m6_runtime import (
    SyntheticM6TrustedContext,
    build_synthetic_installation,
    compile_synthetic_request,
)
from enterprise.domains.synthetic_payment.m7_runtime import (
    M7_APPLICATION_MARKER,
    NativeBoundStateDenied,
    NativeProbeOutcome,
    NativePublicationConflict,
    NativeSkyvernWorkOrderAdapter,
    SqlAlchemyNativePublicationRepository,
    SyntheticNativeActionContextResolver,
    build_native_probe_evidence,
    build_redacted_m7_trace,
    derive_native_step_id,
    derive_native_task_id,
)
from enterprise.domains.synthetic_payment.sdk_manifest import build_pack_sdk_manifest
from enterprise.governance.admission import (
    AdmissionAuditRecord,
    GovernedTaskDraft,
    TaskAdmissionBundle,
)
from enterprise.governance.capabilities import CapabilityDataScope
from enterprise.governance.contracts import (
    ExecutionAttemptStatus,
    ExecutionAuthorization,
    ExecutionEffect,
)
from enterprise.governance.creation_snapshot import (
    TaskCreationPath,
    TrustedTaskCreationSnapshot,
)
from enterprise.governance.execution_profiles import ExecutionMechanism, ExecutionProfile
from enterprise.governance.models import (
    ExecutionAttemptModel,
    ExecutionPermitModel,
    GovernedTaskAdmissionModel,
    TaskContractModel,
)
from enterprise.governance.result_probes import ResultProbeEvidence, ResultProbeStatus
from enterprise.governance.pack_conformance import evaluate_static_pack_conformance
from skyvern.forge.native_action import NativeActionDisposition
from skyvern.forge.sdk.db.models import StepModel, TaskModel
from skyvern.forge.sdk.models import Step, StepStatus
from skyvern.forge.sdk.schemas.tasks import Task, TaskStatus
from skyvern.webeye.actions.actions import ClickAction, InputTextAction

NOW = datetime(2026, 7, 29, 16, 0, tzinfo=timezone.utc)
TENANT = "synthetic-m7-tenant"
REQUEST_ID = "request-m7-native-001"
ADMISSION_ID = "admission-m7-native-001"
INPUTS = {
    "payment_id": "pay-m7-001",
    "beneficiary_id": "vendor-m7-001",
    "amount": "5000.00",
    "currency": "CNY",
    "reference": "Synthetic M7 invoice",
    "object_version": 1,
}


class _ScalarResult:
    def __init__(self, values: list[Any]) -> None:
        self._values = values

    def first(self) -> Any | None:
        return self._values[0] if self._values else None

    def all(self) -> list[Any]:
        return list(self._values)


class _Store:
    def __init__(self) -> None:
        self.admissions: dict[str, GovernedTaskAdmissionModel] = {}
        self.tasks: dict[str, TaskModel] = {}
        self.steps: dict[str, StepModel] = {}
        self.contracts: dict[str, TaskContractModel] = {}
        self.attempts: dict[str, ExecutionAttemptModel] = {}
        self.permits: dict[str, ExecutionPermitModel] = {}
        self.transaction_count = 0


class _Transaction:
    def __init__(self, session: "_Session") -> None:
        self._session = session

    async def __aenter__(self) -> "_Session":
        return self._session

    async def __aexit__(self, exc_type, _exc, _tb) -> bool:
        if exc_type is None:
            self._session._commit_pending()
            self._session.store.transaction_count += 1
        self._session.pending.clear()
        return False


class _Session:
    def __init__(self, store: _Store) -> None:
        self.store = store
        self.pending: list[Any] = []

    def begin(self) -> _Transaction:
        return _Transaction(self)

    def add(self, model: Any) -> None:
        self.pending.append(model)

    async def flush(self) -> None:
        for model in self.pending:
            if isinstance(model, (TaskModel, StepModel)):
                model.created_at = model.created_at or NOW.replace(tzinfo=None)
                model.modified_at = model.modified_at or NOW.replace(tzinfo=None)
            if isinstance(model, StepModel) and model.is_last is None:
                model.is_last = False

    async def scalars(self, statement: Any) -> _ScalarResult:
        entity = statement.column_descriptions[0]["entity"]
        params = statement.compile().params
        if entity is GovernedTaskAdmissionModel:
            values = list(self.store.admissions.values())
            key = params.get("admission_id_1")
            if key is not None:
                values = [value for value in values if value.admission_id == key]
        elif entity is TaskModel:
            values = list(self.store.tasks.values())
            key = params.get("task_id_1")
            if key is not None:
                values = [value for value in values if value.task_id == key]
        elif entity is StepModel:
            values = list(self.store.steps.values())
            key = params.get("step_id_1")
            if key is not None:
                values = [value for value in values if value.step_id == key]
        elif entity is TaskContractModel:
            values = list(self.store.contracts.values())
            key = params.get("contract_id_1")
            if key is not None:
                values = [value for value in values if value.contract_id == key]
        elif entity is ExecutionAttemptModel:
            values = list(self.store.attempts.values())
            for field in ("attempt_id", "task_id", "step_id"):
                key = params.get(f"{field}_1")
                if key is not None:
                    values = [value for value in values if getattr(value, field) == key]
        elif entity is ExecutionPermitModel:
            values = list(self.store.permits.values())
            key = params.get("permit_id_1")
            if key is not None:
                values = [value for value in values if value.permit_id == key]
        else:  # pragma: no cover - protects the fake from silent query widening
            raise AssertionError(f"Unexpected entity query: {entity}")
        return _ScalarResult(values)

    def _commit_pending(self) -> None:
        for model in self.pending:
            if isinstance(model, TaskModel):
                self.store.tasks[model.task_id] = model
            elif isinstance(model, StepModel):
                self.store.steps[model.step_id] = model
            elif isinstance(model, TaskContractModel):
                self.store.contracts[model.contract_id] = model
            elif isinstance(model, ExecutionAttemptModel):
                self.store.attempts[model.attempt_id] = model
            elif isinstance(model, ExecutionPermitModel):
                self.store.permits[model.permit_id] = model


class _SessionContext(AbstractAsyncContextManager[_Session]):
    def __init__(self, store: _Store) -> None:
        self.session = _Session(store)

    async def __aenter__(self) -> _Session:
        return self.session

    async def __aexit__(self, _exc_type, _exc, _tb) -> bool:
        return False


class _Observer:
    def __init__(self, inputs: dict[str, object] | None = None) -> None:
        self._inputs = inputs or INPUTS

    async def observe(self, *, scraped_page: Any) -> dict[str, object]:
        del scraped_page
        return dict(self._inputs)


class _Authorizer:
    async def authorize(self, *, task, step, scraped_page, action, binding, execution_binding):
        del binding
        from enterprise.governance.audit import observation_hash
        from enterprise.governance.classification import action_fingerprint

        observed_hash = observation_hash(url=scraped_page.url, html=scraped_page.html, secret="m7-secret")
        fingerprint = action_fingerprint(
            task_id=task.task_id,
            step_id=step.step_id,
            action_payload=action.model_dump(mode="json", exclude_none=True),
            observation_hash=observed_hash,
            secret="m7-secret",
        )
        return (
            ExecutionAuthorization(
                permit_id="permit-m7",
                action_fingerprint=fingerprint,
                observation_hash=observed_hash,
                idempotency_key=f"synthetic:{INPUTS['payment_id']}",
                effect=ExecutionEffect.EXTERNAL_WRITE,
            ),
            ExecutionProfile(
                mechanism=ExecutionMechanism.LOCATOR,
                fallback_rank=0,
                evidence_refs=[f"agentpact://m7/{execution_binding.binding_digest}"],
            ),
        )


class _ForgedAuthorizer(_Authorizer):
    async def authorize(self, **kwargs: Any):
        authorization, profile = await super().authorize(**kwargs)
        return authorization.model_copy(update={"action_fingerprint": "0" * 64}), profile


def _compiled_bundle():
    preliminary = _compile(task_id="placeholder-task")
    native_task_id = derive_native_task_id(
        admission_id=ADMISSION_ID,
        request_id=REQUEST_ID,
        work_order_id=preliminary.work_order.work_order_id,
    )
    compilation = _compile(task_id=native_task_id)
    grant = compilation.grants.grants[0]
    request = CapabilityRequest(
        request_id=REQUEST_ID,
        submitted_at=NOW,
        entry_mode=EntryMode.CHAT,
        principal_ref="operator-m7",
        session_ref="session-m7",
        tenant_id=TENANT,
        requested_scope=compilation.business_plan.data_scope,
        capability_ref=CAPABILITY_ID,
        capability_version=PACK_VERSION,
        request_kind=CapabilityRequestKind.TRANSITION,
        typed_inputs=dict(INPUTS),
        resource_refs={INPUTS["payment_id"]},
        user_intent_summary="Submit one synthetic payment",
        grant_ref=grant.grant_id,
        contract_versions={"domain_pack": PACK_VERSION},
    )
    snapshot = TrustedTaskCreationSnapshot(
        task_id=native_task_id,
        organization_id=TENANT,
        creation_path=TaskCreationPath.NATIVE,
        initiator_id="operator-m7",
        service_principal_id="synthetic_m6_planner_service",
        department_id=PAYMENTS_DEPARTMENT_ID,
        business_line_id=BUSINESS_LINE_ID,
        authorization_snapshot={"installation_id": compilation.installation.installation_id},
        policy_version=POLICY_VERSION,
        contract_version=1,
        created_at=NOW,
        request_id=REQUEST_ID,
    )
    audit = AdmissionAuditRecord(
        admission_id=ADMISSION_ID,
        request_id=REQUEST_ID,
        task_id=native_task_id,
        organization_id=TENANT,
        contract_id=compilation.task_contract.contract_id,
        plan_id=compilation.business_plan.plan_id,
        grant_id=grant.grant_id,
        capability_id=CAPABILITY_ID,
        capability_version=PACK_VERSION,
        policy_version=POLICY_VERSION,
        revocation_epoch=grant.revocation_epoch,
        mode=compilation.task_contract.mode,
        created_at=NOW,
    )
    bundle = TaskAdmissionBundle(
        admission_id=ADMISSION_ID,
        task=GovernedTaskDraft(
            task_id=native_task_id,
            organization_id=TENANT,
            goal=compilation.task_contract.goal,
        ),
        creation_snapshot=snapshot,
        contract=compilation.task_contract,
        request=request,
        grants=tuple(compilation.grants.grants),
        plan=compilation.business_plan,
        work_orders=(compilation.work_order,),
        audit_record=audit,
    )
    return compilation, bundle


def _compile(*, task_id: str):
    context = SyntheticM6TrustedContext(
        request_id=REQUEST_ID,
        task_id=task_id,
        contract_id="contract-m7-native-001",
        tenant_id=TENANT,
        user=UserContext(
            user_id="operator-m7",
            org_id=TENANT,
            department_roles=[
                DepartmentRole(
                    department_id=PAYMENTS_DEPARTMENT_ID,
                    department_name="Synthetic payments",
                    role="operator",
                )
            ],
            business_line_ids=[BUSINESS_LINE_ID],
        ),
        data_scope=CapabilityDataScope(
            department_id=PAYMENTS_DEPARTMENT_ID,
            business_line_id=BUSINESS_LINE_ID,
            resource_ids={INPUTS["payment_id"]},
        ),
        resolved_at=NOW,
    )
    manifest = build_pack_sdk_manifest()
    installation = build_synthetic_installation(
        tenant_id=TENANT,
        accepted_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(minutes=30),
        contract_digest=manifest.manifest_digest,
    )
    return compile_synthetic_request(
        natural_language_request="Submit the approved synthetic payment once",
        context=context,
        installation=installation,
        conformance_report=evaluate_static_pack_conformance(manifest),
        planner=DeterministicPlanner(INPUTS),
    )


def _store_with_admission(bundle: TaskAdmissionBundle) -> _Store:
    store = _Store()
    store.admissions[bundle.admission_id] = GovernedTaskAdmissionModel(
        admission_id=bundle.admission_id,
        organization_id=bundle.task.organization_id,
        request_id=bundle.request.request_id,
        task_id=bundle.task.task_id,
        contract_id=bundle.contract.contract_id,
        bundle_schema_version=bundle.schema_version,
        admission_fingerprint="admission-fingerprint-m7",
        bundle_fingerprint="bundle-fingerprint-m7",
        bundle_payload=bundle.model_dump(mode="json"),
        mode="audit",
        committed_at=NOW,
    )
    return store


async def _publish(store: _Store):
    compilation, bundle = _compiled_bundle()
    adapter = NativeSkyvernWorkOrderAdapter(
        SqlAlchemyNativePublicationRepository(lambda: _SessionContext(store)),
        compilation=compilation,
        admission_bundle=bundle,
        target_url="http://127.0.0.1:8000/synthetic-payment",
        navigation_payload=dict(INPUTS),
        clock=lambda: NOW,
    )
    binding = await adapter.prepare(compilation.work_order)
    return compilation, bundle, adapter, binding


def _task_and_step(store: _Store, binding) -> tuple[Task, Step]:
    task_model = store.tasks[binding.native_task_id]
    step_model = store.steps[binding.native_step_id]
    task_model.created_at = task_model.created_at or NOW.replace(tzinfo=None)
    task_model.modified_at = task_model.modified_at or NOW.replace(tzinfo=None)
    step_model.created_at = step_model.created_at or NOW.replace(tzinfo=None)
    step_model.modified_at = step_model.modified_at or NOW.replace(tzinfo=None)
    task = Task(
        task_id=task_model.task_id,
        organization_id=task_model.organization_id,
        status=TaskStatus(task_model.status),
        url=task_model.url,
        title=task_model.title,
        navigation_goal=task_model.navigation_goal,
        navigation_payload=task_model.navigation_payload,
        application=task_model.application,
        created_at=task_model.created_at,
        modified_at=task_model.modified_at,
    )
    step = Step(
        task_id=step_model.task_id,
        step_id=step_model.step_id,
        organization_id=step_model.organization_id,
        status=StepStatus(step_model.status),
        order=step_model.order,
        retry_index=step_model.retry_index,
        is_last=step_model.is_last,
        created_by=step_model.created_by,
        created_at=step_model.created_at,
        modified_at=step_model.modified_at,
    )
    return task, step


def _scraped(action_name: str = "execute_payment", field_name: str | None = None):
    attributes = {"data-governance-action": action_name}
    if field_name:
        attributes["data-governance-field"] = field_name
    return SimpleNamespace(
        url="http://127.0.0.1:8000/synthetic-payment",
        html="<html>fresh synthetic observation</html>",
        id_to_element_dict={"m7-element": {"attributes": attributes}},
    )


def _click(task: Task, step: Step) -> ClickAction:
    return ClickAction(
        element_id="m7-element",
        organization_id=task.organization_id,
        task_id=task.task_id,
        step_id=step.step_id,
        step_order=0,
        action_order=0,
        reasoning="Synthetic M7",
        intention="Execute once",
    )


def _record_consumed_permit(store: _Store, binding, authorization: ExecutionAuthorization) -> None:
    store.permits[authorization.permit_id] = ExecutionPermitModel(
        permit_id=authorization.permit_id,
        task_id=binding.native_task_id,
        step_id=binding.native_step_id,
        contract_id=binding.contract_id,
        action_fingerprint=authorization.action_fingerprint,
        observation_hash=authorization.observation_hash,
        policy_decision_id="decision-m7",
        decision_payload={},
        status="consumed",
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
        used_at=NOW,
    )


def _signed_probe(
    *,
    binding,
    authorization: ExecutionAuthorization,
    attempt_id: str,
    status: ResultProbeStatus,
    observed_version: int | None,
    facts_hash: str | None,
):
    probe = ResultProbeEvidence(
        probe_ref=binding.result_probe_ref,
        status=status,
        resource_id=INPUTS["payment_id"],
        checked_at=NOW,
        observed_version=observed_version,
        facts_hash=facts_hash,
    )
    return build_native_probe_evidence(
        binding=binding,
        authorization=authorization,
        attempt_id=attempt_id,
        result_probe=probe,
        hmac_secret="m7-secret",
    )


@pytest.mark.asyncio
async def test_native_adapter_atomically_publishes_and_reconciles_exact_pair():
    compilation, bundle = _compiled_bundle()
    store = _store_with_admission(bundle)
    compilation, _bundle, adapter, binding = await _publish(store)

    assert binding.native_task_id == compilation.work_order.task_id
    assert binding.native_step_id == derive_native_step_id(native_task_id=binding.native_task_id)
    assert store.tasks[binding.native_task_id].status == TaskStatus.running.value
    assert store.steps[binding.native_step_id].status == StepStatus.created.value
    assert store.tasks[binding.native_task_id].application == M7_APPLICATION_MARKER
    assert store.steps[binding.native_step_id].created_by == M7_APPLICATION_MARKER
    assert store.contracts[binding.contract_id].task_id == binding.native_task_id
    assert store.transaction_count == 2

    duplicate = await adapter.prepare(compilation.work_order)
    assert duplicate == binding.model_copy(update={"duplicate": True})
    assert len(store.tasks) == len(store.steps) == len(store.contracts) == 1


@pytest.mark.asyncio
async def test_native_adapter_rejects_partial_or_semantically_changed_rows():
    compilation, bundle = _compiled_bundle()
    store = _store_with_admission(bundle)
    _compilation, _bundle, adapter, binding = await _publish(store)
    del store.steps[binding.native_step_id]
    with pytest.raises(NativePublicationConflict, match="partial"):
        await adapter.prepare(compilation.work_order)

    store = _store_with_admission(bundle)
    _compilation, _bundle, adapter, binding = await _publish(store)
    store.tasks[binding.native_task_id].navigation_goal = "forged goal"
    with pytest.raises(NativePublicationConflict, match="read-back mismatch"):
        await adapter.prepare(compilation.work_order)


@pytest.mark.asyncio
async def test_resolver_covers_four_dispositions_and_fails_closed_on_mismatch():
    compilation, bundle = _compiled_bundle()
    store = _store_with_admission(bundle)
    compilation, _bundle, _adapter, binding = await _publish(store)
    store.steps[binding.native_step_id].status = StepStatus.running.value
    task, step = _task_and_step(store, binding)
    resolver = SyntheticNativeActionContextResolver(
        lambda: _SessionContext(store),
        binding=binding,
        compilation=compilation,
        authorizer=_Authorizer(),
        business_input_observer=_Observer(),
        hmac_secret="m7-secret",
        clock=lambda: NOW,
    )

    non_effect = await resolver.resolve(
        task=task,
        step=step,
        scraped_page=_scraped(action_name="", field_name="reference"),
        action=InputTextAction(
            element_id="m7-element",
            text=INPUTS["reference"],
            organization_id=task.organization_id,
            task_id=task.task_id,
            step_id=step.step_id,
            step_order=0,
            action_order=0,
            reasoning="Synthetic M7",
            intention="Fill exact reference",
        ),
    )
    assert non_effect.disposition is NativeActionDisposition.BOUND_NON_EFFECT

    authorized = await resolver.resolve(
        task=task,
        step=step,
        scraped_page=_scraped(),
        action=_click(task, step),
    )
    assert authorized.disposition is NativeActionDisposition.BOUND_AUTHORIZED_EFFECT
    assert authorized.execution_authorization is not None

    denied = await resolver.resolve(
        task=task,
        step=step,
        scraped_page=_scraped(action_name="delete_payment"),
        action=_click(task, step),
    )
    assert denied.disposition is NativeActionDisposition.BOUND_DENIED

    unbound_task = task.model_copy(update={"task_id": "ordinary-task"})
    unbound_step = step.model_copy(update={"task_id": "ordinary-task", "step_id": "ordinary-step"})
    store.tasks["ordinary-task"] = TaskModel(
        task_id="ordinary-task",
        organization_id=TENANT,
        status=TaskStatus.running.value,
        url=task.url,
        application=None,
    )
    store.steps["ordinary-step"] = StepModel(
        step_id="ordinary-step",
        task_id="ordinary-task",
        organization_id=TENANT,
        status=StepStatus.running.value,
        order=0,
        retry_index=0,
    )
    unbound = await resolver.resolve(
        task=unbound_task,
        step=unbound_step,
        scraped_page=_scraped(),
        action=_click(unbound_task, unbound_step),
    )
    assert unbound.disposition is NativeActionDisposition.UNBOUND_COMPATIBILITY

    store.steps[binding.native_step_id].created_by = None
    partial = await resolver.resolve(
        task=task,
        step=step,
        scraped_page=_scraped(),
        action=_click(task, step),
    )
    assert partial.disposition is NativeActionDisposition.BOUND_DENIED


@pytest.mark.asyncio
async def test_resolver_denies_stale_changed_or_forged_bound_authority():
    compilation, bundle = _compiled_bundle()
    store = _store_with_admission(bundle)
    compilation, _bundle, _adapter, binding = await _publish(store)
    store.steps[binding.native_step_id].status = StepStatus.running.value
    task, step = _task_and_step(store, binding)
    authority_expiries = (
        compilation.installation.expires_at,
        compilation.grants.grants[0].expires_at,
        compilation.task_contract.expires_at,
    )
    assert binding.expires_at == min(authority_expiries)

    stale = SyntheticNativeActionContextResolver(
        lambda: _SessionContext(store),
        binding=binding,
        compilation=compilation,
        authorizer=_Authorizer(),
        business_input_observer=_Observer(),
        hmac_secret="m7-secret",
        clock=lambda: max(authority_expiries),
    )
    assert (
        await stale.resolve(task=task, step=step, scraped_page=_scraped(), action=_click(task, step))
    ).disposition is NativeActionDisposition.BOUND_DENIED

    changed_inputs = dict(INPUTS)
    changed_inputs["amount"] = "9000.00"
    input_mismatch = SyntheticNativeActionContextResolver(
        lambda: _SessionContext(store),
        binding=binding,
        compilation=compilation,
        authorizer=_Authorizer(),
        business_input_observer=_Observer(changed_inputs),
        hmac_secret="m7-secret",
        clock=lambda: NOW,
    )
    assert (
        await input_mismatch.resolve(task=task, step=step, scraped_page=_scraped(), action=_click(task, step))
    ).disposition is NativeActionDisposition.BOUND_DENIED

    changed_work_order = compilation.model_copy(
        update={
            "work_order": compilation.work_order.model_copy(
                update={"navigation_goal": "forged changed Work Order"}
            )
        }
    )
    work_order_mismatch = SyntheticNativeActionContextResolver(
        lambda: _SessionContext(store),
        binding=binding,
        compilation=changed_work_order,
        authorizer=_Authorizer(),
        business_input_observer=_Observer(),
        hmac_secret="m7-secret",
        clock=lambda: NOW,
    )
    assert (
        await work_order_mismatch.resolve(
            task=task,
            step=step,
            scraped_page=_scraped(),
            action=_click(task, step),
        )
    ).disposition is NativeActionDisposition.BOUND_DENIED

    for mismatched_binding in (
        binding.model_copy(update={"adapter_ref": "forged-adapter"}),
        binding.model_copy(update={"result_probe_ref": "forged-probe"}),
    ):
        resolver = SyntheticNativeActionContextResolver(
            lambda: _SessionContext(store),
            binding=mismatched_binding,
            compilation=compilation,
            authorizer=_Authorizer(),
            business_input_observer=_Observer(),
            hmac_secret="m7-secret",
            clock=lambda: NOW,
        )
        assert (
            await resolver.resolve(task=task, step=step, scraped_page=_scraped(), action=_click(task, step))
        ).disposition is NativeActionDisposition.BOUND_DENIED

    forged = SyntheticNativeActionContextResolver(
        lambda: _SessionContext(store),
        binding=binding,
        compilation=compilation,
        authorizer=_ForgedAuthorizer(),
        business_input_observer=_Observer(),
        hmac_secret="m7-secret",
        clock=lambda: NOW,
    )
    assert (
        await forged.resolve(task=task, step=step, scraped_page=_scraped(), action=_click(task, step))
    ).disposition is NativeActionDisposition.BOUND_DENIED


@pytest.mark.asyncio
async def test_unknown_suspension_probe_recovery_and_trace_are_idempotent_and_redacted():
    compilation, bundle = _compiled_bundle()
    store = _store_with_admission(bundle)
    compilation, _bundle, _adapter, binding = await _publish(store)
    store.steps[binding.native_step_id].status = StepStatus.running.value
    task, step = _task_and_step(store, binding)
    resolver = SyntheticNativeActionContextResolver(
        lambda: _SessionContext(store),
        binding=binding,
        compilation=compilation,
        authorizer=_Authorizer(),
        business_input_observer=_Observer(),
        hmac_secret="m7-secret",
        clock=lambda: NOW,
    )
    resolution = await resolver.resolve(
        task=task,
        step=step,
        scraped_page=_scraped(),
        action=_click(task, step),
    )
    authorization = resolution.execution_authorization
    assert authorization is not None
    attempt = ExecutionAttemptModel(
        attempt_id="attempt-m7",
        task_id=binding.native_task_id,
        step_id=binding.native_step_id,
        contract_id=binding.contract_id,
        action_fingerprint=authorization.action_fingerprint,
        observation_hash=authorization.observation_hash,
        idempotency_key=authorization.idempotency_key,
        status=ExecutionAttemptStatus.UNKNOWN.value,
        error_message="probe pending",
    )
    store.attempts[attempt.attempt_id] = attempt
    _record_consumed_permit(store, binding, authorization)

    suspended = await resolver.suspend_for_probe(
        task=task,
        step=step,
        resolution=resolution,
        attempt_id=attempt.attempt_id,
    )
    assert suspended.status is StepStatus.pending_result_probe
    assert store.tasks[binding.native_task_id].status == TaskStatus.pending_result_probe.value

    # Simulate the crash window after Attempt UNKNOWN but before native suspension.
    store.tasks[binding.native_task_id].status = TaskStatus.running.value
    store.steps[binding.native_step_id].status = StepStatus.running.value
    inconclusive_evidence = _signed_probe(
        binding=binding,
        authorization=authorization,
        attempt_id=attempt.attempt_id,
        status=ResultProbeStatus.UNKNOWN,
        observed_version=None,
        facts_hash=None,
    )
    inconclusive = await resolver.reconcile_probe(evidence=inconclusive_evidence)
    assert inconclusive.attempt_status is ExecutionAttemptStatus.UNKNOWN
    assert inconclusive.task_status is TaskStatus.pending_result_probe
    assert inconclusive.step_status is StepStatus.pending_result_probe

    confirmed_evidence = _signed_probe(
        binding=binding,
        authorization=authorization,
        attempt_id=attempt.attempt_id,
        status=ResultProbeStatus.CONFIRMED,
        observed_version=2,
        facts_hash="a" * 64,
    )
    confirmed = await resolver.reconcile_probe(evidence=confirmed_evidence)
    repeated = await resolver.reconcile_probe(evidence=confirmed_evidence)
    assert confirmed.attempt_status is ExecutionAttemptStatus.CONFIRMED
    assert confirmed.task_status is TaskStatus.completed
    assert confirmed.step_status is StepStatus.completed
    assert repeated.duplicate is True

    trace = build_redacted_m7_trace(
        compilation=compilation,
        binding=binding,
        permit_id=authorization.permit_id,
        attempt_id=attempt.attempt_id,
        probe_receipt=confirmed,
    )
    serialized = trace.model_dump_json()
    for field_name in ("payment_id", "beneficiary_id", "amount", "currency", "reference"):
        assert str(INPUTS[field_name]) not in serialized
    assert binding.native_task_id in serialized
    assert binding.native_step_id in serialized
    assert binding.result_probe_ref in serialized
    with pytest.raises(ValueError, match="Permit"):
        build_redacted_m7_trace(
            compilation=compilation,
            binding=binding,
            permit_id="permit-substituted",
            attempt_id=attempt.attempt_id,
            probe_receipt=confirmed,
        )
    with pytest.raises(ValueError, match="Attempt"):
        build_redacted_m7_trace(
            compilation=compilation,
            binding=binding,
            permit_id=authorization.permit_id,
            attempt_id="attempt-substituted",
            probe_receipt=confirmed,
        )


@pytest.mark.asyncio
async def test_probe_rejects_mismatched_attempt_permit_binding_and_signature():
    compilation, bundle = _compiled_bundle()
    store = _store_with_admission(bundle)
    compilation, _bundle, _adapter, binding = await _publish(store)
    store.steps[binding.native_step_id].status = StepStatus.running.value
    task, step = _task_and_step(store, binding)
    resolver = SyntheticNativeActionContextResolver(
        lambda: _SessionContext(store),
        binding=binding,
        compilation=compilation,
        authorizer=_Authorizer(),
        business_input_observer=_Observer(),
        hmac_secret="m7-secret",
        clock=lambda: NOW,
    )
    resolution = await resolver.resolve(task=task, step=step, scraped_page=_scraped(), action=_click(task, step))
    authorization = resolution.execution_authorization
    assert authorization is not None
    attempt = ExecutionAttemptModel(
        attempt_id="attempt-m7-correlation",
        task_id=binding.native_task_id,
        step_id=binding.native_step_id,
        contract_id=binding.contract_id,
        action_fingerprint=authorization.action_fingerprint,
        observation_hash=authorization.observation_hash,
        idempotency_key=authorization.idempotency_key,
        status=ExecutionAttemptStatus.UNKNOWN.value,
    )
    store.attempts[attempt.attempt_id] = attempt
    _record_consumed_permit(store, binding, authorization)

    mismatched = [
        _signed_probe(
            binding=binding,
            authorization=authorization,
            attempt_id="attempt-substituted",
            status=ResultProbeStatus.CONFIRMED,
            observed_version=2,
            facts_hash="c" * 64,
        ),
        _signed_probe(
            binding=binding,
            authorization=authorization.model_copy(update={"permit_id": "permit-substituted"}),
            attempt_id=attempt.attempt_id,
            status=ResultProbeStatus.CONFIRMED,
            observed_version=2,
            facts_hash="c" * 64,
        ),
        _signed_probe(
            binding=binding.model_copy(update={"result_probe_ref": "probe-substituted"}),
            authorization=authorization,
            attempt_id=attempt.attempt_id,
            status=ResultProbeStatus.CONFIRMED,
            observed_version=2,
            facts_hash="c" * 64,
        ),
    ]
    valid = _signed_probe(
        binding=binding,
        authorization=authorization,
        attempt_id=attempt.attempt_id,
        status=ResultProbeStatus.CONFIRMED,
        observed_version=2,
        facts_hash="c" * 64,
    )
    mismatched.append(valid.model_copy(update={"action_fingerprint": "0" * 64}))
    for evidence in mismatched:
        with pytest.raises(NativeBoundStateDenied):
            await resolver.reconcile_probe(evidence=evidence)


@pytest.mark.asyncio
async def test_negative_probe_atomically_fails_attempt_and_native_pair():
    compilation, bundle = _compiled_bundle()
    store = _store_with_admission(bundle)
    compilation, _bundle, _adapter, binding = await _publish(store)
    store.steps[binding.native_step_id].status = StepStatus.running.value
    task, step = _task_and_step(store, binding)
    resolver = SyntheticNativeActionContextResolver(
        lambda: _SessionContext(store),
        binding=binding,
        compilation=compilation,
        authorizer=_Authorizer(),
        business_input_observer=_Observer(),
        hmac_secret="m7-secret",
        clock=lambda: NOW,
    )
    resolution = await resolver.resolve(
        task=task,
        step=step,
        scraped_page=_scraped(),
        action=_click(task, step),
    )
    authorization = resolution.execution_authorization
    assert authorization is not None
    attempt = ExecutionAttemptModel(
        attempt_id="attempt-m7-failed-probe",
        task_id=binding.native_task_id,
        step_id=binding.native_step_id,
        contract_id=binding.contract_id,
        action_fingerprint=authorization.action_fingerprint,
        observation_hash=authorization.observation_hash,
        idempotency_key=authorization.idempotency_key,
        status=ExecutionAttemptStatus.UNKNOWN.value,
        error_message="probe pending",
    )
    store.attempts[attempt.attempt_id] = attempt
    _record_consumed_permit(store, binding, authorization)

    failed_evidence = _signed_probe(
        binding=binding,
        authorization=authorization,
        attempt_id=attempt.attempt_id,
        status=ResultProbeStatus.NOT_CONFIRMED,
        observed_version=1,
        facts_hash="b" * 64,
    )
    failed = await resolver.reconcile_probe(evidence=failed_evidence)
    repeated = await resolver.reconcile_probe(evidence=failed_evidence)
    assert failed.attempt_status is ExecutionAttemptStatus.FAILED
    assert failed.task_status is TaskStatus.failed
    assert failed.step_status is StepStatus.failed
    assert store.tasks[binding.native_task_id].status == TaskStatus.failed.value
    assert store.steps[binding.native_step_id].status == StepStatus.failed.value
    assert repeated.duplicate is True
