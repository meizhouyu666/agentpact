from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from enterprise.governance.pack_runtime import (
    PackAdvanceStatus,
    PackProbeStatus,
    PackRunRequest,
    PackRuntimeRegistry,
)
from tests.recorded_pack_runtime_fixture import (
    RECORDED_ORDER_CONTRACT,
    RecordedOrdersRuntimeAdapter,
)


async def test_independent_recorded_adapter_runs_the_complete_generic_lifecycle() -> None:
    adapter = RecordedOrdersRuntimeAdapter()
    registry = PackRuntimeRegistry([RECORDED_ORDER_CONTRACT])
    registry.register(adapter)
    prepared = adapter.prepare_run(
        PackRunRequest(
            tenant_id="orders-tenant",
            request_id="orders-request-1",
            intent_digest="e" * 64,
            business_inputs={"order_id": "order-1", "version": 1},
            target_url="https://orders.example.test/recorded",
            principal={"principal_id": "orders-operator"},
            now=datetime(2026, 8, 31, 16, 0, tzinfo=timezone.utc),
        )
    )
    approval_calls = []

    async def approval_handler(reference, approval, operation_key):
        approval_calls.append((reference, approval, operation_key))
        return {"approval_id": "recorded-order-approval"}

    admitted = await adapter.admit_run(
        prepared,
        approval_handler=approval_handler,
        operation_key="orders-admit",
    )
    assert admitted.initial.status is PackAdvanceStatus.AWAITING_APPROVAL
    assert len(approval_calls) == 1
    assert adapter.store.permits == []
    assert adapter.store.attempts == []

    adapter.approved = True
    advanced = await adapter.advance_run(
        prepared,
        approval_handler=approval_handler,
        operation_key="orders-advance",
    )
    assert advanced.status is PackAdvanceStatus.PENDING_RESULT_PROBE
    assert advanced.execution_checkpoint is not None
    assert adapter.runtime.preflights == 1
    assert adapter.runtime.browser_calls == 1
    assert adapter.store.permits[0].status == "consumed"
    assert adapter.store.attempts[0].status == "unknown"
    assert [event.stage for event in adapter.events.events] == [
        "observation",
        "decision",
        "policy",
        "action",
        "action",
        "verification",
        "terminal",
    ]

    probed = await adapter.probe_run(prepared, operation_key="orders-probe")
    assert probed.status is PackProbeStatus.CONFIRMED
    assert probed.checkpoint == advanced.execution_checkpoint


def test_recorded_adapter_fixture_has_no_synthetic_dependency() -> None:
    source = (Path(__file__).resolve().parents[1] / "recorded_pack_runtime_fixture.py").read_text(
        encoding="utf-8"
    )
    assert "synthetic_payment" not in source
    assert "synthetic.payment" not in source

