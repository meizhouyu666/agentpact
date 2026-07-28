"""Versioned registration contracts for trusted business Domain Packs.

Domain Packs define business facts and policy references. They never execute a
browser action and they are never populated from model output.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, model_validator

from .capabilities import CapabilityDefinition, CapabilityRegistry


class DomainPackKind(StrEnum):
    SYNTHETIC = "synthetic"
    PRODUCTION = "production"


class DomainPackManifest(BaseModel):
    pack_id: str
    version: str
    kind: DomainPackKind
    display_name: str
    owner: str
    capabilities: list[CapabilityDefinition]
    canonical_fact_schema: dict[str, Any] = Field(default_factory=dict)
    state_transitions: dict[str, list[str]] = Field(default_factory=dict)
    policy_refs: set[str] = Field(default_factory=set)
    result_probe_refs: set[str] = Field(default_factory=set)
    production_eligible: bool = False

    @model_validator(mode="after")
    def validate_manifest_boundary(self) -> "DomainPackManifest":
        if not self.capabilities:
            raise ValueError("Domain Pack must register at least one capability")
        capability_ids = [capability.capability_id for capability in self.capabilities]
        if len(capability_ids) != len(set(capability_ids)):
            raise ValueError("Domain Pack capability ids must be unique")
        if self.kind is DomainPackKind.SYNTHETIC:
            if not self.pack_id.startswith("synthetic."):
                raise ValueError("Synthetic Domain Pack ids must use the synthetic namespace")
            if self.production_eligible:
                raise ValueError("Synthetic Domain Packs can never be production eligible")
        return self


class DomainPackRegistry:
    """Trusted registry that exposes a capability view to the resolver."""

    def __init__(self, manifests: list[DomainPackManifest] | None = None) -> None:
        self._manifests: dict[str, DomainPackManifest] = {}
        for manifest in manifests or []:
            self.register(manifest)

    def register(self, manifest: DomainPackManifest) -> None:
        if manifest.pack_id in self._manifests:
            raise ValueError(f"Duplicate Domain Pack id: {manifest.pack_id}")
        self._manifests[manifest.pack_id] = manifest

    def require(self, pack_id: str) -> DomainPackManifest:
        try:
            return self._manifests[pack_id]
        except KeyError as exc:
            raise ValueError(f"Domain Pack is not installed: {pack_id}") from exc

    def capability_registry(self) -> CapabilityRegistry:
        return CapabilityRegistry(
            capability
            for manifest in self._manifests.values()
            for capability in manifest.capabilities
        )
