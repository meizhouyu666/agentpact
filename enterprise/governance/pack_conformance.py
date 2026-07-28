"""Deterministic static conformance checks for offline Pack SDK manifests."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from .capabilities import AuthorizationDimension
from .domain_pack_contracts import UNASSIGNED, ContractOwnerRole
from .pack_sdk import PackEffectClass, PackEvidenceKind, PackSdkManifest

PACK_CONFORMANCE_REPORT_SCHEMA_VERSION = "domain-pack-conformance-report/v1"
PACK_CONFORMANCE_KIT_VERSION = "domain-pack-conformance-kit/v1"


class ConformanceStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"


class ConformanceViolation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    path: str
    message: str


class ConformanceCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    check_id: str
    status: ConformanceStatus
    violation_codes: tuple[str, ...] = ()


class StaticConformanceReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["domain-pack-conformance-report/v1"] = PACK_CONFORMANCE_REPORT_SCHEMA_VERSION
    kit_version: Literal["domain-pack-conformance-kit/v1"] = PACK_CONFORMANCE_KIT_VERSION
    candidate_pack_id: str | None = None
    candidate_pack_version: str | None = None
    manifest_digest: str | None = None
    status: ConformanceStatus
    checks: tuple[ConformanceCheck, ...]
    violations: tuple[ConformanceViolation, ...]


def evaluate_static_pack_conformance(candidate: PackSdkManifest | Mapping[str, Any]) -> StaticConformanceReport:
    """Validate a Pack description without calling any runtime or evidence source."""

    raw_candidate = candidate if isinstance(candidate, Mapping) else None
    candidate_pack_id = _parse_candidate_text(raw_candidate, "pack_id")
    candidate_pack_version = _parse_candidate_text(raw_candidate, "pack_version")

    try:
        manifest = candidate if isinstance(candidate, PackSdkManifest) else PackSdkManifest.model_validate(candidate)
    except (TypeError, ValueError, ValidationError):
        violations = [
            ConformanceViolation(
                code="manifest_parse_error",
                path="$",
                message="Candidate does not match the immutable PackSdkManifest schema.",
            )
        ]
        boundary_violations = _raw_runtime_boundary_violations(raw_candidate)
        violations.extend(boundary_violations)
        checks = [
            _make_check("manifest_parse", violations[:1]),
            _make_check("runtime_boundary", boundary_violations),
        ]
        return StaticConformanceReport(
            candidate_pack_id=candidate_pack_id,
            candidate_pack_version=candidate_pack_version,
            status=ConformanceStatus.FAIL,
            checks=tuple(checks),
            violations=tuple(violations),
        )

    check_results = (
        ("runtime_boundary", _runtime_boundary_violations(manifest)),
        ("owner_roles", _owner_violations(manifest)),
        ("references_and_versions", _reference_violations(manifest)),
        ("evidence_freshness", _freshness_violations(manifest)),
        ("lifecycle", _lifecycle_violations(manifest)),
        ("read_only_capabilities", _read_only_violations(manifest)),
        ("external_write_capabilities", _external_write_violations(manifest)),
    )
    checks = tuple(_make_check(check_id, violations) for check_id, violations in check_results)
    violations = tuple(violation for _, result in check_results for violation in result)
    status = ConformanceStatus.FAIL if violations else ConformanceStatus.PASS
    return StaticConformanceReport(
        candidate_pack_id=manifest.pack_id,
        candidate_pack_version=manifest.pack_version,
        manifest_digest=manifest.manifest_digest,
        status=status,
        checks=checks,
        violations=violations,
    )


def _parse_candidate_text(candidate: Mapping[str, Any] | None, field: str) -> str | None:
    if candidate is None:
        return None
    value = candidate.get(field)
    return value if isinstance(value, str) and value else None


def _raw_runtime_boundary_violations(candidate: Mapping[str, Any] | None) -> list[ConformanceViolation]:
    if candidate is None:
        return []
    violations: list[ConformanceViolation] = []
    if candidate.get("contract_catalog_only") is not True:
        violations.append(
            _violation(
                "runtime_boundary_violation",
                "contract_catalog_only",
                "Pack SDK artifacts must remain contract-catalog-only.",
            )
        )
    if candidate.get("runtime_wiring_eligible") is not False:
        violations.append(
            _violation(
                "runtime_boundary_violation",
                "runtime_wiring_eligible",
                "Pack SDK artifacts must remain ineligible for runtime wiring.",
            )
        )
    return violations


def _runtime_boundary_violations(manifest: PackSdkManifest) -> list[ConformanceViolation]:
    return _raw_runtime_boundary_violations(manifest.model_dump(mode="python"))


def _owner_violations(manifest: PackSdkManifest) -> list[ConformanceViolation]:
    violations: list[ConformanceViolation] = []
    counts = Counter(owner.role for owner in manifest.owner_refs)
    missing = sorted(role.value for role in ContractOwnerRole if counts[role] == 0)
    duplicates = sorted(role.value for role, count in counts.items() if count != 1)
    if missing or duplicates:
        violations.append(
            _violation(
                "owner_roles_incomplete",
                "owner_refs",
                "Every ContractOwnerRole must be declared exactly once.",
            )
        )
    for index, owner in enumerate(manifest.owner_refs):
        if owner.owner_ref == UNASSIGNED:
            violations.append(
                _violation(
                    "owner_unresolved",
                    f"owner_refs.{index}.owner_ref",
                    "Conformant Pack owners must be assigned.",
                )
            )
    return violations


def _reference_violations(manifest: PackSdkManifest) -> list[ConformanceViolation]:
    violations: list[ConformanceViolation] = []
    fact_ids = [fact.fact_id for fact in manifest.canonical_facts]
    evidence_ids = [evidence.evidence_id for evidence in manifest.evidence_requirements]
    transition_ids = [transition.transition_id for transition in manifest.lifecycle.transitions]
    capability_ids = [capability.capability_id for capability in manifest.capabilities]
    for path, values in (
        ("canonical_facts", fact_ids),
        ("evidence_requirements", evidence_ids),
        ("lifecycle.transitions", transition_ids),
        ("capabilities", capability_ids),
    ):
        if len(values) != len(set(values)):
            violations.append(
                _violation("reference_missing", path, "Identifiers must be unique within each manifest collection.")
            )

    known_facts = set(fact_ids)
    known_evidence = set(evidence_ids)
    known_transitions = set(transition_ids)
    for index, fact in enumerate(manifest.canonical_facts):
        if fact.evidence_requirement_id not in known_evidence:
            violations.append(
                _violation(
                    "reference_missing",
                    f"canonical_facts.{index}.evidence_requirement_id",
                    "Canonical fact evidence reference does not resolve.",
                )
            )
    for index, capability in enumerate(manifest.capabilities):
        base = f"capabilities.{index}"
        if capability.pack_version != manifest.pack_version:
            violations.append(
                _violation(
                    "capability_version_mismatch",
                    f"{base}.pack_version",
                    "Capability Pack version must exactly match the manifest Pack version.",
                )
            )
        violations.extend(
            _capability_binding_violations(
                capability.canonical_fact_ids,
                f"{base}.canonical_fact_ids",
                "Capability must bind at least one unique canonical fact.",
            )
        )
        violations.extend(
            _capability_binding_violations(
                capability.evidence_requirement_ids,
                f"{base}.evidence_requirement_ids",
                "Capability must bind at least one unique evidence requirement.",
            )
        )
        violations.extend(
            _missing_collection_references(
                capability.canonical_fact_ids,
                known_facts,
                f"{base}.canonical_fact_ids",
                "Capability canonical fact reference does not resolve.",
            )
        )
        violations.extend(
            _missing_collection_references(
                capability.evidence_requirement_ids,
                known_evidence,
                f"{base}.evidence_requirement_ids",
                "Capability evidence reference does not resolve.",
            )
        )
        if capability.lifecycle_transition_id and capability.lifecycle_transition_id not in known_transitions:
            violations.append(
                _violation(
                    "reference_missing",
                    f"{base}.lifecycle_transition_id",
                    "Capability lifecycle transition reference does not resolve.",
                )
            )
        if capability.result_evidence_id and capability.result_evidence_id not in known_evidence:
            violations.append(
                _violation(
                    "reference_missing",
                    f"{base}.result_evidence_id",
                    "Capability result evidence reference does not resolve.",
                )
            )
    return violations


def _capability_binding_violations(
    references: Sequence[str],
    path: str,
    message: str,
) -> list[ConformanceViolation]:
    if not references or len(references) != len(set(references)):
        return [_violation("reference_missing", path, message)]
    return []


def _missing_collection_references(
    references: Sequence[str],
    known: set[str],
    path: str,
    message: str,
) -> list[ConformanceViolation]:
    return [
        _violation("reference_missing", f"{path}.{index}", message)
        for index, reference in enumerate(references)
        if reference not in known
    ]


def _freshness_violations(manifest: PackSdkManifest) -> list[ConformanceViolation]:
    return [
        _violation(
            "evidence_freshness_exceeds_ceiling",
            f"evidence_requirements.{index}.maximum_age_seconds",
            "Evidence maximum age must not exceed the manifest freshness ceiling.",
        )
        for index, evidence in enumerate(manifest.evidence_requirements)
        if evidence.maximum_age_seconds > manifest.freshness_ceiling_seconds
    ]


def _lifecycle_violations(manifest: PackSdkManifest) -> list[ConformanceViolation]:
    violations: list[ConformanceViolation] = []
    known_states = set(manifest.lifecycle.states)
    for index, terminal_state in enumerate(manifest.lifecycle.terminal_states):
        if terminal_state not in known_states:
            violations.append(
                _violation(
                    "lifecycle_state_unknown",
                    f"lifecycle.terminal_states.{index}",
                    "Lifecycle terminal state is not declared in states.",
                )
            )
    for index, transition in enumerate(manifest.lifecycle.transitions):
        if transition.source_state not in known_states:
            violations.append(
                _violation(
                    "lifecycle_state_unknown",
                    f"lifecycle.transitions.{index}.source_state",
                    "Lifecycle transition source state is not declared.",
                )
            )
        if transition.target_state not in known_states:
            violations.append(
                _violation(
                    "lifecycle_state_unknown",
                    f"lifecycle.transitions.{index}.target_state",
                    "Lifecycle transition target state is not declared.",
                )
            )
    return violations


def _read_only_violations(manifest: PackSdkManifest) -> list[ConformanceViolation]:
    violations: list[ConformanceViolation] = []
    allowed = {AuthorizationDimension.DISCOVER, AuthorizationDimension.READ_RECORD}
    evidence = {item.evidence_id: item for item in manifest.evidence_requirements}
    for index, capability in enumerate(manifest.capabilities):
        if capability.effect_class is not PackEffectClass.READ_ONLY:
            continue
        base = f"capabilities.{index}"
        dimensions = set(capability.authorization_dimensions)
        if not dimensions or not dimensions <= allowed or len(dimensions) != len(capability.authorization_dimensions):
            violations.append(
                _violation(
                    "read_only_authority_violation",
                    f"{base}.authorization_dimensions",
                    "Read-only capabilities may declare only unique discover/read_record authority.",
                )
            )
        if (
            capability.lifecycle_transition_id is not None
            or capability.result_evidence_id is not None
            or capability.approval_policy_ref is not None
            or any(
                evidence_item is not None and evidence_item.kind is not PackEvidenceKind.AUTHORITATIVE_READ
                for evidence_id in capability.evidence_requirement_ids
                if (evidence_item := evidence.get(evidence_id)) is not None
            )
        ):
            violations.append(
                _violation(
                    "read_only_effect_violation",
                    base,
                    "Read-only capabilities cannot declare transition, result-probe, or approval metadata.",
                )
            )
    return violations


def _external_write_violations(manifest: PackSdkManifest) -> list[ConformanceViolation]:
    violations: list[ConformanceViolation] = []
    transitions = {transition.transition_id for transition in manifest.lifecycle.transitions}
    evidence = {item.evidence_id: item for item in manifest.evidence_requirements}
    for index, capability in enumerate(manifest.capabilities):
        if capability.effect_class is not PackEffectClass.EXTERNAL_WRITE:
            continue
        base = f"capabilities.{index}"
        if AuthorizationDimension.EXECUTE_TRANSITION not in capability.authorization_dimensions:
            violations.append(
                _violation(
                    "external_write_authority_missing",
                    f"{base}.authorization_dimensions",
                    "External-write capabilities require explicit execute_transition authority.",
                )
            )
        if not capability.lifecycle_transition_id or capability.lifecycle_transition_id not in transitions:
            violations.append(
                _violation(
                    "external_write_transition_missing",
                    f"{base}.lifecycle_transition_id",
                    "External-write capabilities require a resolved lifecycle transition.",
                )
            )
        result_evidence = evidence.get(capability.result_evidence_id or "")
        if (
            result_evidence is None
            or result_evidence.kind is not PackEvidenceKind.RESULT_PROBE
            or capability.result_evidence_id not in capability.evidence_requirement_ids
        ):
            violations.append(
                _violation(
                    "external_write_probe_missing",
                    f"{base}.result_evidence_id",
                    "External-write capabilities require result_probe evidence.",
                )
            )
        if not capability.approval_policy_ref:
            violations.append(
                _violation(
                    "external_write_approval_policy_missing",
                    f"{base}.approval_policy_ref",
                    "External-write capabilities require an approval-policy reference.",
                )
            )
    return violations


def _make_check(check_id: str, violations: Sequence[ConformanceViolation]) -> ConformanceCheck:
    codes = tuple(dict.fromkeys(violation.code for violation in violations))
    return ConformanceCheck(
        check_id=check_id,
        status=ConformanceStatus.FAIL if violations else ConformanceStatus.PASS,
        violation_codes=codes,
    )


def _violation(code: str, path: str, message: str) -> ConformanceViolation:
    return ConformanceViolation(code=code, path=path, message=message)
