from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from enterprise.agent.constrained_planner import DeterministicPlanner
from enterprise.auth.schemas import DepartmentRole, UserContext
from enterprise.domains.stripe_payment.constants import BUSINESS_LINE_ID, PAYMENTS_DEPARTMENT_ID
from enterprise.domains.stripe_payment.m6_runtime import (
    M6TraceStage,
    StripeM6TrustedContext,
    append_execution_trace,
    bind_compilation_for_execution,
    bind_permit_to_execution,
    build_stripe_installation,
    compile_stripe_request,
)
from enterprise.domains.stripe_payment.sdk_manifest import build_pack_sdk_manifest
from enterprise.governance.capabilities import CapabilityDataScope
from enterprise.governance.contracts import ExecutionAttemptStatus
from enterprise.governance.execution_attempt_service import ExecutionAttemptRecoveryRequired
from enterprise.governance.pack_conformance import evaluate_static_pack_conformance
from skyvern.webeye.actions.actions import ClickAction
from tests.e2e.m4_synthetic_support import (
    CONTRACT_ID,
    HMAC_SECRET,
    ORGANIZATION_ID,
    TASK_ID,
    ActionHandler,
    M4Database,
    assert_loopback_url,
    click_action,
    configured_forge_boundary,
    element_id_by_aria_label,
    execution_attempt,
    http_json,
    install_execute_order_probe,
    is_loopback_port_open,
    isolated_m4_environment,
    issue_exact_permit,
    real_chromium,
    resolve_attempt_from_probe,
    run_handler_action,
    scrape_current_page,
    seed_governance_context,
    select_action,
)

PLANNED_PAYMENT_INTENT_ID = "pi_demo_001"
PAGE_DEFAULTS = {
    "payment_intent_id": "pi_demo_001",
    "customer_id": "cus_demo_001",
    "amount_minor": 5000,
    "currency": "usd",
    "description": "Stripe test invoice 001",
    "object_version": 1,
}


def _compile_browser_request(*, planning_time: datetime, payment_intent_id: str, request_id: str):
    sdk_manifest = build_pack_sdk_manifest()
    inputs = dict(PAGE_DEFAULTS)
    inputs["payment_intent_id"] = payment_intent_id
    return compile_stripe_request(
        natural_language_request="Submit the approved Stripe test-mode payment once through the governed browser",
        context=StripeM6TrustedContext(
            request_id=request_id,
            task_id=TASK_ID,
            contract_id=CONTRACT_ID,
            tenant_id=ORGANIZATION_ID,
            user=UserContext(
                user_id="stripe-m6-browser-operator",
                org_id=ORGANIZATION_ID,
                department_roles=[
                    DepartmentRole(
                        department_id=PAYMENTS_DEPARTMENT_ID,
                        department_name="Stripe payments",
                        role="operator",
                    )
                ],
                business_line_ids=[BUSINESS_LINE_ID],
            ),
            data_scope=CapabilityDataScope(
                department_id=PAYMENTS_DEPARTMENT_ID,
                business_line_id=BUSINESS_LINE_ID,
                resource_ids={payment_intent_id},
            ),
            resolved_at=planning_time,
        ),
        installation=build_stripe_installation(
            tenant_id=ORGANIZATION_ID,
            accepted_at=planning_time - timedelta(minutes=1),
            expires_at=planning_time + timedelta(minutes=10),
            contract_digest=sdk_manifest.manifest_digest,
        ),
        conformance_report=evaluate_static_pack_conformance(sdk_manifest),
        planner=DeterministicPlanner(inputs),
    )


async def observe_stripe_payment_inputs(page) -> dict[str, object]:
    """Read the Stripe checkout form facts without granting a second execution path."""

    return {
        "payment_intent_id": await page.input_value("#pi"),
        "customer_id": await page.input_value("#customer"),
        "amount_minor": int(await page.input_value("#amount")),
        "currency": await page.input_value("#currency"),
        "description": await page.input_value("#description"),
        "object_version": 1,
    }


