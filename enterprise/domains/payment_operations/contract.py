"""Uninstalled, source-free Payment Operations contract skeleton."""

from enterprise.governance.capabilities import AuthorizationDimension
from enterprise.governance.domain_pack_contracts import (
    UNASSIGNED,
    ContractOwnerReference,
    ContractOwnerRole,
    ContractParameterDefinition,
    DomainPackContractCapability,
    DomainPackContractCatalog,
    DomainPackContractManifest,
)

PACK_ID = "payment.operations"
CONTRACT_VERSION = "0.1.0-draft.1"
PLATFORM_OWNER = "FinRPA Platform"


def build_contract_manifest() -> DomainPackContractManifest:
    parameters = tuple(
        ContractParameterDefinition(parameter_id=parameter_id, description=description)
        for parameter_id, description in (
            ("business_glossary_ref", "Immutable business glossary supplied by a future Pack adopter"),
            ("authoritative_source_ref", "Versioned authoritative read source supplied by a future Pack adopter"),
            ("canonical_fact_schema_ref", "Versioned canonical payment fact schema"),
            ("lifecycle_schema_ref", "Versioned lifecycle and terminal-state schema"),
            ("read_evidence_contract_ref", "Read provenance and freshness evidence contract"),
            ("role_dimension_matrix_ref", "Tenant discover/read role and scope matrix"),
            ("identity_authority_ref", "Trusted identity authority and version"),
            ("policy_authority_ref", "Trusted authorization policy authority and version"),
            ("revocation_authority_ref", "Trusted revocation authority and freshness contract"),
            ("authorization_freshness_policy_ref", "Maximum authorization age and fail-safe rule"),
            ("read_source_boundary_ref", "Permitted read-only source and credential boundary"),
            ("field_classification_policy_ref", "Field and artifact classification policy"),
            ("model_egress_policy_ref", "Provider, region, purpose, and transformation decision"),
            ("retention_access_policy_ref", "Retention, access, hold, export, and deletion policy"),
        )
    )
    all_parameter_ids = tuple(item.parameter_id for item in parameters)
    capabilities = (
        DomainPackContractCapability(
            capability_id="payment.operations.discover",
            version=CONTRACT_VERSION,
            domain=PACK_ID,
            display_name="Discover payment records",
            authorization_dimensions=(AuthorizationDimension.DISCOVER,),
            required_parameter_ids=("business_glossary_ref", "role_dimension_matrix_ref"),
        ),
        DomainPackContractCapability(
            capability_id="payment.operations.read",
            version=CONTRACT_VERSION,
            domain=PACK_ID,
            display_name="Read a payment record",
            authorization_dimensions=(
                AuthorizationDimension.DISCOVER,
                AuthorizationDimension.READ_RECORD,
            ),
            required_parameter_ids=all_parameter_ids,
        ),
    )
    owner_refs = tuple(
        ContractOwnerReference(
            role=role,
            owner_ref=PLATFORM_OWNER if role is ContractOwnerRole.REGISTRY_INTERFACE else UNASSIGNED,
        )
        for role in ContractOwnerRole
    )
    return DomainPackContractManifest(
        pack_id=PACK_ID,
        contract_version=CONTRACT_VERSION,
        display_name="Payment Operations Reference Contract",
        parameter_definitions=parameters,
        capabilities=capabilities,
        owner_refs=owner_refs,
    )


def build_contract_catalog() -> DomainPackContractCatalog:
    return DomainPackContractCatalog((build_contract_manifest(),))
