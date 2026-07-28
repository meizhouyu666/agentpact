"""Tests for the Phase 2 audit-only action observer."""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from enterprise.governance.analysis import analyze_action, build_observation, evaluate_audit_policy
from enterprise.governance.audit import (
    observation_hash,
    record_action_candidates,
    redacted_action_payload,
)
from enterprise.governance.contracts import DecisionOutcome, PageReadiness, TaskContract
from enterprise.governance.egress_shadow import conservative_shadow_policy, scan_egress_shadow


class FakeAction:
    element_id = "element_4"

    def model_dump(self, **_kwargs):
        return {
            "action_type": "input_text",
            "text": "13800138000",
            "password": "not-for-logs",
            "element_id": "element_4",
        }


class FakeSession:
    def __init__(self):
        self.entries = []
        self.committed = False
        self.rolled_back = False

    def add(self, entry):
        self.entries.append(entry)

    async def commit(self):
        self.committed = True

    async def rollback(self):
        self.rolled_back = True


def test_action_payload_redacts_sensitive_values():
    payload = redacted_action_payload(FakeAction())

    assert payload["text"] == "[REDACTED_PII]"
    assert payload["password"] == "[REDACTED_SECRET]"
    assert payload["element_id"] == "element_4"


def test_observation_hash_is_keyed_when_secret_is_available():
    first = observation_hash(url="https://bank.example", html="<form>1</form>", secret="key-a")
    assert first == observation_hash(url="https://bank.example", html="<form>1</form>", secret="key-a")
    assert first != observation_hash(url="https://bank.example", html="<form>1</form>", secret="key-b")


def test_audit_recorder_requires_hmac_secret():
    session = FakeSession()
    with pytest.raises(ValueError):
        asyncio.run(
            record_action_candidates(
                db_session=session,
                task_id="task_1",
                step_id="step_1",
                organization_id="org_1",
                actions=[FakeAction()],
                page_url="https://bank.example",
                page_html="<form>1</form>",
                mode="audit",
                hmac_secret=None,
            )
        )
    assert session.entries == []


def test_audit_recorder_persists_only_redacted_candidate_and_opaque_evidence_refs():
    session = FakeSession()

    asyncio.run(
        record_action_candidates(
            db_session=session,
            task_id="task_1",
            step_id="step_1",
            organization_id="org_1",
            actions=[FakeAction()],
            page_url="https://bank.example",
            page_html="<input>",
            mode="audit",
            hmac_secret="audit-key",
            element_lookup={"element_4": {"text": "13800138000", "attributes": {}}},
            screenshots=[b"first screenshot"],
        )
    )

    payload = session.entries[0].payload
    assert payload["schema_version"] == "phase2-audit-candidate-v1"
    assert payload["candidate_action"]["text"] == "[REDACTED_PII]"
    assert payload["evidence_refs"]["observation_hash"] == session.entries[0].observation_hash
    assert payload["evidence_refs"]["element_fingerprint"]
    assert len(payload["evidence_refs"]["screenshot_fingerprints"]) == 1
    assert "intent" not in payload
    assert "proposed_policy_decision" not in payload
    assert session.entries[0].contract_id is None
    assert session.entries[0].policy_version is None
    assert "13800138000" not in str(payload)


def test_runtime_element_fingerprint_is_rekeyed_and_bound_to_its_observation():
    unkeyed_skyvern_hash = "unkeyed-sha256-element-hash"
    first_session = FakeSession()
    second_session = FakeSession()

    for session, page_html in ((first_session, "<button>first</button>"), (second_session, "<button>second</button>")):
        asyncio.run(
            record_action_candidates(
                db_session=session,
                task_id="task_1",
                step_id="step_1",
                organization_id="org_1",
                actions=[FakeAction()],
                page_url="https://bank.example",
                page_html=page_html,
                mode="audit",
                hmac_secret="audit-key",
                element_fingerprints={"element_4": unkeyed_skyvern_hash},
            )
        )

    first_fingerprint = first_session.entries[0].payload["evidence_refs"]["element_fingerprint"]
    second_fingerprint = second_session.entries[0].payload["evidence_refs"]["element_fingerprint"]
    assert first_fingerprint != unkeyed_skyvern_hash
    assert second_fingerprint != unkeyed_skyvern_hash
    assert first_fingerprint != second_fingerprint


def test_audit_recorder_rejects_non_audit_modes():
    with pytest.raises(ValueError, match="only in audit mode"):
        asyncio.run(
            record_action_candidates(
                db_session=FakeSession(),
                task_id="task_1",
                step_id="step_1",
                organization_id="org_1",
                actions=[FakeAction()],
                page_url="https://bank.example",
                page_html="<input>",
                mode="off",
                hmac_secret="audit-key",
            )
        )


def test_egress_shadow_records_only_redacted_local_findings():
    dom = "<input value='4111 1111 1111 1111'>"
    prompt = "password=do-not-persist"
    screenshot = b"unredacted screenshot bytes"

    report = scan_egress_shadow(
        policy=conservative_shadow_policy(),
        dom=dom,
        prompt=prompt,
        screenshots=[screenshot],
    )

    assert {(finding.artifact_kind, finding.classification) for finding in report.findings} == {
        ("dom", "financial"),
        ("prompt", "credential"),
        ("screenshot", "restricted"),
    }
    serialized = report.model_dump(mode="json")
    assert "4111 1111 1111 1111" not in str(serialized)
    assert "do-not-persist" not in str(serialized)
    assert "unredacted screenshot bytes" not in str(serialized)
    assert all(finding["redacted_value"].startswith("[REDACTED_") for finding in serialized["findings"])


