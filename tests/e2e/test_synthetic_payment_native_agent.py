"""M7 native-Agent proof over isolated PostgreSQL and real Chromium."""

# ruff: noqa: E402, F401, I001

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from tests.e2e import m4_synthetic_support as support

from enterprise.agent.constrained_planner import DeterministicPlanner
from enterprise.agent.interactions import CapabilityRequest, CapabilityRequestKind, EntryMode
from enterprise.auth.schemas import DepartmentRole, UserContext
from enterprise.domains.synthetic_payment.constants import (
    BUSINESS_LINE_ID,
    CAPABILITY_ID,
    PACK_VERSION,
    PAYMENTS_DEPARTMENT_ID,
    POLICY_VERSION,
)
from enterprise.domains.synthetic_payment.m6_runtime import (
    SyntheticM6TrustedContext,
    build_synthetic_installation,
    compile_synthetic_request,
)
from enterprise.domains.synthetic_payment.m7_runtime import (
    NativeProbeOutcome,
    NativeSkyvernBinding,
    NativeSkyvernWorkOrderAdapter,
    SqlAlchemyNativePublicationRepository,
    SyntheticNativeActionContextResolver,
    build_native_probe_evidence,
    build_redacted_m7_trace,
    derive_native_task_id,
)
from enterprise.domains.synthetic_payment.sdk_manifest import build_pack_sdk_manifest
from enterprise.governance.admission import (
    AdmissionAuditRecord,
    GovernedTaskDraft,
    TaskAdmissionBundle,
)
from enterprise.governance.audit import observation_hash
from enterprise.governance.capabilities import CapabilityDataScope
from enterprise.governance.classification import action_fingerprint
from enterprise.governance.contracts import (
    DecisionOutcome,
    ExecutionAttemptStatus,
    ExecutionAuthorization,
    ExecutionEffect,
    PolicyDecision,
)
from enterprise.governance.creation_snapshot import TaskCreationPath, TrustedTaskCreationSnapshot
from enterprise.governance.execution_attempt_service import ExecutionAttemptRecoveryRequired
from enterprise.governance.execution_profiles import ExecutionMechanism, ExecutionProfile
from enterprise.governance.models import GovernedTaskAdmissionModel, PendingActionModel
from enterprise.governance.pack_conformance import evaluate_static_pack_conformance
from enterprise.governance.permit_service import issue_permit
from enterprise.governance.result_probes import ResultProbeEvidence
from skyvern.forge.agent import ForgeAgent
from skyvern.forge.native_action import NativeActionDisposition, NativeActionResolution
from skyvern.forge.sdk.db.models import OrganizationModel
from skyvern.forge.sdk.schemas.tasks import TaskStatus
from skyvern.webeye.actions.actions import ClickAction, InputOrSelectContext, SelectOption, SelectOptionAction
from skyvern.webeye.actions.handler import ActionHandler

RUN_ID = "agentpact-m7-native-agent-e2e"
ORGANIZATION_ID = "org-m7-native-agent"
REQUEST_ID = "request-m7-native-agent-e2e"
ADMISSION_ID = "admission-m7-native-agent-e2e"
CONTRACT_ID = "contract-m7-native-agent-e2e"
INPUTS = {
    "payment_id": "pay-demo-001",
    "beneficiary_id": "vendor-demo-001",
    "amount": "5000.00",
    "currency": "CNY",
    "reference": "Synthetic invoice 001",
    "object_version": 1,
}


