"""Trusted runtime-adapter contracts kept separate from static Pack SDK manifests."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from .pack_sdk import PackSdkManifest


class PackRuntimeBinding(BaseModel):
    """Implementation identity that can be matched to one immutable manifest."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    pack_id: str = Field(min_length=1)
    pack_version: str = Field(min_length=1)
    capability_ids: tuple[str, ...] = Field(min_length=1)
    adapter_id: str = Field(min_length=1)


class ModelSafeRuntimeProjection(BaseModel):
    """Runtime metadata safe to expose to a constrained planning provider."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    pack_id: str
    pack_version: str
    capability_ids: tuple[str, ...]
    input_slot_names: tuple[str, ...]


class PublicPackRuntimeMetadata(BaseModel):
    """Safe installed-Pack identity for public run projections."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    pack_id: str = Field(min_length=1)
    pack_version: str = Field(min_length=1)
    display_name: str = Field(min_length=1)


@runtime_checkable
class PackRuntimeAdapter(Protocol):
    """Authority-minimized application boundary for a trusted Pack implementation.

    The protocol deliberately uses opaque values for compiled and durable runtime
    artifacts. Core governance can validate and dispatch adapters without importing
    any Domain Pack implementation or learning browser/authority internals.
    """

    @property
    def binding(self) -> PackRuntimeBinding: ...

    def model_safe_projection(self, authority: object) -> ModelSafeRuntimeProjection: ...

    def prepare_run(self, **trusted_inputs: Any) -> object: ...

    async def admit_run(self, prepared: object, **trusted_inputs: Any) -> object: ...

    async def advance_run(self, prepared: object, **trusted_inputs: Any) -> object: ...

    async def probe_run(self, prepared: object, **trusted_inputs: Any) -> object: ...


class PackRuntimeRegistry:
    """Boot-time exact adapter/manifest conformance registry."""

    def __init__(self, manifests: Iterable[PackSdkManifest]) -> None:
        self._manifests = {(item.pack_id, item.pack_version): item for item in manifests}
        self._adapters: dict[tuple[str, str], PackRuntimeAdapter] = {}

    def register(self, adapter: PackRuntimeAdapter) -> None:
        binding = adapter.binding
        key = (binding.pack_id, binding.pack_version)
        manifest = self._manifests.get(key)
        if manifest is None:
            raise ValueError("Runtime adapter does not match an installed static Pack manifest")
        expected = tuple(sorted(item.capability_id for item in manifest.capabilities))
        actual = tuple(sorted(binding.capability_ids))
        if len(actual) != len(set(actual)) or actual != expected:
            raise ValueError("Runtime adapter capabilities do not exactly match the static Pack manifest")
        if key in self._adapters:
            raise ValueError("A runtime adapter is already registered for this Pack version")
        self._adapters[key] = adapter

    def require(self, *, pack_id: str, pack_version: str) -> PackRuntimeAdapter:
        try:
            return self._adapters[(pack_id, pack_version)]
        except KeyError as exc:
            raise LookupError("No conformant runtime adapter is registered for this Pack version") from exc

    def public_metadata(self, *, pack_id: str, pack_version: str) -> PublicPackRuntimeMetadata:
        self.require(pack_id=pack_id, pack_version=pack_version)
        try:
            manifest = self._manifests[(pack_id, pack_version)]
        except KeyError as exc:
            raise LookupError("No installed static Pack manifest matches this runtime") from exc
        return PublicPackRuntimeMetadata(
            pack_id=manifest.pack_id,
            pack_version=manifest.pack_version,
            display_name=manifest.display_name,
        )

    @property
    def registered_bindings(self) -> tuple[PackRuntimeBinding, ...]:
        return tuple(adapter.binding for _, adapter in sorted(self._adapters.items()))
