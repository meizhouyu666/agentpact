"""API smoke tests for the Stripe test checkout console.

Verifies the two-channel contract: the checkout channel drives the governed
flow, and the authoritative read channel answers independently from the
simulated backend. No network, no credentials.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from enterprise.domains.stripe_payment.app import create_app
from enterprise.domains.stripe_payment.harness import ChallengeState, StripePaymentEnforceHarness


def _client() -> TestClient:
    return TestClient(create_app(StripePaymentEnforceHarness(hmac_secret="stripe-api-test-hmac")))


def test_health_reports_stripe_pack_not_production_eligible():
    with _client() as client:
        health = client.get("/health").json()
    assert health == {"status": "ready", "domain_pack": "stripe.payment", "production_eligible": False}


def test_checkout_channel_full_governed_round_trip():
    with _client() as client:
        created = client.post(
            "/api/checkout/sessions",
            json={
                "payment_intent_id": "pi_api_001",
                "customer_id": "cus_api_001",
                "amount_minor": 5000,
                "currency": "usd",
                "description": "API round trip",
            },
        ).json()
        assert created["state"] == ChallengeState.PENDING_APPROVAL.value
        challenge_id = created["challenge_id"]

        approved = client.post(
            f"/api/checkout/sessions/{challenge_id}/approval",
            json={"approver_account": "approver", "approved": True},
        ).json()
        assert approved["state"] == ChallengeState.READY.value
        assert approved["permit"]["permit_id"]

        executed = client.post(
            f"/api/checkout/sessions/{challenge_id}/execute",
            json={"fault_mode": "none", "outcome": "succeeded"},
        ).json()
        assert executed["state"] == ChallengeState.CONFIRMED.value
        assert executed["attempt"]["status"] == "confirmed"

        authoritative = client.get("/v1/payment_intents/pi_api_001").json()
        assert authoritative["status"] == "submitted"
        assert authoritative["outcome"] == "succeeded"
        assert authoritative["commit_count"] == 1


def test_authoritative_read_channel_is_separate_and_answers_404_for_unknown():
    with _client() as client:
        missing = client.get("/v1/payment_intents/pi_does_not_exist")
    assert missing.status_code == 404


def test_commit_then_inconclusive_round_trip_resolves_only_after_probe():
    with _client() as client:
        created = client.post(
            "/api/checkout/sessions",
            json={
                "payment_intent_id": "pi_api_002",
                "amount_minor": 5000,
                "currency": "usd",
                "description": "API unknown round trip",
            },
        ).json()
        challenge_id = created["challenge_id"]
        client.post(
            f"/api/checkout/sessions/{challenge_id}/approval",
            json={"approver_account": "approver", "approved": True},
        )
        executed = client.post(
            f"/api/checkout/sessions/{challenge_id}/execute",
            json={"fault_mode": "commit_then_inconclusive", "outcome": "succeeded"},
        ).json()
        assert executed["state"] == ChallengeState.UNKNOWN.value

        client.post("/api/payment_intents/pi_api_002/clear-probe-fault", json={})
        resolved = client.post(f"/api/checkout/sessions/{challenge_id}/probe", json={}).json()
        assert resolved["state"] == ChallengeState.CONFIRMED.value
        assert client.get("/v1/payment_intents/pi_api_002").json()["commit_count"] == 1
