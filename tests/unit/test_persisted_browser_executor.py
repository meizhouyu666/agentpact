from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from enterprise.applications.agent_run_recovery import recover_abandoned_agent_run_executions
from enterprise.browser_loop.contracts import (
    ActionKind,
    AuthorizedAction,
    BrowserAction,
    BrowserActionResult,
)
from enterprise.browser_loop.persisted_executor import (
    PersistedBrowserExecutor,
    PersistedExecutionStage,
    recover_abandoned_persisted_executions,
)
from enterprise.browser_loop.ports import StaleObservationError
from enterprise.governance.contracts import (
    DecisionOutcome,
    ExecutionAttemptStatus,
    ExecutionAuthorization,
    ExecutionEffect,
    PolicyDecision,
)
from enterprise.governance.execution_profiles import ExecutionMechanism, ExecutionProfile
from enterprise.governance.models import ExecutionAttemptModel, ExecutionPermitModel
from enterprise.governance.permit_service import issue_permit

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
PROFILE = ExecutionProfile(mechanism=ExecutionMechanism.LOCATOR, evidence_refs=["dom:submit"])


class _Result:
    def __init__(self, values: list[Any]) -> None:
        self._values = values

    def first(self):
        return self._values[0] if self._values else None

    def all(self):
        return list(self._values)


class _Transaction:
    def __init__(self, factory: "_SessionFactory") -> None:
        self._factory = factory

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, _exc, _tb):
        if exc_type is None:
            self._factory.commits += 1
        return False


class _Session:
    def __init__(self, factory: "_SessionFactory") -> None:
        self._factory = factory

    def begin(self) -> _Transaction:
        return _Transaction(self._factory)

    def add(self, model: Any) -> None:
        if isinstance(model, ExecutionPermitModel):
            self._factory.permits.append(model)
        elif isinstance(model, ExecutionAttemptModel):
            self._factory.attempts.append(model)

    async def flush(self) -> None:
        for index, permit in enumerate(self._factory.permits, start=1):
            permit.permit_id = permit.permit_id or f"permit_{index}"
            permit.status = permit.status or "issued"
        for index, attempt in enumerate(self._factory.attempts, start=1):
            attempt.attempt_id = attempt.attempt_id or f"attempt_{index}"
            attempt.status = attempt.status or ExecutionAttemptStatus.AUTHORIZED.value
            attempt.created_at = attempt.created_at or NOW

    async def scalars(self, statement):
        entity = statement.column_descriptions[0]["entity"]
        if entity is ExecutionPermitModel:
            return _Result(self._factory.permits)
        if entity is ExecutionAttemptModel:
            return _Result(self._factory.attempts)
        raise AssertionError(f"Unexpected query entity {entity}")


class _SessionContext(AbstractAsyncContextManager[_Session]):
    def __init__(self, factory: "_SessionFactory") -> None:
        self._factory = factory

    async def __aenter__(self) -> _Session:
        return _Session(self._factory)

    async def __aexit__(self, _exc_type, _exc, _tb) -> None:
        return None


class _SessionFactory:
    def __init__(self) -> None:
        self.permits: list[ExecutionPermitModel] = []
        self.attempts: list[ExecutionAttemptModel] = []
        self.commits = 0

    def __call__(self) -> _SessionContext:
        return _SessionContext(self)


class _Runtime:
    def __init__(self, store: _SessionFactory, *, preflight_error: Exception | None = None) -> None:
        self.store = store
        self.preflight_error = preflight_error
        self.preflights = 0
        self.browser_calls = 0
        self.browser_error: Exception | None = None
        self.status_at_call: str | None = None
        self.commits_at_call = 0

    async def observe(self):
        raise AssertionError("observe is not used by executor tests")

    async def preflight(self, _command: AuthorizedAction) -> None:
        self.preflights += 1
        if self.preflight_error is not None:
            raise self.preflight_error

    async def execute(self, command: AuthorizedAction) -> BrowserActionResult:
        await self.preflight(command)
        return await self.execute_preflighted(command)

    async def execute_preflighted(self, _command: AuthorizedAction) -> BrowserActionResult:
        self.browser_calls += 1
        self.status_at_call = self.store.attempts[0].status
        self.commits_at_call = self.store.commits
        if self.browser_error is not None:
            raise self.browser_error
        return BrowserActionResult(completed=True, effect_may_have_started=True, detail_code="ACTION_COMPLETED")


