"""Synthetic-only M7 native Task publication and governed Agent integration."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from enterprise.agent.work_orders import ExecutionWorkOrder, SkyvernPreparationReceipt
from enterprise.governance.admission import TaskAdmissionBundle
from enterprise.governance.contracts import (
    ActionIntent,
    DecisionOutcome,
    ExecutionAttemptStatus,
    ExecutionAuthorization,
    ExecutionEffect,
    PolicyDecision,
)
from enterprise.governance.execution_attempt_service import resolve_unknown_execution_attempt
from enterprise.governance.execution_profiles import ExecutionProfile
from enterprise.governance.models import (
    ExecutionAttemptModel,
    ExecutionPermitModel,
    GovernedTaskAdmissionModel,
    TaskContractModel,
)
from enterprise.governance.result_probes import ResultProbeEvidence, ResultProbeStatus
from skyvern.forge.native_action import (
    M7_APPLICATION_MARKER,
    NativeActionDisposition,
    NativeActionResolution,
)
from skyvern.forge.sdk.db.models import StepModel, TaskModel
from skyvern.forge.sdk.models import Step, StepStatus
from skyvern.forge.sdk.schemas.tasks import Task, TaskStatus
from skyvern.webeye.actions.action_types import ActionType
from skyvern.webeye.actions.actions import Action, CompleteAction, InputTextAction, SelectOptionAction
from skyvern.webeye.scraper.scraped_page import ScrapedPage

from .constants import CAPABILITY_ID, RESULT_PROBE_REF
from .m6_runtime import (
    SYNTHETIC_ADAPTER_REF,
    SyntheticM6Compilation,
    SyntheticM6ExecutionBinding,
    bind_compilation_for_execution,
)

M7_BINDING_SCHEMA = "agentpact-m7-native-binding/v1"
M7_PROBE_EVIDENCE_SCHEMA = "agentpact-m7-probe-evidence/v1"
M7_TRACE_SCHEMA = "agentpact-m7-trace/v1"


class NativePublicationConflict(ValueError):
    """A deterministic native identity exists with different semantics."""


class NativeBoundStateDenied(PermissionError):
    """A marked or expected M7 pair failed exact revalidation."""


class NativeProbeOutcome(StrEnum):
    CONFIRMED = "confirmed"
    FAILED = "failed"
    INCONCLUSIVE = "inconclusive"


class M7TraceStage(StrEnum):
    M6_COMPILATION = "m6_compilation"
    ADMISSION = "admission"
    INSTALLATION = "installation"
    GRANT = "grant"
    TASK_CONTRACT = "task_contract"
    WORK_ORDER = "work_order"
    NATIVE_TASK = "native_task"
    NATIVE_STEP = "native_step"
    PERMIT = "permit"
    ATTEMPT = "attempt"
    BROWSER_EFFECT = "browser_effect"
    RESULT_PROBE = "result_probe"
    FINAL_STATE = "final_state"


class M7TraceEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    stage: M7TraceStage
    artifact_ref: str = Field(min_length=1)
    status: str = Field(min_length=1)
    digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class M7Trace(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = M7_TRACE_SCHEMA
    request_id: str
    events: tuple[M7TraceEvent, ...]


class NativeSkyvernBinding(SkyvernPreparationReceipt):
    """Frozen correlation receipt for one admitted native Task/Step pair."""

    schema_version: str = M7_BINDING_SCHEMA
    admission_id: str
    request_id: str
    plan_id: str
    organization_id: str
    contract_id: str
    plan_task_id: str | None = None
    authority_contract_id: str | None = None
    grant_id: str
    installation_id: str
    adapter_ref: str
    result_probe_ref: str
    navigation_payload_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    admission_bundle_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    compilation_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    expires_at: datetime


class NativeProbeReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    binding_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    adapter_ref: str
    result_probe_ref: str
    work_order_id: str
    task_id: str
    step_id: str
    contract_id: str
    permit_id: str
    attempt_id: str
    action_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    observation_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    idempotency_key_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    attempt_status: ExecutionAttemptStatus
    task_status: TaskStatus
    step_status: StepStatus
    outcome: NativeProbeOutcome
    duplicate: bool = False


class NativeProbeEvidence(BaseModel):
    """Signed authoritative probe evidence bound to one exact native effect."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = M7_PROBE_EVIDENCE_SCHEMA
    binding_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    adapter_ref: str
    result_probe_ref: str
    work_order_id: str
    task_id: str
    step_id: str
    contract_id: str
    permit_id: str
    attempt_id: str
    action_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    observation_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    idempotency_key_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    probe_status: ResultProbeStatus
    resource_id: str
    checked_at: datetime
    observed_version: int | None = Field(default=None, ge=0)
    business_facts_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    signature: str = Field(pattern=r"^[0-9a-f]{64}$")


def build_native_probe_evidence(
    *,
    binding: NativeSkyvernBinding,
    authorization: ExecutionAuthorization,
    attempt_id: str,
    result_probe: ResultProbeEvidence,
    hmac_secret: str,
) -> NativeProbeEvidence:
    """Seal trusted business-probe output to its Permit, Attempt, and M7 binding."""

    if not hmac_secret:
        raise ValueError("M7 native probe evidence requires an HMAC secret")
    evidence = NativeProbeEvidence(
        binding_digest=binding.binding_digest,
        adapter_ref=binding.adapter_ref,
        result_probe_ref=binding.result_probe_ref,
        work_order_id=binding.work_order_id,
        task_id=binding.native_task_id,
        step_id=binding.native_step_id,
        contract_id=binding.contract_id,
        permit_id=authorization.permit_id,
        attempt_id=attempt_id,
        action_fingerprint=authorization.action_fingerprint,
        observation_hash=authorization.observation_hash,
        idempotency_key_digest=_canonical_digest(authorization.idempotency_key),
        probe_status=result_probe.status,
        resource_id=result_probe.resource_id,
        checked_at=result_probe.checked_at,
        observed_version=result_probe.observed_version,
        business_facts_digest=result_probe.facts_hash,
        signature="0" * 64,
    )
    return evidence.model_copy(update={"signature": _probe_signature(evidence, hmac_secret)})


