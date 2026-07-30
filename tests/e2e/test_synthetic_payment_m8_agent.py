"""M8 governed sequential Replan proof over PostgreSQL and real Chromium."""

# ruff: noqa: E402, F401, I001

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select

from tests.e2e import m4_synthetic_support as support
from tests.e2e.test_synthetic_payment_native_agent import (
    INPUTS,
    ORGANIZATION_ID,
    REQUEST_ID,
    _BrowserInputObserver,
    _PostgresPermitAuthorizer,
    _click,
    _compile_admission,
    _select_fault,
)

from enterprise.domains.synthetic_payment.m7_runtime import (
    NativeSkyvernBinding,
    NativeSkyvernWorkOrderAdapter,
    SqlAlchemyNativePublicationRepository,
    SyntheticNativeActionContextResolver,
    build_native_probe_evidence,
)
from enterprise.domains.synthetic_payment.m8_runtime import (
    GovernedPlanError,
    GovernedPlanCoordinator,
    NativeWorkOutcome,
    NativeWorkOutcomeKind,
    PlanJournalTransition,
    PlanRunState,
    SqlAlchemyGovernedPlanJournal,
    _authority_digests,
    build_m8_admission_bundle,
    build_replacement_suffix,
    build_synthetic_m8_compilation,
)
from enterprise.domains.synthetic_payment.m9_runtime import (
    M9PlannerDisposition,
    M9PlannerEngine,
    M9ReplanPreconditions,
    M9StepRole,
    PlanProposal,
    RecordedM9Provider,
    SuffixReplanProposal,
    build_m9_plan_input,
    build_m9_replan_input,
    compile_m9_plan,
    compile_m9_replan,
    redact_replan_evidence,
)
from enterprise.governance.contracts import ExecutionAttemptStatus, ExecutionAuthorization, ExecutionEffect
from enterprise.governance.models import (
    ExecutionAttemptModel,
    ExecutionPermitModel,
    GovernanceAuditEventModel,
    GovernedTaskAdmissionModel,
)
from enterprise.governance.result_probes import ResultProbeEvidence, ResultProbeStatus
from skyvern.forge.agent import ForgeAgent
from skyvern.forge.sdk.db.models import OrganizationModel, StepModel, TaskModel
from skyvern.forge.sdk.models import StepStatus
from skyvern.forge.sdk.schemas.tasks import TaskStatus

RUN_ID = "agentpact-m8-governed-replan-e2e"
PLAN_RUN_ID = "m8-governed-replan-e2e-plan"
M9_PLAN_RUN_ID = "m9-model-governed-replan-e2e-plan"


async def _persist_m8_admission(
    database: support.M4Database,
    admission: Any,
    planning_time: datetime,
) -> None:
    async with database.Session() as session:
        session.add(
            OrganizationModel(
                organization_id=ORGANIZATION_ID,
                organization_name="FinRPA M8 Journal Recovery Tests",
            )
        )
        session.add(
            GovernedTaskAdmissionModel(
                admission_id=admission.admission_id,
                organization_id=ORGANIZATION_ID,
                request_id=REQUEST_ID,
                task_id=admission.task.task_id,
                contract_id=admission.contract.contract_id,
                bundle_schema_version=admission.schema_version,
                admission_fingerprint="m8-recovery-admission-e2e",
                bundle_fingerprint="m8-recovery-bundle-e2e",
                bundle_payload=admission.model_dump(mode="json"),
                mode="audit",
                committed_at=planning_time,
            )
        )
        await session.commit()


async def _seed_m8_activated_child(
    database: support.M4Database,
    compilation: Any,
    admission: Any,
    target_url: str,
    journal: SqlAlchemyGovernedPlanJournal,
) -> tuple[Any, NativeSkyvernBinding]:
    checkpoint = await journal.initialize(
        compilation=compilation,
        admission_bundle=admission,
        target_url=target_url,
    )
    child = compilation.child_compilations[0]
    adapter = NativeSkyvernWorkOrderAdapter(
        SqlAlchemyNativePublicationRepository(database.Session),
        compilation=child,
        admission_bundle=admission,
        target_url=target_url,
        navigation_payload=dict(INPUTS),
    )
    binding = await adapter.prepare(child.work_order)
    activated, _ = await journal.append(
        checkpoint=checkpoint,
        transition=PlanJournalTransition.CHILD_ACTIVATED,
        authority_digests=_authority_digests(compilation),
    )
    return activated, binding