def _decision() -> PolicyDecision:
    return PolicyDecision(
        decision_id="decision_1",
        intent_id="intent_1",
        outcome=DecisionOutcome.ALLOW,
        risk_level="high",
        policy_version="policy-v1",
    )


async def _command(store: _SessionFactory, *, operation: str = "orders.submit") -> AuthorizedAction:
    async with store() as session:
        async with session.begin():
            permit = await issue_permit(
                db_session=session,
                task_id="task_1",
                step_id="step_1",
                contract_id="contract_1",
                action_fingerprint="action_fp",
                observation_hash="observation_fp",
                decision=_decision(),
                effect=ExecutionEffect.EXTERNAL_WRITE,
                execution_profile=PROFILE,
            )
    return AuthorizedAction(
        action=BrowserAction(kind=ActionKind.CLICK, operation=operation, element_id="ap-0001"),
        action_fingerprint="action_fp",
        observation_id="observation_fp",
        expected_snapshot_hash="snapshot_fp",
        authorization=ExecutionAuthorization(
            permit_id=permit.permit_id,
            action_fingerprint="action_fp",
            observation_hash="observation_fp",
            idempotency_key="orders:request_1",
            effect=ExecutionEffect.EXTERNAL_WRITE,
        ),
        execution_profile=PROFILE,
    )


@pytest.mark.asyncio
async def test_preflight_failure_consumes_no_permit_and_creates_no_attempt() -> None:
    store = _SessionFactory()
    command = await _command(store)
    runtime = _Runtime(store, preflight_error=StaleObservationError())
    executor = PersistedBrowserExecutor(store, runtime, result_probe_ref="probe://orders/v1", clock=lambda: NOW)

    with pytest.raises(StaleObservationError):
        await executor.execute(command)

    assert store.permits[0].status == "issued"
    assert store.attempts == []
    assert runtime.browser_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "result_probe_ref"),
    [
        ("synthetic.payment.submit", "synthetic.payment.submit.result-probe.v1"),
        ("stripe.payment.submit", "stripe.payment.submit.result-probe.v1"),
    ],
)
async def test_two_pack_fixtures_share_the_persisted_execution_contract(
    operation: str,
    result_probe_ref: str,
) -> None:
    store = _SessionFactory()
    command = await _command(store, operation=operation)
    runtime = _Runtime(store)
    executor = PersistedBrowserExecutor(store, runtime, result_probe_ref=result_probe_ref, clock=lambda: NOW)

    result = await executor.execute(command)

    assert runtime.browser_calls == 1
    assert runtime.status_at_call == ExecutionAttemptStatus.EXECUTING.value
    assert runtime.commits_at_call >= 3  # Permit issue plus AUTHORIZED and EXECUTING commits.
    assert store.permits[0].status == "consumed"
    assert store.attempts[0].status == ExecutionAttemptStatus.UNKNOWN.value
    assert result.pending_result_probe is True
    assert result.execution_checkpoint is not None
    assert result.execution_checkpoint.attempt_id == store.attempts[0].attempt_id
    assert result.execution_checkpoint.idempotency_key_digest != command.authorization.idempotency_key
    assert result.execution_checkpoint.result_probe_ref == result_probe_ref


@pytest.mark.asyncio
async def test_browser_failure_never_replays_external_write() -> None:
    store = _SessionFactory()
    command = await _command(store)
    runtime = _Runtime(store)
    runtime.browser_error = TimeoutError("response lost")
    executor = PersistedBrowserExecutor(store, runtime, result_probe_ref="probe://orders/v1", clock=lambda: NOW)

    result = await executor.execute(command)
    assert result.pending_result_probe is True
    assert runtime.browser_calls == 1
    assert store.attempts[0].status == ExecutionAttemptStatus.UNKNOWN.value

    with pytest.raises(ValueError):
        await executor.execute(command)
    assert runtime.browser_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("crash_stage", "expected_status", "browser_calls"),
    [
        (PersistedExecutionStage.AFTER_AUTHORIZED, ExecutionAttemptStatus.AUTHORIZED, 0),
        (PersistedExecutionStage.AFTER_EXECUTING, ExecutionAttemptStatus.EXECUTING, 0),
        (PersistedExecutionStage.AFTER_BROWSER_RETURN, ExecutionAttemptStatus.EXECUTING, 1),
        (PersistedExecutionStage.AFTER_UNKNOWN, ExecutionAttemptStatus.UNKNOWN, 1),
    ],
)
async def test_fault_windows_preserve_the_authoritative_checkpoint(
    crash_stage: PersistedExecutionStage,
    expected_status: ExecutionAttemptStatus,
    browser_calls: int,
) -> None:
    store = _SessionFactory()
    command = await _command(store)
    runtime = _Runtime(store)

    async def crash(stage: PersistedExecutionStage, _checkpoint) -> None:
        if stage is crash_stage:
            raise RuntimeError(f"crash:{stage.value}")

    executor = PersistedBrowserExecutor(
        store,
        runtime,
        result_probe_ref="probe://orders/v1",
        fault_hook=crash,
        clock=lambda: NOW,
    )
    with pytest.raises(RuntimeError, match="crash"):
        await executor.execute(command)
    assert store.attempts[0].status == expected_status.value
    assert runtime.browser_calls == browser_calls


