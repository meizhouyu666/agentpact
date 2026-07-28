"""Immutable authoring models for offline Domain Pack SDK artifacts.

The models in this module describe static contracts only. They do not install
packs, resolve authorization, call evidence sources, or expose runtime wiring.
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .capabilities import AuthorizationDimension
from .domain_pack_contracts import ContractOwnerReference

PACK_SDK_SCHEMA_VERSION = "domain-pack-sdk/v1"


class PackContractKind(StrEnum):
    SYNTHETIC_REFERENCE = "synthetic_reference"
    EXTERNAL_CANDIDATE = "external_candidate"


class PackEffectClass(StrEnum):
    READ_ONLY = "read_only"
    EXTERNAL_WRITE = "external_write"


class PackEvidenceKind(StrEnum):
    AUTHORITATIVE_READ = "authoritative_read"
    RESULT_PROBE = "result_probe"


class PackDataClassification(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


class PackCanonicalFact(BaseModel):
    """One versioned fact shape and its static evidence provenance."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    fact_id: str = Field(min_length=1)
    schema_ref: str = Field(min_length=1)
    data_classification: PackDataClassification
    source_ref: str = Field(min_length=1)
    evidence_requirement_id: str = Field(min_length=1)


class PackEvidenceRequirement(BaseModel):
    """A freshness-bounded evidence shape; it is never invoked by the SDK."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_id: str = Field(min_length=1)
    kind: PackEvidenceKind
    source_schema_ref: str = Field(min_length=1)
    maximum_age_seconds: int = Field(gt=0, strict=True)


class PackLifecycleTransition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    transition_id: str = Field(min_length=1)
    source_state: str = Field(min_length=1)
    target_state: str = Field(min_length=1)


class PackLifecycle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    lifecycle_id: str = Field(min_length=1)
    states: tuple[str, ...] = Field(min_length=1)
    terminal_states: tuple[str, ...] = Field(min_length=1)
    transitions: tuple[PackLifecycleTransition, ...] = Field(min_length=1)


class PackCapability(BaseModel):
    """Static capability metadata without an implementation or caller.

    Fact and evidence bindings remain parseable when incomplete so the static
    evaluator can return all applicable stable conformance codes in one report.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    capability_id: str = Field(min_length=1)
    pack_version: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    effect_class: PackEffectClass
    authorization_dimensions: tuple[AuthorizationDimension, ...]
    canonical_fact_ids: tuple[str, ...]
    evidence_requirement_ids: tuple[str, ...]
    lifecycle_transition_id: str | None = None
    result_evidence_id: str | None = None
    approval_policy_ref: str | None = None


class PackSdkManifest(BaseModel):
    """An immutable, digestible Pack contract that is permanently offline."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["domain-pack-sdk/v1"] = PACK_SDK_SCHEMA_VERSION
    pack_id: str = Field(min_length=1)
    pack_version: str = Field(min_length=1)
    kind: PackContractKind
    display_name: str = Field(min_length=1)
    contract_catalog_only: Literal[True] = True
    runtime_wiring_eligible: Literal[False] = False
    owner_refs: tuple[ContractOwnerReference, ...] = Field(min_length=1)
    freshness_ceiling_seconds: int = Field(gt=0, strict=True)
    canonical_facts: tuple[PackCanonicalFact, ...] = Field(min_length=1)
    evidence_requirements: tuple[PackEvidenceRequirement, ...] = Field(min_length=1)
    lifecycle: PackLifecycle
    capabilities: tuple[PackCapability, ...] = Field(min_length=1)

    @field_validator("contract_catalog_only", mode="before")
    @classmethod
    def validate_contract_catalog_boundary(cls, value: object) -> object:
        if value is not True:
            raise ValueError("Pack SDK manifests must be contract-catalog-only")
        return value

    @field_validator("runtime_wiring_eligible", mode="before")
    @classmethod
    def validate_runtime_wiring_boundary(cls, value: object) -> object:
        if value is not False:
            raise ValueError("Pack SDK manifests must be ineligible for runtime wiring")
        return value

    @property
    def manifest_digest(self) -> str:
        """Return SHA-256 over stable canonical JSON, excluding this property."""

        canonical = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
