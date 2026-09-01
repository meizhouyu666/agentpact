"""Independent approval-required external-write Pack fixture.

This fixture intentionally models only the generic Pack runtime boundary.  The
mutable state object stands in for durable state owned by an external system so
tests can replace the adapter instance (a process restart) without replaying a
write.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone

from enterprise.governance.pack_runtime import (
    ApprovalHandler,
    ApprovalRequestSpecification,
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

FAKE_WRITE_PACK_ID = "fake.external-write"
FAKE_WRITE_PACK_VERSION = "2.0.0"
FAKE_WRITE_PACK_DISPLAY_NAME = "Fake External Write Pack"
FAKE_WRITE_CAPABILITY_IDS = ("fake.external-write.submit",)
FAKE_WRITE_ADAPTER_ID = "tests.fake-external-write-adapter.v2"
FAKE_WRITE_BUSINESS_INPUTS = {"resource_key": "external-record-1", "object_version": 1}

FAKE_WRITE_RUNTIME_CONTRACT = PackRuntimeContract(
    pack_id=FAKE_WRITE_PACK_ID,
    pack_version=FAKE_WRITE_PACK_VERSION,
    display_name=FAKE_WRITE_PACK_DISPLAY_NAME,
    capability_ids=FAKE_WRITE_CAPABILITY_IDS,
    adapter_id=FAKE_WRITE_ADAPTER_ID,
    manifest_digest="e" * 64,
)
FAKE_WRITE_RUNTIME_BINDING = PackRuntimeBinding(
    pack_id=FAKE_WRITE_PACK_ID,
    pack_version=FAKE_WRITE_PACK_VERSION,
    capability_ids=FAKE_WRITE_CAPABILITY_IDS,
    adapter_id=FAKE_WRITE_ADAPTER_ID,
)


def _coerce_probe_status(value: PackProbeStatus | str) -> PackProbeStatus:
    """Accept the platform's INCONCLUSIVE spelling and a test-friendly UNKNOWN alias."""

    if isinstance(value, PackProbeStatus):
        return value
    if value.upper() == "UNKNOWN":
        return PackProbeStatus.INCONCLUSIVE
    return PackProbeStatus(value.upper())


@dataclass
class FakeWritePackState:
    """Durable test state shared by adapter instances across a restart."""

    approved_runs: set[str] = field(default_factory=set)
    approval_requests: dict[str, ApprovalRequestSpecification] = field(default_factory=dict)
    checkpoints: dict[str, ExecutionCheckpoint] = field(default_factory=dict)
    probe_status: dict[str, PackProbeStatus] = field(default_factory=dict)
    write_calls: int = 0
    probe_calls: int = 0

    def approve(self, run_id: str) -> None:
        self.approved_runs.add(run_id)