class _M8NativeRunner:
    def __init__(self, database: support.M4Database, browser: Any, environment: Any) -> None:
        self._database = database
        self._browser = browser
        self._environment = environment
        self.entered_forge: list[str] = []
        self.mismatched_native_task_id: str | None = None
        self.execute_urls: list[str] = []
        self.challenge_id: str | None = None
        self.payment_id: str | None = None

    async def execute(self, *, compilation: Any, binding: NativeSkyvernBinding) -> NativeWorkOutcome:
        role = compilation.work_order.navigation_goal.rsplit(" ", 1)[-1]
        if role == "payment":
            role = compilation.work_order.navigation_goal.split(" governed ", 1)[1].split(" for ", 1)[0]
        if role == "submit" and compilation.business_plan.version == 1:
            self.mismatched_native_task_id = binding.native_task_id
            return NativeWorkOutcome(
                kind=NativeWorkOutcomeKind.BUSINESS_STATE_MISMATCH,
                evidence_refs=("agentpact://m8/evidence/business-state-mismatch",),
                message="Synthetic page state changed before the original submit suffix",
            )
        if role == "submit":
            return await self._execute_submit(compilation=compilation, binding=binding)
        return await self._execute_read(compilation=compilation, binding=binding)

    async def _execute_read(self, *, compilation: Any, binding: NativeSkyvernBinding) -> NativeWorkOutcome:
        task, step = await self._native_rows(binding)

        async def deterministic_complete(**_kwargs: Any) -> dict[str, Any]:
            return {
                "actions": [
                    {
                        "action_type": "COMPLETE",
                        "reasoning": "Complete the admitted read-only M8 child",
                    }
                ]
            }

        resolver = SyntheticNativeActionContextResolver(
            self._database.Session,
            binding=binding,
            compilation=compilation,
            authorizer=_PostgresPermitAuthorizer(self._database),
            business_input_observer=_BrowserInputObserver(self._browser.page),
            hmac_secret=support.HMAC_SECRET,
        )
        with support.configured_native_forge_boundary(
            self._database,
            self._browser.state,
            organization_id=ORGANIZATION_ID,
            task_id=binding.native_task_id,
            step_id=binding.native_step_id,
            run_id=RUN_ID,
            llm_api_handler=deterministic_complete,
        ):
            completed_step, _output = await ForgeAgent(resolver).agent_step(
                task=task,
                step=step,
                browser_state=self._browser.state,
                complete_verification=False,
            )
        self.entered_forge.append(binding.native_task_id)
        assert completed_step.status.value == "completed"
        return NativeWorkOutcome(kind=NativeWorkOutcomeKind.COMPLETED)

    async def _execute_submit(self, *, compilation: Any, binding: NativeSkyvernBinding) -> NativeWorkOutcome:
        task, step = await self._native_rows(binding)
        await self._prepare_approved_challenge(task=task, step=step, contract_id=binding.contract_id)
        with support.configured_forge_boundary(self._database, self._browser.state):
            execute_observation = await support.scrape_current_page(self._browser)
            execute_element_id = support.element_id_by_aria_label(
                execute_observation,
                "Execute synthetic payment once",
            )

        async def deterministic_submit(*, prompt: str, **_kwargs: Any) -> dict[str, Any]:
            assert execute_element_id in prompt
            return {
                "actions": [
                    {
                        "action_type": "CLICK",
                        "id": execute_element_id,
                        "reasoning": "Execute the admitted replacement submit exactly once",
                    }
                ]
            }

        authorizer = _PostgresPermitAuthorizer(self._database)
        resolver = SyntheticNativeActionContextResolver(
            self._database.Session,
            binding=binding,
            compilation=compilation,
            authorizer=authorizer,
            business_input_observer=_BrowserInputObserver(self._browser.page),
            hmac_secret=support.HMAC_SECRET,
        )
        observed_statuses: list[str | None] = []
        idempotency_key = f"synthetic:{INPUTS['payment_id']}"
        await support.install_execute_order_probe(
            page=self._browser.page,
            database=self._database,
            idempotency_key=idempotency_key,
            observed_statuses=observed_statuses,
            observed_urls=self.execute_urls,
            task_id=binding.native_task_id,
        )
        with support.configured_native_forge_boundary(
            self._database,
            self._browser.state,
            organization_id=ORGANIZATION_ID,
            task_id=binding.native_task_id,
            step_id=binding.native_step_id,
            run_id=RUN_ID,
            llm_api_handler=deterministic_submit,
        ):
            await ForgeAgent(resolver).agent_step(
                task=task,
                step=step,
                browser_state=self._browser.state,
                complete_verification=False,
            )
        self.entered_forge.append(binding.native_task_id)
        assert observed_statuses == [ExecutionAttemptStatus.EXECUTING.value]
        assert len(self.execute_urls) == 1

        attempt = await support.execution_attempt(
            self._database,
            idempotency_key,
            task_id=binding.native_task_id,
        )
        authorization = authorizer.authorization
        assert authorization is not None
        assert attempt.status == ExecutionAttemptStatus.UNKNOWN.value
        assert self.challenge_id is not None
        inconclusive_payload = support.http_json(
            f"{self._environment.console_url.rstrip('/')}/api/challenges/{self.challenge_id}/probe",
            payload={},
        )
        inconclusive = await resolver.reconcile_probe(
            evidence=build_native_probe_evidence(
                binding=binding,
                authorization=authorization,
                attempt_id=attempt.attempt_id,
                result_probe=ResultProbeEvidence.model_validate(inconclusive_payload["result_probe"]),
                hmac_secret=support.HMAC_SECRET,
            )
        )
        assert inconclusive.attempt_status is ExecutionAttemptStatus.UNKNOWN
        assert self.payment_id is not None
        support.http_json(
            f"{self._environment.console_url.rstrip('/')}/api/payments/{self.payment_id}/clear-probe-fault",
            payload={},
        )
        probe_payload = support.http_json(
            f"{self._environment.console_url.rstrip('/')}/api/challenges/{self.challenge_id}/probe",
            payload={},
        )
        probe = await resolver.reconcile_probe(
            evidence=build_native_probe_evidence(
                binding=binding,
                authorization=authorization,
                attempt_id=attempt.attempt_id,
                result_probe=ResultProbeEvidence.model_validate(probe_payload["result_probe"]),
                hmac_secret=support.HMAC_SECRET,
            )
        )
        assert probe.attempt_status is ExecutionAttemptStatus.CONFIRMED
        return NativeWorkOutcome(
            kind=NativeWorkOutcomeKind.COMPLETED,
            permit_id=authorization.permit_id,
            attempt_id=attempt.attempt_id,
            probe_ref=probe.result_probe_ref,
        )

    async def _prepare_approved_challenge(self, *, task: Any, step: Any, contract_id: str) -> None:
        if self.challenge_id is not None:
            return
        governance = support.SeededGovernanceContext(task=task, step=step, contract_id=contract_id)
        with support.configured_forge_boundary(self._database, self._browser.state):
            create_observation = await support.scrape_current_page(self._browser)
            await support.run_handler_action(
                browser=self._browser,
                governance=governance,
                scraped_page=create_observation,
                action=_click(
                    task,
                    step,
                    support.element_id_by_aria_label(
                        create_observation,
                        "Create synthetic payment challenge",
                    ),
                    0,
                    "Create the isolated M8 payment challenge",
                ),
            )
            await self._browser.page.wait_for_function(
                "document.getElementById('state').textContent === 'pending_approval'",
                timeout=10_000,
            )
            self.challenge_id = (await self._browser.page.text_content("#challenge") or "").strip()
            prepared = support.http_json(
                f"{self._environment.console_url.rstrip('/')}/api/challenges/{self.challenge_id}"
            )
            self.payment_id = prepared["facts"]["payment_id"]
            approval_observation = await support.scrape_current_page(self._browser)
            await support.run_handler_action(
                browser=self._browser,
                governance=governance,
                scraped_page=approval_observation,
                action=_click(
                    task,
                    step,
                    support.element_id_by_aria_label(approval_observation, "Approve synthetic payment"),
                    1,
                    "Approve the isolated M8 payment challenge",
                ),
            )
            await self._browser.page.wait_for_function(
                "document.getElementById('state').textContent === 'ready'",
                timeout=10_000,
            )
            fault_observation = await support.scrape_current_page(self._browser)
            await support.run_handler_action(
                browser=self._browser,
                governance=governance,
                scraped_page=fault_observation,
                action=_select_fault(
                    task,
                    step,
                    support.element_id_by_aria_label(
                        fault_observation,
                        "Synthetic execution fault mode",
                    ),
                ),
            )

    async def _native_rows(self, binding: NativeSkyvernBinding) -> tuple[Any, Any]:
        task = await self._database.get_task(binding.native_task_id, ORGANIZATION_ID)
        step = await self._database.get_step(binding.native_step_id, ORGANIZATION_ID)
        assert task is not None and step is not None
        return task, step


