"""Offline governance-chain validation that can never execute a browser action.

The dry-run binds trusted authorization, business-planning, work-order, page
observation, and typed-action policy contracts into redacted evidence.  It is
an interface test and review aid only: it neither issues a permit nor calls an
execution adapter.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal, Protocol
from uuid import uuid4

from pydantic import BaseModel, Field

from enterprise.agent.work_orders import (
    BusinessPlan,
    BusinessPlanStep,
    ExecutionWorkOrder,
    validate_business_plan,
    validate_work_order,
)

from .analysis import normalize_typed_action_type
from .capabilities import CapabilityDataScope, CapabilityGrant, CapabilityGrantSet
from .classification import hmac_fingerprint
from .contracts import (
    ActionIntent,
    ExecutionEffect,
    GovernanceMode,
    ObservationContext,
    PageReadiness,
    PolicyDecision,
    TaskContract,
)
from .governor import GovernanceBatchError, build_governance_batch_plan

MIN_BUSINESS_BINDING_CONFIDENCE = 0.8


class GovernedDryRunError(ValueError):
    """A trusted contract or proposed action crossed the dry-run boundary."""


class CandidateBusinessBinding(BaseModel):
    """Transient Domain-Pack result binding business facts to one candidate.

    The value must be returned by ``BusinessSemanticResolver.derive``. The
    resolver receives only the current candidate and observation, never the
    BusinessPlan or WorkOrder, so it cannot simply echo Plan facts as evidence.
    Raw observed facts are compared in memory and are never copied into the
    dry-run report.
    """

    action_index: int = Field(ge=0)
    capability_id: str
    observed_inputs: dict[str, Any] = Field(default_factory=dict)
    proposed_transition: dict[str, Any] = Field(default_factory=dict)
    fact_sources: dict[str, str] = Field(default_factory=dict)
    extractor_ref: str = Field(min_length=1)
    evidence_refs: list[str] = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)


class BusinessSemanticResolver(Protocol):
    """Pure Domain-Pack adapter that derives facts from current evidence only."""

    def derive(
        self,
        *,
        action_index: int,
        action: Any,
        intent: ActionIntent,
        observation: ObservationContext,
        element: dict[str, Any] | None,
        page_html: str,
    ) -> CandidateBusinessBinding:
        """Derive canonical facts without receiving Plan, Grant, or WorkOrder."""


class DryRunCandidateEvidence(BaseModel):
    """Redacted policy evidence for one candidate; no action payload is retained."""

    action_index: int = Field(ge=0)
    action_fingerprint: str
    operation: str
    effect: ExecutionEffect
    decision: PolicyDecision
    business_binding_required: bool
    business_binding_verified: bool
    business_binding_ref: str | None = None


class GovernedDryRunReport(BaseModel):
    """Versioned evidence that the offline chain was evaluated without execution."""

    schema_version: Literal["phase2-governed-dry-run-v1"] = "phase2-governed-dry-run-v1"
    dry_run_id: str = Field(default_factory=lambda: f"dryrun_{uuid4().hex}")
    generated_at: datetime
    governance_mode: Literal["audit"] = "audit"
    contract_ref: str
    grant_ref: str
    business_plan_ref: str
    business_plan_step_ref: str
    work_order_ref: str
    page_url_ref: str
    observation_hash: str
    candidates: list[DryRunCandidateEvidence] = Field(default_factory=list)
    execution_skipped: Literal[True] = True
    execution_adapter_called: Literal[False] = False
    runtime_wiring_eligible: Literal[False] = False


def run_governed_dry_run(
    *,
    task_contract: TaskContract,
    grants: CapabilityGrantSet,
    business_plan: BusinessPlan,
    business_plan_step: BusinessPlanStep,
    work_order: ExecutionWorkOrder,
    actions: list[Any],
    page_url: str,
    page_html: str,
    element_lookup: dict[str, dict[str, Any]] | None,
    semantic_resolver: BusinessSemanticResolver | None = None,
    hmac_secret: str | bytes | None,
    now: datetime,
    readiness: PageReadiness = PageReadiness.UNKNOWN,
    readiness_confidence: float = 0.0,
) -> GovernedDryRunReport:
    """Validate the pre-enforce chain and return redacted, non-executable evidence.

    Only an audit-mode contract is accepted.  The function intentionally has no
    execution callback argument, so a caller cannot convert a successful
    report into browser execution through this API.
    """

    if not hmac_secret:
        raise GovernedDryRunError("Governed dry-run requires GOVERNANCE_AUDIT_HMAC_SECRET")
    if task_contract.mode is not GovernanceMode.AUDIT:
        raise GovernedDryRunError("Governed dry-run accepts audit-mode TaskContract only")
    if task_contract.expires_at is not None and now >= task_contract.expires_at:
        raise GovernedDryRunError("TaskContract has expired")
    if business_plan.contract_id != task_contract.contract_id:
        raise GovernedDryRunError("BusinessPlan contract_id must match TaskContract contract_id")
    if business_plan.task_id != task_contract.task_id:
        raise GovernedDryRunError("BusinessPlan task_id must match TaskContract task_id")
    for action in actions:
        try:
            action_type = normalize_typed_action_type(
                action.model_dump(mode="json", exclude_none=True).get("action_type")
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise GovernedDryRunError("Governed dry-run requires typed Action candidates") from exc
        if action_type == "unknown":
            raise GovernedDryRunError("Governed dry-run rejected an unsupported typed Action")

    try:
        validate_business_plan(business_plan, grants, now=now)
        validate_work_order(work_order, business_plan, business_plan_step, grants, now=now)
    except ValueError as exc:
        raise GovernedDryRunError(str(exc)) from exc

    grant = grants.require_executable(
        capability_id=business_plan_step.capability_id,
        grant_id=business_plan_step.grant_id,
        now=now,
    )
    _validate_trusted_bindings(task_contract=task_contract, grant=grant, business_plan=business_plan)

    try:
        governance_plan = build_governance_batch_plan(
            task_id=task_contract.task_id,
            step_id=business_plan_step.step_id,
            actions=actions,
            page_url=page_url,
            page_html=page_html,
            element_lookup=element_lookup,
            hmac_secret=hmac_secret,
            readiness=readiness,
            readiness_confidence=readiness_confidence,
            task_contract=task_contract,
            now=now,
        )
    except GovernanceBatchError as exc:
        raise GovernedDryRunError(str(exc)) from exc

    binding_required = bool(business_plan_step.inputs or business_plan_step.expected_transition)
    if binding_required and semantic_resolver is None:
        raise GovernedDryRunError("BusinessPlanStep facts require a Domain Pack semantic resolver")
    candidate_evidence: list[DryRunCandidateEvidence] = []
    for candidate in governance_plan.candidates:
        operation = candidate.intent.operation
        if operation in work_order.prohibited_operations:
            raise GovernedDryRunError(f"Operation {operation} is prohibited by ExecutionWorkOrder")
        if work_order.allowed_operations and operation not in work_order.allowed_operations:
            raise GovernedDryRunError(f"Operation {operation} is outside ExecutionWorkOrder allowed_operations")
        binding = None
        if semantic_resolver is not None:
            action = actions[candidate.action_index]
            element_id = getattr(action, "element_id", None)
            element = (element_lookup or {}).get(str(element_id)) if element_id is not None else None
            try:
                binding = semantic_resolver.derive(
                    action_index=candidate.action_index,
                    action=action,
                    intent=candidate.intent,
                    observation=governance_plan.observation,
                    element=element,
                    page_html=page_html,
                )
            except Exception as exc:
                raise GovernedDryRunError("Domain Pack semantic resolver failed closed") from exc
            if not isinstance(binding, CandidateBusinessBinding):
                raise GovernedDryRunError("Domain Pack semantic resolver returned an invalid binding")
        binding_ref = _verify_semantic_binding(
            binding=binding,
            binding_required=binding_required,
            step=business_plan_step,
            action_index=candidate.action_index,
            actual_target_facts=candidate.intent.extracted_facts,
            action_fingerprint=candidate.intent.action_fingerprint,
            observation_hash=governance_plan.observation.snapshot_hash,
            hmac_secret=hmac_secret,
        )
        candidate_evidence.append(
            DryRunCandidateEvidence(
                action_index=candidate.action_index,
                action_fingerprint=candidate.intent.action_fingerprint,
                operation=operation,
                effect=candidate.intent.effect,
                decision=candidate.decision,
                business_binding_required=binding_required,
                business_binding_verified=binding_ref is not None,
                business_binding_ref=binding_ref,
            )
        )

    return GovernedDryRunReport(
        generated_at=now,
        contract_ref=_opaque_ref("contract", task_contract.contract_id, hmac_secret),
        grant_ref=_opaque_ref("grant", grant.grant_id, hmac_secret),
        business_plan_ref=_opaque_ref("plan", business_plan.plan_id, hmac_secret),
        business_plan_step_ref=_opaque_ref("plan-step", business_plan_step.step_id, hmac_secret),
        work_order_ref=_opaque_ref("work-order", work_order.work_order_id, hmac_secret),
        page_url_ref=_opaque_ref("page-url", page_url, hmac_secret),
        observation_hash=governance_plan.observation.snapshot_hash,
        candidates=candidate_evidence,
    )


def _validate_trusted_bindings(
    *,
    task_contract: TaskContract,
    grant: CapabilityGrant,
    business_plan: BusinessPlan,
) -> None:
    if grant.tenant_id != task_contract.organization_id:
        raise GovernedDryRunError("CapabilityGrant tenant must match TaskContract organization")
    expected_principal = task_contract.initiator_id or task_contract.service_principal_id
    if expected_principal and grant.principal_id != expected_principal:
        raise GovernedDryRunError("CapabilityGrant principal must match TaskContract principal")
    if grant.data_scope != business_plan.data_scope:
        raise GovernedDryRunError("CapabilityGrant data scope must match BusinessPlan data scope")
    if task_contract.department_id and task_contract.department_id != business_plan.data_scope.department_id:
        raise GovernedDryRunError("TaskContract department must match BusinessPlan data scope")
    if task_contract.business_line_id and task_contract.business_line_id != business_plan.data_scope.business_line_id:
        raise GovernedDryRunError("TaskContract business line must match BusinessPlan data scope")
    if task_contract.data_scope:
        unknown_keys = set(task_contract.data_scope) - set(CapabilityDataScope.model_fields)
        if unknown_keys:
            raise GovernedDryRunError(f"TaskContract data_scope contains unsupported fields: {sorted(unknown_keys)}")
        contract_scope = CapabilityDataScope.model_validate(task_contract.data_scope)
        if contract_scope != business_plan.data_scope:
            raise GovernedDryRunError("TaskContract data scope must match BusinessPlan data scope")


def _opaque_ref(kind: str, value: str, secret: str | bytes) -> str:
    return f"{kind}:hmac-sha256:{hmac_fingerprint(value, secret)}"


def _verify_semantic_binding(
    *,
    binding: CandidateBusinessBinding | None,
    binding_required: bool,
    step: BusinessPlanStep,
    action_index: int,
    actual_target_facts: dict[str, Any],
    action_fingerprint: str,
    observation_hash: str,
    hmac_secret: str | bytes,
) -> str | None:
    if binding is None:
        if binding_required:
            raise GovernedDryRunError("BusinessPlanStep facts require a Domain Pack semantic binding")
        return None
    if binding.action_index != action_index:
        raise GovernedDryRunError("Business binding action_index must match the current Action candidate")
    if binding.capability_id != step.capability_id:
        raise GovernedDryRunError("Business binding capability must match BusinessPlanStep capability")
    if binding.confidence < MIN_BUSINESS_BINDING_CONFIDENCE:
        raise GovernedDryRunError("Business binding confidence is below the governed minimum")
    if binding.observed_inputs != step.inputs:
        raise GovernedDryRunError("Business binding inputs do not match BusinessPlanStep inputs")
    if binding.proposed_transition != step.expected_transition:
        raise GovernedDryRunError("Business binding transition does not match BusinessPlanStep expected_transition")
    _verify_fact_sources(binding=binding, actual_target_facts=actual_target_facts)

    binding_payload = {
        "schema": "phase2-candidate-business-binding-v1",
        "binding": binding.model_dump(mode="json"),
        "actual_target_facts": actual_target_facts,
        "action_fingerprint": action_fingerprint,
        "observation_hash": observation_hash,
    }
    canonical = json.dumps(binding_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _opaque_ref("business-binding", canonical, hmac_secret)


def _verify_fact_sources(
    *,
    binding: CandidateBusinessBinding,
    actual_target_facts: dict[str, Any],
) -> None:
    """Prove every canonical binding leaf came from the current Action target."""

    canonical_facts = {
        "inputs": binding.observed_inputs,
        "transition": binding.proposed_transition,
    }
    canonical_leaves = _flatten_fact_leaves(canonical_facts)
    if set(binding.fact_sources) != set(canonical_leaves):
        raise GovernedDryRunError("Business binding fact sources must cover every canonical fact exactly")

    for canonical_path, canonical_value in canonical_leaves.items():
        source_path = binding.fact_sources[canonical_path]
        try:
            observed_value = _resolve_fact_path(actual_target_facts, source_path)
        except (KeyError, TypeError, ValueError) as exc:
            raise GovernedDryRunError("Business binding fact source is absent from the current Action target") from exc
        if observed_value != canonical_value:
            raise GovernedDryRunError("Business binding fact does not match the current Action target")


def _flatten_fact_leaves(value: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(value, dict):
        leaves: dict[str, Any] = {}
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            leaves.update(_flatten_fact_leaves(item, path))
        return leaves
    if not prefix:
        raise ValueError("Canonical fact path cannot be empty")
    return {prefix: value}


def _resolve_fact_path(facts: dict[str, Any], path: str) -> Any:
    if not path or path.startswith(".") or path.endswith("."):
        raise ValueError("Fact source path must be a non-empty dotted path")
    current: Any = facts
    for segment in path.split("."):
        if not segment or not isinstance(current, dict):
            raise TypeError("Fact source path does not resolve through mappings")
        current = current[segment]
    return current