class FakeWritePackAdapter:
    """Deterministic approval/write/probe adapter with no business runtime imports."""

    def __init__(
        self,
        state: FakeWritePackState | None = None,
        *,
        probe_status: PackProbeStatus | str = PackProbeStatus.CONFIRMED,
        execution_failure: bool = False,
    ) -> None:
        self.state = state if state is not None else FakeWritePackState()
        self._probe_status = _coerce_probe_status(probe_status)
        self._execution_failure = execution_failure

    @property
    def binding(self) -> PackRuntimeBinding:
        return FAKE_WRITE_RUNTIME_BINDING

    def model_safe_projection(self, authority: object) -> ModelSafeRuntimeProjection:
        del authority
        return ModelSafeRuntimeProjection(
            pack_id=self.binding.pack_id,
            pack_version=self.binding.pack_version,
            capability_ids=self.binding.capability_ids,
            input_slot_names=tuple(FAKE_WRITE_BUSINESS_INPUTS),
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
            raise ValueError("Fake write adapter binding mismatch")
        expected_run_id = derive_pack_run_id(tenant_id=request.tenant_id, request_id=request.request_id)
        if request.run_id != expected_run_id:
            raise ValueError("Fake write run identity mismatch")
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

    def _approval_specification(self, prepared: PreparedRunReference) -> ApprovalRequestSpecification:
        digest = hashlib.sha256(f"{prepared.run_id}:fake-external-write".encode("utf-8")).hexdigest()
        return ApprovalRequestSpecification(
            task_id=f"task-{prepared.request_id}",
            step_id=f"step-{prepared.request_id}",
            contract_id=prepared.contract_id or f"contract-{prepared.request_id}",
            organization_id=prepared.tenant_id,
            intent_id=f"intent-{digest}",
            action_fingerprint=digest,
            observation_hash=hashlib.sha256(f"{digest}:observation".encode("utf-8")).hexdigest(),
            requested_approval_route="fake-write:approver",
            source_department_id="fake-write-operations",
            risk_level="high",
            effect="external_write",
            expires_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
            reason_code="FAKE_WRITE_APPROVAL_REQUIRED",
            redacted_description="Submit one fake external write",
            policy_decision={
                "decision_id": f"decision-{prepared.request_id}",
                "intent_id": f"intent-{digest}",
                "outcome": "require_approval",
                "risk_level": "high",
                "reasons": ["External writes require an approver"],
                "matched_rules": ["fake-write-approval-policy"],
                "required_approver": {"department_id": "fake-write", "role": "approver"},
                "policy_version": "fake-write-policy.v1",
            },
        )

    async def admit_run(
        self,
        prepared: PreparedRunReference,
        *,
        approval_handler: ApprovalHandler,
        operation_key: str,
    ) -> PackAdmissionResult:
        specification = self._approval_specification(prepared)
        self.state.approval_requests[prepared.run_id] = specification
        await approval_handler(prepared, specification, operation_key)
        return PackAdmissionResult(
            prepared=prepared,
            admission_id=prepared.admission_id or f"admission-{prepared.request_id}",
            initial=PackAdvanceResult(
                status=PackAdvanceStatus.AWAITING_APPROVAL,
                run_id=prepared.run_id,
                step_id=specification.step_id,
                reason_code=specification.reason_code,
                approval=specification,
            ),
        )

    def _checkpoint(self, prepared: PreparedRunReference) -> ExecutionCheckpoint:
        action_fingerprint = hashlib.sha256(f"{prepared.run_id}:action".encode("utf-8")).hexdigest()
        observation_hash = hashlib.sha256(f"{prepared.run_id}:observation".encode("utf-8")).hexdigest()
        idempotency_key_digest = hashlib.sha256(f"{prepared.run_id}:idempotency".encode("utf-8")).hexdigest()
        return ExecutionCheckpoint(
            permit_id=f"permit-{prepared.request_id}",
            attempt_id=f"attempt-{prepared.request_id}",
            task_id=prepared.run_id,
            step_id=f"step-{prepared.request_id}",
            action_fingerprint=action_fingerprint,
            observation_hash=observation_hash,
            idempotency_key_digest=idempotency_key_digest,
            execution_effect="external_write",
            result_probe_ref=f"fake-write://result-probe/{prepared.request_id}",
            attempt_status="unknown",
        )

    async def advance_run(
        self,
        prepared: PreparedRunReference,
        *,
        approval_handler: ApprovalHandler,
        operation_key: str,
    ) -> PackAdvanceResult:
        del approval_handler, operation_key
        if prepared.run_id not in self.state.approved_runs:
            raise ValueError("FAKE_WRITE_APPROVAL_REQUIRED")
        existing = self.state.checkpoints.get(prepared.run_id)
        if existing is not None:
            return PackAdvanceResult(
                status=PackAdvanceStatus.PENDING_RESULT_PROBE,
                run_id=prepared.run_id,
                step_id=existing.step_id,
                reason_code="FAKE_WRITE_RESULT_UNCERTAIN",
                execution_checkpoint=existing,
            )
        self.state.write_calls += 1
        if self._execution_failure:
            return PackAdvanceResult(
                status=PackAdvanceStatus.FAILED,
                run_id=prepared.run_id,
                step_id=f"step-{prepared.request_id}",
                reason_code="FAKE_WRITE_EXECUTION_FAILED",
            )
        checkpoint = self._checkpoint(prepared)
        self.state.checkpoints[prepared.run_id] = checkpoint
        self.state.probe_status.setdefault(prepared.run_id, self._probe_status)
        return PackAdvanceResult(
            status=PackAdvanceStatus.PENDING_RESULT_PROBE,
            run_id=prepared.run_id,
            step_id=checkpoint.step_id,
            reason_code="FAKE_WRITE_RESULT_UNCERTAIN",
            execution_checkpoint=checkpoint,
        )

    async def probe_run(self, prepared: PreparedRunReference, *, operation_key: str) -> PackProbeResult:
        del operation_key
        try:
            checkpoint = self.state.checkpoints[prepared.run_id]
        except KeyError as exc:
            raise ValueError("Fake write has no exact UNKNOWN checkpoint") from exc
        self.state.probe_calls += 1
        status = _coerce_probe_status(self.state.probe_status.get(prepared.run_id, self._probe_status))
        reason_codes = {
            PackProbeStatus.CONFIRMED: "FAKE_WRITE_RESULT_CONFIRMED",
            PackProbeStatus.NOT_CONFIRMED: "FAKE_WRITE_RESULT_NOT_CONFIRMED",
            PackProbeStatus.INCONCLUSIVE: "FAKE_WRITE_RESULT_UNKNOWN",
        }
        return PackProbeResult(
            status=status,
            checkpoint=checkpoint,
            reason_code=reason_codes[status],
            evidence_refs=(f"fake-write://evidence/{status.value.lower()}/{prepared.request_id}",),
        )


__all__ = [
    "FAKE_WRITE_ADAPTER_ID",
    "FAKE_WRITE_BUSINESS_INPUTS",
    "FAKE_WRITE_CAPABILITY_IDS",
    "FAKE_WRITE_PACK_DISPLAY_NAME",
    "FAKE_WRITE_PACK_ID",
    "FAKE_WRITE_PACK_VERSION",
    "FAKE_WRITE_RUNTIME_BINDING",
    "FAKE_WRITE_RUNTIME_CONTRACT",
    "FakeWritePackAdapter",
    "FakeWritePackState",
]