@pytest.mark.e2e
def test_m8_journal_reads_back_exact_committed_duplicate_and_rejects_conflict() -> None:
    planning_time = datetime.now(timezone.utc)
    authority, original_admission = _compile_admission(planning_time)
    compilation = build_synthetic_m8_compilation(
        authority,
        admission_id=original_admission.admission_id,
        plan_run_id="m8-journal-duplicate-e2e",
    )
    admission = build_m8_admission_bundle(original_admission, compilation)

    with support.isolated_m4_environment() as environment:
        database = support.M4Database(environment.database_url)

        async def scenario() -> None:
            try:
                await _persist_m8_admission(database, admission, planning_time)
                journal = SqlAlchemyGovernedPlanJournal(database.Session)
                checkpoint = await journal.initialize(
                    compilation=compilation,
                    admission_bundle=admission,
                    target_url=environment.console_url,
                )
                child = compilation.child_compilations[0]
                adapter = NativeSkyvernWorkOrderAdapter(
                    SqlAlchemyNativePublicationRepository(database.Session),
                    compilation=child,
                    admission_bundle=admission,
                    target_url=environment.console_url,
                    navigation_payload=dict(INPUTS),
                )
                await adapter.prepare(child.work_order)
                committed, _ = await journal.append(
                    checkpoint=checkpoint,
                    transition=PlanJournalTransition.CHILD_ACTIVATED,
                    authority_digests=_authority_digests(compilation),
                )
                recovered, _ = await journal.append(
                    checkpoint=checkpoint,
                    transition=PlanJournalTransition.CHILD_ACTIVATED,
                    authority_digests=_authority_digests(compilation),
                )
                assert recovered == committed
                with pytest.raises(GovernedPlanError, match="one-event-ahead append conflicts"):
                    await journal.append(
                        checkpoint=checkpoint,
                        transition=PlanJournalTransition.CHILD_ACTIVATED,
                        authority_digests=_authority_digests(compilation),
                        reason="conflicting reply-loss retry",
                    )
                async with database.Session() as session:
                    events = list(
                        (
                            await session.scalars(
                                select(GovernanceAuditEventModel).where(
                                    GovernanceAuditEventModel.task_id == compilation.business_plan.task_id,
                                    GovernanceAuditEventModel.event_type.like("m8.plan.%"),
                                )
                            )
                        ).all()
                    )
                assert len(events) == 2
            finally:
                await database.engine.dispose()

        asyncio.run(scenario())