def _compile(task_id: str, now: datetime):
    manifest = build_pack_sdk_manifest()
    return compile_synthetic_request(
        natural_language_request="Submit the approved synthetic payment once through the native Agent",
        context=SyntheticM6TrustedContext(
            request_id=REQUEST_ID,
            task_id=task_id,
            contract_id=CONTRACT_ID,
            tenant_id=ORGANIZATION_ID,
            user=UserContext(
                user_id="synthetic-m7-browser-operator",
                org_id=ORGANIZATION_ID,
                department_roles=[
                    DepartmentRole(
                        department_id=PAYMENTS_DEPARTMENT_ID,
                        department_name="Synthetic payments",
                        role="operator",
                    )
                ],
                business_line_ids=[BUSINESS_LINE_ID],
            ),
            data_scope=CapabilityDataScope(
                department_id=PAYMENTS_DEPARTMENT_ID,
                business_line_id=BUSINESS_LINE_ID,
                resource_ids={str(INPUTS["payment_id"])},
            ),
            resolved_at=now,
        ),
        installation=build_synthetic_installation(
            tenant_id=ORGANIZATION_ID,
            accepted_at=now - timedelta(minutes=1),
            expires_at=now + timedelta(minutes=20),
            contract_digest=manifest.manifest_digest,
        ),
        conformance_report=evaluate_static_pack_conformance(manifest),
        planner=DeterministicPlanner(INPUTS),
    )


def _compile_admission(now: datetime) -> tuple[Any, TaskAdmissionBundle]:
    preliminary = _compile("placeholder-m7-native-task", now)
    native_task_id = derive_native_task_id(
        admission_id=ADMISSION_ID,
        request_id=REQUEST_ID,
        work_order_id=preliminary.work_order.work_order_id,
    )
    compilation = _compile(native_task_id, now)
    grant = compilation.grants.grants[0]
    request = CapabilityRequest(
        request_id=REQUEST_ID,
        submitted_at=now,
        entry_mode=EntryMode.CHAT,
        principal_ref="synthetic-m7-browser-operator",
        session_ref="session-m7-native-agent-e2e",
        tenant_id=ORGANIZATION_ID,
        requested_scope=compilation.business_plan.data_scope,
        capability_ref=CAPABILITY_ID,
        capability_version=PACK_VERSION,
        request_kind=CapabilityRequestKind.TRANSITION,
        typed_inputs=dict(INPUTS),
        resource_refs={str(INPUTS["payment_id"])},
        user_intent_summary="Submit one synthetic payment through the native Agent",
        grant_ref=grant.grant_id,
        contract_versions={"domain_pack": PACK_VERSION},
    )
    snapshot = TrustedTaskCreationSnapshot(
        task_id=native_task_id,
        organization_id=ORGANIZATION_ID,
        creation_path=TaskCreationPath.NATIVE,
        initiator_id="synthetic-m7-browser-operator",
        service_principal_id="synthetic_m6_planner_service",
        department_id=PAYMENTS_DEPARTMENT_ID,
        business_line_id=BUSINESS_LINE_ID,
        authorization_snapshot={"installation_id": compilation.installation.installation_id},
        policy_version=POLICY_VERSION,
        contract_version=1,
        created_at=now,
        request_id=REQUEST_ID,
    )
    audit = AdmissionAuditRecord(
        admission_id=ADMISSION_ID,
        request_id=REQUEST_ID,
        task_id=native_task_id,
        organization_id=ORGANIZATION_ID,
        contract_id=compilation.task_contract.contract_id,
        plan_id=compilation.business_plan.plan_id,
        grant_id=grant.grant_id,
        capability_id=CAPABILITY_ID,
        capability_version=PACK_VERSION,
        policy_version=POLICY_VERSION,
        revocation_epoch=grant.revocation_epoch,
        mode=compilation.task_contract.mode,
        created_at=now,
    )
    return compilation, TaskAdmissionBundle(
        admission_id=ADMISSION_ID,
        task=GovernedTaskDraft(
            task_id=native_task_id,
            organization_id=ORGANIZATION_ID,
            goal=compilation.task_contract.goal,
        ),
        creation_snapshot=snapshot,
        contract=compilation.task_contract,
        request=request,
        grants=tuple(compilation.grants.grants),
        plan=compilation.business_plan,
        work_orders=(compilation.work_order,),
        audit_record=audit,
    )


def _click(task: Any, step: Any, element_id: str, order: int, description: str) -> ClickAction:
    return ClickAction(
        element_id=element_id,
        organization_id=task.organization_id,
        task_id=task.task_id,
        step_id=step.step_id,
        step_order=step.order,
        action_order=order,
        description=description,
        reasoning="M7 deterministic native-Agent proof",
        intention=description,
    )


