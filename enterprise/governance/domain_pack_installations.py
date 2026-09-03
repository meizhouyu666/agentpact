"""Process-local Domain Pack installation and active-registry boundary."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Iterable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .domain_packs import DomainPackManifest, DomainPackRegistry
from .pack_conformance import ConformanceStatus, StaticConformanceReport


class DomainPackInstallationStatus(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    DISABLED = "disabled"


class DomainPackInstallation(BaseModel):
    """Immutable tenant authority that is separate from the offline catalog."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    installation_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    pack_id: str = Field(min_length=1)
    pack_version: str = Field(min_length=1)
    contract_digest: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    enabled_capability_ids: tuple[str, ...] = Field(min_length=1)
    adapter_ref: str = Field(min_length=1)
    result_probe_ref: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    status: DomainPackInstallationStatus
    accepted_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def validate_installation(self) -> "DomainPackInstallation":
        if len(self.enabled_capability_ids) != len(set(self.enabled_capability_ids)):
            raise ValueError("Enabled Capability ids must be unique")
        if self.expires_at <= self.accepted_at:
            raise ValueError("Domain Pack installation expiry must follow acceptance")
        return self

    @property
    def identity(self) -> tuple[str, str]:
        """Return the immutable Pack identity pinned by this installation."""

        return self.pack_id, self.pack_version


