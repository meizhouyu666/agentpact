"""Deterministic tests for the Stripe enforce harness: governed lifecycle,
fault injection, UNKNOWN recovery, no-replay, and authorization invalidation.

Network-free: the simulated Stripe backend and its recorded probe are in
memory. The live Stripe API is never contacted.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from enterprise.domains.stripe_payment.accounts import require_stripe_account
from enterprise.domains.stripe_payment.harness import ChallengeState, StripePaymentEnforceHarness
from enterprise.domains.stripe_payment.models import (
    StripeOutcome,
    StripePaymentError,
    StripePaymentFacts,
)
from enterprise.domains.stripe_payment.store import StripeFaultMode, StripePaymentStore

NOW = datetime(2026, 7, 29, 11, 30, tzinfo=timezone.utc)


class _Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


def _facts(**overrides: object) -> StripePaymentFacts:
    values: dict[str, object] = {
        "payment_intent_id": "pi_harness_001",
        "customer_id": "cus_harness_001",
        "amount_minor": 5000,
        "currency": "usd",
        "description": "Stripe harness test payment",
        "object_version": 1,
    }
    values.update(overrides)
    return StripePaymentFacts.model_validate(values)


def _harness(*, clock: _Clock | None = None, store: StripePaymentStore | None = None):
    return StripePaymentEnforceHarness(
        hmac_secret="stripe-test-hmac",
        store=store,
        clock=clock or _Clock(),
    )


def _prepare_approve_execute(
    harness: StripePaymentEnforceHarness,
    *,
    outcome: StripeOutcome = StripeOutcome.SUCCEEDED,
    fault_mode: StripeFaultMode = StripeFaultMode.NONE,
    facts: StripePaymentFacts | None = None,
):
    facts = facts or _facts()
    challenge = harness.prepare_submission(
        requester=require_stripe_account("operator"),
        facts=facts,
    )
    approved = harness.decide_approval(
        challenge_id=challenge.challenge_id,
        requester=require_stripe_account("operator"),
        approver=require_stripe_account("approver"),
        approved=True,
    )
    return harness.execute_submission(
        challenge_id=approved.challenge_id,
        outcome=outcome,
        fault_mode=fault_mode,
    )


def test_happy_path_confirmed_once_with_durable_attempt_and_audit_trail():
    harness = _harness()
    result = _prepare_approve_execute(harness)

    assert result.state is ChallengeState.CONFIRMED
    assert result.attempt is not None and result.attempt.status.value == "confirmed"
    assert result.result_probe is not None and result.result_probe.status.value == "confirmed"
    assert harness.store.require("pi_harness_001").commit_count == 1
    assert [event.event_type for event in harness.audit_events] == [
        "approval_requested",
        "permit_issued",
        "attempt_executing",
        "attempt_confirmed",
    ]
    assert result.work_order.prohibited_operations == {"delete", "change_amount_after_approval"}


def test_commit_then_timeout_is_resolved_by_probe_and_never_replayed():
    harness = _harness()
    result = _prepare_approve_execute(harness, fault_mode=StripeFaultMode.COMMIT_THEN_TIMEOUT)

    # Commit happened and the independent probe can already confirm it.
    assert result.state is ChallengeState.CONFIRMED
    assert harness.store.require("pi_harness_001").commit_count == 1
    # Replay is refused: the challenge is no longer in READY state.
    with pytest.raises(StripePaymentError, match="no executable approval and permit"):
        harness.execute_submission(challenge_id=result.challenge_id)
    assert harness.store.require("pi_harness_001").commit_count == 1


def test_commit_then_inconclusive_enters_unknown_and_probe_resolves_it():
    clock = _Clock()
    harness = _harness(clock=clock)
    result = _prepare_approve_execute(harness, fault_mode=StripeFaultMode.COMMIT_THEN_INCONCLUSIVE)

    assert result.state is ChallengeState.UNKNOWN
    assert harness.store.require("pi_harness_001").commit_count == 1
    # Probe keeps answering UNKNOWN while the authoritative read is unavailable.
    still_unknown = harness.resolve_unknown(result.challenge_id)
    assert still_unknown.state is ChallengeState.UNKNOWN
    # Replay during UNKNOWN is forbidden.
    with pytest.raises(StripePaymentError, match="no executable approval and permit"):
        harness.execute_submission(challenge_id=result.challenge_id)
    # The independent probe (not a retry) resolves the attempt.
    harness.store.clear_probe_fault("pi_harness_001")
    resolved = harness.resolve_unknown(result.challenge_id)
    assert resolved.state is ChallengeState.CONFIRMED
    assert harness.store.require("pi_harness_001").commit_count == 1


def test_fail_before_commit_is_definite_failure_without_side_effect():
    harness = _harness()
    result = _prepare_approve_execute(harness, fault_mode=StripeFaultMode.FAIL_BEFORE_COMMIT)

    assert result.state is ChallengeState.FAILED
    assert harness.store.require("pi_harness_001").commit_count == 0
    assert "before commit" in (result.attempt.error_message or "")


def test_processing_outcome_enters_unknown_and_stays_until_authoritative_state_settles():
    harness = _harness()
    result = _prepare_approve_execute(harness, outcome=StripeOutcome.PROCESSING)

    assert result.state is ChallengeState.UNKNOWN
    assert result.result_probe.metadata["outcome"] == "processing"
    still_unknown = harness.resolve_unknown(result.challenge_id)
    assert still_unknown.state is ChallengeState.UNKNOWN
    assert [event.event_type for event in harness.audit_events][-1] == "result_still_unknown"


def test_canceled_outcome_fails_closed_after_probe_says_no_submission():
    harness = _harness()
    result = _prepare_approve_execute(harness, outcome=StripeOutcome.CANCELED)

    assert result.state is ChallengeState.UNKNOWN
    resolved = harness.resolve_unknown(result.challenge_id)
    assert resolved.state is ChallengeState.FAILED
    assert "confirmed no submission" in (resolved.attempt.error_message or "")


def test_permit_expiry_invalidates_the_challenge():
    clock = _Clock()
    harness = _harness(clock=clock)
    challenge = harness.prepare_submission(requester=require_stripe_account("operator"), facts=_facts())
    approved = harness.decide_approval(
        challenge_id=challenge.challenge_id,
        requester=require_stripe_account("operator"),
        approver=require_stripe_account("approver"),
        approved=True,
    )
    clock.advance(seconds=61)
    with pytest.raises(StripePaymentError, match="permit is expired"):
        harness.execute_submission(challenge_id=approved.challenge_id)
    assert harness.get_challenge(approved.challenge_id).state is ChallengeState.INVALIDATED
    assert harness.store.require("pi_harness_001").commit_count == 0


def test_out_of_band_state_change_invalidates_before_side_effect():
    harness = _harness()
    challenge = harness.prepare_submission(requester=require_stripe_account("operator"), facts=_facts())
    approved = harness.decide_approval(
        challenge_id=challenge.challenge_id,
        requester=require_stripe_account("operator"),
        approver=require_stripe_account("approver"),
        approved=True,
    )
    # Simulate another system mutating the record after authorization.
    store = harness.store
    record = store.require("pi_harness_001")
    record.facts = record.facts.model_copy(update={"description": "tampered out-of-band"})
    store._records["pi_harness_001"] = record

    with pytest.raises(StripePaymentError, match="changed after authorization"):
        harness.execute_submission(challenge_id=approved.challenge_id)
    assert harness.get_challenge(approved.challenge_id).state is ChallengeState.INVALIDATED
    assert store.require("pi_harness_001").commit_count == 0


def test_critical_amount_routes_approval_to_compliance_and_rejects_operator_approval():
    clock = _Clock()
    harness = _harness(clock=clock)
    critical = _facts(amount_minor=1_000_000)
    challenge = harness.prepare_submission(requester=require_stripe_account("operator"), facts=critical)

    assert challenge.decision.risk_level == "critical"
    assert challenge.decision.required_approver == {
        "department_id": "stripe_compliance",
        "role": "approver",
    }
    with pytest.raises(StripePaymentError, match="Approver lacks"):
        harness.decide_approval(
            challenge_id=challenge.challenge_id,
            requester=require_stripe_account("operator"),
            approver=require_stripe_account("approver"),
            approved=True,
        )
    approved = harness.decide_approval(
        challenge_id=challenge.challenge_id,
        requester=require_stripe_account("operator"),
        approver=require_stripe_account("compliance"),
        approved=True,
    )
    assert approved.state is ChallengeState.READY


def test_separation_of_duties_forbids_requester_self_approval():
    harness = _harness()
    challenge = harness.prepare_submission(requester=require_stripe_account("operator"), facts=_facts())
    with pytest.raises(StripePaymentError, match="Requester cannot approve"):
        harness.decide_approval(
            challenge_id=challenge.challenge_id,
            requester=require_stripe_account("operator"),
            approver=require_stripe_account("operator"),
            approved=True,
        )


def test_only_operator_can_request_submission():
    harness = _harness()
    with pytest.raises(StripePaymentError, match="operator"):
        harness.prepare_submission(requester=require_stripe_account("viewer"), facts=_facts())


def test_rejected_approval_never_reaches_execution():
    harness = _harness()
    challenge = harness.prepare_submission(requester=require_stripe_account("operator"), facts=_facts())
    rejected = harness.decide_approval(
        challenge_id=challenge.challenge_id,
        requester=require_stripe_account("operator"),
        approver=require_stripe_account("approver"),
        approved=False,
    )
    assert rejected.state is ChallengeState.REJECTED
    with pytest.raises(StripePaymentError, match="no executable approval and permit"):
        harness.execute_submission(challenge_id=rejected.challenge_id)
    assert harness.store.require("pi_harness_001").commit_count == 0


def test_store_rejects_version_change_and_double_submit_with_different_key():
    store = StripePaymentStore()
    store.create_draft(facts=_facts(), requester_user_id="stripe_operator")
    with pytest.raises(StripePaymentError, match="object version changed"):
        store.submit(
            payment_intent_id="pi_harness_001",
            expected_version=2,
            approval_id="approval-x",
            idempotency_key="stripe:one",
        )
    store.submit(
        payment_intent_id="pi_harness_001",
        expected_version=1,
        approval_id="approval-x",
        idempotency_key="stripe:one",
    )
    assert store.require("pi_harness_001").commit_count == 1
    # Same idempotency key is a safe no-op; a different key on a non-draft is rejected.
    store.submit(
        payment_intent_id="pi_harness_001",
        expected_version=2,
        approval_id="approval-x",
        idempotency_key="stripe:one",
    )
    assert store.require("pi_harness_001").commit_count == 1
    with pytest.raises(StripePaymentError, match="no longer a draft"):
        store.submit(
            payment_intent_id="pi_harness_001",
            expected_version=2,
            approval_id="approval-x",
            idempotency_key="stripe:two",
        )
