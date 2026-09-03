"""Trusted runtime-adapter contracts kept separate from static Pack SDK manifests."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping
from datetime import datetime, timezone
from enum import Enum, StrEnum
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, JsonValue

if TYPE_CHECKING:
    from .domain_pack_installations import ActiveDomainPackSet


class PackRuntimeBinding(BaseModel):
    """Implementation identity that can be matched to one immutable manifest."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    pack_id: str = Field(min_length=1)
    pack_version: str = Field(min_length=1)
    capability_ids: tuple[str, ...] = Field(min_length=1)
    adapter_id: str = Field(min_length=1)

    def model_post_init(self, __context: Any) -> None:
        if len(self.capability_ids) != len(set(self.capability_ids)):
            raise ValueError("Pack runtime binding capability ids must be unique")

    @property
    def identity(self) -> tuple[str, str, tuple[str, ...], str]:
        return (self.pack_id, self.pack_version, tuple(sorted(self.capability_ids)), self.adapter_id)


# JSON is the only shape that may cross the platform/Pack boundary as an
# opaque payload. Pack adapters can retain richer state internally, but the
# formal lifecycle contract persists and correlates only serializable values.

RuntimeValue = BaseModel | JsonValue


@runtime_checkable
class RuntimeInstallation(Protocol):
    """Minimal accepted-installation shape required by the runtime registry.

    The registry deliberately does not import a concrete installation model;
    composition code supplies an implementation of this structural contract.
    """

    @property
    def tenant_id(self) -> str: ...

    @property
    def pack_id(self) -> str: ...

    @property
    def pack_version(self) -> str: ...

    @property
    def status(self) -> str | Enum: ...

    @property
    def accepted_at(self) -> datetime: ...

    @property
    def expires_at(self) -> datetime: ...

    @property
    def contract_digest(self) -> str: ...

    @property
    def adapter_ref(self) -> str: ...

    @property
    def enabled_capability_ids(self) -> tuple[str, ...]: ...


class PackRuntimeContract(BaseModel):
    """Runtime-safe identity pinned to one offline-conformant Pack contract."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    pack_id: str = Field(min_length=1)
    pack_version: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    capability_ids: tuple[str, ...] = Field(min_length=1)
    adapter_id: str = Field(min_length=1)
    manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    def model_post_init(self, __context: Any) -> None:
        if len(self.capability_ids) != len(set(self.capability_ids)):
            raise ValueError("Pack runtime contract capability ids must be unique")

    @property
    def identity(self) -> tuple[str, str, tuple[str, ...], str]:
        return (self.pack_id, self.pack_version, tuple(sorted(self.capability_ids)), self.adapter_id)


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
    business_inputs: dict[str, JsonValue]
    target_url: str = Field(min_length=1)
    # A composed application may pass its typed principal model; a mapping is
    # retained solely for lightweight adapters and test composition edges.
    principal: BaseModel | dict[str, JsonValue]
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
    admission_payload: dict[str, JsonValue]


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
    opaque_payload: dict[str, JsonValue]


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
    policy_decision: dict[str, JsonValue]


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
        expects_approval = self.status is PackAdvanceStatus.AWAITING_APPROVAL
        expects_checkpoint = self.status is PackAdvanceStatus.PENDING_RESULT_PROBE
        if has_approval != expects_approval:
            raise ValueError("Only AWAITING_APPROVAL may carry an approval specification")
        if has_checkpoint != expects_checkpoint:
            raise ValueError("Only PENDING_RESULT_PROBE may carry an execution checkpoint")
        if self.status is PackAdvanceStatus.FAILED and self.reason_code is None:
            raise ValueError("FAILED requires a stable reason code")
        if self.status in {
            PackAdvanceStatus.AWAITING_APPROVAL,
            PackAdvanceStatus.PENDING_RESULT_PROBE,
        } and self.reason_code is None:
            raise ValueError(f"{self.status.value} requires a stable reason code")
        if expects_approval:
            approval = self.approval
            assert approval is not None
            if self.step_id != approval.step_id:
                raise ValueError("Approval result step correlation does not match its specification")
        if expects_checkpoint:
            checkpoint = self.execution_checkpoint
            assert checkpoint is not None
            if self.step_id != checkpoint.step_id:
                raise ValueError("Probe result step correlation does not match its checkpoint")
            if checkpoint.attempt_status.casefold() != "unknown":
                raise ValueError("PENDING_RESULT_PROBE requires an UNKNOWN execution checkpoint")


class PackAdmissionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    prepared: PreparedRunReference
    admission_id: str = Field(min_length=1)
    initial: PackAdvanceResult

    def model_post_init(self, __context: Any) -> None:
        if self.initial.run_id != self.prepared.run_id:
            raise ValueError("Admission result run correlation does not match the prepared run")
        if self.prepared.admission_id is not None and self.admission_id != self.prepared.admission_id:
            raise ValueError("Admission result identity does not match the prepared run")


class PackProbeResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: PackProbeStatus
    checkpoint: ExecutionCheckpoint
    reason_code: str = Field(min_length=1, pattern=r"^[A-Z][A-Z0-9_]*$")
    evidence_refs: tuple[str, ...] = ()

    def model_post_init(self, __context: Any) -> None:
        if self.checkpoint.attempt_status.casefold() != "unknown":
            raise ValueError("Probe results require an UNKNOWN execution checkpoint")


class PackLifecycleError(RuntimeError):
    """Code-bearing boundary for validation/planning failures, not lifecycle flow."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def validate_pack_admission_result(
    value: RuntimeValue,
    *,
    prepared: PreparedRunReference,
) -> PackAdmissionResult:
    """Validate the closed admission envelope at the platform boundary."""

    try:
        result = PackAdmissionResult.model_validate(value)
    except (TypeError, ValueError) as exc:
        raise PackLifecycleError("PACK_ADMISSION_RESULT_INVALID") from exc
    if result.prepared != prepared:
        raise PackLifecycleError("PACK_ADMISSION_RESULT_CORRELATION_MISMATCH")
    return result