@pytest.mark.e2e
def test_m8_resume_repairs_unknown_and_probe_finalized_journal_lag() -> None:
    planning_time = datetime.now(timezone.utc)
    authority, original_admission = _compile_admission(planning_time)
    compilation = build_synthetic_m8_compilation(
        authority,
        admission_id=original_admission.admission_id,
        plan_run_id="m8-journal-probe-lag-e2e",
    )
    admission = build_m8_admission_bundle(original_admission, compilation)

    with support.isolated_m4_environment() as environment:
        database = support.M4Database(environment.database_url)

        async def scenario() -> None:
            try:
                await _persist_m8_admission(database, admission, planning_time)
                journal = SqlAlchemyGovernedPlanJournal(database.Session)
                activated, binding = await _seed_m8_activated_child(
                    database,
                    compilation,
                    admission,
                    environment.console_url,
                    journal,
                )
                permit_id = "permit-m8-journal-lag"
                attempt_id = "attempt-m8-journal-lag"
                action_fingerprint = "a" * 64
                observation_hash = "b" * 64
                idempotency_key = f"synthetic:{INPUTS['payment_id']}"
                async with database.Session() as session:
                    task = await session.get(TaskModel, binding.native_task_id)
                    step = await session.get(StepModel, binding.native_step_id)
                    assert task is not None and step is not None
                    task.status = TaskStatus.pending_result_probe.value
                    step.status = StepStatus.pending_result_probe.value
                    session.add(
                        ExecutionPermitModel(
                            permit_id=permit_id,
                            task_id=binding.native_task_id,
                            step_id=binding.native_step_id,
                            contract_id=binding.contract_id,
                            action_fingerprint=action_fingerprint,
                            observation_hash=observation_hash,
                            policy_decision_id="decision-m8-journal-lag",
                            decision_payload={"outcome": "allow"},
                            status="consumed",
                            issued_at=planning_time,
                            expires_at=planning_time + timedelta(minutes=10),
                            used_at=planning_time,
                        )
                    )
                    session.add(
                        ExecutionAttemptModel(
                            attempt_id=attempt_id,
                            task_id=binding.native_task_id,
                            step_id=binding.native_step_id,
                            contract_id=binding.contract_id,
                            action_fingerprint=action_fingerprint,
                            observation_hash=observation_hash,
                            status=ExecutionAttemptStatus.UNKNOWN.value,
                            idempotency_key=idempotency_key,
                            started_at=planning_time,
                        )
                    )
                    await session.commit()

                blocked = await journal.initialize(
                    compilation=compilation,
                    admission_bundle=admission,
                    target_url=environment.console_url,
                )
                assert blocked.state is PlanRunState.PROBE_BLOCKED
                assert blocked.journal_sequence == activated.journal_sequence + 1
                assert blocked.active_step is not None
                assert blocked.active_step.permit_id == permit_id
                assert blocked.active_step.attempt_id == attempt_id
                assert blocked.active_step.probe_ref == compilation.work_orders[0].result_probe_ref

                async with database.Session() as session:
                    attempt = await session.get(ExecutionAttemptModel, attempt_id)
                    assert attempt is not None
                    attempt.action_fingerprint = "d" * 64
                    await session.commit()
                with pytest.raises(GovernedPlanError, match="exact consumed Permit"):
                    await journal.initialize(
                        compilation=compilation,
                        admission_bundle=admission,
                        target_url=environment.console_url,
                    )
                async with database.Session() as session:
                    attempt = await session.get(ExecutionAttemptModel, attempt_id)
                    assert attempt is not None
                    attempt.action_fingerprint = action_fingerprint
                    await session.commit()

                authorization = ExecutionAuthorization(
                    permit_id=permit_id,
                    action_fingerprint=action_fingerprint,
                    observation_hash=observation_hash,
                    idempotency_key=idempotency_key,
                    effect=ExecutionEffect.EXTERNAL_WRITE,
                )
                evidence = build_native_probe_evidence(
                    binding=binding,
                    authorization=authorization,
                    attempt_id=attempt_id,
                    result_probe=ResultProbeEvidence(
                        probe_ref=binding.result_probe_ref,
                        status=ResultProbeStatus.CONFIRMED,
                        resource_id=str(INPUTS["payment_id"]),
                        checked_at=planning_time,
                        observed_version=int(INPUTS["object_version"]) + 1,
                        facts_hash="c" * 64,
                    ),
                    hmac_secret=support.HMAC_SECRET,
                )
                async with database.Session() as session:
                    task = await session.get(TaskModel, binding.native_task_id)
                    step = await session.get(StepModel, binding.native_step_id)
                    attempt = await session.get(ExecutionAttemptModel, attempt_id)
                    assert task is not None and step is not None and attempt is not None
                    task.status = TaskStatus.completed.value
                    step.status = StepStatus.completed.value
                    attempt.status = ExecutionAttemptStatus.CONFIRMED.value
                    attempt.result_probe = evidence.model_dump(mode="json")
                    attempt.completed_at = planning_time
                    await session.commit()

                recovered = await journal.initialize(
                    compilation=compilation,
                    admission_bundle=admission,
                    target_url=environment.console_url,
                )
                assert recovered.state is PlanRunState.ACTIVE
                assert len(recovered.completed_prefix) == 1
                assert recovered.completed_prefix[0].attempt_id == attempt_id
                assert recovered.active_step is not None
                assert recovered.active_step.native_task_id == compilation.work_orders[1].task_id
                async with database.Session() as session:
                    events = list(
                        (
                            await session.scalars(
                                select(GovernanceAuditEventModel).where(
                                    GovernanceAuditEventModel.task_id == compilation.business_plan.task_id,
                                    GovernanceAuditEventModel.event_type.like("m8.plan.%"),
                                )
                            )
                        ).all()
                    )
                assert [item.event_type for item in sorted(events, key=lambda item: item.created_at)][-2:] == [
                    "m8.plan.probe_blocked",
                    "m8.plan.probe_resolved",
                ]
            finally:
                await database.engine.dispose()

        asyncio.run(scenario())


