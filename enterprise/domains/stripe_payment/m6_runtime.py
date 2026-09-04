"""Stripe test-mode M6 coordinator: installation, constrained compilation,
execution binding, Permit binding, and independent probe resolution.

This module follows the same generic M6 contracts exercised by the Synthetic
test/reference fixture, without importing that fixture or making it a runtime
dependency. The two Stripe-specific differences are architectural:

1. There is no loopback store: the authoritative read and the business-result
   probe both come from the Stripe API (``result_probe.py``).
2. ``probe_submission_outcome`` runs the *independent* probe and classifies the
   governed final state; an unconfirmed outcome must enter UNKNOWN and can
   never be replayed.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator

from enterprise.agent.constrained_planner import (
    ConstrainedPlanner,
    ModelSafeCapability,
    ModelSafePlannerInput,
    PlannerProposal,
    build_model_safe_projection,
    parse_planner_proposal,
    require_projected_capability,
)
from enterprise.agent.work_orders import (
    BusinessPlan,
    BusinessPlanStep,
    ExecutionWorkOrder,
    RecoveryLevel,
    validate_business_plan,
    validate_work_order,
)
from enterprise.auth.schemas import UserContext
from enterprise.governance.capabilities import (
    CapabilityDataScope,
    CapabilityGrant,
    CapabilityGrantSet,
    CapabilityResolutionContext,
    CapabilityResolver,
)
from enterprise.governance.contracts import GovernanceMode, TaskContract
from enterprise.governance.domain_pack_installations import (
    DomainPackInstallation,
    DomainPackInstallationStatus,
    build_active_domain_pack_set,
)
from enterprise.governance.pack_conformance import ConformanceStatus, StaticConformanceReport
from enterprise.governance.pack_runtime import PackRuntimeContract
from enterprise.governance.result_probes import BusinessResultProbe, ResultProbeStatus

from .constants import (
    CAPABILITY_ID,
    PACK_CAPABILITY_IDS,
    PACK_CONFORMANCE_MANIFEST_DIGEST,
    PACK_DISPLAY_NAME,
    PACK_ID,
    PACK_VERSION,
    POLICY_VERSION,
    RESULT_PROBE_REF,
)
from .definition import build_manifest
from .models import AmbiguousSubmissionFailure, StripePaymentFacts

STRIPE_ADAPTER_REF = "stripe.payment.skyvern-locator-adapter.v1"
STRIPE_RUNTIME_CONTRACT = PackRuntimeContract(
    pack_id=PACK_ID,
    pack_version=PACK_VERSION,
    display_name=PACK_DISPLAY_NAME,
    capability_ids=PACK_CAPABILITY_IDS,
    adapter_id="stripe.payment.agent-run-runtime.v1",
    manifest_digest=PACK_CONFORMANCE_MANIFEST_DIGEST,
)


def build_stripe_conformance_attestation() -> StaticConformanceReport:
    """Return the M6-authorized fixed attestation for the accepted offline contract."""

    return StaticConformanceReport(
        candidate_pack_id=PACK_ID,
        candidate_pack_version=PACK_VERSION,
        manifest_digest=PACK_CONFORMANCE_MANIFEST_DIGEST,
        status=ConformanceStatus.PASS,
        checks=(),
        violations=(),
    )


class M6TraceStage(StrEnum):
    REQUEST = "request"
    INSTALLATION = "installation"
    PROJECTION = "projection"
    PROPOSAL = "proposal"
    VALIDATION = "validation"
    TASK_CONTRACT = "task_contract"
    BUSINESS_PLAN = "business_plan"
    WORK_ORDER = "work_order"
    EXECUTION_BINDING = "execution_binding"
    PERMIT = "permit"
    ATTEMPT = "attempt"
    BROWSER_EFFECT = "browser_effect"
    RESULT_PROBE = "result_probe"
    FINAL_STATE = "final_state"


class M6TraceEvent(BaseModel):
    """Deliberately narrow evidence link without payload or browser content."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    stage: M6TraceStage
    artifact_ref: str = Field(min_length=1)
    status: str = Field(min_length=1)
    digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class M6Trace(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "agentpact-m6-trace/v1"
    request_id: str
    events: tuple[M6TraceEvent, ...]


class StripeM6TrustedContext(BaseModel):
    """Server-supplied authority; none of these fields enter the model projection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    contract_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    user: UserContext
    data_scope: CapabilityDataScope
    resolved_at: datetime
    policy_version: str = POLICY_VERSION
    workload_principal_id: str = "stripe_m6_planner_service"

    @model_validator(mode="after")
    def validate_identity_scope(self) -> "StripeM6TrustedContext":
        if self.user.org_id != self.tenant_id:
            raise ValueError("Trusted user tenant does not match the M6 tenant")
        return self


class StripeM6Compilation(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    installation: DomainPackInstallation
    grants: CapabilityGrantSet
    projection: tuple[ModelSafeCapability, ...]
    proposal: PlannerProposal
    task_contract: TaskContract
    business_plan: BusinessPlan
    work_order: ExecutionWorkOrder
    trace: M6Trace


class StripeM6ExecutionBinding(BaseModel):
    """Redacted trusted bridge from compilation to the sole browser executor."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    binding_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    installation_id: str
    compilation_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    proposal_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    business_inputs_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    work_order_id: str
    work_order_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    task_id: str
    contract_id: str
    grant_id: str
    capability_id: str
    result_probe_ref: str
    idempotency_key_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    expires_at: datetime


class StripeM6PermitBinding(BaseModel):
    """Exact Permit identity bound to one previously validated M6 execution bridge."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    execution_binding_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    work_order_id: str
    task_id: str
    contract_id: str
    permit_id: str
    action_fingerprint: str
    idempotency_key_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    bound_at: datetime
    expires_at: datetime


def build_stripe_installation(
    *,
    tenant_id: str,
    accepted_at: datetime,
    expires_at: datetime,
    contract_digest: str,
    status: DomainPackInstallationStatus = DomainPackInstallationStatus.ACCEPTED,
) -> DomainPackInstallation:
    return DomainPackInstallation(
        installation_id=_stable_id(tenant_id, "installation"),
        tenant_id=tenant_id,
        pack_id=PACK_ID,
        pack_version=PACK_VERSION,
        contract_digest=contract_digest,
        enabled_capability_ids=(CAPABILITY_ID,),
        adapter_ref=STRIPE_ADAPTER_REF,
        result_probe_ref=RESULT_PROBE_REF,
        policy_version=POLICY_VERSION,
        status=status,
        accepted_at=accepted_at,
        expires_at=expires_at,
    )


def compile_stripe_request(
    *,
    natural_language_request: str,
    context: StripeM6TrustedContext,
    installation: DomainPackInstallation,
    conformance_report: StaticConformanceReport,
    planner: ConstrainedPlanner,
) -> StripeM6Compilation:
    """Run the constrained Planner and compile exactly one trusted Work Order."""

    active = build_active_domain_pack_set(
        tenant_id=context.tenant_id,
        runtime_manifests=[build_manifest()],
        conformance_reports=[conformance_report],
        installations=[installation],
        expected_policy_versions={PACK_ID: context.policy_version},
        expected_adapter_refs={PACK_ID: STRIPE_ADAPTER_REF},
        now=context.resolved_at,
    )
    if len(active.installations) != 1:
        raise ValueError("Stripe M6 requires one accepted active Domain Pack installation")

    registry = active.registry.capability_registry()
    installed_ids = {item.capability_id for item in registry.definitions()}
    resolved_grants = CapabilityResolver(registry).resolve(
        CapabilityResolutionContext(
            user=context.user,
            tenant_id=context.tenant_id,
            data_scope=context.data_scope,
            installed_capability_ids=installed_ids,
            policy_snapshot_version=context.policy_version,
            resolved_at=context.resolved_at,
            workload_principal_id=context.workload_principal_id,
            revocation_epoch="stripe-m6-epoch-1",
            purpose="stripe_m6_constrained_planning",
        )
    )
    grants = CapabilityGrantSet(
        grants=[
            grant.model_copy(
                update={
                    "grant_id": _stable_id(context.request_id, f"grant-{grant.capability_id}"),
                    "expires_at": min(grant.expires_at, installation.expires_at),
                },
                deep=True,
            )
            for grant in resolved_grants.grants
        ]
    )
    projection = build_model_safe_projection(grants=grants, registry=registry, now=context.resolved_at)
    planner_input = ModelSafePlannerInput(
        natural_language_request=natural_language_request,
        capabilities=projection,
    )
    proposal = parse_planner_proposal(planner.propose(planner_input))
    projected = require_projected_capability(proposal, projection)
    if projected.capability_id != CAPABILITY_ID:
        raise ValueError("Stripe M6 Planner selected an unsupported Capability")
    normalized_inputs = _normalize_business_inputs(proposal.business_inputs)
    grant = _require_single_executable_grant(grants, capability_id=CAPABILITY_ID, now=context.resolved_at)
    definition = registry.require(CAPABILITY_ID)

    contract = TaskContract(
        contract_id=context.contract_id,
        task_id=context.task_id,
        organization_id=context.tenant_id,
        initiator_id=context.user.user_id,
        service_principal_id=context.workload_principal_id,
        department_id=context.data_scope.department_id,
        business_line_id=context.data_scope.business_line_id,
        goal=natural_language_request,
        allowed_operations={CAPABILITY_ID},
        data_scope=context.data_scope.model_dump(mode="json"),
        authorization_snapshot={
            "installation_id": installation.installation_id,
            "contract_digest": installation.contract_digest,
            "grant_id": grant.grant_id,
            "revocation_epoch": grant.revocation_epoch,
        },
        policy_profile=PACK_ID,
        policy_version=context.policy_version,
        success_criteria=["Independent Stripe API result probe confirms one submission"],
        expires_at=grant.expires_at,
        mode=GovernanceMode.AUDIT,
    )
    step = BusinessPlanStep(
        step_id=_stable_id(context.request_id, "plan-step"),
        capability_id=CAPABILITY_ID,
        capability_version=PACK_VERSION,
        grant_id=grant.grant_id,
        contract_id=context.contract_id,
        inputs=normalized_inputs,
        expected_transition=definition.state_transition,
        success_criteria=contract.success_criteria,
    )
    plan = BusinessPlan(
        plan_id=_stable_id(context.request_id, "business-plan"),
        request_id=context.request_id,
        task_id=context.task_id,
        contract_id=context.contract_id,
        data_scope=context.data_scope,
        steps=[step],
    )
    work_order = ExecutionWorkOrder(
        work_order_id=_stable_id(context.request_id, "work-order"),
        business_plan_step_id=step.step_id,
        task_id=context.task_id,
        contract_id=context.contract_id,
        grant_id=grant.grant_id,
        navigation_goal="Submit the approved Stripe test-mode payment exactly once through the governed locator path",
        allowed_operations={"read", "input", "select", "submit"},
        prohibited_operations={"javascript", "coordinate", "download", "upload"},
        success_criteria=contract.success_criteria,
        required_evidence=["permit", "execution_attempt", RESULT_PROBE_REF],
        max_recovery_level=RecoveryLevel.L2,
        result_probe_ref=RESULT_PROBE_REF,
    )
    _validate_contract(contract=contract, context=context, grant=grant, installation=installation)
    validate_business_plan(plan, grants, now=context.resolved_at)
    validate_work_order(work_order, plan, step, grants, now=context.resolved_at)

    trace = M6Trace(
        request_id=context.request_id,
        events=(
            _trace(M6TraceStage.REQUEST, context.request_id, "accepted", natural_language_request),
            M6TraceEvent(
                stage=M6TraceStage.INSTALLATION,
                artifact_ref=installation.installation_id,
                status="accepted",
                digest=installation.contract_digest,
            ),
            _trace(M6TraceStage.PROJECTION, f"projection:{context.request_id}", "executable", projection),
            _trace(M6TraceStage.PROPOSAL, f"proposal:{context.request_id}", "validated", proposal),
            _trace(M6TraceStage.VALIDATION, f"validation:{context.request_id}", "passed", normalized_inputs),
            _trace(M6TraceStage.TASK_CONTRACT, contract.contract_id, "compiled", contract),
            _trace(M6TraceStage.BUSINESS_PLAN, plan.plan_id, "compiled", plan),
            _trace(M6TraceStage.WORK_ORDER, work_order.work_order_id, "compiled", work_order),
        ),
    )
    return StripeM6Compilation(
        installation=installation,
        grants=grants,
        projection=projection,
        proposal=proposal,
        task_contract=contract,
        business_plan=plan,
        work_order=work_order,
        trace=trace,
    )


def append_execution_trace(
    trace: M6Trace,
    *,
    compilation: StripeM6Compilation,
    execution_binding: StripeM6ExecutionBinding,
    permit_binding: StripeM6PermitBinding,
    attempt_id: str,
    attempt_task_id: str,
    attempt_contract_id: str,
    attempt_action_fingerprint: str,
    attempt_idempotency_key: str,
    attempt_state_sequence: tuple[str, ...],
    result_probe_evidence: dict[str, object],
    final_state: str,
    browser_effect_count: int,
) -> M6Trace:
    """Validate and bind exact compiled, Permit, Attempt, effect, and probe evidence."""

    result_probe = result_probe_evidence.get("result_probe")
    observed_business_inputs = result_probe_evidence.get("facts")
    if not isinstance(result_probe, dict) or not isinstance(observed_business_inputs, dict):
        raise ValueError("M6 result-probe evidence must bind probe status and observed business facts")
    if _canonical_digest(trace) != _canonical_digest(compilation.trace):
        raise ValueError("Execution trace does not belong to the supplied M6 compilation")
    expected_binding = bind_compilation_for_execution(
        compilation,
        observed_business_inputs=observed_business_inputs,
        work_order_id=execution_binding.work_order_id,
        now=permit_binding.bound_at,
    )
    if expected_binding != execution_binding:
        raise ValueError("Execution binding does not match the compiled proposal and Work Order")
    if (
        permit_binding.execution_binding_digest != execution_binding.binding_digest
        or permit_binding.work_order_id != execution_binding.work_order_id
        or permit_binding.task_id != execution_binding.task_id
        or permit_binding.contract_id != execution_binding.contract_id
        or permit_binding.idempotency_key_digest != execution_binding.idempotency_key_digest
    ):
        raise ValueError("Permit identity does not match the M6 execution binding")
    if attempt_task_id != execution_binding.task_id or attempt_contract_id != execution_binding.contract_id:
        raise ValueError("Execution Attempt task or contract does not match the M6 execution binding")
    if attempt_action_fingerprint != permit_binding.action_fingerprint:
        raise ValueError("Execution Attempt fingerprint does not match the bound Permit")
    if _canonical_digest(attempt_idempotency_key) != execution_binding.idempotency_key_digest:
        raise ValueError("Execution Attempt idempotency key does not match the compiled business input")
    if attempt_state_sequence != ("executing", "unknown", "confirmed"):
        raise ValueError("Execution Attempt state sequence is not the governed M6 sequence")
    if browser_effect_count != 1:
        raise ValueError("M6 trace requires exactly one correlated browser effect")
    if result_probe.get("status") != "confirmed" or final_state != "confirmed":
        raise ValueError("M6 trace requires an independently confirmed final result")

    return trace.model_copy(
        update={
            "events": trace.events
            + (
                M6TraceEvent(
                    stage=M6TraceStage.EXECUTION_BINDING,
                    artifact_ref=execution_binding.work_order_id,
                    status="validated",
                    digest=execution_binding.binding_digest,
                ),
                _trace(M6TraceStage.PERMIT, permit_binding.permit_id, "consumed", permit_binding),
                _trace(
                    M6TraceStage.ATTEMPT,
                    attempt_id,
                    "unknown_then_confirmed",
                    {
                        "execution_binding_digest": execution_binding.binding_digest,
                        "attempt_id": attempt_id,
                        "task_id": attempt_task_id,
                        "contract_id": attempt_contract_id,
                        "action_fingerprint": attempt_action_fingerprint,
                        "idempotency_key_digest": execution_binding.idempotency_key_digest,
                        "states": attempt_state_sequence,
                    },
                ),
                M6TraceEvent(
                    stage=M6TraceStage.BROWSER_EFFECT,
                    artifact_ref=execution_binding.work_order_id,
                    status=f"committed:{browser_effect_count}",
                    digest=execution_binding.business_inputs_digest,
                ),
                _trace(M6TraceStage.RESULT_PROBE, RESULT_PROBE_REF, "confirmed", result_probe_evidence),
                M6TraceEvent(stage=M6TraceStage.FINAL_STATE, artifact_ref=trace.request_id, status=final_state),
            )
        }
    )


def bind_compilation_for_execution(
    compilation: StripeM6Compilation,
    *,
    observed_business_inputs: dict[str, object],
    work_order_id: str,
    now: datetime,
) -> StripeM6ExecutionBinding:
    """Fail closed before Permit issuance if installation, inputs, or Work Order diverge."""

    grant = _require_live_compilation(compilation, now=now)
    if work_order_id != compilation.work_order.work_order_id:
        raise ValueError("Browser execution Work Order does not match the compiled Work Order")
    observed = _normalize_business_inputs(observed_business_inputs)
    proposal_inputs = _normalize_business_inputs(compilation.proposal.business_inputs)
    step = compilation.business_plan.steps[0]
    if _canonical_digest(observed) != _canonical_digest(proposal_inputs):
        raise ValueError("Browser business inputs do not match the compiled Planner proposal")
    if _canonical_digest(observed) != _canonical_digest(step.inputs):
        raise ValueError("Browser business inputs do not match the compiled BusinessPlan step")
    payment_intent_id = str(observed["payment_intent_id"])
    if payment_intent_id not in compilation.business_plan.data_scope.resource_ids:
        raise ValueError("Compiled payment input is outside the trusted BusinessPlan scope")
    idempotency_key_digest = _canonical_digest(f"stripe:{payment_intent_id}")
    compilation_digest = _canonical_digest(
        {
            "installation_id": compilation.installation.installation_id,
            "contract_digest": compilation.installation.contract_digest,
            "proposal": compilation.proposal.model_dump(mode="json"),
            "task_contract": compilation.task_contract.model_dump(mode="json"),
            "business_plan": compilation.business_plan.model_dump(mode="json"),
            "work_order": compilation.work_order.model_dump(mode="json"),
        }
    )
    values = {
        "installation_id": compilation.installation.installation_id,
        "compilation_digest": compilation_digest,
        "proposal_digest": _canonical_digest(compilation.proposal),
        "business_inputs_digest": _canonical_digest(observed),
        "work_order_id": compilation.work_order.work_order_id,
        "work_order_digest": _canonical_digest(compilation.work_order),
        "task_id": compilation.work_order.task_id,
        "contract_id": compilation.work_order.contract_id,
        "grant_id": grant.grant_id,
        "capability_id": step.capability_id,
        "result_probe_ref": compilation.work_order.result_probe_ref,
        "idempotency_key_digest": idempotency_key_digest,
        "expires_at": grant.expires_at,
    }
    return StripeM6ExecutionBinding(binding_digest=_canonical_digest(values), **values)


def bind_permit_to_execution(
    execution_binding: StripeM6ExecutionBinding,
    *,
    permit_id: str,
    task_id: str,
    contract_id: str,
    action_fingerprint: str,
    idempotency_key: str,
    now: datetime,
) -> StripeM6PermitBinding:
    """Bind the real issued Permit identity to the pre-effect M6 execution binding."""

    if now >= execution_binding.expires_at:
        raise ValueError("M6 execution binding is stale before Permit binding")
    if task_id != execution_binding.task_id or contract_id != execution_binding.contract_id:
        raise ValueError("Permit task or contract does not match the M6 execution binding")
    idempotency_key_digest = _canonical_digest(idempotency_key)
    if idempotency_key_digest != execution_binding.idempotency_key_digest:
        raise ValueError("Permit idempotency key does not match the compiled business input")
    return StripeM6PermitBinding(
        execution_binding_digest=execution_binding.binding_digest,
        work_order_id=execution_binding.work_order_id,
        task_id=task_id,
        contract_id=contract_id,
        permit_id=permit_id,
        action_fingerprint=action_fingerprint,
        idempotency_key_digest=idempotency_key_digest,
        bound_at=now,
        expires_at=execution_binding.expires_at,
    )


def probe_submission_outcome(
    *,
    probe: BusinessResultProbe,
    observed_business_inputs: dict[str, object],
    idempotency_key: str,
) -> tuple[dict[str, object], str]:
    """Run the independent Stripe probe and classify the governed final state.

    Returns ``(result_probe_evidence, final_state)`` in the exact shape
    ``append_execution_trace`` expects. The probe is the ONLY authority that
    may resolve the outcome; a non-confirmed outcome means the governed path
    must persist UNKNOWN and never replay the submission.
    """

    normalized = _normalize_business_inputs(observed_business_inputs)
    payment_intent_id = str(normalized["payment_intent_id"])
    evidence = probe.probe(resource_id=payment_intent_id, idempotency_key=idempotency_key)
    final_state = {
        ResultProbeStatus.CONFIRMED: "confirmed",
        ResultProbeStatus.NOT_CONFIRMED: "not_confirmed",
        ResultProbeStatus.UNKNOWN: "unknown",
    }[evidence.status]
    return (
        {
            "result_probe": {
                "status": evidence.status.value,
                "observed_version": evidence.observed_version,
            },
            "facts": normalized,
        },
        final_state,
    )


def require_confirmed_outcome(final_state: str) -> None:
    """Fail closed: only an independently confirmed probe result may proceed."""

    if final_state != "confirmed":
        raise AmbiguousSubmissionFailure(
            "Stripe submission outcome is not confirmed; the governed path must persist UNKNOWN "
            "and wait for the correlated probe. Replaying the action is forbidden."
        )


def _require_single_executable_grant(
    grants: CapabilityGrantSet,
    *,
    capability_id: str,
    now: datetime,
) -> CapabilityGrant:
    matching = [item for item in grants.executable_grants(now=now) if item.capability_id == capability_id]
    if len(matching) != 1:
        raise ValueError("Trusted compiler requires exactly one executable CapabilityGrant")
    return matching[0]


def _require_live_compilation(compilation: StripeM6Compilation, *, now: datetime) -> CapabilityGrant:
    if compilation.installation.status is not DomainPackInstallationStatus.ACCEPTED:
        raise ValueError("M6 execution requires an accepted Domain Pack installation")
    if not compilation.installation.accepted_at <= now < compilation.installation.expires_at:
        raise ValueError("M6 Domain Pack installation is stale before execution")
    if compilation.task_contract.expires_at is None or now >= compilation.task_contract.expires_at:
        raise ValueError("M6 TaskContract is stale before execution")
    if len(compilation.business_plan.steps) != 1:
        raise ValueError("M6 execution requires exactly one compiled BusinessPlan step")
    step = compilation.business_plan.steps[0]
    grant = compilation.grants.require_executable(
        capability_id=step.capability_id,
        grant_id=step.grant_id,
        now=now,
    )
    if grant.expires_at > compilation.installation.expires_at:
        raise ValueError("M6 CapabilityGrant exceeds its installation expiry")
    if compilation.task_contract.expires_at > compilation.installation.expires_at:
        raise ValueError("M6 TaskContract exceeds its installation expiry")
    validate_business_plan(compilation.business_plan, compilation.grants, now=now)
    validate_work_order(
        compilation.work_order,
        compilation.business_plan,
        step,
        compilation.grants,
        now=now,
    )
    return grant


def _normalize_business_inputs(value: dict[str, object]) -> dict[str, object]:
    unknown_input_fields = set(value) - StripePaymentFacts.model_fields.keys()
    if unknown_input_fields:
        raise ValueError(f"Stripe payment inputs contain unknown fields: {sorted(unknown_input_fields)}")
    return StripePaymentFacts.model_validate(value).model_dump(mode="json")


def _validate_contract(
    *,
    contract: TaskContract,
    context: StripeM6TrustedContext,
    grant: CapabilityGrant,
    installation: DomainPackInstallation,
) -> None:
    if contract.task_id != context.task_id or contract.organization_id != grant.tenant_id:
        raise ValueError("TaskContract identity does not match trusted context and Grant")
    if contract.allowed_operations != {grant.capability_id}:
        raise ValueError("TaskContract operation does not match the executable Grant")
    if contract.data_scope != grant.data_scope.model_dump(mode="json"):
        raise ValueError("TaskContract data scope does not match the executable Grant")
    if contract.policy_version != grant.policy_snapshot_version or contract.policy_version != installation.policy_version:
        raise ValueError("TaskContract policy does not match Grant and installation")
    if contract.expires_at != grant.expires_at:
        raise ValueError("TaskContract expiry does not match the executable Grant")
    if contract.expires_at is None or contract.expires_at > installation.expires_at:
        raise ValueError("TaskContract authority exceeds the Domain Pack installation expiry")


def _trace(stage: M6TraceStage, artifact_ref: str, status: str, value: object) -> M6TraceEvent:
    return M6TraceEvent(stage=stage, artifact_ref=artifact_ref, status=status, digest=_canonical_digest(value))


def _canonical_digest(value: object) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    elif isinstance(value, tuple) and all(isinstance(item, BaseModel) for item in value):
        value = [item.model_dump(mode="json") for item in value]
    canonical = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _stable_id(seed: str, kind: str) -> str:
    return f"m6_{kind.replace('-', '_')}_{uuid5(NAMESPACE_URL, f'{PACK_ID}|{seed}|{kind}').hex}"
