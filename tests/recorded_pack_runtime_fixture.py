from __future__ import annotations

import hashlib
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timedelta, timezone
from typing import Any

from enterprise.browser_loop.contracts import (
    ActionDecision,
    ActionKind,
    BrowserAction,
    BrowserActionResult,
    BrowserLoopConfig,
    BrowserLoopEvent,
    BrowserLoopRunContext,
    BrowserObservation,
    DecisionKind,
    ModelInput,
    PolicyAuthorization,
    PolicyDisposition,
    RawBrowserObservation,
    VerificationDisposition,
    VerificationRequest,
    VerificationResult,
)
from enterprise.browser_loop.loop import AgentPactBrowserLoop
from enterprise.browser_loop.persisted_executor import PersistedBrowserExecutor
from enterprise.governance.contracts import (
    DecisionOutcome,
    ExecutionAttemptStatus,
    ExecutionAuthorization,
    ExecutionEffect,
    PolicyDecision,
)
from enterprise.governance.execution_profiles import ExecutionMechanism, ExecutionProfile
from enterprise.governance.models import ExecutionAttemptModel, ExecutionPermitModel
from enterprise.governance.pack_runtime import (
    ApprovalHandler,
    ApprovalRequestSpecification,
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
from enterprise.governance.permit_service import issue_permit

NOW = datetime.now(timezone.utc)
PROFILE = ExecutionProfile(mechanism=ExecutionMechanism.LOCATOR, evidence_refs=["dom:recorded-order"])
RECORDED_ORDER_CONTRACT = PackRuntimeContract(
    pack_id="recorded.orders",
    pack_version="1.0.0",
    display_name="Recorded Orders Conformance Pack",
    capability_ids=("recorded.orders.submit",),
    adapter_id="recorded.orders.runtime.v1",
    manifest_digest="d" * 64,
)


class _Result:
    def __init__(self, values: list[Any]) -> None:
        self._values = values

    def first(self):
        return self._values[0] if self._values else None

    def all(self):
        return list(self._values)


class _Transaction:
    def __init__(self, store: "RecordedExecutionStore") -> None:
        self._store = store

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, _exc, _tb):
        if exc_type is None:
            self._store.commits += 1
        return False


class _Session:
    def __init__(self, store: "RecordedExecutionStore") -> None:
        self._store = store

    def begin(self) -> _Transaction:
        return _Transaction(self._store)

    def add(self, model: Any) -> None:
        if isinstance(model, ExecutionPermitModel):
            self._store.permits.append(model)
        elif isinstance(model, ExecutionAttemptModel):
            self._store.attempts.append(model)

    async def flush(self) -> None:
        for index, permit in enumerate(self._store.permits, start=1):
            permit.permit_id = permit.permit_id or f"recorded_permit_{index}"
            permit.status = permit.status or "issued"
        for index, attempt in enumerate(self._store.attempts, start=1):
            attempt.attempt_id = attempt.attempt_id or f"recorded_attempt_{index}"
            attempt.status = attempt.status or ExecutionAttemptStatus.AUTHORIZED.value
            attempt.created_at = attempt.created_at or NOW

    async def scalars(self, statement):
        entity = statement.column_descriptions[0]["entity"]
        if entity is ExecutionPermitModel:
            return _Result(self._store.permits)
        if entity is ExecutionAttemptModel:
            return _Result(self._store.attempts)
        raise AssertionError(f"Unexpected query entity {entity}")


class _SessionContext(AbstractAsyncContextManager[_Session]):
    def __init__(self, store: "RecordedExecutionStore") -> None:
        self._store = store

    async def __aenter__(self) -> _Session:
        return _Session(self._store)

    async def __aexit__(self, _exc_type, _exc, _tb) -> None:
        return None


class RecordedExecutionStore:
    def __init__(self) -> None:
        self.permits: list[ExecutionPermitModel] = []
        self.attempts: list[ExecutionAttemptModel] = []
        self.commits = 0

    def __call__(self) -> _SessionContext:
        return _SessionContext(self)


class RecordedOrderBrowserRuntime:
    def __init__(self) -> None:
        self.browser_calls = 0
        self.preflights = 0

    async def observe(self) -> RawBrowserObservation:
        return RawBrowserObservation(
            url="https://orders.example.test/recorded",
            title="Recorded order",
            page_html="<button id='submit'>Submit order</button>",
            model_dom='[{"element_id":"ap-0000","role":"button","name":"Submit order"}]',
            captured_at=NOW,
        )

    async def preflight(self, _command) -> None:
        self.preflights += 1

    async def execute(self, command) -> BrowserActionResult:
        await self.preflight(command)
        return await self.execute_preflighted(command)

    async def execute_preflighted(self, _command) -> BrowserActionResult:
        self.browser_calls += 1
        return BrowserActionResult(
            completed=True,
            effect_may_have_started=True,
            detail_code="ACTION_COMPLETED",
        )