def test_egress_shadow_does_not_block_audit_candidate_recording():
    session = FakeSession()
    raw_prompt = "password=do-not-persist"
    raw_dom = "<input value='4111 1111 1111 1111'>"
    raw_screenshot = b"unredacted screenshot bytes"

    asyncio.run(
        record_action_candidates(
            db_session=session,
            task_id="task_1",
            step_id="step_1",
            organization_id="org_1",
            actions=[FakeAction()],
            page_url="https://bank.example",
            page_html=raw_dom,
            mode="audit",
            hmac_secret="audit-key",
            prompt=raw_prompt,
            screenshots=[raw_screenshot],
        )
    )

    assert session.committed
    payload = session.entries[0].payload
    assert len(payload["egress_shadow_findings"]) == 4
    assert any(
        finding["field_name"] == "input[0].value"
        and finding["classification"] == "financial"
        for finding in payload["egress_shadow_findings"]
    )
    assert raw_dom not in str(payload)
    assert raw_prompt not in str(payload)
    assert raw_screenshot.decode() not in str(payload)


def test_egress_shadow_classifies_financial_dom_field_names_without_persisting_their_values():
    raw_dom = "<input name='beneficiary' value='supplier-private'><input data-field='amount' value='1200'>"

    report = scan_egress_shadow(policy=conservative_shadow_policy(), dom=raw_dom)

    serialized = report.model_dump(mode="json")
    assert {
        finding["field_name"]
        for finding in serialized["findings"]
        if finding["artifact_kind"] == "dom"
    } >= {"input[0].name", "input[1].data-field"}
    assert "supplier-private" not in str(serialized)
    assert "1200" not in str(serialized)
    assert all(
        finding["redacted_value"].startswith("[REDACTED_")
        for finding in serialized["findings"]
    )
    assert all(
        "beneficiary" not in finding["field_name"] and "amount" not in finding["field_name"]
        for finding in serialized["findings"]
    )


def test_live_audit_hook_does_not_create_contracts_or_evaluate_policy():
    agent_source = (Path(__file__).parents[2] / "skyvern" / "forge" / "agent.py").read_text(encoding="utf-8")
    hook = agent_source[agent_source.index("async def _record_governance_action_candidates") :]

    assert "ensure_task_contract" not in hook.split("async def _get_action_results", maxsplit=1)[0]
    audit_source = (Path(__file__).parents[2] / "enterprise" / "governance" / "audit.py").read_text(encoding="utf-8")
    assert "evaluate_audit_policy" not in audit_source
    assert "analyze_action" not in audit_source
    assert "audit_prompt = task.navigation_goal if engine in CUA_ENGINES else extract_action_prompt" in agent_source


def test_submit_intent_produces_audit_only_approval_recommendation():
    observation = build_observation(
        task_id="task_1",
        step_id="step_1",
        url="https://bank.example/confirm",
        html="<button>Submit payment</button>",
    )
    intent = analyze_action(
        task_id="task_1",
        step_id="step_1",
        action=FakeAction(),
        observation=observation,
        element={"text": "Submit payment", "attributes": {}},
        hmac_secret="test-audit-key",
    )
    decision = evaluate_audit_policy(intent)

    assert intent.operation == "payment"
    assert decision.outcome == DecisionOutcome.REQUIRE_APPROVAL
    assert decision.risk_level == "critical"


def test_policy_denies_expired_or_out_of_contract_operations():
    observation = build_observation(
        task_id="task_1",
        step_id="step_1",
        url="https://bank.example/confirm",
        html="<button>Submit payment</button>",
    )
    intent = analyze_action(
        task_id="task_1",
        step_id="step_1",
        action=FakeAction(),
        observation=observation,
        element={"text": "Submit payment", "attributes": {}},
        hmac_secret="test-audit-key",
    )
    expired_contract = TaskContract(
        contract_id="tc_1",
        task_id="task_1",
        organization_id="org_1",
        goal="Pay supplier",
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    restricted_contract = expired_contract.model_copy(
        update={"expires_at": None, "allowed_operations": {"read"}}
    )

    assert evaluate_audit_policy(intent, task_contract=expired_contract).outcome == DecisionOutcome.DENY
    assert evaluate_audit_policy(intent, task_contract=restricted_contract).outcome == DecisionOutcome.DENY


def test_policy_treats_the_exact_contract_expiry_instant_as_expired():
    now = datetime(2026, 7, 23, 10, 0, tzinfo=timezone.utc)
    observation = build_observation(
        task_id="task_1",
        step_id="step_1",
        url="https://bank.example/query",
        html="<button>Query balance</button>",
        readiness=PageReadiness.READY,
    )
    intent = analyze_action(
        task_id="task_1",
        step_id="step_1",
        action=FakeAction(),
        observation=observation,
        element={"text": "Query balance", "attributes": {}},
        hmac_secret="test-audit-key",
    )
    contract = TaskContract(
        contract_id="tc_exact_expiry",
        task_id="task_1",
        organization_id="org_1",
        goal="Query balance",
        expires_at=now,
    )

    decision = evaluate_audit_policy(intent, observation=observation, task_contract=contract, now=now)

    assert decision.outcome == DecisionOutcome.DENY
    assert decision.reasons == ["Task contract has expired"]