@pytest.mark.asyncio
async def test_recovery_abandons_authorized_and_moves_executing_to_unknown_without_replay() -> None:
    authorized_store = _SessionFactory()
    authorized_command = await _command(authorized_store)

    async def crash_authorized(stage: PersistedExecutionStage, _checkpoint) -> None:
        if stage is PersistedExecutionStage.AFTER_AUTHORIZED:
            raise RuntimeError("crash")

    authorized_runtime = _Runtime(authorized_store)
    with pytest.raises(RuntimeError):
        await PersistedBrowserExecutor(
            authorized_store,
            authorized_runtime,
            result_probe_ref="probe://orders/v1",
            fault_hook=crash_authorized,
            clock=lambda: NOW,
        ).execute(authorized_command)
    report = await recover_abandoned_persisted_executions(
        authorized_store,
        minimum_age=timedelta(0),
        now=NOW,
    )
    assert report.abandoned_attempt_ids == (authorized_store.attempts[0].attempt_id,)
    assert authorized_store.attempts[0].status == ExecutionAttemptStatus.FAILED.value
    assert authorized_runtime.browser_calls == 0

    executing_store = _SessionFactory()
    executing_command = await _command(executing_store)

    async def crash_executing(stage: PersistedExecutionStage, _checkpoint) -> None:
        if stage is PersistedExecutionStage.AFTER_EXECUTING:
            raise RuntimeError("crash")

    executing_runtime = _Runtime(executing_store)
    with pytest.raises(RuntimeError):
        await PersistedBrowserExecutor(
            executing_store,
            executing_runtime,
            result_probe_ref="probe://orders/v1",
            fault_hook=crash_executing,
            clock=lambda: NOW,
        ).execute(executing_command)
    report = await recover_abandoned_persisted_executions(
        executing_store,
        minimum_age=timedelta(0),
        now=NOW,
    )
    assert len(report.ambiguous) == 1
    assert report.ambiguous[0].attempt_id == executing_store.attempts[0].attempt_id
    assert executing_store.attempts[0].status == ExecutionAttemptStatus.UNKNOWN.value
    assert executing_runtime.browser_calls == 0


@pytest.mark.asyncio
async def test_application_composition_hook_owns_abandoned_execution_recovery() -> None:
    store = _SessionFactory()
    command = await _command(store)

    async def crash(stage: PersistedExecutionStage, _checkpoint) -> None:
        if stage is PersistedExecutionStage.AFTER_EXECUTING:
            raise RuntimeError("owner-invoked recovery boundary")

    with pytest.raises(RuntimeError):
        await PersistedBrowserExecutor(
            store,
            _Runtime(store),
            result_probe_ref="probe://orders/v1",
            fault_hook=crash,
            clock=lambda: NOW,
        ).execute(command)

    report = await recover_abandoned_agent_run_executions(
        store,
        minimum_age=timedelta(0),
        now=NOW,
    )
    assert len(report.ambiguous) == 1
    assert report.ambiguous[0].attempt_id == store.attempts[0].attempt_id
    assert store.attempts[0].status == ExecutionAttemptStatus.UNKNOWN.value


@pytest.mark.asyncio
async def test_external_write_without_probe_contract_fails_closed() -> None:
    store = _SessionFactory()
    command = await _command(store)
    runtime = _Runtime(store)
    executor = PersistedBrowserExecutor(store, runtime, result_probe_ref=None, clock=lambda: NOW)

    with pytest.raises(RuntimeError, match="authoritative result probe"):
        await executor.execute(command)
    assert store.permits[0].status == "issued"
    assert runtime.browser_calls == 0
