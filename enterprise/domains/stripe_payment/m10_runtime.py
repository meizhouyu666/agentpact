"""Stripe M10 runtime adapter over the existing M6 governed path.

Implements the generic ``PackRuntimeAdapter`` boundary for ``stripe.payment``
using the same admission primitives as the synthetic adapter:

- ``prepare_run`` compiles exactly one trusted M6 Work Order through the
  constrained Planner (recorded provider by default; live provider requires
  complete configuration and never falls back).
- ``admit_run`` persists the admission and drives the governed stripe harness
  to the always-on approval pause.
- ``advance_run`` applies the independent approval and performs the recorded
  side effect through the stripe enforce harness.
- ``probe_run`` resolves an UNKNOWN attempt through the independent probe.

Platform boundary (documented in PACK.md): ``AgentRunService`` state tracking
uses the pack-neutral M8 journal/checkpoint contract. This adapter is the
pack-side of the M10 boundary and is registry-conformant; callers compose it
explicitly through ``compose_stripe_agent_run_service``. Live browser execution
is available only when an explicit ``StripeHostedCheckoutFlow`` and durable
Attempt/Permit session factory are injected; missing wiring, credentials,
approval/capability expiry, unsafe browser state, or inconclusive probes fail
closed. The hosted flow remains a test-mode candidate and never falls back to
recorded execution.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from enterprise.agent.constrained_planner import (
    DeterministicPlanner,
    OpenAICompatiblePlanner,
    PlannerObservation,
    PlannerTransport,
)
from enterprise.agent.interactions import CapabilityRequest, CapabilityRequestKind, EntryMode
from enterprise.agent_runs.pause_signal import (
    RunPauseAction,
    RunPauseOutcome,
    RunPausePromptMetadata,
    RunPauseSignal,
    RunResumePolicy,
)
from enterprise.agent_runs.service import AgentRunService
from enterprise.applications.agent_runs import compose_agent_run_service
from enterprise.auth.schemas import DepartmentRole, UserContext
from enterprise.browser_loop.persisted_executor import recover_abandoned_persisted_executions
from enterprise.governance.admission import AdmissionAuditRecord, GovernedTaskDraft, TaskAdmissionBundle
from enterprise.governance.capabilities import CapabilityDataScope
from enterprise.governance.contracts import ExecutionAttemptStatus
from enterprise.governance.creation_snapshot import TaskCreationPath, TrustedTaskCreationSnapshot
from enterprise.governance.input_contracts import (
    AdapterRequirement,
    FieldBinding,
    InputRequest,
    InputSensitivity,
    InputSlotSpec,
    InputSlotStatus,
    InputSource,
    InputTargetKind,
)
from enterprise.governance.models import ExecutionAttemptModel, GovernedTaskAdmissionModel
from enterprise.governance.pack_runtime import (
    ApprovalHandler,
    ApprovalRequestSpecification,
    ExecutionCheckpoint,
    ModelSafeRuntimeProjection,
    PackAdmissionResult,
    PackAdvanceResult,
    PackAdvanceStatus,
    PackProbeResult,
    PackProbeStatus,
    PackRunRequest,
    PackRunRestoreRequest,
    PackRuntimeBinding,
    PackRuntimeRegistry,
    PreparedRunReference,
)

from .accounts import require_stripe_account
from .constants import (
    BUSINESS_LINE_ID,
    CAPABILITY_ID,
    PACK_ID,
    PACK_VERSION,
    PAYMENTS_DEPARTMENT_ID,
    POLICY_VERSION,
    RESULT_PROBE_REF,
)
from .harness import ChallengeState, StripePaymentEnforceHarness, StripeSubmissionChallenge
from .live_browser import (
    StripeHostedCheckoutError,
    StripeHostedCheckoutFlow,
    derive_live_idempotency_key,
    stripe_test_key_from_environment,
)
from .m6_runtime import (
    STRIPE_RUNTIME_CONTRACT,
    StripeM6Compilation,
    StripeM6TrustedContext,
    build_stripe_conformance_attestation,
    build_stripe_installation,
    compile_stripe_request,
)
from .models import StripePaymentFacts

M10_ADAPTER_ID = "stripe.payment.agent-run-runtime.v1"
M10_HMAC_SECRET_ENV = "AGENT_RUN_HMAC_SECRET"
M10_DEMO_HMAC_SECRET = "stripe-m10-demo-only-hmac"

StripePlannerFactory = Callable[[dict[str, object]], object]


class StripeM10NotWired(RuntimeError):
    """Fail-closed marker for missing explicit live M10 composition."""


_STRIPE_INPUT_STEP_ID = "stripe.payment.input-preflight.v1"
_STRIPE_INPUT_TARGETS: dict[str, InputTargetKind] = {
    "payment_intent_id": InputTargetKind.IDENTIFIER,
    "amount_minor": InputTargetKind.NUMBER,
}
_STRIPE_ADAPTER_ID = M10_ADAPTER_ID
STRIPE_INPUT_SLOTS: tuple[InputSlotSpec, ...] = tuple(
    InputSlotSpec(
        slot_name=name,
        target_kind=kind,
        source=InputSource.USER,
        sensitivity=InputSensitivity.SENSITIVE,
        allowed_sources=(InputSource.USER,),
    )
    for name, kind in _STRIPE_INPUT_TARGETS.items()
)
STRIPE_INPUT_BINDINGS: tuple[FieldBinding, ...] = (
    FieldBinding(
        binding_version="v1",
        slot_name="payment_intent_id",
        adapter_field="payment_intent",
        target_kind=InputTargetKind.IDENTIFIER,
        adapter_id=_STRIPE_ADAPTER_ID,
        source=InputSource.ADAPTER,
        sensitivity=InputSensitivity.SENSITIVE,
    ),
    FieldBinding(
        binding_version="v1",
        slot_name="amount_minor",
        adapter_field="amount",
        target_kind=InputTargetKind.NUMBER,
        adapter_id=_STRIPE_ADAPTER_ID,
        source=InputSource.ADAPTER,
        sensitivity=InputSensitivity.SENSITIVE,
    ),
)
STRIPE_ADAPTER_REQUIREMENTS: tuple[AdapterRequirement, ...] = (
    AdapterRequirement(
        requirement_name="hosted_checkout_session",
        target_kind=InputTargetKind.CUSTOM,
        source=InputSource.ADAPTER,
        sensitivity=InputSensitivity.INTERNAL,
        description="A governed Stripe hosted Checkout session must be available before effect execution.",
    ),
)


def stripe_input_declaration() -> tuple[tuple[InputSlotSpec, ...], tuple[FieldBinding, ...], tuple[AdapterRequirement, ...]]:
    """Return Stripe's adapter-local semantic declaration and hosted mappings."""

    return STRIPE_INPUT_SLOTS, STRIPE_INPUT_BINDINGS, STRIPE_ADAPTER_REQUIREMENTS