def validate_pack_advance_result(
    value: RuntimeValue,
    *,
    run_id: str,
) -> PackAdvanceResult:
    """Validate and correlate one closed Pack advance result."""

    try:
        result = PackAdvanceResult.model_validate(value)
    except (TypeError, ValueError) as exc:
        raise PackLifecycleError("PACK_ADVANCE_RESULT_INVALID") from exc
    if result.run_id != run_id:
        raise PackLifecycleError("PACK_ADVANCE_RESULT_CORRELATION_MISMATCH")
    return result


def validate_pack_probe_result(
    value: RuntimeValue,
    *,
    run_id: str,
    native_task_id: str,
    native_step_id: str,
    permit_id: str,
    attempt_id: str,
) -> PackProbeResult:
    """Validate a probe result against the exact durable execution identity.

    The checkpoint task ID belongs to the Pack's native child execution. It is
    intentionally correlated with ``native_task_id`` rather than the Agent Run
    root ID supplied by ``run_id``.
    """

    try:
        result = PackProbeResult.model_validate(value)
    except (TypeError, ValueError) as exc:
        raise PackLifecycleError("PACK_PROBE_RESULT_INVALID") from exc
    checkpoint = result.checkpoint
    if (
        checkpoint.task_id != native_task_id
        or checkpoint.step_id != native_step_id
        or checkpoint.permit_id != permit_id
        or checkpoint.attempt_id != attempt_id
    ):
        raise PackLifecycleError("PACK_PROBE_RESULT_CORRELATION_MISMATCH")
    if not run_id:
        raise PackLifecycleError("PACK_PROBE_RESULT_CORRELATION_MISMATCH")
    return result


ApprovalHandler = Callable[
    [PreparedRunReference, ApprovalRequestSpecification, str],
    Awaitable[BaseModel | None],
]


