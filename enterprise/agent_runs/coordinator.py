"""Pack-neutral translation of typed runtime outcomes into the Agent Run journal."""

from __future__ import annotations

from enterprise.governance.pack_runtime import PackAdvanceResult, PackAdvanceStatus, PackProbeResult, PackProbeStatus

from .journal import GovernedPlanCheckpoint, GovernedPlanError, PlanJournalTransition, PlanRunState, PlanStepState


class AgentRunResultCoordinator:
    """Translate Pack lifecycle envelopes without interpreting business facts."""

    @staticmethod
    def advance(
        checkpoint: GovernedPlanCheckpoint,
        result: PackAdvanceResult,
    ) -> tuple[GovernedPlanCheckpoint, PlanJournalTransition] | None:
        if result.run_id != checkpoint.root_task_id:
            raise GovernedPlanError("Pack advance result does not match the Agent Run root")
        active = checkpoint.active_step
        if result.status is PackAdvanceStatus.COMPLETED:
            if checkpoint.state is PlanRunState.COMPLETED:
                return None
            if active is None:
                raise GovernedPlanError("Completed Pack result has no active Agent Run step")
            completed = active.model_copy(update={"state": PlanStepState.COMPLETED})
            remaining = checkpoint.remaining_suffix
            next_active = remaining[0].model_copy(update={"state": PlanStepState.ACTIVE}) if remaining else None
            updated = checkpoint.model_copy(
                update={
                    "completed_prefix": (*checkpoint.completed_prefix, completed),
                    "active_step": next_active,
                    "remaining_suffix": remaining[1:] if remaining else (),
                    "state": PlanRunState.ACTIVE if next_active else PlanRunState.COMPLETED,
                }
            )
            transition = PlanJournalTransition.CHILD_COMPLETED if next_active else PlanJournalTransition.PLAN_COMPLETED
            return updated, transition
        if result.status is PackAdvanceStatus.PENDING_RESULT_PROBE:
            if active is None or result.execution_checkpoint is None:
                raise GovernedPlanError("Pending Pack result has no active execution checkpoint")
            execution = result.execution_checkpoint
            if (
                execution.task_id != active.native_task_id
                or execution.step_id != active.native_step_id
                or (active.permit_id is not None and execution.permit_id != active.permit_id)
                or (active.attempt_id is not None and execution.attempt_id != active.attempt_id)
            ):
                raise GovernedPlanError("Pack execution checkpoint does not match the active Agent Run step")
            if checkpoint.state is PlanRunState.PROBE_BLOCKED and active.attempt_id == execution.attempt_id:
                return None
            updated = checkpoint.model_copy(
                update={
                    "state": PlanRunState.PROBE_BLOCKED,
                    "active_step": active.model_copy(
                        update={
                            "state": PlanStepState.PROBE_BLOCKED,
                            "permit_id": execution.permit_id,
                            "attempt_id": execution.attempt_id,
                            "probe_ref": execution.result_probe_ref,
                        }
                    ),
                }
            )
            return updated, PlanJournalTransition.PROBE_BLOCKED
        return None

    @staticmethod
    def probe(
        checkpoint: GovernedPlanCheckpoint,
        result: PackProbeResult,
    ) -> tuple[GovernedPlanCheckpoint, PlanJournalTransition] | None:
        active = checkpoint.active_step
        if checkpoint.state is not PlanRunState.PROBE_BLOCKED or active is None:
            raise GovernedPlanError("Probe result requires a probe-blocked Agent Run step")
        execution = result.checkpoint
        if (
            execution.task_id != active.native_task_id
            or execution.step_id != active.native_step_id
            or execution.permit_id != active.permit_id
            or execution.attempt_id != active.attempt_id
        ):
            raise GovernedPlanError("Pack probe checkpoint does not match the blocked Agent Run step")
        if result.status is PackProbeStatus.INCONCLUSIVE:
            return None
        if result.status is not PackProbeStatus.CONFIRMED:
            raise GovernedPlanError("Pack probe did not confirm the blocked Agent Run step")
        completed = active.model_copy(update={"state": PlanStepState.COMPLETED})
        remaining = checkpoint.remaining_suffix
        next_active = remaining[0].model_copy(update={"state": PlanStepState.ACTIVE}) if remaining else None
        updated = checkpoint.model_copy(
            update={
                "completed_prefix": (*checkpoint.completed_prefix, completed),
                "active_step": next_active,
                "remaining_suffix": remaining[1:] if remaining else (),
                "state": PlanRunState.ACTIVE if next_active else PlanRunState.COMPLETED,
            }
        )
        return updated, PlanJournalTransition.PROBE_RESOLVED if next_active else PlanJournalTransition.PLAN_COMPLETED
