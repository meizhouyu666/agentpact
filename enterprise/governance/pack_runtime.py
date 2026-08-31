"""Trusted runtime-adapter contracts kept separate from static Pack SDK manifests."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal, Protocol, runtime_checkable

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
    adapter_id: str = Field(min_length=1)
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


class PackAdvanceStatus(StrEnum):
    COMPLETED = "COMPLETED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    PENDING_RESULT_PROBE = "PENDING_RESULT_PROBE"
    FAILED = "FAILED"


class PackProbeStatus(StrEnum):
    CONFIRMED = "CONFIRMED"
    NOT_CONFIRMED = "NOT_CONFIRMED"
    INCONCLUSIVE = "INCONCLUSIVE"


class PackRunRequest(BaseModel):
    """Trusted platform inputs supplied to one immutable Pack adapter."""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    tenant_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    intent_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    business_inputs: dict[str, Any]
    target_url: str = Field(min_length=1)
    principal: object
    now: datetime


class PackRunRestoreRequest(BaseModel):
    """Durable, version-pinned input used to reconstruct a Pack-owned run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    binding: PackRuntimeBinding
    provider_mode: Literal["recorded", "live"]
    target_url: str = Field(min_length=1)
    admission_payload: dict[str, Any]


class PreparedRunReference(BaseModel):
    """Platform-safe identity plus an opaque, Pack-owned reconstruction payload."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    pack_id: str = Field(min_length=1)
    pack_version: str = Field(min_length=1)
    adapter_id: str = Field(min_length=1)
    admission_id: str | None = Field(default=None, min_length=1)
    contract_id: str | None = Field(default=None, min_length=1)
    provider_mode: Literal["recorded", "live"]
    opaque_payload: dict[str, Any]


class ApprovalRequestSpecification(BaseModel):
    """Generic approval material; it contains no executable browser selector."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str = Field(min_length=1)
    step_id: str = Field(min_length=1)
    contract_id: str = Field(min_length=1)
    organization_id: str = Field(min_length=1)
    intent_id: str = Field(min_length=1)
    action_fingerprint: str = Field(min_length=1)
    observation_hash: str = Field(min_length=1)
    requested_approval_route: str = Field(min_length=1)
    source_department_id: str = Field(min_length=1)
    business_line_id: str | None = Field(default=None, min_length=1)
    risk_level: Literal["low", "medium", "high", "critical", "unknown"]
    effect: str = Field(min_length=1)
    expires_at: datetime
    reason_code: str = Field(min_length=1, pattern=r"^[A-Z][A-Z0-9_]*$")
    redacted_description: str = Field(min_length=1, max_length=240)
    policy_decision: dict[str, Any]


class ExecutionCheckpoint(BaseModel):
    """Exact immutable identity of an external effect awaiting business proof."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    permit_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    step_id: str = Field(min_length=1)
    action_fingerprint: str = Field(min_length=1)
    observation_hash: str = Field(min_length=1)
    idempotency_key_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_effect: str = Field(min_length=1)
    result_probe_ref: str = Field(min_length=1)
    attempt_status: str = Field(min_length=1)


class PackAdvanceResult(BaseModel):
    """Closed Pack-to-platform lifecycle result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: PackAdvanceStatus
    run_id: str = Field(min_length=1)
    step_id: str | None = Field(default=None, min_length=1)
    reason_code: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]*$")
    approval: ApprovalRequestSpecification | None = None
    execution_checkpoint: ExecutionCheckpoint | None = None

    def model_post_init(self, __context: Any) -> None:
        has_approval = self.approval is not None
        has_checkpoint = self.execution_checkpoint is not None
        if has_approval != (self.status is PackAdvanceStatus.AWAITING_APPROVAL):
            raise ValueError("Only AWAITING_APPROVAL may carry an approval specification")
        if has_checkpoint != (self.status is PackAdvanceStatus.PENDING_RESULT_PROBE):
            raise ValueError("Only PENDING_RESULT_PROBE may carry an execution checkpoint")
        if self.status is PackAdvanceStatus.FAILED and self.reason_code is None:
            raise ValueError("FAILED requires a stable reason code")


class PackAdmissionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    prepared: PreparedRunReference
    admission_id: str = Field(min_length=1)
    initial: PackAdvanceResult


class PackProbeResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: PackProbeStatus
    checkpoint: ExecutionCheckpoint
    reason_code: str = Field(min_length=1, pattern=r"^[A-Z][A-Z0-9_]*$")
    evidence_refs: tuple[str, ...] = ()


class PackLifecycleError(RuntimeError):
    """Code-bearing boundary for validation/planning failures, not lifecycle flow."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


ApprovalHandler = Callable[[PreparedRunReference, ApprovalRequestSpecification, str], Awaitable[object]]


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

    def prepare_run(self, request: PackRunRequest) -> PreparedRunReference: ...

    def restore_run(self, request: PackRunRestoreRequest) -> PreparedRunReference: ...

    async def admit_run(
        self,
        prepared: PreparedRunReference,
        *,
        approval_handler: ApprovalHandler,
        operation_key: str,
    ) -> PackAdmissionResult: ...

    async def advance_run(
        self,
        prepared: PreparedRunReference,
        *,
        approval_handler: ApprovalHandler,
        operation_key: str,
    ) -> PackAdvanceResult: ...

    async def probe_run(self, prepared: PreparedRunReference, *, operation_key: str) -> PackProbeResult: ...


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
        if binding.adapter_id != contract.adapter_id:
            raise ValueError("Runtime adapter identity does not match the Pack runtime contract")
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


def derive_pack_run_id(*, tenant_id: str, request_id: str) -> str:
    """Stable Agent Run identity shared by every Pack implementation."""

    import hashlib
    import json

    material = json.dumps(
        ["agentpact-agent-run/v1", tenant_id, request_id],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return "run_m10_" + hashlib.sha256(material.encode("utf-8")).hexdigest()