def map_stripe_inputs_to_checkout(business_inputs: dict[str, Any]) -> dict[str, Any]:
    """Translate semantic Pack values to hosted Checkout fields at the adapter edge."""

    missing = missing_stripe_inputs(business_inputs)
    if missing:
        raise ValueError(f"Missing Stripe semantic inputs: {', '.join(missing)}")
    return {binding.adapter_field: business_inputs[binding.slot_name] for binding in STRIPE_INPUT_BINDINGS}


def _missing_stripe_business_slots(business_inputs: dict[str, Any]) -> tuple[str, ...]:
    """Return only absent required Pack slots; invalid supplied values still fail closed."""

    return tuple(
        name
        for name in _STRIPE_INPUT_TARGETS
        if name not in business_inputs or business_inputs[name] is None
    )


def missing_stripe_inputs(business_inputs: dict[str, Any]) -> tuple[str, ...]:
    """Return missing required Stripe semantic inputs without observing a page."""

    return _missing_stripe_business_slots(business_inputs)


def build_stripe_input_pause_signal(
    *,
    run_id: str,
    missing_slots: tuple[str, ...],
    task_id: str | None = None,
    step_id: str = _STRIPE_INPUT_STEP_ID,
    checkpoint_id: str | None = None,
) -> RunPauseSignal:
    """Compose a redacted, pre-effect Stripe input pause at the Pack edge.

    This helper deliberately carries semantic slot names only. Adapter field
    bindings and observed Checkout values stay outside the platform contract.
    """

    unknown = set(missing_slots) - set(_STRIPE_INPUT_TARGETS)
    if unknown:
        raise ValueError(f"Unsupported Stripe semantic input slots: {sorted(unknown)}")
    if not missing_slots:
        raise ValueError("Stripe input pause requires at least one missing slot")
    unique_slots = tuple(dict.fromkeys(missing_slots))
    checkpoint = checkpoint_id or "stripe-input-" + _digest([run_id, step_id, unique_slots])[:32]
    slots = tuple(slot for slot in STRIPE_INPUT_SLOTS if slot.slot_name in unique_slots)
    request = InputRequest(
        request_id=f"stripe-input-request:{checkpoint}",
        pack_id=PACK_ID,
        pack_version=PACK_VERSION,
        slots=slots,
        status={name: InputSlotStatus.MISSING for name in unique_slots},
        recovery=True,
        external_effect_started=False,
    )
    return RunPauseSignal(
        outcome=RunPauseOutcome.AWAITING_INPUT,
        reason_code="STRIPE_INPUT_REQUIRED",
        run_id=run_id,
        task_id=task_id or run_id,
        step_id=step_id,
        checkpoint_id=checkpoint,
        input_request=request,
        prompt=RunPausePromptMetadata(
            title="Payment details required",
            message="Provide the missing payment details to continue this test-mode run.",
        ),
        allowed_actions=(RunPauseAction.SUBMIT_INPUT, RunPauseAction.CANCEL),
        resume_policy=RunResumePolicy.INPUT_SUBMISSION,
        external_effect_started=False,
    )


