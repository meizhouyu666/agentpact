"""Focused M8 governed sequential loop, journal, identity, and Replan tests."""

# ruff: noqa: E402, F401, I001

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from enterprise.agent.work_orders import ReplanReason
from tests.fixtures.synthetic_payment_runtime.m8_runtime import (
    GovernedPlanError,
    GovernedPlanCheckpoint,
    PlanJournalTransition,
    PlanRunState,
    _complete_active,
    _event,
    _replay,
    build_m8_admission_bundle,
    build_replacement_suffix,
    build_redacted_m8_trace,
    build_synthetic_m8_compilation,
    initial_checkpoint,
)
from tests.fixtures.synthetic_payment_runtime.m7_runtime import NativeSkyvernBinding
from tests.fixtures.synthetic_payment_runtime.m6_runtime import SyntheticM6Compilation
from enterprise.governance.contracts import ExecutionAttemptStatus
from enterprise.governance.execution_profiles import ExecutionMechanism, ExecutionProfile
from enterprise.governance.contracts import ExecutionAuthorization, ExecutionEffect
from enterprise.governance.result_probes import ResultProbeStatus
from tests.unit.test_m7_native_runtime import _compiled_bundle


def _m8():
    authority, bundle = _compiled_bundle()
    compilation = build_synthetic_m8_compilation(
        authority,
        admission_id=bundle.admission_id,
        plan_run_id="m8-plan-run-001",
    )
    m8_bundle = build_m8_admission_bundle(bundle, compilation)
    return compilation, m8_bundle


class _MemoryJournal:
    async def initialize(self, *, compilation, admission_bundle, target_url):
        del admission_bundle, target_url
        return initial_checkpoint(compilation).model_copy(update={"journal_sequence": 1, "journal_digest": "d" * 64})

    async def append(self, *, checkpoint, superseded_task_ids=(), **kwargs):
        del kwargs
        digest = f"{checkpoint.journal_sequence + 1:064x}"
        return checkpoint.model_copy(
            update={"journal_sequence": checkpoint.journal_sequence + 1, "journal_digest": digest}
        ), tuple(f"permit-{index}" for index, _ in enumerate(superseded_task_ids))


def test_m8_compilation_maps_root_and_three_distinct_child_identities():
    compilation, bundle = _m8()
    assert len(compilation.business_plan.steps) == 3
    assert len(compilation.work_orders) == 3
    assert all(order.plan_task_id == compilation.business_plan.task_id for order in compilation.work_orders)
    assert all(order.authority_contract_id == compilation.business_plan.contract_id for order in compilation.work_orders)
    assert len({order.task_id for order in compilation.work_orders}) == 3
    assert len({order.contract_id for order in compilation.work_orders}) == 3
    assert bundle.plan == compilation.business_plan


def test_m8_replacement_changes_only_unexecuted_suffix_and_keeps_prefix_digest():
    compilation, bundle = _m8()
    replacement = build_replacement_suffix(compilation, completed_prefix_length=1)
    assert replacement.business_plan.version == 2
    assert replacement.business_plan.steps[0] == compilation.business_plan.steps[0]
    assert replacement.business_plan.steps[1].step_id != compilation.business_plan.steps[1].step_id
    assert replacement.business_plan.steps[2].step_id != compilation.business_plan.steps[2].step_id
    assert replacement.business_plan.steps[1].inputs == compilation.business_plan.steps[1].inputs


def test_m8_journal_replay_rejects_gap_chain_corruption_and_terminal_followup():
    compilation, _bundle = _m8()
    checkpoint = initial_checkpoint(compilation)
    event = _event(
        checkpoint=checkpoint,
        transition="admitted",
        authority_digests={"plan": "a" * 64},
        created_at=datetime.now(timezone.utc),
    )
    restored = _replay([event])
    assert restored.journal_sequence == 1
    assert restored.journal_digest == event.event_digest

    with pytest.raises(GovernedPlanError, match="sequence gap"):
        _replay([event.model_copy(update={"sequence": 2})])

    with pytest.raises(GovernedPlanError, match="digest chain"):
        _replay([event.model_copy(update={"previous_event_digest": "b" * 64})])


def test_m8_journal_duplicate_is_deterministic_and_conflicting_branch_is_rejected():
    compilation, _bundle = _m8()
    checkpoint = initial_checkpoint(compilation)
    created_at = datetime.now(timezone.utc)
    authority = {"plan": "a" * 64}
    admitted = _event(
        checkpoint=checkpoint,
        transition=PlanJournalTransition.ADMITTED,
        authority_digests=authority,
        created_at=created_at,
    )
    duplicate = _event(
        checkpoint=checkpoint,
        transition=PlanJournalTransition.ADMITTED,
        authority_digests=authority,
        created_at=created_at,
    )
    assert duplicate == admitted

    restored = _replay([admitted])
    first_branch = _event(
        checkpoint=restored,
        transition=PlanJournalTransition.CHILD_ACTIVATED,
        authority_digests=authority,
        created_at=created_at,
        reason="coordinator-a",
    )
    conflicting_branch = _event(
        checkpoint=restored,
        transition=PlanJournalTransition.CHILD_ACTIVATED,
        authority_digests=authority,
        created_at=created_at,
        reason="coordinator-b",
    )
    assert first_branch.event_id == conflicting_branch.event_id
    assert first_branch.event_digest != conflicting_branch.event_digest
    with pytest.raises(GovernedPlanError, match="sequence gap or reorder"):
        _replay([admitted, first_branch, conflicting_branch])