class NativePublicationRepository(Protocol):
    async def publish(
        self,
        *,
        binding: NativeSkyvernBinding,
        target_url: str,
        navigation_goal: str,
        navigation_payload: dict[str, object],
    ) -> NativeSkyvernBinding: ...


class NativeEffectAuthorizer(Protocol):
    async def authorize(
        self,
        *,
        task: Task,
        step: Step,
        scraped_page: ScrapedPage,
        action: Action,
        binding: NativeSkyvernBinding,
        execution_binding: SyntheticM6ExecutionBinding,
    ) -> tuple[ExecutionAuthorization, ExecutionProfile]: ...


class NativeApprovalEvaluator(Protocol):
    async def evaluate(
        self,
        *,
        intent: ActionIntent,
        observed_business_inputs: dict[str, Any],
    ) -> PolicyDecision: ...


class NativeBusinessInputObserver(Protocol):
    async def observe(self, *, scraped_page: ScrapedPage) -> dict[str, object]: ...


def derive_native_task_id(*, admission_id: str, request_id: str, work_order_id: str) -> str:
    return "tsk_m7_" + _canonical_digest(["agentpact-m7-task-id/v1", admission_id, request_id, work_order_id])


def derive_native_step_id(*, native_task_id: str, order: int = 0, retry_index: int = 0) -> str:
    return "stp_m7_" + _canonical_digest(["agentpact-m7-step-id/v1", native_task_id, order, retry_index])


