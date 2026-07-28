"""Normative M3 static conformance tests for the synthetic payment reference."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from enterprise.domains.synthetic_payment.constants import (
    CAPABILITY_ID,
    PACK_ID,
    PACK_VERSION,
    RESULT_PROBE_REF,
    RISK_POLICY_REF,
)
from enterprise.domains.synthetic_payment.definition import build_manifest
from enterprise.domains.synthetic_payment.models import PaymentFacts, PaymentStatus
from enterprise.domains.synthetic_payment.sdk_manifest import build_pack_sdk_manifest
from enterprise.governance.capabilities import AuthorizationDimension
from enterprise.governance.domain_pack_contracts import ContractOwnerRole
from enterprise.governance.pack_conformance import (
    PACK_CONFORMANCE_REPORT_SCHEMA_VERSION,
    ConformanceStatus,
    evaluate_static_pack_conformance,
)
from enterprise.governance.pack_sdk import PackEffectClass, PackEvidenceKind, PackSdkManifest

ROOT = Path(__file__).parents[2]
SDK_MANIFEST_PATH = ROOT / "enterprise" / "domains" / "synthetic_payment" / "sdk_manifest.py"
SDK_MANIFEST_MODULE = ".".join(SDK_MANIFEST_PATH.relative_to(ROOT).with_suffix("").parts)


def _candidate() -> dict[str, object]:
    return build_pack_sdk_manifest().model_dump(mode="json")


def _codes(candidate: dict[str, object]) -> set[str]:
    return {violation.code for violation in evaluate_static_pack_conformance(candidate).violations}


def _resolved_import_modules(path: Path, source: str) -> set[str]:
    """Resolve absolute and package-relative imports to dotted module names."""
    package_parts = list(path.relative_to(ROOT).with_suffix("").parts[:-1])
    modules: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
            continue
        if not isinstance(node, ast.ImportFrom):
            continue

        if node.level:
            retained_parts = max(0, len(package_parts) - node.level + 1)
            base_parts = package_parts[:retained_parts]
        else:
            base_parts = []
        if node.module:
            base_parts.extend(node.module.split("."))

        if base_parts:
            modules.add(".".join(base_parts))
        for alias in node.names:
            if alias.name != "*":
                modules.add(".".join((*base_parts, *alias.name.split("."))))

    return modules


def test_synthetic_payment_sdk_manifest_passes_deterministically():
    manifest = build_pack_sdk_manifest()
    from_model = evaluate_static_pack_conformance(manifest)
    from_mapping = evaluate_static_pack_conformance(manifest.model_dump(mode="json"))

    assert isinstance(manifest, PackSdkManifest)
    assert from_model.status is ConformanceStatus.PASS
    assert from_model.schema_version == PACK_CONFORMANCE_REPORT_SCHEMA_VERSION
    assert from_model.candidate_pack_id == PACK_ID
    assert from_model.candidate_pack_version == PACK_VERSION
    assert from_model.manifest_digest == manifest.manifest_digest
    assert not from_model.violations
    assert from_model.model_dump(mode="json") == from_mapping.model_dump(mode="json")
    assert json.dumps(from_model.model_dump(mode="json"), sort_keys=True, separators=(",", ":")) == json.dumps(
        from_mapping.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    )


def test_sdk_manifest_matches_existing_synthetic_contract_shapes():
    sdk_manifest = build_pack_sdk_manifest()
    active_manifest = build_manifest()
    capabilities = {capability.capability_id: capability for capability in sdk_manifest.capabilities}
    read_capability = capabilities["synthetic.payment.read"]
    submit_capability = capabilities[CAPABILITY_ID]
    evidence = {item.evidence_id: item for item in sdk_manifest.evidence_requirements}
    expected_fact_ids = {f"{PACK_ID}.{field_name}" for field_name in (*PaymentFacts.model_fields, "status")}
    active_dimensions = set().union(
        *active_manifest.capabilities[0].access_policy.role_dimensions.values()
    )

    assert sdk_manifest.pack_id == active_manifest.pack_id == PACK_ID
    assert sdk_manifest.pack_version == active_manifest.version == PACK_VERSION
    assert sdk_manifest.contract_catalog_only is True
    assert sdk_manifest.runtime_wiring_eligible is False
    assert {owner.role for owner in sdk_manifest.owner_refs} == set(ContractOwnerRole)
    assert all(owner.owner_ref == f"synthetic-owner:{owner.role.value}" for owner in sdk_manifest.owner_refs)
    assert {fact.fact_id for fact in sdk_manifest.canonical_facts} == expected_fact_ids
    assert {fact.source_ref for fact in sdk_manifest.canonical_facts} == {"synthetic.payment.store/v1"}
    assert read_capability.effect_class is PackEffectClass.READ_ONLY
    assert set(read_capability.authorization_dimensions) == {
        AuthorizationDimension.DISCOVER,
        AuthorizationDimension.READ_RECORD,
    }
    assert submit_capability.effect_class is PackEffectClass.EXTERNAL_WRITE
    assert set(submit_capability.authorization_dimensions) == active_dimensions
    assert submit_capability.result_evidence_id == RESULT_PROBE_REF
    assert submit_capability.approval_policy_ref == RISK_POLICY_REF
    assert RESULT_PROBE_REF in active_manifest.result_probe_refs
    assert evidence[RESULT_PROBE_REF].kind is PackEvidenceKind.RESULT_PROBE
    assert evidence["synthetic.payment.authoritative-read.v1"].kind is PackEvidenceKind.AUTHORITATIVE_READ
    assert sdk_manifest.lifecycle.states == tuple(state.value for state in PaymentStatus)
    assert sdk_manifest.lifecycle.terminal_states == (PaymentStatus.SUBMITTED.value,)
    assert sdk_manifest.lifecycle.transitions[0].source_state == PaymentStatus.DRAFT.value
    assert sdk_manifest.lifecycle.transitions[0].target_state == PaymentStatus.SUBMITTED.value
    assert active_manifest.state_transitions == {"draft": ["submitted"], "submitted": []}
    assert active_manifest.production_eligible is False


def test_read_only_execute_authority_is_denied():
    candidate = _candidate()
    candidate["capabilities"][0]["authorization_dimensions"].append("execute_transition")

    assert "read_only_authority_violation" in _codes(candidate)


@pytest.mark.parametrize("evidence_index", [0, 1])
def test_stale_evidence_policy_is_denied(evidence_index: int):
    candidate = _candidate()
    candidate["evidence_requirements"][evidence_index]["maximum_age_seconds"] = 301

    assert "evidence_freshness_exceeds_ceiling" in _codes(candidate)


def test_unknown_lifecycle_state_is_malformed():
    candidate = _candidate()
    candidate["lifecycle"]["transitions"][0]["target_state"] = "unknown"

    assert "lifecycle_state_unknown" in _codes(candidate)


@pytest.mark.parametrize(
    ("capability_index", "field", "expected_code"),
    [
        (0, "canonical_fact_ids", "reference_missing"),
        (1, "evidence_requirement_ids", "reference_missing"),
    ],
)
def test_incomplete_capability_bindings_fail_closed(capability_index: int, field: str, expected_code: str):
    candidate = _candidate()
    candidate["capabilities"][capability_index][field] = []

    assert expected_code in _codes(candidate)


def test_detached_write_result_probe_fails_closed():
    candidate = _candidate()
    candidate["capabilities"][1]["evidence_requirement_ids"] = [
        "synthetic.payment.authoritative-read.v1"
    ]

    assert "external_write_probe_missing" in _codes(candidate)


def test_offline_builder_has_no_runtime_or_probe_implementation_imports():
    source = SDK_MANIFEST_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)

    forbidden_roots = {
        "aiohttp",
        "alembic",
        "anthropic",
        "fastapi",
        "httpx",
        "litellm",
        "openai",
        "playwright",
        "requests",
        "skyvern",
        "sqlalchemy",
    }
    forbidden_modules = {
        "app",
        "browser_audit",
        "domain_packs",
        "harness",
        "pack_conformance",
        "result_probes",
        "store",
    }
    assert {module.split(".", 1)[0] for module in imported_modules}.isdisjoint(forbidden_roots)
    assert all(module.rsplit(".", 1)[-1] not in forbidden_modules for module in imported_modules)


@pytest.mark.parametrize(
    "source",
    [
        "from .sdk_manifest import build_pack_sdk_manifest",
        "from . import sdk_manifest",
    ],
)
def test_runtime_import_guard_resolves_relative_sdk_adapter_imports(source: str):
    runtime_path = SDK_MANIFEST_PATH.parent / "app.py"

    assert SDK_MANIFEST_MODULE in _resolved_import_modules(runtime_path, source)


def test_sdk_adapter_is_not_reexported_or_imported_by_runtime_modules():
    package_init = (SDK_MANIFEST_PATH.parent / "__init__.py").read_text(encoding="utf-8")
    violations: list[str] = []
    for source_root in (ROOT / "enterprise", ROOT / "skyvern"):
        for path in source_root.rglob("*.py"):
            if path == SDK_MANIFEST_PATH:
                continue
            imported_modules = _resolved_import_modules(path, path.read_text(encoding="utf-8"))
            if SDK_MANIFEST_MODULE in imported_modules:
                violations.append(str(path.relative_to(ROOT)))

    assert "sdk_manifest" not in package_init
    assert "build_pack_sdk_manifest" not in package_init
    assert not violations
