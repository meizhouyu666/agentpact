"""Offline contract and active-registry exclusion tests for payment.operations."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from pydantic import ValidationError

from enterprise.domains.payment_operations import (
    CONTRACT_VERSION,
    PACK_ID,
    build_contract_catalog,
    build_contract_manifest,
)
from enterprise.governance.capabilities import AuthorizationDimension
from enterprise.governance.domain_pack_contracts import (
    DOMAIN_PACK_CONTRACT_SCHEMA_VERSION,
    UNASSIGNED,
    ContractOwnerRole,
    DomainPackContractCapability,
    DomainPackContractNotInstallable,
)

ROOT = Path(__file__).parents[2]


def test_payment_operations_contract_is_versioned_source_free_and_uninstallable():
    contract = build_contract_manifest()

    assert contract.schema_version == DOMAIN_PACK_CONTRACT_SCHEMA_VERSION
    assert contract.pack_id == PACK_ID
    assert contract.contract_version == CONTRACT_VERSION
    assert contract.maturity.value == "reference"
    assert contract.default_model_egress == "deny"
    assert contract.default_payload_retention == "none"
    assert len(contract.contract_digest) == 64
    assert contract.contract_digest == build_contract_manifest().contract_digest
    assert contract.unresolved_parameter_ids
    assert all(item.required_for_installation for item in contract.parameter_definitions)
    assert all(item.unresolved_value == UNASSIGNED for item in contract.parameter_definitions)
    assert set(contract.unresolved_owner_roles) == set(ContractOwnerRole) - {
        ContractOwnerRole.REGISTRY_INTERFACE
    }

    with pytest.raises(DomainPackContractNotInstallable, match="separately approved Installation"):
        contract.require_installation_ready()
    with pytest.raises(ValidationError, match="frozen"):
        contract.display_name = "Mutated contract"
    with pytest.raises(ValidationError, match="frozen"):
        contract.capabilities[0].display_name = "Mutated capability"


def test_contract_catalog_is_offline_and_version_exact():
    catalog = build_contract_catalog()
    contract = catalog.require(pack_id=PACK_ID, contract_version=CONTRACT_VERSION)

    assert catalog.contracts() == (contract,)
    with pytest.raises(ValueError, match="not in the offline catalog"):
        catalog.require(pack_id=PACK_ID, contract_version="latest")
    with pytest.raises(ValueError, match="Duplicate Domain Pack contract"):
        catalog.add(contract)
    with pytest.raises(DomainPackContractNotInstallable):
        catalog.require_installable(pack_id=PACK_ID, contract_version=CONTRACT_VERSION)


def test_capability_surface_is_discover_read_only_and_has_no_effect_or_transition():
    contract = build_contract_manifest()
    allowed = {AuthorizationDimension.DISCOVER, AuthorizationDimension.READ_RECORD}

    assert {capability.capability_id for capability in contract.capabilities} == {
        "payment.operations.discover",
        "payment.operations.read",
    }
    for capability in contract.capabilities:
        assert set(capability.authorization_dimensions) <= allowed
        assert capability.state_transition is None
        assert capability.effect is None
    assert all(
        AuthorizationDimension.EXECUTE_TRANSITION not in capability.authorization_dimensions
        for capability in contract.capabilities
    )

    with pytest.raises(ValidationError, match="discover/read"):
        DomainPackContractCapability(
            capability_id="payment.operations.submit",
            version=CONTRACT_VERSION,
            domain=PACK_ID,
            display_name="Submit payment",
            authorization_dimensions=(AuthorizationDimension.EXECUTE_TRANSITION,),
        )
    with pytest.raises(ValidationError):
        DomainPackContractCapability(
            capability_id="payment.operations.transition",
            version=CONTRACT_VERSION,
            domain=PACK_ID,
            display_name="Transition payment",
            authorization_dimensions=(AuthorizationDimension.DISCOVER,),
            state_transition={"from": "unknown", "to": "unknown"},
        )
    with pytest.raises(ValidationError):
        DomainPackContractCapability(
            capability_id="payment.operations.effect",
            version=CONTRACT_VERSION,
            domain=PACK_ID,
            display_name="Effect payment",
            authorization_dimensions=(AuthorizationDimension.DISCOVER,),
            effect="read",
        )


def test_contract_is_not_imported_by_active_registry_or_runtime_modules():
    active_registry_source = (ROOT / "enterprise" / "governance" / "domain_packs.py").read_text(encoding="utf-8")
    governance_exports = (ROOT / "enterprise" / "governance" / "__init__.py").read_text(encoding="utf-8")
    assert "domain_pack_contracts" not in active_registry_source
    assert "DomainPackContractManifest" not in active_registry_source
    assert "payment_operations" not in governance_exports

    for source_root in (ROOT / "enterprise", ROOT / "skyvern"):
        for path in source_root.rglob("*.py"):
            if "payment_operations" in path.parts:
                continue
            source = path.read_text(encoding="utf-8")
            assert "enterprise.domains.payment_operations" not in source


def test_payment_operations_contract_has_no_runtime_or_synthetic_dependencies():
    package_root = ROOT / "enterprise" / "domains" / "payment_operations"
    imported_modules: set[str] = set()
    combined_source = ""
    for path in package_root.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        combined_source += source
        tree = ast.parse(source)
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
    assert "synthetic_payment" not in combined_source
    assert "DomainPackInstallation" not in combined_source
    assert "DomainPackRegistry" not in combined_source
    assert "CapabilityResolver" not in combined_source
