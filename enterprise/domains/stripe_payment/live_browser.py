"""Explicit Stripe test-mode hosted Checkout flow.

This module is the only browser adapter for the Stripe pack.  It is deliberately
separate from the recorded loopback console: the API creates a real Stripe test
Checkout Session, Playwright visits the returned ``checkout.stripe.com`` URL,
and the final business result is read by :class:`StripeApiResultProbe`.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field

from enterprise.governance.result_probes import ResultProbeEvidence, ResultProbeStatus

from .models import StripePaymentFacts
from .result_probe import (
    STRIPE_API_BASE,
    STRIPE_SECRET_KEY_ENV,
    StripeApiResultProbe,
    StripeProbeError,
    validate_payment_intent_id,
)

STRIPE_CHECKOUT_HOST = "checkout.stripe.com"
STRIPE_CHECKOUT_PATH_PREFIX = "/c/"
DEFAULT_SUCCESS_URL = "https://example.com/agentpact-stripe-success?session_id={CHECKOUT_SESSION_ID}"
DEFAULT_CANCEL_URL = "https://example.com/agentpact-stripe-cancel"


class StripeLiveConfigurationError(RuntimeError):
    """Live flow configuration is missing or unsafe."""


class StripeHostedCheckoutError(RuntimeError):
    """The hosted checkout could not be completed or identified safely."""


def validate_stripe_test_key(key: str | None) -> str:
    """Accept only a non-empty Stripe test secret; never accept a live key."""
    if not key:
        raise StripeLiveConfigurationError(
            f"Missing {STRIPE_SECRET_KEY_ENV}; hosted Checkout requires a sk_test_* key"
        )
    if not key.startswith("sk_test_") or len(key) <= len("sk_test_"):
        raise StripeLiveConfigurationError(
            f"{STRIPE_SECRET_KEY_ENV} must start with sk_test_; refusing live Stripe credentials"
        )
    return key


def stripe_test_key_from_environment(environ: Mapping[str, str] | None = None) -> str:
    """Read the live secret exclusively from the process environment."""
    values = environ if environ is not None else os.environ
    return validate_stripe_test_key(values.get(STRIPE_SECRET_KEY_ENV))


def derive_live_idempotency_key(*, request_id: str, payment_intent_id: str) -> str:
    """Return a stable, bounded key for one governed Stripe submission."""
    digest = hashlib.sha256(f"{request_id}|{payment_intent_id}".encode("utf-8")).hexdigest()
    return f"stripe-payment-live-v1:{digest}"


def parse_stripe_checkout_url(url: str) -> str:
    """Validate and return a hosted Stripe Checkout URL.

    A local console, arbitrary redirect, or non-HTTPS URL is never a live target.
    """
    parsed = urlparse(url)
    try:
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise StripeHostedCheckoutError("Stripe Checkout Session URL has an invalid authority") from exc
    if (
        parsed.scheme != "https"
        or hostname != STRIPE_CHECKOUT_HOST
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
    ):
        raise StripeHostedCheckoutError("Stripe Checkout Session URL is not a hosted HTTPS Stripe URL")
    if not parsed.path.startswith(STRIPE_CHECKOUT_PATH_PREFIX) or len(parsed.path) <= len(STRIPE_CHECKOUT_PATH_PREFIX):
        raise StripeHostedCheckoutError("Stripe Checkout Session URL has an unknown hosted path")
    return url


class StripeHostedCheckoutSession(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str = Field(min_length=1)
    checkout_url: str = Field(min_length=1)
    payment_intent_id: str | None = None


class StripeHostedCheckoutEvidence(BaseModel):
    """Redacted browser evidence safe to persist in reports."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str
    checkout_url_digest: str
    payment_intent_id: str | None
    idempotency_key_digest: str
    browser_state: str
    probe_status: ResultProbeStatus
    captured_at: datetime
    evidence_path: str | None = None


class StripeHostedCheckoutResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    session: StripeHostedCheckoutSession
    browser_state: str
    probe: ResultProbeEvidence
    evidence: StripeHostedCheckoutEvidence


