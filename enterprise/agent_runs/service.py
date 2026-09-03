"""Root-locked, fail-closed Agent Run commands and public projections."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from enterprise.agent_runs.journal import (
    GovernedPlanCheckpoint,
    GovernedPlanError,
    PlanJournalEvent,
    PlanJournalTransition,
    PlanRunState,
    PlanStepState,
    is_plan_application_marker,
)
from enterprise.approval.models import ApprovalRequestModel, ApprovalStatus
from enterprise.approval.persistence import decide_approval_request
from enterprise.approval.routing import ApprovalRoute
from enterprise.auth.schemas import UserContext
from enterprise.governance.admission import TaskAdmissionBundle
from enterprise.governance.approval_orchestrator import create_approval_pause
from enterprise.governance.approval_pause_service import begin_reobservation_after_approval
from enterprise.governance.contracts import ActionIntent, ExecutionEffect, PolicyDecision
from enterprise.governance.models import (
    ExecutionAttemptModel,
    ExecutionPermitModel,
    GovernedTaskAdmissionModel,
    PendingActionModel,
)
from enterprise.governance.pack_runtime import (
    ApprovalRequestSpecification,
    PackLifecycleError,
    PackAdvanceStatus,
    PackRunRequest,
    PackRunRestoreRequest,
    PackRuntimeAdapter,
    PackRuntimeBinding,
    PackRuntimeRegistry,
    PreparedRunReference,
    validate_pack_admission_result,
    validate_pack_advance_result,
    validate_pack_probe_result,
    derive_pack_run_id,
)

from .journal import AgentRunJournal
from .persistence import (
    AgentRunNativeStore,
    AgentRunStepStatus,
    AgentRunTaskSnapshot,
    AgentRunTaskStatus,
)


class AgentRunError(RuntimeError):
    def __init__(self, code: str, *, status_code: int = 409) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(code)


class AgentRunState(StrEnum):
    PLANNING = "PLANNING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    RUNNING = "RUNNING"
    UNKNOWN = "UNKNOWN"
    SUCCEEDED = "SUCCEEDED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


class AgentRunAction(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    PROBE = "probe"
    CANCEL = "cancel"


class AgentRunCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    intent: str = Field(min_length=1, max_length=2000)
    business_inputs: dict[str, Any]
    pack_id: str | None = Field(default=None, min_length=1)
    pack_version: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def validate_pack_selection(self) -> "AgentRunCreateRequest":
        if (self.pack_id is None) != (self.pack_version is None):
            raise ValueError("pack_id and pack_version must be supplied together")
        return self


class AgentRunCommandRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_key: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    reason: str | None = Field(default=None, max_length=240)


class _ApprovalIntentRecord(BaseModel):
    """Non-executable approval history; fresh observation must create the real action."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    operation: str
    action_fingerprint: str


class AgentRunPlanStep(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sequence: int = Field(ge=1)
    role: str = Field(min_length=1)
    state: Literal["pending", "active", "completed", "blocked", "terminal"]


class AgentRunProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    pack_id: str
    pack_version: str
    pack_display_name: str
    provider_mode: Literal["recorded", "live"]
    state: AgentRunState
    legal_actions: tuple[AgentRunAction, ...]
    plan: tuple[AgentRunPlanStep, ...]
    completed_steps: int = Field(ge=0)
    total_steps: int = Field(ge=1)
    reason_code: str | None = None


class AgentRunTimelineEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sequence: int
    stage: str
    state: AgentRunState
    reason_code: str | None = None


DecisionTraceStageName = Literal[
    "provider",
    "validation",
    "compilation",
    "admission",
    "approval",
    "execution",
    "recovery",
]
DecisionTraceStatus = Literal["not_recorded", "pending", "active", "completed", "blocked", "failed"]


class AgentRunDecisionTraceStage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    stage: DecisionTraceStageName
    status: DecisionTraceStatus
    reason_code: str | None = None
    timestamp: datetime | None = None
    duration_ms: float | None = Field(default=None, ge=0, le=3_600_000)
    provider_calls: int | None = Field(default=None, ge=0, le=2)
    repair_count: int | None = Field(default=None, ge=0, le=1)
    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)