class NativeSkyvernWorkOrderAdapter:
    """Admission-first concrete adapter that publishes but never executes."""

    def __init__(
        self,
        repository: NativePublicationRepository,
        *,
        compilation: SyntheticM6Compilation,
        admission_bundle: TaskAdmissionBundle,
        target_url: str,
        navigation_payload: dict[str, object],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._compilation = compilation
        self._admission_bundle = admission_bundle
        self._target_url = target_url
        self._navigation_payload = navigation_payload
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def prepare(self, work_order: ExecutionWorkOrder) -> NativeSkyvernBinding:
        now = self._clock()
        bundle = self._admission_bundle
        compilation = self._compilation
        if work_order != compilation.work_order:
            raise NativePublicationConflict("M7 adapter rejected a substituted Work Order")
        if work_order not in bundle.work_orders:
            raise NativePublicationConflict("M7 admission does not contain the exact Work Order")
        admitted_step = next(
            (step for step in bundle.plan.steps if step.step_id == work_order.business_plan_step_id),
            None,
        )
        if admitted_step is None or len(compilation.business_plan.steps) != 1:
            raise NativePublicationConflict("M7 admission is missing the compiled Work Order step")
        compiled_step = compilation.business_plan.steps[0]
        if (
            bundle.request.request_id != compilation.trace.request_id
            or bundle.contract != compilation.task_contract
            or tuple(bundle.grants) != tuple(compilation.grants.grants)
            or bundle.plan.task_id != compilation.business_plan.task_id
            or bundle.plan.contract_id != compilation.business_plan.contract_id
            or admitted_step != compiled_step
        ):
            raise NativePublicationConflict("M7 admission does not match the M6 compilation")
        plan_task_id = work_order.plan_task_id or work_order.task_id
        authority_contract_id = work_order.authority_contract_id or work_order.contract_id
        if (
            bundle.task.organization_id != compilation.installation.tenant_id
            or bundle.task.task_id != plan_task_id
            or bundle.contract.contract_id != authority_contract_id
        ):
            raise NativePublicationConflict("M7 admission identity does not match the Work Order")

        native_task_id = derive_native_task_id(
            admission_id=bundle.admission_id,
            request_id=bundle.request.request_id,
            work_order_id=work_order.work_order_id,
        )
        if native_task_id != work_order.task_id:
            raise NativePublicationConflict("M7 Work Order task identity is not its deterministic native Task ID")
        native_step_id = derive_native_step_id(native_task_id=native_task_id)
        execution_binding = bind_compilation_for_execution(
            compilation,
            observed_business_inputs=self._navigation_payload,
            work_order_id=work_order.work_order_id,
            now=now,
        )
        if execution_binding.task_id != native_task_id or execution_binding.contract_id != work_order.contract_id:
            raise NativePublicationConflict("M7 compilation cannot authorize the deterministic native Task")

        values: dict[str, object] = {
            "schema_version": M7_BINDING_SCHEMA,
            "work_order_id": work_order.work_order_id,
            "native_task_id": native_task_id,
            "native_step_id": native_step_id,
            "admission_id": bundle.admission_id,
            "request_id": bundle.request.request_id,
            "plan_id": bundle.plan.plan_id,
            "organization_id": bundle.task.organization_id,
            "contract_id": work_order.contract_id,
            "plan_task_id": plan_task_id,
            "authority_contract_id": authority_contract_id,
            "grant_id": work_order.grant_id,
            "installation_id": compilation.installation.installation_id,
            "adapter_ref": SYNTHETIC_ADAPTER_REF,
            "result_probe_ref": work_order.result_probe_ref,
            "navigation_payload_digest": _canonical_digest(self._navigation_payload),
            "admission_bundle_digest": _canonical_digest(bundle),
            "compilation_digest": _canonical_digest(compilation),
            "expires_at": execution_binding.expires_at,
        }
        binding = NativeSkyvernBinding(binding_digest=_canonical_digest(values), **values)
        return await self._repository.publish(
            binding=binding,
            target_url=self._target_url,
            navigation_goal=work_order.navigation_goal,
            navigation_payload=self._navigation_payload,
        )


class SqlAlchemyNativePublicationRepository:
    """Atomic deterministic Task/Step publication using existing tables."""

    def __init__(self, session_factory: Callable[[], AbstractAsyncContextManager[Any]]) -> None:
        self._session_factory = session_factory

    async def publish(
        self,
        *,
        binding: NativeSkyvernBinding,
        target_url: str,
        navigation_goal: str,
        navigation_payload: dict[str, object],
    ) -> NativeSkyvernBinding:
        duplicate = False
        async with self._session_factory() as session:
            async with session.begin():
                admission = await _load_admission(session, binding=binding, lock=True)
                task = await _load_task(session, binding.native_task_id, lock=True)
                step = await _load_step(session, binding.native_step_id, lock=True)
                if (task is None) != (step is None):
                    raise NativePublicationConflict("M7 found a partial deterministic Task/Step pair")
                contract = (
                    await session.scalars(
                        select(TaskContractModel)
                        .where(TaskContractModel.contract_id == binding.contract_id)
                        .with_for_update()
                    )
                ).first()
                bundle = TaskAdmissionBundle.model_validate(admission.bundle_payload)
                if task is None:
                    task = TaskModel(
                        task_id=binding.native_task_id,
                        organization_id=binding.organization_id,
                        status=TaskStatus.created.value,
                        title="AgentPact M7 synthetic native execution",
                        url=target_url,
                        navigation_goal=navigation_goal,
                        navigation_payload=navigation_payload,
                        application=M7_APPLICATION_MARKER,
                        errors=[],
                    )
                    step = StepModel(
                        step_id=binding.native_step_id,
                        organization_id=binding.organization_id,
                        task_id=binding.native_task_id,
                        status=StepStatus.created.value,
                        order=0,
                        retry_index=0,
                        is_last=True,
                        created_by=M7_APPLICATION_MARKER,
                    )
                    session.add(task)
                    await session.flush()
                    session.add(step)
                    if contract is None:
                        contract = _task_contract_model(
                            bundle=bundle,
                            native_task_id=binding.native_task_id,
                            native_contract_id=binding.contract_id,
                        )
                        session.add(contract)
                    await session.flush()
                else:
                    duplicate = True
                assert task is not None and step is not None
                _validate_publication_rows(
                    task=task,
                    step=step,
                    contract=contract,
                    bundle=bundle,
                    binding=binding,
                    target_url=target_url,
                    navigation_goal=navigation_goal,
                    navigation_payload=navigation_payload,
                    allowed_task_statuses={
                        TaskStatus.created.value,
                        TaskStatus.running.value,
                        TaskStatus.resuming.value,
                        TaskStatus.pending_result_probe.value,
                    },
                    allowed_step_statuses={
                        StepStatus.created.value,
                        StepStatus.running.value,
                        StepStatus.resuming.value,
                        StepStatus.pending_result_probe.value,
                    },
                )

        async with self._session_factory() as session:
            async with session.begin():
                task = await _load_task(session, binding.native_task_id, lock=True)
                step = await _load_step(session, binding.native_step_id, lock=True)
                admission = await _load_admission(session, binding=binding, lock=True)
                contract = (
                    await session.scalars(
                        select(TaskContractModel)
                        .where(TaskContractModel.contract_id == binding.contract_id)
                        .with_for_update()
                    )
                ).first()
                if task is None or step is None:
                    raise NativePublicationConflict("M7 deterministic pair disappeared before activation")
                bundle = TaskAdmissionBundle.model_validate(admission.bundle_payload)
                _validate_publication_rows(
                    task=task,
                    step=step,
                    contract=contract,
                    bundle=bundle,
                    binding=binding,
                    target_url=target_url,
                    navigation_goal=navigation_goal,
                    navigation_payload=navigation_payload,
                    allowed_task_statuses={
                        TaskStatus.created.value,
                        TaskStatus.running.value,
                        TaskStatus.resuming.value,
                        TaskStatus.pending_result_probe.value,
                    },
                    allowed_step_statuses={
                        StepStatus.created.value,
                        StepStatus.running.value,
                        StepStatus.resuming.value,
                        StepStatus.pending_result_probe.value,
                    },
                )
                if task.status == TaskStatus.created.value:
                    task.status = TaskStatus.running.value
                    task.started_at = datetime.now(timezone.utc)
                    await session.flush()
        return binding.model_copy(update={"duplicate": duplicate})


class SyntheticNativeActionContextResolver:
    """Fail-closed scoped resolver for one published synthetic M7 binding."""

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[Any]],
        *,
        binding: NativeSkyvernBinding,
        compilation: SyntheticM6Compilation,
        authorizer: NativeEffectAuthorizer,
        business_input_observer: NativeBusinessInputObserver,
        hmac_secret: str,
        approval_evaluator: NativeApprovalEvaluator | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not hmac_secret:
            raise ValueError("M7 native resolver requires an HMAC secret")
        self._session_factory = session_factory
        self._binding = binding
        self._compilation = compilation
        self._authorizer = authorizer
        self._business_input_observer = business_input_observer
        self._hmac_secret = hmac_secret
        self._approval_evaluator = approval_evaluator
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def resolve(
        self,
        *,
        task: Task,
        step: Step,
        scraped_page: ScrapedPage,
        action: Action,
    ) -> NativeActionResolution:
        try:
            bound = await self._require_or_prove_unbound(task=task, step=step)
            if not bound:
                return NativeActionResolution(disposition=NativeActionDisposition.UNBOUND_COMPATIBILITY)
            now = self._clock()
            self._require_live_authority(task=task, step=step, now=now)
            operation, field_name = _synthetic_operation(scraped_page=scraped_page, action=action)
            work_order = self._compilation.work_order
            if operation in work_order.prohibited_operations or operation not in work_order.allowed_operations:
                raise NativeBoundStateDenied("M7 action operation is outside the Work Order")
            if operation != "submit":
                if operation in {"input", "select"}:
                    _require_exact_non_effect_input(
                        action=action,
                        field_name=field_name,
                        expected_inputs=self._compilation.business_plan.steps[0].inputs,
                    )
                return NativeActionResolution(
                    disposition=NativeActionDisposition.BOUND_NON_EFFECT,
                    operation=operation,
                    binding_digest=self._binding.binding_digest,
                )

            observed_inputs = await self._business_input_observer.observe(scraped_page=scraped_page)
            execution_binding = bind_compilation_for_execution(
                self._compilation,
                observed_business_inputs=observed_inputs,
                work_order_id=self._binding.work_order_id,
                now=now,
            )
            if (
                execution_binding.task_id != self._binding.native_task_id
                or execution_binding.contract_id != self._binding.contract_id
                or execution_binding.grant_id != self._binding.grant_id
                or execution_binding.result_probe_ref != self._binding.result_probe_ref
            ):
                raise NativeBoundStateDenied("M7 execution binding does not match the native correlation")
            from enterprise.governance.audit import observation_hash
            from enterprise.governance.classification import action_fingerprint

            observed_hash = observation_hash(
                url=scraped_page.url,
                html=scraped_page.html,
                secret=self._hmac_secret,
            )
            fingerprint = action_fingerprint(
                task_id=task.task_id,
                step_id=step.step_id,
                action_payload=action.model_dump(mode="json", exclude_none=True),
                observation_hash=observed_hash,
                secret=self._hmac_secret,
            )
            if self._approval_evaluator is not None:
                intent = ActionIntent(
                    intent_id=f"m10-intent:{fingerprint}",
                    task_id=task.task_id,
                    step_id=step.step_id,
                    action_fingerprint=fingerprint,
                    observation_id=observed_hash,
                    operation=operation,
                    effect=ExecutionEffect.EXTERNAL_WRITE,
                    target={"kind": "synthetic-payment-submit"},
                    confidence=1.0,
                    evidence=["fresh-native-observation"],
                )
                decision = await self._approval_evaluator.evaluate(
                    intent=intent,
                    observed_business_inputs=dict(observed_inputs),
                )
                if decision.outcome is DecisionOutcome.REQUIRE_APPROVAL:
                    return NativeActionResolution(
                        disposition=NativeActionDisposition.APPROVAL_REQUIRED,
                        operation=operation,
                        binding_digest=self._binding.binding_digest,
                        observation_hash=observed_hash,
                        action_fingerprint=fingerprint,
                        approval_intent=intent,
                        approval_decision=decision,
                    )
                if decision.outcome is not DecisionOutcome.ALLOW:
                    raise NativeBoundStateDenied("M7 approval evaluator denied the bound effect")
            authorization, profile = await self._authorizer.authorize(
                task=task,
                step=step,
                scraped_page=scraped_page,
                action=action,
                binding=self._binding,
                execution_binding=execution_binding,
            )
            if (
                authorization.effect is not ExecutionEffect.EXTERNAL_WRITE
                or authorization.observation_hash != observed_hash
                or authorization.action_fingerprint != fingerprint
                or _canonical_digest(authorization.idempotency_key) != execution_binding.idempotency_key_digest
            ):
                raise NativeBoundStateDenied("M7 authorizer returned mismatched effect authority")
            return NativeActionResolution(
                disposition=NativeActionDisposition.BOUND_AUTHORIZED_EFFECT,
                operation=operation,
                binding_digest=self._binding.binding_digest,
                observation_hash=observed_hash,
                action_fingerprint=fingerprint,
                execution_authorization=authorization,
                execution_profile=profile,
            )
        except NativeBoundStateDenied:
            return NativeActionResolution(
                disposition=NativeActionDisposition.BOUND_DENIED,
                binding_digest=self._binding.binding_digest,
                denial_code="M7_BOUND_VALIDATION_FAILED",
            )
        except Exception:
            return NativeActionResolution(
                disposition=NativeActionDisposition.BOUND_DENIED,
                binding_digest=self._binding.binding_digest,
                denial_code="M7_BOUND_CONTEXT_INVALID",
            )

    async def suspend_for_probe(
        self,
        *,
        task: Task,
        step: Step,
        resolution: NativeActionResolution,
        attempt_id: str,
    ) -> Step:
        if resolution.disposition is not NativeActionDisposition.BOUND_AUTHORIZED_EFFECT:
            raise NativeBoundStateDenied("Only an authorized M7 effect may suspend for a probe")
        authorization = resolution.execution_authorization
        if authorization is None:
            raise NativeBoundStateDenied("M7 suspension is missing its execution authorization")
        async with self._session_factory() as session:
            async with session.begin():
                task_model = await _load_task(session, task.task_id, lock=True)
                step_model = await _load_step(session, step.step_id, lock=True)
                attempt = (
                    await session.scalars(
                        select(ExecutionAttemptModel)
                        .where(ExecutionAttemptModel.attempt_id == attempt_id)
                        .with_for_update()
                    )
                ).first()
                if task_model is None or step_model is None or attempt is None:
                    raise NativeBoundStateDenied("M7 suspension state is incomplete")
                _verify_unknown_attempt(
                    attempt=attempt,
                    binding=self._binding,
                    authorization=authorization,
                )
                _force_pending_probe(task=task_model, step=step_model)
                await session.flush()
                result = _step_contract(step_model)
        return result

    async def reconcile_probe(
        self,
        *,
        evidence: NativeProbeEvidence,
    ) -> NativeProbeReceipt:
        now = self._clock()
        outcome = _native_probe_outcome(evidence.probe_status)
        async with self._session_factory() as session:
            async with session.begin():
                attempt = (
                    await session.scalars(
                        select(ExecutionAttemptModel)
                        .where(ExecutionAttemptModel.attempt_id == evidence.attempt_id)
                        .with_for_update()
                    )
                ).first()
                permit = (
                    await session.scalars(
                        select(ExecutionPermitModel)
                        .where(ExecutionPermitModel.permit_id == evidence.permit_id)
                        .with_for_update()
                    )
                ).first()
                task = await _load_task(session, self._binding.native_task_id, lock=True)
                step = await _load_step(session, self._binding.native_step_id, lock=True)
                if attempt is None or permit is None or task is None or step is None:
                    raise NativeBoundStateDenied("M7 probe recovery is missing exact correlated state")
                _verify_native_probe_evidence(
                    evidence=evidence,
                    binding=self._binding,
                    compilation=self._compilation,
                    attempt=attempt,
                    permit=permit,
                    hmac_secret=self._hmac_secret,
                    now=now,
                )
                evidence_payload = evidence.model_dump(mode="json")

                terminal = {
                    ExecutionAttemptStatus.CONFIRMED.value: (
                        NativeProbeOutcome.CONFIRMED,
                        TaskStatus.completed,
                        StepStatus.completed,
                    ),
                    ExecutionAttemptStatus.FAILED.value: (
                        NativeProbeOutcome.FAILED,
                        TaskStatus.failed,
                        StepStatus.failed,
                    ),
                }
                if attempt.status in terminal:
                    expected_outcome, task_status, step_status = terminal[attempt.status]
                    if (
                        outcome is not expected_outcome
                        or task.status != task_status.value
                        or step.status != step_status.value
                        or _canonical_digest(attempt.result_probe) != _canonical_digest(evidence_payload)
                    ):
                        raise NativeBoundStateDenied("M7 repeated probe conflicts with terminal state")
                    return _native_probe_receipt(
                        binding=self._binding,
                        evidence=evidence,
                        attempt_status=ExecutionAttemptStatus(attempt.status),
                        task_status=task_status,
                        step_status=step_status,
                        outcome=outcome,
                        duplicate=True,
                    )
                if attempt.status != ExecutionAttemptStatus.UNKNOWN.value:
                    raise NativeBoundStateDenied("M7 probe may resolve only an UNKNOWN Attempt")
                _force_pending_probe(task=task, step=step)
                if outcome is NativeProbeOutcome.INCONCLUSIVE:
                    await session.flush()
                    return _native_probe_receipt(
                        binding=self._binding,
                        evidence=evidence,
                        attempt_status=ExecutionAttemptStatus.UNKNOWN,
                        task_status=TaskStatus.pending_result_probe,
                        step_status=StepStatus.pending_result_probe,
                        outcome=outcome,
                    )

                resolved = await resolve_unknown_execution_attempt(
                    db_session=session,
                    attempt_id=attempt.attempt_id,
                    confirmed=outcome is NativeProbeOutcome.CONFIRMED,
                    result_probe=evidence_payload,
                    now=now,
                )
                task_status = TaskStatus.completed if outcome is NativeProbeOutcome.CONFIRMED else TaskStatus.failed
                step_status = StepStatus.completed if outcome is NativeProbeOutcome.CONFIRMED else StepStatus.failed
                task.status = task_status.value
                step.status = step_status.value
                task.finished_at = now
                step.finished_at = now
                await session.flush()
                return _native_probe_receipt(
                    binding=self._binding,
                    evidence=evidence,
                    attempt_status=resolved.status,
                    task_status=task_status,
                    step_status=step_status,
                    outcome=outcome,
                )

    async def _require_or_prove_unbound(self, *, task: Task, step: Step) -> bool:
        async with self._session_factory() as session:
            task_model = await _load_task(session, task.task_id, lock=False)
            step_model = await _load_step(session, step.step_id, lock=False)
            if task_model is None or step_model is None or step_model.task_id != task.task_id:
                raise NativeBoundStateDenied("M7 authoritative Task/Step read failed")
            task_marked = task_model.application == M7_APPLICATION_MARKER
            step_marked = step_model.created_by == M7_APPLICATION_MARKER
            if not task_marked and not step_marked:
                if task.task_id == self._binding.native_task_id or step.step_id == self._binding.native_step_id:
                    raise NativeBoundStateDenied("Expected M7 pair lost its binding markers")
                return False
            if not task_marked or not step_marked:
                raise NativeBoundStateDenied("M7 found a partial bound marker")
            if (
                task.task_id != self._binding.native_task_id
                or step.step_id != self._binding.native_step_id
                or task.organization_id != self._binding.organization_id
                or step.organization_id != self._binding.organization_id
                or task_model.status != TaskStatus.running.value
                or step_model.status != StepStatus.running.value
                or _canonical_digest(task_model.navigation_payload) != self._binding.navigation_payload_digest
            ):
                raise NativeBoundStateDenied("M7 native Task/Step identity or state mismatch")
            await _load_admission(session, binding=self._binding, lock=False)
            return True

    def _require_live_authority(self, *, task: Task, step: Step, now: datetime) -> None:
        compilation = self._compilation
        business_step = compilation.business_plan.steps[0]
        expected_plan_task_id = self._binding.plan_task_id or self._binding.native_task_id
        expected_authority_contract_id = self._binding.authority_contract_id or self._binding.contract_id
        if (
            now >= self._binding.expires_at
            or compilation.installation.installation_id != self._binding.installation_id
            or compilation.installation.tenant_id != task.organization_id
            or compilation.installation.adapter_ref != self._binding.adapter_ref
            or compilation.installation.result_probe_ref != self._binding.result_probe_ref
            or compilation.work_order.result_probe_ref != RESULT_PROBE_REF
            or business_step.capability_id != CAPABILITY_ID
            or business_step.grant_id != self._binding.grant_id
            or compilation.task_contract.contract_id != expected_authority_contract_id
            or compilation.task_contract.task_id != expected_plan_task_id
            or compilation.work_order.task_id != task.task_id
            or (compilation.work_order.plan_task_id or compilation.work_order.task_id) != expected_plan_task_id
            or (compilation.work_order.authority_contract_id or compilation.work_order.contract_id)
            != expected_authority_contract_id
            or compilation.work_order.contract_id != self._binding.contract_id
            or task.task_id != self._binding.native_task_id
            or step.step_id != self._binding.native_step_id
            or _canonical_digest(compilation) != self._binding.compilation_digest
        ):
            raise NativeBoundStateDenied("M7 compilation authority is stale or mismatched")
        compilation.grants.require_executable(
            capability_id=business_step.capability_id,
            grant_id=business_step.grant_id,
            now=now,
        )


