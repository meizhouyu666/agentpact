from __future__ import annotations

from datetime import datetime, timezone

import pytest

from enterprise.governance.pack_runtime import (
    PackAdvanceStatus,
    PackProbeStatus,
    PackRunRequest,
    PackRunRestoreRequest,
    PackRuntimeAdapter,
    PackRuntimeRegistry,
)
from tests.fixtures.fake_domain_pack import (
    FAKE_RUNTIME_CONTRACT,
    FakeDomainPackAdapter,
)
from tests.fixtures.fake_write_pack import (
    FAKE_WRITE_PACK_DISPLAY_NAME,
    FAKE_WRITE_PACK_ID,
    FAKE_WRITE_PACK_VERSION,
    FAKE_WRITE_RUNTIME_CONTRACT,
    FakeWritePackAdapter,
    FakeWritePackState,
)


def _request(request_id: str = "fake-write-request") -> PackRunRequest:
    return PackRunRequest(
        tenant_id="fake-write-tenant",
        request_id=request_id,
        intent_digest="a" * 64,
        business_inputs={"resource_key": "external-record-1", "object_version": 1},
        target_url="https://fake-write.example.test",
        principal={"principal_id": "fake-write-operator"},
        now=datetime(2026, 8, 31, 16, 0, tzinfo=timezone.utc),
    )


def test_read_and_write_packs_register_together_without_identity_collisions() -> None:
    registry = PackRuntimeRegistry([FAKE_RUNTIME_CONTRACT, FAKE_WRITE_RUNTIME_CONTRACT])
    read_adapter = FakeDomainPackAdapter()
    write_adapter = FakeWritePackAdapter()

    assert isinstance(read_adapter, PackRuntimeAdapter)
    assert isinstance(write_adapter, PackRuntimeAdapter)
    registry.register(read_adapter)
    registry.register(write_adapter)

    assert registry.registered_bindings == (read_adapter.binding, write_adapter.binding)
    assert registry.require(pack_id=FAKE_WRITE_PACK_ID, pack_version=FAKE_WRITE_PACK_VERSION) is write_adapter
    assert registry.public_metadata(
        pack_id=FAKE_WRITE_PACK_ID,
        pack_version=FAKE_WRITE_PACK_VERSION,
    ).model_dump() == {
        "pack_id": FAKE_WRITE_PACK_ID,
        "pack_version": FAKE_WRITE_PACK_VERSION,
        "display_name": FAKE_WRITE_PACK_DISPLAY_NAME,
    }
    assert read_adapter.binding.pack_id != write_adapter.binding.pack_id
    assert read_adapter.binding.capability_ids != write_adapter.binding.capability_ids
    assert read_adapter.binding.adapter_id != write_adapter.binding.adapter_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("probe_status", "reason_code"),
    [
        (PackProbeStatus.CONFIRMED, "FAKE_WRITE_RESULT_CONFIRMED"),
        (PackProbeStatus.NOT_CONFIRMED, "FAKE_WRITE_RESULT_NOT_CONFIRMED"),
    ],
)
async def test_approval_write_advance_and_probe_outcomes_use_generic_contract(
    probe_status: PackProbeStatus,
    reason_code: str,
) -> None:
    state = FakeWritePackState()
    adapter = FakeWritePackAdapter(state, probe_status=probe_status)
    prepared = adapter.prepare_run(_request())
    approval_calls = []

    async def approval_handler(reference, specification, operation_key):
        approval_calls.append((reference, specification, operation_key))
        return {"approval_id": "fake-write-approval"}

    admitted = await adapter.admit_run(
        prepared,
        approval_handler=approval_handler,
        operation_key="fake-write-admit",
    )
    assert admitted.initial.status is PackAdvanceStatus.AWAITING_APPROVAL
    assert admitted.initial.approval == state.approval_requests[prepared.run_id]
    assert approval_calls[0][0] == prepared
    assert approval_calls[0][2] == "fake-write-admit"
    assert state.write_calls == 0

    with pytest.raises(ValueError, match="APPROVAL_REQUIRED"):
        await adapter.advance_run(
            prepared,
            approval_handler=approval_handler,
            operation_key="fake-write-before-approval",
        )

    state.approve(prepared.run_id)
    advanced = await adapter.advance_run(
        prepared,
        approval_handler=approval_handler,
        operation_key="fake-write-advance",
    )
    checkpoint = advanced.execution_checkpoint
    assert advanced.status is PackAdvanceStatus.PENDING_RESULT_PROBE
    assert checkpoint is not None
    assert checkpoint.task_id == prepared.run_id
    assert checkpoint.attempt_status == "unknown"
    assert checkpoint.execution_effect == "external_write"
    assert state.write_calls == 1

    probed = await adapter.probe_run(prepared, operation_key="fake-write-probe")
    assert probed.status is probe_status
    assert probed.reason_code == reason_code
    assert probed.checkpoint == checkpoint
    assert probed.evidence_refs


