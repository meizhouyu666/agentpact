"""Authority-minimized model planning and deterministic M9 evaluation boundary."""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from enum import Enum, StrEnum
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from enterprise.agent.constrained_planner import (
    OpenAICompatiblePlanner,
    PlannerObservation,
    PlannerProviderError,
    PlannerUsage,
)
from enterprise.agent.work_orders import RecoveryLevel
from enterprise.domains.synthetic_payment.m6_runtime import SyntheticM6Compilation
from enterprise.domains.synthetic_payment.m8_runtime import (
    SyntheticM8Compilation,
    build_replacement_suffix,
    build_synthetic_m8_compilation,
)
from enterprise.domains.synthetic_payment.models import PaymentFacts


class M9StepRole(StrEnum):
    PRECHECK = "precheck"
    SUBMIT = "submit"
    CONFIRM = "confirm"


class M9PlannerCode(StrEnum):
    MALFORMED_JSON = "MALFORMED_JSON"
    MISSING_REQUIRED_FIELD = "MISSING_REQUIRED_FIELD"
    WRONG_VALUE_TYPE = "WRONG_VALUE_TYPE"
    INVALID_STEP_ROLE = "INVALID_STEP_ROLE"
    UNRECOGNIZED_NON_AUTHORITY_FIELD = "UNRECOGNIZED_NON_AUTHORITY_FIELD"
    FORBIDDEN_AUTHORITY_FIELD = "FORBIDDEN_AUTHORITY_FIELD"
    CAPABILITY_NOT_PROJECTED = "CAPABILITY_NOT_PROJECTED"
    UNDECLARED_INPUT_SLOT = "UNDECLARED_INPUT_SLOT"
    ILLEGAL_STEP_SEQUENCE = "ILLEGAL_STEP_SEQUENCE"
    INPUT_SCOPE_EXPANSION = "INPUT_SCOPE_EXPANSION"
    PREFIX_MUTATION = "PREFIX_MUTATION"
    UNKNOWN_STATE = "UNKNOWN_STATE"
    L4_REAUTHORIZATION_REQUIRED = "L4_REAUTHORIZATION_REQUIRED"
    REPLAN_BUDGET_EXHAUSTED = "REPLAN_BUDGET_EXHAUSTED"
    STALE_PROJECTION = "STALE_PROJECTION"
    NO_EXECUTABLE_CAPABILITY = "NO_EXECUTABLE_CAPABILITY"
    PROVIDER_FAILURE = "PROVIDER_FAILURE"


class M9PlannerDisposition(StrEnum):
    ACCEPTED = "accepted"
    REPAIRED = "repaired"
    REJECTED = "rejected"


class AgentEvalMetric(StrEnum):
    PLAN_SCHEMA_VALIDITY = "plan_schema_validity_rate"
    CAPABILITY_SELECTION_ACCURACY = "capability_selection_accuracy"
    AUTHORITY_COMPLIANCE = "authority_compliance_rate"
    LEGAL_REPLAN_ACCEPTANCE = "legal_replan_acceptance_rate"
    HALLUCINATION_REJECTION = "hallucination_rejection_rate"
    REPAIR_SUCCESS = "repair_success_rate"


class InputSlotMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    primitive_type: str = Field(pattern=r"^(string|integer|number|boolean)$")
    required: bool
    non_sensitive_constraints: tuple[str, ...] = ()


class M9CapabilityMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    capability_id: str = Field(min_length=1)
    capability_version: str = Field(min_length=1)
    description: str = Field(min_length=1)
    input_slots: tuple[InputSlotMetadata, ...] = Field(min_length=1)
    step_roles: tuple[M9StepRole, ...] = tuple(M9StepRole)


