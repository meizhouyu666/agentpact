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
is built around the synthetic M8 journal/checkpoint. This adapter is the
pack-side of the M10 boundary and is registry-conformant; making the service
itself multi-pack is a separate platform refactor. Live browser execution is
not wired into this M10 adapter; ``advance_run`` and ``probe_run`` fail closed
even when an explicit ``StripeHostedCheckoutFlow`` is injected. The standalone
hosted flow is a manual test-mode smoke path until durable Attempt/Permit
recovery is wired.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
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
from enterprise.auth.schemas import DepartmentRole, UserContext
from enterprise.governance.admission import AdmissionAuditRecord, GovernedTaskDraft, TaskAdmissionBundle
from enterprise.governance.capabilities import CapabilityDataScope
from enterprise.governance.creation_snapshot import TaskCreationPath, TrustedTaskCreationSnapshot
from enterprise.governance.models import GovernedTaskAdmissionModel
from enterprise.governance.pack_runtime import (
    ModelSafeRuntimeProjection,
    PackRuntimeBinding,
)

from .accounts import require_stripe_account
from .constants import (
    BUSINESS_LINE_ID,
    CAPABILITY_ID,
    PACK_ID,
    PACK_VERSION,
    PAYMENTS_DEPARTMENT_ID,
    POLICY_VERSION,
)
from .harness import ChallengeState, StripePaymentEnforceHarness
from .live_browser import StripeHostedCheckoutFlow
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

StripePlannerFactory = Callable[[dict[str, object]], object]


class StripeM10NotWired(RuntimeError):
    """Fail-closed marker for missing explicit live M10 composition."""


def derive_stripe_agent_run_id(*, tenant_id: str, request_id: str) -> str:
    return "run_m10_" + _digest(["agentpact-agent-run/v1", tenant_id, request_id])


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
        self._secret = hmac_secret or os.environ.get(M10_HMAC_SECRET_ENV) or "stripe-m10-demo-only-hmac"
        self._provider_mode = provider_mode
        self._provider_factory = provider_factory or build_stripe_provider_factory(provider_mode)
        self._live_browser = live_browser
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._harnesses: dict[str, StripePaymentEnforceHarness] = {}
        self._challenge_ids: dict[str, str] = {}

    @property
    def provider_mode(self) -> Literal["recorded", "live"]:
        return self._provider_mode

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

    def prepare_run(self, **trusted_inputs: Any) -> StripeM10PreparedRun:
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

    def restore_run(self, bundle: TaskAdmissionBundle, *, target_url: str) -> StripeM10PreparedRun:
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

    async def admit_run(self, prepared: object, **trusted_inputs: Any) -> object:
        run = StripeM10PreparedRun.model_validate(prepared)
        if self._session_factory is not None:
            await self._persist_admission(run)
        harness = self._harness_for(run)
        challenge = harness.prepare_submission(
            requester=run.user,
            facts=StripePaymentFacts.model_validate(run.business_inputs),
        )
        self._challenge_ids[run.run_id] = challenge.challenge_id
        pause_handler: StripeM10PauseHandler | None = trusted_inputs.get("pause_handler")
        if pause_handler is not None:
            return await pause_handler(
                prepared=run,
                challenge_id=challenge.challenge_id,
                operation_key=trusted_inputs.get("operation_key"),
            )
        return {"state": ChallengeState.PENDING_APPROVAL.value, "challenge_id": challenge.challenge_id}

    async def advance_run(self, prepared: object, **trusted_inputs: Any) -> object:
        run = StripeM10PreparedRun.model_validate(prepared)
        if self._provider_mode == "live":
            raise StripeM10NotWired(
                "stripe.payment live browser execution is not wired through the governed Attempt/Permit lifecycle; "
                "use the explicit test-mode hosted Checkout smoke command only"
            )
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

    async def probe_run(self, prepared: object, **trusted_inputs: Any) -> object:
        run = StripeM10PreparedRun.model_validate(prepared)
        if self._provider_mode == "live":
            raise StripeM10NotWired(
                "stripe.payment live result probing is not wired to durable Attempt recovery; "
                "use the explicit smoke flow's independent Probe only"
            )
        harness = self._harness_for(run)
        challenge_id = self._challenge_ids[run.run_id]
        resolved = harness.resolve_unknown(challenge_id)
        return {
            "run_id": run.run_id,
            "state": resolved.state.value,
            "probe_status": resolved.result_probe.status.value if resolved.result_probe is not None else None,
            "challenge_id": challenge_id,
        }

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
