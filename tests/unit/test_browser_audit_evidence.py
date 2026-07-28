import json

import pytest

from enterprise.governance.browser_audit import build_browser_audit_manifest
from enterprise.governance.contracts import DecisionOutcome, PageReadiness


def _snapshot(*, readiness: str = "ready") -> dict:
    return {
        "page_marker": "synthetic-payment-console",
        "domain_pack": "synthetic.payment",
        "readiness": readiness,
        "aria_busy": False,
        "fields": [
            {
                "field_name": "payment_id",
                "element_id": "payment-id",
                "tag_name": "input",
                "role": None,
                "value_present": True,
            },
            {
                "field_name": "amount",
                "element_id": "amount",
                "tag_name": "input",
                "role": None,
                "value_present": True,
            },
        ],
        "actions": [
            {"semantic_action": "create_challenge", "element_id": "create-challenge", "enabled": True},
            {"semantic_action": "approve_payment", "element_id": "approve-payment", "enabled": False},
            {"semantic_action": "execute_payment", "element_id": "execute-payment", "enabled": False},
            {"semantic_action": "probe_payment_result", "element_id": "probe-result", "enabled": False},
        ],
    }


def test_browser_manifest_is_redacted_and_classifies_semantic_actions():
    manifest = build_browser_audit_manifest(
        page_url="http://127.0.0.1:18081/",
        scenario_id="synthetic-payment-initial",
        task_id="task-browser-audit",
        step_id="step-observe",
        html="<html><input value='pay-secret-001'></html>",
        screenshot=b"synthetic-png-bytes",
        dom_snapshot=_snapshot(),
        hmac_secret="browser-audit-test-secret",
        page_title="Synthetic Payment Console",
    )

    payload = json.dumps(manifest.model_dump(mode="json"), ensure_ascii=False)
    assert manifest.schema_version == "phase2-browser-audit-v1"
    assert manifest.readiness is PageReadiness.READY
    assert all(len(field.field_ref) == 64 and len(field.element_ref) == 64 for field in manifest.dom_field_refs)
    assert all(len(action.element_ref) == 64 for action in manifest.action_candidates)
    assert "payment_id" not in payload
    assert "payment-id" not in payload
    assert "http://127.0.0.1:18081/" not in payload
    assert "pay-secret-001" not in payload
    assert "synthetic-png-bytes" not in payload
    assert manifest.redaction_summary["raw_html_persisted"] is False
    assert manifest.redaction_summary["raw_screenshot_persisted"] is False
    assert manifest.redaction_summary["page_url_persisted"] is False
    assert manifest.redaction_summary["semantic_names_persisted"] is False
    assert len(manifest.action_fingerprints) == 4

    decisions = {
        action.semantic_action: decision
        for action, decision in zip(manifest.action_candidates, manifest.policy_decisions)
    }
    assert decisions["execute_payment"].outcome is DecisionOutcome.REQUIRE_APPROVAL
    assert decisions["execute_payment"].risk_level == "critical"


def test_loading_observation_requires_human_for_high_impact_candidate():
    snapshot = _snapshot(readiness="loading")
    manifest = build_browser_audit_manifest(
        page_url="http://127.0.0.1:18081/",
        scenario_id="synthetic-payment-loading",
        task_id="task-browser-audit",
        step_id="step-loading",
        html="<html data-state='loading'></html>",
        screenshot=b"png",
        dom_snapshot=snapshot,
        hmac_secret="browser-audit-test-secret",
    )

    assert manifest.readiness is PageReadiness.LOADING
    execute_decision = next(
        decision
        for action, decision in zip(manifest.action_candidates, manifest.policy_decisions)
        if action.semantic_action == "execute_payment"
    )
    assert execute_decision.outcome is DecisionOutcome.NEEDS_HUMAN


def test_unsettled_network_observation_is_transitioning_and_requires_human():
    manifest = build_browser_audit_manifest(
        page_url="http://127.0.0.1:18081/",
        scenario_id="synthetic-payment-network-unsettled",
        task_id="task-browser-audit",
        step_id="step-network-unsettled",
        html="<html></html>",
        screenshot=b"png",
        dom_snapshot=_snapshot(),
        hmac_secret="browser-audit-test-secret",
        network_idle_reached=False,
    )

    assert manifest.readiness is PageReadiness.TRANSITIONING
    assert manifest.network_idle_reached is False
    execute_decision = next(
        decision
        for action, decision in zip(manifest.action_candidates, manifest.policy_decisions)
        if action.semantic_action == "execute_payment"
    )
    assert execute_decision.outcome is DecisionOutcome.NEEDS_HUMAN


def test_browser_manifest_rejects_untrusted_url_or_page_marker():
    with pytest.raises(ValueError, match="localhost console"):
        build_browser_audit_manifest(
            page_url="https://example.com/",
            scenario_id="untrusted",
            task_id="task",
            step_id="step",
            html="<html></html>",
            screenshot=b"png",
            dom_snapshot=_snapshot(),
            hmac_secret="browser-audit-test-secret",
        )

    untrusted = _snapshot()
    untrusted["domain_pack"] = "other.pack"
    with pytest.raises(ValueError, match="trusted synthetic page marker"):
        build_browser_audit_manifest(
            page_url="http://127.0.0.1:18081/",
            scenario_id="untrusted",
            task_id="task",
            step_id="step",
            html="<html></html>",
            screenshot=b"png",
            dom_snapshot=untrusted,
            hmac_secret="browser-audit-test-secret",
        )
