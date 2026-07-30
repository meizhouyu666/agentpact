"""Root-locked, fail-closed Agent Run commands and public projections."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from enterprise.approval.models import ApprovalRequestModel, ApprovalStatus
from enterprise.approval.persistence import decide_approval_request
from enterprise.approval.routing import ApprovalRoute
from enterprise.auth.schemas import DepartmentRole, UserContext
from enterprise.domains.synthetic_payment.constants import BUSINESS_LINE_ID, PAYMENTS_DEPARTMENT_ID
from enterprise.domains.synthetic_payment.m8_runtime import (
    GovernedPlanCheckpoint,
    GovernedPlanError,
    PlanJournalEvent,
    PlanJournalTransition,
    PlanRunState,
    PlanStepState,
    _authority_digests,
    _replay,
    append_m10_transition,
)
from enterprise.domains.synthetic_payment.m10_runtime import (
    M10ApprovalPause,
    SyntheticM10PreparedRun,
    SyntheticPaymentRuntimeAdapter,
    derive_agent_run_id,
)
from enterprise.governance.admission import TaskAdmissionBundle
from enterprise.governance.approval_orchestrator import create_approval_pause
from enterprise.governance.approval_pause_service import begin_reobservation_after_approval
from enterprise.governance.models import (
    ExecutionAttemptModel,
    ExecutionPermitModel,
    GovernanceAuditEventModel,
    GovernedTaskAdmissionModel,
    PendingActionModel,
)
from enterprise.governance.pack_runtime import PackRuntimeRegistry
from skyvern.forge.sdk.db.models import StepModel, TaskModel
from skyvern.forge.sdk.models import StepStatus
from skyvern.forge.sdk.schemas.tasks import TaskStatus


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


class AgentRunCommandRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_key: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    reason: str | None = Field(default=None, max_length=240)


class AgentRunPlanStep(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sequence: int = Field(ge=1)
    role: Literal["precheck", "submit", "confirm"]
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


class AgentRunReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["agentpact-agent-run-report/v1"] = "agentpact-agent-run-report/v1"
    projection: AgentRunProjection
    events: tuple[AgentRunTimelineEvent, ...]
    report_digest: str


class AgentRunService:
    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[Any]],
        *,
        runtime_registry: PackRuntimeRegistry,
        target_url: str,
        provider_mode: Literal["recorded", "live"] = "recorded",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._registry = runtime_registry
        self._target_url = target_url
        pack = runtime_registry.public_metadata(pack_id="synthetic.payment", pack_version="1.0.0")
        self._projection_metadata = {
            "pack_id": pack.pack_id,
            "pack_version": pack.pack_version,
            "pack_display_name": pack.display_name,
            "provider_mode": provider_mode,
        }
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    @property
    def _adapter(self) -> SyntheticPaymentRuntimeAdapter:
        adapter = self._registry.require(pack_id="synthetic.payment", pack_version="1.0.0")
        if not isinstance(adapter, SyntheticPaymentRuntimeAdapter):
            raise AgentRunError("ADAPTER_CONFORMANCE_FAILED", status_code=503)
        return adapter

    async def create(self, request: AgentRunCreateRequest, *, user: UserContext) -> AgentRunProjection:
        _require_operator(user)
        intent_digest = _digest(["m10-intent", request.intent])
        existing_run_id = derive_agent_run_id(tenant_id=user.org_id, request_id=request.request_id)
        async with self._session_factory() as session:
            async with session.begin():
                root = (
                    await session.scalars(
                        select(TaskModel)
                        .where(TaskModel.task_id == existing_run_id, TaskModel.organization_id == user.org_id)
                        .with_for_update()
                    )
                ).first()
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
                    ):
                        raise AgentRunError("IDEMPOTENCY_CONFLICT")
                    return await self._project_locked(
                        session,
                        await self._load_locked(session, run_id=existing_run_id, user=user),
                    )
        prepared = self._adapter.prepare_run(
            user=user,
            tenant_id=user.org_id,
            request_id=request.request_id,
            intent_digest=intent_digest,
            business_inputs=request.business_inputs,
            target_url=self._target_url,
            now=self._clock(),
        )
        operation_key = _operation_key(
            tenant_id=user.org_id,
            run_id=prepared.run_id,
            command="create",
            caller_key=request.request_id,
            predecessor="reservation",
        )
        try:
            await self._adapter.admit_run(
                prepared,
                pause_handler=self._pause_for_approval,
                operation_key=operation_key,
            )
        except AgentRunError:
            raise
        except (ValueError, GovernedPlanError) as exc:
            raise AgentRunError("STATE_CONFLICT") from exc
        return await self.get(prepared.run_id, user=user)

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

    async def report(self, run_id: str, *, user: UserContext) -> AgentRunReport:
        projection = await self.get(run_id, user=user)
        events = await self.events(run_id, user=user)
        payload = {
            "schema_version": "agentpact-agent-run-report/v1",
            "projection": projection.model_dump(mode="json"),
            "events": [item.model_dump(mode="json") for item in events],
        }
        return AgentRunReport(projection=projection, events=events, report_digest=_digest(payload))

    async def approve(
        self,
        run_id: str,
        command: AgentRunCommandRequest,
        *,
        user: UserContext,
    ) -> AgentRunProjection:
        _require_approver(user)
        prepared: SyntheticM10PreparedRun | None = None
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
                    await append_m10_transition(
                        session,
                        checkpoint=active,
                        transition=PlanJournalTransition.APPROVAL_RESUMED,
                        authority_digests=locked.authority_digests,
                        operation_key=operation_key,
                        created_at=self._clock(),
                    )
                    if resumed.pending_action.status.value != "invalidated":
                        raise AgentRunError("STATE_CONFLICT")
                    should_advance = True
                prepared = self._restore_prepared(locked.bundle)
        if should_advance and prepared is not None:
            try:
                await self._adapter.advance_run(
                    prepared,
                    pause_handler=self._pause_for_approval,
                    operation_key=command.operation_key,
                )
            except (ValueError, GovernedPlanError) as exc:
                raise AgentRunError("STATE_CONFLICT") from exc
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
                await _cancel_native_pair(session, locked.checkpoint, user.org_id)
                rejected = locked.checkpoint.model_copy(
                    update={
                        "state": PlanRunState.REJECTED,
                        "active_step": locked.checkpoint.active_step.model_copy(update={"state": PlanStepState.FAILED}),
                    }
                )
                await append_m10_transition(
                    session,
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
                await _cancel_native_pair(session, locked.checkpoint, user.org_id)
                cancelled = locked.checkpoint.model_copy(
                    update={
                        "state": PlanRunState.CANCELLED,
                        "active_step": locked.checkpoint.active_step.model_copy(update={"state": PlanStepState.FAILED}),
                    }
                )
                await append_m10_transition(
                    session,
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
                prepared = self._restore_prepared(locked.bundle)
        try:
            await self._adapter.probe_run(
                prepared,
                pause_handler=self._pause_for_approval,
                operation_key=command.operation_key,
            )
        except (ValueError, GovernedPlanError) as exc:
            raise AgentRunError("STATE_CONFLICT") from exc
        return await self.get(run_id, user=user)

    async def _pause_for_approval(
        self,
        *,
        prepared: SyntheticM10PreparedRun,
        checkpoint: object,
        binding: object,
        pause: M10ApprovalPause,
        operation_key: str,
    ) -> GovernedPlanCheckpoint:
        current = GovernedPlanCheckpoint.model_validate(checkpoint)
        resolution = pause.resolution
        if (
            current.active_step is None
            or binding.native_task_id != current.active_step.native_task_id
            or binding.native_step_id != current.active_step.native_step_id
            or resolution.approval_intent is None
            or resolution.approval_decision is None
            or resolution.binding_digest != binding.binding_digest
        ):
            raise AgentRunError("STATE_CONFLICT")
        route = ApprovalRoute(
            requires_approval=True,
            approver_department_id=str(
                (resolution.approval_decision.required_approver or {}).get("department_id", PAYMENTS_DEPARTMENT_ID)
            ),
            approver_role=str((resolution.approval_decision.required_approver or {}).get("role", "approver")),
            description="Governed synthetic payment approval",
        )
        async with self._session_factory() as session:
            async with session.begin():
                await _require_root_lock(session, prepared.run_id, prepared.admission_bundle.task.organization_id)
                await create_approval_pause(
                    db_session=session,
                    task_id=binding.native_task_id,
                    step_id=binding.native_step_id,
                    organization_id=binding.organization_id,
                    contract_id=binding.contract_id,
                    source_department_id=PAYMENTS_DEPARTMENT_ID,
                    action=pause.action,
                    intent=resolution.approval_intent,
                    observation_hash=resolution.observation_hash,
                    decision=resolution.approval_decision,
                    route=route,
                    requester_user_id=prepared.admission_bundle.request.principal_ref,
                    business_line_id=BUSINESS_LINE_ID,
                )
                paused = current.model_copy(update={"state": PlanRunState.APPROVAL_REQUIRED})
                return await append_m10_transition(
                    session,
                    checkpoint=paused,
                    transition=PlanJournalTransition.APPROVAL_REQUIRED,
                    authority_digests=_authority_digests(prepared.compilation),
                    operation_key=operation_key,
                    created_at=self._clock(),
                )

    async def _load_locked(self, session: Any, *, run_id: str, user: UserContext) -> "_LockedRun":
        root = await _require_root_lock(session, run_id, user.org_id)
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
        expected_run_id = derive_agent_run_id(tenant_id=user.org_id, request_id=bundle.request.request_id)
        if root.task_id != expected_run_id or bundle.task.task_id != expected_run_id:
            raise AgentRunError("STATE_CONFLICT")
        models = list(
            (
                await session.scalars(
                    select(GovernanceAuditEventModel).where(
                        GovernanceAuditEventModel.task_id == run_id,
                        GovernanceAuditEventModel.event_type.like("m8.plan.%"),
                    )
                )
            ).all()
        )
        try:
            events = tuple(
                sorted((PlanJournalEvent.model_validate(item.payload) for item in models), key=lambda x: x.sequence)
            )
            checkpoint = _replay(list(events))
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
                run_id=checkpoint.root_task_id,
                state=AgentRunState.SUCCEEDED,
                legal_actions=(),
                plan=plan,
                completed_steps=completed,
                total_steps=total,
            )
        if active is None:
            return self._state_conflict(checkpoint.root_task_id, plan, completed)
        task, step = await _native_pair(session, active.native_task_id, active.native_step_id)
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
                and task.status == TaskStatus.pending_result_probe.value
                and step.status == StepStatus.pending_result_probe.value
                and len(exact_attempt) == 1
                and len(exact_permit) == 1
                and exact_attempt[0].action_fingerprint == exact_permit[0].action_fingerprint
                and exact_attempt[0].observation_hash == exact_permit[0].observation_hash
            ):
                return self._projection(
                    run_id=checkpoint.root_task_id,
                    state=AgentRunState.UNKNOWN,
                    legal_actions=(AgentRunAction.PROBE,),
                    plan=plan,
                    completed_steps=completed,
                    total_steps=total,
                    reason_code="RESULT_UNCERTAIN",
                )
            return self._state_conflict(checkpoint.root_task_id, plan, completed)
        if latest is PlanJournalTransition.APPROVAL_REJECTED:
            if (
                checkpoint.state is PlanRunState.REJECTED
                and task is not None
                and step is not None
                and task.status == TaskStatus.canceled.value
                and step.status == StepStatus.canceled.value
                and pending is not None
                and approval is not None
                and pending.status == "rejected"
                and approval.status == ApprovalStatus.REJECTED.value
                and not attempts
                and not any(item.status == "consumed" for item in permits)
            ):
                return self._projection(
                    run_id=checkpoint.root_task_id,
                    state=AgentRunState.REJECTED,
                    legal_actions=(),
                    plan=plan,
                    completed_steps=completed,
                    total_steps=total,
                    reason_code="APPROVAL_REJECTED",
                )
            return self._state_conflict(checkpoint.root_task_id, plan, completed)
        if latest is PlanJournalTransition.RUN_CANCELLED:
            if (
                checkpoint.state is PlanRunState.CANCELLED
                and task is not None
                and step is not None
                and task.status == TaskStatus.canceled.value
                and step.status == StepStatus.canceled.value
                and not attempts
                and not any(item.status == "consumed" for item in permits)
            ):
                return self._projection(
                    run_id=checkpoint.root_task_id,
                    state=AgentRunState.CANCELLED,
                    legal_actions=(),
                    plan=plan,
                    completed_steps=completed,
                    total_steps=total,
                    reason_code="RUN_CANCELLED",
                )
            return self._state_conflict(checkpoint.root_task_id, plan, completed)
        if checkpoint.state is PlanRunState.APPROVAL_REQUIRED:
            if (
                task is not None
                and step is not None
                and task.status == TaskStatus.pending_approval.value
                and step.status == StepStatus.pending_approval.value
                and pending is not None
                and approval is not None
                and pending.status == "pending"
                and approval.status == ApprovalStatus.PENDING.value
                and not attempts
                and not permits
            ):
                return self._projection(
                    run_id=checkpoint.root_task_id,
                    state=AgentRunState.AWAITING_APPROVAL,
                    legal_actions=(AgentRunAction.APPROVE, AgentRunAction.REJECT),
                    plan=plan,
                    completed_steps=completed,
                    total_steps=total,
                )
            if pending is not None and approval is not None and pending.status == "approved":
                return self._projection(
                    run_id=checkpoint.root_task_id,
                    state=AgentRunState.RUNNING,
                    legal_actions=(),
                    plan=plan,
                    completed_steps=completed,
                    total_steps=total,
                    reason_code="APPROVAL_RECORDED",
                )
            return self._state_conflict(checkpoint.root_task_id, plan, completed)
        if checkpoint.state is PlanRunState.ACTIVE:
            if task is None and step is None and latest is PlanJournalTransition.ADMITTED:
                return self._projection(
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
                and task.status in {TaskStatus.created.value, TaskStatus.running.value, TaskStatus.resuming.value}
                and step.status in {StepStatus.created.value, StepStatus.running.value, StepStatus.resuming.value}
                and not attempts
                and not any(item.status == "consumed" for item in permits)
            ):
                legal = () if task.status == TaskStatus.resuming.value else (AgentRunAction.CANCEL,)
                return self._projection(
                    run_id=checkpoint.root_task_id,
                    state=AgentRunState.RUNNING,
                    legal_actions=legal,
                    plan=plan,
                    completed_steps=completed,
                    total_steps=total,
                )
        return self._state_conflict(checkpoint.root_task_id, plan, completed)

    def _projection(self, **values: Any) -> AgentRunProjection:
        return AgentRunProjection(**self._projection_metadata, **values)

    def _state_conflict(
        self,
        run_id: str,
        plan: tuple[AgentRunPlanStep, ...],
        completed: int,
    ) -> AgentRunProjection:
        return self._projection(
            run_id=run_id,
            state=AgentRunState.FAILED,
            legal_actions=(),
            plan=plan,
            completed_steps=completed,
            total_steps=len(plan),
            reason_code="STATE_CONFLICT",
        )

    def _restore_prepared(self, bundle: TaskAdmissionBundle) -> SyntheticM10PreparedRun:
        token = bundle.request.user_intent_summary.rsplit(" ", 1)[-1]
        requester = UserContext(
            user_id=bundle.request.principal_ref,
            org_id=bundle.request.tenant_id,
            department_roles=[
                DepartmentRole(
                    department_id=PAYMENTS_DEPARTMENT_ID,
                    department_name="Synthetic payments",
                    role="operator",
                )
            ],
            business_line_ids=[BUSINESS_LINE_ID],
        )
        prepared = self._adapter.prepare_run(
            user=requester,
            tenant_id=requester.org_id,
            request_id=bundle.request.request_id,
            intent_digest=token,
            business_inputs=bundle.request.typed_inputs,
            target_url=self._target_url,
            now=bundle.request.submitted_at,
        )
        if prepared.admission_bundle != bundle:
            raise AgentRunError("STATE_CONFLICT")
        return prepared


class _LockedRun(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    root: Any
    bundle: TaskAdmissionBundle
    checkpoint: GovernedPlanCheckpoint
    events: tuple[PlanJournalEvent, ...]
    authority_digests: dict[str, str]


async def _require_root_lock(session: Any, run_id: str, organization_id: str) -> TaskModel:
    root = (
        await session.scalars(
            select(TaskModel)
            .where(TaskModel.task_id == run_id, TaskModel.organization_id == organization_id)
            .with_for_update()
        )
    ).first()
    if root is None:
        raise AgentRunError("RUN_NOT_FOUND", status_code=404)
    return root


async def _native_pair(session: Any, task_id: str, step_id: str) -> tuple[TaskModel | None, StepModel | None]:
    task = (await session.scalars(select(TaskModel).where(TaskModel.task_id == task_id))).first()
    step = (
        await session.scalars(select(StepModel).where(StepModel.step_id == step_id, StepModel.task_id == task_id))
    ).first()
    if (task is None) != (step is None):
        raise AgentRunError("STATE_CONFLICT")
    return task, step


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


async def _cancel_native_pair(session: Any, checkpoint: GovernedPlanCheckpoint, organization_id: str) -> None:
    if checkpoint.active_step is None:
        raise AgentRunError("STATE_CONFLICT")
    task, step = await _native_pair(
        session, checkpoint.active_step.native_task_id, checkpoint.active_step.native_step_id
    )
    if (
        task is None
        or step is None
        or task.organization_id != organization_id
        or step.organization_id != organization_id
    ):
        raise AgentRunError("STATE_CONFLICT")
    task.status = TaskStatus.canceled.value
    step.status = StepStatus.canceled.value
    await session.flush()


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
        role = next((role for role in ("precheck", "submit", "confirm") if role in item.work_order_id), "submit")
        state = {
            PlanStepState.PENDING: "pending",
            PlanStepState.ACTIVE: "active",
            PlanStepState.COMPLETED: "completed",
            PlanStepState.PROBE_BLOCKED: "blocked",
            PlanStepState.FAILED: "terminal",
            PlanStepState.SUPERSEDED: "terminal",
        }[item.state]
        result.append(AgentRunPlanStep(sequence=index, role=role, state=state))
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


def _require_operator(user: UserContext) -> None:
    if not user.is_any_operator:
        raise AgentRunError("OPERATOR_REQUIRED", status_code=403)


def _require_approver(user: UserContext) -> None:
    if not user.is_any_approver:
        raise AgentRunError("APPROVER_REQUIRED", status_code=403)


def _safe_reason(reason: str | None) -> str:
    return "Authenticated decision" if not reason else f"Authenticated decision ({_digest(reason)[:12]})"


def _operation_key(*, tenant_id: str, run_id: str, command: str, caller_key: str, predecessor: str) -> str:
    return f"m10:{command}:{_digest([tenant_id, run_id, command, caller_key, predecessor])}"


def _digest(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
