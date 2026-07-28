from __future__ import annotations

import asyncio

import pytest

from enterprise.governance.contracts import ExecutionAttemptStatus
from enterprise.governance.execution_attempt_service import ExecutionAttemptRecoveryRequired
from skyvern.webeye.actions.actions import ClickAction
from tests.e2e.m4_synthetic_support import (
    HMAC_SECRET,
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


@pytest.mark.e2e
def test_real_chromium_payment_effect_is_governed_unknown_probed_and_not_replayed() -> None:
    with isolated_m4_environment() as environment:
        database = M4Database(environment.database_url)

        async def scenario() -> dict[str, object]:
            try:
                async with real_chromium(environment.console_url, environment.cleanup) as browser:
                    with configured_forge_boundary(database, browser.state):
                        governance = await seed_governance_context(database, environment.console_url)

                        create_observation = await scrape_current_page(browser)
                        await run_handler_action(
                            browser=browser,
                            governance=governance,
                            scraped_page=create_observation,
                            action=click_action(
                                element_id=element_id_by_aria_label(
                                    create_observation,
                                    "Create synthetic payment challenge",
                                ),
                                order=0,
                                description="Create the isolated synthetic payment challenge",
                            ),
                        )
                        await browser.page.wait_for_function(
                            "document.getElementById('state').textContent === 'pending_approval'",
                            timeout=10_000,
                        )
                        challenge_id = (await browser.page.text_content("#challenge") or "").strip()
                        assert challenge_id and challenge_id != "-"
                        prepared = http_json(
                            f"{environment.console_url.rstrip('/')}/api/challenges/{challenge_id}"
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
                                    "Approve synthetic payment",
                                ),
                                order=1,
                                description="Apply the independent synthetic approval",
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
                                    "Synthetic execution fault mode",
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
                                "Execute synthetic payment once",
                            ),
                            order=3,
                            description="Submit the approved synthetic payment exactly once",
                        )
                        pristine_execute_payload = execute_action.model_dump(mode="json", exclude_none=True)
                        idempotency_key = f"synthetic:{challenge_id}"
                        authorization, profile = await issue_exact_permit(
                            database=database,
                            action=execute_action,
                            scraped_page=execute_observation,
                            idempotency_key=idempotency_key,
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

                        synthetic_unknown = http_json(
                            f"{environment.console_url.rstrip('/')}/api/challenges/{challenge_id}"
                        )
                        assert synthetic_unknown["state"] == "unknown"
                        assert synthetic_unknown["attempt"]["status"] == "unknown"
                        assert synthetic_unknown["attempt"]["idempotency_key"] == idempotency_key
                        assert synthetic_unknown["result_probe"]["status"] == "unknown"
                        assert synthetic_unknown["facts"]["object_version"] == 1

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
                            f"{environment.console_url.rstrip('/')}/api/challenges/{challenge_id}/probe",
                            payload={},
                        )
                        assert still_inconclusive["state"] == "unknown"
                        assert still_inconclusive["result_probe"]["status"] == "unknown"
                        assert (await execution_attempt(database, idempotency_key)).status == "unknown"

                        payment_id = synthetic_unknown["facts"]["payment_id"]
                        cleared = http_json(
                            f"{environment.console_url.rstrip('/')}/api/payments/{payment_id}/clear-probe-fault",
                            payload={},
                        )
                        assert cleared == {"cleared": True}
                        independently_probed = http_json(
                            f"{environment.console_url.rstrip('/')}/api/challenges/{challenge_id}/probe",
                            payload={},
                        )
                        assert independently_probed["state"] == "confirmed"
                        assert independently_probed["result_probe"]["status"] == "confirmed"
                        assert independently_probed["attempt"]["idempotency_key"] == idempotency_key
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

                        return {
                            "attempt_state_sequence": ["executing", "unknown", "confirmed"],
                            "execute_request_count": len(execute_request_urls),
                            "synthetic_object_version": independently_probed["result_probe"]["observed_version"],
                            "synthetic_attempt_events": event_types.count("attempt_executing"),
                            "task_id": TASK_ID,
                        }
            finally:
                await database.engine.dispose()

        evidence = asyncio.run(scenario())
        assert evidence == {
            "attempt_state_sequence": ["executing", "unknown", "confirmed"],
            "execute_request_count": 1,
            "synthetic_object_version": 2,
            "synthetic_attempt_events": 1,
            "task_id": TASK_ID,
        }

    assert environment.cleanup.browser_closed is True
    assert environment.cleanup.console_stopped is True
    assert environment.cleanup.postgres_stopped is True
    assert environment.cleanup.temp_root_removed is True
    assert environment.cleanup.console_port is not None
    assert environment.cleanup.postgres_port is not None
    assert not is_loopback_port_open(environment.cleanup.console_port)
    assert not is_loopback_port_open(environment.cleanup.postgres_port)