def _select_fault(task: Any, step: Any, element_id: str) -> SelectOptionAction:
    return SelectOptionAction(
        element_id=element_id,
        option=SelectOption(value="commit_then_inconclusive"),
        input_or_select_context=InputOrSelectContext(
            intention="Select the approved synthetic ambiguity fault",
            field="Synthetic execution fault mode",
            is_required=True,
        ),
        organization_id=task.organization_id,
        task_id=task.task_id,
        step_id=step.step_id,
        step_order=step.order,
        action_order=2,
        description="Select the approved synthetic ambiguity fault",
        reasoning="M7 deterministic fault injection through Skyvern",
        intention="Select the approved synthetic ambiguity fault",
    )


class _BrowserInputObserver:
    def __init__(self, page: Any) -> None:
        self._page = page

    async def observe(self, *, scraped_page: Any) -> dict[str, object]:
        if scraped_page.url != self._page.url:
            raise ValueError("M7 observer rejected a changed browser target")
        observed = await support.observe_synthetic_payment_inputs(self._page)
        observed["object_version"] = INPUTS["object_version"]
        return observed


class _PostgresPermitAuthorizer:
    def __init__(self, database: support.M4Database) -> None:
        self._database = database
        self.authorization: ExecutionAuthorization | None = None
        self.profile: ExecutionProfile | None = None
        self.authorized_action_payload: dict[str, Any] | None = None

    async def authorize(
        self,
        *,
        task: Any,
        step: Any,
        scraped_page: Any,
        action: Any,
        binding: NativeSkyvernBinding,
        execution_binding: Any,
    ) -> tuple[ExecutionAuthorization, ExecutionProfile]:
        observed_hash = observation_hash(
            url=scraped_page.url,
            html=scraped_page.html,
            secret=support.HMAC_SECRET,
        )
        fingerprint = action_fingerprint(
            task_id=task.task_id,
            step_id=step.step_id,
            action_payload=action.model_dump(mode="json", exclude_none=True),
            observation_hash=observed_hash,
            secret=support.HMAC_SECRET,
        )
        idempotency_key = f"synthetic:{INPUTS['payment_id']}"
        profile = ExecutionProfile(
            mechanism=ExecutionMechanism.LOCATOR,
            fallback_rank=0,
            evidence_refs=[f"agentpact://m7-native/{binding.binding_digest}"],
        )
        decision = PolicyDecision(
            decision_id="decision-m7-native-agent-e2e",
            intent_id="intent-m7-native-agent-e2e",
            outcome=DecisionOutcome.ALLOW,
            risk_level="critical",
            reasons=["Owner-approved localhost-only synthetic M7 proof"],
            matched_rules=["agentpact-m7-native-v1"],
            policy_version=POLICY_VERSION,
        )
        now = datetime.now(timezone.utc)
        expires_at = min(now + timedelta(minutes=5), execution_binding.expires_at)
        async with self._database.Session() as session:
            session.add(
                PendingActionModel(
                    pending_action_id="pending-m7-native-agent-e2e",
                    task_id=task.task_id,
                    step_id=step.step_id,
                    contract_id=binding.contract_id,
                    organization_id=task.organization_id,
                    action_fingerprint=fingerprint,
                    observation_hash=observed_hash,
                    action_payload=action.model_dump(mode="json", exclude_none=True),
                    intent_payload={
                        "intent_id": decision.intent_id,
                        "operation": "synthetic.payment.submit",
                        "effect": ExecutionEffect.EXTERNAL_WRITE.value,
                        "m7_binding_digest": binding.binding_digest,
                        "m6_execution_binding": execution_binding.model_dump(mode="json"),
                    },
                    decision_payload=decision.model_dump(mode="json"),
                    approval_id="approval-m7-native-agent-e2e",
                    status="approved",
                    row_version=1,
                    expires_at=expires_at,
                )
            )
            permit = await issue_permit(
                db_session=session,
                task_id=task.task_id,
                step_id=step.step_id,
                contract_id=binding.contract_id,
                action_fingerprint=fingerprint,
                observation_hash=observed_hash,
                decision=decision,
                effect=ExecutionEffect.EXTERNAL_WRITE,
                execution_profile=profile,
                ttl_seconds=max(1, int((expires_at - now).total_seconds())),
            )
            await session.commit()
        authorization = ExecutionAuthorization(
            permit_id=permit.permit_id,
            action_fingerprint=fingerprint,
            observation_hash=observed_hash,
            idempotency_key=idempotency_key,
            effect=ExecutionEffect.EXTERNAL_WRITE,
        )
        self.authorization = authorization
        self.profile = profile
        self.authorized_action_payload = action.model_dump(mode="json", exclude_none=True)
        return authorization, profile


