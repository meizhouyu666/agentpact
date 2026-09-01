"""Agent Run native persistence-boundary contracts and fake-store coverage."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from enterprise.agent_runs.journal import GovernedPlanCheckpoint, GovernedPlanStepRef
from enterprise.agent_runs.persistence import (
    AgentRunStepSnapshot,
    AgentRunStepStatus,
    AgentRunTaskSnapshot,
    AgentRunTaskStatus,
)
from enterprise.agent_runs.service import AgentRunService
from enterprise.domains.synthetic_payment.constants import TENANT_ID
from enterprise.integrations.skyvern_agent_run_store import SkyvernAgentRunStore


def test_agent_run_core_has_no_direct_skyvern_imports() -> None:
    for path in Path("enterprise/agent_runs").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert "from skyvern" not in source
        assert "import skyvern" not in source


def test_skyvern_adapter_maps_unknown_statuses_to_native_unknown() -> None:
    now = datetime.now(timezone.utc)
    task = SkyvernAgentRunStore._task_snapshot(
        SimpleNamespace(
            task_id="run_m10_unknown",
            organization_id=TENANT_ID,
            status="future_task_status",
            created_at=now,
            modified_at=now,
        )
    )
    step = SkyvernAgentRunStore._step_snapshot(
        SimpleNamespace(
            step_id="step_unknown",
            task_id=task.task_id,
            organization_id=TENANT_ID,
            status="future_step_status",
        )
    )
    assert task.status is AgentRunTaskStatus.UNKNOWN
    assert step.status is AgentRunStepStatus.UNKNOWN


@pytest.mark.asyncio
async def test_service_cancellation_uses_fake_native_store() -> None:
    calls: list[dict[str, str]] = []

    class FakeStore:
        async def cancel_native_pair(self, session, *, task_id, step_id, organization_id):
            del session
            calls.append({"task_id": task_id, "step_id": step_id, "organization_id": organization_id})
            return True

    service = object.__new__(AgentRunService)
    service._native_store = FakeStore()  # type: ignore[attr-defined]
    checkpoint = GovernedPlanCheckpoint(
        plan_run_id="run_m10_fake",
        admission_id="admission-fake",
        root_task_id="run_m10_fake",
        plan_id="plan-fake",
        plan_version=1,
        authority_contract_id="contract-fake",
        active_step=GovernedPlanStepRef(
            business_plan_step_id="business-step-fake",
            step_digest="a" * 64,
            work_order_id="submit-fake",
            work_order_digest="b" * 64,
            native_task_id="native-task-fake",
            native_step_id="native-step-fake",
            native_contract_id="native-contract-fake",
            authority_contract_id="contract-fake",
            state="active",
        ),
    )

    await service._cancel_native_pair(object(), checkpoint, TENANT_ID)
    assert calls == [
        {
            "task_id": "native-task-fake",
            "step_id": "native-step-fake",
            "organization_id": TENANT_ID,
        }
    ]


def test_native_snapshots_are_frozen_and_typed() -> None:
    now = datetime.now(timezone.utc)
    task = AgentRunTaskSnapshot(
        task_id="run_m10_typed",
        organization_id=TENANT_ID,
        status="running",
        created_at=now,
        modified_at=now,
    )
    step = AgentRunStepSnapshot(
        step_id="step_typed",
        task_id=task.task_id,
        organization_id=TENANT_ID,
        status="running",
    )
    assert task.status is AgentRunTaskStatus.RUNNING
    assert step.status is AgentRunStepStatus.RUNNING
    with pytest.raises(ValidationError):
        task.status = AgentRunTaskStatus.FAILED  # type: ignore[misc]