@runtime_checkable
class PackRuntimeAdapter(Protocol):
    """Authority-minimized application boundary for a trusted Pack implementation.

    The protocol deliberately uses opaque values for compiled and durable runtime
    artifacts. Core governance can validate and dispatch adapters without importing
    any Domain Pack implementation or learning browser/authority internals.
    """

    @property
    def binding(self) -> PackRuntimeBinding: ...

    def model_safe_projection(self, authority: BaseModel) -> ModelSafeRuntimeProjection: ...

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

    def __init__(
        self,
        contracts: Iterable[PackRuntimeContract],
        *,
        installations: Iterable[RuntimeInstallation] = (),
        trusted_adapter_refs: Mapping[tuple[str, str] | str, str] | None = None,
        now: datetime | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        installation_items = tuple(installations)
        self._contracts: dict[tuple[str, str], PackRuntimeContract] = {}
        for contract in contracts:
            if not isinstance(contract, PackRuntimeContract):
                raise TypeError("Pack runtime registry accepts runtime contracts only")
            key = (contract.pack_id, contract.pack_version)
            if key in self._contracts:
                raise ValueError(f"A runtime contract is already registered for {contract.pack_id}@{contract.pack_version}")
            self._contracts[key] = contract
        self._adapters: dict[tuple[str, str], PackRuntimeAdapter] = {}
        self._installations: dict[tuple[str, str, str], RuntimeInstallation] = {}
        self._trusted_adapter_refs = dict(trusted_adapter_refs or {})
        self._now = now
        self._clock = clock or (lambda: datetime.now(timezone.utc)) if now is None else clock
        if installation_items and trusted_adapter_refs is None:
            raise ValueError("Tenant-scoped runtime registry requires trusted adapter references")
        for installation in installation_items:
            self.bind_installation(installation, now=now)

    @classmethod
    def from_active_domain_pack_set(
        cls,
        active: "ActiveDomainPackSet",
        contracts: Iterable[PackRuntimeContract],
        *,
        now: datetime | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> "PackRuntimeRegistry":
        """Create a tenant-scoped runtime registry from an active installation set."""

        if not active.installations:
            raise LookupError("Cannot create a runtime registry without an accepted Domain Pack installation")
        return cls(
            contracts,
            installations=active.installations,
            trusted_adapter_refs={
                (pack_id, pack_version): adapter_ref
                for pack_id, pack_version, adapter_ref in active.trusted_adapter_refs
            },
            now=active.validated_at if now is None else now,
            clock=clock or (lambda: datetime.now(timezone.utc)),
        )

    @property
    def tenant_scoped(self) -> bool:
        return bool(self._installations)

    def _effective_now(self, now: datetime | None) -> datetime | None:
        if now is not None:
            return now
        if self._clock is not None:
            return self._clock()
        return self._now

    def validate_binding(self, binding: PackRuntimeBinding) -> None:
        """Validate immutable binding identity without resolving a tenant adapter."""

        contract = self._contracts.get((binding.pack_id, binding.pack_version))
        if contract is None:
            raise LookupError("No runtime contract matches the Pack binding")
        if tuple(sorted(binding.capability_ids)) != tuple(sorted(contract.capability_ids)):
            raise LookupError("Runtime binding capabilities do not match the Pack runtime contract")
        if binding.adapter_id != contract.adapter_id:
            raise LookupError("Runtime binding adapter identity does not match the Pack runtime contract")

    def bind_installation(self, installation: RuntimeInstallation, *, now: datetime | None = None) -> None:
        """Pin a runtime contract to one accepted tenant installation."""

        if not isinstance(installation, RuntimeInstallation):
            raise TypeError("Runtime registry accepts Domain Pack installations only")
        status = getattr(getattr(installation, "status", None), "value", getattr(installation, "status", None))
        if status != "accepted":
            raise ValueError("Only accepted Domain Pack installations may bind a runtime")
        tenant_id = installation.tenant_id
        pack_id = installation.pack_id
        pack_version = installation.pack_version
        if not all(isinstance(value, str) and value for value in (tenant_id, pack_id, pack_version)):
            raise ValueError("Domain Pack installation must carry tenant, Pack, and version identity")
        effective_now = self._effective_now(now)
        accepted_at = installation.accepted_at
        expires_at = installation.expires_at
        if effective_now is not None and (not accepted_at <= effective_now < expires_at):
            raise ValueError("Accepted Domain Pack installation is stale")
        key = (pack_id, pack_version)
        contract = self._contracts.get(key)
        if contract is None:
            raise ValueError("Domain Pack installation has no matching runtime contract")
        if installation.contract_digest != contract.manifest_digest:
            raise ValueError("Domain Pack installation digest does not match its runtime contract")
        expected_adapter_ref = self._trusted_adapter_refs.get((pack_id, pack_version))
        if expected_adapter_ref is None:
            expected_adapter_ref = self._trusted_adapter_refs.get(pack_id)
        if expected_adapter_ref is None or installation.adapter_ref != expected_adapter_ref:
            raise ValueError("Domain Pack installation adapter reference is not trusted")
        enabled = tuple(installation.enabled_capability_ids)
        if not enabled or len(enabled) != len(set(enabled)) or not set(enabled) <= set(contract.capability_ids):
            raise ValueError("Domain Pack installation capabilities do not resolve to its runtime contract")
        installation_key = (tenant_id, pack_id, pack_version)
        if installation_key in self._installations and self._installations[installation_key] != installation:
            raise ValueError("A tenant already has a different installation for this Pack version")
        self._installations[installation_key] = installation

    def register_for_installation(
        self,
        adapter: PackRuntimeAdapter,
        installation: RuntimeInstallation,
        *,
        now: datetime | None = None,
    ) -> None:
        """Bind and register an adapter as one explicit tenant installation."""

        self.register(adapter, installation=installation, now=now)

    def require_for_tenant(
        self,
        *,
        tenant_id: str,
        pack_id: str,
        pack_version: str,
        capability_ids: Iterable[str] | None = None,
        adapter_id: str | None = None,
        now: datetime | None = None,
    ) -> PackRuntimeAdapter:
        """Resolve an adapter only when the exact tenant installation is active."""

        installation = self._installations.get((tenant_id, pack_id, pack_version))
        if installation is None:
            raise LookupError(f"No active Domain Pack installation matches {tenant_id}:{pack_id}@{pack_version}")
        effective_now = self._effective_now(now)
        if effective_now is not None and not (
            installation.accepted_at <= effective_now < installation.expires_at
        ):
            raise LookupError("Domain Pack installation is stale")
        expected_capabilities = tuple(sorted(installation.enabled_capability_ids))
        if capability_ids is not None and not set(capability_ids) <= set(expected_capabilities):
            raise LookupError("Requested capabilities are not enabled by the active Domain Pack installation")
        adapter = self._require_registered(pack_id=pack_id, pack_version=pack_version)
        if adapter_id is not None and adapter.binding.adapter_id != adapter_id:
            raise LookupError("Requested adapter identity does not match the active Domain Pack installation")
        return adapter

    def require_binding(self, binding: PackRuntimeBinding) -> PackRuntimeAdapter:
        """Resolve an adapter only when every immutable binding field matches."""

        if self.tenant_scoped:
            raise LookupError("Tenant-scoped runtime lookup requires an explicit tenant installation")
        adapter = self._require_registered(pack_id=binding.pack_id, pack_version=binding.pack_version)
        if adapter.binding != binding:
            raise LookupError("No conformant runtime adapter matches the exact Pack binding")
        return adapter

    def resolve_for_execution(
        self,
        *,
        tenant_id: str,
        binding: PackRuntimeBinding,
        now: datetime | None = None,
        capability_ids: Iterable[str] | None = None,
    ) -> PackRuntimeAdapter:
        """Resolve an adapter for execution through an exact tenant binding only."""

        if not self._installations:
            raise LookupError("No active Domain Pack installation is available for execution")
        installation = self._installations.get((tenant_id, binding.pack_id, binding.pack_version))
        if installation is None:
            raise LookupError("No active Domain Pack installation matches the exact runtime binding")
        effective_now = self._effective_now(now)
        if effective_now is not None and not installation.accepted_at <= effective_now < installation.expires_at:
            raise LookupError("Domain Pack installation is stale")
        requested = tuple(sorted(capability_ids if capability_ids is not None else binding.capability_ids))
        enabled = tuple(sorted(installation.enabled_capability_ids))
        if not set(requested) <= set(enabled) or not set(requested) <= set(binding.capability_ids):
            raise LookupError("Requested capabilities are not enabled by the exact tenant runtime binding")
        adapter = self._require_registered(pack_id=binding.pack_id, pack_version=binding.pack_version)
        if adapter.binding != binding:
            raise LookupError("No conformant runtime adapter matches the exact Pack binding")
        return adapter

    def register(
        self,
        adapter: PackRuntimeAdapter,
        *,
        installation: RuntimeInstallation | None = None,
        now: datetime | None = None,
    ) -> None:
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
        if self._installations and installation is None:
            raise ValueError("Tenant-scoped runtime registration requires an explicit installation")
        if installation is not None:
            if (
                installation.pack_id != binding.pack_id
                or installation.pack_version != binding.pack_version
            ):
                raise ValueError("Runtime adapter identity does not match the Domain Pack installation")
            self.bind_installation(installation, now=now)
        self._adapters[key] = adapter

    def require(self, *, pack_id: str, pack_version: str) -> PackRuntimeAdapter:
        if self.tenant_scoped:
            raise LookupError("Tenant-scoped runtime lookup is required for an installed Pack")
        return self._require_registered(pack_id=pack_id, pack_version=pack_version)

    def _require_registered(self, *, pack_id: str, pack_version: str) -> PackRuntimeAdapter:
        try:
            return self._adapters[(pack_id, pack_version)]
        except KeyError as exc:
            raise LookupError("No conformant runtime adapter is registered for this Pack version") from exc

    def public_metadata_for_tenant(
        self,
        *,
        tenant_id: str,
        pack_id: str,
        pack_version: str,
    ) -> PublicPackRuntimeMetadata:
        self.require_for_tenant(tenant_id=tenant_id, pack_id=pack_id, pack_version=pack_version)
        return self._public_metadata(pack_id=pack_id, pack_version=pack_version)

    def public_metadata(self, *, pack_id: str, pack_version: str) -> PublicPackRuntimeMetadata:
        self._require_registered(pack_id=pack_id, pack_version=pack_version)
        if self._installations:
            raise LookupError("Tenant-scoped runtime metadata lookup is required for an installed Pack")
        return self._public_metadata(pack_id=pack_id, pack_version=pack_version)

    def _public_metadata(self, *, pack_id: str, pack_version: str) -> PublicPackRuntimeMetadata:
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
    return "run_" + hashlib.sha256(material.encode("utf-8")).hexdigest()