class _RecordedActions:
    binding = PackRuntimeBinding(
        pack_id=RECORDED_ORDER_CONTRACT.pack_id,
        pack_version=RECORDED_ORDER_CONTRACT.pack_version,
        capability_ids=RECORDED_ORDER_CONTRACT.capability_ids,
        adapter_id="recorded.orders.browser-actions.v1",
    )

    async def decide(self, *, run: BrowserLoopRunContext, observation: BrowserObservation) -> ActionDecision:
        return ActionDecision(
            kind=DecisionKind.ACTION,
            observation_id=observation.observation_id,
            action=BrowserAction(
                kind=ActionKind.CLICK,
                operation="recorded.orders.submit",
                element_id="ap-0000",
            ),
            reason_code="RECORDED_ORDER_ACTION",
        )


class _RecordedPolicy:
    def __init__(self, store: RecordedExecutionStore) -> None:
        self._store = store

    async def prepare_model_input(self, *, run, observation) -> ModelInput:
        return ModelInput(
            observation_id=observation.observation_id,
            goal=run.goal,
            url=observation.url,
            dom=observation.model_dom,
            allowed_action_kinds=(),
        )

    async def authorize_action(self, *, run, observation, action, action_fingerprint) -> PolicyAuthorization:
        decision = PolicyDecision(
            decision_id="recorded-order-approved",
            intent_id="recorded-order-intent",
            outcome=DecisionOutcome.ALLOW,
            risk_level="high",
            policy_version="recorded-orders-v1",
        )
        async with self._store() as session:
            async with session.begin():
                permit = await issue_permit(
                    db_session=session,
                    task_id=run.task_id,
                    step_id=run.step_id,
                    contract_id=run.contract_id or "recorded-order-contract",
                    action_fingerprint=action_fingerprint,
                    observation_hash=observation.observation_id,
                    decision=decision,
                    effect=ExecutionEffect.EXTERNAL_WRITE,
                    execution_profile=PROFILE,
                )
        return PolicyAuthorization(
            disposition=PolicyDisposition.ALLOW,
            reason_code="RECORDED_ORDER_APPROVED",
            authorization=ExecutionAuthorization(
                permit_id=permit.permit_id,
                action_fingerprint=action_fingerprint,
                observation_hash=observation.observation_id,
                idempotency_key=f"recorded-order:{run.run_id}",
                effect=ExecutionEffect.EXTERNAL_WRITE,
            ),
            execution_profile=PROFILE,
        )


class _RecordedVerifier:
    async def verify(self, request: VerificationRequest) -> VerificationResult:
        return VerificationResult(
            disposition=VerificationDisposition.UNKNOWN,
            reason_code="RECORDED_ORDER_PROBE_REQUIRED",
            evidence_refs=("probe://recorded.orders/v1",),
        )


class _UnavailableModel:
    async def decide(self, _model_input: ModelInput) -> ActionDecision:
        raise AssertionError("Recorded Pack must use its deterministic action provider")


class _EventSink:
    def __init__(self) -> None:
        self.events: list[BrowserLoopEvent] = []

    async def emit(self, event: BrowserLoopEvent) -> None:
        self.events.append(event)


class _CapturingPersistedExecutor(PersistedBrowserExecutor):
    error: Exception | None = None

    async def execute(self, command) -> BrowserActionResult:
        try:
            return await super().execute(command)
        except Exception as exc:
            self.error = exc
            raise