def build_redacted_m7_trace(
    *,
    compilation: SyntheticM6Compilation,
    binding: NativeSkyvernBinding,
    permit_id: str,
    attempt_id: str,
    probe_receipt: NativeProbeReceipt,
) -> M7Trace:
    if (
        probe_receipt.binding_digest != binding.binding_digest
        or probe_receipt.adapter_ref != binding.adapter_ref
        or probe_receipt.result_probe_ref != binding.result_probe_ref
        or probe_receipt.work_order_id != binding.work_order_id
        or probe_receipt.task_id != binding.native_task_id
        or probe_receipt.step_id != binding.native_step_id
        or probe_receipt.contract_id != binding.contract_id
    ):
        raise ValueError("M7 trace probe receipt does not match the native binding")
    if probe_receipt.permit_id != permit_id:
        raise ValueError("M7 trace Permit does not match the authoritative probe receipt")
    if probe_receipt.attempt_id != attempt_id:
        raise ValueError("M7 trace Attempt does not match the authoritative probe receipt")
    if _canonical_digest(compilation) != binding.compilation_digest:
        raise ValueError("M7 trace compilation does not match the native binding")
    status = probe_receipt.outcome.value
    return M7Trace(
        request_id=binding.request_id,
        events=(
            M7TraceEvent(
                stage=M7TraceStage.M6_COMPILATION,
                artifact_ref=binding.request_id,
                status="validated",
                digest=_canonical_digest(compilation.trace),
            ),
            M7TraceEvent(
                stage=M7TraceStage.ADMISSION,
                artifact_ref=binding.admission_id,
                status="committed",
                digest=binding.admission_bundle_digest,
            ),
            M7TraceEvent(
                stage=M7TraceStage.INSTALLATION,
                artifact_ref=binding.installation_id,
                status="accepted",
                digest=compilation.installation.contract_digest,
            ),
            M7TraceEvent(stage=M7TraceStage.GRANT, artifact_ref=binding.grant_id, status="executable"),
            M7TraceEvent(
                stage=M7TraceStage.TASK_CONTRACT,
                artifact_ref=binding.contract_id,
                status="bound",
            ),
            M7TraceEvent(
                stage=M7TraceStage.WORK_ORDER,
                artifact_ref=binding.work_order_id,
                status="bound",
                digest=_canonical_digest(compilation.work_order),
            ),
            M7TraceEvent(
                stage=M7TraceStage.NATIVE_TASK,
                artifact_ref=binding.native_task_id,
                status=probe_receipt.task_status.value,
                digest=binding.binding_digest,
            ),
            M7TraceEvent(
                stage=M7TraceStage.NATIVE_STEP,
                artifact_ref=binding.native_step_id,
                status=probe_receipt.step_status.value,
                digest=binding.navigation_payload_digest,
            ),
            M7TraceEvent(stage=M7TraceStage.PERMIT, artifact_ref=permit_id, status="consumed"),
            M7TraceEvent(
                stage=M7TraceStage.ATTEMPT, artifact_ref=attempt_id, status=probe_receipt.attempt_status.value
            ),
            M7TraceEvent(
                stage=M7TraceStage.BROWSER_EFFECT,
                artifact_ref=binding.work_order_id,
                status="transport_unknown_then_probed",
            ),
            M7TraceEvent(
                stage=M7TraceStage.RESULT_PROBE,
                artifact_ref=binding.result_probe_ref,
                status=status,
                digest=probe_receipt.evidence_digest,
            ),
            M7TraceEvent(stage=M7TraceStage.FINAL_STATE, artifact_ref=binding.request_id, status=status),
        ),
    )