@pytest.mark.e2e
def test_native_agent_suspends_unknown_effect_and_reconciles_exactly_once() -> None:
    planning_time = datetime.now(timezone.utc)
    compilation, admission = _compile_admission(planning_time)
    assert compilation.work_order.task_id == admission.task.task_id

    with support.isolated_m4_environment() as environment:
        database = support.M4Database(environment.database_url)

        async def scenario() -> dict[str, object]:
            try:
                async with database.Session() as session:
                    session.add(
                        OrganizationModel(
                            organization_id=ORGANIZATION_ID,
                            organization_name="FinRPA M7 Native Agent",
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
                            admission_fingerprint="m7-native-admission-e2e",
                            bundle_fingerprint="m7-native-bundle-e2e",
                            bundle_payload=admission.model_dump(mode="json"),
                            mode="audit",
                            committed_at=planning_time,
                        )
                    )
                    await session.commit()

                adapter = NativeSkyvernWorkOrderAdapter(
                    SqlAlchemyNativePublicationRepository(database.Session),
                    compilation=compilation,
                    admission_bundle=admission,
                    target_url=environment.console_url,
                    navigation_payload=dict(INPUTS),
                )
                binding = await adapter.prepare(compilation.work_order)
                task = await database.get_task(binding.native_task_id, ORGANIZATION_ID)
                step = await database.get_step(binding.native_step_id, ORGANIZATION_ID)
                assert task is not None and step is not None

                async with support.real_chromium(environment.console_url, environment.cleanup) as browser:
                    governance = support.SeededGovernanceContext(
                        task=task,
                        step=step,
                        contract_id=binding.contract_id,
                    )
                    with support.configured_forge_boundary(database, browser.state):
                        create_observation = await support.scrape_current_page(browser)
                        await support.run_handler_action(
                            browser=browser,
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
                                "Create the isolated synthetic payment challenge",
                            ),
                        )
                        await browser.page.wait_for_function(
                            "document.getElementById('state').textContent === 'pending_approval'",
                            timeout=10_000,
                        )
                        challenge_id = (await browser.page.text_content("#challenge") or "").strip()
                        prepared = support.http_json(
                            f"{environment.console_url.rstrip('/')}/api/challenges/{challenge_id}"
                        )

                        approval_observation = await support.scrape_current_page(browser)
                        await support.run_handler_action(
                            browser=browser,
                            governance=governance,
                            scraped_page=approval_observation,
                            action=_click(
                                task,
                                step,
                                support.element_id_by_aria_label(
                                    approval_observation,
                                    "Approve synthetic payment",
                                ),
                                1,
                                "Apply the independent synthetic approval",
                            ),
                        )
                        await browser.page.wait_for_function(
                            "document.getElementById('state').textContent === 'ready'",
                            timeout=10_000,
                        )

                        fault_observation = await support.scrape_current_page(browser)
                        await support.run_handler_action(
                            browser=browser,
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

                    with support.configured_forge_boundary(database, browser.state):
                        execute_observation = await support.scrape_current_page(browser)
                        execute_element_id = support.element_id_by_aria_label(
                            execute_observation,
                            "Execute synthetic payment once",
                        )
                        later_element_id = support.element_id_by_aria_label(
                            execute_observation,
                            "Create synthetic payment challenge",
                        )

                    llm_calls: list[str] = []

                    async def deterministic_llm(*, prompt: str, **_kwargs: Any) -> dict[str, Any]:
                        assert execute_element_id in prompt
                        assert later_element_id in prompt
                        llm_calls.append(prompt)
                        return {
                            "actions": [
                                {
                                    "action_type": "CLICK",
                                    "id": execute_element_id,
                                    "reasoning": "Execute the exact admitted synthetic Work Order once",
                                },
                                {
                                    "action_type": "CLICK",
                                    "id": later_element_id,
                                    "reasoning": "This later batched action must remain unexecuted",
                                },
                            ]
                        }

                    authorizer = _PostgresPermitAuthorizer(database)
                    resolver = SyntheticNativeActionContextResolver(
                        database.Session,
                        binding=binding,
                        compilation=compilation,
                        authorizer=authorizer,
                        business_input_observer=_BrowserInputObserver(browser.page),
                        hmac_secret=support.HMAC_SECRET,
                    )
                    idempotency_key = f"synthetic:{INPUTS['payment_id']}"
                    observed_statuses: list[str | None] = []
                    execute_urls: list[str] = []
                    await support.install_execute_order_probe(
                        page=browser.page,
                        database=database,
                        idempotency_key=idempotency_key,
                        observed_statuses=observed_statuses,
                        observed_urls=execute_urls,
                        task_id=binding.native_task_id,
                    )

                    with support.configured_native_forge_boundary(
                        database,
                        browser.state,
                        organization_id=ORGANIZATION_ID,
                        task_id=binding.native_task_id,
                        step_id=binding.native_step_id,
                        run_id=RUN_ID,
                        llm_api_handler=deterministic_llm,
                    ):
                        suspended_step, output = await ForgeAgent(resolver).agent_step(
                            task=task,
                            step=step,
                            browser_state=browser.state,
                            complete_verification=False,
                        )

                    await browser.page.wait_for_function(
                        "document.getElementById('state').textContent === 'unknown'",
                        timeout=10_000,
                    )
                    assert len(llm_calls) == 1
                    assert len(output.actions or []) == 2
                    assert output.actions_and_results is not None
                    assert output.actions_and_results[1][1] == []
                    assert observed_statuses == [ExecutionAttemptStatus.EXECUTING.value]
                    assert len(execute_urls) == 1
                    assert suspended_step.status.value == "pending_result_probe"

                    durable_unknown = await support.execution_attempt(
                        database,
                        idempotency_key,
                        task_id=binding.native_task_id,
                    )
                    persisted_task = await database.get_task(binding.native_task_id, ORGANIZATION_ID)
                    persisted_step = await database.get_step(binding.native_step_id, ORGANIZATION_ID)
                    assert durable_unknown.status == ExecutionAttemptStatus.UNKNOWN.value
                    assert persisted_task is not None and persisted_task.status is TaskStatus.pending_result_probe
                    assert persisted_step is not None and persisted_step.status.value == "pending_result_probe"

                    authorization = authorizer.authorization
                    profile = authorizer.profile
                    authorized_action_payload = authorizer.authorized_action_payload
                    assert authorization is not None and profile is not None
                    assert authorized_action_payload is not None
                    replay_resolution = NativeActionResolution(
                        disposition=NativeActionDisposition.BOUND_AUTHORIZED_EFFECT,
                        operation="submit",
                        binding_digest=binding.binding_digest,
                        observation_hash=authorization.observation_hash,
                        action_fingerprint=authorization.action_fingerprint,
                        execution_authorization=authorization,
                        execution_profile=profile,
                    )
                    with support.configured_forge_boundary(database, browser.state):
                        with pytest.raises(ExecutionAttemptRecoveryRequired, match="recovery probe is required"):
                            await ActionHandler.handle_action(
                                scraped_page=output.scraped_page,
                                task=task,
                                step=step,
                                page=browser.page,
                                action=ClickAction.model_validate(authorized_action_payload),
                                native_resolution=replay_resolution,
                            )
                    assert len(execute_urls) == 1
                    assert (await browser.page.text_content("#challenge") or "").strip() == challenge_id

                    inconclusive_probe = support.http_json(
                        f"{environment.console_url.rstrip('/')}/api/challenges/{challenge_id}/probe",
                        payload={},
                    )
                    assert inconclusive_probe["result_probe"]["status"] == "unknown"
                    inconclusive = await resolver.reconcile_probe(
                        evidence=build_native_probe_evidence(
                            binding=binding,
                            authorization=authorization,
                            attempt_id=durable_unknown.attempt_id,
                            result_probe=ResultProbeEvidence.model_validate(
                                inconclusive_probe["result_probe"]
                            ),
                            hmac_secret=support.HMAC_SECRET,
                        )
                    )
                    assert inconclusive.attempt_status is ExecutionAttemptStatus.UNKNOWN

                    payment_id = prepared["facts"]["payment_id"]
                    cleared = support.http_json(
                        f"{environment.console_url.rstrip('/')}/api/payments/{payment_id}/clear-probe-fault",
                        payload={},
                    )
                    assert cleared == {"cleared": True}
                    confirmed_probe = support.http_json(
                        f"{environment.console_url.rstrip('/')}/api/challenges/{challenge_id}/probe",
                        payload={},
                    )
                    confirmed = await resolver.reconcile_probe(
                        evidence=build_native_probe_evidence(
                            binding=binding,
                            authorization=authorization,
                            attempt_id=durable_unknown.attempt_id,
                            result_probe=ResultProbeEvidence.model_validate(
                                confirmed_probe["result_probe"]
                            ),
                            hmac_secret=support.HMAC_SECRET,
                        )
                    )
                    final_task = await database.get_task(binding.native_task_id, ORGANIZATION_ID)
                    final_step = await database.get_step(binding.native_step_id, ORGANIZATION_ID)
                    assert confirmed.attempt_status is ExecutionAttemptStatus.CONFIRMED
                    assert final_task is not None and final_task.status is TaskStatus.completed
                    assert final_step is not None and final_step.status.value == "completed"

                    trace = build_redacted_m7_trace(
                        compilation=compilation,
                        binding=binding,
                        permit_id=authorization.permit_id,
                        attempt_id=durable_unknown.attempt_id,
                        probe_receipt=confirmed,
                    )
                    serialized_trace = trace.model_dump_json()
                    for field_name in ("payment_id", "beneficiary_id", "amount", "currency", "reference"):
                        assert str(INPUTS[field_name]) not in serialized_trace

                    audit = support.http_json(f"{environment.console_url.rstrip('/')}/api/audit")
                    event_types = [event["event_type"] for event in audit]
                    assert event_types.count("approval_requested") == 1
                    assert event_types.count("attempt_executing") == 1
                    assert event_types.count("attempt_unknown") == 1
                    assert event_types.count("attempt_confirmed") == 1
                    return {
                        "task_status": final_task.status.value,
                        "step_status": final_step.status.value,
                        "effect_count": len(execute_urls),
                        "attempt_sequence": ["executing", "unknown", "confirmed"],
                    }
            finally:
                await database.engine.dispose()

        evidence = asyncio.run(scenario())
        assert evidence == {
            "task_status": "completed",
            "step_status": "completed",
            "effect_count": 1,
            "attempt_sequence": ["executing", "unknown", "confirmed"],
        }

    assert environment.cleanup.browser_closed is True
    assert environment.cleanup.console_stopped is True
    assert environment.cleanup.postgres_stopped is True
    assert environment.cleanup.temp_root_removed is True
    assert environment.cleanup.console_port is not None
    assert environment.cleanup.postgres_port is not None
    assert not support.is_loopback_port_open(environment.cleanup.console_port)
    assert not support.is_loopback_port_open(environment.cleanup.postgres_port)