class RedactedEvidenceToken(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mismatch_code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{2,63}$")
    step_role: M9StepRole
    content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class M9PlanInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    intent_summary: str = Field(min_length=1)
    capabilities: tuple[M9CapabilityMetadata, ...] = Field(min_length=1)
    input_slots: tuple[InputSlotMetadata, ...] = Field(min_length=1)
    allowed_step_roles: tuple[M9StepRole, ...] = tuple(M9StepRole)


class M9ReplanInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    intent_summary: str = Field(min_length=1)
    capabilities: tuple[M9CapabilityMetadata, ...] = Field(min_length=1)
    input_slots: tuple[InputSlotMetadata, ...] = Field(min_length=1)
    completed_prefix_digests: tuple[str, ...]
    allowed_remaining_step_roles: tuple[M9StepRole, ...] = Field(min_length=1)
    remaining_replans: int = Field(ge=1)
    evidence_tokens: tuple[RedactedEvidenceToken, ...] = Field(min_length=1)


class PlanProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    capability_id: str = Field(min_length=1)
    input_slots: tuple[str, ...] = Field(min_length=1)
    step_roles: tuple[M9StepRole, ...] = Field(min_length=2, max_length=4)


class SuffixReplanProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    step_roles: tuple[M9StepRole, ...] = Field(min_length=1, max_length=3)


class M9RepairRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    safe_input: dict[str, Any]
    expected_response_schema: dict[str, Any]
    structural_codes: tuple[M9PlannerCode, ...] = Field(min_length=1, max_length=1)


class M9ProviderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    safe_input: dict[str, Any]
    repair: M9RepairRequest | None = None


class M9ProviderResponse(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid", frozen=True)

    output: Any
    usage: PlannerUsage | None = None


class M9PlannerDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    disposition: M9PlannerDisposition
    codes: tuple[M9PlannerCode, ...] = ()
    proposal: PlanProposal | SuffixReplanProposal | None = None
    provider_calls: int = Field(ge=0, le=2)
    repair_count: int = Field(ge=0, le=1)
    observation: PlannerObservation


class M9ReplanPreconditions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    attempt_unknown: bool = False
    required_recovery_level: RecoveryLevel = RecoveryLevel.L3
    remaining_replans: int = Field(default=1, ge=0)
    projection_fresh: bool = True
    executable_capability: bool = True


class AgentEvalCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str = Field(min_length=1)
    kind: str = Field(pattern=r"^(plan|replan)$")
    provider_outputs: tuple[Any, ...] = ()
    expected_disposition: M9PlannerDisposition
    expected_codes: tuple[M9PlannerCode, ...] = ()
    metric_labels: tuple[AgentEvalMetric, ...] = Field(min_length=1)
    preconditions: M9ReplanPreconditions | None = None


class AgentEvalCaseResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    kind: Literal["plan", "replan"]
    expected_disposition: M9PlannerDisposition
    actual_disposition: M9PlannerDisposition
    expected_codes: tuple[M9PlannerCode, ...] = ()
    actual_codes: tuple[M9PlannerCode, ...] = ()
    provider_calls: int = Field(ge=0, le=2)
    repair_count: int = Field(ge=0, le=1)
    trusted_compile_result: Literal["accepted", "rejected", "not_applicable", "not_exercised"]
    passed: bool


class AgentEvalReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["agentpact-agent-eval/v2"] = "agentpact-agent-eval/v2"
    case_count: int = Field(ge=1)
    passed_case_count: int = Field(ge=0)
    rejected_case_count: int = Field(ge=0)
    plan_schema_validity_rate: float = Field(ge=0, le=1)
    capability_selection_accuracy: float = Field(ge=0, le=1)
    authority_compliance_rate: float = Field(ge=0, le=1)
    legal_replan_acceptance_rate: float = Field(ge=0, le=1)
    hallucination_rejection_rate: float = Field(ge=0, le=1)
    repair_success_rate: float = Field(ge=0, le=1)
    cases: tuple[AgentEvalCaseResult, ...]
    limitations: tuple[str, ...]
    report_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class M9PlannerProvider(Protocol):
    def propose(
        self,
        planner_input: M9PlanInput | M9ReplanInput,
        *,
        repair: M9RepairRequest | None,
        response_model: type[BaseModel],
    ) -> object: ...


class RecordedM9Provider:
    """Credential-free provider that returns recorded untrusted responses in order."""

    def __init__(self, outputs: tuple[Any, ...] | list[Any]) -> None:
        self._outputs = list(outputs)
        self.calls: list[M9ProviderRequest] = []

    def propose(
        self,
        planner_input: M9PlanInput | M9ReplanInput,
        *,
        repair: M9RepairRequest | None,
        response_model: type[BaseModel],
    ) -> object:
        del response_model
        self.calls.append(
            M9ProviderRequest(safe_input=planner_input.model_dump(mode="json"), repair=repair)
        )
        if not self._outputs:
            raise PlannerProviderError("Recorded M9 provider has no response for this invocation")
        return self._outputs.pop(0)


class OpenAICompatibleM9Provider:
    """Reuse the M6 injected OpenAI-compatible strict-output transport for M9."""

    def __init__(self, planner: OpenAICompatiblePlanner) -> None:
        self._planner = planner

    def propose(
        self,
        planner_input: M9PlanInput | M9ReplanInput,
        *,
        repair: M9RepairRequest | None,
        response_model: type[BaseModel],
    ) -> object:
        request = M9ProviderRequest(
            safe_input=planner_input.model_dump(mode="json"),
            repair=repair,
        )
        usage: list[PlannerUsage] = []
        output = self._planner.propose_structured(
            request,
            response_model=response_model,
            schema_name=(
                "authority_minimized_plan_proposal"
                if response_model is PlanProposal
                else "authority_minimized_suffix_replan"
            ),
            system_prompt=(
                "Return only the requested finite capability/input-slot/step-role shape. "
                "Never emit business values, authority identifiers, policy fields, browser fields, or raw evidence."
            ),
            usage_callback=usage.append,
        )
        return M9ProviderResponse(output=output, usage=usage[0] if usage else None)


class M9PlannerEngine:
    """Apply terminal-first validation and at most one structural repair."""

    def __init__(
        self,
        provider: M9PlannerProvider,
        *,
        provider_mode: Literal["recorded", "live"] = "recorded",
    ) -> None:
        self._provider = provider
        self._provider_mode = provider_mode

    def plan(self, planner_input: M9PlanInput) -> M9PlannerDecision:
        return self._run(planner_input, PlanProposal)

    def replan(
        self,
        planner_input: M9ReplanInput,
        *,
        preconditions: M9ReplanPreconditions,
    ) -> M9PlannerDecision:
        denial = _replan_precondition_denial(planner_input, preconditions)
        if denial is not None:
            return self._decision(M9PlannerDisposition.REJECTED, codes=(denial,))
        return self._run(planner_input, SuffixReplanProposal)

    def _run(
        self,
        planner_input: M9PlanInput | M9ReplanInput,
        response_model: type[PlanProposal] | type[SuffixReplanProposal],
    ) -> M9PlannerDecision:
        started = time.monotonic()
        provider_duration = 0.0
        calls = 0
        usage = PlannerUsage()

        def invoke(*, repair: M9RepairRequest | None) -> object:
            nonlocal calls, provider_duration, usage
            calls += 1
            provider_started = time.monotonic()
            try:
                response = self._provider.propose(planner_input, repair=repair, response_model=response_model)
            finally:
                provider_duration += time.monotonic() - provider_started
            if isinstance(response, M9ProviderResponse):
                usage = _merge_usage(usage, response.usage)
                return response.output
            return response

        def decision(
            disposition: M9PlannerDisposition,
            *,
            codes: tuple[M9PlannerCode, ...] = (),
            proposal: PlanProposal | SuffixReplanProposal | None = None,
            repair_count: int = 0,
        ) -> M9PlannerDecision:
            duration = time.monotonic() - started
            return self._decision(
                disposition,
                codes=codes,
                proposal=proposal,
                provider_calls=calls,
                repair_count=repair_count,
                duration_ms=duration * 1000,
                provider_duration_ms=provider_duration * 1000,
                usage=usage,
            )

        try:
            raw = invoke(repair=None)
        except Exception:
            return decision(M9PlannerDisposition.REJECTED, codes=(M9PlannerCode.PROVIDER_FAILURE,))
        try:
            proposal = _validate_candidate(raw, response_model=response_model, planner_input=planner_input)
        except _ValidationFailure as first:
            if not first.repairable:
                return decision(M9PlannerDisposition.REJECTED, codes=(first.code,))
            repair = M9RepairRequest(
                safe_input=planner_input.model_dump(mode="json"),
                expected_response_schema=response_model.model_json_schema(),
                structural_codes=(first.code,),
            )
            try:
                raw = invoke(repair=repair)
            except Exception:
                return decision(
                    M9PlannerDisposition.REJECTED,
                    codes=(M9PlannerCode.PROVIDER_FAILURE,),
                    repair_count=1,
                )
            try:
                proposal = _validate_candidate(
                    raw,
                    response_model=response_model,
                    planner_input=planner_input,
                )
            except _ValidationFailure as second:
                return decision(M9PlannerDisposition.REJECTED, codes=(second.code,), repair_count=1)
            return decision(
                M9PlannerDisposition.REPAIRED,
                proposal=proposal,
                repair_count=1,
            )
        return decision(M9PlannerDisposition.ACCEPTED, proposal=proposal)

    def _decision(
        self,
        disposition: M9PlannerDisposition,
        *,
        codes: tuple[M9PlannerCode, ...] = (),
        proposal: PlanProposal | SuffixReplanProposal | None = None,
        provider_calls: int = 0,
        repair_count: int = 0,
        duration_ms: float = 0.0,
        provider_duration_ms: float = 0.0,
        usage: PlannerUsage | None = None,
    ) -> M9PlannerDecision:
        live = self._provider_mode == "live"
        recorded_usage = usage if usage and any(value is not None for value in usage.model_dump().values()) else None
        observation = PlannerObservation(
            provider_mode=self._provider_mode,
            disposition=disposition.value,
            codes=tuple(item.value for item in codes),
            provider_calls=provider_calls,
            repair_count=repair_count,
            duration_ms=duration_ms if live else None,
            provider_duration_ms=provider_duration_ms if live else None,
            usage=recorded_usage,
        )
        return M9PlannerDecision(
            disposition=disposition,
            codes=codes,
            proposal=proposal,
            provider_calls=provider_calls,
            repair_count=repair_count,
            observation=observation,
        )


class _ValidationFailure(ValueError):
    def __init__(self, code: M9PlannerCode, *, repairable: bool) -> None:
        super().__init__(code.value)
        self.code = code
        self.repairable = repairable


def _normalize_property_name(value: str) -> str:
    return "".join(character for character in value.lower() if character.isalnum())


def _reserved_property_aliases(*stems: str) -> set[str]:
    aliases: set[str] = set()
    for stem in stems:
        parts = stem.split("_")
        plural_last = "policies" if parts[-1] == "policy" else f"{parts[-1]}s"
        plural = "_".join((*parts[:-1], plural_last))
        for value in (
            stem,
            plural,
            f"{stem}_id",
            f"{stem}_ids",
            f"{plural}_id",
            f"{plural}_ids",
            f"{stem}_ref",
            f"{stem}_refs",
            f"{plural}_ref",
            f"{plural}_refs",
        ):
            aliases.add(_normalize_property_name(value))
    return aliases


_STRUCTURAL_CODES = {
    M9PlannerCode.MALFORMED_JSON,
    M9PlannerCode.MISSING_REQUIRED_FIELD,
    M9PlannerCode.WRONG_VALUE_TYPE,
    M9PlannerCode.INVALID_STEP_ROLE,
    M9PlannerCode.UNRECOGNIZED_NON_AUTHORITY_FIELD,
}

_FORBIDDEN_PROPERTY_NAMES = _reserved_property_aliases(
    "grant",
    "contract",
    "authority_contract",
    "task",
    "native_task",
    "step",
    "native_step",
    "work_order",
    "permit",
    "attempt",
    "policy",
    "policy_decision",
    "policy_version",
    "adapter",
    "adapter_ref",
    "probe",
    "probe_ref",
    "result_probe",
    "result_probe_ref",
    "browser",
    "browser_action",
    "action",
    "locator",
    "selector",
    "coordinate",
    "javascript",
    "html",
    "screenshot",
    "rawbrowser",
    "raw_browser",
    "credential",
    "authority",
) | {
    "tenant",
    "tenantid",
    "tenantids",
    "principal",
    "principalid",
    "principalids",
    "authorization",
    "nativeid",
    "nativeids",
}

_SEMANTIC_PROPERTY_CODES = {
    "businessinputs": M9PlannerCode.INPUT_SCOPE_EXPANSION,
    "inputvalues": M9PlannerCode.INPUT_SCOPE_EXPANSION,
    "actualvalues": M9PlannerCode.INPUT_SCOPE_EXPANSION,
    "datascope": M9PlannerCode.INPUT_SCOPE_EXPANSION,
    "scope": M9PlannerCode.INPUT_SCOPE_EXPANSION,
    "inputscope": M9PlannerCode.INPUT_SCOPE_EXPANSION,
    "capabilities": M9PlannerCode.INPUT_SCOPE_EXPANSION,
    "capabilityids": M9PlannerCode.INPUT_SCOPE_EXPANSION,
    "newcapability": M9PlannerCode.INPUT_SCOPE_EXPANSION,
    "inputslotvalues": M9PlannerCode.INPUT_SCOPE_EXPANSION,
    "slots": M9PlannerCode.INPUT_SCOPE_EXPANSION,
    "values": M9PlannerCode.INPUT_SCOPE_EXPANSION,
    "businessvalue": M9PlannerCode.INPUT_SCOPE_EXPANSION,
    "rawevidence": M9PlannerCode.INPUT_SCOPE_EXPANSION,
    "evidence": M9PlannerCode.INPUT_SCOPE_EXPANSION,
    "evidencetokens": M9PlannerCode.INPUT_SCOPE_EXPANSION,
    "paymentid": M9PlannerCode.INPUT_SCOPE_EXPANSION,
    "beneficiaryid": M9PlannerCode.INPUT_SCOPE_EXPANSION,
    "amount": M9PlannerCode.INPUT_SCOPE_EXPANSION,
    "currency": M9PlannerCode.INPUT_SCOPE_EXPANSION,
    "reference": M9PlannerCode.INPUT_SCOPE_EXPANSION,
    "objectversion": M9PlannerCode.INPUT_SCOPE_EXPANSION,
    "completedprefix": M9PlannerCode.PREFIX_MUTATION,
    "completedprefixdigests": M9PlannerCode.PREFIX_MUTATION,
}

_QUOTED_PROPERTY = re.compile(r'"((?:[^"\\]|\\.)+)"\s*:')


def _validate_candidate(
    raw: object,
    *,
    response_model: type[PlanProposal] | type[SuffixReplanProposal],
    planner_input: M9PlanInput | M9ReplanInput,
) -> PlanProposal | SuffixReplanProposal:
    names = _property_names(raw)
    normalized_names = {_normalize_property_name(name) for name in names}
    if normalized_names & _FORBIDDEN_PROPERTY_NAMES:
        raise _ValidationFailure(M9PlannerCode.FORBIDDEN_AUTHORITY_FIELD, repairable=False)
    for name in sorted(normalized_names):
        if name in _SEMANTIC_PROPERTY_CODES:
            raise _ValidationFailure(_SEMANTIC_PROPERTY_CODES[name], repairable=False)
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise _ValidationFailure(M9PlannerCode.MALFORMED_JSON, repairable=True) from exc
    else:
        value = raw
    if not isinstance(value, Mapping):
        raise _ValidationFailure(M9PlannerCode.WRONG_VALUE_TYPE, repairable=True)

    if response_model is PlanProposal:
        _validate_plan_semantics(value, planner_input)
        allowed_fields = {"capability_id", "input_slots", "step_roles"}
    else:
        _validate_replan_semantics(value, planner_input)
        allowed_fields = {"step_roles"}
    extras = set(value) - allowed_fields
    if extras:
        raise _ValidationFailure(M9PlannerCode.UNRECOGNIZED_NON_AUTHORITY_FIELD, repairable=True)
    missing = allowed_fields - set(value)
    if missing:
        raise _ValidationFailure(M9PlannerCode.MISSING_REQUIRED_FIELD, repairable=True)
    try:
        return response_model.model_validate(value)
    except ValidationError as exc:
        code = _pydantic_structural_code(exc)
        raise _ValidationFailure(code, repairable=code in _STRUCTURAL_CODES) from exc


def _validate_plan_semantics(value: Mapping[str, Any], planner_input: M9PlanInput | M9ReplanInput) -> None:
    if not isinstance(planner_input, M9PlanInput):
        raise TypeError("Plan validation requires M9PlanInput")
    capability_id = value.get("capability_id")
    if isinstance(capability_id, str) and capability_id not in {
        item.capability_id for item in planner_input.capabilities
    }:
        raise _ValidationFailure(M9PlannerCode.CAPABILITY_NOT_PROJECTED, repairable=False)
    input_slots = value.get("input_slots")
    if isinstance(input_slots, list | tuple) and all(isinstance(item, str) for item in input_slots):
        expected = {item.name for item in planner_input.input_slots}
        if len(input_slots) != len(set(input_slots)) or set(input_slots) != expected:
            raise _ValidationFailure(M9PlannerCode.UNDECLARED_INPUT_SLOT, repairable=False)
    _validate_step_role_semantics(value.get("step_roles"), expected=None)


def _validate_replan_semantics(value: Mapping[str, Any], planner_input: M9PlanInput | M9ReplanInput) -> None:
    if not isinstance(planner_input, M9ReplanInput):
        raise TypeError("Replan validation requires M9ReplanInput")
    _validate_step_role_semantics(
        value.get("step_roles"),
        expected=tuple(item.value for item in planner_input.allowed_remaining_step_roles),
    )


def _validate_step_role_semantics(value: object, *, expected: tuple[str, ...] | None) -> None:
    if not isinstance(value, list | tuple) or not all(isinstance(item, str) for item in value):
        return
    allowed = {item.value for item in M9StepRole}
    if any(item not in allowed for item in value):
        return
    roles = tuple(value)
    if expected is not None:
        if roles != expected:
            raise _ValidationFailure(M9PlannerCode.ILLEGAL_STEP_SEQUENCE, repairable=False)
        return
    if (
        not 2 <= len(roles) <= 4
        or roles.count(M9StepRole.SUBMIT.value) != 1
        or roles[-1] != M9StepRole.CONFIRM.value
        or any(item != M9StepRole.PRECHECK.value for item in roles[: roles.index(M9StepRole.SUBMIT.value)])
        or any(item != M9StepRole.CONFIRM.value for item in roles[roles.index(M9StepRole.SUBMIT.value) + 1 :])
    ):
        raise _ValidationFailure(M9PlannerCode.ILLEGAL_STEP_SEQUENCE, repairable=False)


def _pydantic_structural_code(exc: ValidationError) -> M9PlannerCode:
    kinds = {item["type"] for item in exc.errors()}
    if any("enum" in item or "literal" in item for item in kinds):
        return M9PlannerCode.INVALID_STEP_ROLE
    if any(item == "missing" for item in kinds):
        return M9PlannerCode.MISSING_REQUIRED_FIELD
    return M9PlannerCode.WRONG_VALUE_TYPE


def _property_names(raw: object) -> set[str]:
    if isinstance(raw, str):
        return {bytes(item, "utf-8").decode("unicode_escape") for item in _QUOTED_PROPERTY.findall(raw)}
    names: set[str] = set()

    def visit(value: object) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                names.add(str(key))
                visit(item)
        elif isinstance(value, list | tuple):
            for item in value:
                visit(item)

    visit(raw)
    return names


def _replan_precondition_denial(
    planner_input: M9ReplanInput,
    preconditions: M9ReplanPreconditions,
) -> M9PlannerCode | None:
    if preconditions.attempt_unknown:
        return M9PlannerCode.UNKNOWN_STATE
    if preconditions.required_recovery_level is RecoveryLevel.L4:
        return M9PlannerCode.L4_REAUTHORIZATION_REQUIRED
    if preconditions.remaining_replans <= 0:
        return M9PlannerCode.REPLAN_BUDGET_EXHAUSTED
    if not preconditions.projection_fresh or planner_input.remaining_replans != preconditions.remaining_replans:
        return M9PlannerCode.STALE_PROJECTION
    if not preconditions.executable_capability or not planner_input.capabilities:
        return M9PlannerCode.NO_EXECUTABLE_CAPABILITY
    return None


def _merge_usage(current: PlannerUsage, update: PlannerUsage | None) -> PlannerUsage:
    if update is None:
        return current

    def add(left: int | None, right: int | None) -> int | None:
        if left is None and right is None:
            return None
        return (left or 0) + (right or 0)

    return PlannerUsage(
        prompt_tokens=add(current.prompt_tokens, update.prompt_tokens),
        completion_tokens=add(current.completion_tokens, update.completion_tokens),
        total_tokens=add(current.total_tokens, update.total_tokens),
    )


def build_m9_plan_input(
    authority: SyntheticM6Compilation,
    *,
    intent_summary: str = "Process one approved synthetic payment through governed sequential steps",
) -> M9PlanInput:
    slots = _payment_slot_metadata()
    capabilities = tuple(
        M9CapabilityMetadata(
            capability_id=item.capability_id,
            capability_version=item.capability_version,
            description=item.description,
            input_slots=slots,
        )
        for item in authority.projection
    )
    planner_input = M9PlanInput(
        intent_summary=intent_summary,
        capabilities=capabilities,
        input_slots=slots,
    )
    _require_no_trusted_business_values(planner_input, authority)
    return planner_input


def build_m9_replan_input(
    previous: SyntheticM8Compilation,
    *,
    completed_prefix_length: int,
    remaining_replans: int,
    evidence_tokens: tuple[RedactedEvidenceToken, ...],
) -> M9ReplanInput:
    if completed_prefix_length < 0 or completed_prefix_length >= len(previous.business_plan.steps):
        raise ValueError("M9 Replan requires a non-empty pending suffix")
    plan_input = build_m9_plan_input(previous.authority)
    prefix_digests = tuple(
        _digest(item.model_dump(mode="json"))
        for item in previous.business_plan.steps[:completed_prefix_length]
    )
    roles = tuple(_role_from_work_order(item.navigation_goal) for item in previous.work_orders[completed_prefix_length:])
    return M9ReplanInput(
        intent_summary="Replace only the authorized pending suffix after a redacted business mismatch",
        capabilities=plan_input.capabilities,
        input_slots=plan_input.input_slots,
        completed_prefix_digests=prefix_digests,
        allowed_remaining_step_roles=roles,
        remaining_replans=remaining_replans,
        evidence_tokens=evidence_tokens,
    )


def redact_replan_evidence(
    *,
    mismatch_code: str,
    step_role: M9StepRole,
    raw_evidence: object,
) -> RedactedEvidenceToken:
    return RedactedEvidenceToken(
        mismatch_code=mismatch_code,
        step_role=step_role,
        content_digest=_digest(raw_evidence),
    )


def compile_m9_plan(
    authority: SyntheticM6Compilation,
    proposal: PlanProposal,
    *,
    admission_id: str,
    plan_run_id: str,
) -> SyntheticM8Compilation:
    projected = {item.capability_id for item in authority.projection}
    if proposal.capability_id not in projected:
        raise ValueError(M9PlannerCode.CAPABILITY_NOT_PROJECTED.value)
    trusted_inputs = authority.business_plan.steps[0].inputs
    if len(proposal.input_slots) != len(set(proposal.input_slots)) or set(proposal.input_slots) != set(trusted_inputs):
        raise ValueError(M9PlannerCode.UNDECLARED_INPUT_SLOT.value)
    return build_synthetic_m8_compilation(
        authority,
        admission_id=admission_id,
        plan_run_id=plan_run_id,
        step_roles=tuple(item.value for item in proposal.step_roles),
    )


def compile_m9_replan(
    previous: SyntheticM8Compilation,
    proposal: SuffixReplanProposal,
    *,
    completed_prefix_length: int,
) -> SyntheticM8Compilation:
    return build_replacement_suffix(
        previous,
        completed_prefix_length=completed_prefix_length,
        replacement_roles=tuple(item.value for item in proposal.step_roles),
    )


def load_agent_eval_cases(path: str | Path) -> tuple[AgentEvalCase, ...]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, list):
        raise ValueError("M9 eval fixture corpus must be a JSON array")
    return tuple(AgentEvalCase.model_validate(item) for item in value)


def run_agent_eval(
    cases: tuple[AgentEvalCase, ...],
    *,
    plan_input: M9PlanInput,
    replan_input: M9ReplanInput,
    authority: SyntheticM6Compilation | None = None,
    previous: SyntheticM8Compilation | None = None,
    provider_factory: Any | None = None,
    provider_mode: Literal["recorded", "live"] = "recorded",
) -> AgentEvalReport:
    metric_totals = {item: 0 for item in AgentEvalMetric}
    metric_passes = {item: 0 for item in AgentEvalMetric}
    passed = 0
    rejected = 0
    results: list[AgentEvalCaseResult] = []
    for case in cases:
        provider = provider_factory(case) if provider_factory is not None else RecordedM9Provider(case.provider_outputs)
        engine = M9PlannerEngine(provider, provider_mode=provider_mode)
        if case.kind == "plan":
            decision = engine.plan(plan_input)
        else:
            decision = engine.replan(
                replan_input,
                preconditions=case.preconditions or M9ReplanPreconditions(),
            )
        compile_result: Literal["accepted", "rejected", "not_applicable", "not_exercised"] = "not_applicable"
        if decision.disposition is not M9PlannerDisposition.REJECTED:
            if case.kind == "plan" and authority is not None and isinstance(decision.proposal, PlanProposal):
                try:
                    compile_m9_plan(
                        authority,
                        decision.proposal,
                        admission_id=f"eval-{case.case_id}",
                        plan_run_id=f"eval-{case.case_id}",
                    )
                    compile_result = "accepted"
                except ValueError:
                    compile_result = "rejected"
            elif case.kind == "replan" and previous is not None and isinstance(decision.proposal, SuffixReplanProposal):
                try:
                    compile_m9_replan(previous, decision.proposal, completed_prefix_length=1)
                    compile_result = "accepted"
                except ValueError:
                    compile_result = "rejected"
            else:
                compile_result = "not_exercised"
        matched = decision.disposition is case.expected_disposition and decision.codes == case.expected_codes
        if decision.disposition is not M9PlannerDisposition.REJECTED and compile_result == "rejected":
            matched = False
        passed += int(matched)
        rejected += int(decision.disposition is M9PlannerDisposition.REJECTED)
        results.append(
            AgentEvalCaseResult(
                case_id=case.case_id,
                kind=case.kind,
                expected_disposition=case.expected_disposition,
                actual_disposition=decision.disposition,
                expected_codes=case.expected_codes,
                actual_codes=decision.codes,
                provider_calls=decision.provider_calls,
                repair_count=decision.repair_count,
                trusted_compile_result=compile_result,
                passed=matched,
            )
        )
        for metric in case.metric_labels:
            metric_totals[metric] += 1
            metric_passes[metric] += int(matched)
    rates = {
        metric: (metric_passes[metric] / metric_totals[metric] if metric_totals[metric] else 0.0)
        for metric in AgentEvalMetric
    }
    limitations = (
        "Evaluation covers only the synthetic.payment reference Pack.",
        "Recorded results are deterministic governance checks, not production task-success claims.",
        "Live mode is planning-only and creates no Task, database record, interactive session, or business effect.",
    )
    payload = {
        "schema_version": "agentpact-agent-eval/v2",
        "case_count": len(cases),
        "passed_case_count": passed,
        "rejected_case_count": rejected,
        "plan_schema_validity_rate": rates[AgentEvalMetric.PLAN_SCHEMA_VALIDITY],
        "capability_selection_accuracy": rates[AgentEvalMetric.CAPABILITY_SELECTION_ACCURACY],
        "authority_compliance_rate": rates[AgentEvalMetric.AUTHORITY_COMPLIANCE],
        "legal_replan_acceptance_rate": rates[AgentEvalMetric.LEGAL_REPLAN_ACCEPTANCE],
        "hallucination_rejection_rate": rates[AgentEvalMetric.HALLUCINATION_REJECTION],
        "repair_success_rate": rates[AgentEvalMetric.REPAIR_SUCCESS],
        "cases": [item.model_dump(mode="json") for item in results],
        "limitations": list(limitations),
    }
    return AgentEvalReport(**payload, report_digest=_digest(payload))


def _payment_slot_metadata() -> tuple[InputSlotMetadata, ...]:
    schema = PaymentFacts.model_json_schema()
    required = set(schema.get("required", ()))
    slots: list[InputSlotMetadata] = []
    for name, definition in schema.get("properties", {}).items():
        primitive_type = definition.get("type")
        if primitive_type not in {"string", "integer", "number", "boolean"}:
            primitive_type = "string"
        constraints: list[str] = []
        if definition.get("minLength"):
            constraints.append("non_empty")
        if definition.get("maxLength"):
            constraints.append("bounded_length")
        if "minimum" in definition or "exclusiveMinimum" in definition:
            constraints.append("bounded_numeric")
        if "pattern" in definition:
            constraints.append("validated_format")
        slots.append(
            InputSlotMetadata(
                name=name,
                primitive_type=primitive_type,
                required=name in required,
                non_sensitive_constraints=tuple(constraints),
            )
        )
    return tuple(slots)


def _require_no_trusted_business_values(
    planner_input: M9PlanInput,
    authority: SyntheticM6Compilation,
) -> None:
    intent = planner_input.intent_summary
    for value in _trusted_leaf_values(authority.business_plan.steps[0].inputs):
        if _text_contains_trusted_value(intent, value):
            raise ValueError("M9 model-safe input contains a trusted business value")


def _trusted_leaf_values(value: object) -> tuple[object, ...]:
    leaves: list[object] = []

    def visit(item: object) -> None:
        if isinstance(item, BaseModel):
            visit(item.model_dump(mode="python"))
        elif isinstance(item, Enum):
            visit(item.value)
        elif isinstance(item, Mapping):
            for nested in item.values():
                visit(nested)
        elif isinstance(item, list | tuple | set | frozenset):
            for nested in item:
                visit(nested)
        else:
            leaves.append(item)

    visit(value)
    return tuple(leaves)


def _text_contains_trusted_value(text: str, value: object) -> bool:
    tokens: set[str] = set()
    if value is None:
        tokens.update({"null", "none"})
    elif isinstance(value, bool):
        tokens.update({str(value), json.dumps(value)})
    elif isinstance(value, Decimal):
        tokens.update({str(value), format(value, "f")})
    elif isinstance(value, float | int):
        tokens.update({str(value), repr(value), json.dumps(value)})
    elif isinstance(value, datetime | date):
        tokens.add(value.isoformat())
    else:
        tokens.add(str(value))
    folded = text.casefold()
    for token in sorted((item for item in tokens if item), key=len, reverse=True):
        escaped = re.escape(token.casefold())
        if isinstance(value, str) and not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)", token):
            if token.casefold() in folded:
                return True
        elif re.search(rf"(?<![\w.%-]){escaped}(?![\w.%-])", folded):
            return True
    return False


def _role_from_work_order(navigation_goal: str) -> M9StepRole:
    marker = "M8 governed "
    suffix = " for the admitted synthetic payment"
    if not navigation_goal.startswith(marker) or not navigation_goal.endswith(suffix):
        raise ValueError("M9 cannot derive a finite role from the trusted M8 Work Order")
    return M9StepRole(navigation_goal[len(marker) : -len(suffix)])


def _digest(value: object) -> str:
    canonical = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