@pytest.mark.e2e
def test_m8_replans_only_the_suffix_and_executes_replacement_once() -> None:
    planning_time = datetime.now(timezone.utc)
    authority, original_admission = _compile_admission(planning_time)
    compilation = build_synthetic_m8_compilation(
        authority,
        admission_id=original_admission.admission_id,
        plan_run_id=PLAN_RUN_ID,
    )
    admission = build_m8_admission_bundle(original_admission, compilation)

    with support.isolated_m4_environment() as environment:
        database = support.M4Database(environment.database_url)

        async def scenario() -> dict[str, object]:
            try:
                async with database.Session() as session:
                    session.add(
                        OrganizationModel(
                            organization_id=ORGANIZATION_ID,
                            organization_name="FinRPA M8 Governed Agent Loop",
                        )
                    )
                    session.add(
                        GovernedTaskAdmissionModel(
                            admission_id=admission.admission_id,
                            organization_id=ORGANIZATION_ID,
                            request_id=REQUEST_ID,
                            task_id=admission.task.task_id,
                            contract_id=admission.contract.contract_id,
                            bundle_schema_version=admission.schema_version,
                            admission_fingerprint="m8-governed-admission-e2e",
                            bundle_fingerprint="m8-governed-bundle-v1-e2e",
                            bundle_payload=admission.model_dump(mode="json"),
                            mode="audit",
                            committed_at=planning_time,
                        )
                    )
                    await session.commit()

                journal = SqlAlchemyGovernedPlanJournal(database.Session)
                checkpoint = await journal.initialize(
                    compilation=compilation,
                    admission_bundle=admission,
                    target_url=environment.console_url,
                )
                async with support.real_chromium(environment.console_url, environment.cleanup) as browser:
                    runner = _M8NativeRunner(database, browser, environment)

                    def adapter_factory(child: Any, bundle: Any) -> NativeSkyvernWorkOrderAdapter:
                        return NativeSkyvernWorkOrderAdapter(
                            SqlAlchemyNativePublicationRepository(database.Session),
                            compilation=child,
                            admission_bundle=bundle,
                            target_url=environment.console_url,
                            navigation_payload=dict(INPUTS),
                        )

                    coordinator = GovernedPlanCoordinator(
                        journal,
                        adapter_factory=adapter_factory,
                        runner=runner,
                    )
                    paused = await coordinator.run_until_pause(
                        compilation=compilation,
                        admission_bundle=admission,
                        checkpoint=checkpoint,
                    )
                    assert paused.state is PlanRunState.REPLAN_REQUIRED
                    assert len(paused.completed_prefix) == 1
                    assert runner.mismatched_native_task_id == compilation.work_orders[1].task_id
                    assert await database.get_task(compilation.work_orders[2].task_id, ORGANIZATION_ID) is None

                    replacement = build_replacement_suffix(compilation, completed_prefix_length=1)
                    replacement_admission = build_m8_admission_bundle(admission, replacement)
                    receipt = await coordinator.apply_replan(
                        previous=compilation,
                        proposed=replacement,
                        checkpoint=paused,
                        admission_bundle=replacement_admission,
                    )
                    superseded = await database.get_task(compilation.work_orders[1].task_id, ORGANIZATION_ID)
                    assert superseded is not None and superseded.status is TaskStatus.canceled
                    assert receipt.checkpoint.completed_prefix == paused.completed_prefix

                    finished = await coordinator.run_until_pause(
                        compilation=replacement,
                        admission_bundle=replacement_admission,
                        checkpoint=receipt.checkpoint,
                    )
                    assert finished.state is PlanRunState.COMPLETED
                    assert len(finished.completed_prefix) == 3
                    assert len(runner.entered_forge) == 3
                    assert len(set(runner.entered_forge)) == 3
                    assert len(runner.execute_urls) == 1
                    with pytest.raises(GovernedPlanError, match="concurrent advance"):
                        await journal.append(
                            checkpoint=receipt.checkpoint,
                            transition=PlanJournalTransition.CHILD_ACTIVATED,
                            authority_digests=_authority_digests(replacement),
                        )
                    attempt_id = finished.completed_prefix[1].attempt_id
                    assert attempt_id is not None
                    async with database.Session() as session:
                        attempt = await session.get(ExecutionAttemptModel, attempt_id)
                        assert attempt is not None
                        attempt.status = ExecutionAttemptStatus.FAILED.value
                        await session.commit()
                    with pytest.raises(GovernedPlanError, match="Permit/Attempt state"):
                        await journal.initialize(
                            compilation=replacement,
                            admission_bundle=replacement_admission,
                            target_url=environment.console_url,
                        )
                    return {
                        "plan_version": finished.plan_version,
                        "replan_count": finished.replan_count,
                        "effect_count": len(runner.execute_urls),
                        "completed_children": len(finished.completed_prefix),
                    }
            finally:
                await database.engine.dispose()

        evidence = asyncio.run(scenario())
        assert evidence == {
            "plan_version": 2,
            "replan_count": 1,
            "effect_count": 1,
            "completed_children": 3,
        }