async def _load_admission(
    session: Any,
    *,
    binding: NativeSkyvernBinding,
    lock: bool,
) -> GovernedTaskAdmissionModel:
    query = select(GovernedTaskAdmissionModel).where(GovernedTaskAdmissionModel.admission_id == binding.admission_id)
    if lock:
        query = query.with_for_update()
    admission = (await session.scalars(query)).first()
    if admission is None:
        raise NativePublicationConflict("M7 native publication requires durable admission")
    expected_plan_task_id = binding.plan_task_id or binding.native_task_id
    expected_authority_contract_id = binding.authority_contract_id or binding.contract_id
    if (
        admission.organization_id != binding.organization_id
        or admission.request_id != binding.request_id
        or admission.task_id != expected_plan_task_id
        or admission.contract_id != expected_authority_contract_id
        or _canonical_digest(admission.bundle_payload) != binding.admission_bundle_digest
    ):
        raise NativePublicationConflict("M7 durable admission does not match the native binding")
    return admission


async def _load_task(session: Any, task_id: str, *, lock: bool) -> TaskModel | None:
    query = select(TaskModel).where(TaskModel.task_id == task_id)
    if lock:
        query = query.with_for_update()
    return (await session.scalars(query)).first()


async def _load_step(session: Any, step_id: str, *, lock: bool) -> StepModel | None:
    query = select(StepModel).where(StepModel.step_id == step_id)
    if lock:
        query = query.with_for_update()
    return (await session.scalars(query)).first()


