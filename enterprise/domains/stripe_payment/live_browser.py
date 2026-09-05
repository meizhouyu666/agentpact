"""Explicit Stripe test-mode hosted Checkout flow.

This module is the only browser adapter for the Stripe pack.  It is deliberately
separate from the recorded loopback console: the API creates a real Stripe test
Checkout Session, Playwright visits the returned ``checkout.stripe.com`` URL,
and the final business result is read by :class:`StripeApiResultProbe`.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from enterprise.browser_loop.contracts import (
    ActionDecision,
    ActionKind,
    BrowserAction,
    BrowserActionResult,
    BrowserLoopConfig,
    BrowserLoopRunContext,
    BrowserLoopStatus,
    BrowserObservation,
    BrowserSessionMode,
    BrowserSessionPolicy,
    DecisionKind,
    ModelInput,
    PolicyAuthorization,
    PolicyDisposition,
    VerificationDisposition,
    VerificationRequest,
    VerificationResult,
)
from enterprise.browser_loop.integrations import SqlAlchemyBrowserLoopEventSink
from enterprise.browser_loop.loop import AgentPactBrowserLoop
from enterprise.browser_loop.persisted_executor import PersistedBrowserExecutor
from enterprise.browser_loop.ports import PreflightBrowserRuntime
from enterprise.governance.contracts import DecisionOutcome, ExecutionAuthorization, ExecutionEffect, PolicyDecision
from enterprise.governance.execution_attempt_service import resolve_unknown_execution_attempt
from enterprise.governance.execution_profiles import ExecutionMechanism, ExecutionProfile
from enterprise.governance.models import ExecutionAttemptModel
from enterprise.governance.pack_runtime import ExecutionCheckpoint, PackRuntimeBinding
from enterprise.governance.result_probes import ResultProbeEvidence, ResultProbeStatus

from .constants import CAPABILITY_ID, PACK_ID, PACK_VERSION, RESULT_PROBE_REF
from .models import StripePaymentFacts
from .result_probe import (
    STRIPE_API_BASE,
    STRIPE_SECRET_KEY_ENV,
    StripeApiResultProbe,
    StripeProbeError,
    build_probe_evidence,
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

    def __init__(self, message: str, *, diagnostic: "StripeBrowserDiagnostic | None" = None) -> None:
        super().__init__(message)
        self.diagnostic = diagnostic


class StripeBrowserDiagnostic(BaseModel):
    """Small, redacted browser failure summary suitable for reports."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    stage: str
    reason_code: str
    final_url_summary: str | None = None
    error_type: str | None = None
    session_status: str | None = None
    payment_status: str | None = None
    payment_intent_present: bool | None = None
    browser_stage: str | None = None
    browser_reason_code: str | None = None
    browser_final_url_summary: str | None = None
    browser_error_type: str | None = None


class StripeBrowserOutcome(str):
    """String-compatible browser result carrying optional diagnostics."""

    diagnostic: StripeBrowserDiagnostic

    def __new__(cls, state: str, diagnostic: StripeBrowserDiagnostic):
        value = str.__new__(cls, state)
        value.diagnostic = diagnostic
        return value


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
    status: str | None = None
    payment_status: str | None = None


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
    session_status: str | None = None
    payment_status: str | None = None
    payment_intent_present: bool = False
    browser_stage: str | None = None
    browser_reason_code: str | None = None
    browser_final_url_summary: str | None = None
    browser_error_type: str | None = None
    probe_reason_code: str | None = None


class StripeHostedCheckoutResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    session: StripeHostedCheckoutSession
    browser_state: str
    probe: ResultProbeEvidence
    evidence: StripeHostedCheckoutEvidence
    execution_checkpoint: ExecutionCheckpoint | None = None
    execution_status: ResultProbeStatus | None = None
    diagnostic: StripeBrowserDiagnostic | None = None