@dataclass(frozen=True)
class ActiveDomainPackSet:
    tenant_id: str
    installations: tuple[DomainPackInstallation, ...]
    registry: DomainPackRegistry
    trusted_adapter_refs: tuple[tuple[str, str, str], ...] = ()
    validated_at: datetime | None = None

    def require_installation(self, *, pack_id: str, pack_version: str) -> DomainPackInstallation:
        """Resolve only the accepted tenant installation for an exact identity."""

        for installation in self.installations:
            if installation.identity == (pack_id, pack_version):
                return installation
        raise LookupError(f"No active Domain Pack installation matches {pack_id}@{pack_version}")

    def runtime_registry(
        self,
        contracts: Iterable[object],
        *,
        now: datetime | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        """Build the strict runtime-adapter registry for this tenant set."""

        from .pack_runtime import PackRuntimeRegistry

        return PackRuntimeRegistry.from_active_domain_pack_set(
            self,
            contracts,
            now=self.validated_at if now is None else now,
            clock=clock,
        )


def build_active_domain_pack_set(
    *,
    tenant_id: str,
    runtime_manifests: Iterable[DomainPackManifest],
    conformance_reports: Iterable[StaticConformanceReport],
    installations: Iterable[DomainPackInstallation],
    expected_policy_versions: dict[str, str],
    expected_adapter_refs: dict[str, str],
    now: datetime,
) -> ActiveDomainPackSet:
    """Build one tenant registry exclusively from accepted, current installations."""

    runtime_by_identity = _unique_by_identity(runtime_manifests, "runtime manifest")
    reports_by_id = _unique_reports(conformance_reports)
    accepted: list[DomainPackInstallation] = []
    active_manifests: list[DomainPackManifest] = []
    activated_pack_ids: set[str] = set()

    for installation in installations:
        if installation.tenant_id != tenant_id:
            continue
        if installation.status is not DomainPackInstallationStatus.ACCEPTED:
            continue
        if installation.pack_id in activated_pack_ids:
            raise ValueError(f"Duplicate accepted Domain Pack installation: {installation.pack_id}")
        if not installation.accepted_at <= now < installation.expires_at:
            raise ValueError("Accepted Domain Pack installation is stale")

        runtime_manifest = _require_pack(runtime_by_identity, installation.identity, "runtime manifest")
        report = _require_pack(reports_by_id, installation.identity, "static conformance report")
        if report.status is not ConformanceStatus.PASS:
            raise ValueError("Accepted Domain Pack contract failed static conformance")
        if report.candidate_pack_id != installation.pack_id or report.candidate_pack_version != installation.pack_version:
            raise ValueError("Domain Pack installation identity does not match its conformance report")
        if report.manifest_digest != installation.contract_digest:
            raise ValueError("Domain Pack installation digest does not match its accepted contract")
        if expected_policy_versions.get(installation.pack_id) != installation.policy_version:
            raise ValueError("Domain Pack installation policy version is stale")
        if expected_adapter_refs.get(installation.pack_id) != installation.adapter_ref:
            raise ValueError("Domain Pack installation adapter reference is not trusted")

        runtime_capabilities = {item.capability_id: item for item in runtime_manifest.capabilities}
        enabled = set(installation.enabled_capability_ids)
        if not enabled <= runtime_capabilities.keys():
            raise ValueError("Domain Pack installation enables an unresolved Capability")
        if installation.result_probe_ref not in runtime_manifest.result_probe_refs:
            raise ValueError("Domain Pack installation result probe does not resolve")
        for capability_id in enabled:
            runtime_capability = runtime_capabilities[capability_id]
            if runtime_capability.version != installation.pack_version:
                raise ValueError("Enabled Capability version does not match its installation")
            if runtime_capability.result_probe_ref != installation.result_probe_ref:
                raise ValueError("Enabled Capability result probe does not match its installation")

        active_manifests.append(
            runtime_manifest.model_copy(
                update={
                    "capabilities": [
                        capability
                        for capability in runtime_manifest.capabilities
                        if capability.capability_id in enabled
                    ]
                },
                deep=True,
            )
        )
        accepted.append(installation)
        activated_pack_ids.add(installation.pack_id)

    return ActiveDomainPackSet(
        tenant_id=tenant_id,
        installations=tuple(sorted(accepted, key=lambda item: item.identity)),
        registry=DomainPackRegistry(active_manifests),
        trusted_adapter_refs=tuple(
            sorted(
                (installation.identity[0], installation.identity[1], installation.adapter_ref)
                for installation in accepted
            )
        ),
        validated_at=now,
    )


def _unique_by_identity(items: Iterable[DomainPackManifest], label: str) -> dict[tuple[str, str], DomainPackManifest]:
    indexed: dict[tuple[str, str], DomainPackManifest] = {}
    for item in items:
        if not isinstance(item, DomainPackManifest):
            raise TypeError("Active Domain Pack registry accepts runtime manifests only")
        pack_id = getattr(item, "pack_id")
        pack_version = getattr(item, "version", None)
        if pack_version is None:
            pack_version = getattr(item, "pack_version")
        identity = (pack_id, pack_version)
        if identity in indexed:
            raise ValueError(f"Duplicate {label}: {pack_id}@{pack_version}")
        indexed[identity] = item
    return indexed


def _unique_reports(items: Iterable[StaticConformanceReport]) -> dict[tuple[str, str], StaticConformanceReport]:
    indexed: dict[tuple[str, str], StaticConformanceReport] = {}
    for report in items:
        if not report.candidate_pack_id:
            raise ValueError("Static conformance report has no Pack id")
        if not report.candidate_pack_version:
            raise ValueError("Static conformance report has no Pack version")
        identity = (report.candidate_pack_id, report.candidate_pack_version)
        if identity in indexed:
            raise ValueError(f"Duplicate static conformance report: {report.candidate_pack_id}@{report.candidate_pack_version}")
        indexed[identity] = report
    return indexed


def _require_pack(items: dict[tuple[str, str], object], identity: tuple[str, str], label: str):
    try:
        return items[identity]
    except KeyError as exc:
        if any(candidate_id == identity[0] for candidate_id, _ in items):
            raise ValueError(
                f"Domain Pack installation version does not match its {label}: {identity[0]}@{identity[1]}"
            ) from exc
        raise ValueError(f"Accepted installation has no matching {label}: {identity[0]}@{identity[1]}") from exc