@pytest.mark.e2e
def test_real_chromium_stripe_payment_effect_is_governed_unknown_probed_and_not_replayed() -> None:
    planning_time = datetime.now(timezone.utc)
    compilation = _compile_browser_request(
        planning_time=planning_time,
        payment_intent_id=PLANNED_PAYMENT_INTENT_ID,
        request_id="request-stripe-m6-governed-browser",
    )
    mismatched_compilation = _compile_browser_request(
        planning_time=planning_time,
        payment_intent_id="pi_mismatch_001",
        request_id="request-stripe-m6-mismatched-browser",
    )
    assert compilation.work_order.task_id == TASK_ID
    assert compilation.work_order.contract_id == CONTRACT_ID
    assert compilation.work_order.business_plan_step_id == compilation.business_plan.steps[0].step_id
    assert "submit" in compilation.work_order.allowed_operations
    assert compilation.work_order.result_probe_ref == "stripe.payment.submit.result-probe.v1"

    with isolated_m4_environment(
        console_module="enterprise.domains.stripe_payment.app:app",
        expected_health={
            "status": "ready",
            "domain_pack": "stripe.payment",
            "production_eligible": False,
        },
    ) as environment:
        database = M4Database(environment.database_url)

        async def scenario() -> dict[str, object]:
            try:
                async with real_chromium(environment.console_url, environment.cleanup) as browser:
                    with configured_forge_boundary(database, browser.state):
                        observed_business_inputs = await observe_stripe_payment_inputs(browser.page)
                        with pytest.raises(ValueError, match="do not match the compiled Planner proposal"):
                            bind_compilation_for_execution(
                                mismatched_compilation,
                                observed_business_inputs=observed_business_inputs,
                                work_order_id=mismatched_compilation.work_order.work_order_id,
                                now=datetime.now(timezone.utc),
                            )
                        assert http_json(f"{environment.console_url.rstrip('/')}/api/audit") == []
                        governance = await seed_governance_context(
                            database,
                            environment.console_url,
                            task_contract=compilation.task_contract,
                        )

                        create_observation = await scrape_current_page(browser)
                        await run_handler_action(
                            browser=browser,
                            governance=governance,
                            scraped_page=create_observation,
                            action=click_action(
                                element_id=element_id_by_aria_label(
                                    create_observation,
                                    "Create Stripe checkout session",
                                ),
                                order=0,
                                description="Create the isolated Stripe checkout session",
                            ),
                        )
                        await browser.page.wait_for_function(
                            "document.getElementById('state').textContent === 'pending_approval'",
                            timeout=10_000,
                        )
                        challenge_id = (await browser.page.text_content("#challenge") or "").strip()
                        assert challenge_id and challenge_id != "-"
                        prepared = http_json(
                            f"{environment.console_url.rstrip('/')}/api/checkout/sessions/{challenge_id}"
                        )
                        assert prepared["state"] == "pending_approval"

                        approval_observation = await scrape_current_page(browser)
                        await run_handler_action(
                            browser=browser,
                            governance=governance,
                            scraped_page=approval_observation,
                            action=click_action(
                                element_id=element_id_by_aria_label(
                                    approval_observation,
                                    "Approve Stripe payment",
                                ),
                                order=1,
                                description="Apply the independent Stripe approval",
                            ),
                        )
                        await browser.page.wait_for_function(
                            "document.getElementById('state').textContent === 'ready'",
                            timeout=10_000,
                        )

                        fault_observation = await scrape_current_page(browser)
                        await run_handler_action(
                            browser=browser,
                            governance=governance,
                            scraped_page=fault_observation,
                            action=select_action(
                                element_id=element_id_by_aria_label(
                                    fault_observation,
                                    "Stripe execution fault mode",
                                ),
                                order=2,
                                value="commit_then_inconclusive",
                            ),
                        )
                        assert await browser.page.input_value("#fault") == "commit_then_inconclusive"

                        execute_observation = await scrape_current_page(browser)
                        execute_action = click_action(
                            element_id=element_id_by_aria_label(
                                execute_observation,
                                "Execute Stripe payment once",
                            ),
                            order=3,
                            description="Submit the approved Stripe payment exactly once",
                        )
                        pristine_execute_payload = execute_action.model_dump(mode="json", exclude_none=True)
                        idempotency_key = f"stripe:{PLANNED_PAYMENT_INTENT_ID}"
                        execution_binding = bind_compilation_for_execution(
                            compilation,
                            observed_business_inputs=prepared["facts"],
                            work_order_id=compilation.work_order.work_order_id,
                            now=datetime.now(timezone.utc),
                        )
                        authorization, profile = await issue_exact_permit(
                            database=database,
                            action=execute_action,
                            scraped_page=execute_observation,
                            idempotency_key=idempotency_key,
                            execution_binding=execution_binding,
                            operation="stripe.payment.submit",
                        )
                        permit_binding = bind_permit_to_execution(
                            execution_binding,
                            permit_id=authorization.permit_id,
                            task_id=TASK_ID,
                            contract_id=CONTRACT_ID,
                            action_fingerprint=authorization.action_fingerprint,
                            idempotency_key=authorization.idempotency_key,
                            now=datetime.now(timezone.utc),
                        )
                        assert authorization.idempotency_key == idempotency_key
                        assert authorization.observation_hash
                        assert authorization.action_fingerprint
                        assert HMAC_SECRET

                        status_before_execute_request: list[str | None] = []
                        execute_request_urls: list[str] = []
                        await install_execute_order_probe(
                            page=browser.page,
                            database=database,
                            idempotency_key=idempotency_key,
                            observed_statuses=status_before_execute_request,
                            observed_urls=execute_request_urls,
                            execute_pattern="**/api/checkout/sessions/*/execute",
                        )

                        await run_handler_action(
                            browser=browser,
                            governance=governance,
                            scraped_page=execute_observation,
                            action=execute_action,
                            authorization=authorization,
                            profile=profile,
                        )
                        await browser.page.wait_for_function(
                            "document.getElementById('state').textContent === 'unknown'",
                            timeout=10_000,
                        )

                        assert status_before_execute_request == [ExecutionAttemptStatus.EXECUTING.value]
                        assert len(execute_request_urls) == 1
                        assert_loopback_url(execute_request_urls[0])

                        durable_unknown = await execution_attempt(database, idempotency_key)
                        assert durable_unknown.status == ExecutionAttemptStatus.UNKNOWN.value
                        assert durable_unknown.started_at is not None
                        assert durable_unknown.completed_at is not None
                        assert durable_unknown.result_probe is None

                        stripe_unknown = http_json(
                            f"{environment.console_url.rstrip('/')}/api/checkout/sessions/{challenge_id}"
                        )
                        assert stripe_unknown["state"] == "unknown"
                        assert stripe_unknown["attempt"]["status"] == "unknown"
                        assert stripe_unknown["attempt"]["idempotency_key"] == f"stripe:{challenge_id}"
                        assert stripe_unknown["result_probe"]["status"] == "unknown"
                        assert stripe_unknown["facts"]["object_version"] == 1

                        duplicate_action = ClickAction.model_validate(pristine_execute_payload)
                        with pytest.raises(ExecutionAttemptRecoveryRequired, match="recovery probe is required"):
                            await ActionHandler.handle_action(
                                scraped_page=execute_observation,
                                task=governance.task,
                                step=governance.step,
                                page=browser.page,
                                action=duplicate_action,
                                execution_authorization=authorization,
                                execution_profile=profile,
                            )
                        assert len(execute_request_urls) == 1
                        assert (await execution_attempt(database, idempotency_key)).status == "unknown"

                        still_inconclusive = http_json(
                            f"{environment.console_url.rstrip('/')}/api/checkout/sessions/{challenge_id}/probe",
                            payload={},
                        )
                        assert still_inconclusive["state"] == "unknown"
                        assert still_inconclusive["result_probe"]["status"] == "unknown"
                        assert (await execution_attempt(database, idempotency_key)).status == "unknown"

                        payment_intent_id = stripe_unknown["facts"]["payment_intent_id"]
                        cleared = http_json(
                            f"{environment.console_url.rstrip('/')}/api/payment_intents/{payment_intent_id}"
                            "/clear-probe-fault",
                            payload={},
                        )
                        assert cleared == {"cleared": True}
                        independently_probed = http_json(
                            f"{environment.console_url.rstrip('/')}/api/checkout/sessions/{challenge_id}/probe",
                            payload={},
                        )
                        assert independently_probed["state"] == "confirmed"
                        assert independently_probed["result_probe"]["status"] == "confirmed"
                        assert independently_probed["attempt"]["idempotency_key"] == f"stripe:{challenge_id}"
                        assert independently_probed["facts"]["object_version"] == 1
                        assert independently_probed["result_probe"]["observed_version"] == 2

                        resolved_status = await resolve_attempt_from_probe(
                            database=database,
                            attempt_id=durable_unknown.attempt_id,
                            result_probe=independently_probed["result_probe"],
                        )
                        assert resolved_status is ExecutionAttemptStatus.CONFIRMED
                        durable_confirmed = await execution_attempt(database, idempotency_key)
                        assert durable_confirmed.status == ExecutionAttemptStatus.CONFIRMED.value
                        assert durable_confirmed.result_probe == independently_probed["result_probe"]

                        audit = http_json(f"{environment.console_url.rstrip('/')}/api/audit")
                        event_types = [event["event_type"] for event in audit]
                        assert event_types.count("attempt_executing") == 1
                        assert event_types.count("attempt_unknown") == 1
                        assert event_types.count("attempt_confirmed") == 1
                        assert len(execute_request_urls) == 1

                        trace = append_execution_trace(
                            compilation.trace,
                            compilation=compilation,
                            execution_binding=execution_binding,
                            permit_binding=permit_binding,
                            attempt_id=durable_confirmed.attempt_id,
                            attempt_task_id=durable_confirmed.task_id,
                            attempt_contract_id=durable_confirmed.contract_id,
                            attempt_action_fingerprint=durable_confirmed.action_fingerprint,
                            attempt_idempotency_key=durable_confirmed.idempotency_key,
                            attempt_state_sequence=("executing", "unknown", "confirmed"),
                            result_probe_evidence={
                                "result_probe": independently_probed["result_probe"],
                                "facts": independently_probed["facts"],
                                "stripe_attempt": independently_probed["attempt"],
                            },
                            final_state=durable_confirmed.status,
                            browser_effect_count=len(execute_request_urls),
                        )
                        return {
                            "attempt_state_sequence": ["executing", "unknown", "confirmed"],
                            "execute_request_count": len(execute_request_urls),
                            "stripe_object_version": independently_probed["result_probe"]["observed_version"],
                            "stripe_attempt_events": event_types.count("attempt_executing"),
                            "task_id": TASK_ID,
                            "trace_stages": [event.stage for event in trace.events[-6:]],
                        }
            finally:
                await database.engine.dispose()

        evidence = asyncio.run(scenario())
        assert evidence == {
            "attempt_state_sequence": ["executing", "unknown", "confirmed"],
            "execute_request_count": 1,
            "stripe_object_version": 2,
            "stripe_attempt_events": 1,
            "task_id": TASK_ID,
            "trace_stages": [
                M6TraceStage.EXECUTION_BINDING,
                M6TraceStage.PERMIT,
                M6TraceStage.ATTEMPT,
                M6TraceStage.BROWSER_EFFECT,
                M6TraceStage.RESULT_PROBE,
                M6TraceStage.FINAL_STATE,
            ],
        }

    assert environment.cleanup.browser_closed is True
    assert environment.cleanup.console_stopped is True
    assert environment.cleanup.postgres_stopped is True
    assert environment.cleanup.temp_root_removed is True
    assert environment.cleanup.console_port is not None
    assert environment.cleanup.postgres_port is not None
    assert not is_loopback_port_open(environment.cleanup.console_port)
    assert not is_loopback_port_open(environment.cleanup.postgres_port)