def derive_stripe_agent_run_id(*, tenant_id: str, request_id: str) -> str:
    return "run_" + _digest(["agentpact-agent-run/v1", tenant_id, request_id])


def derive_stripe_admission_id(*, tenant_id: str, request_id: str) -> str:
    return "admission_m10_" + _digest(["agentpact-agent-run-admission/v1", tenant_id, request_id])


def build_stripe_provider_factory(
    provider_mode: Literal["recorded", "live"],
    *,
    endpoint: str | None = None,
    model: str | None = None,
    api_key_env: str = "OPENAI_COMPATIBLE_API_KEY",
    transport: PlannerTransport | None = None,
) -> StripePlannerFactory:
    """Build one explicit planner composition; live mode never falls back."""

    if provider_mode == "recorded":
        return lambda business_inputs: DeterministicPlanner(business_inputs)
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
    return lambda _business_inputs: planner


class StripeM10PreparedRun(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid", frozen=True)

    run_id: str
    intent_digest: str
    business_inputs_digest: str
    business_inputs: dict[str, Any]
    user: UserContext
    compilation: StripeM6Compilation
    admission_bundle: TaskAdmissionBundle
    target_url: str


class StripeM10PauseHandler(Protocol):
    async def __call__(
        self,
        *,
        prepared: StripeM10PreparedRun,
        challenge_id: str,
        operation_key: str | None,
    ) -> object: ...


class StripePaymentRuntimeAdapter:
    """The explicitly composed stripe implementation of the generic adapter."""

    def __init__(
        self,
        session_factory: Callable[[], Any] | None = None,
        *,
        hmac_secret: str | None = None,
        provider_mode: Literal["recorded", "live"] = "recorded",
        provider_factory: StripePlannerFactory | None = None,
        live_browser: StripeHostedCheckoutFlow | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        configured_secret = hmac_secret or os.environ.get(M10_HMAC_SECRET_ENV)
        if provider_mode == "live" and (not configured_secret or configured_secret == M10_DEMO_HMAC_SECRET):
            raise ValueError(
                "Stripe live Agent Run composition requires a non-default injected HMAC integrity secret"
            )
        self._secret = configured_secret or M10_DEMO_HMAC_SECRET
        self._provider_mode = provider_mode
        self._provider_factory = provider_factory or build_stripe_provider_factory(provider_mode)
        self._live_browser = live_browser
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._harnesses: dict[str, StripePaymentEnforceHarness] = {}
        self._challenge_ids: dict[str, str] = {}
        self._live_results: dict[str, object] = {}

    @property
    def provider_mode(self) -> Literal["recorded", "live"]:
        return self._provider_mode

    @staticmethod
    def missing_input_pause(
        *,
        run_id: str,
        business_inputs: dict[str, Any],
        task_id: str | None = None,
        checkpoint_id: str | None = None,
    ) -> RunPauseSignal | None:
        """Return the Pack-local preflight pause, if required inputs are absent.

        The generic runtime protocol still receives a validated
        ``PackRunRequest``. Explicit compositions may call this helper before
        invoking that protocol when their API accepts partially filled input.
        Supplied-but-invalid values continue through normal validation and are
        never repaired from browser observation.
        """

        missing = missing_stripe_inputs(business_inputs)
        if not missing:
            return None
        return build_stripe_input_pause_signal(
            run_id=run_id,
            missing_slots=missing,
            task_id=task_id,
            checkpoint_id=checkpoint_id,
        )

    @property
    def binding(self) -> PackRuntimeBinding:
        return PackRuntimeBinding(
            pack_id=STRIPE_RUNTIME_CONTRACT.pack_id,
            pack_version=STRIPE_RUNTIME_CONTRACT.pack_version,
            capability_ids=STRIPE_RUNTIME_CONTRACT.capability_ids,
            adapter_id=M10_ADAPTER_ID,
        )

    def model_safe_projection(self, authority: object) -> ModelSafeRuntimeProjection:
        compilation = authority
        capabilities = tuple(item.capability_id for item in compilation.projection)
        return ModelSafeRuntimeProjection(
            pack_id=PACK_ID,
            pack_version=PACK_VERSION,
            capability_ids=capabilities,
            input_slot_names=tuple(StripePaymentFacts.model_fields),
        )

    def prepare_run(
        self,
        request: PackRunRequest | None = None,
        **trusted_inputs: Any,
    ) -> PreparedRunReference | StripeM10PreparedRun:
        """Prepare a typed reference; keyword calls remain a composition-edge shim."""

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

    def _prepare_run_legacy(self, **trusted_inputs: Any) -> StripeM10PreparedRun:
        user = UserContext.model_validate(trusted_inputs["user"])
        request_id = str(trusted_inputs["request_id"])
        intent_digest = str(trusted_inputs["intent_digest"])
        facts = StripePaymentFacts.model_validate(trusted_inputs["business_inputs"])
        target_url = str(trusted_inputs["target_url"])
        now = trusted_inputs.get("now") or self._clock()
        if user.org_id != trusted_inputs.get("tenant_id"):
            raise ValueError("M10 authenticated tenant does not match the trusted adapter context")
        run_id = derive_stripe_agent_run_id(tenant_id=user.org_id, request_id=request_id)
        admission_id = derive_stripe_admission_id(tenant_id=user.org_id, request_id=request_id)
        authority = _compile_authority(
            user=user,
            request_id=request_id,
            run_id=run_id,
            intent_digest=intent_digest,
            facts=facts,
            now=now,
            planner=self._provider_factory(facts.model_dump(mode="json")),
        )
        admission = _admission_bundle(
            user=user,
            facts=facts,
            authority=authority,
            admission_id=admission_id,
            intent_digest=intent_digest,
            provider_mode=self._provider_mode,
            planner_observation=None,
            now=now,
        )
        return StripeM10PreparedRun(
            run_id=run_id,
            intent_digest=intent_digest,
            business_inputs_digest=_digest(facts.model_dump(mode="json")),
            business_inputs=facts.model_dump(mode="json"),
            user=user,
            compilation=authority,
            admission_bundle=admission,
            target_url=target_url,
        )

    def restore_run(
        self,
        request: PackRunRestoreRequest | TaskAdmissionBundle,
        *,
        target_url: str | None = None,
    ) -> PreparedRunReference | StripeM10PreparedRun:
        if isinstance(request, TaskAdmissionBundle):
            if target_url is None:
                raise ValueError("Legacy restore requires target_url")
            return self._restore_run_legacy(request, target_url=target_url)
        if request.binding != self.binding:
            raise ValueError("Stored Agent Run binding does not match this adapter")
        bundle = TaskAdmissionBundle.model_validate(request.admission_payload)
        run = self._restore_run_legacy(bundle, target_url=request.target_url)
        return self._reference(run)

    def _restore_run_legacy(self, bundle: TaskAdmissionBundle, *, target_url: str) -> StripeM10PreparedRun:
        """Rebuild trusted execution state from admission without invoking a provider."""

        facts = StripePaymentFacts.model_validate(bundle.request.typed_inputs)
        user = UserContext(
            user_id=bundle.request.principal_ref,
            org_id=bundle.request.tenant_id,
            department_roles=[
                DepartmentRole(
                    department_id=PAYMENTS_DEPARTMENT_ID,
                    department_name="Stripe payments",
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
            planner=DeterministicPlanner(facts.model_dump(mode="json")),
        )
        if (
            _admission_bundle(
                user=user,
                facts=facts,
                authority=authority,
                admission_id=bundle.admission_id,
                intent_digest=token,
                provider_mode=bundle.provider_mode,
                planner_observation=bundle.planner_observation,
                now=bundle.request.submitted_at,
            )
            != bundle
        ):
            raise ValueError("Stored Agent Run admission does not match trusted reconstruction")
        return StripeM10PreparedRun(
            run_id=bundle.task.task_id,
            intent_digest=token,
            business_inputs_digest=_digest(facts.model_dump(mode="json")),
            business_inputs=facts.model_dump(mode="json"),
            user=user,
            compilation=authority,
            admission_bundle=bundle,
            target_url=target_url,
        )

    async def admit_run(
        self,
        prepared: PreparedRunReference | object,
        *,
        approval_handler: ApprovalHandler | None = None,
        operation_key: str | None = None,
        **trusted_inputs: Any,
    ) -> PackAdmissionResult | object:
        if not isinstance(prepared, PreparedRunReference):
            return await self._admit_run_legacy(
                prepared,
                operation_key=operation_key,
                **trusted_inputs,
            )
        if approval_handler is None or operation_key is None:
            raise ValueError("Typed Pack admission requires approval_handler and operation_key")
        run = self._unwrap(prepared)
        challenge = await self._prepare_challenge(run)
        spec = self._approval_specification(run, challenge)
        await approval_handler(prepared, spec, operation_key)
        return PackAdmissionResult(
            prepared=prepared,
            admission_id=run.admission_bundle.admission_id,
            initial=PackAdvanceResult(
                status=PackAdvanceStatus.AWAITING_APPROVAL,
                run_id=run.run_id,
                step_id=spec.step_id,
                reason_code=spec.reason_code,
                approval=spec,
            ),
        )

    async def _admit_run_legacy(self, prepared: object, **trusted_inputs: Any) -> object:
        run = StripeM10PreparedRun.model_validate(prepared)
        challenge = await self._prepare_challenge(run)
        pause_handler: StripeM10PauseHandler | None = trusted_inputs.get("pause_handler")
        if pause_handler is not None:
            return await pause_handler(
                prepared=run,
                challenge_id=challenge.challenge_id,
                operation_key=trusted_inputs.get("operation_key"),
            )
        return {"state": ChallengeState.PENDING_APPROVAL.value, "challenge_id": challenge.challenge_id}

    async def _prepare_challenge(self, run: StripeM10PreparedRun) -> StripeSubmissionChallenge:
        if self._session_factory is not None:
            await self._persist_admission(run)
        harness = self._harness_for(run)
        challenge = harness.prepare_submission(
            requester=run.user,
            facts=StripePaymentFacts.model_validate(run.business_inputs),
        )
        self._challenge_ids[run.run_id] = challenge.challenge_id
        return challenge

    async def advance_run(
        self,
        prepared: PreparedRunReference | object,
        *,
        approval_handler: ApprovalHandler | None = None,
        operation_key: str | None = None,
        **trusted_inputs: Any,
    ) -> PackAdvanceResult | object:
        if not isinstance(prepared, PreparedRunReference):
            return await self._advance_run_legacy(prepared, **trusted_inputs)
        run = self._unwrap(prepared)
        if self._provider_mode == "live":
            return await self._advance_live_run(run, **trusted_inputs)
        result = await self._advance_run_legacy(run)
        if result["state"] == ChallengeState.CONFIRMED.value:
            return PackAdvanceResult(status=PackAdvanceStatus.COMPLETED, run_id=run.run_id)
        return PackAdvanceResult(
            status=PackAdvanceStatus.FAILED,
            run_id=run.run_id,
            reason_code="STRIPE_RECORDED_EXECUTION_FAILED",
        )

    async def _advance_run_legacy(self, prepared: object, **trusted_inputs: Any) -> object:
        run = StripeM10PreparedRun.model_validate(prepared)
        if self._provider_mode == "live":
            return await self._advance_live_run(run, **trusted_inputs)
        harness = self._harness_for(run)
        challenge_id = self._challenge_ids[run.run_id]
        challenge = harness.get_challenge(challenge_id)
        if challenge.state is ChallengeState.PENDING_APPROVAL:
            approver_name = "compliance" if challenge.decision.risk_level == "critical" else "approver"
            harness.decide_approval(
                challenge_id=challenge_id,
                requester=run.user,
                approver=require_stripe_account(approver_name),
                approved=True,
            )
        executed = harness.execute_submission(challenge_id=challenge_id)
        return {
            "run_id": run.run_id,
            "state": executed.state.value,
            "attempt_status": executed.attempt.status.value if executed.attempt is not None else None,
            "probe_status": (
                executed.result_probe.status.value if executed.result_probe is not None else None
            ),
            "challenge_id": challenge_id,
        }

    async def _advance_live_run(
        self,
        run: StripeM10PreparedRun,
        **trusted_inputs: Any,
    ) -> PackAdvanceResult | dict[str, Any]:
        if self._live_browser is None or self._session_factory is None:
            raise StripeM10NotWired(
                "stripe.payment live execution requires an injected hosted Checkout flow and persisted Attempt/Permit session"
            )
        if self._clock() >= run.compilation.task_contract.expires_at:
            raise StripeM10NotWired("stripe.payment live approval or capability grant has expired")
        existing = await self._load_live_attempt(run)
        if existing is not None:
            if existing.status == ExecutionAttemptStatus.UNKNOWN.value:
                checkpoint = self._checkpoint_from_live_attempt(existing)
                return PackAdvanceResult(
                    status=PackAdvanceStatus.PENDING_RESULT_PROBE,
                    run_id=run.run_id,
                    step_id=checkpoint.step_id,
                    reason_code="RESULT_UNCERTAIN",
                    execution_checkpoint=checkpoint,
                )
            if existing.status == ExecutionAttemptStatus.CONFIRMED.value:
                return PackAdvanceResult(status=PackAdvanceStatus.COMPLETED, run_id=run.run_id)
            raise StripeM10NotWired("stripe.payment live Attempt already crossed the browser boundary")

        facts = StripePaymentFacts.model_validate(run.business_inputs)
        step_id = run.compilation.business_plan.steps[0].step_id
        result = await self._live_browser.execute_governed(
            facts=facts,
            idempotency_key=derive_live_idempotency_key(
                request_id=run.admission_bundle.request.request_id,
                payment_intent_id=facts.payment_intent_id,
            ),
            task_id=run.run_id,
            step_id=step_id,
            contract_id=run.compilation.task_contract.contract_id,
            organization_id=run.user.org_id,
            session_factory=self._session_factory,
            integrity_secret=self._secret,
            runtime_factory=trusted_inputs.get("runtime_factory"),
            success_url=trusted_inputs.get(
                "success_url",
                "https://example.com/agentpact-stripe-success?session_id={CHECKOUT_SESSION_ID}",
            ),
            cancel_url=trusted_inputs.get("cancel_url", "https://example.com/agentpact-stripe-cancel"),
            now=self._clock(),
        )
        checkpoint = result.execution_checkpoint
        if checkpoint is None:
            raise StripeHostedCheckoutError("Stripe hosted Checkout did not persist an execution checkpoint")
        self._live_results[run.run_id] = result
        return PackAdvanceResult(
            status=PackAdvanceStatus.PENDING_RESULT_PROBE,
            run_id=run.run_id,
            step_id=checkpoint.step_id,
            reason_code="RESULT_UNCERTAIN",
            execution_checkpoint=checkpoint,
        )

    async def _probe_live_run(self, run: StripeM10PreparedRun, **trusted_inputs: Any) -> PackProbeResult:
        if self._live_browser is None or self._session_factory is None:
            raise StripeM10NotWired(
                "stripe.payment live probing requires an injected hosted Checkout flow and durable Attempt session"
            )
        attempt = await self._load_live_attempt(run)
        if attempt is None or attempt.status != ExecutionAttemptStatus.UNKNOWN.value:
            raise ValueError("Stripe live probe requires the exact UNKNOWN Attempt")
        checkpoint = self._checkpoint_from_live_attempt(attempt)
        context = attempt.result_probe if isinstance(attempt.result_probe, dict) else {}
        metadata = context.get("metadata") if isinstance(context.get("metadata"), dict) else {}
        evidence = await self._live_browser.probe_governed(
            facts=StripePaymentFacts.model_validate(run.business_inputs),
            idempotency_key=attempt.idempotency_key,
            checkpoint=checkpoint,
            resource_id=context.get("resource_id") if context.get("resource_id") else None,
            checkout_session_id=metadata.get("checkout_session_id"),
            session_factory=self._session_factory,
            probe_factory=trusted_inputs.get("probe_factory"),
        )
        status = {
            "confirmed": PackProbeStatus.CONFIRMED,
            "not_confirmed": PackProbeStatus.NOT_CONFIRMED,
            "unknown": PackProbeStatus.INCONCLUSIVE,
        }[evidence.status.value]
        reason = {
            PackProbeStatus.CONFIRMED: "BUSINESS_RESULT_CONFIRMED",
            PackProbeStatus.NOT_CONFIRMED: "BUSINESS_RESULT_NOT_CONFIRMED",
            PackProbeStatus.INCONCLUSIVE: "BUSINESS_RESULT_INCONCLUSIVE",
        }[status]
        return PackProbeResult(
            status=status,
            checkpoint=checkpoint,
            reason_code=reason,
            evidence_refs=(evidence.probe_ref,),
        )

    async def recover_abandoned_executions(
        self,
        *,
        minimum_age: timedelta,
        now: datetime | None = None,
    ) -> object:
        if self._session_factory is None:
            raise StripeM10NotWired("stripe.payment recovery requires a persisted session")
        return await recover_abandoned_persisted_executions(
            self._session_factory,
            minimum_age=minimum_age,
            now=now,
        )

    async def _load_live_attempt(self, run: StripeM10PreparedRun) -> ExecutionAttemptModel | None:
        if self._session_factory is None:
            return None
        async with self._session_factory() as session:
            attempts = list(
                (
                    await session.scalars(
                        select(ExecutionAttemptModel).where(ExecutionAttemptModel.task_id == run.run_id)
                    )
                ).all()
            )
            if len(attempts) > 1:
                raise ValueError("Stripe live run has multiple persisted Attempts")
            if attempts:
                if hasattr(session, "expunge"):
                    session.expunge(attempts[0])
                return attempts[0]
        return None

    @staticmethod
    def _checkpoint_from_live_attempt(attempt: ExecutionAttemptModel) -> ExecutionCheckpoint:
        if (
            not attempt.permit_id
            or not attempt.idempotency_key_digest
            or not attempt.execution_effect
            or attempt.result_probe_ref != RESULT_PROBE_REF
            or attempt.status != ExecutionAttemptStatus.UNKNOWN.value
        ):
            raise ValueError("Stripe live Attempt is not an exact UNKNOWN probe checkpoint")
        return ExecutionCheckpoint(
            permit_id=attempt.permit_id,
            attempt_id=attempt.attempt_id,
            task_id=attempt.task_id,
            step_id=attempt.step_id,
            action_fingerprint=attempt.action_fingerprint,
            observation_hash=attempt.observation_hash,
            idempotency_key_digest=attempt.idempotency_key_digest,
            execution_effect=attempt.execution_effect,
            result_probe_ref=attempt.result_probe_ref,
            attempt_status=attempt.status,
        )

    async def probe_run(
        self,
        prepared: PreparedRunReference | object,
        *,
        operation_key: str | None = None,
        **trusted_inputs: Any,
    ) -> PackProbeResult | object:
        if not isinstance(prepared, PreparedRunReference):
            return await self._probe_run_legacy(prepared, **trusted_inputs)
        run = self._unwrap(prepared)
        if self._provider_mode == "live":
            return await self._probe_live_run(run, **trusted_inputs)
        before = self._current_challenge(run)
        checkpoint = self._execution_checkpoint(before)
        result = await self._probe_run_legacy(run)
        status = {
            ChallengeState.CONFIRMED.value: PackProbeStatus.CONFIRMED,
            ChallengeState.FAILED.value: PackProbeStatus.NOT_CONFIRMED,
        }.get(result["state"], PackProbeStatus.INCONCLUSIVE)
        reason_code = {
            PackProbeStatus.CONFIRMED: "BUSINESS_RESULT_CONFIRMED",
            PackProbeStatus.NOT_CONFIRMED: "BUSINESS_RESULT_NOT_CONFIRMED",
            PackProbeStatus.INCONCLUSIVE: "BUSINESS_RESULT_INCONCLUSIVE",
        }[status]
        evidence_ref = before.work_order.result_probe_ref
        return PackProbeResult(
            status=status,
            checkpoint=checkpoint,
            reason_code=reason_code,
            evidence_refs=(evidence_ref,) if evidence_ref else (),
        )

    async def _probe_run_legacy(self, prepared: object, **trusted_inputs: Any) -> object:
        run = StripeM10PreparedRun.model_validate(prepared)
        if self._provider_mode == "live":
            return await self._probe_live_run(run, **trusted_inputs)
        harness = self._harness_for(run)
        challenge_id = self._challenge_ids[run.run_id]
        resolved = harness.resolve_unknown(challenge_id)
        return {
            "run_id": run.run_id,
            "state": resolved.state.value,
            "probe_status": resolved.result_probe.status.value if resolved.result_probe is not None else None,
            "challenge_id": challenge_id,
        }

    def _approval_specification(
        self,
        run: StripeM10PreparedRun,
        challenge: StripeSubmissionChallenge,
    ) -> ApprovalRequestSpecification:
        approver = challenge.decision.required_approver or {}
        return ApprovalRequestSpecification(
            task_id=challenge.intent.task_id,
            step_id=challenge.intent.step_id,
            contract_id=challenge.contract.contract_id,
            organization_id=run.user.org_id,
            intent_id=challenge.intent.intent_id,
            action_fingerprint=challenge.intent.action_fingerprint,
            observation_hash=challenge.observation_hash,
            requested_approval_route=(
                f"{approver.get('department_id', PAYMENTS_DEPARTMENT_ID)}:"
                f"{approver.get('role', 'approver')}"
            ),
            source_department_id=PAYMENTS_DEPARTMENT_ID,
            business_line_id=BUSINESS_LINE_ID,
            risk_level=challenge.decision.risk_level,
            effect=challenge.intent.effect.value,
            expires_at=challenge.contract.expires_at or self._clock() + timedelta(hours=1),
            reason_code="BUSINESS_APPROVAL_REQUIRED",
            redacted_description="Submit one approved Stripe test-mode payment",
            policy_decision=challenge.decision.model_dump(mode="json"),
        )

    def _reference(self, run: StripeM10PreparedRun) -> PreparedRunReference:
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

    def _unwrap(self, prepared: PreparedRunReference) -> StripeM10PreparedRun:
        if (
            prepared.pack_id != self.binding.pack_id
            or prepared.pack_version != self.binding.pack_version
            or prepared.adapter_id != self.binding.adapter_id
        ):
            raise ValueError("Prepared run reference does not match this immutable adapter")
        run = StripeM10PreparedRun.model_validate(prepared.opaque_payload)
        if (
            run.run_id != prepared.run_id
            or run.admission_bundle.admission_id != prepared.admission_id
            or run.admission_bundle.contract.contract_id != prepared.contract_id
        ):
            raise ValueError("Prepared run reference identity does not match its opaque payload")
        return run

    def _current_challenge(self, run: StripeM10PreparedRun) -> StripeSubmissionChallenge:
        challenge_id = self._challenge_ids.get(run.run_id)
        if challenge_id is None:
            raise ValueError("Stripe run has no admitted challenge")
        return self._harness_for(run).get_challenge(challenge_id)

    @staticmethod
    def _execution_checkpoint(challenge: StripeSubmissionChallenge) -> ExecutionCheckpoint:
        permit = challenge.permit
        attempt = challenge.attempt
        if (
            permit is None
            or attempt is None
            or attempt.status.value != "unknown"
            or permit.task_id != attempt.task_id
            or permit.step_id != attempt.step_id
            or permit.action_fingerprint != attempt.action_fingerprint
            or permit.observation_id != attempt.observation_hash
            or not challenge.work_order.result_probe_ref
        ):
            raise ValueError("Stripe probe requires the exact UNKNOWN execution checkpoint")
        return ExecutionCheckpoint(
            permit_id=permit.permit_id,
            attempt_id=attempt.attempt_id,
            task_id=attempt.task_id,
            step_id=attempt.step_id,
            action_fingerprint=attempt.action_fingerprint,
            observation_hash=attempt.observation_hash,
            idempotency_key_digest=hashlib.sha256(attempt.idempotency_key.encode("utf-8")).hexdigest(),
            execution_effect=challenge.intent.effect.value,
            result_probe_ref=challenge.work_order.result_probe_ref,
            attempt_status=attempt.status.value,
        )

    async def _persist_admission(self, run: StripeM10PreparedRun) -> None:
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

    def _harness_for(self, run: StripeM10PreparedRun) -> StripePaymentEnforceHarness:
        harness = self._harnesses.get(run.run_id)
        if harness is None:
            harness = StripePaymentEnforceHarness(
                hmac_secret=self._secret,
                clock=self._clock,
            )
            self._harnesses[run.run_id] = harness
        return harness


def compose_stripe_agent_run_service(
    *,
    session_factory: Callable[[], AbstractAsyncContextManager[Any]],
    target_url: str,
    provider_mode: Literal["recorded", "live"] = "recorded",
    hmac_secret: str | None = None,
    provider_factory: StripePlannerFactory | None = None,
    live_browser: StripeHostedCheckoutFlow | None = None,
    provider_timeout_seconds: float = 30.0,
    clock: Callable[[], datetime] | None = None,
) -> AgentRunService:
    """Compose Stripe's adapter into the generic Agent Run service explicitly.

    This is a Pack-owned composition edge, not formal application startup. A
    live composition must prove the test-mode credential and inject both the
    hosted browser flow and durable session factory; it never falls back to the
    recorded adapter when those requirements are absent.
    """

    if provider_mode not in {"recorded", "live"}:
        raise ValueError("Stripe Agent Run provider mode must be recorded or live")
    if provider_mode == "live":
        if live_browser is None:
            raise ValueError("Stripe live Agent Run composition requires an explicit hosted browser flow")
        stripe_test_key_from_environment()
    registry = PackRuntimeRegistry([STRIPE_RUNTIME_CONTRACT])
    adapter = StripePaymentRuntimeAdapter(
        session_factory,
        hmac_secret=hmac_secret,
        provider_mode=provider_mode,
        provider_factory=provider_factory,
        live_browser=live_browser,
        clock=clock,
    )
    registry.register(adapter)
    return compose_agent_run_service(
        session_factory,
        runtime_registry=registry,
        target_url=target_url,
        default_pack_binding=adapter.binding,
        provider_timeout_seconds=provider_timeout_seconds,
        clock=clock,
    )


def _compile_authority(
    *,
    user: UserContext,
    request_id: str,
    run_id: str,
    intent_digest: str,
    facts: StripePaymentFacts,
    now: datetime,
    planner: object,
) -> StripeM6Compilation:
    scope = CapabilityDataScope(
        department_id=PAYMENTS_DEPARTMENT_ID,
        business_line_id=BUSINESS_LINE_ID,
        resource_ids={facts.payment_intent_id},
    )
    return compile_stripe_request(
        natural_language_request=f"Process one authorized Stripe payment intent token {intent_digest}",
        context=StripeM6TrustedContext(
            request_id=request_id,
            task_id=run_id,
            contract_id="contract_m10_" + _digest([run_id, "root-contract"]),
            tenant_id=user.org_id,
            user=user,
            data_scope=scope,
            resolved_at=now,
        ),
        installation=build_stripe_installation(
            tenant_id=user.org_id,
            accepted_at=now - timedelta(seconds=1),
            expires_at=now + timedelta(minutes=20),
            contract_digest=STRIPE_RUNTIME_CONTRACT.manifest_digest,
        ),
        conformance_report=build_stripe_conformance_attestation(),
        planner=planner,
    )


def _admission_bundle(
    *,
    user: UserContext,
    facts: StripePaymentFacts,
    authority: StripeM6Compilation,
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
        resource_refs={facts.payment_intent_id},
        user_intent_summary=f"Authorized stripe intent token {intent_digest}",
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
            capability_ids=STRIPE_RUNTIME_CONTRACT.capability_ids,
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
