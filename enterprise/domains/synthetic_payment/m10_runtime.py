"""Synthetic M10 runtime adapter over the existing M6-M9 governed path."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Protocol

import httpx
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from enterprise.agent.constrained_planner import (
    DeterministicPlanner,
    OpenAICompatiblePlanner,
    PlannerObservation,
    PlannerTransport,
)
from enterprise.agent.interactions import CapabilityRequest, CapabilityRequestKind, EntryMode
from enterprise.auth.schemas import DepartmentRole, UserContext
from enterprise.browser_loop.contracts import (
    ActionDecision,
    ActionKind,
    BrowserAction,
    BrowserLoopConfig,
    BrowserLoopRunContext,
    BrowserLoopStatus,
    BrowserObservation,
    DecisionKind,
    ModelInput,
    PolicyAuthorization,
    PolicyDisposition,
    VerificationDisposition,
    VerificationRequest,
    VerificationResult,
)
from enterprise.browser_loop.integrations import SqlAlchemyBrowserLoopEventSink
from enterprise.browser_loop.loop import AgentPactBrowserLoop
from enterprise.browser_loop.persisted_executor import PersistedBrowserExecutor
from enterprise.browser_loop.runtime import PlaywrightPageRuntime
from enterprise.governance.admission import (
    AdmissionAuditRecord,
    GovernedTaskDraft,
    TaskAdmissionBundle,
    canonical_task_admission_payload,
)
from enterprise.governance.audit import observation_hash
from enterprise.governance.capabilities import CapabilityDataScope
from enterprise.governance.classification import action_fingerprint
from enterprise.governance.contracts import (
    ActionIntent,
    DecisionOutcome,
    ExecutionAttemptStatus,
    ExecutionAuthorization,
    ExecutionEffect,
    PolicyDecision,
)
from enterprise.governance.creation_snapshot import TaskCreationPath, TrustedTaskCreationSnapshot
from enterprise.governance.execution_profiles import ExecutionMechanism, ExecutionProfile
from enterprise.governance.models import (
    ExecutionAttemptModel,
    ExecutionPermitModel,
    GovernedTaskAdmissionModel,
)
from enterprise.governance.pack_runtime import (
    ApprovalHandler,
    ApprovalRequestSpecification,
    ExecutionCheckpoint,
    ModelSafeRuntimeProjection,
    PackAdmissionResult,
    PackAdvanceResult,
    PackAdvanceStatus,
    PackLifecycleError,
    PackProbeResult,
    PackProbeStatus,
    PackRunRequest,
    PackRunRestoreRequest,
    PackRuntimeBinding,
    PreparedRunReference,
)
from enterprise.governance.permit_service import issue_permit
from enterprise.governance.result_probes import ResultProbeEvidence
from enterprise.governance.resume_execution_service import (
    claim_resuming_task_for_execution,
    suspend_unknown_execution_for_probe,
)
from skyvern.forge import app as forge_app
from skyvern.forge.sdk.db.models import StepModel, TaskModel
from skyvern.forge.sdk.models import StepStatus
from skyvern.forge.sdk.schemas.tasks import TaskStatus

from .constants import (
    BUSINESS_LINE_ID,
    CAPABILITY_ID,
    PACK_ID,
    PACK_VERSION,
    PAYMENTS_DEPARTMENT_ID,
    POLICY_VERSION,
    RESULT_PROBE_REF,
)
from .m6_runtime import (
    SYNTHETIC_RUNTIME_CONTRACT,
    SyntheticM6TrustedContext,
    bind_compilation_for_execution,
    build_synthetic_conformance_attestation,
    build_synthetic_installation,
    compile_synthetic_request,
)
from .m7_runtime import (
    NativeSkyvernBinding,
    NativeSkyvernWorkOrderAdapter,
    SqlAlchemyNativePublicationRepository,
    SyntheticNativeActionContextResolver,
    build_native_probe_evidence,
)
from .m8_runtime import (
    GovernedPlanCheckpoint,
    GovernedPlanCoordinator,
    GovernedPlanStepRef,
    NativeWorkOutcome,
    NativeWorkOutcomeKind,
    PlanJournalTransition,
    PlanRunState,
    PlanStepState,
    SqlAlchemyGovernedPlanJournal,
    SyntheticM8Compilation,
    _authority_digests,
    _complete_active,
    build_m8_admission_bundle,
    build_synthetic_m8_compilation,
)
from .m9_runtime import (
    M9PlanInput,
    M9PlannerDisposition,
    M9PlannerEngine,
    M9PlannerProvider,
    M9StepRole,
    OpenAICompatibleM9Provider,
    PlanProposal,
    RecordedM9Provider,
    build_m9_plan_input,
    compile_m9_plan,
)
from .models import PaymentFacts
from .policy import require_approval_decision

M10_ADAPTER_ID = "synthetic.payment.agent-run-runtime.v1"
M9ProviderFactory = Callable[[M9PlanInput], M9PlannerProvider]


class M10PlanningError(PackLifecycleError, ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def build_m10_provider_factory(
    provider_mode: Literal["recorded", "live"],
    *,
    endpoint: str | None = None,
    model: str | None = None,
    api_key_env: str = "OPENAI_COMPATIBLE_API_KEY",
    transport: PlannerTransport | None = None,
) -> M9ProviderFactory:
    """Build one explicit provider composition; live mode never falls back."""

    if provider_mode == "recorded":
        return _recorded_provider
    if provider_mode != "live":
        raise ValueError("Agent Run provider mode must be recorded or live")
    if not endpoint or not model or not api_key_env or not os.environ.get(api_key_env):
        raise ValueError("Live Agent Run provider configuration is incomplete")
    planner = OpenAICompatiblePlanner(
        endpoint=endpoint,
        model=model,
        api_key_env=api_key_env,
        transport=transport,
    )
    return lambda _planner_input: OpenAICompatibleM9Provider(planner)


def _recorded_provider(planner_input: M9PlanInput) -> M9PlannerProvider:
    return RecordedM9Provider(
        [
            {
                "capability_id": CAPABILITY_ID,
                "input_slots": [item.name for item in planner_input.input_slots],
                "step_roles": ["precheck", "submit", "confirm"],
            }
        ]
    )


def derive_agent_run_id(*, tenant_id: str, request_id: str) -> str:
    return "run_" + _digest(["agentpact-agent-run/v1", tenant_id, request_id])


def derive_admission_id(*, tenant_id: str, request_id: str) -> str:
    return "admission_m10_" + _digest(["agentpact-agent-run-admission/v1", tenant_id, request_id])


class SyntheticM10PreparedRun(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid", frozen=True)

    run_id: str
    intent_digest: str
    business_inputs_digest: str
    business_inputs: dict[str, Any]
    compilation: SyntheticM8Compilation
    admission_bundle: TaskAdmissionBundle
    target_url: str


class M10ApprovalPause(RuntimeError):
    """Private Pack signal converted to a typed approval result at the adapter."""

    def __init__(
        self,
        *,
        intent: ActionIntent,
        decision: PolicyDecision,
        observation_hash: str,
        binding_digest: str,
    ) -> None:
        self.intent = intent
        self.decision = decision
        self.observation_hash = observation_hash
        self.binding_digest = binding_digest
        super().__init__("M10 approval pause")


class SyntheticM10NativeDriver(Protocol):
    async def execute(
        self,
        *,
        compilation: object,
        binding: NativeSkyvernBinding,
        resume: bool = False,
    ) -> NativeWorkOutcome: ...

    async def probe(
        self,
        *,
        compilation: object,
        binding: NativeSkyvernBinding,
        active_step: GovernedPlanStepRef,
    ) -> NativeWorkOutcome: ...


class _M10ApprovalEvaluator:
    def __init__(self, *, approved: bool) -> None:
        self._approved = approved

    async def evaluate(self, *, intent: ActionIntent, observed_business_inputs: dict[str, Any]) -> PolicyDecision:
        facts = PaymentFacts.model_validate(observed_business_inputs)
        if not self._approved:
            return require_approval_decision(intent, facts)
        return PolicyDecision(
            decision_id=f"m10-approved:{intent.action_fingerprint}",
            intent_id=intent.intent_id,
            outcome=DecisionOutcome.ALLOW,
            risk_level="high",
            reasons=["Authenticated M10 approval accepted fresh synthetic evidence"],
            matched_rules=["synthetic.payment.separation-of-duties", "synthetic.payment.approved"],
            policy_version=POLICY_VERSION,
        )


class _M10BrowserInputObserver:
    def __init__(self, page: Any, *, object_version: int) -> None:
        self._page = page
        self._object_version = object_version

    async def observe(self, *, scraped_page: Any) -> dict[str, object]:
        if scraped_page.url != self._page.url:
            raise ValueError("M10 observer rejected a changed browser target")
        return {
            "payment_id": await self._page.input_value("#paymentId"),
            "beneficiary_id": await self._page.input_value("#beneficiary"),
            "amount": await self._page.input_value("#amount"),
            "currency": await self._page.input_value("#currency"),
            "reference": await self._page.input_value("#reference"),
            "object_version": self._object_version,
        }


class _M10FreshPermitAuthorizer:
    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[Any]],
        *,
        payment_id: str,
        clock: Callable[[], datetime],
        hmac_secret: str,
    ) -> None:
        self._session_factory = session_factory
        self._payment_id = payment_id
        self._clock = clock
        self._hmac_secret = hmac_secret

    async def authorize(
        self,
        *,
        task: Any,
        step: Any,
        scraped_page: Any,
        action: Any,
        binding: NativeSkyvernBinding,
        execution_binding: Any,
    ) -> tuple[ExecutionAuthorization, ExecutionProfile]:
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
        profile = ExecutionProfile(
            mechanism=ExecutionMechanism.LOCATOR,
            fallback_rank=0,
            evidence_refs=[f"agentpact://m10-native/{binding.binding_digest}"],
        )
        decision = PolicyDecision(
            decision_id=f"m10-permit:{fingerprint}",
            intent_id=f"m10-approved:{fingerprint}",
            outcome=DecisionOutcome.ALLOW,
            risk_level="high",
            reasons=["Fresh M10 policy evaluation passed after authenticated approval"],
            matched_rules=["synthetic.payment.separation-of-duties", "synthetic.payment.approved"],
            policy_version=POLICY_VERSION,
        )
        now = self._clock()
        expires_at = min(now + timedelta(minutes=5), execution_binding.expires_at)
        async with self._session_factory() as session:
            async with session.begin():
                permit = await issue_permit(
                    db_session=session,
                    task_id=task.task_id,
                    step_id=step.step_id,
                    contract_id=binding.contract_id,
                    action_fingerprint=fingerprint,
                    observation_hash=observed_hash,
                    decision=decision,
                    effect=ExecutionEffect.EXTERNAL_WRITE,
                    execution_profile=profile,
                    ttl_seconds=max(1, int((expires_at - now).total_seconds())),
                )
        return (
            ExecutionAuthorization(
                permit_id=permit.permit_id,
                action_fingerprint=fingerprint,
                observation_hash=observed_hash,
                idempotency_key=f"synthetic:{self._payment_id}",
                effect=ExecutionEffect.EXTERNAL_WRITE,
            ),
            profile,
        )

    async def authorize_agentpact(
        self,
        *,
        task_id: str,
        step_id: str,
        action: BrowserAction,
        observation: BrowserObservation,
        binding: NativeSkyvernBinding,
        execution_binding: Any,
    ) -> tuple[ExecutionAuthorization, ExecutionProfile]:
        fingerprint = action_fingerprint(
            task_id=task_id,
            step_id=step_id,
            action_payload=action.model_dump(mode="json", exclude_none=True),
            observation_hash=observation.observation_id,
            secret=self._hmac_secret,
        )
        profile = ExecutionProfile(
            mechanism=ExecutionMechanism.LOCATOR,
            fallback_rank=0,
            evidence_refs=[f"agentpact://m10-native/{binding.binding_digest}"],
        )
        decision = PolicyDecision(
            decision_id=f"m10-permit:{fingerprint}",
            intent_id=f"m10-approved:{fingerprint}",
            outcome=DecisionOutcome.ALLOW,
            risk_level="high",
            reasons=["Fresh M10 policy evaluation passed after authenticated approval"],
            matched_rules=["synthetic.payment.separation-of-duties", "synthetic.payment.approved"],
            policy_version=POLICY_VERSION,
        )
        now = self._clock()
        expires_at = min(now + timedelta(minutes=5), execution_binding.expires_at)
        async with self._session_factory() as session:
            async with session.begin():
                permit = await issue_permit(
                    db_session=session,
                    task_id=task_id,
                    step_id=step_id,
                    contract_id=binding.contract_id,
                    action_fingerprint=fingerprint,
                    observation_hash=observation.observation_id,
                    decision=decision,
                    effect=ExecutionEffect.EXTERNAL_WRITE,
                    execution_profile=profile,
                    ttl_seconds=max(1, int((expires_at - now).total_seconds())),
                )
        return (
            ExecutionAuthorization(
                permit_id=permit.permit_id,
                action_fingerprint=fingerprint,
                observation_hash=observation.observation_id,
                idempotency_key=f"synthetic:{self._payment_id}",
                effect=ExecutionEffect.EXTERNAL_WRITE,
            ),
            profile,
        )


class _UnavailableBrowserActionModel:
    async def decide(self, _model_input: ModelInput) -> ActionDecision:
        raise RuntimeError("Synthetic read steps require a deterministic Domain Pack decision")


class _SyntheticReadPolicy:
    async def prepare_model_input(
        self,
        *,
        run: BrowserLoopRunContext,
        observation: Any,
    ) -> ModelInput:
        return ModelInput(
            observation_id=observation.observation_id,
            goal=run.goal,
            url=observation.url,
            dom=observation.model_dom,
            screenshots=observation.screenshots,
            allowed_action_kinds=(),
        )

    async def authorize_action(self, **_kwargs: Any) -> PolicyAuthorization:
        return PolicyAuthorization(
            disposition=PolicyDisposition.DENY,
            reason_code="READ_STEP_ACTION_DENIED",
        )


class _SyntheticReadActions:
    binding = PackRuntimeBinding(
        pack_id=PACK_ID,
        pack_version=PACK_VERSION,
        capability_ids=(CAPABILITY_ID,),
        adapter_id="synthetic.payment.browser-read.v1",
    )

    async def decide(
        self,
        *,
        run: BrowserLoopRunContext,
        observation: Any,
    ) -> ActionDecision:
        role = run.metadata.get("step_role")
        if role not in {"precheck", "confirm"}:
            return ActionDecision(
                kind=DecisionKind.FAILURE,
                observation_id=observation.observation_id,
                reason_code="UNSUPPORTED_READ_STEP",
            )
        return ActionDecision(
            kind=DecisionKind.SUCCESS,
            observation_id=observation.observation_id,
            reason_code="DOMAIN_PAGE_READY",
        )


class _SyntheticReadVerifier:
    _REQUIRED_CONTROLS = {
        "Create synthetic payment challenge",
        "Execute synthetic payment once",
    }

    def __init__(self, *, target_url: str) -> None:
        self._target_url = target_url.rstrip("/")

    async def verify(self, request: VerificationRequest) -> VerificationResult:
        names = {element.name for element in request.after.elements if element.name}
        valid = (
            request.source.value == "domain_pack"
            and request.decision.kind is DecisionKind.SUCCESS
            and request.after.url.rstrip("/") == self._target_url
            and self._REQUIRED_CONTROLS <= names
        )
        return VerificationResult(
            disposition=(VerificationDisposition.SUCCEEDED if valid else VerificationDisposition.FAILED),
            reason_code="DOMAIN_PAGE_VERIFIED" if valid else "DOMAIN_PAGE_MISMATCH",
            evidence_refs=("agentpact://synthetic.payment/page-contract/v1",) if valid else (),
        )


class TrustedSyntheticM10Driver:
    """Trusted recorded-mode native composition used by the mounted application."""

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[Any]],
        *,
        target_url: str,
        hmac_secret: str | None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._target_url = target_url.rstrip("/")
        self._hmac_secret = hmac_secret or ""
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def execute(
        self,
        *,
        compilation: Any,
        binding: NativeSkyvernBinding,
        resume: bool = False,
    ) -> NativeWorkOutcome:
        role = compilation.work_order.navigation_goal.split(" governed ", 1)[1].split(" for ", 1)[0]
        if role != "submit":
            await self._execute_read_step(compilation=compilation, binding=binding, role=role)
            await self._complete_read(binding)
            return NativeWorkOutcome(kind=NativeWorkOutcomeKind.COMPLETED)
        if not self._hmac_secret:
            raise ValueError("M10 native governance HMAC secret is not configured")
        if resume:
            return await self._execute_after_approval(compilation=compilation, binding=binding)
        return await self._request_approval(compilation=compilation, binding=binding)

    async def _execute_read_step(
        self,
        *,
        compilation: Any,
        binding: NativeSkyvernBinding,
        role: str,
    ) -> None:
        _task, _step, page, _browser_state = await self._browser_context(binding)
        loop = AgentPactBrowserLoop(
            runtime=PlaywrightPageRuntime(page, capture_screenshot=False, clock=self._clock),
            model=_UnavailableBrowserActionModel(),
            policy=_SyntheticReadPolicy(),
            verifier=_SyntheticReadVerifier(target_url=self._target_url),
            event_sink=SqlAlchemyBrowserLoopEventSink(
                self._session_factory,
                organization_id=binding.organization_id,
                contract_id=binding.contract_id,
                policy_version=POLICY_VERSION,
            ),
            integrity_secret=self._hmac_secret,
            domain_actions=_SyntheticReadActions(),
            config=BrowserLoopConfig(max_iterations=1, max_retries=0),
            clock=self._clock,
        )
        report = await loop.run(
            BrowserLoopRunContext(
                run_id=compilation.business_plan.task_id,
                task_id=binding.native_task_id,
                step_id=binding.native_step_id,
                goal=compilation.work_order.navigation_goal,
                pack_id=PACK_ID,
                pack_version=PACK_VERSION,
                capability_id=CAPABILITY_ID,
                contract_id=binding.contract_id,
                metadata={"step_role": role},
            )
        )
        if report.status is not BrowserLoopStatus.SUCCEEDED:
            raise ValueError(f"M10 read step browser loop failed: {report.reason_code}")

    async def probe(
        self,
        *,
        compilation: Any,
        binding: NativeSkyvernBinding,
        active_step: GovernedPlanStepRef,
    ) -> NativeWorkOutcome:
        if not self._hmac_secret:
            raise ValueError("M10 result-probe HMAC secret is not configured")
        permit, attempt = await self._probe_authority(active_step)
        authorization = ExecutionAuthorization(
            permit_id=permit.permit_id,
            action_fingerprint=permit.action_fingerprint,
            observation_hash=permit.observation_hash,
            idempotency_key=attempt.idempotency_key,
            effect=ExecutionEffect.EXTERNAL_WRITE,
        )
        task, _step, page, _browser_state = await self._browser_context(binding)
        challenge_id = (await page.text_content("#challenge") or "").strip()
        payment_id = str(compilation.business_plan.steps[0].inputs["payment_id"])
        if not challenge_id or challenge_id == "-":
            raise ValueError("M10 probe cannot resolve the exact synthetic challenge")
        async with httpx.AsyncClient(base_url=self._target_url, timeout=10.0) as client:
            cleared = await client.post(f"/api/payments/{payment_id}/clear-probe-fault", json={})
            cleared.raise_for_status()
            response = await client.post(f"/api/challenges/{challenge_id}/probe", json={})
            response.raise_for_status()
        payload = response.json()
        resolver = self._resolver(
            compilation=compilation,
            binding=binding,
            page=page,
            approved=True,
        )
        receipt = await resolver.reconcile_probe(
            evidence=build_native_probe_evidence(
                binding=binding,
                authorization=authorization,
                attempt_id=attempt.attempt_id,
                result_probe=ResultProbeEvidence.model_validate(payload["result_probe"]),
                hmac_secret=self._hmac_secret,
            )
        )
        if receipt.attempt_status is not ExecutionAttemptStatus.CONFIRMED:
            raise ValueError("M10 authoritative probe did not confirm the exact Attempt")
        del task
        return NativeWorkOutcome(
            kind=NativeWorkOutcomeKind.COMPLETED,
            permit_id=authorization.permit_id,
            attempt_id=attempt.attempt_id,
            probe_ref=receipt.result_probe_ref,
        )

    async def _request_approval(self, *, compilation: Any, binding: NativeSkyvernBinding) -> NativeWorkOutcome:
        task, step, page, _browser_state = await self._browser_context(binding)
        await self._prepare_synthetic_challenge(page=page, compilation=compilation)
        await self._mark_step_running(binding)
        task, step = await self._native_rows(binding)
        _runtime, observation, _action, _execution_binding, intent = await self._fresh_submit_context(
            compilation=compilation,
            binding=binding,
            page=page,
            task=task,
            step=step,
        )

        observed_inputs = await self._observed_business_inputs(page=page, compilation=compilation)
        decision = await _M10ApprovalEvaluator(approved=False).evaluate(
            intent=intent,
            observed_business_inputs=observed_inputs,
        )
        if decision.outcome is not DecisionOutcome.REQUIRE_APPROVAL:
            raise ValueError("M10 initial native resolution did not produce an approval pause")
        raise M10ApprovalPause(
            intent=intent,
            decision=decision,
            observation_hash=observation.observation_id,
            binding_digest=binding.binding_digest,
        )

    async def _execute_after_approval(
        self,
        *,
        compilation: Any,
        binding: NativeSkyvernBinding,
    ) -> NativeWorkOutcome:
        async with self._session_factory() as session:
            async with session.begin():
                await claim_resuming_task_for_execution(
                    db_session=session,
                    task_id=binding.native_task_id,
                    step_id=binding.native_step_id,
                    organization_id=binding.organization_id,
                )
        await self._mark_step_running(binding)
        task, step, page, _browser_state = await self._browser_context(binding)
        await page.evaluate("document.body.dataset.m10FreshObservation = 'approved'")
        runtime = PlaywrightPageRuntime(page, capture_screenshot=False, clock=self._clock)
        persisted_runtime = PersistedBrowserExecutor(
            self._session_factory,
            runtime,
            result_probe_ref=binding.result_probe_ref,
            clock=self._clock,
        )
        loop = AgentPactBrowserLoop(
            runtime=persisted_runtime,
            model=_UnavailableBrowserActionModel(),
            policy=_SyntheticSubmitPolicy(
                page=page,
                compilation=compilation,
                binding=binding,
                session_factory=self._session_factory,
                hmac_secret=self._hmac_secret,
                clock=self._clock,
            ),
            verifier=_SyntheticSubmitVerifier(),
            event_sink=SqlAlchemyBrowserLoopEventSink(
                self._session_factory,
                organization_id=binding.organization_id,
                contract_id=binding.contract_id,
                policy_version=POLICY_VERSION,
            ),
            integrity_secret=self._hmac_secret,
            domain_actions=_SyntheticSubmitActions(action_builder=self._submit_action),
            config=BrowserLoopConfig(max_iterations=1, max_retries=0),
            clock=self._clock,
        )
        report = await loop.run(
            BrowserLoopRunContext(
                run_id=compilation.business_plan.task_id,
                task_id=binding.native_task_id,
                step_id=binding.native_step_id,
                goal=compilation.work_order.navigation_goal,
                pack_id=PACK_ID,
                pack_version=PACK_VERSION,
                capability_id=CAPABILITY_ID,
                contract_id=binding.contract_id,
                metadata={"step_role": "submit"},
            )
        )
        checkpoint = report.execution_checkpoint
        if report.status is not BrowserLoopStatus.UNKNOWN or checkpoint is None:
            raise ValueError("M10 unified browser loop did not enter the exact UNKNOWN probe boundary")
        async with self._session_factory() as session:
            async with session.begin():
                await suspend_unknown_execution_for_probe(
                    db_session=session,
                    organization_id=binding.organization_id,
                    checkpoint=checkpoint,
                )
        return NativeWorkOutcome(
            kind=NativeWorkOutcomeKind.PROBE_BLOCKED,
            permit_id=checkpoint.permit_id,
            attempt_id=checkpoint.attempt_id,
            probe_ref=checkpoint.result_probe_ref,
        )

    async def _fresh_submit_context(
        self,
        *,
        compilation: Any,
        binding: NativeSkyvernBinding,
        page: Any,
        task: Any,
        step: Any,
    ) -> tuple[PlaywrightPageRuntime, BrowserObservation, BrowserAction, Any, ActionIntent]:
        runtime = PlaywrightPageRuntime(page, capture_screenshot=False, clock=self._clock)
        raw = await runtime.observe()
        observed_hash = observation_hash(url=raw.url, html=raw.page_html, secret=self._hmac_secret)
        observation = BrowserObservation(
            observation_id=observed_hash,
            snapshot_hash=hashlib.sha256(f"{raw.url}\n{raw.page_html}".encode("utf-8")).hexdigest(),
            sequence=1,
            url=raw.url,
            title=raw.title,
            model_dom=raw.model_dom,
            screenshots=raw.screenshots,
            elements=raw.elements,
            captured_at=raw.captured_at,
        )
        action = self._submit_action(observation)
        observed_inputs = await self._observed_business_inputs(page=page, compilation=compilation)
        execution_binding = bind_compilation_for_execution(
            compilation,
            observed_business_inputs=observed_inputs,
            work_order_id=binding.work_order_id,
            now=self._clock(),
        )
        if (
            execution_binding.task_id != binding.native_task_id
            or execution_binding.contract_id != binding.contract_id
            or execution_binding.grant_id != binding.grant_id
            or execution_binding.result_probe_ref != binding.result_probe_ref
            or task.task_id != binding.native_task_id
            or step.step_id != binding.native_step_id
        ):
            raise ValueError("M10 fresh AgentPact observation does not match native authority")
        fingerprint = action_fingerprint(
            task_id=task.task_id,
            step_id=step.step_id,
            action_payload=action.model_dump(mode="json", exclude_none=True),
            observation_hash=observation.observation_id,
            secret=self._hmac_secret,
        )
        intent = ActionIntent(
            intent_id=f"m10-intent:{fingerprint}",
            task_id=task.task_id,
            step_id=step.step_id,
            action_fingerprint=fingerprint,
            observation_id=observation.observation_id,
            operation="submit",
            effect=ExecutionEffect.EXTERNAL_WRITE,
            target={"kind": "synthetic-payment-submit"},
            confidence=1.0,
            evidence=["fresh-agentpact-observation"],
        )
        return runtime, observation, action, execution_binding, intent

    async def _observed_business_inputs(self, *, page: Any, compilation: Any) -> dict[str, object]:
        facts = PaymentFacts.model_validate(compilation.business_plan.steps[0].inputs)
        return {
            "payment_id": await page.input_value("#paymentId"),
            "beneficiary_id": await page.input_value("#beneficiary"),
            "amount": await page.input_value("#amount"),
            "currency": await page.input_value("#currency"),
            "reference": await page.input_value("#reference"),
            "object_version": facts.object_version,
        }

    def _resolver(
        self,
        *,
        compilation: Any,
        binding: NativeSkyvernBinding,
        page: Any,
        approved: bool,
    ) -> SyntheticNativeActionContextResolver:
        facts = PaymentFacts.model_validate(compilation.business_plan.steps[0].inputs)
        return SyntheticNativeActionContextResolver(
            self._session_factory,
            binding=binding,
            compilation=compilation,
            authorizer=_M10FreshPermitAuthorizer(
                self._session_factory,
                payment_id=facts.payment_id,
                clock=self._clock,
                hmac_secret=self._hmac_secret,
            ),
            business_input_observer=_M10BrowserInputObserver(page, object_version=facts.object_version),
            approval_evaluator=_M10ApprovalEvaluator(approved=approved),
            hmac_secret=self._hmac_secret,
            clock=self._clock,
        )

    async def _browser_context(self, binding: NativeSkyvernBinding) -> tuple[Any, Any, Any, Any]:
        task, step = await self._native_rows(binding)
        browser_state = forge_app.BROWSER_MANAGER.get_for_task(task.task_id, task.workflow_run_id)
        if browser_state is None:
            browser_state = await forge_app.BROWSER_MANAGER.get_or_create_for_task(task=task)
        page = await browser_state.get_working_page()
        if page is None:
            raise ValueError("M10 native browser has no working page")
        return task, step, page, browser_state

    async def _native_rows(self, binding: NativeSkyvernBinding) -> tuple[Any, Any]:
        task = await forge_app.DATABASE.get_task(binding.native_task_id, binding.organization_id)
        step = await forge_app.DATABASE.get_step(binding.native_step_id, binding.organization_id)
        if task is None or step is None:
            raise ValueError("M10 native Task/Step pair is missing")
        return task, step

    async def _mark_step_running(self, binding: NativeSkyvernBinding) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                step = (
                    await session.scalars(
                        select(StepModel).where(StepModel.step_id == binding.native_step_id).with_for_update()
                    )
                ).one()
                if step.status not in {
                    StepStatus.created.value,
                    StepStatus.resuming.value,
                    StepStatus.running.value,
                }:
                    raise ValueError("M10 native Step is not eligible for approval observation")
                step.status = StepStatus.running.value

    async def _complete_read(self, binding: NativeSkyvernBinding) -> None:
        now = self._clock()
        async with self._session_factory() as session:
            async with session.begin():
                task = (
                    await session.scalars(
                        select(TaskModel).where(TaskModel.task_id == binding.native_task_id).with_for_update()
                    )
                ).one()
                step = (
                    await session.scalars(
                        select(StepModel).where(StepModel.step_id == binding.native_step_id).with_for_update()
                    )
                ).one()
                if task.status != TaskStatus.running.value or step.status not in {
                    StepStatus.created.value,
                    StepStatus.running.value,
                }:
                    raise ValueError("M10 read-only native pair is not runnable")
                task.status = TaskStatus.completed.value
                task.finished_at = now
                step.status = StepStatus.completed.value
                step.finished_at = now

    async def _prepare_synthetic_challenge(self, *, page: Any, compilation: Any) -> None:
        facts = PaymentFacts.model_validate(compilation.business_plan.steps[0].inputs)
        await page.fill("#paymentId", facts.payment_id)
        await page.fill("#beneficiary", facts.beneficiary_id)
        await page.fill("#amount", str(facts.amount))
        await page.select_option("#currency", facts.currency)
        await page.fill("#reference", facts.reference)
        await page.click("#create")
        await page.wait_for_function(
            "document.getElementById('state').textContent === 'pending_approval'",
            timeout=10_000,
        )
        await page.click("#approve")
        await page.wait_for_function(
            "document.getElementById('state').textContent === 'ready'",
            timeout=10_000,
        )
        await page.select_option("#fault", "commit_then_inconclusive")

    def _submit_action(self, observation: BrowserObservation) -> BrowserAction:
        matches = [
            element
            for element in observation.elements
            if element.name == "Execute synthetic payment once" and element.enabled
        ]
        if len(matches) != 1:
            raise ValueError("Fresh AgentPact observation lacks one executable synthetic submit target")
        return BrowserAction(
            kind=ActionKind.CLICK,
            operation="submit",
            element_id=matches[0].element_id,
        )

    async def _probe_authority(self, active_step: GovernedPlanStepRef) -> tuple[Any, Any]:
        async with self._session_factory() as session:
            permit = (
                await session.scalars(
                    select(ExecutionPermitModel).where(ExecutionPermitModel.permit_id == active_step.permit_id)
                )
            ).one()
            attempt = (
                await session.scalars(
                    select(ExecutionAttemptModel).where(ExecutionAttemptModel.attempt_id == active_step.attempt_id)
                )
            ).one()
            if (
                permit.task_id != active_step.native_task_id
                or permit.step_id != active_step.native_step_id
                or permit.status != "consumed"
                or attempt.task_id != active_step.native_task_id
                or attempt.step_id != active_step.native_step_id
                or attempt.status != ExecutionAttemptStatus.UNKNOWN.value
                or attempt.action_fingerprint != permit.action_fingerprint
                or attempt.observation_hash != permit.observation_hash
                or not attempt.idempotency_key
            ):
                raise ValueError("M10 probe authority does not match the exact UNKNOWN boundary")
            session.expunge(permit)
            session.expunge(attempt)
        return permit, attempt


class _SyntheticSubmitActions:
    """Deterministic Pack action proposal used by the unified browser loop."""

    def __init__(self, *, action_builder: Callable[[BrowserObservation], BrowserAction]) -> None:
        self._action_builder = action_builder
        self.binding = PackRuntimeBinding(
            pack_id=PACK_ID,
            pack_version=PACK_VERSION,
            capability_ids=(CAPABILITY_ID,),
            adapter_id="synthetic.payment.browser-submit.v1",
        )

    async def decide(
        self,
        *,
        run: BrowserLoopRunContext,
        observation: BrowserObservation,
    ) -> ActionDecision | None:
        return ActionDecision(
            kind=DecisionKind.ACTION,
            observation_id=observation.observation_id,
            action=self._action_builder(observation),
            reason_code="PACK_SUBMIT_ACTION_PROPOSED",
        )


class _SyntheticSubmitPolicy:
    """Re-evaluate current facts and issue fresh authority inside the loop."""

    def __init__(
        self,
        *,
        page: Any,
        compilation: Any,
        binding: NativeSkyvernBinding,
        session_factory: Callable[[], AbstractAsyncContextManager[Any]],
        hmac_secret: str,
        clock: Callable[[], datetime],
    ) -> None:
        self._page = page
        self._compilation = compilation
        self._binding = binding
        self._session_factory = session_factory
        self._hmac_secret = hmac_secret
        self._clock = clock

    async def prepare_model_input(self, *, run: BrowserLoopRunContext, observation: BrowserObservation) -> ModelInput:
        return ModelInput(
            observation_id=observation.observation_id,
            goal=run.goal,
            url=observation.url,
            dom=observation.model_dom,
            screenshots=observation.screenshots,
            allowed_action_kinds=(),
        )

    async def authorize_action(
        self,
        *,
        run: BrowserLoopRunContext,
        observation: BrowserObservation,
        action: BrowserAction,
        action_fingerprint: str,
    ) -> PolicyAuthorization:
        facts = PaymentFacts.model_validate(
            {
                "payment_id": await self._page.input_value("#paymentId"),
                "beneficiary_id": await self._page.input_value("#beneficiary"),
                "amount": await self._page.input_value("#amount"),
                "currency": await self._page.input_value("#currency"),
                "reference": await self._page.input_value("#reference"),
                "object_version": self._compilation.business_plan.steps[0].inputs["object_version"],
            }
        )
        intent = ActionIntent(
            intent_id=f"synthetic-submit-intent:{action_fingerprint}",
            task_id=run.task_id,
            step_id=run.step_id,
            action_fingerprint=action_fingerprint,
            observation_id=observation.observation_id,
            operation=CAPABILITY_ID,
            effect=ExecutionEffect.EXTERNAL_WRITE,
            target={"kind": "synthetic-payment-submit"},
            confidence=1.0,
            evidence=["fresh-agentpact-observation"],
        )
        decision = await _M10ApprovalEvaluator(approved=True).evaluate(
            intent=intent,
            observed_business_inputs=facts.model_dump(mode="json"),
        )
        if decision.outcome is not DecisionOutcome.ALLOW:
            return PolicyAuthorization(
                disposition=PolicyDisposition.DENY,
                reason_code="FRESH_BUSINESS_FACTS_REJECTED",
            )
        observed_binding = bind_compilation_for_execution(
            self._compilation,
            observed_business_inputs=facts.model_dump(mode="json"),
            work_order_id=self._binding.work_order_id,
            now=self._clock(),
        )
        authorization, profile = await _M10FreshPermitAuthorizer(
            self._session_factory,
            payment_id=facts.payment_id,
            clock=self._clock,
            hmac_secret=self._hmac_secret,
        ).authorize_agentpact(
            task_id=run.task_id,
            step_id=run.step_id,
            action=action,
            observation=observation,
            binding=self._binding,
            execution_binding=observed_binding,
        )
        return PolicyAuthorization(
            disposition=PolicyDisposition.ALLOW,
            reason_code="FRESH_BUSINESS_FACTS_ALLOWED",
            authorization=authorization,
            execution_profile=profile,
        )


class _SyntheticSubmitVerifier:
    async def verify(self, request: VerificationRequest) -> VerificationResult:
        if request.action_result is not None and request.action_result.pending_result_probe:
            return VerificationResult(
                disposition=VerificationDisposition.UNKNOWN,
                reason_code="RESULT_PROBE_DEFERRED",
                evidence_refs=(RESULT_PROBE_REF,),
            )
        return VerificationResult(
            disposition=VerificationDisposition.UNKNOWN,
            reason_code="RESULT_PROBE_REQUIRED",
            evidence_refs=(RESULT_PROBE_REF,),
        )


class M10PauseHandler(Protocol):
    async def __call__(
        self,
        *,
        prepared: SyntheticM10PreparedRun,
        checkpoint: object,
        binding: NativeSkyvernBinding,
        pause: M10ApprovalPause,
        operation_key: str,
    ) -> object: ...


class SyntheticPaymentRuntimeAdapter:
    """The one explicitly composed synthetic implementation of the generic adapter."""

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[Any]],
        *,
        driver: SyntheticM10NativeDriver,
        provider_mode: Literal["recorded", "live"] = "recorded",
        provider_factory: M9ProviderFactory | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._driver = driver
        self._provider_mode = provider_mode
        self._provider_factory = provider_factory or build_m10_provider_factory(provider_mode)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    @property
    def provider_mode(self) -> Literal["recorded", "live"]:
        return self._provider_mode

    @property
    def binding(self) -> PackRuntimeBinding:
        return PackRuntimeBinding(
            pack_id=SYNTHETIC_RUNTIME_CONTRACT.pack_id,
            pack_version=SYNTHETIC_RUNTIME_CONTRACT.pack_version,
            capability_ids=SYNTHETIC_RUNTIME_CONTRACT.capability_ids,
            adapter_id=M10_ADAPTER_ID,
        )

    def model_safe_projection(self, authority: object) -> ModelSafeRuntimeProjection:
        compilation = authority
        capabilities = tuple(item.capability_id for item in compilation.projection)
        slots = tuple(item.name for item in build_m9_plan_input(compilation).input_slots)
        return ModelSafeRuntimeProjection(
            pack_id=PACK_ID,
            pack_version=PACK_VERSION,
            capability_ids=capabilities,
            input_slot_names=slots,
        )

    def prepare_run(
        self,
        request: PackRunRequest | None = None,
        **trusted_inputs: Any,
    ) -> PreparedRunReference | SyntheticM10PreparedRun:
        """Prepare a typed reference; legacy keyword calls remain an API-edge shim."""

        if request is None:
            return self._prepare_run_legacy(**trusted_inputs)
        run = self._prepare_run_legacy(
            user=request.principal,
            tenant_id=request.tenant_id,
            request_id=request.request_id,
            intent_digest=request.intent_digest,
            business_inputs=request.business_inputs,
            target_url=request.target_url,
            now=request.now,
        )
        return self._reference(run)

    def _prepare_run_legacy(self, **trusted_inputs: Any) -> SyntheticM10PreparedRun:
        user = UserContext.model_validate(trusted_inputs["user"])
        request_id = str(trusted_inputs["request_id"])
        intent_digest = str(trusted_inputs["intent_digest"])
        facts = PaymentFacts.model_validate(trusted_inputs["business_inputs"])
        target_url = str(trusted_inputs["target_url"])
        now = trusted_inputs.get("now") or self._clock()
        if user.org_id != trusted_inputs.get("tenant_id"):
            raise ValueError("M10 authenticated tenant does not match the trusted adapter context")
        run_id = derive_agent_run_id(tenant_id=user.org_id, request_id=request_id)
        admission_id = derive_admission_id(tenant_id=user.org_id, request_id=request_id)
        authority = _compile_authority(
            user=user,
            request_id=request_id,
            run_id=run_id,
            intent_digest=intent_digest,
            facts=facts,
            now=now,
        )
        plan_input = build_m9_plan_input(
            authority,
            intent_summary=f"Execute the authorized synthetic capability for intent token {intent_digest}",
        )
        provider = self._provider_factory(plan_input)
        decision = M9PlannerEngine(provider, provider_mode=self._provider_mode).plan(plan_input)
        if decision.disposition not in {M9PlannerDisposition.ACCEPTED, M9PlannerDisposition.REPAIRED} or not isinstance(
            decision.proposal, PlanProposal
        ):
            code = "PLANNER_PROVIDER_FAILURE" if any(item.value == "PROVIDER_FAILURE" for item in decision.codes) else "PLANNER_REJECTED"
            raise M10PlanningError(code)
        compilation = compile_m9_plan(
            authority,
            decision.proposal,
            admission_id=admission_id,
            plan_run_id=run_id,
        )
        original = _admission_bundle(
            user=user,
            facts=facts,
            authority=authority,
            admission_id=admission_id,
            intent_digest=intent_digest,
            provider_mode=self._provider_mode,
            planner_observation=decision.observation,
            now=now,
        )
        admission = build_m8_admission_bundle(original, compilation)
        return SyntheticM10PreparedRun(
            run_id=run_id,
            intent_digest=intent_digest,
            business_inputs_digest=_digest(facts.model_dump(mode="json")),
            business_inputs=facts.model_dump(mode="json"),
            compilation=compilation,
            admission_bundle=admission,
            target_url=target_url,
        )

    def restore_run(
        self,
        request: PackRunRestoreRequest | TaskAdmissionBundle,
        *,
        target_url: str | None = None,
    ) -> PreparedRunReference | SyntheticM10PreparedRun:
        if isinstance(request, TaskAdmissionBundle):
            if target_url is None:
                raise ValueError("Legacy restore requires target_url")
            return self._restore_run_legacy(request, target_url=target_url)
        if request.binding != self.binding:
            raise ValueError("Stored Agent Run binding does not match this adapter")
        bundle = TaskAdmissionBundle.model_validate(request.admission_payload)
        run = self._restore_run_legacy(bundle, target_url=request.target_url)
        return self._reference(run)

    def _restore_run_legacy(self, bundle: TaskAdmissionBundle, *, target_url: str) -> SyntheticM10PreparedRun:
        """Rebuild trusted execution state from admission without invoking a provider."""

        facts = PaymentFacts.model_validate(bundle.request.typed_inputs)
        user = UserContext(
            user_id=bundle.request.principal_ref,
            org_id=bundle.request.tenant_id,
            department_roles=[
                DepartmentRole(
                    department_id=PAYMENTS_DEPARTMENT_ID,
                    department_name="Synthetic payments",
                    role="operator",
                )
            ],
            business_line_ids=[BUSINESS_LINE_ID],
        )
        token = bundle.request.user_intent_summary.rsplit(" ", 1)[-1]
        authority = _compile_authority(
            user=user,
            request_id=bundle.request.request_id,
            run_id=bundle.task.task_id,
            intent_digest=token,
            facts=facts,
            now=bundle.request.submitted_at,
        )
        roles: list[str] = []
        for work_order in bundle.work_orders:
            matching = [
                role.value
                for role in M9StepRole
                if work_order.navigation_goal == f"M8 governed {role.value} for the admitted synthetic payment"
            ]
            if len(matching) != 1:
                raise ValueError("Stored Agent Run has an invalid trusted step role")
            roles.append(matching[0])
        compilation = build_synthetic_m8_compilation(
            authority,
            admission_id=bundle.admission_id,
            plan_run_id=bundle.task.task_id,
            step_roles=tuple(roles),
        )
        if build_m8_admission_bundle(
            _admission_bundle(
                user=user,
                facts=facts,
                authority=authority,
                admission_id=bundle.admission_id,
                intent_digest=token,
                provider_mode=bundle.provider_mode,
                planner_observation=bundle.planner_observation,
                now=bundle.request.submitted_at,
            ),
            compilation,
        ) != bundle:
            raise ValueError("Stored Agent Run admission does not match trusted reconstruction")
        return SyntheticM10PreparedRun(
            run_id=bundle.task.task_id,
            intent_digest=token,
            business_inputs_digest=_digest(facts.model_dump(mode="json")),
            business_inputs=facts.model_dump(mode="json"),
            compilation=compilation,
            admission_bundle=bundle,
            target_url=target_url,
        )

    async def admit_run(
        self,
        prepared: PreparedRunReference | object,
        *,
        approval_handler: ApprovalHandler | None = None,
        operation_key: str,
        **trusted_inputs: Any,
    ) -> PackAdmissionResult | object:
        if not isinstance(prepared, PreparedRunReference):
            return await self._admit_run_legacy(prepared, operation_key=operation_key, **trusted_inputs)
        run = self._unwrap(prepared)
        approval_spec: ApprovalRequestSpecification | None = None

        async def typed_pause_handler(
            *,
            prepared: SyntheticM10PreparedRun,
            checkpoint: object,
            binding: NativeSkyvernBinding,
            pause: M10ApprovalPause,
            operation_key: str,
        ) -> object:
            nonlocal approval_spec
            if approval_handler is None:
                raise ValueError("Typed Pack admission requires an approval handler")
            intent = pause.intent
            decision = pause.decision
            if pause.binding_digest != binding.binding_digest:
                raise ValueError("Pack approval pause does not match its immutable execution binding")
            approver = decision.required_approver or {}
            spec = ApprovalRequestSpecification(
                task_id=binding.native_task_id,
                step_id=binding.native_step_id,
                contract_id=binding.contract_id,
                organization_id=binding.organization_id,
                intent_id=intent.intent_id,
                action_fingerprint=intent.action_fingerprint,
                observation_hash=pause.observation_hash,
                requested_approval_route=f"{approver.get('department_id', PAYMENTS_DEPARTMENT_ID)}:{approver.get('role', 'approver')}",
                source_department_id=PAYMENTS_DEPARTMENT_ID,
                business_line_id=BUSINESS_LINE_ID,
                risk_level=decision.risk_level,
                effect=intent.effect.value,
                expires_at=self._clock() + timedelta(hours=1),
                reason_code="BUSINESS_APPROVAL_REQUIRED",
                redacted_description=intent.operation,
                policy_decision=decision.model_dump(mode="json"),
            )
            approval_spec = spec
            return await approval_handler(self._reference(prepared), spec, operation_key)

        checkpoint = await self._admit_run_legacy(
            run,
            pause_handler=typed_pause_handler,
            operation_key=operation_key,
        )
        initial = (
            PackAdvanceResult(
                status=PackAdvanceStatus.AWAITING_APPROVAL,
                run_id=run.run_id,
                step_id=approval_spec.step_id,
                reason_code=approval_spec.reason_code,
                approval=approval_spec,
            )
            if approval_spec is not None
            else await self._advance_result(checkpoint)
        )
        return PackAdmissionResult(
            prepared=prepared,
            admission_id=run.admission_bundle.admission_id,
            initial=initial,
        )

    async def _admit_run_legacy(self, prepared: object, **trusted_inputs: Any) -> object:
        run = SyntheticM10PreparedRun.model_validate(prepared)
        await self._persist_admission(run)
        pause_handler: M10PauseHandler = trusted_inputs["pause_handler"]
        operation_key = str(trusted_inputs["operation_key"])
        journal = SqlAlchemyGovernedPlanJournal(self._session_factory, clock=self._clock)
        coordinator = self._coordinator(run, journal)
        try:
            return await coordinator.start(
                compilation=run.compilation,
                admission_bundle=run.admission_bundle,
                target_url=run.target_url,
            )
        except M10ApprovalPause as pause:
            checkpoint = await journal.initialize(
                compilation=run.compilation,
                admission_bundle=run.admission_bundle,
                target_url=run.target_url,
            )
            assert checkpoint.active_step is not None
            child = run.compilation.child_for(checkpoint.active_step.work_order_id)
            binding = await self._adapter_factory(run, child).prepare(child.work_order)
            return await pause_handler(
                prepared=run,
                checkpoint=checkpoint,
                binding=binding,
                pause=pause,
                operation_key=operation_key,
            )

    async def advance_run(
        self,
        prepared: PreparedRunReference | object,
        *,
        approval_handler: ApprovalHandler | None = None,
        operation_key: str | None = None,
        **trusted_inputs: Any,
    ) -> PackAdvanceResult | object:
        if not isinstance(prepared, PreparedRunReference):
            return await self._advance_run_legacy(
                prepared,
                operation_key=operation_key,
                **trusted_inputs,
            )
        if approval_handler is None or operation_key is None:
            raise ValueError("Typed Pack advance requires approval_handler and operation_key")
        result = await self._advance_run_legacy(
            self._unwrap(prepared),
            pause_handler=lambda **_kwargs: None,
            operation_key=operation_key,
        )
        return await self._advance_result(result)

    async def _advance_run_legacy(self, prepared: object, **trusted_inputs: Any) -> object:
        run = SyntheticM10PreparedRun.model_validate(prepared)
        journal = SqlAlchemyGovernedPlanJournal(self._session_factory, clock=self._clock)
        checkpoint = await journal.initialize(
            compilation=run.compilation,
            admission_bundle=run.admission_bundle,
            target_url=run.target_url,
        )
        if checkpoint.state is not PlanRunState.ACTIVE or checkpoint.active_step is None:
            return checkpoint
        child = run.compilation.child_for(checkpoint.active_step.work_order_id)
        binding = await self._adapter_factory(run, child).prepare(child.work_order)
        outcome = await self._driver.execute(compilation=child, binding=binding, resume=True)
        if outcome.kind is NativeWorkOutcomeKind.PROBE_BLOCKED:
            blocked = checkpoint.model_copy(
                update={
                    "state": PlanRunState.PROBE_BLOCKED,
                    "active_step": checkpoint.active_step.model_copy(
                        update={
                            "state": PlanStepState.PROBE_BLOCKED,
                            "permit_id": outcome.permit_id,
                            "attempt_id": outcome.attempt_id,
                            "probe_ref": outcome.probe_ref,
                        }
                    ),
                }
            )
            blocked, _ = await journal.append(
                checkpoint=blocked,
                transition=PlanJournalTransition.PROBE_BLOCKED,
                authority_digests=_authority_digests(run.compilation),
                reason=outcome.message or "UNKNOWN requires authoritative result probe",
            )
            return blocked
        if outcome.kind is not NativeWorkOutcomeKind.COMPLETED:
            raise ValueError("M10 approved execution did not reach a governed pause or completion")
        completed = _complete_active(checkpoint, outcome)
        transition = (
            PlanJournalTransition.PLAN_COMPLETED
            if completed.state is PlanRunState.COMPLETED
            else PlanJournalTransition.CHILD_COMPLETED
        )
        completed, _ = await journal.append(
            checkpoint=completed,
            transition=transition,
            authority_digests=_authority_digests(run.compilation),
        )
        if completed.state is PlanRunState.ACTIVE:
            return await self._admit_run_legacy(
                run,
                pause_handler=trusted_inputs["pause_handler"],
                operation_key=trusted_inputs["operation_key"],
            )
        return completed

    async def probe_run(
        self,
        prepared: PreparedRunReference | object,
        *,
        operation_key: str | None = None,
        **trusted_inputs: Any,
    ) -> PackProbeResult | object:
        if not isinstance(prepared, PreparedRunReference):
            return await self._probe_run_legacy(prepared, operation_key=operation_key, **trusted_inputs)
        run = self._unwrap(prepared)

        async def unexpected_approval(**_kwargs: Any) -> object:
            raise ValueError("Result-probe continuation unexpectedly requested a new approval")

        before = await self._current_checkpoint(run)
        if before.active_step is None:
            raise ValueError("Typed probe requires an exact active execution checkpoint")
        exact = await self._execution_checkpoint(before.active_step)
        result = await self._probe_run_legacy(
            run,
            pause_handler=unexpected_approval,
            operation_key=operation_key,
        )
        status = (
            PackProbeStatus.CONFIRMED
            if result.state in {PlanRunState.ACTIVE, PlanRunState.COMPLETED}
            else PackProbeStatus.INCONCLUSIVE
        )
        return PackProbeResult(
            status=status,
            checkpoint=exact,
            reason_code="BUSINESS_RESULT_CONFIRMED" if status is PackProbeStatus.CONFIRMED else "BUSINESS_RESULT_INCONCLUSIVE",
            evidence_refs=(),
        )

    async def _probe_run_legacy(self, prepared: object, **trusted_inputs: Any) -> object:
        run = SyntheticM10PreparedRun.model_validate(prepared)
        journal = SqlAlchemyGovernedPlanJournal(self._session_factory, clock=self._clock)
        checkpoint = await journal.initialize(
            compilation=run.compilation,
            admission_bundle=run.admission_bundle,
            target_url=run.target_url,
        )
        if checkpoint.state is not PlanRunState.PROBE_BLOCKED or checkpoint.active_step is None:
            raise ValueError("M10 probe is legal only for the exact UNKNOWN checkpoint")
        child = run.compilation.child_for(checkpoint.active_step.work_order_id)
        binding = await self._adapter_factory(run, child).prepare(child.work_order)
        outcome = await self._driver.probe(
            compilation=child,
            binding=binding,
            active_step=checkpoint.active_step,
        )
        coordinator = self._coordinator(run, journal)
        resolved = await coordinator.resolve_probe(
            compilation=run.compilation,
            checkpoint=checkpoint,
            outcome=outcome,
        )
        if resolved.state is PlanRunState.ACTIVE:
            return await self._admit_run_legacy(
                run,
                pause_handler=trusted_inputs["pause_handler"],
                operation_key=trusted_inputs["operation_key"],
            )
        return resolved

    def _reference(self, run: SyntheticM10PreparedRun) -> PreparedRunReference:
        return PreparedRunReference(
            run_id=run.run_id,
            tenant_id=run.admission_bundle.request.tenant_id,
            request_id=run.admission_bundle.request.request_id,
            pack_id=self.binding.pack_id,
            pack_version=self.binding.pack_version,
            adapter_id=self.binding.adapter_id,
            admission_id=run.admission_bundle.admission_id,
            contract_id=run.admission_bundle.contract.contract_id,
            provider_mode=self._provider_mode,
            opaque_payload=run.model_dump(mode="json"),
        )

    def _unwrap(self, prepared: PreparedRunReference) -> SyntheticM10PreparedRun:
        if (
            prepared.pack_id != self.binding.pack_id
            or prepared.pack_version != self.binding.pack_version
            or prepared.adapter_id != self.binding.adapter_id
        ):
            raise ValueError("Prepared run reference does not match this immutable adapter")
        run = SyntheticM10PreparedRun.model_validate(prepared.opaque_payload)
        if (
            run.run_id != prepared.run_id
            or run.admission_bundle.admission_id != prepared.admission_id
            or run.admission_bundle.contract.contract_id != prepared.contract_id
        ):
            raise ValueError("Prepared run reference identity does not match its opaque payload")
        return run

    async def _current_checkpoint(self, run: SyntheticM10PreparedRun) -> GovernedPlanCheckpoint:
        return await SqlAlchemyGovernedPlanJournal(self._session_factory, clock=self._clock).initialize(
            compilation=run.compilation,
            admission_bundle=run.admission_bundle,
            target_url=run.target_url,
        )

    async def _advance_result(self, value: object) -> PackAdvanceResult:
        checkpoint = GovernedPlanCheckpoint.model_validate(value)
        if checkpoint.state is PlanRunState.COMPLETED:
            return PackAdvanceResult(status=PackAdvanceStatus.COMPLETED, run_id=checkpoint.root_task_id)
        if checkpoint.state is PlanRunState.PROBE_BLOCKED and checkpoint.active_step is not None:
            return PackAdvanceResult(
                status=PackAdvanceStatus.PENDING_RESULT_PROBE,
                run_id=checkpoint.root_task_id,
                step_id=checkpoint.active_step.native_step_id,
                reason_code="RESULT_UNCERTAIN",
                execution_checkpoint=await self._execution_checkpoint(checkpoint.active_step),
            )
        return PackAdvanceResult(
            status=PackAdvanceStatus.FAILED,
            run_id=checkpoint.root_task_id,
            step_id=checkpoint.active_step.native_step_id if checkpoint.active_step else None,
            reason_code="PACK_ADVANCE_FAILED",
        )

    async def _execution_checkpoint(self, active_step: GovernedPlanStepRef) -> ExecutionCheckpoint:
        async with self._session_factory() as session:
            permit = (
                await session.scalars(
                    select(ExecutionPermitModel).where(ExecutionPermitModel.permit_id == active_step.permit_id)
                )
            ).one()
            attempt = (
                await session.scalars(
                    select(ExecutionAttemptModel).where(ExecutionAttemptModel.attempt_id == active_step.attempt_id)
                )
            ).one()
        if (
            attempt.permit_id != permit.permit_id
            or attempt.status != ExecutionAttemptStatus.UNKNOWN.value
            or attempt.task_id != active_step.native_task_id
            or attempt.step_id != active_step.native_step_id
            or attempt.result_probe_ref != active_step.probe_ref
            or not attempt.idempotency_key_digest
            or not attempt.execution_effect
        ):
            raise ValueError("Persisted Pack checkpoint does not match the exact UNKNOWN Attempt")
        return ExecutionCheckpoint(
            permit_id=permit.permit_id,
            attempt_id=attempt.attempt_id,
            task_id=attempt.task_id,
            step_id=attempt.step_id,
            action_fingerprint=attempt.action_fingerprint,
            observation_hash=attempt.observation_hash,
            idempotency_key_digest=attempt.idempotency_key_digest,
            execution_effect=attempt.execution_effect,
            result_probe_ref=active_step.probe_ref or RESULT_PROBE_REF,
            attempt_status=attempt.status,
        )

    async def _persist_admission(self, run: SyntheticM10PreparedRun) -> None:
        bundle = run.admission_bundle
        async with self._session_factory() as session:
            async with session.begin():
                existing = (
                    await session.scalars(
                        select(GovernedTaskAdmissionModel)
                        .where(
                            GovernedTaskAdmissionModel.organization_id == bundle.task.organization_id,
                            GovernedTaskAdmissionModel.request_id == bundle.request.request_id,
                        )
                        .with_for_update()
                    )
                ).first()
                if existing is not None:
                    if TaskAdmissionBundle.model_validate(existing.bundle_payload) != bundle:
                        raise ValueError("M10 request id was reused with different trusted semantics")
                    return
                payload = canonical_task_admission_payload(bundle)
                session.add(
                    GovernedTaskAdmissionModel(
                        admission_id=bundle.admission_id,
                        organization_id=bundle.task.organization_id,
                        request_id=bundle.request.request_id,
                        task_id=bundle.task.task_id,
                        contract_id=bundle.contract.contract_id,
                        bundle_schema_version=bundle.schema_version,
                        admission_fingerprint=_digest(payload),
                        bundle_fingerprint=_digest(payload),
                        bundle_payload=payload,
                        mode="audit",
                        committed_at=self._clock(),
                    )
                )

    def _adapter_factory(self, run: SyntheticM10PreparedRun, child: object) -> NativeSkyvernWorkOrderAdapter:
        return NativeSkyvernWorkOrderAdapter(
            SqlAlchemyNativePublicationRepository(self._session_factory),
            compilation=child,
            admission_bundle=run.admission_bundle,
            target_url=run.target_url,
            navigation_payload=dict(run.business_inputs),
            clock=self._clock,
        )

    def _coordinator(
        self,
        run: SyntheticM10PreparedRun,
        journal: SqlAlchemyGovernedPlanJournal,
    ) -> GovernedPlanCoordinator:
        return GovernedPlanCoordinator(
            journal,
            adapter_factory=lambda child, _bundle: self._adapter_factory(run, child),
            runner=self._driver,
        )


def _compile_authority(
    *,
    user: UserContext,
    request_id: str,
    run_id: str,
    intent_digest: str,
    facts: PaymentFacts,
    now: datetime,
) -> object:
    scope = CapabilityDataScope(
        department_id=PAYMENTS_DEPARTMENT_ID,
        business_line_id=BUSINESS_LINE_ID,
        resource_ids={facts.payment_id},
    )
    return compile_synthetic_request(
        natural_language_request=f"Process one authorized synthetic payment intent token {intent_digest}",
        context=SyntheticM6TrustedContext(
            request_id=request_id,
            task_id=run_id,
            contract_id="contract_m10_" + _digest([run_id, "root-contract"]),
            tenant_id=user.org_id,
            user=user,
            data_scope=scope,
            resolved_at=now,
        ),
        installation=build_synthetic_installation(
            tenant_id=user.org_id,
            accepted_at=now - timedelta(seconds=1),
            expires_at=now + timedelta(minutes=20),
            contract_digest=SYNTHETIC_RUNTIME_CONTRACT.manifest_digest,
        ),
        conformance_report=build_synthetic_conformance_attestation(),
        planner=DeterministicPlanner(facts.model_dump(mode="json")),
    )


def _admission_bundle(
    *,
    user: UserContext,
    facts: PaymentFacts,
    authority: object,
    admission_id: str,
    intent_digest: str,
    provider_mode: Literal["recorded", "live"],
    planner_observation: PlannerObservation | None,
    now: datetime,
) -> TaskAdmissionBundle:
    grant = authority.grants.grants[0]
    request = CapabilityRequest(
        request_id=authority.trace.request_id,
        submitted_at=now,
        entry_mode=EntryMode.UI,
        principal_ref=user.user_id,
        session_ref=f"m10-session:{_digest([user.user_id, authority.trace.request_id])}",
        tenant_id=user.org_id,
        requested_scope=authority.business_plan.data_scope,
        capability_ref=CAPABILITY_ID,
        capability_version=PACK_VERSION,
        request_kind=CapabilityRequestKind.TRANSITION,
        typed_inputs=facts.model_dump(mode="json"),
        resource_refs={facts.payment_id},
        user_intent_summary=f"Authorized synthetic intent token {intent_digest}",
        grant_ref=grant.grant_id,
        contract_versions={"pack": PACK_VERSION, "policy": POLICY_VERSION, "task_contract": "1"},
    )
    snapshot = TrustedTaskCreationSnapshot(
        task_id=authority.business_plan.task_id,
        organization_id=user.org_id,
        creation_path=TaskCreationPath.SDK_API,
        initiator_id=user.user_id,
        service_principal_id=authority.task_contract.service_principal_id,
        department_id=PAYMENTS_DEPARTMENT_ID,
        business_line_id=BUSINESS_LINE_ID,
        authorization_snapshot={"installation_id": authority.installation.installation_id},
        policy_version=POLICY_VERSION,
        contract_version=1,
        created_at=now,
        request_id=request.request_id,
        caller_id="m10-agent-run-api",
    )
    audit = AdmissionAuditRecord(
        admission_id=admission_id,
        request_id=request.request_id,
        task_id=authority.business_plan.task_id,
        organization_id=user.org_id,
        contract_id=authority.task_contract.contract_id,
        plan_id=authority.business_plan.plan_id,
        grant_id=grant.grant_id,
        capability_id=CAPABILITY_ID,
        capability_version=PACK_VERSION,
        policy_version=POLICY_VERSION,
        revocation_epoch=grant.revocation_epoch,
        mode=authority.task_contract.mode,
        created_at=now,
    )
    return TaskAdmissionBundle(
        provider_mode=provider_mode,
        runtime_binding=PackRuntimeBinding(
            pack_id=PACK_ID,
            pack_version=PACK_VERSION,
            capability_ids=SYNTHETIC_RUNTIME_CONTRACT.capability_ids,
            adapter_id=M10_ADAPTER_ID,
        ),
        planner_observation=planner_observation,
        admission_id=admission_id,
        task=GovernedTaskDraft(
            task_id=authority.business_plan.task_id,
            organization_id=user.org_id,
            goal=authority.task_contract.goal,
        ),
        creation_snapshot=snapshot,
        contract=authority.task_contract,
        request=request,
        grants=tuple(authority.grants.grants),
        plan=authority.business_plan,
        work_orders=(authority.work_order,),
        audit_record=audit,
    )


def _digest(value: object) -> str:
    canonical = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