def _task_contract_model(
    *,
    bundle: TaskAdmissionBundle,
    native_task_id: str,
    native_contract_id: str | None = None,
) -> TaskContractModel:
    contract = bundle.contract
    contract_id = native_contract_id or contract.contract_id
    authorization_snapshot = dict(contract.authorization_snapshot)
    if native_contract_id is not None:
        authorization_snapshot.update(
            {
                "authority_contract_id": contract.contract_id,
                "plan_task_id": bundle.plan.task_id,
            }
        )
    return TaskContractModel(
        contract_id=contract_id,
        task_id=native_task_id,
        organization_id=contract.organization_id,
        initiator_id=contract.initiator_id,
        service_principal_id=contract.service_principal_id,
        department_id=contract.department_id,
        business_line_id=contract.business_line_id,
        goal=contract.goal,
        allowed_operations=sorted(contract.allowed_operations),
        data_scope=contract.data_scope,
        authorization_snapshot=authorization_snapshot,
        policy_profile=contract.policy_profile,
        policy_version=contract.policy_version,
        success_criteria=contract.success_criteria,
        mode=contract.mode.value,
        version=contract.version,
        expires_at=contract.expires_at,
    )


def _validate_publication_rows(
    *,
    task: TaskModel,
    step: StepModel,
    contract: TaskContractModel | None,
    bundle: TaskAdmissionBundle,
    binding: NativeSkyvernBinding,
    target_url: str,
    navigation_goal: str,
    navigation_payload: dict[str, object],
    allowed_task_statuses: set[str],
    allowed_step_statuses: set[str],
) -> None:
    if (
        task.task_id != binding.native_task_id
        or task.organization_id != binding.organization_id
        or task.url != target_url
        or task.navigation_goal != navigation_goal
        or task.application != M7_APPLICATION_MARKER
        or task.status not in allowed_task_statuses
        or _canonical_digest(task.navigation_payload) != _canonical_digest(navigation_payload)
        or _canonical_digest(task.navigation_payload) != binding.navigation_payload_digest
        or step.step_id != binding.native_step_id
        or step.task_id != binding.native_task_id
        or step.organization_id != binding.organization_id
        or step.status not in allowed_step_statuses
        or step.order != 0
        or step.retry_index != 0
        or step.is_last is not True
        or step.created_by != M7_APPLICATION_MARKER
        or contract is None
        or contract.contract_id != binding.contract_id
        or contract.task_id != binding.native_task_id
        or contract.organization_id != binding.organization_id
        or contract.goal != bundle.contract.goal
        or set(contract.allowed_operations or []) != bundle.contract.allowed_operations
        or contract.policy_version != bundle.contract.policy_version
        or (
            binding.authority_contract_id is not None
            and contract.authorization_snapshot.get("authority_contract_id") != binding.authority_contract_id
        )
        or (
            binding.plan_task_id is not None
            and contract.authorization_snapshot.get("plan_task_id") != binding.plan_task_id
        )
    ):
        raise NativePublicationConflict("M7 deterministic publication read-back mismatch")


