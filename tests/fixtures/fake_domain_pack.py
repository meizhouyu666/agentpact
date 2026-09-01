"""Generic Domain Pack fixture for platform-contract tests."""

from __future__ import annotations

from enterprise.governance.pack_runtime import (
    ApprovalHandler,
    ExecutionCheckpoint,
    ModelSafeRuntimeProjection,
    PackAdmissionResult,
    PackAdvanceResult,
    PackAdvanceStatus,
    PackProbeResult,
    PackProbeStatus,
    PackRunRequest,
    PackRunRestoreRequest,
    PackRuntimeBinding,
    PackRuntimeContract,
    PreparedRunReference,
    derive_pack_run_id,
)

FAKE_PACK_ID = "fake.domain"
FAKE_PACK_VERSION = "1.0.0"
FAKE_PACK_DISPLAY_NAME = "Fake Domain Pack"
FAKE_CAPABILITY_IDS = ("fake.domain.execute",)
FAKE_ADAPTER_ID = "tests.fake-domain-adapter.v1"
FAKE_BUSINESS_INPUTS = {"record_id": "fake-record-secret", "object_version": 1}

FAKE_RUNTIME_CONTRACT = PackRuntimeContract(
    pack_id=FAKE_PACK_ID,
    pack_version=FAKE_PACK_VERSION,
    display_name=FAKE_PACK_DISPLAY_NAME,
    capability_ids=FAKE_CAPABILITY_IDS,
    adapter_id=FAKE_ADAPTER_ID,
    manifest_digest="f" * 64,
)
FAKE_RUNTIME_BINDING = PackRuntimeBinding(
    pack_id=FAKE_PACK_ID,
    pack_version=FAKE_PACK_VERSION,
    capability_ids=FAKE_CAPABILITY_IDS,
    adapter_id=FAKE_ADAPTER_ID,
)


class FakeDomainPackAdapter:
    """Small deterministic adapter with no concrete-Pack business semantics."""

    def __init__(self, binding: PackRuntimeBinding = FAKE_RUNTIME_BINDING) -> None:
        self._binding = binding

    @property
    def binding(self) -> PackRuntimeBinding:
        return self._binding

    def model_safe_projection(self, authority: object) -> ModelSafeRuntimeProjection:
        del authority
        return ModelSafeRuntimeProjection(
            pack_id=self.binding.pack_id,
            pack_version=self.binding.pack_version,
            capability_ids=self.binding.capability_ids,
            input_slot_names=tuple(FAKE_BUSINESS_INPUTS),
        )

    def prepare_run(self, request: PackRunRequest) -> PreparedRunReference:
        return PreparedRunReference(
            run_id=derive_pack_run_id(tenant_id=request.tenant_id, request_id=request.request_id),
            tenant_id=request.tenant_id,
            request_id=request.request_id,
            pack_id=self.binding.pack_id,
            pack_version=self.binding.pack_version,
            adapter_id=self.binding.adapter_id,
            admission_id=f"admission-{request.request_id}",
            contract_id=f"contract-{request.request_id}",
            provider_mode="recorded",
            opaque_payload={"business_inputs": request.business_inputs},
        )

    def restore_run(self, request: PackRunRestoreRequest) -> PreparedRunReference:
        if request.binding != self.binding:
            raise ValueError("Fake adapter binding mismatch")
        return PreparedRunReference(
            run_id=request.run_id,
            tenant_id=request.tenant_id,
            request_id=request.request_id,
            pack_id=request.binding.pack_id,
            pack_version=request.binding.pack_version,
            adapter_id=request.binding.adapter_id,
            admission_id=f"admission-{request.request_id}",
            contract_id=f"contract-{request.request_id}",
            provider_mode=request.provider_mode,
            opaque_payload=request.admission_payload,
        )

    async def admit_run(
        self,
        prepared: PreparedRunReference,
        *,
        approval_handler: ApprovalHandler,
        operation_key: str,
    ) -> PackAdmissionResult:
        del approval_handler, operation_key
        return PackAdmissionResult(
            prepared=prepared,
            admission_id=prepared.admission_id or f"admission-{prepared.request_id}",
            initial=PackAdvanceResult(status=PackAdvanceStatus.COMPLETED, run_id=prepared.run_id),
        )

    async def advance_run(
        self,
        prepared: PreparedRunReference,
        *,
        approval_handler: ApprovalHandler,
        operation_key: str,
    ) -> PackAdvanceResult:
        del approval_handler, operation_key
        return PackAdvanceResult(status=PackAdvanceStatus.COMPLETED, run_id=prepared.run_id)

    async def probe_run(self, prepared: PreparedRunReference, *, operation_key: str) -> PackProbeResult:
        del operation_key
        checkpoint = ExecutionCheckpoint(
            permit_id=f"permit-{prepared.request_id}",
            attempt_id=f"attempt-{prepared.request_id}",
            task_id=prepared.run_id,
            step_id=f"step-{prepared.request_id}",
            action_fingerprint="a" * 64,
            observation_hash="b" * 64,
            idempotency_key_digest="c" * 64,
            execution_effect="external_write",
            result_probe_ref="fake://result-probe",
            attempt_status="unknown",
        )
        return PackProbeResult(
            status=PackProbeStatus.CONFIRMED,
            checkpoint=checkpoint,
            reason_code="FAKE_RESULT_CONFIRMED",
            evidence_refs=("fake://evidence/confirmed",),
        )