@pytest.mark.e2e
def test_m9_recorded_model_plan_and_replan_use_the_existing_native_agent_path() -> None:
    planning_time = datetime.now(timezone.utc)
    authority, original_admission = _compile_admission(planning_time)
    plan_input = build_m9_plan_input(authority)
    plan_provider = RecordedM9Provider(
        [
            {
                "capability_id": authority.projection[0].capability_id,
                "input_slots": [item.name for item in plan_input.input_slots],
                "step_roles": ["precheck", "submit", "confirm"],
            }
        ]
    )
    plan_decision = M9PlannerEngine(plan_provider).plan(plan_input)
    assert plan_decision.disposition is M9PlannerDisposition.ACCEPTED
    assert isinstance(plan_decision.proposal, PlanProposal)
    compilation = compile_m9_plan(
        authority,
        plan_decision.proposal,
        admission_id=original_admission.admission_id,
        plan_run_id=M9_PLAN_RUN_ID,
    )
    admission = build_m8_admission_bundle(original_admission, compilation)
    model_request = json.dumps(plan_provider.calls[0].model_dump(mode="json"), sort_keys=True)
    for value in INPUTS.values():
        if isinstance(value, str):
            assert value not in model_request

    with support.isolated_m4_environment() as environment:
        database = support.M4Database(environment.database_url)

        async def scenario() -> dict[str, object]:
            try:
                async with database.Session() as session:
                    session.add(
                        OrganizationModel(
                            organization_id=ORGANIZATION_ID,
                            organization_name="FinRPA M9 Model-Governed Agent Loop",
                        )
                    )
                    session.add(
                        GovernedTaskAdmissionModel(
                            admission_id=admission.admission_id,
                            organization_id=ORGANIZATION_ID,
                            request_id=REQUEST_ID,
                            task_id=admission.task.task_id,
                            contract_id=admission.contract.contract_id,
                            bundle_schema_version=admission.schema_version,
                            admission_fingerprint="m9-governed-admission-e2e",
                            bundle_fingerprint="m9-governed-bundle-v1-e2e",
                            bundle_payload=admission.model_dump(mode="json"),
                            mode="audit",
                            committed_at=planning_time,
                        )
                    )
                    await session.commit()

                journal = SqlAlchemyGovernedPlanJournal(database.Session)
                checkpoint = await journal.initialize(
                    compilation=compilation,
                    admission_bundle=admission,
                    target_url=environment.console_url,
                )
                async with support.real_chromium(environment.console_url, environment.cleanup) as browser:
                    runner = _M8NativeRunner(database, browser, environment)

                    def adapter_factory(child: Any, bundle: Any) -> NativeSkyvernWorkOrderAdapter:
                        return NativeSkyvernWorkOrderAdapter(
                            SqlAlchemyNativePublicationRepository(database.Session),
                            compilation=child,
                            admission_bundle=bundle,
                            target_url=environment.console_url,
                            navigation_payload=dict(INPUTS),
                        )

                    coordinator = GovernedPlanCoordinator(
                        journal,
                        adapter_factory=adapter_factory,
                        runner=runner,
                    )
                    paused = await coordinator.run_until_pause(
                        compilation=compilation,
                        admission_bundle=admission,
                        checkpoint=checkpoint,
                    )
                    assert paused.state is PlanRunState.REPLAN_REQUIRED
                    assert len(paused.completed_prefix) == 1

                    token = redact_replan_evidence(
                        mismatch_code="BUSINESS_STATE_CHANGED",
                        step_role=M9StepRole.SUBMIT,
                        raw_evidence={
                            "native_task_id": runner.mismatched_native_task_id,
                            "message": "raw browser mismatch is reduced to one digest",
                            "trusted_business_values": {
                                "text": INPUTS["payment_id"],
                                "integer": 700001,
                                "float": 700002.5,
                                "boolean": True,
                                "decimal": Decimal("700003.75"),
                                "nested": {"reference": INPUTS["reference"]},
                            },
                        },
                    )
                    replan_input = build_m9_replan_input(
                        compilation,
                        completed_prefix_length=1,
                        remaining_replans=1,
                        evidence_tokens=(token,),
                    )
                    replan_provider = RecordedM9Provider([{"step_roles": ["submit", "confirm"]}])
                    replan_decision = M9PlannerEngine(replan_provider).replan(
                        replan_input,
                        preconditions=M9ReplanPreconditions(remaining_replans=1),
                    )
                    assert replan_decision.disposition is M9PlannerDisposition.ACCEPTED
                    assert isinstance(replan_decision.proposal, SuffixReplanProposal)
                    replacement = compile_m9_replan(
                        compilation,
                        replan_decision.proposal,
                        completed_prefix_length=1,
                    )
                    replacement_admission = build_m8_admission_bundle(admission, replacement)
                    receipt = await coordinator.apply_replan(
                        previous=compilation,
                        proposed=replacement,
                        checkpoint=paused,
                        admission_bundle=replacement_admission,
                    )
                    assert receipt.checkpoint.completed_prefix == paused.completed_prefix

                    finished = await coordinator.run_until_pause(
                        compilation=replacement,
                        admission_bundle=replacement_admission,
                        checkpoint=receipt.checkpoint,
                    )
                    assert finished.state is PlanRunState.COMPLETED
                    assert len(finished.completed_prefix) == 3
                    assert len(runner.entered_forge) == 3
                    assert len(set(runner.entered_forge)) == 3
                    assert len(runner.execute_urls) == 1
                    replan_request = json.dumps(
                        replan_provider.calls[0].model_dump(mode="json"),
                        sort_keys=True,
                    )
                    assert runner.mismatched_native_task_id not in replan_request
                    assert token.content_digest in replan_request
                    assert "trusted_business_values" not in replan_request
                    for value in INPUTS.values():
                        if isinstance(value, str):
                            assert value not in replan_request
                    for canary in ("700001", "700002.5", "700003.75"):
                        assert canary not in replan_request
                    return {
                        "plan_version": finished.plan_version,
                        "replan_count": finished.replan_count,
                        "effect_count": len(runner.execute_urls),
                        "completed_children": len(finished.completed_prefix),
                    }
            finally:
                await database.engine.dispose()

        evidence = asyncio.run(scenario())
        assert evidence == {
            "plan_version": 2,
            "replan_count": 1,
            "effect_count": 1,
            "completed_children": 3,
        }