def _synthetic_operation(*, scraped_page: ScrapedPage, action: Action) -> tuple[str, str | None]:
    if isinstance(action, CompleteAction):
        return "read", None
    element_id = getattr(action, "element_id", None)
    element = scraped_page.id_to_element_dict.get(str(element_id)) if element_id is not None else None
    attributes = (element or {}).get("attributes") or {}
    field_name = attributes.get("data-governance-field")
    action_name = attributes.get("data-governance-action")
    if action.action_type is ActionType.CLICK and action_name == "execute_payment":
        return "submit", None
    if isinstance(action, InputTextAction) and field_name:
        return "input", str(field_name)
    if isinstance(action, SelectOptionAction) and field_name:
        return "select", str(field_name)
    raise NativeBoundStateDenied("M7 synthetic mapper rejected an unknown or prohibited Action")


def _require_exact_non_effect_input(
    *,
    action: Action,
    field_name: str | None,
    expected_inputs: dict[str, object],
) -> None:
    if not field_name or field_name not in expected_inputs:
        raise NativeBoundStateDenied("M7 Action targets an unbound business input")
    expected = str(expected_inputs[field_name])
    if isinstance(action, InputTextAction):
        observed = action.text
    elif isinstance(action, SelectOptionAction):
        observed = action.option.value or action.option.label
    else:
        raise NativeBoundStateDenied("M7 non-effect Action type is unsupported")
    if observed != expected:
        raise NativeBoundStateDenied("M7 Action changed a compiled business input")


def _verify_unknown_attempt(
    *,
    attempt: ExecutionAttemptModel,
    binding: NativeSkyvernBinding,
    authorization: ExecutionAuthorization,
) -> None:
    if (
        attempt.status != ExecutionAttemptStatus.UNKNOWN.value
        or attempt.task_id != binding.native_task_id
        or attempt.step_id != binding.native_step_id
        or attempt.contract_id != binding.contract_id
        or attempt.action_fingerprint != authorization.action_fingerprint
        or attempt.observation_hash != authorization.observation_hash
        or attempt.idempotency_key != authorization.idempotency_key
    ):
        raise NativeBoundStateDenied("M7 UNKNOWN Attempt does not match the authorized native effect")


def _native_probe_outcome(status: ResultProbeStatus) -> NativeProbeOutcome:
    return {
        ResultProbeStatus.CONFIRMED: NativeProbeOutcome.CONFIRMED,
        ResultProbeStatus.NOT_CONFIRMED: NativeProbeOutcome.FAILED,
        ResultProbeStatus.UNKNOWN: NativeProbeOutcome.INCONCLUSIVE,
    }[status]