class HostedCheckoutApi(Protocol):
    def create_checkout_session(
        self,
        *,
        facts: StripePaymentFacts,
        idempotency_key: str,
        success_url: str,
        cancel_url: str,
    ) -> StripeHostedCheckoutSession: ...

    def retrieve_checkout_session(self, *, session_id: str) -> StripeHostedCheckoutSession: ...


class StripeTestApiClient:
    """Small API client used only to prepare and retrieve a hosted test Session."""

    def __init__(self, *, secret_key: str, api_base: str = STRIPE_API_BASE, timeout: float = 15.0) -> None:
        self._secret_key = validate_stripe_test_key(secret_key)
        self.api_base = api_base.rstrip("/")
        self._timeout = timeout

    def create_checkout_session(
        self,
        *,
        facts: StripePaymentFacts,
        idempotency_key: str,
        success_url: str,
        cancel_url: str,
    ) -> StripeHostedCheckoutSession:
        data = {
            "mode": "payment",
            "line_items[0][price_data][currency]": facts.currency,
            "line_items[0][price_data][unit_amount]": str(facts.amount_minor),
            "line_items[0][price_data][product_data][name]": facts.description or "AgentPact Stripe test payment",
            "line_items[0][quantity]": "1",
            "payment_method_types[0]": "card",
            "success_url": success_url,
            "cancel_url": cancel_url,
            "metadata[payment_intent_id]": facts.payment_intent_id,
        }
        payload = self._request(
            "POST",
            "/checkout/sessions",
            data=data,
            headers={"Idempotency-Key": idempotency_key},
        )
        try:
            session_id = str(payload["id"])
            checkout_url = parse_stripe_checkout_url(str(payload["url"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise StripeHostedCheckoutError("Stripe Checkout Session response is missing a hosted URL") from exc
        return StripeHostedCheckoutSession(session_id=session_id, checkout_url=checkout_url)

    def retrieve_checkout_session(self, *, session_id: str) -> StripeHostedCheckoutSession:
        if not session_id.startswith("cs_") or not re.fullmatch(r"cs_[A-Za-z0-9_]+", session_id):
            raise StripeHostedCheckoutError("Stripe Checkout Session id has an invalid format")
        payload = self._request(
            "GET",
            f"/checkout/sessions/{session_id}",
            params={"expand[]": "payment_intent"},
        )
        payment_intent = payload.get("payment_intent")
        payment_intent_id = payment_intent.get("id") if isinstance(payment_intent, dict) else payment_intent
        if not isinstance(payment_intent_id, str) or not payment_intent_id.startswith("pi_"):
            raise StripeHostedCheckoutError("Completed Stripe Checkout Session did not expose a PaymentIntent")
        try:
            validate_payment_intent_id(payment_intent_id)
        except StripeProbeError as exc:
            raise StripeHostedCheckoutError("Completed Stripe Checkout Session exposed an invalid PaymentIntent") from exc
        return StripeHostedCheckoutSession(
            session_id=str(payload.get("id", session_id)),
            checkout_url=str(payload.get("url", "https://checkout.stripe.com/c/unknown")),
            payment_intent_id=payment_intent_id,
        )

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._secret_key}", **kwargs.pop("headers", {})}
        try:
            response = httpx.request(
                method,
                f"{self.api_base}{path}",
                headers=headers,
                timeout=self._timeout,
                **kwargs,
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise StripeHostedCheckoutError(f"Stripe API transport failed: {type(exc).__name__}") from exc
        if response.status_code in {401, 403}:
            raise StripeLiveConfigurationError("Stripe test credentials were rejected")
        if response.status_code >= 400:
            raise StripeHostedCheckoutError(f"Stripe API returned HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise StripeHostedCheckoutError("Stripe API returned malformed JSON") from exc
        if not isinstance(payload, dict):
            raise StripeHostedCheckoutError("Stripe API returned a non-object response")
        return payload


class BrowserRunner(Protocol):
    async def __call__(self, checkout_url: str, *, success_url: str) -> str: ...


async def run_stripe_test_checkout(checkout_url: str, *, success_url: str, headless: bool = True) -> str:
    """Complete the Stripe hosted page with Stripe's documented 4242 test card."""
    parse_stripe_checkout_url(checkout_url)
    try:
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise StripeHostedCheckoutError("Playwright is required for the explicit live browser flow") from exc

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=headless)
        try:
            page = await browser.new_page()
            await page.goto(checkout_url, wait_until="domcontentloaded", timeout=30_000)
            _require_hosted_checkout_page(page.url)
            await _fill_checkout_field(
                page,
                ["input[autocomplete='cc-number']", "input[name='cardnumber']", "input[name='cardNumber']", "input[placeholder*='Card number']"],
                "4242424242424242",
            )
            await _fill_checkout_field(
                page,
                ["input[autocomplete='cc-exp']", "input[name='exp-date']", "input[name='cardExpiry']", "input[placeholder*='MM']"],
                "12/34",
            )
            await _fill_checkout_field(
                page,
                ["input[autocomplete='cc-csc']", "input[name='cvc']", "input[name='cardCvc']", "input[placeholder*='CVC']"],
                "123",
            )
            await _fill_optional_checkout_field(page, ["input[autocomplete='email']", "input[type='email']"], "stripe-test@example.com")
            await _click_payment_button(page)
            for _ in range(10):
                current_url = page.url
                body_text = (await page.locator("body").inner_text()).lower()
                if _success_redirect(current_url, success_url) or "payment successful" in body_text or "thank you" in body_text:
                    return "completed"
                _require_hosted_checkout_page(current_url)
                await page.wait_for_timeout(1_000)
            return "unknown"
        except PlaywrightTimeoutError as exc:
            raise StripeHostedCheckoutError("Stripe hosted checkout timed out or showed an unknown page") from exc
        finally:
            await browser.close()


async def _fill_checkout_field(page: Any, selectors: list[str], value: str) -> None:
    for frame in [page, *page.frames]:
        for selector in selectors:
            locator = frame.locator(selector).first
            try:
                if await locator.is_visible(timeout=1_000):
                    await locator.fill(value)
                    return
            except Exception:
                continue
    raise StripeHostedCheckoutError("Stripe hosted checkout card field was not found")


async def _fill_optional_checkout_field(page: Any, selectors: list[str], value: str) -> None:
    try:
        await _fill_checkout_field(page, selectors, value)
    except StripeHostedCheckoutError:
        return


async def _click_payment_button(page: Any) -> None:
    for selector in ["button[type='submit']", "button:has-text('Pay')", "button:has-text('Subscribe')"]:
        button = page.locator(selector).first
        try:
            if await button.is_visible(timeout=1_000):
                await button.click()
                return
        except Exception:
            continue
    raise StripeHostedCheckoutError("Stripe hosted checkout submit button was not found")


def _success_redirect(current_url: str, success_url: str) -> bool:
    expected = success_url.split("{CHECKOUT_SESSION_ID}", 1)[0]
    return current_url.startswith(expected)


def _require_hosted_checkout_page(url: str) -> None:
    try:
        parse_stripe_checkout_url(url)
    except StripeHostedCheckoutError as exc:
        raise StripeHostedCheckoutError("Stripe browser reached an unknown or non-hosted page") from exc


class StripeHostedCheckoutFlow:
    """Create, drive, and independently verify one Stripe test Checkout flow."""

    def __init__(
        self,
        *,
        api_client_factory: Callable[[str], HostedCheckoutApi] | None = None,
        browser_runner: BrowserRunner | None = None,
        evidence_dir: str | Path | None = None,
        headless: bool = True,
    ) -> None:
        self._api_client_factory = api_client_factory or (lambda key: StripeTestApiClient(secret_key=key))
        self._browser_runner = browser_runner or (lambda url, *, success_url: run_stripe_test_checkout(url, success_url=success_url, headless=headless))
        self._evidence_dir = Path(evidence_dir) if evidence_dir is not None else None
        self._results: dict[str, StripeHostedCheckoutResult] = {}
        self._started_keys: set[str] = set()

    async def execute(
        self,
        *,
        facts: StripePaymentFacts,
        idempotency_key: str,
        success_url: str = DEFAULT_SUCCESS_URL,
        cancel_url: str = DEFAULT_CANCEL_URL,
    ) -> StripeHostedCheckoutResult:
        if not idempotency_key:
            raise StripeHostedCheckoutError("Stripe hosted Checkout requires a non-empty idempotency key")
        prior = self._results.get(idempotency_key)
        if prior is not None:
            return prior
        if idempotency_key in self._started_keys:
            raise StripeHostedCheckoutError(
                "Stripe hosted Checkout already started for this idempotency key; no replay is allowed"
            )
        self._started_keys.add(idempotency_key)
        key = stripe_test_key_from_environment()
        api = self._api_client_factory(key)
        session = api.create_checkout_session(
            facts=facts,
            idempotency_key=idempotency_key,
            success_url=success_url,
            cancel_url=cancel_url,
        )
        parse_stripe_checkout_url(session.checkout_url)
        browser_state = await self._browser_runner(session.checkout_url, success_url=success_url)
        completed_session = api.retrieve_checkout_session(session_id=session.session_id)
        if not completed_session.payment_intent_id:
            raise StripeHostedCheckoutError("Stripe hosted checkout did not return a PaymentIntent")
        probe = StripeApiResultProbe(secret_key=key, api_base=getattr(api, "api_base", STRIPE_API_BASE))
        probe_evidence = probe.probe(
            resource_id=completed_session.payment_intent_id,
            idempotency_key=idempotency_key,
        )
        evidence = self._build_evidence(
            session=completed_session,
            browser_state=browser_state,
            probe=probe_evidence,
            idempotency_key=idempotency_key,
        )
        if browser_state != "completed":
            raise StripeHostedCheckoutError(
                "Stripe hosted checkout ended in an unknown page state after the independent probe; no replay is allowed"
            )
        result = StripeHostedCheckoutResult(
            session=completed_session,
            browser_state=browser_state,
            probe=probe_evidence,
            evidence=evidence,
        )
        self._results[idempotency_key] = result
        return result

    def result_for(self, idempotency_key: str) -> StripeHostedCheckoutResult | None:
        return self._results.get(idempotency_key)

    def _build_evidence(
        self,
        *,
        session: StripeHostedCheckoutSession,
        browser_state: str,
        probe: ResultProbeEvidence,
        idempotency_key: str,
    ) -> StripeHostedCheckoutEvidence:
        evidence = StripeHostedCheckoutEvidence(
            session_id=session.session_id,
            checkout_url_digest=_digest(session.checkout_url),
            payment_intent_id=session.payment_intent_id,
            idempotency_key_digest=_digest(idempotency_key),
            browser_state=browser_state,
            probe_status=probe.status,
            captured_at=datetime.now(timezone.utc),
        )
        if self._evidence_dir is None:
            return evidence
        self._evidence_dir.mkdir(parents=True, exist_ok=True)
        path = self._evidence_dir / f"stripe-hosted-checkout-{_digest(idempotency_key)[:16]}.json"
        path.write_text(evidence.model_dump_json(indent=2), encoding="utf-8")
        return evidence.model_copy(update={"evidence_path": str(path)})

    async def probe_existing(self, *, result: StripeHostedCheckoutResult, idempotency_key: str) -> ResultProbeEvidence:
        """Re-read UNKNOWN through the independent probe; this never reopens Checkout."""
        if result.probe.status is not ResultProbeStatus.UNKNOWN:
            raise ValueError("Only an unknown Stripe hosted checkout result can be probed")
        key = stripe_test_key_from_environment()
        if not result.session.payment_intent_id:
            raise StripeProbeError("Cannot re-probe a hosted Checkout Session without a PaymentIntent")
        probe = StripeApiResultProbe(secret_key=key)
        return probe.probe(resource_id=result.session.payment_intent_id, idempotency_key=idempotency_key)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
