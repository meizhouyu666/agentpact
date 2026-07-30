"""Synthetic M10 runtime adapter over the existing M6-M9 governed path."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from enterprise.agent.constrained_planner import DeterministicPlanner
from enterprise.agent.interactions import CapabilityRequest, CapabilityRequestKind, EntryMode
from enterprise.auth.schemas import UserContext
from enterprise.governance.admission import AdmissionAuditRecord, GovernedTaskDraft, TaskAdmissionBundle
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
from enterprise.governance.pack_conformance import evaluate_static_pack_conformance
from enterprise.governance.pack_runtime import (
    ModelSafeRuntimeProjection,
    PackRuntimeBinding,
)
from enterprise.governance.permit_service import issue_permit
from enterprise.governance.result_probes import ResultProbeEvidence
from enterprise.governance.resume_execution_service import claim_resuming_task_for_execution
from skyvern.forge import app as forge_app
from skyvern.forge.native_action import (
    NativeActionDisposition,
    NativeActionHandlerOutcome,
    NativeActionResolution,
    PostActionControl,
)
from skyvern.forge.sdk.db.models import StepModel, TaskModel
from skyvern.forge.sdk.models import StepStatus
from skyvern.forge.sdk.schemas.tasks import TaskStatus
from skyvern.webeye.actions.actions import ClickAction

from .constants import (
    BUSINESS_LINE_ID,
    CAPABILITY_ID,
    PACK_ID,
    PACK_VERSION,
    PAYMENTS_DEPARTMENT_ID,
    POLICY_VERSION,
)
from .m6_runtime import (
    SyntheticM6TrustedContext,
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
)
from .m9_runtime import (
    M9PlannerDisposition,
    M9PlannerEngine,
    PlanProposal,
    RecordedM9Provider,
    build_m9_plan_input,
    compile_m9_plan,
)
from .models import PaymentFacts
from .policy import require_approval_decision
from .sdk_manifest import build_pack_sdk_manifest

M10_ADAPTER_ID = "synthetic.payment.agent-run-runtime.v1"


def derive_agent_run_id(*, tenant_id: str, request_id: str) -> str:
    return "run_m10_" + _digest(["agentpact-agent-run/v1", tenant_id, request_id])


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
    """Trusted driver signal carrying the resolver's verified pause context."""

    def __init__(self, *, resolution: NativeActionResolution, action: object) -> None:
        self.resolution = resolution
        self.action = action
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
            await self._complete_read(binding)
            return NativeWorkOutcome(kind=NativeWorkOutcomeKind.COMPLETED)
        if not self._hmac_secret:
            raise ValueError("M10 native governance HMAC secret is not configured")
        if resume:
            return await self._execute_after_approval(compilation=compilation, binding=binding)
        return await self._request_approval(compilation=compilation, binding=binding)

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
        task, step, page, browser_state = await self._browser_context(binding)
        await self._prepare_synthetic_challenge(page=page, compilation=compilation)
        await self._mark_step_running(binding)
        task, step = await self._native_rows(binding)
        scraped = await self._scrape(browser_state=browser_state, page=page)
        action = self._submit_action(task=task, step=step, scraped_page=scraped)
        resolution = await self._resolver(
            compilation=compilation,
            binding=binding,
            page=page,
            approved=False,
        ).resolve(task=task, step=step, scraped_page=scraped, action=action)
        if resolution.disposition is not NativeActionDisposition.APPROVAL_REQUIRED:
            raise ValueError("M10 initial native resolution did not produce an approval pause")
        raise M10ApprovalPause(resolution=resolution, action=action)

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
        task, step, page, browser_state = await self._browser_context(binding)
        await page.evaluate("document.body.dataset.m10FreshObservation = 'approved'")
        scraped = await self._scrape(browser_state=browser_state, page=page)
        action = self._submit_action(task=task, step=step, scraped_page=scraped)
        resolver = self._resolver(
            compilation=compilation,
            binding=binding,
            page=page,
            approved=True,
        )
        resolution = await resolver.resolve(task=task, step=step, scraped_page=scraped, action=action)
        if resolution.disposition is not NativeActionDisposition.BOUND_AUTHORIZED_EFFECT:
            raise ValueError("M10 resume did not produce fresh Permit-backed authority")
        from skyvern.webeye.actions.handler import ActionHandler

        handler_outcome = await ActionHandler.handle_action(
            scraped_page=scraped,
            task=task,
            step=step,
            page=page,
            action=action,
            native_resolution=resolution,
        )
        if (
            not isinstance(handler_outcome, NativeActionHandlerOutcome)
            or handler_outcome.post_action_control is not PostActionControl.SUSPEND_FOR_PROBE
            or handler_outcome.attempt_id is None
        ):
            raise ValueError("M10 native effect did not enter the exact UNKNOWN probe boundary")
        await resolver.suspend_for_probe(
            task=task,
            step=step,
            resolution=resolution,
            attempt_id=handler_outcome.attempt_id,
        )
        assert resolution.execution_authorization is not None
        return NativeWorkOutcome(
            kind=NativeWorkOutcomeKind.PROBE_BLOCKED,
            permit_id=resolution.execution_authorization.permit_id,
            attempt_id=handler_outcome.attempt_id,
            probe_ref=binding.result_probe_ref,
        )

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

    async def _scrape(self, *, browser_state: Any, page: Any) -> Any:
        async def identity_cleanup(_page: Any, _url: str, tree: list[dict[str, Any]]) -> list[dict[str, Any]]:
            return tree

        return await browser_state.scrape_website(
            url=page.url,
            cleanup_element_tree=identity_cleanup,
            take_screenshots=False,
            draw_boxes=False,
            scroll=False,
            max_screenshot_number=1,
            must_included_tags=["button", "select"],
        )

    def _submit_action(self, *, task: Any, step: Any, scraped_page: Any) -> ClickAction:
        element_id = _element_id_by_aria_label(scraped_page, "Execute synthetic payment once")
        return ClickAction(
            element_id=element_id,
            organization_id=task.organization_id,
            task_id=task.task_id,
            step_id=step.step_id,
            step_order=step.order,
            action_order=0,
            description="Execute the freshly approved M10 synthetic action exactly once",
            reasoning="Trusted recorded M10 native composition",
            intention="Execute the freshly approved M10 synthetic action exactly once",
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
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._driver = driver
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    @property
    def binding(self) -> PackRuntimeBinding:
        manifest = build_pack_sdk_manifest()
        return PackRuntimeBinding(
            pack_id=manifest.pack_id,
            pack_version=manifest.pack_version,
            capability_ids=tuple(item.capability_id for item in manifest.capabilities),
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

    def prepare_run(self, **trusted_inputs: Any) -> SyntheticM10PreparedRun:
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
        manifest = build_pack_sdk_manifest()
        scope = CapabilityDataScope(
            department_id=PAYMENTS_DEPARTMENT_ID,
            business_line_id=BUSINESS_LINE_ID,
            resource_ids={facts.payment_id},
        )
        authority = compile_synthetic_request(
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
                contract_digest=manifest.manifest_digest,
            ),
            conformance_report=evaluate_static_pack_conformance(manifest),
            planner=DeterministicPlanner(facts.model_dump(mode="json")),
        )
        plan_input = build_m9_plan_input(
            authority,
            intent_summary=f"Execute the authorized synthetic capability for intent token {intent_digest}",
        )
        provider = RecordedM9Provider(
            [
                {
                    "capability_id": CAPABILITY_ID,
                    "input_slots": [item.name for item in plan_input.input_slots],
                    "step_roles": ["precheck", "submit", "confirm"],
                }
            ]
        )
        decision = M9PlannerEngine(provider).plan(plan_input)
        if decision.disposition is not M9PlannerDisposition.ACCEPTED or not isinstance(decision.proposal, PlanProposal):
            raise ValueError("M10 recorded constrained Planner did not produce an accepted proposal")
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

    async def admit_run(self, prepared: object, **trusted_inputs: Any) -> object:
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

    async def advance_run(self, prepared: object, **trusted_inputs: Any) -> object:
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
            return await self.admit_run(
                run,
                pause_handler=trusted_inputs["pause_handler"],
                operation_key=trusted_inputs["operation_key"],
            )
        return completed

    async def probe_run(self, prepared: object, **trusted_inputs: Any) -> object:
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
            return await self.admit_run(
                run,
                pause_handler=trusted_inputs["pause_handler"],
                operation_key=trusted_inputs["operation_key"],
            )
        return resolved

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
                payload = bundle.model_dump(mode="json")
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


def _admission_bundle(
    *,
    user: UserContext,
    facts: PaymentFacts,
    authority: object,
    admission_id: str,
    intent_digest: str,
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


def _element_id_by_aria_label(scraped_page: Any, aria_label: str) -> str:
    for element_id, element in scraped_page.id_to_element_dict.items():
        if (element.get("attributes") or {}).get("aria-label") == aria_label:
            return element_id
    raise ValueError(f"M10 native observation did not contain the expected {aria_label!r} control")
