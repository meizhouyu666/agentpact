"""Focused native Agent-to-ActionHandler governance contract regressions."""

# ruff: noqa: E402, F401, I001

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.e2e import m4_synthetic_support as _m4_runtime_shims

from enterprise.governance.contracts import ExecutionAuthorization, ExecutionEffect
from enterprise.governance.execution_profiles import ExecutionMechanism, ExecutionProfile
from skyvern.forge.agent import ForgeAgent
from skyvern.forge.native_action import (
    M7_APPLICATION_MARKER,
    NativeActionDisposition,
    NativeActionHandlerOutcome,
    NativeActionResolution,
    NativeGovernanceDenied,
    PostActionControl,
)
from skyvern.forge.sdk.models import Step, StepStatus
from skyvern.forge.sdk.schemas.tasks import Task, TaskStatus
from skyvern.webeye.actions.actions import ClickAction, InputTextAction
from skyvern.webeye.actions.handler import ActionHandler
from skyvern.webeye.actions.responses import ActionSuccess

NOW = datetime(2026, 7, 29, 16, 0, tzinfo=timezone.utc)


def _task_step() -> tuple[Task, Step]:
    task = Task(
        task_id="task-m7",
        organization_id="tenant-m7",
        status=TaskStatus.running,
        url="http://127.0.0.1/synthetic",
        created_at=NOW,
        modified_at=NOW,
    )
    step = Step(
        task_id=task.task_id,
        step_id="step-m7",
        organization_id=task.organization_id,
        status=StepStatus.running,
        order=0,
        retry_index=0,
        is_last=True,
        created_at=NOW,
        modified_at=NOW,
    )
    return task, step


def _click(task: Task, step: Step) -> ClickAction:
    return ClickAction(
        element_id="submit",
        organization_id=task.organization_id,
        task_id=task.task_id,
        step_id=step.step_id,
        step_order=0,
        action_order=0,
        reasoning="M7",
        intention="Submit",
    )


@pytest.mark.asyncio
async def test_providerless_bound_task_fails_before_action_handler(monkeypatch):
    task, step = _task_step()
    task = task.model_copy(update={"application": M7_APPLICATION_MARKER})
    step = step.model_copy(update={"created_by": M7_APPLICATION_MARKER})
    handler = AsyncMock(return_value=[ActionSuccess()])
    monkeypatch.setattr(ActionHandler, "handle_action", handler)

    with pytest.raises(NativeGovernanceDenied, match="M7_BOUND_PROVIDER_REQUIRED"):
        await ForgeAgent()._handle_action_with_native_governance(
            scraped_page=SimpleNamespace(),
            task=task,
            step=step,
            page=SimpleNamespace(),
            action=_click(task, step),
        )
    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_providerless_unbound_task_preserves_legacy_handler(monkeypatch):
    task, step = _task_step()
    expected = [ActionSuccess()]
    handler = AsyncMock(return_value=expected)
    monkeypatch.setattr(ActionHandler, "handle_action", handler)

    resolution, outcome = await ForgeAgent()._handle_action_with_native_governance(
        scraped_page=SimpleNamespace(),
        task=task,
        step=step,
        page=SimpleNamespace(),
        action=_click(task, step),
    )
    assert resolution is None
    assert outcome == expected
    handler.assert_awaited_once()


def test_pending_result_probe_is_nonterminal_and_transitions_only_to_probe_final_states():
    assert TaskStatus.running.can_update_to(TaskStatus.pending_result_probe)
    assert TaskStatus.pending_result_probe.can_update_to(TaskStatus.completed)
    assert TaskStatus.pending_result_probe.can_update_to(TaskStatus.failed)
    assert not TaskStatus.pending_result_probe.is_final()
    assert StepStatus.running.can_update_to(StepStatus.pending_result_probe)
    assert StepStatus.pending_result_probe.can_update_to(StepStatus.completed)
    assert StepStatus.pending_result_probe.can_update_to(StepStatus.failed)
    assert StepStatus.pending_result_probe.cant_have_output()
    assert not StepStatus.pending_result_probe.is_terminal()


