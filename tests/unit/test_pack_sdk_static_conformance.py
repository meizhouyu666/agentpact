"""Acceptance tests for the offline Pack SDK and static Conformance Kit."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from enterprise.governance.pack_conformance import (
    PACK_CONFORMANCE_KIT_VERSION,
    PACK_CONFORMANCE_REPORT_SCHEMA_VERSION,
    ConformanceStatus,
    evaluate_static_pack_conformance,
)
from enterprise.governance.pack_sdk import PACK_SDK_SCHEMA_VERSION, PackSdkManifest

ROOT = Path(__file__).parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "domain_pack_conformance"
NEW_MODULES = {
    ROOT / "enterprise" / "governance" / "pack_sdk.py",
    ROOT / "enterprise" / "governance" / "pack_conformance.py",
}
SYNTHETIC_REFERENCE_ADAPTER = (
    ROOT / "enterprise" / "domains" / "synthetic_payment" / "sdk_manifest.py"
)


def _load_fixture(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _report_bytes(name: str) -> bytes:
    report = evaluate_static_pack_conformance(_load_fixture(name))
    payload = json.dumps(report.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return payload.encode("utf-8")


def _new_module_import_violations(source: str, source_label: str) -> list[str]:
    violations: list[str] = []
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in {
                    "enterprise.governance.pack_sdk",
                    "enterprise.governance.pack_conformance",
                }:
                    violations.append(source_label)
        elif isinstance(node, ast.ImportFrom):
            if node.module in {
                "enterprise.governance.pack_sdk",
                "enterprise.governance.pack_conformance",
                "pack_sdk",
                "pack_conformance",
            }:
                violations.append(source_label)
            if node.module == "enterprise.governance" and any(
                alias.name in {"pack_sdk", "pack_conformance"} for alias in node.names
            ):
                violations.append(source_label)
            if node.level > 0 and node.module is None and any(
                alias.name in {"pack_sdk", "pack_conformance"} for alias in node.names
            ):
                violations.append(source_label)
    return violations


def test_valid_synthetic_reference_passes_with_deterministic_json_report():
    candidate = _load_fixture("valid-synthetic-reference.json")
    manifest = PackSdkManifest.model_validate(candidate)
    first = evaluate_static_pack_conformance(manifest)
    second = evaluate_static_pack_conformance(candidate)

    assert manifest.schema_version == PACK_SDK_SCHEMA_VERSION
    assert manifest.contract_catalog_only is True
    assert manifest.runtime_wiring_eligible is False
    assert len(manifest.manifest_digest) == 64
    assert first.schema_version == PACK_CONFORMANCE_REPORT_SCHEMA_VERSION
    assert first.kit_version == PACK_CONFORMANCE_KIT_VERSION
    assert first.status is ConformanceStatus.PASS
    assert first.candidate_pack_id == "conformance.synthetic.reference"
    assert first.candidate_pack_version == "1.0.0"
    assert first.manifest_digest == manifest.manifest_digest
    assert not first.violations
    assert all(check.status is ConformanceStatus.PASS for check in first.checks)
    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert _report_bytes("valid-synthetic-reference.json") == _report_bytes("valid-synthetic-reference.json")

    with pytest.raises(ValidationError, match="frozen"):
        manifest.display_name = "mutated"
    with pytest.raises(ValidationError, match="frozen"):
        manifest.capabilities[0].display_name = "mutated"


@pytest.mark.parametrize(
    ("fixture_name", "required_code"),
    [
        ("invalid-read-execute.json", "read_only_authority_violation"),
        ("invalid-unresolved-owner.json", "owner_unresolved"),
        ("invalid-evidence-freshness.json", "evidence_freshness_exceeds_ceiling"),
        ("invalid-lifecycle-state.json", "lifecycle_state_unknown"),
        ("invalid-write-result-probe.json", "external_write_probe_missing"),
    ],
)
def test_invalid_fixture_fails_with_required_stable_code(fixture_name: str, required_code: str):
    report = evaluate_static_pack_conformance(_load_fixture(fixture_name))

    assert report.status is ConformanceStatus.FAIL
    assert required_code in {violation.code for violation in report.violations}
    assert report.schema_version == PACK_CONFORMANCE_REPORT_SCHEMA_VERSION
    assert report.manifest_digest is not None


def test_malformed_input_returns_manifest_parse_error_instead_of_raising():
    malformed = _load_fixture("valid-synthetic-reference.json")
    malformed["unexpected_runtime_adapter"] = "not-allowed"

    report = evaluate_static_pack_conformance(malformed)

    assert report.status is ConformanceStatus.FAIL
    assert [violation.code for violation in report.violations] == ["manifest_parse_error"]
    assert report.manifest_digest is None


def test_runtime_boundary_literals_fail_closed_in_the_report():
    candidate = _load_fixture("valid-synthetic-reference.json")
    candidate["contract_catalog_only"] = False
    candidate["runtime_wiring_eligible"] = True

    report = evaluate_static_pack_conformance(candidate)
    codes = [violation.code for violation in report.violations]

    assert report.status is ConformanceStatus.FAIL
    assert codes.count("runtime_boundary_violation") == 2
    assert "manifest_parse_error" in codes

    for field, value in (("contract_catalog_only", 1), ("runtime_wiring_eligible", 0)):
        malformed = _load_fixture("valid-synthetic-reference.json")
        malformed[field] = value
        with pytest.raises(ValidationError):
            PackSdkManifest.model_validate(malformed)


def test_remaining_policy_codes_are_reported_without_raising():
    incomplete_owner = _load_fixture("valid-synthetic-reference.json")
    incomplete_owner["owner_refs"].pop()
    assert "owner_roles_incomplete" in {
        violation.code for violation in evaluate_static_pack_conformance(incomplete_owner).violations
    }

    version_mismatch = _load_fixture("valid-synthetic-reference.json")
    version_mismatch["capabilities"][0]["pack_version"] = "1.0.1"
    assert "capability_version_mismatch" in {
        violation.code for violation in evaluate_static_pack_conformance(version_mismatch).violations
    }

    missing_reference = _load_fixture("valid-synthetic-reference.json")
    missing_reference["capabilities"][0]["canonical_fact_ids"] = ["synthetic.record.missing"]
    assert "reference_missing" in {
        violation.code for violation in evaluate_static_pack_conformance(missing_reference).violations
    }

    read_effect = _load_fixture("valid-synthetic-reference.json")
    read_effect["capabilities"][0]["approval_policy_ref"] = "synthetic-policy:unexpected/v1"
    assert "read_only_effect_violation" in {
        violation.code for violation in evaluate_static_pack_conformance(read_effect).violations
    }

    incomplete_write = _load_fixture("valid-synthetic-reference.json")
    write_capability = incomplete_write["capabilities"][1]
    write_capability["authorization_dimensions"] = ["request_transition"]
    write_capability["lifecycle_transition_id"] = None
    write_capability["result_evidence_id"] = None
    write_capability["approval_policy_ref"] = None
    write_codes = {violation.code for violation in evaluate_static_pack_conformance(incomplete_write).violations}
    assert {
        "external_write_authority_missing",
        "external_write_transition_missing",
        "external_write_probe_missing",
        "external_write_approval_policy_missing",
    } <= write_codes


@pytest.mark.parametrize("capability_index", [0, 1])
def test_capabilities_require_canonical_fact_and_evidence_bindings(capability_index: int):
    for field in ("canonical_fact_ids", "evidence_requirement_ids"):
        candidate = _load_fixture("valid-synthetic-reference.json")
        candidate["capabilities"][capability_index][field] = []

        report = evaluate_static_pack_conformance(candidate)

        assert report.status is ConformanceStatus.FAIL
        assert "reference_missing" in {violation.code for violation in report.violations}


@pytest.mark.parametrize("field", ["canonical_fact_ids", "evidence_requirement_ids"])
def test_capability_fact_and_evidence_bindings_must_be_unique(field: str):
    candidate = _load_fixture("valid-synthetic-reference.json")
    references = candidate["capabilities"][0][field]
    candidate["capabilities"][0][field] = [references[0], references[0]]

    report = evaluate_static_pack_conformance(candidate)

    assert report.status is ConformanceStatus.FAIL
    assert "reference_missing" in {violation.code for violation in report.violations}


def test_external_write_result_probe_must_be_declared_in_its_evidence_list():
    candidate = _load_fixture("valid-synthetic-reference.json")
    candidate["capabilities"][1]["evidence_requirement_ids"] = ["synthetic.record.authoritative-read"]

    report = evaluate_static_pack_conformance(candidate)

    assert report.status is ConformanceStatus.FAIL
    assert "external_write_probe_missing" in {violation.code for violation in report.violations}


def test_read_only_capability_rejects_result_probe_as_general_evidence():
    candidate = _load_fixture("valid-synthetic-reference.json")
    candidate["capabilities"][0]["evidence_requirement_ids"] = ["synthetic.record.result-probe"]

    report = evaluate_static_pack_conformance(candidate)

    assert report.status is ConformanceStatus.FAIL
    assert "read_only_effect_violation" in {violation.code for violation in report.violations}


def test_enterprise_and_skyvern_do_not_import_the_new_offline_modules():
    violations: list[str] = []
    for source_root in (ROOT / "enterprise", ROOT / "skyvern"):
        for path in source_root.rglob("*.py"):
            if path in NEW_MODULES or path == SYNTHETIC_REFERENCE_ADAPTER:
                continue
            violations.extend(
                _new_module_import_violations(
                    path.read_text(encoding="utf-8"),
                    str(path.relative_to(ROOT)),
                )
            )

    assert not violations


@pytest.mark.parametrize("module_name", ["pack_sdk", "pack_conformance"])
def test_relative_package_import_shape_is_rejected_by_static_guard(module_name: str):
    source = f"from . import {module_name}\n"

    assert _new_module_import_violations(source, "synthetic_runtime_module.py") == [
        "synthetic_runtime_module.py"
    ]


def test_new_modules_import_only_offline_model_dependencies():
    imported_modules: set[str] = set()
    for path in NEW_MODULES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
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
    assert {module.split(".", 1)[0] for module in imported_modules}.isdisjoint(forbidden_roots)
    assert "domain_packs" not in imported_modules
    assert "DomainPackRegistry" not in imported_modules
    assert "CapabilityRegistry" not in imported_modules
    assert "CapabilityResolver" not in imported_modules