def _probe_signature(evidence: NativeProbeEvidence, hmac_secret: str) -> str:
    payload = evidence.model_dump(mode="json", exclude={"signature"})
    canonical = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hmac.new(
        hmac_secret.encode("utf-8"),
        (M7_PROBE_EVIDENCE_SCHEMA + "\n" + canonical).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _verify_native_probe_evidence(
    *,
    evidence: NativeProbeEvidence,
    binding: NativeSkyvernBinding,
    compilation: SyntheticM6Compilation,
    attempt: ExecutionAttemptModel,
    permit: ExecutionPermitModel,
    hmac_secret: str,
    now: datetime,
) -> None:
    if not hmac.compare_digest(evidence.signature, _probe_signature(evidence, hmac_secret)):
        raise NativeBoundStateDenied("M7 probe evidence signature is invalid")
    if (
        evidence.binding_digest != binding.binding_digest
        or evidence.adapter_ref != binding.adapter_ref
        or evidence.result_probe_ref != binding.result_probe_ref
        or evidence.work_order_id != binding.work_order_id
        or evidence.task_id != binding.native_task_id
        or evidence.step_id != binding.native_step_id
        or evidence.contract_id != binding.contract_id
        or compilation.installation.adapter_ref != evidence.adapter_ref
        or compilation.work_order.result_probe_ref != evidence.result_probe_ref
        or _canonical_digest(compilation) != binding.compilation_digest
    ):
        raise NativeBoundStateDenied("M7 probe evidence does not match the native binding")
    expected_resource = str(compilation.business_plan.steps[0].inputs.get("payment_id", ""))
    if not expected_resource or evidence.resource_id != expected_resource:
        raise NativeBoundStateDenied("M7 probe evidence targets a different business resource")
    if evidence.checked_at > now:
        raise NativeBoundStateDenied("M7 probe evidence is from the future")
    if evidence.probe_status is not ResultProbeStatus.UNKNOWN:
        expected_version = compilation.business_plan.steps[0].inputs.get("object_version")
        if (
            not isinstance(expected_version, int)
            or evidence.observed_version is None
            or evidence.business_facts_digest is None
            or (
                evidence.probe_status is ResultProbeStatus.CONFIRMED
                and evidence.observed_version != expected_version + 1
            )
            or (
                evidence.probe_status is ResultProbeStatus.NOT_CONFIRMED
                and evidence.observed_version < expected_version
            )
        ):
            raise NativeBoundStateDenied("M7 final probe lacks authoritative business facts and version")
    if (
        attempt.attempt_id != evidence.attempt_id
        or attempt.task_id != evidence.task_id
        or attempt.step_id != evidence.step_id
        or attempt.contract_id != evidence.contract_id
        or attempt.action_fingerprint != evidence.action_fingerprint
        or attempt.observation_hash != evidence.observation_hash
        or not attempt.idempotency_key
        or _canonical_digest(attempt.idempotency_key) != evidence.idempotency_key_digest
    ):
        raise NativeBoundStateDenied("M7 probe evidence does not match the exact Attempt")
    if (
        permit.permit_id != evidence.permit_id
        or permit.task_id != evidence.task_id
        or permit.step_id != evidence.step_id
        or permit.contract_id != evidence.contract_id
        or permit.action_fingerprint != evidence.action_fingerprint
        or permit.observation_hash != evidence.observation_hash
        or permit.status != "consumed"
    ):
        raise NativeBoundStateDenied("M7 probe evidence does not match the consumed Permit")


def _native_probe_receipt(
    *,
    binding: NativeSkyvernBinding,
    evidence: NativeProbeEvidence,
    attempt_status: ExecutionAttemptStatus,
    task_status: TaskStatus,
    step_status: StepStatus,
    outcome: NativeProbeOutcome,
    duplicate: bool = False,
) -> NativeProbeReceipt:
    return NativeProbeReceipt(
        binding_digest=binding.binding_digest,
        adapter_ref=binding.adapter_ref,
        result_probe_ref=binding.result_probe_ref,
        work_order_id=binding.work_order_id,
        task_id=binding.native_task_id,
        step_id=binding.native_step_id,
        contract_id=binding.contract_id,
        permit_id=evidence.permit_id,
        attempt_id=evidence.attempt_id,
        action_fingerprint=evidence.action_fingerprint,
        observation_hash=evidence.observation_hash,
        idempotency_key_digest=evidence.idempotency_key_digest,
        evidence_digest=_canonical_digest(evidence),
        attempt_status=attempt_status,
        task_status=task_status,
        step_status=step_status,
        outcome=outcome,
        duplicate=duplicate,
    )


def _force_pending_probe(*, task: TaskModel, step: StepModel) -> None:
    task_allowed = {TaskStatus.running.value, TaskStatus.pending_result_probe.value}
    step_allowed = {StepStatus.running.value, StepStatus.pending_result_probe.value}
    if task.status not in task_allowed or step.status not in step_allowed:
        raise NativeBoundStateDenied("M7 native state cannot suspend for a result probe")
    task.status = TaskStatus.pending_result_probe.value
    step.status = StepStatus.pending_result_probe.value


def _step_contract(model: StepModel) -> Step:
    if model.created_at is None or model.modified_at is None:
        raise NativeBoundStateDenied("M7 native Step is missing persistence timestamps")
    return Step(
        task_id=model.task_id,
        step_id=model.step_id,
        organization_id=model.organization_id,
        status=StepStatus(model.status),
        output=None,
        order=model.order,
        retry_index=model.retry_index,
        is_last=bool(model.is_last),
        created_by=model.created_by,
        created_at=model.created_at,
        modified_at=model.modified_at,
        input_token_count=model.input_token_count or 0,
        output_token_count=model.output_token_count or 0,
        reasoning_token_count=model.reasoning_token_count,
        cached_token_count=model.cached_token_count,
        step_cost=float(model.step_cost or 0),
    )


def _canonical_digest(value: Any) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    canonical = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _json_default(value: Any) -> Any:
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, StrEnum):
        return value.value
    raise TypeError(f"Unsupported canonical value: {type(value)!r}")
