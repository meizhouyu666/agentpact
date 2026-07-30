"""Trusted runtime-adapter contracts kept separate from static Pack SDK manifests."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


class PackRuntimeBinding(BaseModel):
    """Implementation identity that can be matched to one immutable manifest."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    pack_id: str = Field(min_length=1)
    pack_version: str = Field(min_length=1)
    capability_ids: tuple[str, ...] = Field(min_length=1)
    adapter_id: str = Field(min_length=1)


class PackRuntimeContract(BaseModel):
    """Runtime-safe identity pinned to one offline-conformant Pack contract."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    pack_id: str = Field(min_length=1)
    pack_version: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    capability_ids: tuple[str, ...] = Field(min_length=1)
    manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


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
    """Boot-time exact adapter/runtime-contract registry."""

    def __init__(self, contracts: Iterable[PackRuntimeContract]) -> None:
        self._contracts = {(item.pack_id, item.pack_version): item for item in contracts}
        self._adapters: dict[tuple[str, str], PackRuntimeAdapter] = {}

    def register(self, adapter: PackRuntimeAdapter) -> None:
        binding = adapter.binding
        key = (binding.pack_id, binding.pack_version)
        contract = self._contracts.get(key)
        if contract is None:
            raise ValueError("Runtime adapter does not match an installed Pack runtime contract")
        expected = tuple(sorted(contract.capability_ids))
        actual = tuple(sorted(binding.capability_ids))
        if len(actual) != len(set(actual)) or actual != expected:
            raise ValueError("Runtime adapter capabilities do not exactly match the Pack runtime contract")
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
            contract = self._contracts[(pack_id, pack_version)]
        except KeyError as exc:
            raise LookupError("No installed Pack runtime contract matches this runtime") from exc
        return PublicPackRuntimeMetadata(
            pack_id=contract.pack_id,
            pack_version=contract.pack_version,
            display_name=contract.display_name,
        )

    @property
    def registered_bindings(self) -> tuple[PackRuntimeBinding, ...]:
        return tuple(adapter.binding for _, adapter in sorted(self._adapters.items()))
