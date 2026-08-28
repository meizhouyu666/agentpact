"""Offline contract tests for the explicit Stripe hosted Checkout adapter."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from enterprise.domains.stripe_payment.live_browser import (
    StripeHostedCheckoutError,
    StripeHostedCheckoutFlow,
    StripeHostedCheckoutSession,
    derive_live_idempotency_key,
    parse_stripe_checkout_url,
    stripe_test_key_from_environment,
    validate_stripe_test_key,
)
from enterprise.domains.stripe_payment.models import StripePaymentFacts
from enterprise.governance.result_probes import ResultProbeEvidence, ResultProbeStatus

FACTS = StripePaymentFacts(
    payment_intent_id="pi_live_test_001",
    amount_minor=5000,
    currency="usd",
    description="offline flow test",
)


def test_checkout_url_parser_accepts_only_real_hosted_stripe_urls():
    assert parse_stripe_checkout_url("https://checkout.stripe.com/c/pay/cs_test_123")
    for url in (
        "http://checkout.stripe.com/c/pay/cs_test_123",
        "https://example.test/c/pay/cs_test_123",
        "https://checkout.stripe.com/local/cs_test_123",
        "https://user:pass@checkout.stripe.com/c/pay/cs_test_123",
        "https://checkout.stripe.com:8443/c/pay/cs_test_123",
        "https://checkout.stripe.com:invalid/c/pay/cs_test_123",
    ):
        with pytest.raises(StripeHostedCheckoutError):
            parse_stripe_checkout_url(url)


def test_test_key_guard_reads_environment_and_rejects_live_keys(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_123")
    assert stripe_test_key_from_environment() == "sk_test_123"
    with pytest.raises(RuntimeError, match="sk_test"):
        validate_stripe_test_key("sk_live_123")
    monkeypatch.delenv("STRIPE_SECRET_KEY")
    with pytest.raises(RuntimeError, match="Missing"):
        stripe_test_key_from_environment()


def test_live_idempotency_key_is_stable_and_does_not_contain_business_value():
    first = derive_live_idempotency_key(request_id="request-1", payment_intent_id=FACTS.payment_intent_id)
    second = derive_live_idempotency_key(request_id="request-1", payment_intent_id=FACTS.payment_intent_id)
    assert first == second
    assert FACTS.payment_intent_id not in first


class FakeCheckoutApi:
    api_base = "https://api.stripe.test/v1"

    def __init__(self) -> None:
        self.created = 0
        self.retrieved = 0

    def create_checkout_session(self, **_kwargs):
        self.created += 1
        return StripeHostedCheckoutSession(
            session_id="cs_test_123",
            checkout_url="https://checkout.stripe.com/c/pay/cs_test_123",
        )

    def retrieve_checkout_session(self, *, session_id: str):
        self.retrieved += 1
        assert session_id == "cs_test_123"
        return StripeHostedCheckoutSession(
            session_id=session_id,
            checkout_url="https://checkout.stripe.com/c/pay/cs_test_123",
            payment_intent_id=FACTS.payment_intent_id,
        )


class FakeProbe:
    def __init__(self, **_kwargs) -> None:
        pass

    def probe(self, **_kwargs):
        return ResultProbeEvidence(
            probe_ref="stripe.payment.submit.result-probe.v1",
            status=ResultProbeStatus.UNKNOWN,
            resource_id=FACTS.payment_intent_id,
            checked_at=datetime.now(timezone.utc),
            reasons=["processing"],
        )


class ConfirmedProbe(FakeProbe):
    def probe(self, **_kwargs):
        return ResultProbeEvidence(
            probe_ref="stripe.payment.submit.result-probe.v1",
            status=ResultProbeStatus.CONFIRMED,
            resource_id=FACTS.payment_intent_id,
            checked_at=datetime.now(timezone.utc),
            reasons=["succeeded"],
        )


@pytest.mark.asyncio
async def test_completed_hosted_checkout_uses_independent_probe_and_redacted_evidence(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_123")
    api = FakeCheckoutApi()
    browser_calls: list[str] = []

    async def completed_browser(url: str, *, success_url: str) -> str:
        browser_calls.append(url)
        return "completed"

    monkeypatch.setattr("enterprise.domains.stripe_payment.live_browser.StripeApiResultProbe", ConfirmedProbe)
    key = derive_live_idempotency_key(request_id="request-1", payment_intent_id=FACTS.payment_intent_id)
    flow = StripeHostedCheckoutFlow(
        api_client_factory=lambda _key: api,
        browser_runner=completed_browser,
    )
    result = await flow.execute(facts=FACTS, idempotency_key=key)
    replayed = await flow.execute(facts=FACTS, idempotency_key=key)

    assert result.probe.status is ResultProbeStatus.CONFIRMED
    assert replayed == result
    assert len(browser_calls) == 1
    assert result.evidence.checkout_url_digest != browser_calls[0]
    assert FACTS.payment_intent_id not in result.evidence.idempotency_key_digest


@pytest.mark.asyncio
async def test_unknown_hosted_page_fails_closed_without_browser_replay(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_123")
    api = FakeCheckoutApi()
    browser_calls: list[str] = []

    async def unknown_browser(url: str, *, success_url: str) -> str:
        browser_calls.append(url)
        return "unknown"

    monkeypatch.setattr("enterprise.domains.stripe_payment.live_browser.StripeApiResultProbe", FakeProbe)
    flow = StripeHostedCheckoutFlow(api_client_factory=lambda _key: api, browser_runner=unknown_browser)
    with pytest.raises(StripeHostedCheckoutError, match="no replay"):
        await flow.execute(facts=FACTS, idempotency_key="stripe-payment-live-v1:test")

    assert len(browser_calls) == 1
    assert api.created == 1
    assert api.retrieved == 1


def test_test_key_prefix_without_a_secret_is_rejected():
    with pytest.raises(RuntimeError, match="sk_test"):
        validate_stripe_test_key("sk_test_")