class RecordedOrdersRuntimeAdapter:
    """Non-product adapter proving the complete generic lifecycle contract."""

    def __init__(self) -> None:
        self.store = RecordedExecutionStore()
        self.runtime = RecordedOrderBrowserRuntime()
        self.events = _EventSink()
        self.approved = False
        self.checkpoint = None

    @property
    def binding(self) -> PackRuntimeBinding:
        return PackRuntimeBinding(
            pack_id=RECORDED_ORDER_CONTRACT.pack_id,
            pack_version=RECORDED_ORDER_CONTRACT.pack_version,
            capability_ids=RECORDED_ORDER_CONTRACT.capability_ids,
            adapter_id=RECORDED_ORDER_CONTRACT.adapter_id,
        )

    def model_safe_projection(self, authority):
        return authority

    def prepare_run(self, request: PackRunRequest) -> PreparedRunReference:
        run_id = derive_pack_run_id(tenant_id=request.tenant_id, request_id=request.request_id)
        return PreparedRunReference(
            run_id=run_id,
            tenant_id=request.tenant_id,
            request_id=request.request_id,
            pack_id=self.binding.pack_id,
            pack_version=self.binding.pack_version,
            adapter_id=self.binding.adapter_id,
            admission_id=f"admission_{request.request_id}",
            contract_id=f"contract_{request.request_id}",
            provider_mode="recorded",
            opaque_payload={"business_inputs": request.business_inputs},
        )

    def restore_run(self, request: PackRunRestoreRequest) -> PreparedRunReference:
        if request.binding != self.binding:
            raise ValueError("Recorded adapter binding mismatch")
        return PreparedRunReference(
            run_id=request.run_id,
            tenant_id=request.tenant_id,
            request_id=request.request_id,
            pack_id=request.binding.pack_id,
            pack_version=request.binding.pack_version,
            adapter_id=request.binding.adapter_id,
            admission_id=f"admission_{request.request_id}",
            contract_id=f"contract_{request.request_id}",
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
        digest = hashlib.sha256(prepared.run_id.encode("utf-8")).hexdigest()
        approval = ApprovalRequestSpecification(
            task_id=f"task_{prepared.request_id}",
            step_id=f"step_{prepared.request_id}",
            contract_id=prepared.contract_id or "recorded-order-contract",
            organization_id=prepared.tenant_id,
            intent_id=f"intent_{digest}",
            action_fingerprint=digest,
            observation_hash=digest,
            requested_approval_route="orders:approver",
            source_department_id="orders",
            risk_level="high",
            effect=ExecutionEffect.EXTERNAL_WRITE.value,
            expires_at=NOW + timedelta(hours=1),
            reason_code="BUSINESS_APPROVAL_REQUIRED",
            redacted_description="Submit one recorded order",
            policy_decision={"outcome": "require_approval"},
        )
        await approval_handler(prepared, approval, operation_key)
        return PackAdmissionResult(
            prepared=prepared,
            admission_id=prepared.admission_id or "recorded-order-admission",
            initial=PackAdvanceResult(
                status=PackAdvanceStatus.AWAITING_APPROVAL,
                run_id=prepared.run_id,
                step_id=approval.step_id,
                reason_code=approval.reason_code,
                approval=approval,
            ),
        )

    async def advance_run(
        self,
        prepared: PreparedRunReference,
        *,
        approval_handler: ApprovalHandler,
        operation_key: str,
    ) -> PackAdvanceResult:
        if not self.approved:
            raise ValueError("Recorded order approval is not decided")
        task_id = f"task_{prepared.request_id}"
        step_id = f"step_{prepared.request_id}"
        persisted_runtime = _CapturingPersistedExecutor(
            self.store,
            self.runtime,
            result_probe_ref="probe://recorded.orders/v1",
            clock=lambda: NOW,
        )
        loop = AgentPactBrowserLoop(
            runtime=persisted_runtime,
            model=_UnavailableModel(),
            policy=_RecordedPolicy(self.store),
            verifier=_RecordedVerifier(),
            event_sink=self.events,
            integrity_secret="recorded-orders-integrity",
            domain_actions=_RecordedActions(),
            config=BrowserLoopConfig(max_iterations=1, max_retries=0),
            clock=lambda: NOW,
        )
        report = await loop.run(
            BrowserLoopRunContext(
                run_id=prepared.run_id,
                task_id=task_id,
                step_id=step_id,
                goal="Submit one recorded order",
                pack_id=self.binding.pack_id,
                pack_version=self.binding.pack_version,
                capability_id=self.binding.capability_ids[0],
                contract_id=prepared.contract_id,
            )
        )
        if report.execution_checkpoint is None:
            raise ValueError(
                "Recorded order did not persist its execution checkpoint: "
                f"{report.status}:{report.reason_code}; permits={len(self.store.permits)}; "
                f"attempts={len(self.store.attempts)}; browser_calls={self.runtime.browser_calls}; "
                f"events={[event.code for event in self.events.events]}; error={persisted_runtime.error!r}"
            )
        self.checkpoint = report.execution_checkpoint
        return PackAdvanceResult(
            status=PackAdvanceStatus.PENDING_RESULT_PROBE,
            run_id=prepared.run_id,
            step_id=step_id,
            reason_code="RESULT_UNCERTAIN",
            execution_checkpoint=self.checkpoint,
        )

    async def probe_run(self, prepared: PreparedRunReference, *, operation_key: str) -> PackProbeResult:
        if self.checkpoint is None:
            raise ValueError("Recorded order has no exact UNKNOWN checkpoint")
        return PackProbeResult(
            status=PackProbeStatus.CONFIRMED,
            checkpoint=self.checkpoint,
            reason_code="BUSINESS_RESULT_CONFIRMED",
            evidence_refs=("recorded-orders://confirmation/1",),
        )
