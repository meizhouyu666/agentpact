"""Crash-safe persisted execution of one already-authorized browser write."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from enterprise.governance.contracts import ExecutionAttempt, ExecutionAttemptStatus, ExecutionEffect
from enterprise.governance.execution_attempt_service import (
    abandon_authorized_execution_attempt,
    authorize_execution_attempt,
    mark_execution_attempt_executing,
    mark_execution_attempt_unknown,
)
from enterprise.governance.models import ExecutionAttemptModel
from enterprise.governance.pack_runtime import ExecutionCheckpoint

from .contracts import AuthorizedAction, BrowserActionResult
from .ports import PreflightBrowserRuntime


class PersistedExecutionStage(StrEnum):
    AFTER_AUTHORIZED = "after_authorized"
    AFTER_EXECUTING = "after_executing"
    AFTER_BROWSER_RETURN = "after_browser_return"
    AFTER_UNKNOWN = "after_unknown"


FaultHook = Callable[[PersistedExecutionStage, ExecutionCheckpoint], Awaitable[None]]


class PersistedExecutionError(RuntimeError):
    pass


class PersistedExecutionRecoveryReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ambiguous: tuple[ExecutionCheckpoint, ...] = ()
    abandoned_attempt_ids: tuple[str, ...] = ()


class PersistedBrowserExecutor:
    """Compose Permit/Attempt services around exactly one browser invocation."""

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[Any]],
        runtime: PreflightBrowserRuntime,
        *,
        result_probe_ref: str | None,
        fault_hook: FaultHook | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._runtime = runtime
        self._result_probe_ref = result_probe_ref
        self._fault_hook = fault_hook
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def observe(self):
        return await self._runtime.observe()

    async def execute(self, command: AuthorizedAction) -> BrowserActionResult:
        if command.authorization.effect is not ExecutionEffect.EXTERNAL_WRITE:
            return await self._runtime.execute(command)
        if not self._result_probe_ref:
            raise PersistedExecutionError("External writes require an authoritative result probe")

        # All validation capable of producing a safe re-observation happens here.
        await self._runtime.preflight(command)

        async with self._session_factory() as session:
            async with session.begin():
                authorized = await authorize_execution_attempt(
                    db_session=session,
                    permit_id=command.authorization.permit_id,
                    action_fingerprint=command.action_fingerprint,
                    observation_hash=command.observation_id,
                    idempotency_key=command.authorization.idempotency_key,
                    effect=command.authorization.effect,
                    execution_profile=command.execution_profile,
                    result_probe_ref=self._result_probe_ref,
                    now=self._clock(),
                )
        authorized_checkpoint = _checkpoint(authorized)
        await self._inject(PersistedExecutionStage.AFTER_AUTHORIZED, authorized_checkpoint)

        async with self._session_factory() as session:
            async with session.begin():
                executing = await mark_execution_attempt_executing(
                    db_session=session,
                    attempt_id=authorized.attempt_id,
                    now=self._clock(),
                )
        executing_checkpoint = _checkpoint(executing)
        await self._inject(PersistedExecutionStage.AFTER_EXECUTING, executing_checkpoint)

        browser_result: BrowserActionResult | None = None
        browser_error: Exception | None = None
        try:
            browser_result = await self._runtime.execute_preflighted(command)
        except Exception as exc:  # The durable boundary forbids action replay for every browser failure.
            browser_error = exc
        if browser_result is not None:
            await self._inject(PersistedExecutionStage.AFTER_BROWSER_RETURN, executing_checkpoint)

        async with self._session_factory() as session:
            async with session.begin():
                unknown = await mark_execution_attempt_unknown(
                    db_session=session,
                    attempt_id=authorized.attempt_id,
                    error_message=(
                        "PENDING_AUTHORITATIVE_RESULT_PROBE"
                        if browser_error is None
                        else f"BROWSER_EXECUTION_AMBIGUOUS:{type(browser_error).__name__}"
                    ),
                    now=self._clock(),
                )
        unknown_checkpoint = _checkpoint(unknown)
        await self._inject(PersistedExecutionStage.AFTER_UNKNOWN, unknown_checkpoint)
        return BrowserActionResult(
            completed=bool(browser_result and browser_result.completed),
            effect_may_have_started=True,
            detail_code="PENDING_RESULT_PROBE",
            pending_result_probe=True,
            execution_checkpoint=unknown_checkpoint,
        )

    async def _inject(self, stage: PersistedExecutionStage, checkpoint: ExecutionCheckpoint) -> None:
        if self._fault_hook is not None:
            await self._fault_hook(stage, checkpoint)


async def recover_abandoned_persisted_executions(
    session_factory: Callable[[], AbstractAsyncContextManager[Any]],
    *,
    minimum_age: timedelta,
    now: datetime | None = None,
) -> PersistedExecutionRecoveryReport:
    """Single-worker, row-locked recovery; no recovered action is ever replayed."""

    if minimum_age.total_seconds() < 0:
        raise ValueError("Recovery minimum age cannot be negative")
    current = now or datetime.now(timezone.utc)
    threshold = current - minimum_age
    ambiguous: list[ExecutionCheckpoint] = []
    abandoned: list[str] = []
    async with session_factory() as session:
        async with session.begin():
            models = list(
                (
                    await session.scalars(
                        select(ExecutionAttemptModel)
                        .where(
                            ExecutionAttemptModel.status.in_(
                                (ExecutionAttemptStatus.AUTHORIZED.value, ExecutionAttemptStatus.EXECUTING.value)
                            ),
                            ExecutionAttemptModel.created_at <= threshold,
                        )
                        .order_by(ExecutionAttemptModel.created_at, ExecutionAttemptModel.attempt_id)
                        .with_for_update(skip_locked=True)
                    )
                ).all()
            )
            for model in models:
                if model.status == ExecutionAttemptStatus.AUTHORIZED.value:
                    failed = await abandon_authorized_execution_attempt(
                        db_session=session,
                        attempt_id=model.attempt_id,
                        now=current,
                    )
                    abandoned.append(failed.attempt_id)
                else:
                    unknown = await mark_execution_attempt_unknown(
                        db_session=session,
                        attempt_id=model.attempt_id,
                        error_message="ABANDONED_EXECUTING_REQUIRES_RESULT_PROBE",
                        now=current,
                    )
                    ambiguous.append(_checkpoint(unknown))
    return PersistedExecutionRecoveryReport(
        ambiguous=tuple(ambiguous),
        abandoned_attempt_ids=tuple(abandoned),
    )


def _checkpoint(attempt: ExecutionAttempt) -> ExecutionCheckpoint:
    if (
        not attempt.permit_id
        or not attempt.idempotency_key_digest
        or attempt.execution_effect is None
        or not attempt.result_probe_ref
    ):
        raise PersistedExecutionError("Attempt is missing its exact persisted execution checkpoint")
    return ExecutionCheckpoint(
        permit_id=attempt.permit_id,
        attempt_id=attempt.attempt_id,
        task_id=attempt.task_id,
        step_id=attempt.step_id,
        action_fingerprint=attempt.action_fingerprint,
        observation_hash=attempt.observation_hash,
        idempotency_key_digest=attempt.idempotency_key_digest,
        execution_effect=attempt.execution_effect.value,
        result_probe_ref=attempt.result_probe_ref,
        attempt_status=attempt.status.value,
    )
