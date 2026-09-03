"""Focused tests for the AgentPact-owned browser operation loop."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone

import pytest

from enterprise.browser_loop.contracts import (
    ActionDecision,
    ActionKind,
    BrowserAction,
    BrowserActionResult,
    BrowserLoopConfig,
    BrowserLoopRunContext,
    BrowserLoopStatus,
    DecisionKind,
    ModelInput,
    PolicyAuthorization,
    PolicyDisposition,
    RawBrowserObservation,
    VerificationDisposition,
    VerificationResult,
)
from enterprise.browser_loop.loop import AgentPactBrowserLoop
from enterprise.browser_loop.ports import StaleObservationError
from enterprise.governance.contracts import ExecutionAuthorization, ExecutionEffect
from enterprise.governance.execution_profiles import ExecutionMechanism, ExecutionProfile
from enterprise.governance.pack_runtime import PackRuntimeBinding

NOW = datetime(2026, 8, 31, 4, 0, tzinfo=timezone.utc)


def _raw(version: int) -> RawBrowserObservation:
    return RawBrowserObservation(
        url="https://enterprise.example.test/work",
        title="Enterprise work queue",
        page_html=f"<html><button id='submit'>Submit</button><span>{version}</span></html>",
        model_dom=f'<button id="ap-0000">Submit</button><span>{version}</span>',
        captured_at=NOW,
    )


def _run(*, domain: bool = False) -> BrowserLoopRunContext:
    base = {
        "run_id": "run-browser-001",
        "task_id": "task-browser-001",
        "step_id": "step-browser-001",
        "goal": "Open the governed work item",
    }
    if domain:
        base.update(
            pack_id="enterprise.work",
            pack_version="1.0.0",
            capability_id="enterprise.work.open",
        )
    return BrowserLoopRunContext(**base)


def _action(operation: str = "open") -> BrowserAction:
    return BrowserAction(kind=ActionKind.CLICK, operation=operation, element_id="ap-0000")


class FakeRuntime:
    def __init__(self, observations: Iterable[RawBrowserObservation], *, stale_once: bool = False) -> None:
        self._observations = iter(observations)
        self.stale_once = stale_once
        self.commands = []

    async def observe(self) -> RawBrowserObservation:
        return next(self._observations)

    async def execute(self, command):
        if self.stale_once:
            self.stale_once = False
            raise StaleObservationError()
        self.commands.append(command)
        return BrowserActionResult(
            completed=True,
            effect_may_have_started=True,
            detail_code="ACTION_COMPLETED",
        )


class FreshRuntime(FakeRuntime):
    def __init__(self, observations: Iterable[RawBrowserObservation]) -> None:
        super().__init__(observations)
        self.fresh_calls = 0

    async def fresh_observation(self) -> RawBrowserObservation:
        self.fresh_calls += 1
        return await self.observe()


class RecordingSink:
    def __init__(self) -> None:
        self.events = []

    async def emit(self, event) -> None:
        self.events.append(event)


class FailingActionSink(RecordingSink):
    async def emit(self, event) -> None:
        if event.stage == "action" and event.code == "ACTION_STARTED":
            raise RuntimeError("durable event sink unavailable")
        await super().emit(event)


class AllowPolicy:
    def __init__(self, *, effect: ExecutionEffect = ExecutionEffect.READ) -> None:
        self.effect = effect
        self.authorized = []

    async def prepare_model_input(self, *, run, observation) -> ModelInput:
        return ModelInput(
            observation_id=observation.observation_id,
            goal=run.goal,
            url=observation.url,
            dom=observation.model_dom,
            screenshots=observation.screenshots,
        )

    async def authorize_action(self, *, run, observation, action, action_fingerprint):
        self.authorized.append((run, observation, action, action_fingerprint))
        return PolicyAuthorization(
            disposition=PolicyDisposition.ALLOW,
            reason_code="POLICY_ALLOWED",
            authorization=ExecutionAuthorization(
                permit_id=f"permit-{len(self.authorized)}",
                action_fingerprint=action_fingerprint,
                observation_hash=observation.observation_id,
                idempotency_key=f"idem-{len(self.authorized)}",
                effect=self.effect,
            ),
            execution_profile=ExecutionProfile(
                mechanism=ExecutionMechanism.LOCATOR,
                evidence_refs=[observation.observation_id],
            ),
        )


class ActionModel:
    def __init__(self, action: BrowserAction | None = None) -> None:
        self.action = action or _action()
        self.calls = []

    async def decide(self, model_input: ModelInput) -> ActionDecision:
        self.calls.append(model_input)
        return ActionDecision(
            kind=DecisionKind.ACTION,
            observation_id=model_input.observation_id,
            action=self.action,
            reason_code="MODEL_ACTION",
        )


class FailingModel:
    async def decide(self, _model_input):
        raise RuntimeError("provider unavailable")


class TerminalModel:
    def __init__(self, kind: DecisionKind) -> None:
        self.kind = kind

    async def decide(self, model_input):
        return ActionDecision(
            kind=self.kind,
            observation_id=model_input.observation_id,
            reason_code=f"MODEL_{self.kind.value.upper()}",
        )


class SequenceVerifier:
    def __init__(self, dispositions: Iterable[VerificationDisposition]) -> None:
        self._dispositions = iter(dispositions)
        self.requests = []

    async def verify(self, request):
        self.requests.append(request)
        disposition = next(self._dispositions)
        return VerificationResult(
            disposition=disposition,
            reason_code=f"VERIFY_{disposition.value.upper()}",
            evidence_refs=(f"evidence-{len(self.requests)}",),
        )


def _loop(
    *,
    runtime,
    model,
    policy=None,
    verifier=None,
    sink=None,
    domain_actions=None,
    config=None,
):
    return AgentPactBrowserLoop(
        runtime=runtime,
        model=model,
        policy=policy or AllowPolicy(),
        verifier=verifier or SequenceVerifier([VerificationDisposition.SUCCEEDED]),
        event_sink=sink or RecordingSink(),
        integrity_secret="browser-loop-test-secret",
        domain_actions=domain_actions,
        config=config,
        clock=lambda: NOW,
    )


@pytest.mark.asyncio
async def test_observe_decide_enforce_act_reobserve_verify_success() -> None:
    runtime = FakeRuntime([_raw(1), _raw(2)])
    model = ActionModel()
    policy = AllowPolicy()
    verifier = SequenceVerifier([VerificationDisposition.SUCCEEDED])
    sink = RecordingSink()

    report = await _loop(
        runtime=runtime,
        model=model,
        policy=policy,
        verifier=verifier,
        sink=sink,
    ).run(_run())

    assert report.status is BrowserLoopStatus.SUCCEEDED
    assert report.actions_executed == 1
    assert report.observations == 2
    assert len(runtime.commands) == 1
    assert verifier.requests[0].before.observation_id != verifier.requests[0].after.observation_id
    assert [event.stage for event in sink.events] == [
        "observation",
        "decision",
        "policy",
        "action",
        "action",
        "observation",
        "verification",
        "terminal",
    ]


@pytest.mark.asyncio
async def test_loop_prefers_explicit_fresh_observation_when_runtime_supports_it() -> None:
    runtime = FreshRuntime([_raw(1)])

    report = await _loop(
        runtime=runtime,
        model=TerminalModel(DecisionKind.SUCCESS),
        verifier=SequenceVerifier([VerificationDisposition.SUCCEEDED]),
    ).run(_run())

    assert report.status is BrowserLoopStatus.SUCCEEDED
    assert runtime.fresh_calls == 1


@pytest.mark.asyncio
async def test_model_failure_is_terminal_and_never_reaches_policy_or_browser() -> None:
    runtime = FakeRuntime([_raw(1)])
    policy = AllowPolicy()

    report = await _loop(runtime=runtime, model=FailingModel(), policy=policy).run(_run())

    assert report.status is BrowserLoopStatus.FAILED
    assert report.reason_code == "DECISION_PROVIDER_FAILED"
    assert not policy.authorized
    assert not runtime.commands


@pytest.mark.asyncio
async def test_stale_observation_reobserves_and_reauthorizes_before_acting() -> None:
    runtime = FakeRuntime([_raw(1), _raw(2), _raw(3)], stale_once=True)
    model = ActionModel()
    policy = AllowPolicy()

    report = await _loop(runtime=runtime, model=model, policy=policy).run(_run())

    assert report.status is BrowserLoopStatus.SUCCEEDED
    assert report.retries_used == 1
    assert report.observations == 3
    assert len(model.calls) == 2
    assert len(policy.authorized) == 2
    assert len(runtime.commands) == 1
    assert model.calls[0].observation_id != model.calls[1].observation_id


@pytest.mark.asyncio
async def test_retry_budget_exhaustion_is_terminal() -> None:
    runtime = FakeRuntime([_raw(1), _raw(2), _raw(3)])
    verifier = SequenceVerifier([VerificationDisposition.RETRY, VerificationDisposition.RETRY])

    report = await _loop(
        runtime=runtime,
        model=ActionModel(),
        verifier=verifier,
        config=BrowserLoopConfig(max_iterations=5, max_retries=1),
    ).run(_run())

    assert report.status is BrowserLoopStatus.FAILED
    assert report.reason_code == "RETRY_BUDGET_EXHAUSTED"
    assert report.retries_used == 1
    assert report.actions_executed == 2


@pytest.mark.asyncio
async def test_write_retry_never_replays_and_requires_recovery() -> None:
    runtime = FakeRuntime([_raw(1), _raw(2), _raw(3)])
    verifier = SequenceVerifier([VerificationDisposition.RETRY])

    report = await _loop(
        runtime=runtime,
        model=ActionModel(),
        policy=AllowPolicy(effect=ExecutionEffect.EXTERNAL_WRITE),
        verifier=verifier,
    ).run(_run())

    assert report.status is BrowserLoopStatus.UNKNOWN
    assert report.reason_code == "WRITE_RETRY_REQUIRES_RECOVERY"
    assert report.actions_executed == 1
    assert len(runtime.commands) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("decision_kind", "verification", "expected_status"),
    [
        (DecisionKind.SUCCESS, VerificationDisposition.SUCCEEDED, BrowserLoopStatus.SUCCEEDED),
        (DecisionKind.FAILURE, VerificationDisposition.SUCCEEDED, BrowserLoopStatus.FAILED),
    ],
)
async def test_terminal_model_decisions(
    decision_kind: DecisionKind,
    verification: VerificationDisposition,
    expected_status: BrowserLoopStatus,
) -> None:
    runtime = FakeRuntime([_raw(1)])
    verifier = SequenceVerifier([verification])

    report = await _loop(
        runtime=runtime,
        model=TerminalModel(decision_kind),
        verifier=verifier,
    ).run(_run())

    assert report.status is expected_status
    assert not runtime.commands
    assert len(verifier.requests) == (1 if decision_kind is DecisionKind.SUCCESS else 0)


class DeterministicDomainActions:
    binding = PackRuntimeBinding(
        pack_id="enterprise.work",
        pack_version="1.0.0",
        capability_ids=("enterprise.work.open",),
        adapter_id="enterprise-work-browser-actions-v1",
    )

    def __init__(self) -> None:
        self.calls = 0

    async def decide(self, *, run, observation):
        self.calls += 1
        assert run.capability_id in self.binding.capability_ids
        return ActionDecision(
            kind=DecisionKind.ACTION,
            observation_id=observation.observation_id,
            action=_action("domain_pack_open"),
            reason_code="DOMAIN_PACK_ACTION",
        )


@pytest.mark.asyncio
async def test_matching_domain_pack_uses_deterministic_action_without_model_fallback() -> None:
    runtime = FakeRuntime([_raw(1), _raw(2)])
    domain_actions = DeterministicDomainActions()

    report = await _loop(
        runtime=runtime,
        model=FailingModel(),
        domain_actions=domain_actions,
    ).run(_run(domain=True))

    assert report.status is BrowserLoopStatus.SUCCEEDED
    assert domain_actions.calls == 1
    assert runtime.commands[0].action.operation == "domain_pack_open"
    decision_event = next(event for event in report.events if event.stage == "decision")
    assert decision_event.details["source"] == "domain_pack"


class ApprovalPolicy(AllowPolicy):
    async def authorize_action(self, **_kwargs):
        return PolicyAuthorization(
            disposition=PolicyDisposition.REQUIRE_APPROVAL,
            reason_code="APPROVAL_REQUIRED",
            approval_ref="approval-browser-001",
        )


@pytest.mark.asyncio
async def test_approval_boundary_pauses_without_browser_action() -> None:
    runtime = FakeRuntime([_raw(1)])

    report = await _loop(runtime=runtime, model=ActionModel(), policy=ApprovalPolicy()).run(_run())

    assert report.status is BrowserLoopStatus.AWAITING_APPROVAL
    assert report.approval_ref == "approval-browser-001"
    assert not runtime.commands


@pytest.mark.asyncio
async def test_event_sink_failure_before_action_fails_closed() -> None:
    runtime = FakeRuntime([_raw(1)])

    report = await _loop(
        runtime=runtime,
        model=ActionModel(),
        sink=FailingActionSink(),
    ).run(_run())

    assert report.status is BrowserLoopStatus.FAILED
    assert report.reason_code == "EVENT_OR_LOOP_FAILURE"
    assert not runtime.commands


@pytest.mark.asyncio
async def test_report_events_never_persist_action_values_or_model_operation_text() -> None:
    secret_value = "credential-value-must-not-be-persisted"
    action = BrowserAction(
        kind=ActionKind.INPUT_TEXT,
        operation="form.fill",
        element_id="ap-0000",
        text=secret_value,
    )
    runtime = FakeRuntime([_raw(1), _raw(2)])

    report = await _loop(runtime=runtime, model=ActionModel(action)).run(_run())

    encoded = report.model_dump_json()
    assert secret_value not in encoded
    assert "form.fill" not in encoded
    assert all(event.task_id == report.task_id and event.step_id == report.step_id for event in report.events)
