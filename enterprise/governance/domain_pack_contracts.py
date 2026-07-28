"""Offline Domain Pack contract authoring primitives.

These immutable artifacts describe an SDK contract. They are deliberately
separate from ``DomainPackManifest`` and the active ``DomainPackRegistry`` and
carry no tenant installation or runtime authority.
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .capabilities import AuthorizationDimension

DOMAIN_PACK_CONTRACT_SCHEMA_VERSION = "domain-pack-contract/v1"
UNASSIGNED = "unassigned"


class DomainPackContractNotInstallable(ValueError):
    """The offline reference contract cannot be used for an installation."""


class ContractMaturity(StrEnum):
    REFERENCE = "reference"


class ContractOwnerRole(StrEnum):
    BUSINESS = "business"
    TENANT_POLICY = "tenant_policy"
    IDENTITY_SECURITY = "identity_security"
    INTEGRATION_PROBE = "integration_probe"
    REGISTRY_INTERFACE = "registry_interface"
    DATA_CONTROL = "data_control"
    RISK = "risk"
    LEGAL_COMPLIANCE = "legal_compliance"
    REHEARSAL = "rehearsal"
    SRE_RELEASE = "sre_release"


class ContractParameterDefinition(BaseModel):
    """A required future binding, never a production value."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    parameter_id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    required_for_installation: bool = True
    unresolved_value: Literal["unassigned"] = UNASSIGNED


class ContractOwnerReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    role: ContractOwnerRole
    owner_ref: str = Field(min_length=1)

    @property
    def is_assigned(self) -> bool:
        return self.owner_ref != UNASSIGNED


class DomainPackContractCapability(BaseModel):
    """A non-executable capability shape for the offline Contract Catalog."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    capability_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    domain: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    authorization_dimensions: tuple[AuthorizationDimension, ...]
    required_parameter_ids: tuple[str, ...] = ()
    state_transition: None = None
    effect: None = None

    @model_validator(mode="after")
    def validate_read_only_surface(self) -> "DomainPackContractCapability":
        allowed = {AuthorizationDimension.DISCOVER, AuthorizationDimension.READ_RECORD}
        dimensions = set(self.authorization_dimensions)
        if not dimensions or not dimensions <= allowed:
            raise ValueError("Contract capability may declare only discover/read authorization dimensions")
        if len(dimensions) != len(self.authorization_dimensions):
            raise ValueError("Contract capability authorization dimensions must be unique")
        if len(set(self.required_parameter_ids)) != len(self.required_parameter_ids):
            raise ValueError("Contract capability parameter references must be unique")
        return self


class DomainPackContractManifest(BaseModel):
    """Immutable source-free contract that cannot enter the active registry."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["domain-pack-contract/v1"] = DOMAIN_PACK_CONTRACT_SCHEMA_VERSION
    pack_id: str = Field(min_length=1)
    contract_version: str = Field(min_length=1)
    maturity: Literal[ContractMaturity.REFERENCE] = ContractMaturity.REFERENCE
    display_name: str = Field(min_length=1)
    parameter_definitions: tuple[ContractParameterDefinition, ...] = Field(min_length=1)
    capabilities: tuple[DomainPackContractCapability, ...] = Field(min_length=1)
    owner_refs: tuple[ContractOwnerReference, ...] = Field(min_length=1)
    default_model_egress: Literal["deny"] = "deny"
    default_payload_retention: Literal["none"] = "none"

    @model_validator(mode="after")
    def validate_contract(self) -> "DomainPackContractManifest":
        parameter_ids = [item.parameter_id for item in self.parameter_definitions]
        if len(parameter_ids) != len(set(parameter_ids)):
            raise ValueError("Contract parameter ids must be unique")

        capability_ids = [item.capability_id for item in self.capabilities]
        if len(capability_ids) != len(set(capability_ids)):
            raise ValueError("Contract capability ids must be unique")
        known_parameters = set(parameter_ids)
        for capability in self.capabilities:
            if capability.domain != self.pack_id:
                raise ValueError("Contract capability domain must match its Pack")
            if not set(capability.required_parameter_ids) <= known_parameters:
                raise ValueError("Contract capability references an unknown parameter")

        owner_roles = [item.role for item in self.owner_refs]
        if len(owner_roles) != len(set(owner_roles)):
            raise ValueError("Contract owner roles must be unique")
        if set(owner_roles) != set(ContractOwnerRole):
            raise ValueError("Contract must declare every required owner role")
        return self

    @property
    def contract_digest(self) -> str:
        canonical = json.dumps(self.model_dump(mode="json"), ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @property
    def unresolved_parameter_ids(self) -> tuple[str, ...]:
        return tuple(item.parameter_id for item in self.parameter_definitions if item.required_for_installation)

    @property
    def unresolved_owner_roles(self) -> tuple[ContractOwnerRole, ...]:
        return tuple(item.role for item in self.owner_refs if not item.is_assigned)

    def require_installation_ready(self) -> None:
        """Fail closed until a separately approved Installation contract exists."""

        raise DomainPackContractNotInstallable(
            "Reference Domain Pack contract is offline-only; a separately approved Installation is required"
        )


class DomainPackContractCatalog:
    """In-memory authoring catalog with no active registry adapter."""

    def __init__(self, contracts: tuple[DomainPackContractManifest, ...] = ()) -> None:
        self._contracts: dict[tuple[str, str], DomainPackContractManifest] = {}
        for contract in contracts:
            self.add(contract)

    def add(self, contract: DomainPackContractManifest) -> None:
        key = (contract.pack_id, contract.contract_version)
        if key in self._contracts:
            raise ValueError(f"Duplicate Domain Pack contract: {contract.pack_id}@{contract.contract_version}")
        self._contracts[key] = contract

    def require(self, *, pack_id: str, contract_version: str) -> DomainPackContractManifest:
        try:
            return self._contracts[(pack_id, contract_version)]
        except KeyError as exc:
            raise ValueError(f"Domain Pack contract is not in the offline catalog: {pack_id}@{contract_version}") from exc

    def contracts(self) -> tuple[DomainPackContractManifest, ...]:
        return tuple(self._contracts[key] for key in sorted(self._contracts))

    def require_installable(self, *, pack_id: str, contract_version: str) -> DomainPackContractManifest:
        contract = self.require(pack_id=pack_id, contract_version=contract_version)
        contract.require_installation_ready()
        return contract
