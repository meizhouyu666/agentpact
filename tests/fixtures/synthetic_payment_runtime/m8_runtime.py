"""Governed sequential M8 plan journal, bounded suffix Replan, and coordinator."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from enterprise.agent.work_orders import (
    BusinessPlan,
    BusinessPlanStep,
    ExecutionWorkOrder,
    RecoveryLevel,
    ReplanAssessment,
    ReplanReason,
    assess_suffix_replan,
    validate_work_order,
)
from enterprise.agent_runs.journal import (
    PLAN_APPLICATION_MARKER as M8_PLAN_MARKER,
)
from enterprise.agent_runs.journal import (
    PLAN_JOURNAL_SCHEMA as M8_JOURNAL_SCHEMA,
)
from enterprise.agent_runs.journal import (
    GovernedPlanCheckpoint,
    GovernedPlanError,
    GovernedPlanStepRef,
    PlanJournalEvent,
    PlanJournalTransition,
    PlanRunState,
    PlanStepState,
)
from enterprise.agent_runs.journal import _stable_id as _generic_stable_id
from enterprise.agent_runs.journal import (
    replay_plan_journal as _replay,
)
from enterprise.domains.synthetic_payment.constants import RESULT_PROBE_REF
from enterprise.governance.admission import TaskAdmissionBundle
from enterprise.governance.contracts import ExecutionAttemptStatus
from enterprise.governance.models import (
    ExecutionAttemptModel,
    ExecutionPermitModel,
    GovernanceAuditEventModel,
    GovernedTaskAdmissionModel,
    TaskContractModel,
)
from enterprise.governance.recovery import ExecutionFailureClass, ExecutionFailureEvent, decide_recovery
from enterprise.governance.result_probes import ResultProbeStatus
from skyvern.forge.native_action import M7_APPLICATION_MARKER
from skyvern.forge.sdk.db.models import StepModel, TaskModel
from skyvern.forge.sdk.models import StepStatus
from skyvern.forge.sdk.schemas.tasks import TaskStatus

from .m6_runtime import SyntheticM6Compilation
from .m7_runtime import (
    NativeProbeEvidence,
    NativeSkyvernBinding,
    NativeSkyvernWorkOrderAdapter,
    derive_native_step_id,
    derive_native_task_id,
)


class ReplanDisposition(StrEnum):
    ACCEPTED = "accepted"
    REAUTHORIZATION_REQUIRED = "reauthorization_required"


class NativeWorkOutcomeKind(StrEnum):
    COMPLETED = "completed"
    BUSINESS_STATE_MISMATCH = "business_state_mismatch"
    PROBE_BLOCKED = "probe_blocked"
    FAILED = "failed"


class ReplanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    plan_run_id: str
    root_task_id: str
    original_goal_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    completed_prefix_digests: tuple[str, ...]
    current_plan_id: str
    current_plan_version: int
    failed_step_id: str
    evidence_refs: tuple[str, ...]
    executable_grant_ids: tuple[str, ...]
    recovery_level: RecoveryLevel
    remaining_replan_budget: int = Field(ge=0)
    predecessor_journal_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class ReplanReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    disposition: ReplanDisposition
    assessment: ReplanAssessment
    accepted_plan_version: int | None = None
    accepted_suffix_step_ids: tuple[str, ...] = ()
    invalidated_work_order_ids: tuple[str, ...] = ()
    revoked_permit_ids: tuple[str, ...] = ()
    checkpoint: GovernedPlanCheckpoint


class NativeWorkOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: NativeWorkOutcomeKind
    permit_id: str | None = None
    attempt_id: str | None = None
    probe_ref: str | None = None
    evidence_refs: tuple[str, ...] = ()
    message: str = ""


class SyntheticM8Compilation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    plan_run_id: str
    admission_id: str
    authority: SyntheticM6Compilation
    business_plan: BusinessPlan
    work_orders: tuple[ExecutionWorkOrder, ...]
    child_compilations: tuple[SyntheticM6Compilation, ...]

    def child_for(self, work_order_id: str) -> SyntheticM6Compilation:
        matches = [item for item in self.child_compilations if item.work_order.work_order_id == work_order_id]
        if len(matches) != 1:
            raise GovernedPlanError("M8 child compilation identity is missing or ambiguous")
        return matches[0]


class GovernedNativeRunner(Protocol):
    async def execute(
        self,
        *,
        compilation: SyntheticM6Compilation,
        binding: NativeSkyvernBinding,
    ) -> NativeWorkOutcome: ...


class GovernedPlanJournal(Protocol):
    async def initialize(
        self,
        *,
        compilation: SyntheticM8Compilation,
        admission_bundle: TaskAdmissionBundle,
        target_url: str,
    ) -> GovernedPlanCheckpoint: ...

    async def append(
        self,
        *,
        checkpoint: GovernedPlanCheckpoint,
        transition: PlanJournalTransition,
        authority_digests: dict[str, str],
        recovery_level: RecoveryLevel | None = None,
        reason: str | None = None,
        superseded_task_ids: tuple[str, ...] = (),
        replacement_admission: TaskAdmissionBundle | None = None,
    ) -> tuple[GovernedPlanCheckpoint, tuple[str, ...]]: ...


def build_synthetic_m8_compilation(
    authority: SyntheticM6Compilation,
    *,
    admission_id: str,
    plan_run_id: str,
    step_roles: tuple[str, ...] | None = None,
) -> SyntheticM8Compilation:
    """Expand trusted authority into sequential child Work Orders for trusted step roles."""

    if len(authority.business_plan.steps) != 1:
        raise ValueError("M8 synthetic compiler requires one trusted M6 authority step")
    root_step = authority.business_plan.steps[0]
    roles = step_roles or ("precheck", "submit", "confirm")
    _require_plan_roles(roles)
    steps = tuple(
        root_step.model_copy(
            update={
                "step_id": _stable_id(
                    plan_run_id,
                    f"step-v1-{role}" if step_roles is None else f"step-v1-{index}-{role}",
                ),
                "success_criteria": [f"M8 {role} completes within the admitted authority envelope"],
            },
            deep=True,
        )
        for index, role in enumerate(roles)
    )
    plan = authority.business_plan.model_copy(
        update={
            "plan_id": _stable_id(plan_run_id, "plan-v1"),
            "version": 1,
            "replan_reason": None,
            "steps": list(steps),
        },
        deep=True,
    )
    work_orders: list[ExecutionWorkOrder] = []
    children: list[SyntheticM6Compilation] = []
    for index, (role, step) in enumerate(zip(roles, steps, strict=True)):
        role_key = role if step_roles is None else f"{index}-{role}"
        work_order_id = _stable_id(plan_run_id, f"work-order-v1-{role_key}")
        native_task_id = derive_native_task_id(
            admission_id=admission_id,
            request_id=authority.trace.request_id,
            work_order_id=work_order_id,
        )
        native_contract_id = _stable_id(plan_run_id, f"native-contract-v1-{role_key}")
        work_order = ExecutionWorkOrder(
            work_order_id=work_order_id,
            business_plan_step_id=step.step_id,
            task_id=native_task_id,
            contract_id=native_contract_id,
            plan_task_id=plan.task_id,
            authority_contract_id=plan.contract_id,
            grant_id=step.grant_id,
            navigation_goal=f"M8 governed {role} for the admitted synthetic payment",
            allowed_operations={"read"} if role != "submit" else {"read", "input", "select", "submit"},
            prohibited_operations={"javascript", "coordinate", "download", "upload"},
            success_criteria=step.success_criteria,
            required_evidence=["plan_journal", "native_binding", RESULT_PROBE_REF],
            max_recovery_level=RecoveryLevel.L3,
            result_probe_ref=RESULT_PROBE_REF,
        )
        validate_work_order(work_order, plan, step, authority.grants, now=authority.grants.grants[0].resolved_at)
        child_plan = plan.model_copy(update={"steps": [step]}, deep=True)
        child = authority.model_copy(
            update={"business_plan": child_plan, "work_order": work_order},
            deep=True,
        )
        work_orders.append(work_order)
        children.append(child)
    return SyntheticM8Compilation(
        plan_run_id=plan_run_id,
        admission_id=admission_id,
        authority=authority,
        business_plan=plan,
        work_orders=tuple(work_orders),
        child_compilations=tuple(children),
    )


def build_m8_admission_bundle(
    original: TaskAdmissionBundle,
    compilation: SyntheticM8Compilation,
) -> TaskAdmissionBundle:
    """Bind the current M8 plan/child set to the immutable root admission identity."""

    if (
        original.admission_id != compilation.admission_id
        or original.task.task_id != compilation.business_plan.task_id
        or original.contract.contract_id != compilation.business_plan.contract_id
        or original.request.request_id != compilation.authority.trace.request_id
        or tuple(original.grants) != tuple(compilation.authority.grants.grants)
    ):
        raise GovernedPlanError("M8 replacement admission changed root authority")
    audit = original.audit_record.model_copy(update={"plan_id": compilation.business_plan.plan_id})
    return original.model_copy(
        update={
            "plan": compilation.business_plan,
            "work_orders": compilation.work_orders,
            "audit_record": audit,
        },
        deep=True,
    )


def build_replacement_suffix(
    previous: SyntheticM8Compilation,
    *,
    completed_prefix_length: int,
    replacement_roles: tuple[str, ...] | None = None,
) -> SyntheticM8Compilation:
    """Create a deterministic direct-successor plan with new IDs only in the pending suffix."""

    if completed_prefix_length < 0 or completed_prefix_length >= len(previous.business_plan.steps):
        raise ValueError("M8 replacement requires a non-empty pending suffix")
    next_version = previous.business_plan.version + 1
    prefix = list(previous.business_plan.steps[:completed_prefix_length])
    old_suffix = previous.business_plan.steps[completed_prefix_length:]
    roles = replacement_roles or tuple(
        _work_order_role(item) for item in previous.work_orders[completed_prefix_length:]
    )
    if len(roles) != len(old_suffix) or any(role not in {"precheck", "submit", "confirm"} for role in roles):
        raise ValueError("M8 replacement roles must exactly cover the pending finite-role suffix")
    suffix = [
        step.model_copy(
            update={"step_id": _stable_id(previous.plan_run_id, f"step-v{next_version}-{index}")},
            deep=True,
        )
        for index, step in enumerate(old_suffix, start=completed_prefix_length)
    ]
    plan = previous.business_plan.model_copy(
        update={
            "plan_id": _stable_id(previous.plan_run_id, f"plan-v{next_version}"),
            "version": next_version,
            "replan_reason": ReplanReason.BUSINESS_STATE_CHANGED,
            "steps": [*prefix, *suffix],
        },
        deep=True,
    )
    assessment = assess_suffix_replan(
        previous.business_plan,
        plan,
        completed_prefix_length=completed_prefix_length,
    )
    if assessment.requires_reauthorization:
        raise GovernedPlanError("M8 deterministic replacement unexpectedly changed authority")
    prefix_orders = list(previous.work_orders[:completed_prefix_length])
    orders: list[ExecutionWorkOrder] = prefix_orders
    children: list[SyntheticM6Compilation] = list(previous.child_compilations[:completed_prefix_length])
    for index, (step, role) in enumerate(
        zip(suffix, roles, strict=True),
        start=completed_prefix_length,
    ):
        work_order_id = _stable_id(previous.plan_run_id, f"work-order-v{next_version}-{index}")
        native_task_id = derive_native_task_id(
            admission_id=previous.admission_id,
            request_id=previous.authority.trace.request_id,
            work_order_id=work_order_id,
        )
        old = previous.work_orders[index]
        order = old.model_copy(
            update={
                "work_order_id": work_order_id,
                "business_plan_step_id": step.step_id,
                "task_id": native_task_id,
                "contract_id": _stable_id(previous.plan_run_id, f"native-contract-v{next_version}-{index}"),
                "navigation_goal": f"M8 governed {role} for the admitted synthetic payment",
                "allowed_operations": ({"read"} if role != "submit" else {"read", "input", "select", "submit"}),
            },
            deep=True,
        )
        validate_work_order(
            order, plan, step, previous.authority.grants, now=previous.authority.grants.grants[0].resolved_at
        )
        orders.append(order)
        children.append(
            previous.authority.model_copy(
                update={"business_plan": plan.model_copy(update={"steps": [step]}), "work_order": order},
                deep=True,
            )
        )
    return SyntheticM8Compilation(
        plan_run_id=previous.plan_run_id,
        admission_id=previous.admission_id,
        authority=previous.authority,
        business_plan=plan,
        work_orders=tuple(orders),
        child_compilations=tuple(children),
    )


def initial_checkpoint(compilation: SyntheticM8Compilation, *, max_replans: int = 2) -> GovernedPlanCheckpoint:
    refs = tuple(
        _step_ref(step, order, PlanStepState.ACTIVE if index == 0 else PlanStepState.PENDING)
        for index, (step, order) in enumerate(
            zip(compilation.business_plan.steps, compilation.work_orders, strict=True)
        )
    )
    return GovernedPlanCheckpoint(
        plan_run_id=compilation.plan_run_id,
        admission_id=compilation.admission_id,
        root_task_id=compilation.business_plan.task_id,
        plan_id=compilation.business_plan.plan_id,
        plan_version=compilation.business_plan.version,
        authority_contract_id=compilation.business_plan.contract_id,
        active_step=refs[0],
        remaining_suffix=refs[1:],
        max_replans=max_replans,
    )


class SqlAlchemyGovernedPlanJournal:
    """Hash-chained M8 state machine over the existing governance audit table."""

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[Any]],
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def initialize(
        self,
        *,
        compilation: SyntheticM8Compilation,
        admission_bundle: TaskAdmissionBundle,
        target_url: str,
    ) -> GovernedPlanCheckpoint:
        if (
            admission_bundle.admission_id != compilation.admission_id
            or admission_bundle.plan != compilation.business_plan
            or admission_bundle.work_orders != compilation.work_orders
            or admission_bundle.task.task_id != compilation.business_plan.task_id
            or admission_bundle.contract.contract_id != compilation.business_plan.contract_id
        ):
            raise GovernedPlanError("M8 durable admission does not match the compiled root/child mapping")
        checkpoint = initial_checkpoint(compilation)
        async with self._session_factory() as session:
            async with session.begin():
                admission = (
                    await session.scalars(
                        select(GovernedTaskAdmissionModel)
                        .where(GovernedTaskAdmissionModel.admission_id == compilation.admission_id)
                        .with_for_update()
                    )
                ).first()
                if (
                    admission is None
                    or TaskAdmissionBundle.model_validate(admission.bundle_payload) != admission_bundle
                ):
                    raise GovernedPlanError("M8 requires the exact durable admission before plan activation")
                root = await _load_task(session, checkpoint.root_task_id, lock=True)
                if root is None:
                    root = TaskModel(
                        task_id=checkpoint.root_task_id,
                        organization_id=admission_bundle.task.organization_id,
                        status=TaskStatus.running.value,
                        title="AgentPact M8 governed plan root",
                        url=target_url,
                        navigation_goal=admission_bundle.task.goal,
                        navigation_payload={},
                        application=M8_PLAN_MARKER,
                        errors=[],
                    )
                    session.add(root)
                    await session.flush()
                elif root.application != M8_PLAN_MARKER or root.status not in {
                    TaskStatus.running.value,
                    TaskStatus.completed.value,
                }:
                    raise GovernedPlanError("M8 root Task conflicts with the admitted plan")
                contract = (
                    await session.scalars(
                        select(TaskContractModel)
                        .where(TaskContractModel.contract_id == checkpoint.authority_contract_id)
                        .with_for_update()
                    )
                ).first()
                if contract is None:
                    contract = _root_contract(admission_bundle)
                    session.add(contract)
                    await session.flush()
                elif contract.task_id != checkpoint.root_task_id:
                    raise GovernedPlanError("M8 root authority contract is bound to another Task")
                events = await _load_events(session, checkpoint.root_task_id)
                if events:
                    restored = _replay(events)
                    if restored.plan_run_id != checkpoint.plan_run_id:
                        raise GovernedPlanError("M8 root Task already owns a different plan run")
                    for _ in range(2):
                        recovery = await _recover_journal_lag(
                            session,
                            checkpoint=restored,
                            transition=events[-1].transition,
                            compilation=compilation,
                        )
                        if recovery is None:
                            await _verify_checkpoint_native_state(
                                session,
                                restored,
                                transition=events[-1].transition,
                            )
                            return restored
                        recovered_checkpoint, recovered_transition, recovery_reason = recovery
                        if recovered_transition is PlanJournalTransition.PROBE_BLOCKED:
                            if not await _probe_lag_is_exactly_uncertain(
                                session,
                                recovered_checkpoint.active_step,
                            ):
                                # A confirmed native state can be bridged through the missing
                                # suspension event, but it must immediately resolve in the
                                # same locked resume operation.
                                if not await _probe_lag_is_exactly_confirmed(
                                    session,
                                    recovered_checkpoint.active_step,
                                ):
                                    raise GovernedPlanError("M8 uncorrelated probe lag state")
                        else:
                            await _verify_checkpoint_native_state(
                                session,
                                recovered_checkpoint,
                                transition=recovered_transition,
                            )
                        event = _event(
                            checkpoint=recovered_checkpoint,
                            transition=recovered_transition,
                            authority_digests=_authority_digests(compilation),
                            recovery_level=RecoveryLevel.L4,
                            reason=recovery_reason,
                            created_at=self._clock(),
                        )
                        session.add(_event_model(event, admission_bundle.task.organization_id))
                        if recovered_transition is PlanJournalTransition.PLAN_COMPLETED:
                            root.status = TaskStatus.completed.value
                            root.finished_at = self._clock()
                        await session.flush()
                        events.append(event)
                        restored = recovered_checkpoint.model_copy(
                            update={"journal_sequence": event.sequence, "journal_digest": event.event_digest}
                        )
                    raise GovernedPlanError("M8 journal lag exceeded one bounded recovery operation")
                event = _event(
                    checkpoint=checkpoint,
                    transition=PlanJournalTransition.ADMITTED,
                    authority_digests=_authority_digests(compilation),
                    created_at=self._clock(),
                )
                session.add(_event_model(event, admission_bundle.task.organization_id))
                await session.flush()
        return checkpoint.model_copy(update={"journal_sequence": event.sequence, "journal_digest": event.event_digest})

    async def append(
        self,
        *,
        checkpoint: GovernedPlanCheckpoint,
        transition: PlanJournalTransition,
        authority_digests: dict[str, str],
        recovery_level: RecoveryLevel | None = None,
        reason: str | None = None,
        superseded_task_ids: tuple[str, ...] = (),
        replacement_admission: TaskAdmissionBundle | None = None,
    ) -> tuple[GovernedPlanCheckpoint, tuple[str, ...]]:
        transition = PlanJournalTransition(transition)
        revoked: list[str] = []
        async with self._session_factory() as session:
            async with session.begin():
                root = await _load_task(session, checkpoint.root_task_id, lock=True)
                if root is None or root.application != M8_PLAN_MARKER:
                    raise GovernedPlanError("M8 journal root Task is missing or untrusted")
                events = await _load_events(session, checkpoint.root_task_id)
                restored = _replay(events)
                if (
                    restored.journal_sequence != checkpoint.journal_sequence
                    or restored.journal_digest != checkpoint.journal_digest
                ):
                    if restored.journal_sequence == checkpoint.journal_sequence + 1:
                        committed = events[-1]
                        intended = _event(
                            checkpoint=checkpoint,
                            transition=transition,
                            authority_digests=authority_digests,
                            recovery_level=recovery_level,
                            reason=reason,
                            created_at=committed.created_at,
                        )
                        if committed != intended:
                            raise GovernedPlanError("M8 committed one-event-ahead append conflicts with retry")
                        revoked = await _reconcile_committed_duplicate_side_effects(
                            session,
                            checkpoint=checkpoint,
                            transition=transition,
                            superseded_task_ids=superseded_task_ids,
                            replacement_admission=replacement_admission,
                        )
                        return restored, tuple(revoked)
                    raise GovernedPlanError("M8 unrelated stale or concurrent advance detected")
                if superseded_task_ids:
                    revoked = await _supersede_unstarted(session, superseded_task_ids)
                if replacement_admission is not None:
                    _require_checkpoint_admission(checkpoint, replacement_admission)
                    admission = (
                        await session.scalars(
                            select(GovernedTaskAdmissionModel)
                            .where(GovernedTaskAdmissionModel.admission_id == checkpoint.admission_id)
                            .with_for_update()
                        )
                    ).first()
                    if admission is None:
                        raise GovernedPlanError("M8 replacement admission is missing")
                    if (
                        admission.task_id != checkpoint.root_task_id
                        or admission.contract_id != checkpoint.authority_contract_id
                        or admission.organization_id != replacement_admission.task.organization_id
                        or admission.request_id != replacement_admission.request.request_id
                    ):
                        raise GovernedPlanError("M8 replacement admission identity conflicts with durable root")
                    admission.bundle_payload = replacement_admission.model_dump(mode="json")
                    admission.bundle_fingerprint = _digest(replacement_admission)
                if transition in {
                    PlanJournalTransition.CHILD_COMPLETED,
                    PlanJournalTransition.PLAN_COMPLETED,
                }:
                    await _finalize_completed_native(session, checkpoint.completed_prefix[-1])
                await _verify_checkpoint_native_state(
                    session,
                    checkpoint,
                    transition=transition,
                )
                event = _event(
                    checkpoint=checkpoint,
                    transition=transition,
                    authority_digests=authority_digests,
                    recovery_level=recovery_level,
                    reason=reason,
                    created_at=self._clock(),
                )
                session.add(_event_model(event, root.organization_id))
                if transition is PlanJournalTransition.PLAN_COMPLETED:
                    root.status = TaskStatus.completed.value
                    root.finished_at = self._clock()
                await session.flush()
        return checkpoint.model_copy(
            update={"journal_sequence": event.sequence, "journal_digest": event.event_digest}
        ), tuple(revoked)


class GovernedPlanCoordinator:
    """Execute one admitted child at a time and stop on Replan/probe/L4 boundaries."""

    def __init__(
        self,
        journal: GovernedPlanJournal,
        *,
        adapter_factory: Callable[[SyntheticM6Compilation, TaskAdmissionBundle], NativeSkyvernWorkOrderAdapter],
        runner: GovernedNativeRunner,
    ) -> None:
        self._journal = journal
        self._adapter_factory = adapter_factory
        self._runner = runner

    async def start(
        self,
        *,
        compilation: SyntheticM8Compilation,
        admission_bundle: TaskAdmissionBundle,
        target_url: str,
    ) -> GovernedPlanCheckpoint:
        checkpoint = await self._journal.initialize(
            compilation=compilation,
            admission_bundle=admission_bundle,
            target_url=target_url,
        )
        return await self.run_until_pause(
            compilation=compilation,
            admission_bundle=admission_bundle,
            checkpoint=checkpoint,
        )

    async def resume(
        self,
        *,
        compilation: SyntheticM8Compilation,
        admission_bundle: TaskAdmissionBundle,
        target_url: str,
    ) -> GovernedPlanCheckpoint:
        return await self.start(
            compilation=compilation,
            admission_bundle=admission_bundle,
            target_url=target_url,
        )

    async def run_until_pause(
        self,
        *,
        compilation: SyntheticM8Compilation,
        admission_bundle: TaskAdmissionBundle,
        checkpoint: GovernedPlanCheckpoint,
    ) -> GovernedPlanCheckpoint:
        _require_checkpoint_compilation(checkpoint, compilation)
        current = checkpoint
        while current.state is PlanRunState.ACTIVE and current.active_step is not None:
            active = current.active_step
            child = compilation.child_for(active.work_order_id)
            adapter = self._adapter_factory(child, admission_bundle)
            binding = await adapter.prepare(child.work_order)
            if (
                binding.native_task_id != active.native_task_id
                or binding.native_step_id != active.native_step_id
                or binding.plan_task_id != current.root_task_id
                or binding.authority_contract_id != current.authority_contract_id
            ):
                raise GovernedPlanError("M8 adapter returned a substituted root/child binding")
            current, _ = await self._journal.append(
                checkpoint=current,
                transition=PlanJournalTransition.CHILD_ACTIVATED,
                authority_digests=_authority_digests(compilation),
            )
            outcome = await self._runner.execute(compilation=child, binding=binding)
            if outcome.kind is NativeWorkOutcomeKind.COMPLETED:
                current = _complete_active(current, outcome)
                transition = (
                    PlanJournalTransition.PLAN_COMPLETED
                    if current.state is PlanRunState.COMPLETED
                    else PlanJournalTransition.CHILD_COMPLETED
                )
                current, _ = await self._journal.append(
                    checkpoint=current,
                    transition=transition,
                    authority_digests=_authority_digests(compilation),
                )
                continue
            if outcome.kind is NativeWorkOutcomeKind.PROBE_BLOCKED:
                current = current.model_copy(
                    update={
                        "state": PlanRunState.PROBE_BLOCKED,
                        "active_step": active.model_copy(
                            update={
                                "state": PlanStepState.PROBE_BLOCKED,
                                "permit_id": outcome.permit_id,
                                "attempt_id": outcome.attempt_id,
                                "probe_ref": outcome.probe_ref,
                            }
                        ),
                    }
                )
                current, _ = await self._journal.append(
                    checkpoint=current,
                    transition=PlanJournalTransition.PROBE_BLOCKED,
                    authority_digests=_authority_digests(compilation),
                    recovery_level=RecoveryLevel.L4,
                    reason=outcome.message or "UNKNOWN requires authoritative result probe",
                )
                return current
            if outcome.kind is NativeWorkOutcomeKind.BUSINESS_STATE_MISMATCH:
                decision = decide_recovery(
                    ExecutionFailureEvent(
                        task_id=active.native_task_id,
                        step_id=active.native_step_id,
                        failure_class=ExecutionFailureClass.BUSINESS_STATE_MISMATCH,
                        contract_scope_unchanged=True,
                        message=outcome.message,
                    )
                )
                if decision.level.value != RecoveryLevel.L3.value:
                    raise GovernedPlanError("M8 business mismatch did not resolve to L3")
                current = current.model_copy(update={"state": PlanRunState.REPLAN_REQUIRED})
                current, _ = await self._journal.append(
                    checkpoint=current,
                    transition=PlanJournalTransition.REPLAN_REQUIRED,
                    authority_digests=_authority_digests(compilation),
                    recovery_level=RecoveryLevel.L3,
                    reason=outcome.message or decision.reason,
                )
                return current
            current = current.model_copy(update={"state": PlanRunState.REAUTHORIZATION_REQUIRED})
            current, _ = await self._journal.append(
                checkpoint=current,
                transition=PlanJournalTransition.REAUTHORIZATION_REQUIRED,
                authority_digests=_authority_digests(compilation),
                recovery_level=RecoveryLevel.L4,
                reason=outcome.message or "M8 native execution requires owner intervention",
            )
            return current
        return current

    async def resolve_probe(
        self,
        *,
        compilation: SyntheticM8Compilation,
        checkpoint: GovernedPlanCheckpoint,
        outcome: NativeWorkOutcome,
    ) -> GovernedPlanCheckpoint:
        _require_checkpoint_compilation(checkpoint, compilation)
        active = checkpoint.active_step
        if checkpoint.state is not PlanRunState.PROBE_BLOCKED or active is None:
            raise GovernedPlanError("M8 probe resolution requires a probe-blocked active child")
        if outcome.kind is NativeWorkOutcomeKind.PROBE_BLOCKED:
            return checkpoint
        if outcome.kind is not NativeWorkOutcomeKind.COMPLETED:
            raise GovernedPlanError("M8 authoritative probe did not prove child completion")
        if (
            outcome.permit_id != active.permit_id
            or outcome.attempt_id != active.attempt_id
            or outcome.probe_ref != active.probe_ref
        ):
            raise GovernedPlanError("M8 probe outcome identity does not match the blocked child")
        resolved = _complete_active(checkpoint, outcome)
        transition = (
            PlanJournalTransition.PLAN_COMPLETED
            if resolved.state is PlanRunState.COMPLETED
            else PlanJournalTransition.PROBE_RESOLVED
        )
        resolved, _ = await self._journal.append(
            checkpoint=resolved,
            transition=transition,
            authority_digests=_authority_digests(compilation),
            recovery_level=RecoveryLevel.L4,
            reason="Authoritative M7 probe finalized the UNKNOWN child",
        )
        return resolved

    async def apply_replan(
        self,
        *,
        previous: SyntheticM8Compilation,
        proposed: SyntheticM8Compilation,
        checkpoint: GovernedPlanCheckpoint,
        admission_bundle: TaskAdmissionBundle,
    ) -> ReplanReceipt:
        _require_checkpoint_compilation(checkpoint, previous)
        if checkpoint.state is PlanRunState.PROBE_BLOCKED:
            raise GovernedPlanError("M8 cannot Replan while an Attempt is UNKNOWN")
        if checkpoint.state is not PlanRunState.REPLAN_REQUIRED or checkpoint.active_step is None:
            raise GovernedPlanError("M8 Replan requires one failed active suffix step")
        if checkpoint.replan_count >= checkpoint.max_replans:
            blocked = checkpoint.model_copy(update={"state": PlanRunState.REAUTHORIZATION_REQUIRED})
            blocked, _ = await self._journal.append(
                checkpoint=blocked,
                transition=PlanJournalTransition.REAUTHORIZATION_REQUIRED,
                authority_digests=_authority_digests(previous),
                recovery_level=RecoveryLevel.L4,
                reason="M8 Replan budget exhausted",
            )
            return ReplanReceipt(
                disposition=ReplanDisposition.REAUTHORIZATION_REQUIRED,
                assessment=ReplanAssessment(requires_reauthorization=True, reasons=["Replan budget exhausted"]),
                checkpoint=blocked,
            )
        prefix_len = len(checkpoint.completed_prefix)
        _require_checkpoint_admission(checkpoint, admission_bundle, match_plan=False)
        if (
            admission_bundle.plan != proposed.business_plan
            or admission_bundle.work_orders != proposed.work_orders
            or admission_bundle.admission_id != proposed.admission_id
        ):
            raise GovernedPlanError("M8 Replan admission does not contain the proposed suffix")
        assessment = assess_suffix_replan(
            previous.business_plan,
            proposed.business_plan,
            completed_prefix_length=prefix_len,
        )
        work_order_reason = _assess_work_order_suffix(previous, proposed, prefix_len)
        if work_order_reason:
            assessment = assessment.model_copy(
                update={
                    "requires_reauthorization": True,
                    "reasons": [*assessment.reasons, work_order_reason],
                    "invalidated_contract_ids": {previous.business_plan.contract_id},
                    "invalidated_grant_ids": {item.grant_id for item in previous.work_orders[prefix_len:]},
                }
            )
        if assessment.requires_reauthorization:
            blocked = checkpoint.model_copy(update={"state": PlanRunState.REAUTHORIZATION_REQUIRED})
            blocked, _ = await self._journal.append(
                checkpoint=blocked,
                transition=PlanJournalTransition.REAUTHORIZATION_REQUIRED,
                authority_digests=_authority_digests(previous),
                recovery_level=RecoveryLevel.L4,
                reason="; ".join(assessment.reasons),
            )
            return ReplanReceipt(
                disposition=ReplanDisposition.REAUTHORIZATION_REQUIRED,
                assessment=assessment,
                checkpoint=blocked,
            )
        old_suffix = (checkpoint.active_step, *checkpoint.remaining_suffix)
        new_refs = tuple(
            _step_ref(step, order, PlanStepState.ACTIVE if index == 0 else PlanStepState.PENDING)
            for index, (step, order) in enumerate(
                zip(
                    proposed.business_plan.steps[prefix_len:],
                    proposed.work_orders[prefix_len:],
                    strict=True,
                )
            )
        )
        if not new_refs:
            raise GovernedPlanError("M8 Replan cannot remove the failed active suffix")
        superseded = tuple(item.model_copy(update={"state": PlanStepState.SUPERSEDED}) for item in old_suffix)
        replaced = checkpoint.model_copy(
            update={
                "plan_id": proposed.business_plan.plan_id,
                "plan_version": proposed.business_plan.version,
                "active_step": new_refs[0],
                "remaining_suffix": new_refs[1:],
                "superseded_suffix": (*checkpoint.superseded_suffix, *superseded),
                "state": PlanRunState.ACTIVE,
                "replan_count": checkpoint.replan_count + 1,
            }
        )
        replaced, revoked = await self._journal.append(
            checkpoint=replaced,
            transition=PlanJournalTransition.SUFFIX_SUPERSEDED,
            authority_digests=_authority_digests(proposed),
            recovery_level=RecoveryLevel.L3,
            reason="Authorized suffix replacement",
            superseded_task_ids=tuple(item.native_task_id for item in old_suffix),
            replacement_admission=admission_bundle,
        )
        return ReplanReceipt(
            disposition=ReplanDisposition.ACCEPTED,
            assessment=assessment,
            accepted_plan_version=proposed.business_plan.version,
            accepted_suffix_step_ids=assessment.accepted_suffix_step_ids,
            invalidated_work_order_ids=tuple(item.work_order_id for item in old_suffix),
            revoked_permit_ids=revoked,
            checkpoint=replaced,
        )


def build_redacted_m8_trace(checkpoint: GovernedPlanCheckpoint) -> dict[str, Any]:
    return {
        "schema_version": "agentpact-m8-trace/v1",
        "plan_run_id": checkpoint.plan_run_id,
        "root_task_id": checkpoint.root_task_id,
        "plan_id": checkpoint.plan_id,
        "plan_version": checkpoint.plan_version,
        "state": checkpoint.state.value,
        "replan_count": checkpoint.replan_count,
        "completed_prefix": [_trace_ref(item) for item in checkpoint.completed_prefix],
        "active_step": _trace_ref(checkpoint.active_step) if checkpoint.active_step else None,
        "remaining_suffix": [_trace_ref(item) for item in checkpoint.remaining_suffix],
        "superseded_suffix": [_trace_ref(item) for item in checkpoint.superseded_suffix],
        "journal_sequence": checkpoint.journal_sequence,
        "journal_digest": checkpoint.journal_digest,
    }


def _complete_active(checkpoint: GovernedPlanCheckpoint, outcome: NativeWorkOutcome) -> GovernedPlanCheckpoint:
    active = checkpoint.active_step
    if active is None:
        raise GovernedPlanError("M8 cannot complete a missing active step")
    completed = active.model_copy(
        update={
            "state": PlanStepState.COMPLETED,
            "permit_id": outcome.permit_id,
            "attempt_id": outcome.attempt_id,
            "probe_ref": outcome.probe_ref,
        }
    )
    remaining = checkpoint.remaining_suffix
    next_active = remaining[0].model_copy(update={"state": PlanStepState.ACTIVE}) if remaining else None
    return checkpoint.model_copy(
        update={
            "completed_prefix": (*checkpoint.completed_prefix, completed),
            "active_step": next_active,
            "remaining_suffix": remaining[1:] if remaining else (),
            "state": PlanRunState.ACTIVE if next_active else PlanRunState.COMPLETED,
        }
    )


def _step_ref(step: BusinessPlanStep, order: ExecutionWorkOrder, state: PlanStepState) -> GovernedPlanStepRef:
    return GovernedPlanStepRef(
        business_plan_step_id=step.step_id,
        step_digest=_digest(step),
        work_order_id=order.work_order_id,
        work_order_digest=_digest(order),
        native_task_id=order.task_id,
        native_step_id=derive_native_step_id(native_task_id=order.task_id),
        native_contract_id=order.contract_id,
        authority_contract_id=order.authority_contract_id or order.contract_id,
        state=state,
    )


def _event(
    *,
    checkpoint: GovernedPlanCheckpoint,
    transition: PlanJournalTransition,
    authority_digests: dict[str, str],
    created_at: datetime,
    recovery_level: RecoveryLevel | None = None,
    reason: str | None = None,
) -> PlanJournalEvent:
    transition = PlanJournalTransition(transition)
    sequence = checkpoint.journal_sequence + 1
    previous = checkpoint.journal_digest
    checkpoint_payload = checkpoint.model_copy(update={"journal_sequence": sequence, "journal_digest": previous})
    values = {
        "schema_version": M8_JOURNAL_SCHEMA,
        "event_id": _stable_id(
            checkpoint.plan_run_id,
            f"event-v{checkpoint.plan_version}-{sequence}-{transition.value}",
        ),
        "plan_run_id": checkpoint.plan_run_id,
        "root_task_id": checkpoint.root_task_id,
        "plan_id": checkpoint.plan_id,
        "plan_version": checkpoint.plan_version,
        "sequence": sequence,
        "previous_event_digest": previous,
        "transition": transition,
        "recovery_level": recovery_level,
        "reason": reason,
        "authority_digests": authority_digests,
        "checkpoint": checkpoint_payload,
        "created_at": created_at,
    }
    unsigned = PlanJournalEvent(event_digest="0" * 64, **values)
    event_digest = _digest(unsigned.model_dump(mode="json", exclude={"event_digest"}))
    return unsigned.model_copy(update={"event_digest": event_digest})


async def _load_events(session: Any, root_task_id: str) -> list[PlanJournalEvent]:
    models = list(
        (await session.scalars(select(GovernanceAuditEventModel).where(GovernanceAuditEventModel.task_id == root_task_id))).all()
    )
    events = [PlanJournalEvent.model_validate(item.payload) for item in models]
    return sorted(events, key=lambda item: item.sequence)


async def _recover_journal_lag(
    session: Any,
    *,
    checkpoint: GovernedPlanCheckpoint,
    transition: PlanJournalTransition,
    compilation: SyntheticM8Compilation,
) -> tuple[GovernedPlanCheckpoint, PlanJournalTransition, str] | None:
    if transition not in {
        PlanJournalTransition.CHILD_ACTIVATED,
        PlanJournalTransition.PROBE_BLOCKED,
    }:
        return None
    active = checkpoint.active_step
    if active is None:
        raise GovernedPlanError("M8 journal lag is missing its active child")
    correlation = await _correlate_probe_state(session, active=active, compilation=compilation)
    if correlation is None:
        return None
    outcome = correlation["outcome"]
    if outcome == ExecutionAttemptStatus.UNKNOWN.value:
        if transition is PlanJournalTransition.PROBE_BLOCKED:
            return None
        blocked_ref = active.model_copy(
            update={
                "state": PlanStepState.PROBE_BLOCKED,
                "permit_id": correlation["permit"].permit_id,
                "attempt_id": correlation["attempt"].attempt_id,
                "probe_ref": correlation["probe_ref"],
            }
        )
        return (
            checkpoint.model_copy(update={"state": PlanRunState.PROBE_BLOCKED, "active_step": blocked_ref}),
            PlanJournalTransition.PROBE_BLOCKED,
            "Recovered exact M7 UNKNOWN state before the M8 probe-block event",
        )
    if outcome == ExecutionAttemptStatus.CONFIRMED.value:
        if transition is PlanJournalTransition.CHILD_ACTIVATED:
            blocked_ref = active.model_copy(
                update={
                    "state": PlanStepState.PROBE_BLOCKED,
                    "permit_id": correlation["permit"].permit_id,
                    "attempt_id": correlation["attempt"].attempt_id,
                    "probe_ref": correlation["probe_ref"],
                }
            )
            return (
                checkpoint.model_copy(update={"state": PlanRunState.PROBE_BLOCKED, "active_step": blocked_ref}),
                PlanJournalTransition.PROBE_BLOCKED,
                "Recovered exact M7 probe-finalized state across the missing M8 suspension event",
            )
        completed = _complete_active(
            checkpoint,
            NativeWorkOutcome(
                kind=NativeWorkOutcomeKind.COMPLETED,
                permit_id=correlation["permit"].permit_id,
                attempt_id=correlation["attempt"].attempt_id,
                probe_ref=correlation["probe_ref"],
            ),
        )
        return (
            completed,
            (
                PlanJournalTransition.PLAN_COMPLETED
                if completed.state is PlanRunState.COMPLETED
                else PlanJournalTransition.PROBE_RESOLVED
            ),
            "Recovered exact authoritative M7 probe finalization",
        )
    raise GovernedPlanError("M8 journal lag has an unrecognized Attempt state")


async def _probe_lag_is_exactly_uncertain(
    session: Any,
    active: GovernedPlanStepRef | None,
) -> bool:
    if active is None:
        return False
    correlation = await _correlate_probe_state(session, active=active, compilation=None)
    return correlation is not None and correlation["outcome"] == ExecutionAttemptStatus.UNKNOWN.value


async def _probe_lag_is_exactly_confirmed(
    session: Any,
    active: GovernedPlanStepRef | None,
) -> bool:
    if active is None:
        return False
    correlation = await _correlate_probe_state(session, active=active, compilation=None)
    return correlation is not None and correlation["outcome"] == ExecutionAttemptStatus.CONFIRMED.value


async def _correlate_probe_state(
    session: Any,
    *,
    active: GovernedPlanStepRef,
    compilation: SyntheticM8Compilation | None,
) -> dict[str, Any] | None:
    task = await _load_task(session, active.native_task_id, lock=True)
    step = (
        await session.scalars(select(StepModel).where(StepModel.step_id == active.native_step_id).with_for_update())
    ).first()
    contract = (
        await session.scalars(
            select(TaskContractModel)
            .where(TaskContractModel.contract_id == active.native_contract_id)
            .with_for_update()
        )
    ).first()
    if task is None and step is None and contract is None:
        return None
    if (
        task is None
        or step is None
        or contract is None
        or task.task_id != active.native_task_id
        or task.application != M7_APPLICATION_MARKER
        or step.task_id != active.native_task_id
        or step.step_id != active.native_step_id
        or step.created_by != M7_APPLICATION_MARKER
        or contract.task_id != active.native_task_id
        or contract.contract_id != active.native_contract_id
        or contract.authorization_snapshot.get("authority_contract_id") != active.authority_contract_id
    ):
        raise GovernedPlanError("M8 journal lag native Task/Step/contract correlation failed")
    attempts = list(
        (
            await session.scalars(
                select(ExecutionAttemptModel)
                .where(
                    ExecutionAttemptModel.task_id == active.native_task_id,
                    ExecutionAttemptModel.step_id == active.native_step_id,
                    ExecutionAttemptModel.contract_id == active.native_contract_id,
                )
                .with_for_update()
            )
        ).all()
    )
    if not attempts:
        if task.status in {TaskStatus.created.value, TaskStatus.running.value} and step.status in {
            StepStatus.created.value,
            StepStatus.running.value,
        }:
            return None
        raise GovernedPlanError("M8 journal lag has native state without an exact Attempt")
    if len(attempts) != 1:
        raise GovernedPlanError("M8 journal lag has multiple correlated Attempts")
    attempt = attempts[0]
    permits = list(
        (
            await session.scalars(
                select(ExecutionPermitModel)
                .where(
                    ExecutionPermitModel.task_id == active.native_task_id,
                    ExecutionPermitModel.step_id == active.native_step_id,
                    ExecutionPermitModel.contract_id == active.native_contract_id,
                    ExecutionPermitModel.action_fingerprint == attempt.action_fingerprint,
                    ExecutionPermitModel.observation_hash == attempt.observation_hash,
                    ExecutionPermitModel.status == "consumed",
                )
                .with_for_update()
            )
        ).all()
    )
    if len(permits) != 1:
        raise GovernedPlanError("M8 journal lag lacks one exact consumed Permit")
    permit = permits[0]
    probe_ref = active.probe_ref
    if compilation is not None:
        child = compilation.child_for(active.work_order_id)
        if (
            child.work_order.task_id != active.native_task_id
            or child.work_order.contract_id != active.native_contract_id
        ):
            raise GovernedPlanError("M8 journal lag Work Order/native identity mismatch")
        probe_ref = child.work_order.result_probe_ref
    if active.permit_id is not None and active.permit_id != permit.permit_id:
        raise GovernedPlanError("M8 journal lag Permit identity mismatch")
    if active.attempt_id is not None and active.attempt_id != attempt.attempt_id:
        raise GovernedPlanError("M8 journal lag Attempt identity mismatch")
    evidence = None
    if attempt.result_probe is not None:
        try:
            evidence = NativeProbeEvidence.model_validate(attempt.result_probe)
        except Exception as exc:
            raise GovernedPlanError("M8 journal lag probe evidence is malformed") from exc
        if (
            evidence.task_id != active.native_task_id
            or evidence.step_id != active.native_step_id
            or evidence.contract_id != active.native_contract_id
            or evidence.permit_id != permit.permit_id
            or evidence.attempt_id != attempt.attempt_id
            or evidence.result_probe_ref != probe_ref
            or evidence.action_fingerprint != attempt.action_fingerprint
            or evidence.observation_hash != attempt.observation_hash
            or evidence.idempotency_key_digest != _digest(attempt.idempotency_key)
        ):
            raise GovernedPlanError("M8 journal lag probe evidence is uncorrelated")
    if attempt.status == ExecutionAttemptStatus.UNKNOWN.value:
        if task.status != TaskStatus.pending_result_probe.value or step.status != StepStatus.pending_result_probe.value:
            raise GovernedPlanError("M8 UNKNOWN Attempt disagrees with native pending-probe state")
        if evidence is not None and evidence.probe_status is not ResultProbeStatus.UNKNOWN:
            raise GovernedPlanError("M8 UNKNOWN Attempt carries a non-inconclusive probe")
    elif attempt.status == ExecutionAttemptStatus.CONFIRMED.value:
        if task.status != TaskStatus.completed.value or step.status != StepStatus.completed.value:
            raise GovernedPlanError("M8 confirmed Attempt disagrees with native completed state")
        if evidence is None or evidence.probe_status is not ResultProbeStatus.CONFIRMED:
            raise GovernedPlanError("M8 confirmed Attempt lacks authoritative final probe evidence")
    else:
        raise GovernedPlanError("M8 journal lag Attempt is not UNKNOWN or CONFIRMED")
    return {
        "attempt": attempt,
        "permit": permit,
        "probe_ref": probe_ref,
        "outcome": attempt.status,
    }


async def _verify_checkpoint_native_state(
    session: Any,
    checkpoint: GovernedPlanCheckpoint,
    *,
    transition: PlanJournalTransition,
) -> None:
    refs = (
        *checkpoint.completed_prefix,
        *((checkpoint.active_step,) if checkpoint.active_step is not None else ()),
        *checkpoint.remaining_suffix,
        *checkpoint.superseded_suffix,
    )
    task_ids = tuple(dict.fromkeys(item.native_task_id for item in refs))
    if not task_ids:
        return
    tasks = {
        item.task_id: item
        for item in (await session.scalars(select(TaskModel).where(TaskModel.task_id.in_(task_ids)))).all()
    }
    steps = {
        item.task_id: item
        for item in (await session.scalars(select(StepModel).where(StepModel.task_id.in_(task_ids)))).all()
    }
    for item in checkpoint.completed_prefix:
        task = tasks.get(item.native_task_id)
        step = steps.get(item.native_task_id)
        if (
            task is None
            or step is None
            or task.status != TaskStatus.completed.value
            or step.status != StepStatus.completed.value
            or step.step_id != item.native_step_id
        ):
            raise GovernedPlanError("M8 completed prefix disagrees with native Task/Step state")
        await _verify_completed_effect_identity(session, item)

    for item in checkpoint.remaining_suffix:
        if item.native_task_id in tasks or item.native_task_id in steps:
            raise GovernedPlanError("M8 non-current suffix child became runnable")

    for item in checkpoint.superseded_suffix:
        task = tasks.get(item.native_task_id)
        step = steps.get(item.native_task_id)
        if task is not None and task.status != TaskStatus.canceled.value:
            raise GovernedPlanError("M8 superseded native Task remains runnable")
        if step is not None and step.status != StepStatus.canceled.value:
            raise GovernedPlanError("M8 superseded native Step remains runnable")

    active = checkpoint.active_step
    if active is None:
        return
    task = tasks.get(active.native_task_id)
    step = steps.get(active.native_task_id)
    must_exist = transition in {
        PlanJournalTransition.CHILD_ACTIVATED,
        PlanJournalTransition.REPLAN_REQUIRED,
        PlanJournalTransition.PROBE_BLOCKED,
        PlanJournalTransition.APPROVAL_REQUIRED,
        PlanJournalTransition.APPROVAL_RESUMED,
        PlanJournalTransition.APPROVAL_REJECTED,
        PlanJournalTransition.RUN_CANCELLED,
    }
    if (task is None) != (step is None) or (must_exist and task is None):
        raise GovernedPlanError("M8 active native Task/Step state is partial or missing")
    if task is None or step is None:
        return
    if step.step_id != active.native_step_id:
        raise GovernedPlanError("M8 active native Step identity disagrees with checkpoint")
    if transition is PlanJournalTransition.PROBE_BLOCKED:
        if (
            task.status != TaskStatus.pending_result_probe.value
            or step.status != StepStatus.pending_result_probe.value
            or not active.attempt_id
            or not active.permit_id
        ):
            raise GovernedPlanError("M8 probe-blocked checkpoint lacks exact native state")
        await _verify_uncertain_effect_identity(session, active)
    elif transition is PlanJournalTransition.APPROVAL_REQUIRED:
        if task.status != TaskStatus.pending_approval.value or step.status != StepStatus.pending_approval.value:
            raise GovernedPlanError("M10 approval checkpoint lacks exact pending native state")
    elif transition is PlanJournalTransition.APPROVAL_RESUMED:
        if task.status != TaskStatus.resuming.value or step.status != StepStatus.resuming.value:
            raise GovernedPlanError("M10 resume checkpoint lacks exact resuming native state")
    elif transition in {PlanJournalTransition.APPROVAL_REJECTED, PlanJournalTransition.RUN_CANCELLED}:
        if task.status != TaskStatus.canceled.value or step.status != StepStatus.canceled.value:
            raise GovernedPlanError("M10 terminal non-effect checkpoint lacks exact cancelled native state")
    elif task.status not in {TaskStatus.created.value, TaskStatus.running.value} or step.status not in {
        StepStatus.created.value,
        StepStatus.running.value,
    }:
        raise GovernedPlanError("M8 active native child is not runnable")


async def _verify_completed_effect_identity(session: Any, item: GovernedPlanStepRef) -> None:
    if item.attempt_id is None and item.permit_id is None:
        return
    if item.attempt_id is None or item.permit_id is None:
        raise GovernedPlanError("M8 completed effect has partial Permit/Attempt identity")
    attempt = (
        await session.scalars(select(ExecutionAttemptModel).where(ExecutionAttemptModel.attempt_id == item.attempt_id))
    ).first()
    permit = (
        await session.scalars(select(ExecutionPermitModel).where(ExecutionPermitModel.permit_id == item.permit_id))
    ).first()
    if (
        attempt is None
        or permit is None
        or attempt.task_id != item.native_task_id
        or attempt.step_id != item.native_step_id
        or attempt.contract_id != item.native_contract_id
        or attempt.status != ExecutionAttemptStatus.CONFIRMED.value
        or permit.task_id != item.native_task_id
        or permit.step_id != item.native_step_id
        or permit.contract_id != item.native_contract_id
        or permit.status != "consumed"
    ):
        raise GovernedPlanError("M8 completed child disagrees with authoritative Permit/Attempt state")


async def _verify_uncertain_effect_identity(session: Any, item: GovernedPlanStepRef) -> None:
    assert item.attempt_id is not None and item.permit_id is not None
    attempt = (
        await session.scalars(select(ExecutionAttemptModel).where(ExecutionAttemptModel.attempt_id == item.attempt_id))
    ).first()
    permit = (
        await session.scalars(select(ExecutionPermitModel).where(ExecutionPermitModel.permit_id == item.permit_id))
    ).first()
    if (
        attempt is None
        or permit is None
        or attempt.task_id != item.native_task_id
        or attempt.step_id != item.native_step_id
        or attempt.contract_id != item.native_contract_id
        or attempt.status != ExecutionAttemptStatus.UNKNOWN.value
        or permit.task_id != item.native_task_id
        or permit.step_id != item.native_step_id
        or permit.contract_id != item.native_contract_id
        or permit.status != "consumed"
    ):
        raise GovernedPlanError("M8 UNKNOWN checkpoint disagrees with authoritative Permit/Attempt state")


async def _finalize_completed_native(session: Any, item: GovernedPlanStepRef) -> None:
    task = await _load_task(session, item.native_task_id, lock=True)
    step = (
        await session.scalars(select(StepModel).where(StepModel.step_id == item.native_step_id).with_for_update())
    ).first()
    if task is None or step is None:
        raise GovernedPlanError("M8 cannot finalize a missing native child")
    if item.attempt_id is not None or item.permit_id is not None:
        await _verify_completed_effect_identity(session, item)
        if task.status != TaskStatus.completed.value or step.status != StepStatus.completed.value:
            raise GovernedPlanError("M8 effectful child was not finalized by authoritative M7 recovery")
        return
    if task.status not in {TaskStatus.running.value, TaskStatus.completed.value}:
        raise GovernedPlanError("M8 read-only child Task is not completable")
    if step.status != StepStatus.completed.value:
        raise GovernedPlanError("M8 read-only child Step did not complete through ForgeAgent")
    task.status = TaskStatus.completed.value
    task.finished_at = datetime.now(timezone.utc)


async def _reconcile_committed_duplicate_side_effects(
    session: Any,
    *,
    checkpoint: GovernedPlanCheckpoint,
    transition: PlanJournalTransition,
    superseded_task_ids: tuple[str, ...],
    replacement_admission: TaskAdmissionBundle | None,
) -> list[str]:
    if replacement_admission is not None:
        _require_checkpoint_admission(checkpoint, replacement_admission)
        admission = (
            await session.scalars(
                select(GovernedTaskAdmissionModel).where(
                    GovernedTaskAdmissionModel.admission_id == checkpoint.admission_id
                )
            )
        ).first()
        if admission is None or TaskAdmissionBundle.model_validate(admission.bundle_payload) != replacement_admission:
            raise GovernedPlanError("M8 committed duplicate admission readback conflicts")
    revoked: list[str] = []
    if superseded_task_ids:
        tasks = list((await session.scalars(select(TaskModel).where(TaskModel.task_id.in_(superseded_task_ids)))).all())
        steps = list((await session.scalars(select(StepModel).where(StepModel.task_id.in_(superseded_task_ids)))).all())
        permits = list(
            (
                await session.scalars(
                    select(ExecutionPermitModel).where(ExecutionPermitModel.task_id.in_(superseded_task_ids))
                )
            ).all()
        )
        if any(item.status != TaskStatus.canceled.value for item in tasks) or any(
            item.status != StepStatus.canceled.value for item in steps
        ):
            raise GovernedPlanError("M8 committed duplicate supersession readback conflicts")
        if any(item.status not in {"revoked", "expired"} for item in permits):
            raise GovernedPlanError("M8 committed duplicate Permit readback conflicts")
        revoked = sorted(item.permit_id for item in permits if item.status == "revoked")
    await _verify_checkpoint_native_state(session, checkpoint, transition=transition)
    return revoked


async def _supersede_unstarted(session: Any, task_ids: tuple[str, ...]) -> list[str]:
    attempts = list(
        (await session.scalars(select(ExecutionAttemptModel).where(ExecutionAttemptModel.task_id.in_(task_ids)))).all()
    )
    if any(
        item.status in {ExecutionAttemptStatus.EXECUTING.value, ExecutionAttemptStatus.UNKNOWN.value}
        for item in attempts
    ):
        raise GovernedPlanError("M8 cannot supersede a suffix with an uncertain Attempt")
    permits = list(
        (
            await session.scalars(
                select(ExecutionPermitModel).where(ExecutionPermitModel.task_id.in_(task_ids)).with_for_update()
            )
        ).all()
    )
    revoked: list[str] = []
    for permit in permits:
        if permit.status == "issued":
            permit.status = "revoked"
            revoked.append(permit.permit_id)
        elif permit.status == "consumed":
            raise GovernedPlanError("M8 cannot supersede a consumed suffix Permit")
    tasks = list((await session.scalars(select(TaskModel).where(TaskModel.task_id.in_(task_ids)))).all())
    steps = list((await session.scalars(select(StepModel).where(StepModel.task_id.in_(task_ids)))).all())
    for task in tasks:
        if task.status in {TaskStatus.created.value, TaskStatus.running.value}:
            task.status = TaskStatus.canceled.value
        elif task.status != TaskStatus.canceled.value:
            raise GovernedPlanError("M8 cannot supersede a terminal native Task")
    for step in steps:
        if step.status in {StepStatus.created.value, StepStatus.running.value}:
            step.status = StepStatus.canceled.value
        elif step.status != StepStatus.canceled.value:
            raise GovernedPlanError("M8 cannot supersede a terminal native Step")
    return sorted(revoked)


async def _load_task(session: Any, task_id: str, *, lock: bool) -> TaskModel | None:
    query = select(TaskModel).where(TaskModel.task_id == task_id)
    if lock:
        query = query.with_for_update()
    return (await session.scalars(query)).first()


def _root_contract(bundle: TaskAdmissionBundle) -> TaskContractModel:
    contract = bundle.contract
    return TaskContractModel(
        contract_id=contract.contract_id,
        task_id=bundle.task.task_id,
        organization_id=contract.organization_id,
        initiator_id=contract.initiator_id,
        service_principal_id=contract.service_principal_id,
        department_id=contract.department_id,
        business_line_id=contract.business_line_id,
        goal=contract.goal,
        allowed_operations=sorted(contract.allowed_operations),
        data_scope=contract.data_scope,
        authorization_snapshot=contract.authorization_snapshot,
        policy_profile=contract.policy_profile,
        policy_version=contract.policy_version,
        success_criteria=contract.success_criteria,
        mode=contract.mode.value,
        version=contract.version,
        expires_at=contract.expires_at,
    )


def _event_model(event: PlanJournalEvent, organization_id: str) -> GovernanceAuditEventModel:
    return GovernanceAuditEventModel(
        event_id=event.event_id,
        task_id=event.root_task_id,
        step_id=None,
        contract_id=event.checkpoint.authority_contract_id,
        organization_id=organization_id,
        event_type=f"m8.plan.{event.transition.value}",
        mode="audit",
        action_fingerprint=None,
        observation_hash=None,
        policy_version=None,
        payload=event.model_dump(mode="json"),
        created_at=event.created_at,
    )


def _authority_digests(compilation: SyntheticM8Compilation) -> dict[str, str]:
    return {
        "installation": compilation.authority.installation.contract_digest,
        "grants": _digest(compilation.authority.grants),
        "authority_contract": _digest(compilation.authority.task_contract),
        "data_scope": _digest(compilation.business_plan.data_scope),
        "plan": _digest(compilation.business_plan),
        "work_orders": _digest(compilation.work_orders),
    }


def _require_checkpoint_admission(
    checkpoint: GovernedPlanCheckpoint,
    admission: TaskAdmissionBundle,
    *,
    match_plan: bool = True,
) -> None:
    if (
        admission.admission_id != checkpoint.admission_id
        or admission.task.task_id != checkpoint.root_task_id
        or admission.contract.contract_id != checkpoint.authority_contract_id
        or (
            match_plan
            and (admission.plan.plan_id != checkpoint.plan_id or admission.plan.version != checkpoint.plan_version)
        )
    ):
        raise GovernedPlanError("M8 checkpoint and admission plan identity disagree")


def _require_checkpoint_compilation(
    checkpoint: GovernedPlanCheckpoint,
    compilation: SyntheticM8Compilation,
) -> None:
    if (
        checkpoint.plan_run_id != compilation.plan_run_id
        or checkpoint.admission_id != compilation.admission_id
        or checkpoint.root_task_id != compilation.business_plan.task_id
        or checkpoint.authority_contract_id != compilation.business_plan.contract_id
        or checkpoint.plan_id != compilation.business_plan.plan_id
        or checkpoint.plan_version != compilation.business_plan.version
    ):
        raise GovernedPlanError("M8 checkpoint does not match the compiled plan identity")
    refs = (
        *checkpoint.completed_prefix,
        *((checkpoint.active_step,) if checkpoint.active_step else ()),
        *checkpoint.remaining_suffix,
    )
    expected = tuple(
        _step_ref(step, order, PlanStepState.PENDING)
        for step, order in zip(compilation.business_plan.steps, compilation.work_orders, strict=True)
    )
    if len(refs) != len(expected) or any(
        _ref_identity_payload(left) != _ref_identity_payload(right) for left, right in zip(refs, expected, strict=True)
    ):
        raise GovernedPlanError("M8 checkpoint child identity mapping disagrees with compilation")


def _ref_identity_payload(ref: GovernedPlanStepRef) -> tuple[str, str, str, str, str, str, str, str]:
    return (
        ref.business_plan_step_id,
        ref.step_digest,
        ref.work_order_id,
        ref.work_order_digest,
        ref.native_task_id,
        ref.native_step_id,
        ref.native_contract_id,
        ref.authority_contract_id,
    )


def _assess_work_order_suffix(
    previous: SyntheticM8Compilation,
    proposed: SyntheticM8Compilation,
    prefix_len: int,
) -> str | None:
    old = previous.work_orders[prefix_len:]
    new = proposed.work_orders[prefix_len:]
    if len(old) != len(new):
        return "Replacement suffix changed Work Order cardinality"
    for old_order, new_order in zip(old, new, strict=True):
        old_payload = _work_order_authority_payload(old_order)
        new_payload = _work_order_authority_payload(new_order)
        if _digest(old_payload) != _digest(new_payload):
            return "Replacement suffix changed Work Order authority, adapter, or probe"
    return None


def _work_order_authority_payload(order: ExecutionWorkOrder) -> dict[str, Any]:
    payload = order.model_dump(
        mode="json",
        exclude={"work_order_id", "business_plan_step_id", "task_id", "contract_id", "navigation_goal"},
    )
    for field in ("allowed_operations", "prohibited_operations"):
        if field in payload:
            payload[field] = sorted(payload[field])
    return payload


def _require_plan_roles(roles: tuple[str, ...]) -> None:
    if not 2 <= len(roles) <= 4:
        raise ValueError("M8 trusted step roles require two to four sequential steps")
    if any(role not in {"precheck", "submit", "confirm"} for role in roles):
        raise ValueError("M8 trusted step roles contain an unsupported role")
    if roles.count("submit") != 1 or roles[-1] != "confirm":
        raise ValueError("M8 trusted step roles require exactly one submit and terminal confirm")
    if any(role != "precheck" for role in roles[: roles.index("submit")]):
        raise ValueError("M8 trusted step roles before submit must be prechecks")
    if any(role != "confirm" for role in roles[roles.index("submit") + 1 :]):
        raise ValueError("M8 trusted step roles after submit must be terminal confirmation")


def _work_order_role(order: ExecutionWorkOrder) -> str:
    marker = "M8 governed "
    suffix = " for the admitted synthetic payment"
    if not order.navigation_goal.startswith(marker) or not order.navigation_goal.endswith(suffix):
        raise ValueError("M8 Work Order does not expose a trusted finite step role")
    return order.navigation_goal[len(marker) : -len(suffix)]


def _trace_ref(item: GovernedPlanStepRef) -> dict[str, Any]:
    return {
        "business_plan_step_id": item.business_plan_step_id,
        "step_digest": item.step_digest,
        "work_order_id": item.work_order_id,
        "work_order_digest": item.work_order_digest,
        "native_task_id": item.native_task_id,
        "native_step_id": item.native_step_id,
        "native_contract_id": item.native_contract_id,
        "authority_contract_id": item.authority_contract_id,
        "state": item.state.value,
        "permit_id": item.permit_id,
        "attempt_id": item.attempt_id,
        "probe_ref": item.probe_ref,
    }


def _digest(value: Any) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    elif isinstance(value, tuple) and all(isinstance(item, BaseModel) for item in value):
        value = [item.model_dump(mode="json") for item in value]
    canonical = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _stable_id(seed: str, kind: str) -> str:
    return _generic_stable_id(seed, kind)