class AgentRunDecisionTrace(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["agentpact-agent-run-decision-trace/v1"] = "agentpact-agent-run-decision-trace/v1"
    run_id: str
    non_authoritative: Literal[True] = True
    stages: tuple[AgentRunDecisionTraceStage, ...] = Field(min_length=7, max_length=7)


class AgentRunReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["agentpact-agent-run-report/v2"] = "agentpact-agent-run-report/v2"
    projection: AgentRunProjection
    events: tuple[AgentRunTimelineEvent, ...]
    decision_trace: AgentRunDecisionTrace
    report_digest: str


class AgentRunSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    pack_id: str
    pack_version: str
    pack_display_name: str
    provider_mode: Literal["recorded", "live"]
    state: AgentRunState
    completed_steps: int = Field(ge=0)
    total_steps: int = Field(ge=1)
    reason_code: str | None = None
    created_at: datetime
    modified_at: datetime


class AgentRunPage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    items: tuple[AgentRunSummary, ...]
    next_cursor: str | None = None


CreateGateFactory = Callable[[str, str], AbstractAsyncContextManager[Any]]


class AgentRunService:
    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[Any]],
        *,
        runtime_registry: PackRuntimeRegistry,
        default_pack_binding: PackRuntimeBinding | None = None,
        target_url: str,
        provider_timeout_seconds: float = 30.0,
        create_gate_factory: CreateGateFactory | None = None,
        clock: Callable[[], datetime] | None = None,
        native_store: AgentRunNativeStore | None = None,
    ) -> None:
        self._session_factory = session_factory
        if native_store is None:
            from enterprise.integrations.skyvern_agent_run_store import SkyvernAgentRunStore

            native_store = SkyvernAgentRunStore()
        self._native_store = native_store
        self._journal = AgentRunJournal(native_store)
        self._registry = runtime_registry
        self._target_url = target_url
        if default_pack_binding is not None:
            try:
                runtime_registry.validate_binding(default_pack_binding)
                if not runtime_registry.tenant_scoped:
                    adapter = runtime_registry.require_binding(default_pack_binding)
                    if adapter.binding != default_pack_binding:
                        raise ValueError("Default Pack binding does not match the registered adapter")
            except LookupError as exc:
                raise ValueError("Default Pack binding does not match the runtime registry") from exc
        self._default_pack_binding = default_pack_binding
        self._provider_timeout_seconds = provider_timeout_seconds
        self._create_gate_factory = create_gate_factory or (
            lambda tenant_id, request_id: _postgres_advisory_gate(
                self._session_factory,
                tenant_id=tenant_id,
                request_id=request_id,
            )
        )
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def _adapter(
        self,
        binding: PackRuntimeBinding,
        *,
        tenant_id: str | None = None,
        capability_ids: tuple[str, ...] | None = None,
    ) -> PackRuntimeAdapter:
        try:
            if self._registry.tenant_scoped:
                if tenant_id is None:
                    raise AgentRunError("TENANT_REQUIRED", status_code=503)
                if capability_ids is None:
                    adapter = self._registry.require_for_tenant(
                        tenant_id=tenant_id,
                        pack_id=binding.pack_id,
                        pack_version=binding.pack_version,
                        adapter_id=binding.adapter_id,
                        now=self._clock(),
                    )
                else:
                    adapter = self._registry.resolve_for_execution(
                        tenant_id=tenant_id,
                        binding=binding,
                        capability_ids=capability_ids,
                        now=self._clock(),
                    )
            else:
                adapter = self._registry.require_binding(binding)
        except LookupError as exc:
            raise AgentRunError("PACK_RUNTIME_UNAVAILABLE", status_code=503) from exc
        if adapter.binding != binding:
            raise AgentRunError("ADAPTER_CONFORMANCE_FAILED", status_code=503)
        return adapter

    async def create(self, request: AgentRunCreateRequest, *, user: UserContext) -> AgentRunProjection:
        _require_operator(user)
        intent_digest = _digest(["agent-run-intent", request.intent])
        try:
            if request.pack_id is not None and request.pack_version is not None:
                if self._registry.tenant_scoped:
                    selected = self._registry.require_for_tenant(
                        tenant_id=user.org_id,
                        pack_id=request.pack_id,
                        pack_version=request.pack_version,
                        now=self._clock(),
                    ).binding
                else:
                    selected = self._registry.require(
                        pack_id=request.pack_id,
                        pack_version=request.pack_version,
                    ).binding
            elif self._default_pack_binding is not None:
                selected = self._default_pack_binding
            else:
                raise LookupError("No Pack was selected for this Agent Run")
        except LookupError as exc:
            raise AgentRunError("PACK_RUNTIME_UNAVAILABLE", status_code=422) from exc
        adapter = self._adapter(selected, tenant_id=user.org_id)
        existing_run_id = derive_pack_run_id(tenant_id=user.org_id, request_id=request.request_id)
        async with self._create_gate_factory(user.org_id, request.request_id) as session:
            async with session.begin():
                root = await self._native_store.get_root(
                    session,
                    run_id=existing_run_id,
                    organization_id=user.org_id,
                    lock=True,
                )
                admission = (
                    await session.scalars(
                        select(GovernedTaskAdmissionModel)
                        .where(
                            GovernedTaskAdmissionModel.organization_id == user.org_id,
                            GovernedTaskAdmissionModel.request_id == request.request_id,
                        )
                        .with_for_update()
                    )
                ).first()
                if (root is None) != (admission is None):
                    raise AgentRunError("STATE_CONFLICT")
                if root is not None and admission is not None:
                    bundle = TaskAdmissionBundle.model_validate(admission.bundle_payload)
                    stored_token = bundle.request.user_intent_summary.rsplit(" ", 1)[-1]
                    if (
                        bundle.task.task_id != existing_run_id
                        or stored_token != intent_digest
                        or bundle.request.typed_inputs != request.business_inputs
                        or self._binding_for_bundle(bundle) != selected
                    ):
                        raise AgentRunError("IDEMPOTENCY_CONFLICT")
                    return await self._project_locked(
                        session,
                        await self._load_locked(session, run_id=existing_run_id, user=user),
                    )
            try:
                prepared = await asyncio.wait_for(
                    asyncio.to_thread(
                        adapter.prepare_run,
                        PackRunRequest(
                            principal=user,
                            tenant_id=user.org_id,
                            request_id=request.request_id,
                            intent_digest=intent_digest,
                            business_inputs=request.business_inputs,
                            target_url=self._target_url,
                            now=self._clock(),
                        ),
                    ),
                    timeout=self._provider_timeout_seconds,
                )
            except TimeoutError as exc:
                raise AgentRunError("PLANNER_TIMEOUT", status_code=503) from exc
            except PackLifecycleError as exc:
                raise AgentRunError(exc.code, status_code=503) from exc
            except (ValueError, GovernedPlanError) as exc:
                raise AgentRunError("PLANNER_REJECTED", status_code=422) from exc
            try:
                prepared = PreparedRunReference.model_validate(prepared)
            except (TypeError, ValueError) as exc:
                raise AgentRunError("PACK_PREPARE_RESULT_INVALID", status_code=503) from exc
            if (
                prepared.run_id != existing_run_id
                or prepared.tenant_id != user.org_id
                or prepared.request_id != request.request_id
                or prepared.pack_id != selected.pack_id
                or prepared.pack_version != selected.pack_version
                or prepared.adapter_id != selected.adapter_id
            ):
                raise AgentRunError("PACK_PREPARE_RESULT_INVALID", status_code=503)
            operation_key = _operation_key(
                tenant_id=user.org_id,
                run_id=prepared.run_id,
                command="create",
                caller_key=request.request_id,
                predecessor="reservation",
            )
            try:
                admitted = await adapter.admit_run(
                    prepared,
                    approval_handler=self._pause_for_approval,
                    operation_key=operation_key,
                )
                validate_pack_admission_result(admitted, prepared=prepared)
            except AgentRunError:
                raise
            except PackLifecycleError as exc:
                raise AgentRunError(exc.code, status_code=503) from exc
            except (ValueError, GovernedPlanError) as exc:
                raise AgentRunError("PACK_ADMISSION_FAILED", status_code=503) from exc
            async with session.begin():
                return await self._project_locked(
                    session,
                    await self._load_locked(session, run_id=prepared.run_id, user=user),
                )

    async def list_runs(self, *, user: UserContext, cursor: str | None = None, limit: int = 20) -> AgentRunPage:
        _require_operator(user)
        if not 1 <= limit <= 50:
            raise AgentRunError("INVALID_CURSOR", status_code=422)
        boundary = _decode_cursor(cursor) if cursor else None
        async with self._session_factory() as session:
            async with session.begin():
                roots = list(
                    await self._native_store.list_roots(
                        session,
                        organization_id=user.org_id,
                        boundary=boundary,
                        limit=limit + 1,
                    )
                )
                items: list[AgentRunSummary] = []
                for root in roots[:limit]:
                    locked = await self._load_locked(session, run_id=root.task_id, user=user, lock_root=False)
                    projection = await self._project_locked(session, locked)
                    items.append(
                        AgentRunSummary(
                            **projection.model_dump(
                                exclude={"legal_actions", "plan"},
                            ),
                            created_at=_as_utc(root.created_at),
                            modified_at=_as_utc(root.modified_at),
                        )
                    )
                next_cursor = None
                if len(roots) > limit and items:
                    next_cursor = _encode_cursor(items[-1].created_at, items[-1].run_id)
                return AgentRunPage(items=tuple(items), next_cursor=next_cursor)

    async def get(self, run_id: str, *, user: UserContext) -> AgentRunProjection:
        async with self._session_factory() as session:
            async with session.begin():
                locked = await self._load_locked(session, run_id=run_id, user=user)
                return await self._project_locked(session, locked)

    async def events(self, run_id: str, *, user: UserContext) -> tuple[AgentRunTimelineEvent, ...]:
        async with self._session_factory() as session:
            async with session.begin():
                locked = await self._load_locked(session, run_id=run_id, user=user)
                await self._project_locked(session, locked)
                return tuple(_timeline_event(item) for item in locked.events)

    async def decision_trace(self, run_id: str, *, user: UserContext) -> AgentRunDecisionTrace:
        async with self._session_factory() as session:
            async with session.begin():
                locked = await self._load_locked(session, run_id=run_id, user=user)
                projection = await self._project_locked(session, locked)
                return _decision_trace(locked, projection)

    async def report(self, run_id: str, *, user: UserContext) -> AgentRunReport:
        async with self._session_factory() as session:
            async with session.begin():
                locked = await self._load_locked(session, run_id=run_id, user=user)
                projection = await self._project_locked(session, locked)
                events = tuple(_timeline_event(item) for item in locked.events)
                trace = _decision_trace(locked, projection)
                payload = {
                    "schema_version": "agentpact-agent-run-report/v2",
                    "projection": projection.model_dump(mode="json"),
                    "events": [item.model_dump(mode="json") for item in events],
                    "decision_trace": trace.model_dump(mode="json"),
                }
                return AgentRunReport(
                    projection=projection,
                    events=events,
                    decision_trace=trace,
                    report_digest=_digest(payload),
                )

    async def approve(
        self,
        run_id: str,
        command: AgentRunCommandRequest,
        *,
        user: UserContext,
    ) -> AgentRunProjection:
        _require_approver(user)
        prepared: PreparedRunReference | None = None
        adapter: PackRuntimeAdapter | None = None
        should_advance = False
        async with self._session_factory() as session:
            async with session.begin():
                locked = await self._load_locked(session, run_id=run_id, user=user)
                projection = await self._project_locked(session, locked)
                pending, approval = await _pending_approval(session, locked.checkpoint)
                if projection.state is AgentRunState.AWAITING_APPROVAL:
                    if AgentRunAction.APPROVE not in projection.legal_actions or pending is None or approval is None:
                        raise AgentRunError("ILLEGAL_ACTION")
                    await decide_approval_request(
                        db_session=session,
                        approval_id=approval.approval_id,
                        organization_id=user.org_id,
                        approver_user_id=user.user_id,
                        approved=True,
                        decision_note=_safe_reason(command.reason),
                        now=self._clock(),
                    )
                    pending = await _require_pending_by_id(session, pending.pending_action_id)
                elif pending is None or approval is None or approval.status != ApprovalStatus.APPROVED.value:
                    return projection
                if pending.status == "approved":
                    resumed = await begin_reobservation_after_approval(
                        db_session=session,
                        task_id=pending.task_id,
                        step_id=pending.step_id,
                        organization_id=user.org_id,
                        pending_action_id=pending.pending_action_id,
                        expected_row_version=pending.row_version,
                    )
                    operation_key = _operation_key(
                        tenant_id=user.org_id,
                        run_id=run_id,
                        command="approve",
                        caller_key=command.operation_key,
                        predecessor=locked.checkpoint.journal_digest,
                    )
                    active = locked.checkpoint.model_copy(update={"state": PlanRunState.ACTIVE})
                    await self._journal.append(
                        session,
                        organization_id=user.org_id,
                        checkpoint=active,
                        transition=PlanJournalTransition.APPROVAL_RESUMED,
                        authority_digests=locked.authority_digests,
                        operation_key=operation_key,
                        created_at=self._clock(),
                    )
                    if resumed.pending_action.status.value != "invalidated":
                        raise AgentRunError("STATE_CONFLICT")
                    should_advance = True
                adapter, prepared = self._restore_prepared(locked.bundle)
        if should_advance and prepared is not None and adapter is not None:
            try:
                advanced = await adapter.advance_run(
                    prepared,
                    approval_handler=self._pause_for_approval,
                    operation_key=command.operation_key,
                )
                result = validate_pack_advance_result(advanced, run_id=prepared.run_id)
                if result.status is PackAdvanceStatus.FAILED:
                    raise AgentRunError(result.reason_code or "PACK_ADVANCE_FAILED")
            except PackLifecycleError as exc:
                raise AgentRunError(exc.code, status_code=503) from exc
            except (ValueError, GovernedPlanError) as exc:
                raise AgentRunError("PACK_ADVANCE_FAILED", status_code=503) from exc
        return await self.get(run_id, user=user)

    async def reject(
        self,
        run_id: str,
        command: AgentRunCommandRequest,
        *,
        user: UserContext,
    ) -> AgentRunProjection:
        _require_approver(user)
        async with self._session_factory() as session:
            async with session.begin():
                locked = await self._load_locked(session, run_id=run_id, user=user)
                projection = await self._project_locked(session, locked)
                if projection.state is AgentRunState.REJECTED:
                    return projection
                if AgentRunAction.REJECT not in projection.legal_actions:
                    raise AgentRunError("ILLEGAL_ACTION")
                pending, approval = await _pending_approval(session, locked.checkpoint)
                if pending is None or approval is None:
                    raise AgentRunError("STATE_CONFLICT")
                await decide_approval_request(
                    db_session=session,
                    approval_id=approval.approval_id,
                    organization_id=user.org_id,
                    approver_user_id=user.user_id,
                    approved=False,
                    decision_note=_safe_reason(command.reason),
                    now=self._clock(),
                )
                await self._cancel_native_pair(session, locked.checkpoint, user.org_id)
                rejected = locked.checkpoint.model_copy(
                    update={
                        "state": PlanRunState.REJECTED,
                        "active_step": locked.checkpoint.active_step.model_copy(update={"state": PlanStepState.FAILED}),
                    }
                )
                await self._journal.append(
                    session,
                    organization_id=user.org_id,
                    checkpoint=rejected,
                    transition=PlanJournalTransition.APPROVAL_REJECTED,
                    authority_digests=locked.authority_digests,
                    operation_key=_operation_key(
                        tenant_id=user.org_id,
                        run_id=run_id,
                        command="reject",
                        caller_key=command.operation_key,
                        predecessor=locked.checkpoint.journal_digest,
                    ),
                    created_at=self._clock(),
                )
        return await self.get(run_id, user=user)

    async def cancel(
        self,
        run_id: str,
        command: AgentRunCommandRequest,
        *,
        user: UserContext,
    ) -> AgentRunProjection:
        _require_operator(user)
        async with self._session_factory() as session:
            async with session.begin():
                locked = await self._load_locked(session, run_id=run_id, user=user)
                projection = await self._project_locked(session, locked)
                if projection.state is AgentRunState.CANCELLED:
                    return projection
                if AgentRunAction.CANCEL not in projection.legal_actions:
                    raise AgentRunError("ILLEGAL_ACTION")
                if await _effect_may_have_started(session, locked.checkpoint):
                    raise AgentRunError("CANCEL_EFFECT_BOUNDARY_CROSSED")
                await self._cancel_native_pair(session, locked.checkpoint, user.org_id)
                cancelled = locked.checkpoint.model_copy(
                    update={
                        "state": PlanRunState.CANCELLED,
                        "active_step": locked.checkpoint.active_step.model_copy(update={"state": PlanStepState.FAILED}),
                    }
                )
                await self._journal.append(
                    session,
                    organization_id=user.org_id,
                    checkpoint=cancelled,
                    transition=PlanJournalTransition.RUN_CANCELLED,
                    authority_digests=locked.authority_digests,
                    operation_key=_operation_key(
                        tenant_id=user.org_id,
                        run_id=run_id,
                        command="cancel",
                        caller_key=command.operation_key,
                        predecessor=locked.checkpoint.journal_digest,
                    ),
                    created_at=self._clock(),
                )
        return await self.get(run_id, user=user)

    async def probe(
        self,
        run_id: str,
        command: AgentRunCommandRequest,
        *,
        user: UserContext,
    ) -> AgentRunProjection:
        _require_operator(user)
        async with self._session_factory() as session:
            async with session.begin():
                locked = await self._load_locked(session, run_id=run_id, user=user)
                projection = await self._project_locked(session, locked)
                if AgentRunAction.PROBE not in projection.legal_actions:
                    if projection.state is not AgentRunState.UNKNOWN:
                        return projection
                    raise AgentRunError("STATE_CONFLICT")
                adapter, prepared = self._restore_prepared(locked.bundle)
        try:
            probed = await adapter.probe_run(
                prepared,
                operation_key=command.operation_key,
            )
            active = locked.checkpoint.active_step
            if active is None or active.permit_id is None or active.attempt_id is None:
                raise AgentRunError("STATE_CONFLICT")
            validate_pack_probe_result(
                probed,
                run_id=prepared.run_id,
                native_task_id=active.native_task_id,
                native_step_id=active.native_step_id,
                permit_id=active.permit_id,
                attempt_id=active.attempt_id,
            )
        except PackLifecycleError as exc:
            raise AgentRunError(exc.code, status_code=503) from exc
        except (ValueError, GovernedPlanError) as exc:
            raise AgentRunError("PACK_PROBE_FAILED", status_code=503) from exc
        return await self.get(run_id, user=user)

    async def _pause_for_approval(
        self,
        prepared: PreparedRunReference,
        specification: ApprovalRequestSpecification,
        operation_key: str,
    ) -> GovernedPlanCheckpoint:
        route_department, separator, route_role = specification.requested_approval_route.rpartition(":")
        if not separator or not route_department or not route_role:
            raise AgentRunError("STATE_CONFLICT")
        route = ApprovalRoute(
            requires_approval=True,
            approver_department_id=route_department,
            approver_role=route_role,
            description=specification.redacted_description,
        )
        async with self._session_factory() as session:
            async with session.begin():
                root = await self._native_store.get_root(
                    session,
                    run_id=prepared.run_id,
                    organization_id=prepared.tenant_id,
                    lock=True,
                )
                if root is None or not is_plan_application_marker(root.application):
                    raise AgentRunError("STATE_CONFLICT")
                admission = (
                    await session.scalars(
                        select(GovernedTaskAdmissionModel).where(
                            GovernedTaskAdmissionModel.task_id == prepared.run_id,
                            GovernedTaskAdmissionModel.organization_id == prepared.tenant_id,
                        )
                    )
                ).one()
                bundle = TaskAdmissionBundle.model_validate(admission.bundle_payload)
                events = tuple(await self._journal.load_events(session, prepared.run_id))
                current = self._journal.replay(list(events))
                active = current.active_step
                binding = self._binding_for_bundle(bundle)
                if (
                    active is None
                    or active.native_task_id != specification.task_id
                    or active.native_step_id != specification.step_id
                    or active.native_contract_id != specification.contract_id
                    or prepared.admission_id != bundle.admission_id
                    or prepared.contract_id != bundle.contract.contract_id
                    or prepared.tenant_id != bundle.request.tenant_id
                    or prepared.request_id != bundle.request.request_id
                    or prepared.provider_mode != bundle.provider_mode
                    or prepared.pack_id != binding.pack_id
                    or prepared.pack_version != binding.pack_version
                    or prepared.adapter_id != binding.adapter_id
                ):
                    raise AgentRunError("STATE_CONFLICT")
                decision = PolicyDecision.model_validate(specification.policy_decision)
                if (
                    decision.intent_id != specification.intent_id
                    or decision.risk_level != specification.risk_level
                    or specification.organization_id != prepared.tenant_id
                ):
                    raise AgentRunError("STATE_CONFLICT")
                intent = ActionIntent(
                    intent_id=specification.intent_id,
                    task_id=specification.task_id,
                    step_id=specification.step_id,
                    action_fingerprint=specification.action_fingerprint,
                    observation_id=specification.observation_hash,
                    operation=specification.redacted_description,
                    effect=ExecutionEffect(specification.effect),
                )
                await create_approval_pause(
                    db_session=session,
                    task_id=specification.task_id,
                    step_id=specification.step_id,
                    organization_id=specification.organization_id,
                    contract_id=specification.contract_id,
                    source_department_id=specification.source_department_id,
                    action=_ApprovalIntentRecord(
                        operation=specification.redacted_description,
                        action_fingerprint=specification.action_fingerprint,
                    ),
                    intent=intent,
                    observation_hash=specification.observation_hash,
                    decision=decision,
                    route=route,
                    requester_user_id=bundle.request.principal_ref,
                    business_line_id=specification.business_line_id,
                    ttl_seconds=max(1, int((specification.expires_at - self._clock()).total_seconds())),
                )
                paused = current.model_copy(update={"state": PlanRunState.APPROVAL_REQUIRED})
                return await self._journal.append(
                    session,
                    organization_id=prepared.tenant_id,
                    checkpoint=paused,
                    transition=PlanJournalTransition.APPROVAL_REQUIRED,
                    authority_digests=events[-1].authority_digests,
                    operation_key=operation_key,
                    created_at=self._clock(),
                )

    async def _cancel_native_pair(
        self,
        session: Any,
        checkpoint: GovernedPlanCheckpoint,
        organization_id: str,
    ) -> None:
        active = checkpoint.active_step
        if active is None:
            raise AgentRunError("STATE_CONFLICT")
        try:
            cancelled = await self._native_store.cancel_native_pair(
                session,
                task_id=active.native_task_id,
                step_id=active.native_step_id,
                organization_id=organization_id,
            )
        except ValueError as exc:
            raise AgentRunError("STATE_CONFLICT") from exc
        if not cancelled:
            raise AgentRunError("STATE_CONFLICT")

    async def _load_locked(
        self,
        session: Any,
        *,
        run_id: str,
        user: UserContext,
        lock_root: bool = True,
    ) -> "_LockedRun":
        root = await self._native_store.get_root(
            session,
            run_id=run_id,
            organization_id=user.org_id,
            lock=lock_root,
        )
        if root is None:
            raise AgentRunError("RUN_NOT_FOUND", status_code=404)
        if not is_plan_application_marker(root.application):
            raise AgentRunError("STATE_CONFLICT")
        admission = (
            await session.scalars(
                select(GovernedTaskAdmissionModel).where(
                    GovernedTaskAdmissionModel.task_id == run_id,
                    GovernedTaskAdmissionModel.organization_id == user.org_id,
                )
            )
        ).first()
        if admission is None:
            raise AgentRunError("RUN_NOT_FOUND", status_code=404)
        bundle = TaskAdmissionBundle.model_validate(admission.bundle_payload)
        expected_run_id = derive_pack_run_id(tenant_id=user.org_id, request_id=bundle.request.request_id)
        if root.task_id != expected_run_id or bundle.task.task_id != expected_run_id:
            raise AgentRunError("STATE_CONFLICT")
        events = tuple(await self._journal.load_events(session, run_id))
        try:
            checkpoint = self._journal.replay(list(events))
        except (ValueError, GovernedPlanError) as exc:
            raise AgentRunError("STATE_CONFLICT") from exc
        return _LockedRun(
            root=root,
            bundle=bundle,
            checkpoint=checkpoint,
            events=events,
            authority_digests=events[-1].authority_digests,
        )

    async def _project_locked(self, session: Any, locked: "_LockedRun") -> AgentRunProjection:
        checkpoint = locked.checkpoint
        plan = _public_plan(checkpoint)
        completed = len(checkpoint.completed_prefix)
        total = len(plan)
        active = checkpoint.active_step
        latest = locked.events[-1].transition
        if checkpoint.state is PlanRunState.COMPLETED and active is None:
            return self._projection(
                bundle=locked.bundle,
                provider_mode=locked.bundle.provider_mode,
                run_id=checkpoint.root_task_id,
                state=AgentRunState.SUCCEEDED,
                legal_actions=(),
                plan=plan,
                completed_steps=completed,
                total_steps=total,
            )
        if active is None:
            return self._state_conflict(
                checkpoint.root_task_id, plan, completed, bundle=locked.bundle
            )
        try:
            task, step = await self._native_store.get_native_pair(
                session,
                task_id=active.native_task_id,
                step_id=active.native_step_id,
                organization_id=locked.root.organization_id,
            )
        except ValueError as exc:
            raise AgentRunError("STATE_CONFLICT") from exc
        pending, approval = await _pending_approval(session, checkpoint)
        attempts = list(
            (
                await session.scalars(
                    select(ExecutionAttemptModel).where(ExecutionAttemptModel.task_id == active.native_task_id)
                )
            ).all()
        )
        permits = list(
            (
                await session.scalars(
                    select(ExecutionPermitModel).where(ExecutionPermitModel.task_id == active.native_task_id)
                )
            ).all()
        )
        if checkpoint.state is PlanRunState.PROBE_BLOCKED:
            exact_attempt = [
                item for item in attempts if item.attempt_id == active.attempt_id and item.status == "unknown"
            ]
            exact_permit = [
                item for item in permits if item.permit_id == active.permit_id and item.status == "consumed"
            ]
            if (
                task is not None
                and step is not None
                and task.status == AgentRunTaskStatus.PENDING_RESULT_PROBE
                and step.status == AgentRunStepStatus.PENDING_RESULT_PROBE
                and len(exact_attempt) == 1
                and len(exact_permit) == 1
                and exact_attempt[0].action_fingerprint == exact_permit[0].action_fingerprint
                and exact_attempt[0].observation_hash == exact_permit[0].observation_hash
            ):
                return self._projection(
                    bundle=locked.bundle,
                    provider_mode=locked.bundle.provider_mode,
                    run_id=checkpoint.root_task_id,
                    state=AgentRunState.UNKNOWN,
                    legal_actions=(AgentRunAction.PROBE,),
                    plan=plan,
                    completed_steps=completed,
                    total_steps=total,
                    reason_code="RESULT_UNCERTAIN",
                )
            return self._state_conflict(
                checkpoint.root_task_id, plan, completed, bundle=locked.bundle
            )
        if latest is PlanJournalTransition.APPROVAL_REJECTED:
            if (
                checkpoint.state is PlanRunState.REJECTED
                and task is not None
                and step is not None
                and task.status == AgentRunTaskStatus.CANCELED
                and step.status == AgentRunStepStatus.CANCELED
                and pending is not None
                and approval is not None
                and pending.status == "rejected"
                and approval.status == ApprovalStatus.REJECTED.value
                and not attempts
                and not any(item.status == "consumed" for item in permits)
            ):
                return self._projection(
                    bundle=locked.bundle,
                    provider_mode=locked.bundle.provider_mode,
                    run_id=checkpoint.root_task_id,
                    state=AgentRunState.REJECTED,
                    legal_actions=(),
                    plan=plan,
                    completed_steps=completed,
                    total_steps=total,
                    reason_code="APPROVAL_REJECTED",
                )
            return self._state_conflict(
                checkpoint.root_task_id, plan, completed, bundle=locked.bundle
            )
        if latest is PlanJournalTransition.RUN_CANCELLED:
            if (
                checkpoint.state is PlanRunState.CANCELLED
                and task is not None
                and step is not None
                and task.status == AgentRunTaskStatus.CANCELED
                and step.status == AgentRunStepStatus.CANCELED
                and not attempts
                and not any(item.status == "consumed" for item in permits)
            ):
                return self._projection(
                    bundle=locked.bundle,
                    provider_mode=locked.bundle.provider_mode,
                    run_id=checkpoint.root_task_id,
                    state=AgentRunState.CANCELLED,
                    legal_actions=(),
                    plan=plan,
                    completed_steps=completed,
                    total_steps=total,
                    reason_code="RUN_CANCELLED",
                )
            return self._state_conflict(
                checkpoint.root_task_id, plan, completed, bundle=locked.bundle
            )
        if checkpoint.state is PlanRunState.APPROVAL_REQUIRED:
            if (
                task is not None
                and step is not None
                and task.status == AgentRunTaskStatus.PENDING_APPROVAL
                and step.status == AgentRunStepStatus.PENDING_APPROVAL
                and pending is not None
                and approval is not None
                and pending.status == "pending"
                and approval.status == ApprovalStatus.PENDING.value
                and not attempts
                and not permits
            ):
                return self._projection(
                    bundle=locked.bundle,
                    provider_mode=locked.bundle.provider_mode,
                    run_id=checkpoint.root_task_id,
                    state=AgentRunState.AWAITING_APPROVAL,
                    legal_actions=(AgentRunAction.APPROVE, AgentRunAction.REJECT),
                    plan=plan,
                    completed_steps=completed,
                    total_steps=total,
                )
            if pending is not None and approval is not None and pending.status == "approved":
                return self._projection(
                    bundle=locked.bundle,
                    provider_mode=locked.bundle.provider_mode,
                    run_id=checkpoint.root_task_id,
                    state=AgentRunState.RUNNING,
                    legal_actions=(),
                    plan=plan,
                    completed_steps=completed,
                    total_steps=total,
                    reason_code="APPROVAL_RECORDED",
                )
            return self._state_conflict(
                checkpoint.root_task_id, plan, completed, bundle=locked.bundle
            )
        if checkpoint.state is PlanRunState.ACTIVE:
            if task is None and step is None and latest is PlanJournalTransition.ADMITTED:
                return self._projection(
                    bundle=locked.bundle,
                    provider_mode=locked.bundle.provider_mode,
                    run_id=checkpoint.root_task_id,
                    state=AgentRunState.PLANNING,
                    legal_actions=(AgentRunAction.CANCEL,),
                    plan=plan,
                    completed_steps=completed,
                    total_steps=total,
                )
            if (
                task is not None
                and step is not None
                and task.status in {
                    AgentRunTaskStatus.CREATED,
                    AgentRunTaskStatus.RUNNING,
                    AgentRunTaskStatus.RESUMING,
                }
                and step.status in {
                    AgentRunStepStatus.CREATED,
                    AgentRunStepStatus.RUNNING,
                    AgentRunStepStatus.RESUMING,
                }
                and not attempts
                and not any(item.status == "consumed" for item in permits)
            ):
                legal = () if task.status == AgentRunTaskStatus.RESUMING else (AgentRunAction.CANCEL,)
                return self._projection(
                    bundle=locked.bundle,
                    provider_mode=locked.bundle.provider_mode,
                    run_id=checkpoint.root_task_id,
                    state=AgentRunState.RUNNING,
                    legal_actions=legal,
                    plan=plan,
                    completed_steps=completed,
                    total_steps=total,
                )
        return self._state_conflict(
            checkpoint.root_task_id, plan, completed, bundle=locked.bundle
        )

    def _projection(self, *, bundle: TaskAdmissionBundle, **values: Any) -> AgentRunProjection:
        binding = self._binding_for_bundle(bundle)
        if self._registry.tenant_scoped:
            metadata = self._registry.public_metadata_for_tenant(
                tenant_id=bundle.request.tenant_id,
                pack_id=binding.pack_id,
                pack_version=binding.pack_version,
            )
        else:
            metadata = self._registry.public_metadata(pack_id=binding.pack_id, pack_version=binding.pack_version)
        return AgentRunProjection(
            pack_id=metadata.pack_id,
            pack_version=metadata.pack_version,
            pack_display_name=metadata.display_name,
            **values,
        )

    def _state_conflict(
        self,
        run_id: str,
        plan: tuple[AgentRunPlanStep, ...],
        completed: int,
        *,
        bundle: TaskAdmissionBundle,
    ) -> AgentRunProjection:
        return self._projection(
            bundle=bundle,
            provider_mode=bundle.provider_mode,
            run_id=run_id,
            state=AgentRunState.FAILED,
            legal_actions=(),
            plan=plan,
            completed_steps=completed,
            total_steps=len(plan),
            reason_code="STATE_CONFLICT",
        )

    def _binding_for_bundle(self, bundle: TaskAdmissionBundle) -> PackRuntimeBinding:
        binding = bundle.runtime_binding
        if binding is None:
            raise AgentRunError("RUNTIME_BINDING_MISSING", status_code=409)
        return binding

    def _restore_prepared(self, bundle: TaskAdmissionBundle) -> tuple[PackRuntimeAdapter, PreparedRunReference]:
        binding = self._binding_for_bundle(bundle)
        adapter = self._adapter(
            binding,
            tenant_id=bundle.request.tenant_id,
            capability_ids=(bundle.request.capability_ref,),
        )
        try:
            prepared = adapter.restore_run(
                PackRunRestoreRequest(
                    run_id=bundle.task.task_id,
                    tenant_id=bundle.request.tenant_id,
                    request_id=bundle.request.request_id,
                    binding=binding,
                    provider_mode=bundle.provider_mode,
                    target_url=self._target_url,
                    admission_payload=bundle.model_dump(mode="json"),
                )
            )
        except PackLifecycleError as exc:
            raise AgentRunError(exc.code, status_code=503) from exc
        except (ValueError, GovernedPlanError) as exc:
            raise AgentRunError("STATE_CONFLICT") from exc
        try:
            prepared = PreparedRunReference.model_validate(prepared)
        except (TypeError, ValueError) as exc:
            raise AgentRunError("PACK_RESTORE_RESULT_INVALID", status_code=503) from exc
        if (
            prepared.run_id != bundle.task.task_id
            or prepared.admission_id != bundle.admission_id
            or prepared.contract_id != bundle.contract.contract_id
            or prepared.pack_id != binding.pack_id
            or prepared.pack_version != binding.pack_version
            or prepared.adapter_id != binding.adapter_id
            or prepared.tenant_id != bundle.request.tenant_id
            or prepared.request_id != bundle.request.request_id
            or prepared.provider_mode != bundle.provider_mode
        ):
            raise AgentRunError("STATE_CONFLICT")
        return adapter, prepared


