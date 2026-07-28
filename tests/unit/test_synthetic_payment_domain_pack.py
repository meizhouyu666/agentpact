import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from enterprise.domains.synthetic_payment import (
    ChallengeState,
    FaultMode,
    PaymentFacts,
    SyntheticPaymentEnforceHarness,
    SyntheticPaymentError,
    build_manifest,
    require_synthetic_account,
)
from enterprise.domains.synthetic_payment.constants import CAPABILITY_ID
from enterprise.governance.domain_packs import DomainPackKind, DomainPackRegistry


NOW = datetime(2026, 7, 23, 8, 0, tzinfo=timezone.utc)


def _facts(payment_id: str = "pay_1", amount: str = "5000.00") -> PaymentFacts:
    return PaymentFacts(
        payment_id=payment_id,
        beneficiary_id="vendor_1",
        amount=amount,
        currency="CNY",
        reference="synthetic invoice 1",
    )


def _harness() -> SyntheticPaymentEnforceHarness:
    return SyntheticPaymentEnforceHarness(hmac_secret="synthetic-test-secret", clock=lambda: NOW)


def _approved(harness: SyntheticPaymentEnforceHarness, facts: PaymentFacts | None = None):
    operator = require_synthetic_account("operator")
    challenge = harness.prepare_submission(requester=operator, facts=facts or _facts())
    return harness.decide_approval(
        challenge_id=challenge.challenge_id,
        requester=operator,
        approver=require_synthetic_account("approver"),
        approved=True,
    )


def test_manifest_is_synthetic_and_never_production_eligible():
    manifest = build_manifest()
    registry = DomainPackRegistry([manifest])

    assert manifest.kind is DomainPackKind.SYNTHETIC
    assert not manifest.production_eligible
    assert registry.require(manifest.pack_id) == manifest
    assert registry.capability_registry().definitions()[0].capability_id == CAPABILITY_ID


def test_operator_submission_requires_independent_approval_and_confirms_once():
    harness = _harness()
    challenge = _approved(harness)

    assert challenge.state is ChallengeState.READY
    assert challenge.permit is not None and challenge.permit.used_at is None
    completed = harness.execute_submission(challenge_id=challenge.challenge_id)

    assert completed.state is ChallengeState.CONFIRMED
    assert completed.permit is not None and completed.permit.used_at == NOW
    assert completed.result_probe is not None and completed.result_probe.status.value == "confirmed"
    assert harness.store.require("pay_1").commit_count == 1
    with pytest.raises(SyntheticPaymentError, match="no executable"):
        harness.execute_submission(challenge_id=challenge.challenge_id)


def test_requester_cannot_approve_own_payment():
    harness = _harness()
    operator = require_synthetic_account("operator")
    challenge = harness.prepare_submission(requester=operator, facts=_facts())

    with pytest.raises(SyntheticPaymentError, match="cannot approve"):
        harness.decide_approval(
            challenge_id=challenge.challenge_id,
            requester=operator,
            approver=operator,
            approved=True,
        )


def test_critical_payment_requires_compliance_approver():
    harness = _harness()
    operator = require_synthetic_account("operator")
    challenge = harness.prepare_submission(requester=operator, facts=_facts(amount="100000.00"))

    with pytest.raises(SyntheticPaymentError, match="department role"):
        harness.decide_approval(
            challenge_id=challenge.challenge_id,
            requester=operator,
            approver=require_synthetic_account("approver"),
            approved=True,
        )
    approved = harness.decide_approval(
        challenge_id=challenge.challenge_id,
        requester=operator,
        approver=require_synthetic_account("compliance"),
        approved=True,
    )
    assert approved.state is ChallengeState.READY


def test_commit_then_timeout_is_confirmed_by_probe_without_replay():
    harness = _harness()
    challenge = _approved(harness)

    completed = harness.execute_submission(
        challenge_id=challenge.challenge_id,
        fault_mode=FaultMode.COMMIT_THEN_TIMEOUT,
    )

    assert completed.state is ChallengeState.CONFIRMED
    assert harness.store.require("pay_1").commit_count == 1
    assert "timed out" in (completed.attempt.error_message or "")


def test_inconclusive_probe_stays_unknown_until_probe_recovers_without_replay():
    harness = _harness()
    challenge = _approved(harness)

    unknown = harness.execute_submission(
        challenge_id=challenge.challenge_id,
        fault_mode=FaultMode.COMMIT_THEN_INCONCLUSIVE,
    )

    assert unknown.state is ChallengeState.UNKNOWN
    assert harness.resolve_unknown(challenge.challenge_id).state is ChallengeState.UNKNOWN
    assert harness.store.require("pay_1").commit_count == 1
    harness.store.clear_probe_fault("pay_1")
    resolved = harness.resolve_unknown(challenge.challenge_id)
    assert resolved.state is ChallengeState.CONFIRMED
    assert harness.store.require("pay_1").commit_count == 1


def test_definite_precommit_failure_is_failed_and_consumes_permit():
    harness = _harness()
    challenge = _approved(harness)

    failed = harness.execute_submission(
        challenge_id=challenge.challenge_id,
        fault_mode=FaultMode.FAIL_BEFORE_COMMIT,
    )

    assert failed.state is ChallengeState.FAILED
    assert harness.store.require("pay_1").commit_count == 0
    assert failed.permit is not None and failed.permit.used_at == NOW


def test_viewer_cannot_prepare_a_submission():
    with pytest.raises(SyntheticPaymentError, match="operator"):
        _harness().prepare_submission(requester=require_synthetic_account("viewer"), facts=_facts())


def test_expired_capability_grant_invalidates_before_approval():
    current = [NOW]
    harness = SyntheticPaymentEnforceHarness(
        hmac_secret="synthetic-test-secret",
        clock=lambda: current[0],
    )
    operator = require_synthetic_account("operator")
    challenge = harness.prepare_submission(requester=operator, facts=_facts(payment_id="pay-expired-approval"))
    current[0] = NOW + timedelta(minutes=5, seconds=1)

    with pytest.raises(SyntheticPaymentError, match="grant has expired"):
        harness.decide_approval(
            challenge_id=challenge.challenge_id,
            requester=operator,
            approver=require_synthetic_account("approver"),
            approved=True,
        )
    assert harness.get_challenge(challenge.challenge_id).state is ChallengeState.INVALIDATED


def test_authorization_is_revalidated_before_execution():
    current = [NOW]
    harness = SyntheticPaymentEnforceHarness(
        hmac_secret="synthetic-test-secret",
        clock=lambda: current[0],
    )
    operator = require_synthetic_account("operator")
    challenge = harness.prepare_submission(requester=operator, facts=_facts(payment_id="pay-expired-execution"))
    approved = harness.decide_approval(
        challenge_id=challenge.challenge_id,
        requester=operator,
        approver=require_synthetic_account("approver"),
        approved=True,
    )
    current[0] = NOW + timedelta(minutes=5, seconds=1)

    with pytest.raises(SyntheticPaymentError, match="grant has expired"):
        harness.execute_submission(challenge_id=approved.challenge_id)
    assert harness.get_challenge(approved.challenge_id).state is ChallengeState.INVALIDATED
    assert harness.store.require("pay-expired-execution").commit_count == 0


def test_domain_pack_has_no_browser_or_skyvern_imports():
    root = Path(__file__).parents[2] / "enterprise" / "domains" / "synthetic_payment"
    imported_roots = set()
    for source_path in root.glob("*.py"):
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_roots.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_roots.add(node.module.split(".", 1)[0])

    assert imported_roots.isdisjoint({"playwright", "skyvern"})
