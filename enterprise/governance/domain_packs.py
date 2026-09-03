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
    pack_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    kind: DomainPackKind
    display_name: str = Field(min_length=1)
    owner: str = Field(min_length=1)
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
        for capability in self.capabilities:
            if capability.version != self.version:
                raise ValueError("Domain Pack capability version must match its Pack")
        if self.kind is DomainPackKind.SYNTHETIC:
            if self.production_eligible:
                raise ValueError("Synthetic Domain Packs can never be production eligible")
        return self


class DomainPackRegistry:
    """Trusted registry that exposes a capability view to the resolver."""

    def __init__(self, manifests: list[DomainPackManifest] | None = None) -> None:
        self._manifests: dict[tuple[str, str], DomainPackManifest] = {}
        for manifest in manifests or []:
            self.register(manifest)

    def register(self, manifest: DomainPackManifest) -> None:
        if not isinstance(manifest, DomainPackManifest):
            raise TypeError("Active Domain Pack registry accepts runtime manifests only")
        key = (manifest.pack_id, manifest.version)
        if key in self._manifests:
            raise ValueError(f"Duplicate Domain Pack identity: {manifest.pack_id}@{manifest.version}")
        self._manifests[key] = manifest

    def require(self, pack_id: str, pack_version: str | None = None) -> DomainPackManifest:
        """Resolve an active manifest, requiring a version when it is ambiguous."""

        if pack_version is not None:
            key = (pack_id, pack_version)
            try:
                return self._manifests[key]
            except KeyError as exc:
                raise ValueError(f"Domain Pack is not installed: {pack_id}@{pack_version}") from exc

        matches = [manifest for (candidate_id, _), manifest in self._manifests.items() if candidate_id == pack_id]
        if not matches:
            raise ValueError(f"Domain Pack is not installed: {pack_id}")
        if len(matches) > 1:
            raise ValueError(f"Domain Pack version is required for an ambiguous active identity: {pack_id}")
        return matches[0]

    def require_exact(self, *, pack_id: str, pack_version: str) -> DomainPackManifest:
        return self.require(pack_id, pack_version)

    @property
    def manifests(self) -> tuple[DomainPackManifest, ...]:
        return tuple(self._manifests[key] for key in sorted(self._manifests))

    def capability_registry(self) -> CapabilityRegistry:
        return CapabilityRegistry(
            capability
            for manifest in self.manifests
            for capability in manifest.capabilities
        )