class GovernedCheckoutRuntimeFactory(Protocol):
    """Create a preflight-capable runtime already attached to hosted Checkout."""

    def __call__(
        self,
        checkout_url: str,
    ) -> PreflightBrowserRuntime | Awaitable[PreflightBrowserRuntime]: ...


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
        returned_id = payload.get("id")
        if returned_id != session_id:
            raise StripeHostedCheckoutError("Stripe Checkout Session response id does not match the requested id")
        payment_intent = payload.get("payment_intent")
        payment_intent_id = payment_intent.get("id") if isinstance(payment_intent, dict) else payment_intent
        if payment_intent_id is not None:
            if not isinstance(payment_intent_id, str) or not payment_intent_id.startswith("pi_"):
                raise StripeHostedCheckoutError("Stripe Checkout Session exposed an invalid PaymentIntent")
            try:
                validate_payment_intent_id(payment_intent_id)
            except StripeProbeError as exc:
                raise StripeHostedCheckoutError("Stripe Checkout Session exposed an invalid PaymentIntent") from exc
        return StripeHostedCheckoutSession(
            session_id=session_id,
            checkout_url=str(payload.get("url", "https://checkout.stripe.com/c/unknown")),
            payment_intent_id=payment_intent_id,
            status=payload.get("status") if isinstance(payload.get("status"), str) else None,
            payment_status=payload.get("payment_status") if isinstance(payload.get("payment_status"), str) else None,
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


def _find_installed_chromium() -> str | None:
    """Locate an installed Chromium so launch does not require a fresh download.

    Mirrors the project's own discovery (``tests/e2e/m4_synthetic_support``)
    without importing test code: honor ``FINRPA_CHROMIUM_EXECUTABLE`` and the
    standard Playwright browser roots. Returns None to let Playwright use its
    pinned default (which then reports the usual install hint if absent).
    """
    override = os.environ.get("FINRPA_CHROMIUM_EXECUTABLE")
    if override and Path(override).is_file():
        return str(Path(override).resolve())
    roots: list[Path] = []
    browser_root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if browser_root and browser_root != "0":
        roots.append(Path(browser_root))
    elif os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        roots.append(Path(os.environ["LOCALAPPDATA"]) / "ms-playwright")
    else:
        roots.append(Path.home() / ".cache" / "ms-playwright")
    patterns = (
        "chromium-*/chrome-win/chrome.exe",
        "chromium-*/chrome-linux/chrome",
        "chromium-*/chrome-mac/Chromium.app/Contents/MacOS/Chromium",
    )
    for root in roots:
        for pattern in patterns:
            for candidate in sorted(root.glob(pattern), reverse=True):
                if candidate.is_file():
                    return str(candidate.resolve())
    return None


def _url_summary(url: str | None, *, success_url: str | None = None) -> str | None:
    if not url:
        return None
    try:
        parsed = urlparse(url)
    except ValueError:
        return "invalid_url"
    if parsed.hostname == STRIPE_CHECKOUT_HOST:
        return "checkout.stripe.com:hosted_checkout"
    if success_url and _success_redirect(url, success_url):
        return f"{parsed.hostname or 'unknown'}:success_redirect"
    if parsed.hostname == urlparse(DEFAULT_CANCEL_URL).hostname:
        return f"{parsed.hostname}:cancel_redirect"
    return f"{parsed.hostname or 'unknown'}:other_redirect"


def _browser_diagnostic(
    *, stage: str, reason_code: str, url: str | None = None, success_url: str | None = None,
    error: BaseException | None = None, session: StripeHostedCheckoutSession | None = None,
    browser: StripeBrowserDiagnostic | None = None,
) -> StripeBrowserDiagnostic:
    return StripeBrowserDiagnostic(
        stage=stage,
        reason_code=reason_code,
        final_url_summary=_url_summary(url, success_url=success_url),
        error_type=type(error).__name__ if error is not None else None,
        session_status=session.status if session else None,
        payment_status=session.payment_status if session else None,
        payment_intent_present=session.payment_intent_id is not None if session else None,
        browser_stage=browser.stage if browser else None,
        browser_reason_code=browser.reason_code if browser else None,
        browser_final_url_summary=browser.final_url_summary if browser else None,
        browser_error_type=browser.error_type if browser else None,
    )


async def run_stripe_test_checkout(
    checkout_url: str,
    *,
    success_url: str,
    session_policy: BrowserSessionPolicy | None = None,
    headless: bool | None = None,
) -> StripeBrowserOutcome:
    """Complete the Stripe hosted page with Stripe's documented 4242 test card."""
    parse_stripe_checkout_url(checkout_url)
    if session_policy is not None and headless is not None:
        raise StripeLiveConfigurationError("Specify session_policy or legacy headless, not both")
    if session_policy is None:
        session_policy = BrowserSessionPolicy(
            mode=BrowserSessionMode.HEADLESS if headless is not False else BrowserSessionMode.HEADED,
        )
    if session_policy.mode is BrowserSessionMode.REMOTE_INTERACTIVE:
        raise StripeLiveConfigurationError(
            "remote_interactive Stripe checkout requires an injected controlled browser runner"
        )
    try:
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise StripeHostedCheckoutError("Playwright is required for the explicit live browser flow") from exc

    launch_options: dict[str, object] = {
        "headless": session_policy.mode is BrowserSessionMode.HEADLESS,
    }
    installed = _find_installed_chromium()
    if installed is not None:
        launch_options["executable_path"] = installed

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(**launch_options)
        try:
            page = await browser.new_page()
            try:
                await page.goto(checkout_url, wait_until="domcontentloaded", timeout=30_000)
                _require_hosted_checkout_page(page.url)
            except Exception as exc:
                return StripeBrowserOutcome("unknown", _browser_diagnostic(stage="navigation", reason_code="browser_navigation_error", url=page.url, success_url=success_url, error=exc))
            try:
                await _fill_checkout_field(page, ["input[autocomplete='cc-number']", "input[name='cardnumber']", "input[name='cardNumber']", "input[placeholder*='Card number']"], "4242424242424242")
                await _fill_checkout_field(page, ["input[autocomplete='cc-exp']", "input[name='exp-date']", "input[name='cardExpiry']", "input[placeholder*='MM']"], "12/34")
                await _fill_checkout_field(page, ["input[autocomplete='cc-csc']", "input[name='cvc']", "input[name='cardCvc']", "input[placeholder*='CVC']"], "123")
                await _fill_optional_checkout_field(page, ["input[autocomplete='email']", "input[type='email']"], "stripe-test@example.com")
            except Exception as exc:
                return StripeBrowserOutcome("unknown", _browser_diagnostic(stage="field", reason_code="browser_field_error", url=page.url, success_url=success_url, error=exc))
            try:
                await _click_payment_button(page)
            except Exception as exc:
                return StripeBrowserOutcome("unknown", _browser_diagnostic(stage="submit", reason_code="browser_submit_error", url=page.url, success_url=success_url, error=exc))
            for _ in range(25):
                current_url = page.url
                # Stripe's page text is non-authoritative and can show a success
                # message while the Session is still open/unpaid. Only the
                # configured redirect is a browser completion signal; the
                # independent Checkout Session/PaymentIntent reads remain the
                # business-result authority.
                if _browser_completion_detected(current_url, success_url):
                    return StripeBrowserOutcome("completed", _browser_diagnostic(stage="redirect", reason_code="browser_success_redirect", url=current_url, success_url=success_url))
                try:
                    _require_hosted_checkout_page(current_url)
                except Exception as exc:
                    return StripeBrowserOutcome("unknown", _browser_diagnostic(stage="redirect", reason_code="browser_unexpected_redirect", url=current_url, success_url=success_url, error=exc))
                await page.wait_for_timeout(1_000)
            return StripeBrowserOutcome("unknown", _browser_diagnostic(stage="redirect", reason_code="browser_redirect_timeout", url=page.url, success_url=success_url))
        except PlaywrightTimeoutError as exc:
            diagnostic = _browser_diagnostic(stage="navigation", reason_code="browser_timeout", url=locals().get("page").url if "page" in locals() else None, success_url=success_url, error=exc)
            return StripeBrowserOutcome("unknown", diagnostic)
        finally:
            await browser.close()


class StripeHostedCheckoutBrowserRuntime:
    """Small governed port around the hosted browser callback.

    The callback is intentionally invoked only from ``execute_preflighted``.
    Observation exposes a stable, redacted Checkout affordance so the generic
    loop can bind a fresh ``BrowserAction`` before the callback is entered.
    """

    def __init__(self, checkout_url: str, browser_runner: BrowserRunner, *, success_url: str) -> None:
        parse_stripe_checkout_url(checkout_url)
        self._checkout_url = checkout_url
        self._browser_runner = browser_runner
        self._success_url = success_url
        self._observed = False

    async def observe(self):
        from enterprise.browser_loop.contracts import BrowserElement, RawBrowserObservation

        self._observed = True
        return RawBrowserObservation(
            url=self._checkout_url,
            title="Stripe Checkout",
            page_html="<button data-agentpact-submit='true'>Pay</button>",
            model_dom='[{"element_id":"stripe-submit","role":"button","name":"Pay"}]',
            elements=(
                BrowserElement(
                    element_id="stripe-submit",
                    tag_name="button",
                    role="button",
                    name="Pay",
                ),
            ),
            captured_at=datetime.now(timezone.utc),
        )

    async def preflight(self, command: Any) -> None:
        from enterprise.browser_loop.ports import BrowserRuntimeError, StaleObservationError

        if not self._observed:
            raise StaleObservationError("Stripe Checkout requires a fresh observation before submit")
        if command.action.operation != CAPABILITY_ID or command.action.element_id != "stripe-submit":
            raise BrowserRuntimeError("Stripe Checkout action is outside the hosted submit contract", effect_may_have_started=False)

    async def execute(self, command: Any) -> BrowserActionResult:
        await self.preflight(command)
        return await self.execute_preflighted(command)

    async def execute_preflighted(self, command: Any) -> BrowserActionResult:
        from enterprise.browser_loop.ports import BrowserRuntimeError

        try:
            state = await self._browser_runner(self._checkout_url, success_url=self._success_url)
        except Exception as exc:
            raise BrowserRuntimeError(
                f"Stripe hosted browser outcome is uncertain: {type(exc).__name__}",
                effect_may_have_started=True,
            ) from exc
        if state not in {"completed", "unknown"}:
            raise BrowserRuntimeError(
                "Stripe hosted browser returned an unsafe state",
                effect_may_have_started=True,
            )
        return BrowserActionResult(
            completed=state == "completed",
            effect_may_have_started=True,
            detail_code="STRIPE_CHECKOUT_SUBMIT_RETURNED",
        )


async def _fill_checkout_field(page: Any, selectors: list[str], value: str) -> None:
    """Fill one card/billing field, waiting for Stripe's JS-rendered form.

    Stripe's hosted Checkout is a heavy client-side app; the form fields are
    rendered well after ``domcontentloaded``, so each candidate is awaited
    with a generous visibility timeout instead of a short poll.
    """
    for frame in [page, *page.frames]:
        for selector in selectors:
            locator = frame.locator(selector).first
            try:
                await locator.wait_for(state="visible", timeout=30_000)
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
            await button.wait_for(state="visible", timeout=15_000)
            await button.click()
            return
        except Exception:
            continue
    raise StripeHostedCheckoutError("Stripe hosted checkout submit button was not found")


def _success_redirect(current_url: str, success_url: str) -> bool:
    expected = success_url.split("{CHECKOUT_SESSION_ID}", 1)[0]
    return current_url.startswith(expected)


def _browser_completion_detected(current_url: str, success_url: str) -> bool:
    """Return true only for Stripe's configured success redirect.

    Hosted page text is intentionally excluded: it is presentation evidence,
    not an authoritative Checkout Session or PaymentIntent state transition.
    """
    return _success_redirect(current_url, success_url)


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
        session_policy: BrowserSessionPolicy | None = None,
        headless: bool | None = None,
    ) -> None:
        if session_policy is not None and headless is not None:
            raise StripeLiveConfigurationError("Specify session_policy or legacy headless, not both")
        if session_policy is None:
            session_policy = BrowserSessionPolicy(
                mode=BrowserSessionMode.HEADLESS if headless is not False else BrowserSessionMode.HEADED,
            )
        if session_policy.mode is BrowserSessionMode.REMOTE_INTERACTIVE and browser_runner is None:
            raise StripeLiveConfigurationError(
                "remote_interactive Stripe checkout requires an injected controlled browser runner"
            )
        self._session_policy = session_policy
        self._api_client_factory = api_client_factory or (lambda key: StripeTestApiClient(secret_key=key))
        self._browser_runner = browser_runner or (
            lambda url, *, success_url: run_stripe_test_checkout(
                url,
                success_url=success_url,
                session_policy=session_policy,
            )
        )
        self._evidence_dir = Path(evidence_dir) if evidence_dir is not None else None
        self._results: dict[str, StripeHostedCheckoutResult] = {}
        self._started_keys: set[str] = set()

    @property
    def session_policy(self) -> BrowserSessionPolicy:
        return self._session_policy

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
        try:
            browser_result = await self._browser_runner(session.checkout_url, success_url=success_url)
        except Exception as exc:
            diagnostic = getattr(exc, "diagnostic", None) or _browser_diagnostic(stage="browser", reason_code="browser_runner_error", error=exc)
            raise StripeHostedCheckoutError("Stripe hosted checkout browser runner failed", diagnostic=diagnostic) from exc
        browser_state = str(browser_result)
        diagnostic = getattr(browser_result, "diagnostic", None) or _browser_diagnostic(
            stage="redirect" if browser_state == "completed" else "browser",
            reason_code="browser_success_redirect" if browser_state == "completed" else "browser_unknown",
        )
        completed_session = api.retrieve_checkout_session(session_id=session.session_id)
        if completed_session.session_id != session.session_id:
            raise StripeHostedCheckoutError("Stripe Checkout Session response id does not match the requested id")
        if not completed_session.payment_intent_id:
            raise StripeHostedCheckoutError(
                "Stripe hosted checkout did not return a PaymentIntent",
                diagnostic=_browser_diagnostic(
                    stage="session",
                    reason_code="checkout_session_payment_intent_missing",
                    session=completed_session,
                    browser=diagnostic,
                ),
            )
        probe = StripeApiResultProbe(secret_key=key, api_base=getattr(api, "api_base", STRIPE_API_BASE))
        try:
            probe_evidence = probe.probe(resource_id=completed_session.payment_intent_id, idempotency_key=idempotency_key)
        except StripeProbeError as exc:
            raise StripeHostedCheckoutError(
                "Stripe PaymentIntent probe failed closed",
                diagnostic=_browser_diagnostic(stage="probe", reason_code="probe_stripe_api_error", error=exc),
            ) from exc
        evidence = self._build_evidence(
            session=completed_session,
            browser_state=browser_state,
            probe=probe_evidence,
            idempotency_key=idempotency_key,
            diagnostic=diagnostic,
        )
        if browser_state != "completed":
            raise StripeHostedCheckoutError("Stripe hosted checkout ended in an unknown page state after the independent probe; no replay is allowed", diagnostic=diagnostic)
        result = StripeHostedCheckoutResult(
            session=completed_session,
            browser_state=browser_state,
            probe=probe_evidence,
            evidence=evidence,
            diagnostic=diagnostic,
        )
        self._results[idempotency_key] = result
        return result

    async def execute_governed(
        self,
        *,
        facts: StripePaymentFacts,
        idempotency_key: str,
        task_id: str,
        step_id: str,
        contract_id: str,
        organization_id: str,
        session_factory: Callable[[], AbstractAsyncContextManager[Any]],
        integrity_secret: str,
        runtime_factory: GovernedCheckoutRuntimeFactory | None = None,
        success_url: str = DEFAULT_SUCCESS_URL,
        cancel_url: str = DEFAULT_CANCEL_URL,
        now: datetime | None = None,
    ) -> StripeHostedCheckoutResult:
        """Execute one hosted Checkout submit through the AgentPact boundary.

        Checkout Session creation is preparation. The hosted submit is the only
        browser side effect and is consequently proposed from a fresh
        observation, authorized by a fresh Permit, and invoked only by
        ``PersistedBrowserExecutor``. The executor intentionally leaves the
        Attempt UNKNOWN; the independent PaymentIntent probe below is the only
        resolver.
        """

        if not integrity_secret:
            raise StripeHostedCheckoutError("Governed Stripe Checkout requires an integrity secret")
        if not idempotency_key:
            raise StripeHostedCheckoutError("Governed Stripe Checkout requires a non-empty idempotency key")
        runtime_factory = runtime_factory or self.governed_runtime_factory(success_url=success_url)
        prior = self._results.get(idempotency_key)
        if prior is not None:
            return prior
        if idempotency_key in self._started_keys:
            raise StripeHostedCheckoutError(
                "Governed Stripe Checkout already started for this idempotency key; no replay is allowed"
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
        runtime = runtime_factory(session.checkout_url)
        if hasattr(runtime, "__await__"):
            runtime = await runtime  # type: ignore[union-attr]
        persisted = PersistedBrowserExecutor(
            session_factory,
            runtime,  # type: ignore[arg-type]
            result_probe_ref=RESULT_PROBE_REF,
            clock=lambda: now or datetime.now(timezone.utc),
        )
        loop = AgentPactBrowserLoop(
            runtime=persisted,
            model=_UnavailableStripeModel(),
            policy=_StripeSubmitPolicy(
                facts=facts,
                session_factory=session_factory,
                task_id=task_id,
                step_id=step_id,
                contract_id=contract_id,
                idempotency_key=idempotency_key,
                integrity_secret=integrity_secret,
                clock=lambda: now or datetime.now(timezone.utc),
            ),
            verifier=_StripeDeferredVerifier(),
            event_sink=SqlAlchemyBrowserLoopEventSink(
                session_factory,
                organization_id=organization_id,
                contract_id=contract_id,
                policy_version="stripe-payment-policy-v0.1.0-draft.1",
            ),
            integrity_secret=integrity_secret,
            domain_actions=_StripeSubmitActions(),
            config=BrowserLoopConfig(max_iterations=1, max_retries=0),
            clock=lambda: now or datetime.now(timezone.utc),
        )
        report = await loop.run(
            BrowserLoopRunContext(
                run_id=task_id,
                task_id=task_id,
                step_id=step_id,
                goal="Submit the approved Stripe test-mode Checkout exactly once",
                pack_id=PACK_ID,
                pack_version=PACK_VERSION,
                capability_id=CAPABILITY_ID,
                contract_id=contract_id,
            )
        )
        checkpoint = report.execution_checkpoint
        if report.status is not BrowserLoopStatus.UNKNOWN or checkpoint is None:
            raise StripeHostedCheckoutError(
                f"Governed Stripe browser did not reach the UNKNOWN probe boundary: {report.reason_code}"
            )

        # A browser return never proves the business result. Read the Checkout
        # Session only to bind its exact PaymentIntent, then keep the Attempt
        # UNKNOWN until the independent PaymentIntent probe runs.
        try:
            completed_session = api.retrieve_checkout_session(session_id=session.session_id)
            if completed_session.session_id != session.session_id:
                raise StripeHostedCheckoutError(
                    "Stripe Checkout Session response id does not match the requested id"
                )
            if not completed_session.payment_intent_id:
                raise StripeHostedCheckoutError(
                    "Stripe Checkout Session did not expose the exact PaymentIntent"
                )
            resource_id = completed_session.payment_intent_id
            reason = "hosted Checkout submit requires an independent PaymentIntent probe"
            await _persist_probe_context(
                session_factory,
                checkpoint=checkpoint,
                evidence=None,
                checkout_session_id=session.session_id,
                reason=reason,
            )
        except (StripeHostedCheckoutError, httpx.TimeoutException, httpx.TransportError) as exc:
            reason = f"hosted Checkout Session read unavailable: {type(exc).__name__}"
            # The Attempt is already UNKNOWN. Preserve only the Checkout
            # Session correlation; inventing the request's PaymentIntent here
            # would allow a later probe to certify the wrong business object.
            await _persist_probe_context(
                session_factory,
                checkpoint=checkpoint,
                evidence=None,
                checkout_session_id=session.session_id,
                reason=reason,
            )
            raise StripeHostedCheckoutError(
                f"{reason}; retry the exact Checkout Session before probing"
            ) from exc
        probe_evidence = build_probe_evidence(
            resource_id=resource_id,
            idempotency_key=idempotency_key,
            status=ResultProbeStatus.UNKNOWN,
            read=None,
            reasons=[reason],
        )
        evidence = self._build_evidence(
            session=completed_session,
            browser_state="unknown",
            probe=probe_evidence,
            idempotency_key=idempotency_key,
        )
        await _persist_probe_context(
            session_factory,
            checkpoint=checkpoint,
            evidence=probe_evidence,
            checkout_session_id=session.session_id,
        )
        result = StripeHostedCheckoutResult(
            session=completed_session,
            browser_state="unknown",
            probe=probe_evidence,
            evidence=evidence,
            execution_checkpoint=checkpoint,
            execution_status=ResultProbeStatus.UNKNOWN,
        )
        return result

    async def probe_governed(
        self,
        *,
        facts: StripePaymentFacts,
        idempotency_key: str,
        checkpoint: ExecutionCheckpoint,
        resource_id: str | None = None,
        checkout_session_id: str | None = None,
        session_factory: Callable[[], AbstractAsyncContextManager[Any]],
        probe_factory: Callable[..., Any] | None = None,
    ) -> ResultProbeEvidence:
        """Resolve one persisted UNKNOWN Attempt without reopening Checkout."""

        if checkpoint.result_probe_ref != RESULT_PROBE_REF or checkpoint.attempt_status != "unknown":
            raise StripeHostedCheckoutError("Stripe probe requires the exact UNKNOWN execution checkpoint")
        if not hmac.compare_digest(_digest(idempotency_key), checkpoint.idempotency_key_digest):
            raise StripeHostedCheckoutError("Stripe probe idempotency key does not match the exact Attempt")
        persisted_context = await _load_probe_context(session_factory, checkpoint)
        persisted_metadata = persisted_context.get("metadata")
        persisted_session_id = (
            persisted_metadata.get("checkout_session_id")
            if isinstance(persisted_metadata, dict)
            else None
        )
        if checkout_session_id is not None and checkout_session_id != persisted_session_id:
            raise StripeHostedCheckoutError(
                "Stripe Checkout Session does not match the exact persisted Attempt"
            )
        persisted_resource_id = persisted_context.get("resource_id")
        if persisted_resource_id is not None and resource_id is not None and resource_id != persisted_resource_id:
            raise StripeHostedCheckoutError("Stripe probe resource does not match the exact persisted Attempt")
        if persisted_resource_id is not None and resource_id is None:
            resource_id = persisted_resource_id
        if persisted_resource_id is None and resource_id is not None and checkout_session_id is None:
            raise StripeHostedCheckoutError(
                "Stripe PaymentIntent may be bound only from the exact persisted Checkout Session"
            )
        key = stripe_test_key_from_environment()
        api = self._api_client_factory(key)
        if checkout_session_id:
            session = api.retrieve_checkout_session(session_id=checkout_session_id)
            if session.session_id != checkout_session_id:
                raise StripeHostedCheckoutError(
                    "Stripe Checkout Session response id does not match the requested id"
                )
            if resource_id is not None and resource_id != session.payment_intent_id:
                raise StripeHostedCheckoutError(
                    "Stripe Checkout Session returned a different PaymentIntent than the persisted context"
                )
            resource_id = session.payment_intent_id
        if not resource_id:
            raise StripeHostedCheckoutError("Stripe probe requires the exact PaymentIntent returned by Checkout")
        validate_payment_intent_id(resource_id)
        # The result probe is bound to the PaymentIntent returned by Stripe,
        # never to a model-supplied or newly-created identifier.
        probe = (probe_factory or StripeApiResultProbe)(secret_key=key, api_base=getattr(api, "api_base", STRIPE_API_BASE))
        evidence = probe.probe(resource_id=resource_id, idempotency_key=idempotency_key)
        if evidence.resource_id != resource_id or evidence.probe_ref != RESULT_PROBE_REF:
            raise StripeHostedCheckoutError("Stripe result probe evidence is not bound to the exact PaymentIntent")
        await _persist_probe_context(
            session_factory,
            checkpoint=checkpoint,
            evidence=evidence,
            checkout_session_id=checkout_session_id,
        )
        if evidence.status is not ResultProbeStatus.UNKNOWN:
            await _resolve_attempt(session_factory, checkpoint, evidence)
        return evidence

    def result_for(self, idempotency_key: str) -> StripeHostedCheckoutResult | None:
        return self._results.get(idempotency_key)

    def governed_runtime_factory(self, *, success_url: str = DEFAULT_SUCCESS_URL) -> GovernedCheckoutRuntimeFactory:
        """Return the hosted browser callback as a preflight-capable runtime."""

        return lambda checkout_url: StripeHostedCheckoutBrowserRuntime(
            checkout_url,
            self._browser_runner,
            success_url=success_url,
        )

    def _build_evidence(
        self,
        *,
        session: StripeHostedCheckoutSession,
        browser_state: str,
        probe: ResultProbeEvidence,
        idempotency_key: str,
        diagnostic: StripeBrowserDiagnostic | None = None,
    ) -> StripeHostedCheckoutEvidence:
        evidence = StripeHostedCheckoutEvidence(
            session_id=session.session_id,
            checkout_url_digest=_digest(session.checkout_url),
            payment_intent_id=session.payment_intent_id,
            idempotency_key_digest=_digest(idempotency_key),
            browser_state=browser_state,
            probe_status=probe.status,
            captured_at=datetime.now(timezone.utc),
            session_status=session.status,
            payment_status=session.payment_status,
            payment_intent_present=session.payment_intent_id is not None,
            browser_stage=diagnostic.stage if diagnostic else None,
            browser_reason_code=diagnostic.reason_code if diagnostic else None,
            browser_final_url_summary=diagnostic.final_url_summary if diagnostic else None,
            browser_error_type=diagnostic.error_type if diagnostic else None,
            probe_reason_code=probe.metadata.get("reason_code") if isinstance(probe.metadata, dict) else None,
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


class _UnavailableStripeModel:
    async def decide(self, _model_input: ModelInput) -> ActionDecision:
        raise StripeHostedCheckoutError("Stripe hosted Checkout requires its deterministic Pack action provider")


class _StripeSubmitActions:
    binding = PackRuntimeBinding(
        pack_id=PACK_ID,
        pack_version=PACK_VERSION,
        capability_ids=(CAPABILITY_ID,),
        adapter_id="stripe.payment.browser-submit.v1",
    )

    async def decide(self, *, run: BrowserLoopRunContext, observation: BrowserObservation) -> ActionDecision:
        candidates = [
            element
            for element in observation.elements
            if element.enabled
            and any(
                token in f"{element.name or ''} {element.text or ''}".lower()
                for token in ("pay", "submit", "purchase", "complete")
            )
        ]
        if len(candidates) != 1:
            return ActionDecision(
                kind=DecisionKind.FAILURE,
                observation_id=observation.observation_id,
                reason_code="STRIPE_CHECKOUT_SUBMIT_TARGET_UNSAFE",
            )
        return ActionDecision(
            kind=DecisionKind.ACTION,
            observation_id=observation.observation_id,
            action=BrowserAction(
                kind=ActionKind.CLICK,
                operation=CAPABILITY_ID,
                element_id=candidates[0].element_id,
            ),
            reason_code="STRIPE_CHECKOUT_SUBMIT_ACTION",
        )


class _StripeSubmitPolicy:
    def __init__(
        self,
        *,
        facts: StripePaymentFacts,
        session_factory: Callable[[], AbstractAsyncContextManager[Any]],
        task_id: str,
        step_id: str,
        contract_id: str,
        idempotency_key: str,
        integrity_secret: str,
        clock: Callable[[], datetime],
    ) -> None:
        self._facts = facts
        self._session_factory = session_factory
        self._task_id = task_id
        self._step_id = step_id
        self._contract_id = contract_id
        self._idempotency_key = idempotency_key
        self._integrity_secret = integrity_secret
        self._clock = clock

    async def prepare_model_input(self, *, run: BrowserLoopRunContext, observation: BrowserObservation) -> ModelInput:
        return ModelInput(
            observation_id=observation.observation_id,
            goal=run.goal,
            url=observation.url,
            dom=observation.model_dom,
            screenshots=observation.screenshots,
            allowed_action_kinds=(),
        )

    async def authorize_action(
        self,
        *,
        run: BrowserLoopRunContext,
        observation: BrowserObservation,
        action: BrowserAction,
        action_fingerprint: str,
    ) -> PolicyAuthorization:
        if run.task_id != self._task_id or run.step_id != self._step_id or run.contract_id != self._contract_id:
            return PolicyAuthorization(disposition=PolicyDisposition.DENY, reason_code="STRIPE_AUTHORITY_ID_MISMATCH")
        # Business facts are immutable for this one admitted run. They are
        # still checked against the exact scoped PaymentIntent before issuing a
        # Permit so a changed browser target cannot cross the write boundary.
        decision = PolicyDecision(
            decision_id=f"stripe-live-allow:{action_fingerprint}",
            intent_id=f"stripe-live-intent:{action_fingerprint}",
            outcome=DecisionOutcome.ALLOW,
            risk_level="high",
            reasons=["Fresh hosted Checkout observation passed Stripe submit policy"],
            matched_rules=["stripe.payment.separation-of-duties", "stripe.payment.fresh-observation"],
            policy_version="stripe-payment-policy-v0.1.0-draft.1",
        )
        profile = ExecutionProfile(
            mechanism=ExecutionMechanism.LOCATOR,
            fallback_rank=0,
            evidence_refs=[f"agentpact://stripe.payment/observation/{observation.observation_id}"],
        )
        async with self._session_factory() as session:
            async with session.begin():
                from enterprise.governance.permit_service import issue_permit

                permit = await issue_permit(
                    db_session=session,
                    task_id=self._task_id,
                    step_id=self._step_id,
                    contract_id=self._contract_id,
                    action_fingerprint=action_fingerprint,
                    observation_hash=observation.observation_id,
                    decision=decision,
                    effect=ExecutionEffect.EXTERNAL_WRITE,
                    execution_profile=profile,
                    ttl_seconds=60,
                )
        return PolicyAuthorization(
            disposition=PolicyDisposition.ALLOW,
            reason_code="STRIPE_CHECKOUT_PERMIT_ISSUED",
            authorization=ExecutionAuthorization(
                permit_id=permit.permit_id,
                action_fingerprint=action_fingerprint,
                observation_hash=observation.observation_id,
                idempotency_key=self._idempotency_key,
                effect=ExecutionEffect.EXTERNAL_WRITE,
            ),
            execution_profile=profile,
        )


class _StripeDeferredVerifier:
    async def verify(self, request: VerificationRequest) -> VerificationResult:
        return VerificationResult(
            disposition=VerificationDisposition.UNKNOWN,
            reason_code="STRIPE_RESULT_PROBE_REQUIRED",
            evidence_refs=(RESULT_PROBE_REF,),
        )


async def _persist_probe_context(
    session_factory: Callable[[], AbstractAsyncContextManager[Any]],
    *,
    checkpoint: ExecutionCheckpoint,
    evidence: ResultProbeEvidence | None,
    checkout_session_id: str | None = None,
    reason: str | None = None,
) -> None:
    """Persist redacted probe correlation so a restarted worker can recover."""

    async with session_factory() as session:
        async with session.begin():
            model = (
                await session.scalars(
                    select(ExecutionAttemptModel).where(ExecutionAttemptModel.attempt_id == checkpoint.attempt_id)
                )
            ).first()
            if model is None:
                raise StripeHostedCheckoutError("Stripe execution Attempt disappeared before probe persistence")
            if model.status != "unknown":
                raise StripeHostedCheckoutError("Stripe probe context is not attached to an UNKNOWN Attempt")
            existing = model.result_probe if isinstance(model.result_probe, dict) else None
            existing_resource = existing.get("resource_id") if existing is not None else None
            new_resource = evidence.resource_id if evidence is not None else None
            existing_metadata = existing.get("metadata") if existing is not None else None
            existing_session_id = (
                existing_metadata.get("checkout_session_id")
                if isinstance(existing_metadata, dict)
                else None
            )
            if (
                existing_session_id is not None
                and checkout_session_id is not None
                and existing_session_id != checkout_session_id
            ):
                raise StripeHostedCheckoutError("Stripe Checkout Session changed for the persisted Attempt")
            if existing_resource is not None and existing_resource != new_resource:
                raise StripeHostedCheckoutError("Stripe probe resource changed for the persisted Attempt")
            if existing_resource is None and new_resource is not None:
                if not checkout_session_id or existing_session_id != checkout_session_id:
                    raise StripeHostedCheckoutError(
                        "Stripe PaymentIntent may be bound only from the exact persisted Checkout Session"
                    )
            if evidence is None:
                payload: dict[str, Any] = {
                    "probe_ref": RESULT_PROBE_REF,
                    "status": ResultProbeStatus.UNKNOWN.value,
                    "resource_id": None,
                    "reasons": [reason] if reason else [],
                    "metadata": {},
                }
            else:
                payload = evidence.model_dump(mode="json")
            if existing is not None and isinstance(existing.get("metadata"), dict):
                payload.setdefault("metadata", {}).update(existing["metadata"])
            if checkout_session_id:
                payload.setdefault("metadata", {})["checkout_session_id"] = checkout_session_id
            model.result_probe = payload


async def _load_probe_context(
    session_factory: Callable[[], AbstractAsyncContextManager[Any]],
    checkpoint: ExecutionCheckpoint,
) -> dict[str, Any]:
    """Load and validate the exact UNKNOWN Attempt before any Stripe read."""

    async with session_factory() as session:
        model = (
            await session.scalars(
                select(ExecutionAttemptModel).where(ExecutionAttemptModel.attempt_id == checkpoint.attempt_id)
            )
        ).first()
        if (
            model is None
            or model.status != "unknown"
            or model.permit_id != checkpoint.permit_id
            or model.task_id != checkpoint.task_id
            or model.step_id != checkpoint.step_id
            or model.result_probe_ref != RESULT_PROBE_REF
            or model.idempotency_key_digest != checkpoint.idempotency_key_digest
        ):
            raise StripeHostedCheckoutError("Stripe probe does not match the exact persisted Attempt")
        return model.result_probe if isinstance(model.result_probe, dict) else {}


async def _resolve_attempt(
    session_factory: Callable[[], AbstractAsyncContextManager[Any]],
    checkpoint: ExecutionCheckpoint,
    evidence: ResultProbeEvidence,
) -> None:
    async with session_factory() as session:
        async with session.begin():
            attempt = (
                await session.scalars(
                    select(ExecutionAttemptModel).where(ExecutionAttemptModel.attempt_id == checkpoint.attempt_id)
                )
            ).first()
            if (
                attempt is None
                or attempt.status != "unknown"
                or attempt.permit_id != checkpoint.permit_id
                or attempt.result_probe_ref != RESULT_PROBE_REF
                or attempt.idempotency_key_digest != checkpoint.idempotency_key_digest
            ):
                raise StripeHostedCheckoutError("Stripe probe does not match the exact persisted Attempt")
            await resolve_unknown_execution_attempt(
                db_session=session,
                attempt_id=checkpoint.attempt_id,
                confirmed=evidence.status is ResultProbeStatus.CONFIRMED,
                result_probe=evidence.model_dump(mode="json"),
            )


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