def test_m8_journal_rejects_reorder_corruption_and_transition_after_terminal():
    compilation, _bundle = _m8()
    checkpoint = initial_checkpoint(compilation)
    created_at = datetime.now(timezone.utc)
    authority = {"plan": "a" * 64}
    admitted = _event(
        checkpoint=checkpoint,
        transition=PlanJournalTransition.ADMITTED,
        authority_digests=authority,
        created_at=created_at,
    )
    restored = _replay([admitted])
    activated = _event(
        checkpoint=restored,
        transition=PlanJournalTransition.CHILD_ACTIVATED,
        authority_digests=authority,
        created_at=created_at,
    )
    with pytest.raises(GovernedPlanError, match="sequence gap or reorder"):
        _replay([activated, admitted])
    with pytest.raises(GovernedPlanError, match="digest is corrupt"):
        _replay([admitted.model_copy(update={"event_digest": "f" * 64})])

    completed = restored
    for _ in range(3):
        completed = _complete_active(completed, __import__(
            "tests.fixtures.synthetic_payment_runtime.m8_runtime", fromlist=["NativeWorkOutcome"]
        ).NativeWorkOutcome(kind="completed"))
    terminal = _event(
        checkpoint=completed,
        transition=PlanJournalTransition.PLAN_COMPLETED,
        authority_digests=authority,
        created_at=created_at,
    )
    terminal_restored = _replay([admitted, terminal])
    illegal_active = terminal_restored.model_copy(
        update={"state": PlanRunState.ACTIVE, "active_step": checkpoint.active_step}
    )
    followup = _event(
        checkpoint=illegal_active,
        transition=PlanJournalTransition.CHILD_ACTIVATED,
        authority_digests=authority,
        created_at=created_at,
    )
    with pytest.raises(GovernedPlanError, match="after terminal"):
        _replay([admitted, terminal, followup])


@pytest.mark.asyncio
async def test_m8_replan_accepts_l3_suffix_and_revokes_only_superseded_suffix():
    compilation, bundle = _m8()
    checkpoint = initial_checkpoint(compilation)
    checkpoint = _complete_active(checkpoint, __import__(
        "tests.fixtures.synthetic_payment_runtime.m8_runtime", fromlist=["NativeWorkOutcome"]
    ).NativeWorkOutcome(kind="completed"))
    checkpoint = checkpoint.model_copy(update={"state": PlanRunState.REPLAN_REQUIRED, "journal_sequence": 1, "journal_digest": "d" * 64})
    replacement = build_replacement_suffix(compilation, completed_prefix_length=1)
    replacement_bundle = build_m8_admission_bundle(bundle, replacement)
    from tests.fixtures.synthetic_payment_runtime.m8_runtime import GovernedPlanCoordinator

    coordinator = GovernedPlanCoordinator(_MemoryJournal(), adapter_factory=lambda *_: None, runner=None)
    receipt = await coordinator.apply_replan(
        previous=compilation,
        proposed=replacement,
        checkpoint=checkpoint,
        admission_bundle=replacement_bundle,
    )
    assert receipt.disposition.value == "accepted"
    assert receipt.accepted_plan_version == 2
    assert receipt.invalidated_work_order_ids == tuple(item.work_order_id for item in compilation.work_orders[1:])
    assert receipt.checkpoint.replan_count == 1


@pytest.mark.asyncio
async def test_m8_replan_expansion_fails_closed_at_l4():
    compilation, bundle = _m8()
    checkpoint = initial_checkpoint(compilation).model_copy(
        update={"state": PlanRunState.REPLAN_REQUIRED, "journal_sequence": 1, "journal_digest": "d" * 64}
    )
    replacement = build_replacement_suffix(compilation, completed_prefix_length=0)
    replacement.business_plan.steps[1].inputs = {"amount": "forged"}
    replacement_bundle = build_m8_admission_bundle(bundle, replacement)
    from tests.fixtures.synthetic_payment_runtime.m8_runtime import GovernedPlanCoordinator

    coordinator = GovernedPlanCoordinator(_MemoryJournal(), adapter_factory=lambda *_: None, runner=None)
    receipt = await coordinator.apply_replan(
        previous=compilation,
        proposed=replacement,
        checkpoint=checkpoint,
        admission_bundle=replacement_bundle,
    )
    assert receipt.disposition.value == "reauthorization_required"
    assert receipt.checkpoint.state is PlanRunState.REAUTHORIZATION_REQUIRED


def test_m8_trace_is_redacted_and_contains_root_prefix_and_suffix_identity():
    compilation, _bundle = _m8()
    checkpoint = initial_checkpoint(compilation)
    trace = build_redacted_m8_trace(checkpoint)
    serialized = str(trace)
    assert compilation.business_plan.task_id in serialized
    assert compilation.work_orders[0].task_id in serialized
    assert "payment_id" not in serialized
    assert "amount" not in serialized