@pytest.mark.asyncio
async def test_bound_non_effect_rejects_effect_capable_handler_before_invocation(monkeypatch):
    task, step = _task_step()
    ungoverned = AsyncMock(return_value=[ActionSuccess()])
    monkeypatch.setattr(ActionHandler, "_handle_action_ungoverned", ungoverned)
    resolution = NativeActionResolution(
        disposition=NativeActionDisposition.BOUND_NON_EFFECT,
        operation="read",
        binding_digest="a" * 64,
    )

    with pytest.raises(NativeGovernanceDenied, match="M7_NON_EFFECT_HANDLER_REJECTED"):
        await ActionHandler.handle_action(
            scraped_page=SimpleNamespace(),
            task=task,
            step=step,
            page=SimpleNamespace(),
            action=_click(task, step),
            native_resolution=resolution,
        )
    ungoverned.assert_not_awaited()


@pytest.mark.asyncio
async def test_bound_non_effect_allows_explicit_input_without_execution_authority(monkeypatch):
    task, step = _task_step()
    ungoverned = AsyncMock(return_value=[ActionSuccess()])
    monkeypatch.setattr(ActionHandler, "_handle_action_ungoverned", ungoverned)
    resolution = NativeActionResolution(
        disposition=NativeActionDisposition.BOUND_NON_EFFECT,
        operation="input",
        binding_digest="b" * 64,
    )
    action = InputTextAction(
        element_id="field",
        text="synthetic",
        organization_id=task.organization_id,
        task_id=task.task_id,
        step_id=step.step_id,
        step_order=0,
        action_order=0,
        reasoning="M7",
        intention="Input",
    )

    outcome = await ActionHandler.handle_action(
        scraped_page=SimpleNamespace(),
        task=task,
        step=step,
        page=SimpleNamespace(),
        action=action,
        native_resolution=resolution,
    )
    assert isinstance(outcome, NativeActionHandlerOutcome)
    assert outcome.post_action_control is PostActionControl.CONTINUE
    ungoverned.assert_awaited_once()


@pytest.mark.asyncio
async def test_authorized_native_effect_returns_explicit_suspend_with_attempt(monkeypatch):
    task, step = _task_step()
    authorization = ExecutionAuthorization(
        permit_id="permit-m7",
        action_fingerprint="c" * 64,
        observation_hash="d" * 64,
        idempotency_key="synthetic:pay-m7",
        effect=ExecutionEffect.EXTERNAL_WRITE,
    )
    profile = ExecutionProfile(
        mechanism=ExecutionMechanism.LOCATOR,
        fallback_rank=0,
        evidence_refs=["agentpact://m7/evidence"],
    )
    governed = AsyncMock(return_value=([ActionSuccess()], "attempt-m7"))
    monkeypatch.setattr(ActionHandler, "_handle_governed_action", governed)
    resolution = NativeActionResolution(
        disposition=NativeActionDisposition.BOUND_AUTHORIZED_EFFECT,
        operation="submit",
        binding_digest="e" * 64,
        observation_hash=authorization.observation_hash,
        action_fingerprint=authorization.action_fingerprint,
        execution_authorization=authorization,
        execution_profile=profile,
    )

    outcome = await ActionHandler.handle_action(
        scraped_page=SimpleNamespace(),
        task=task,
        step=step,
        page=SimpleNamespace(),
        action=_click(task, step),
        native_resolution=resolution,
    )
    assert isinstance(outcome, NativeActionHandlerOutcome)
    assert outcome.post_action_control is PostActionControl.SUSPEND_FOR_PROBE
    assert outcome.attempt_id == "attempt-m7"
    governed.assert_awaited_once()


@pytest.mark.asyncio
async def test_partial_legacy_authority_cannot_downgrade_to_ungoverned(monkeypatch):
    task, step = _task_step()
    ungoverned = AsyncMock(return_value=[ActionSuccess()])
    monkeypatch.setattr(ActionHandler, "_handle_action_ungoverned", ungoverned)
    authorization = ExecutionAuthorization(
        permit_id="permit-m7",
        action_fingerprint="f" * 64,
        observation_hash="1" * 64,
        idempotency_key="synthetic:pay-m7",
        effect=ExecutionEffect.EXTERNAL_WRITE,
    )

    with pytest.raises(PermissionError, match="Partial governed execution context"):
        await ActionHandler.handle_action(
            scraped_page=SimpleNamespace(),
            task=task,
            step=step,
            page=SimpleNamespace(),
            action=_click(task, step),
            execution_authorization=authorization,
        )
    ungoverned.assert_not_awaited()