class _LockedRun(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    root: AgentRunTaskSnapshot
    bundle: TaskAdmissionBundle
    checkpoint: GovernedPlanCheckpoint
    events: tuple[PlanJournalEvent, ...]
    authority_digests: dict[str, str]


@asynccontextmanager
async def _postgres_advisory_gate(
    session_factory: Callable[[], AbstractAsyncContextManager[Any]],
    *,
    tenant_id: str,
    request_id: str,
) -> AsyncIterator[Any]:
    key = int.from_bytes(
        hashlib.sha256(f"{tenant_id}\0{request_id}".encode("utf-8")).digest()[:8],
        byteorder="big",
        signed=True,
    )
    async with session_factory() as probe_session:
        engine = probe_session.bind
    if engine is None or not hasattr(engine, "connect"):
        raise AgentRunError("CREATE_LOCK_UNAVAILABLE", status_code=503)
    async with engine.connect() as connection:
        session = AsyncSession(bind=connection, expire_on_commit=False)
        acquired = False
        try:
            await connection.execute(text("SELECT pg_advisory_lock(:key)"), {"key": key})
            await connection.commit()
            acquired = True
            yield session
        finally:
            if acquired:
                unlocked = False
                try:
                    await session.rollback()
                    result = await connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
                    unlocked = bool(result.scalar_one())
                    await connection.commit()
                except BaseException:
                    await connection.invalidate()
                    raise
                finally:
                    await session.close()
                if not unlocked:
                    await connection.invalidate()
                    raise AgentRunError("CREATE_LOCK_RELEASE_FAILED", status_code=503)


async def _pending_approval(
    session: Any,
    checkpoint: GovernedPlanCheckpoint,
) -> tuple[PendingActionModel | None, ApprovalRequestModel | None]:
    if checkpoint.active_step is None:
        return None, None
    pending_rows = list(
        (
            await session.scalars(
                select(PendingActionModel).where(
                    PendingActionModel.task_id == checkpoint.active_step.native_task_id,
                    PendingActionModel.step_id == checkpoint.active_step.native_step_id,
                )
            )
        ).all()
    )
    if not pending_rows:
        return None, None
    if len(pending_rows) != 1 or not pending_rows[0].approval_id:
        raise AgentRunError("STATE_CONFLICT")
    pending = pending_rows[0]
    approval = (
        await session.scalars(
            select(ApprovalRequestModel).where(ApprovalRequestModel.approval_id == pending.approval_id)
        )
    ).first()
    if approval is None or approval.task_id != pending.task_id or approval.organization_id != pending.organization_id:
        raise AgentRunError("STATE_CONFLICT")
    return pending, approval


async def _require_pending_by_id(session: Any, pending_action_id: str) -> PendingActionModel:
    pending = (
        await session.scalars(
            select(PendingActionModel)
            .where(PendingActionModel.pending_action_id == pending_action_id)
            .with_for_update()
        )
    ).first()
    if pending is None:
        raise AgentRunError("STATE_CONFLICT")
    return pending


async def _effect_may_have_started(session: Any, checkpoint: GovernedPlanCheckpoint) -> bool:
    if checkpoint.active_step is None:
        return False
    attempts = list(
        (
            await session.scalars(
                select(ExecutionAttemptModel).where(
                    ExecutionAttemptModel.task_id == checkpoint.active_step.native_task_id
                )
            )
        ).all()
    )
    permits = list(
        (
            await session.scalars(
                select(ExecutionPermitModel).where(
                    ExecutionPermitModel.task_id == checkpoint.active_step.native_task_id
                )
            )
        ).all()
    )
    return bool(attempts or any(item.status == "consumed" for item in permits))


def _public_plan(checkpoint: GovernedPlanCheckpoint) -> tuple[AgentRunPlanStep, ...]:
    refs = (
        *checkpoint.completed_prefix,
        *((checkpoint.active_step,) if checkpoint.active_step else ()),
        *checkpoint.remaining_suffix,
    )
    result: list[AgentRunPlanStep] = []
    for index, item in enumerate(refs, start=1):
        state = {
            PlanStepState.PENDING: "pending",
            PlanStepState.ACTIVE: "active",
            PlanStepState.COMPLETED: "completed",
            PlanStepState.PROBE_BLOCKED: "blocked",
            PlanStepState.FAILED: "terminal",
            PlanStepState.SUPERSEDED: "terminal",
        }[item.state]
        result.append(AgentRunPlanStep(sequence=index, role="step", state=state))
    return tuple(result)


def _timeline_event(event: PlanJournalEvent) -> AgentRunTimelineEvent:
    state = {
        PlanJournalTransition.ADMITTED: AgentRunState.PLANNING,
        PlanJournalTransition.APPROVAL_REQUIRED: AgentRunState.AWAITING_APPROVAL,
        PlanJournalTransition.APPROVAL_RESUMED: AgentRunState.RUNNING,
        PlanJournalTransition.APPROVAL_REJECTED: AgentRunState.REJECTED,
        PlanJournalTransition.RUN_CANCELLED: AgentRunState.CANCELLED,
        PlanJournalTransition.PROBE_BLOCKED: AgentRunState.UNKNOWN,
        PlanJournalTransition.PLAN_COMPLETED: AgentRunState.SUCCEEDED,
    }.get(event.transition, AgentRunState.RUNNING)
    reason = {
        PlanJournalTransition.PROBE_BLOCKED: "RESULT_UNCERTAIN",
        PlanJournalTransition.APPROVAL_REJECTED: "APPROVAL_REJECTED",
        PlanJournalTransition.RUN_CANCELLED: "RUN_CANCELLED",
        PlanJournalTransition.REAUTHORIZATION_REQUIRED: "REAUTHORIZATION_REQUIRED",
    }.get(event.transition)
    return AgentRunTimelineEvent(sequence=event.sequence, stage=event.transition.value, state=state, reason_code=reason)


def _decision_trace(locked: _LockedRun, projection: AgentRunProjection) -> AgentRunDecisionTrace:
    observation = locked.bundle.planner_observation
    first_event = locked.events[0]
    latest_event = locked.events[-1]
    transitions = {item.transition for item in locked.events}
    if observation is None:
        provider = AgentRunDecisionTraceStage(
            stage="provider",
            status="not_recorded",
            reason_code="PLANNER_OBSERVATION_NOT_RECORDED",
        )
        validation = AgentRunDecisionTraceStage(
            stage="validation",
            status="not_recorded",
            reason_code="PLANNER_OBSERVATION_NOT_RECORDED",
        )
    else:
        failed = observation.disposition == "rejected"
        reason = observation.codes[0] if observation.codes else observation.disposition.upper()
        usage = observation.usage
        provider = AgentRunDecisionTraceStage(
            stage="provider",
            status="failed" if failed else "completed",
            reason_code=reason,
            duration_ms=observation.provider_duration_ms,
            provider_calls=observation.provider_calls,
            repair_count=observation.repair_count,
            prompt_tokens=usage.prompt_tokens if usage else None,
            completion_tokens=usage.completion_tokens if usage else None,
            total_tokens=usage.total_tokens if usage else None,
        )
        validation = AgentRunDecisionTraceStage(
            stage="validation",
            status="failed" if failed else "completed",
            reason_code=reason,
            duration_ms=observation.duration_ms,
            provider_calls=observation.provider_calls,
            repair_count=observation.repair_count,
        )

    approval_status: DecisionTraceStatus = "pending"
    approval_reason = "APPROVAL_NOT_REACHED"
    approval_timestamp: datetime | None = None
    if PlanJournalTransition.APPROVAL_REJECTED in transitions:
        approval_status, approval_reason = "failed", "APPROVAL_REJECTED"
    elif PlanJournalTransition.APPROVAL_RESUMED in transitions:
        approval_status, approval_reason = "completed", "APPROVAL_RESUMED"
    elif PlanJournalTransition.APPROVAL_REQUIRED in transitions:
        approval_status, approval_reason = "active", "APPROVAL_REQUIRED"
    approval_event = next(
        (
            item
            for item in reversed(locked.events)
            if item.transition
            in {
                PlanJournalTransition.APPROVAL_REQUIRED,
                PlanJournalTransition.APPROVAL_RESUMED,
                PlanJournalTransition.APPROVAL_REJECTED,
            }
        ),
        None,
    )
    if approval_event is not None:
        approval_timestamp = _as_utc(approval_event.created_at)

    execution_status: DecisionTraceStatus = {
        AgentRunState.PLANNING: "pending",
        AgentRunState.AWAITING_APPROVAL: "pending",
        AgentRunState.RUNNING: "active",
        AgentRunState.UNKNOWN: "blocked",
        AgentRunState.SUCCEEDED: "completed",
        AgentRunState.REJECTED: "failed",
        AgentRunState.CANCELLED: "failed",
        AgentRunState.FAILED: "failed",
    }[projection.state]
    execution_reason = projection.reason_code or projection.state.value

    if PlanJournalTransition.PROBE_BLOCKED not in transitions:
        recovery_status: DecisionTraceStatus = "pending"
        recovery_reason = "RECOVERY_NOT_REQUIRED"
        recovery_timestamp = None
    elif PlanJournalTransition.PROBE_RESOLVED in transitions:
        recovery_status, recovery_reason = "completed", "PROBE_RESOLVED"
        recovery_timestamp = next(
            _as_utc(item.created_at)
            for item in reversed(locked.events)
            if item.transition is PlanJournalTransition.PROBE_RESOLVED
        )
    else:
        recovery_status, recovery_reason = "blocked", "PROBE_REQUIRED"
        recovery_timestamp = next(
            _as_utc(item.created_at)
            for item in reversed(locked.events)
            if item.transition is PlanJournalTransition.PROBE_BLOCKED
        )

    return AgentRunDecisionTrace(
        run_id=projection.run_id,
        stages=(
            provider,
            validation,
            AgentRunDecisionTraceStage(
                stage="compilation",
                status="completed",
                reason_code="TRUSTED_COMPILATION_ACCEPTED",
                timestamp=_as_utc(first_event.created_at),
            ),
            AgentRunDecisionTraceStage(
                stage="admission",
                status="completed",
                reason_code="ADMITTED",
                timestamp=_as_utc(first_event.created_at),
            ),
            AgentRunDecisionTraceStage(
                stage="approval",
                status=approval_status,
                reason_code=approval_reason,
                timestamp=approval_timestamp,
            ),
            AgentRunDecisionTraceStage(
                stage="execution",
                status=execution_status,
                reason_code=execution_reason,
                timestamp=_as_utc(latest_event.created_at),
            ),
            AgentRunDecisionTraceStage(
                stage="recovery",
                status=recovery_status,
                reason_code=recovery_reason,
                timestamp=recovery_timestamp,
            ),
        ),
    )


def _encode_cursor(created_at: datetime, run_id: str) -> str:
    payload = json.dumps(
        {"created_at": _as_utc(created_at).isoformat(timespec="microseconds"), "run_id": run_id},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        payload = json.loads(raw.decode("utf-8"))
        if set(payload) != {"created_at", "run_id"} or not isinstance(payload["run_id"], str):
            raise ValueError
        created_at = datetime.fromisoformat(payload["created_at"])
        if created_at.tzinfo is None or not payload["run_id"].startswith("run_"):
            raise ValueError
        return created_at.astimezone(timezone.utc).replace(tzinfo=None), payload["run_id"]
    except (ValueError, TypeError, KeyError, UnicodeDecodeError, json.JSONDecodeError, binascii.Error) as exc:
        raise AgentRunError("INVALID_CURSOR", status_code=422) from exc


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _require_operator(user: UserContext) -> None:
    if not user.is_any_operator:
        raise AgentRunError("OPERATOR_REQUIRED", status_code=403)


def _require_approver(user: UserContext) -> None:
    if not user.is_any_approver:
        raise AgentRunError("APPROVER_REQUIRED", status_code=403)


def _safe_reason(reason: str | None) -> str:
    return "Authenticated decision" if not reason else f"Authenticated decision ({_digest(reason)[:12]})"


def _operation_key(*, tenant_id: str, run_id: str, command: str, caller_key: str, predecessor: str) -> str:
    return f"agent-run:{command}:{_digest([tenant_id, run_id, command, caller_key, predecessor])}"


def _digest(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