@pytest.mark.asyncio
async def test_failed_external_write_is_terminal_and_has_no_probe_checkpoint() -> None:
    state = FakeWritePackState()
    adapter = FakeWritePackAdapter(state, execution_failure=True)
    prepared = adapter.prepare_run(_request("fake-write-failure"))

    async def approval_handler(*_args):
        return {"approval_id": "fake-write-approval"}

    await adapter.admit_run(prepared, approval_handler=approval_handler, operation_key="admit")
    state.approve(prepared.run_id)
    failed = await adapter.advance_run(prepared, approval_handler=approval_handler, operation_key="advance")

    assert failed.status is PackAdvanceStatus.FAILED
    assert failed.reason_code == "FAKE_WRITE_EXECUTION_FAILED"
    assert failed.execution_checkpoint is None
    assert prepared.run_id not in state.checkpoints
    assert state.write_calls == 1
    with pytest.raises(ValueError, match="no exact UNKNOWN checkpoint"):
        await adapter.probe_run(prepared, operation_key="probe")


@pytest.mark.asyncio
async def test_unknown_probe_and_restart_restore_never_replay_the_write() -> None:
    state = FakeWritePackState()
    adapter = FakeWritePackAdapter(state, probe_status=PackProbeStatus.INCONCLUSIVE)
    prepared = adapter.prepare_run(_request("fake-write-restart"))

    async def approval_handler(*_args):
        return {"approval_id": "fake-write-approval"}

    await adapter.admit_run(prepared, approval_handler=approval_handler, operation_key="admit")
    state.approve(prepared.run_id)
    pending = await adapter.advance_run(prepared, approval_handler=approval_handler, operation_key="advance")
    assert pending.status is PackAdvanceStatus.PENDING_RESULT_PROBE

    restarted = FakeWritePackAdapter(state)
    restored = restarted.restore_run(
        PackRunRestoreRequest(
            run_id=prepared.run_id,
            tenant_id=prepared.tenant_id,
            request_id=prepared.request_id,
            binding=restarted.binding,
            provider_mode=prepared.provider_mode,
            target_url="https://fake-write.example.test",
            admission_payload=prepared.opaque_payload,
        )
    )
    assert restored == prepared

    resumed = await restarted.advance_run(restored, approval_handler=approval_handler, operation_key="resume")
    assert resumed.status is PackAdvanceStatus.PENDING_RESULT_PROBE
    assert resumed.execution_checkpoint == pending.execution_checkpoint
    assert state.write_calls == 1

    unknown = await restarted.probe_run(restored, operation_key="probe")
    assert unknown.status is PackProbeStatus.INCONCLUSIVE
    assert unknown.reason_code == "FAKE_WRITE_RESULT_UNKNOWN"
    assert unknown.checkpoint == pending.execution_checkpoint
    assert state.probe_calls == 1
