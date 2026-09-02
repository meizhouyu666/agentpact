"""AgentPact-owned observe/decide/enforce/act/verify state machine."""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timezone
from typing import Any

from enterprise.governance.audit import observation_hash
from enterprise.governance.classification import action_fingerprint
from enterprise.governance.contracts import ExecutionEffect
from enterprise.governance.execution_profiles import ExecutionProfileRejected, require_allowed_profile
from enterprise.governance.pack_runtime import ExecutionCheckpoint

from .contracts import (
    ActionDecision,
    AuthorizedAction,
    BrowserLoopConfig,
    BrowserLoopEvent,
    BrowserLoopReport,
    BrowserLoopRunContext,
    BrowserLoopStatus,
    BrowserObservation,
    DecisionKind,
    DecisionSource,
    PolicyAuthorization,
    PolicyDisposition,
    RawBrowserObservation,
    VerificationDisposition,
    VerificationRequest,
    VerificationResult,
)
from .ports import (
    BrowserActionModel,
    BrowserLoopEventSink,
    BrowserLoopPolicy,
    BrowserLoopVerifier,
    BrowserRuntime,
    BrowserRuntimeError,
    DomainPackActionProvider,
    StaleObservationError,
)


class AgentPactBrowserLoop:
    """A complete browser-operation loop independent of the Skyvern product shell."""

    def __init__(
        self,
        *,
        runtime: BrowserRuntime,
        model: BrowserActionModel,
        policy: BrowserLoopPolicy,
        verifier: BrowserLoopVerifier,
        event_sink: BrowserLoopEventSink,
        integrity_secret: str,
        domain_actions: DomainPackActionProvider | None = None,
        config: BrowserLoopConfig | None = None,
        clock: Any | None = None,
    ) -> None:
        if not integrity_secret:
            raise ValueError("Browser loop requires a non-empty integrity secret")
        self._runtime = runtime
        self._model = model
        self._policy = policy
        self._verifier = verifier
        self._event_sink = event_sink
        self._secret = integrity_secret
        self._domain_actions = domain_actions
        self._config = config or BrowserLoopConfig()
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def run(self, run: BrowserLoopRunContext) -> BrowserLoopReport:
        state = _RunState(run=run)
        try:
            return await self._run(state)
        except Exception:
            status = BrowserLoopStatus.UNKNOWN if state.effect_may_have_started else BrowserLoopStatus.FAILED
            return await self._terminal(state, status, "EVENT_OR_LOOP_FAILURE")

    async def _run(self, state: "_RunState") -> BrowserLoopReport:
        run = state.run
        try:
            observation = await self._observe(state)
        except Exception:
            return await self._terminal(state, BrowserLoopStatus.FAILED, "OBSERVATION_FAILED")

        for iteration in range(1, self._config.max_iterations + 1):
            state.iterations = iteration
            try:
                decision, source = await self._decide(run=run, observation=observation)
            except Exception:
                return await self._terminal(state, BrowserLoopStatus.FAILED, "DECISION_PROVIDER_FAILED")

            if not hmac.compare_digest(decision.observation_id, observation.observation_id):
                return await self._terminal(state, BrowserLoopStatus.FAILED, "DECISION_OBSERVATION_MISMATCH")
            await self._emit(
                state,
                stage="decision",
                code=decision.reason_code,
                observation_id=observation.observation_id,
                details={"kind": decision.kind.value, "source": source.value},
            )

            if decision.kind is DecisionKind.FAILURE:
                return await self._terminal(state, BrowserLoopStatus.FAILED, decision.reason_code)
            if decision.kind is DecisionKind.SUCCESS:
                verification = await self._verify(
                    state=state,
                    before=observation,
                    after=observation,
                    decision=decision,
                    source=source,
                )
                terminal = await self._verification_terminal(state, verification)
                if terminal is not None:
                    return terminal
                retried = verification.disposition is VerificationDisposition.RETRY
                if retried:
                    refreshed = await self._retry_reobserve(state)
                    if refreshed is None:
                        return await self._terminal(state, BrowserLoopStatus.FAILED, "RETRY_BUDGET_EXHAUSTED")
                    observation = refreshed
                else:
                    try:
                        observation = await self._observe(state)
                    except Exception:
                        return await self._terminal(state, BrowserLoopStatus.FAILED, "OBSERVATION_FAILED")
                continue

            assert decision.action is not None
            fingerprint = action_fingerprint(
                task_id=run.task_id,
                step_id=run.step_id,
                action_payload=decision.action.model_dump(mode="json", exclude_none=True),
                observation_hash=observation.observation_id,
                secret=self._secret,
            )
            try:
                policy = await self._policy.authorize_action(
                    run=run,
                    observation=observation,
                    action=decision.action,
                    action_fingerprint=fingerprint,
                )
            except Exception:
                return await self._terminal(state, BrowserLoopStatus.FAILED, "POLICY_EVALUATION_FAILED")
            await self._emit(
                state,
                stage="policy",
                code=policy.reason_code,
                observation_id=observation.observation_id,
                action_fingerprint=fingerprint,
                details={"disposition": policy.disposition.value},
            )

            if policy.disposition is PolicyDisposition.DENY:
                return await self._terminal(state, BrowserLoopStatus.FAILED, policy.reason_code)
            if policy.disposition is PolicyDisposition.REQUIRE_APPROVAL:
                return await self._terminal(
                    state,
                    BrowserLoopStatus.AWAITING_APPROVAL,
                    policy.reason_code,
                    approval_ref=policy.approval_ref,
                )
            if policy.disposition is PolicyDisposition.REOBSERVE:
                refreshed = await self._retry_reobserve(state)
                if refreshed is None:
                    return await self._terminal(state, BrowserLoopStatus.FAILED, "RETRY_BUDGET_EXHAUSTED")
                observation = refreshed
                continue

            try:
                command = self._authorized_command(
                    observation=observation,
                    decision=decision,
                    fingerprint=fingerprint,
                    policy=policy,
                )
            except (ValueError, ExecutionProfileRejected):
                return await self._terminal(state, BrowserLoopStatus.FAILED, "INVALID_EXECUTION_AUTHORIZATION")

            if self._observation_is_stale(observation):
                refreshed = await self._retry_reobserve(state)
                if refreshed is None:
                    return await self._terminal(state, BrowserLoopStatus.FAILED, "RETRY_BUDGET_EXHAUSTED")
                observation = refreshed
                continue

            await self._emit(
                state,
                stage="action",
                code="ACTION_STARTED",
                observation_id=observation.observation_id,
                action_fingerprint=fingerprint,
                details={
                    "kind": decision.action.kind.value,
                    "effect": command.authorization.effect.value,
                    "source": source.value,
                },
            )
            state.last_effect = command.authorization.effect
            try:
                action_result = await self._runtime.execute(command)
            except StaleObservationError:
                refreshed = await self._retry_reobserve(state)
                if refreshed is None:
                    return await self._terminal(state, BrowserLoopStatus.FAILED, "RETRY_BUDGET_EXHAUSTED")
                observation = refreshed
                continue
            except BrowserRuntimeError as exc:
                if exc.effect_may_have_started and _is_write(command.authorization.effect):
                    return await self._terminal(state, BrowserLoopStatus.UNKNOWN, "ACTION_EFFECT_UNKNOWN")
                refreshed = await self._retry_reobserve(state)
                if refreshed is None:
                    return await self._terminal(state, BrowserLoopStatus.FAILED, "RETRY_BUDGET_EXHAUSTED")
                observation = refreshed
                continue
            except Exception:
                if _is_write(command.authorization.effect):
                    return await self._terminal(state, BrowserLoopStatus.UNKNOWN, "ACTION_EFFECT_UNKNOWN")
                return await self._terminal(state, BrowserLoopStatus.FAILED, "ACTION_EXECUTION_FAILED")

            state.actions_executed += 1
            if action_result.effect_may_have_started and _is_write(command.authorization.effect):
                state.effect_may_have_started = True
            await self._emit(
                state,
                stage="action",
                code=action_result.detail_code,
                observation_id=observation.observation_id,
                action_fingerprint=fingerprint,
                details={"completed": action_result.completed},
            )
            if action_result.pending_result_probe:
                state.effect_may_have_started = True
                # Record the deferred business-verification boundary before
                # suspending orchestration on the exact persisted checkpoint.
                await self._verify(
                    state=state,
                    before=observation,
                    after=observation,
                    decision=decision,
                    source=source,
                    action_result=action_result,
                    authorized_effect=command.authorization.effect,
                    action_fingerprint=fingerprint,
                )
                return await self._terminal(
                    state,
                    BrowserLoopStatus.UNKNOWN,
                    "PENDING_RESULT_PROBE",
                    execution_checkpoint=action_result.execution_checkpoint,
                )
            try:
                after = await self._observe(state)
            except Exception:
                status = (
                    BrowserLoopStatus.UNKNOWN if _is_write(command.authorization.effect) else BrowserLoopStatus.FAILED
                )
                return await self._terminal(state, status, "POST_ACTION_OBSERVATION_FAILED")

            verification = await self._verify(
                state=state,
                before=observation,
                after=after,
                decision=decision,
                source=source,
                action_result=action_result,
                authorized_effect=command.authorization.effect,
                action_fingerprint=fingerprint,
            )
            terminal = await self._verification_terminal(state, verification)
            if terminal is not None:
                return terminal
            if verification.disposition is VerificationDisposition.RETRY:
                if _is_write(command.authorization.effect):
                    return await self._terminal(state, BrowserLoopStatus.UNKNOWN, "WRITE_RETRY_REQUIRES_RECOVERY")
                if not self._consume_retry(state):
                    return await self._terminal(state, BrowserLoopStatus.FAILED, "RETRY_BUDGET_EXHAUSTED")
            observation = after

        return await self._terminal(state, BrowserLoopStatus.FAILED, "ITERATION_BUDGET_EXHAUSTED")

    async def _decide(
        self,
        *,
        run: BrowserLoopRunContext,
        observation: BrowserObservation,
    ) -> tuple[ActionDecision, DecisionSource]:
        if self._domain_provider_matches(run):
            assert self._domain_actions is not None
            deterministic = await self._domain_actions.decide(run=run, observation=observation)
            if deterministic is not None:
                return deterministic, DecisionSource.DOMAIN_PACK
        model_input = await self._policy.prepare_model_input(run=run, observation=observation)
        if not hmac.compare_digest(model_input.observation_id, observation.observation_id):
            raise ValueError("Policy returned model input for a different observation")
        return await self._model.decide(model_input), DecisionSource.MODEL

    def _domain_provider_matches(self, run: BrowserLoopRunContext) -> bool:
        if self._domain_actions is None or run.pack_id is None:
            return False
        binding = self._domain_actions.binding
        return (
            binding.pack_id == run.pack_id
            and binding.pack_version == run.pack_version
            and run.capability_id in binding.capability_ids
        )

    async def _observe(self, state: "_RunState") -> BrowserObservation:
        raw = await self._runtime.observe()
        state.observations += 1
        observation = self._bind_observation(raw, state.observations)
        state.last_observation_id = observation.observation_id
        await self._emit(
            state,
            stage="observation",
            code="OBSERVATION_CAPTURED",
            observation_id=observation.observation_id,
            details={"sequence": observation.sequence},
        )
        return observation

    def _bind_observation(self, raw: RawBrowserObservation, sequence: int) -> BrowserObservation:
        snapshot_material = f"{raw.url}\n{raw.page_html}".encode("utf-8")
        return BrowserObservation(
            observation_id=observation_hash(url=raw.url, html=raw.page_html, secret=self._secret),
            snapshot_hash=hashlib.sha256(snapshot_material).hexdigest(),
            sequence=sequence,
            url=raw.url,
            title=raw.title,
            model_dom=raw.model_dom,
            screenshots=raw.screenshots,
            elements=raw.elements,
            iframes=raw.iframes,
            captured_at=_as_utc(raw.captured_at),
        )

    def _authorized_command(
        self,
        *,
        observation: BrowserObservation,
        decision: ActionDecision,
        fingerprint: str,
        policy: PolicyAuthorization,
    ) -> AuthorizedAction:
        assert decision.action is not None
        assert policy.authorization is not None
        assert policy.execution_profile is not None
        authorization = policy.authorization
        if not authorization.permit_id or not authorization.idempotency_key:
            raise ValueError("Execution authorization requires permit and idempotency references")
        if not (
            hmac.compare_digest(authorization.action_fingerprint, fingerprint)
            and hmac.compare_digest(authorization.observation_hash, observation.observation_id)
        ):
            raise ValueError("Execution authorization does not match the action and observation")
        require_allowed_profile(effect=authorization.effect, profile=policy.execution_profile)
        return AuthorizedAction(
            action=decision.action,
            action_fingerprint=fingerprint,
            observation_id=observation.observation_id,
            expected_snapshot_hash=observation.snapshot_hash,
            authorization=authorization,
            execution_profile=policy.execution_profile,
        )

    async def _verify(
        self,
        *,
        state: "_RunState",
        before: BrowserObservation,
        after: BrowserObservation,
        decision: ActionDecision,
        source: DecisionSource,
        action_result: Any | None = None,
        authorized_effect: ExecutionEffect | None = None,
        action_fingerprint: str | None = None,
    ) -> VerificationResult:
        try:
            result = await self._verifier.verify(
                VerificationRequest(
                    run=state.run,
                    before=before,
                    after=after,
                    decision=decision,
                    source=source,
                    action_result=action_result,
                    authorized_effect=authorized_effect,
                )
            )
        except Exception:
            result = VerificationResult(
                disposition=VerificationDisposition.UNKNOWN,
                reason_code="VERIFICATION_FAILED",
            )
        await self._emit(
            state,
            stage="verification",
            code=result.reason_code,
            observation_id=after.observation_id,
            action_fingerprint=action_fingerprint,
            details={"disposition": result.disposition.value, "evidence_count": len(result.evidence_refs)},
        )
        return result

    async def _verification_terminal(
        self,
        state: "_RunState",
        result: VerificationResult,
    ) -> BrowserLoopReport | None:
        if result.disposition is VerificationDisposition.SUCCEEDED:
            return await self._terminal(state, BrowserLoopStatus.SUCCEEDED, result.reason_code)
        if result.disposition is VerificationDisposition.FAILED:
            return await self._terminal(state, BrowserLoopStatus.FAILED, result.reason_code)
        if result.disposition is VerificationDisposition.UNKNOWN:
            return await self._terminal(state, BrowserLoopStatus.UNKNOWN, result.reason_code)
        return None

    async def _retry_reobserve(self, state: "_RunState") -> BrowserObservation | None:
        if not self._consume_retry(state):
            return None
        try:
            return await self._observe(state)
        except Exception:
            return None

    def _consume_retry(self, state: "_RunState") -> bool:
        if state.retries_used >= self._config.max_retries:
            return False
        state.retries_used += 1
        return True

    def _observation_is_stale(self, observation: BrowserObservation) -> bool:
        age = _as_utc(self._clock()) - _as_utc(observation.captured_at)
        return age.total_seconds() > self._config.max_observation_age_seconds or age.total_seconds() < -5

    async def _emit(
        self,
        state: "_RunState",
        *,
        stage: Any,
        code: str,
        observation_id: str | None = None,
        action_fingerprint: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        event = BrowserLoopEvent(
            sequence=len(state.events) + 1,
            run_id=state.run.run_id,
            task_id=state.run.task_id,
            step_id=state.run.step_id,
            stage=stage,
            code=code,
            occurred_at=_as_utc(self._clock()),
            observation_id=observation_id,
            action_fingerprint=action_fingerprint,
            details=details or {},
        )
        await self._event_sink.emit(event)
        state.events.append(event)

    async def _terminal(
        self,
        state: "_RunState",
        status: BrowserLoopStatus,
        reason_code: str,
        *,
        approval_ref: str | None = None,
        execution_checkpoint: ExecutionCheckpoint | None = None,
    ) -> BrowserLoopReport:
        try:
            await self._emit(
                state,
                stage="terminal",
                code=reason_code,
                observation_id=state.last_observation_id,
                details={"status": status.value},
            )
        except Exception:
            status = BrowserLoopStatus.UNKNOWN if state.effect_may_have_started else BrowserLoopStatus.FAILED
            reason_code = "EVENT_SINK_FAILED"
        return BrowserLoopReport(
            run_id=state.run.run_id,
            task_id=state.run.task_id,
            step_id=state.run.step_id,
            status=status,
            reason_code=reason_code,
            iterations=state.iterations,
            retries_used=state.retries_used,
            observations=state.observations,
            actions_executed=state.actions_executed,
            last_observation_id=state.last_observation_id,
            approval_ref=approval_ref,
            execution_checkpoint=execution_checkpoint,
            events=tuple(state.events),
        )


class _RunState:
    def __init__(self, *, run: BrowserLoopRunContext) -> None:
        self.run = run
        self.iterations = 0
        self.retries_used = 0
        self.observations = 0
        self.actions_executed = 0
        self.last_observation_id: str | None = None
        self.last_effect = ExecutionEffect.NONE
        self.effect_may_have_started = False
        self.events: list[BrowserLoopEvent] = []


def _is_write(effect: ExecutionEffect) -> bool:
    return effect in {ExecutionEffect.INTERNAL_WRITE, ExecutionEffect.EXTERNAL_WRITE}


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
