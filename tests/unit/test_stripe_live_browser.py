"""Offline contract tests for the explicit Stripe hosted Checkout adapter."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from enterprise.domains.stripe_payment.live_browser import (
    StripeHostedCheckoutError,
    StripeHostedCheckoutFlow,
    StripeHostedCheckoutSession,
    StripeTestApiClient,
    _browser_completion_detected,
    _persist_probe_context,
    derive_live_idempotency_key,
    parse_stripe_checkout_url,
    stripe_test_key_from_environment,
    validate_stripe_test_key,
)
from enterprise.domains.stripe_payment.models import StripePaymentFacts
from enterprise.governance.contracts import ExecutionAttemptStatus
from enterprise.governance.models import ExecutionAttemptModel, ExecutionPermitModel
from enterprise.governance.pack_runtime import ExecutionCheckpoint
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


class _Values:
    def __init__(self, values):
        self.values = values

    def first(self):
        return self.values[0] if self.values else None

    def all(self):
        return list(self.values)


class _Transaction:
    def __init__(self, store):
        self.store = store

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, _exc, _tb):
        if exc_type is None:
            self.store.commits += 1
        return False


class _Session:
    def __init__(self, store):
        self.store = store

    def begin(self):
        return _Transaction(self.store)

    def add(self, model):
        if isinstance(model, ExecutionPermitModel):
            self.store.permits.append(model)
        elif isinstance(model, ExecutionAttemptModel):
            self.store.attempts.append(model)

    async def flush(self):
        for index, permit in enumerate(self.store.permits, start=1):
            permit.permit_id = permit.permit_id or f"permit_{index}"
            permit.status = permit.status or "issued"
        for index, attempt in enumerate(self.store.attempts, start=1):
            attempt.attempt_id = attempt.attempt_id or f"attempt_{index}"
            attempt.status = attempt.status or ExecutionAttemptStatus.AUTHORIZED.value
            attempt.created_at = attempt.created_at or datetime.now(timezone.utc)

    async def scalars(self, statement):
        entity = statement.column_descriptions[0]["entity"]
        if entity is ExecutionPermitModel:
            return _Values(self.store.permits)
        if entity is ExecutionAttemptModel:
            return _Values(self.store.attempts)
        return _Values([])


class _SessionContext:
    def __init__(self, store):
        self.store = store

    async def __aenter__(self):
        return _Session(self.store)

    async def __aexit__(self, _exc_type, _exc, _tb):
        return None


class _ExecutionStore:
    def __init__(self):
        self.permits = []
        self.attempts = []
        self.commits = 0

    def __call__(self):
        return _SessionContext(self)


def _checkpoint_for_attempt(attempt: ExecutionAttemptModel) -> ExecutionCheckpoint:
    return ExecutionCheckpoint(
        permit_id=attempt.permit_id,
        attempt_id=attempt.attempt_id,
        task_id=attempt.task_id,
        step_id=attempt.step_id,
        action_fingerprint=attempt.action_fingerprint,
        observation_hash=attempt.observation_hash,
        idempotency_key_digest=attempt.idempotency_key_digest,
        execution_effect=attempt.execution_effect,
        result_probe_ref=attempt.result_probe_ref,
        attempt_status=attempt.status,
    )


class _StableProbe:
    status = ResultProbeStatus.CONFIRMED

    def __init__(self, **_kwargs):
        pass

    def probe(self, *, resource_id, idempotency_key):
        return ResultProbeEvidence(
            probe_ref="stripe.payment.submit.result-probe.v1",
            status=self.status,
            resource_id=resource_id,
            checked_at=datetime.now(timezone.utc),
            reasons=[self.status.value],
        )


class _CheckoutRuntime:
    def __init__(self):
        self.observations = 0
        self.preflights = 0
        self.browser_calls = 0

    async def observe(self):
        from enterprise.browser_loop.contracts import BrowserElement, RawBrowserObservation

        self.observations += 1
        return RawBrowserObservation(
            url="https://checkout.stripe.com/c/pay/cs_test_123",
            title="Checkout",
            page_html="<button>Pay</button>",
            model_dom="pay",
            elements=(BrowserElement(element_id="pay", tag_name="button", name="Pay"),),
            captured_at=datetime.now(timezone.utc),
        )

    async def preflight(self, _command):
        self.preflights += 1

    async def execute_preflighted(self, _command):
        from enterprise.browser_loop.contracts import BrowserActionResult

        self.browser_calls += 1
        return BrowserActionResult(completed=True, effect_may_have_started=True, detail_code="ACTION_COMPLETED")


@pytest.mark.asyncio
async def test_governed_hosted_checkout_persists_attempt_and_resolves_exact_probe(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_123")
    store = _ExecutionStore()
    api = FakeCheckoutApi()
    runtime = _CheckoutRuntime()
    flow = StripeHostedCheckoutFlow(api_client_factory=lambda _key: api)
    key = derive_live_idempotency_key(request_id="governed-1", payment_intent_id=FACTS.payment_intent_id)

    result = await flow.execute_governed(
        facts=FACTS,
        idempotency_key=key,
        task_id="task-governed",
        step_id="step-governed",
        contract_id="contract-governed",
        organization_id="stripe-test-tenant",
        session_factory=store,
        integrity_secret="stripe-governed-hmac",
        runtime_factory=lambda _url: runtime,
    )

    assert result.execution_checkpoint is not None
    assert runtime.observations == 1
    assert runtime.preflights == 1
    assert runtime.browser_calls == 1
    assert store.permits[0].status == "consumed"
    assert store.attempts[0].status == ExecutionAttemptStatus.UNKNOWN.value

    resolved = await flow.probe_governed(
        facts=FACTS,
        idempotency_key=key,
        checkpoint=result.execution_checkpoint,
        session_factory=store,
        probe_factory=_StableProbe,
        resource_id=FACTS.payment_intent_id,
    )
    assert resolved.status is ResultProbeStatus.CONFIRMED
    assert store.attempts[0].status == ExecutionAttemptStatus.CONFIRMED.value


@pytest.mark.asyncio
async def test_governed_restart_never_replays_ambiguous_checkout(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_123")
    store = _ExecutionStore()
    calls = 0

    async def browser(_url: str, *, success_url: str) -> str:
        nonlocal calls
        calls += 1
        return "unknown"

    flow = StripeHostedCheckoutFlow(api_client_factory=lambda _key: FakeCheckoutApi(), browser_runner=browser)
    key = derive_live_idempotency_key(request_id="restart-1", payment_intent_id=FACTS.payment_intent_id)
    first = await flow.execute_governed(
        facts=FACTS,
        idempotency_key=key,
        task_id="task-restart",
        step_id="step-restart",
        contract_id="contract-restart",
        organization_id="stripe-test-tenant",
        session_factory=store,
        integrity_secret="stripe-governed-hmac",
    )
    assert first.execution_checkpoint is not None
    assert calls == 1

    restarted = StripeHostedCheckoutFlow(api_client_factory=lambda _key: FakeCheckoutApi(), browser_runner=browser)
    with pytest.raises(StripeHostedCheckoutError, match="UNKNOWN probe boundary"):
        await restarted.execute_governed(
            facts=FACTS,
            idempotency_key=key,
            task_id="task-restart",
            step_id="step-restart",
            contract_id="contract-restart",
            organization_id="stripe-test-tenant",
            session_factory=store,
            integrity_secret="stripe-governed-hmac",
        )
    assert calls == 1


@pytest.mark.asyncio
async def test_session_read_failure_preserves_exact_checkout_context_without_fabricating_payment_intent(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_123")
    store = _ExecutionStore()

    class FlakyCheckoutApi(FakeCheckoutApi):
        def __init__(self) -> None:
            super().__init__()
            self.fail_retrieve = True

        def retrieve_checkout_session(self, *, session_id: str):
            if self.fail_retrieve:
                self.retrieved += 1
                raise StripeHostedCheckoutError("session read unavailable")
            return super().retrieve_checkout_session(session_id=session_id)

    api = FlakyCheckoutApi()
    calls = 0

    async def browser(_url: str, *, success_url: str) -> str:
        nonlocal calls
        calls += 1
        return "unknown"

    flow = StripeHostedCheckoutFlow(api_client_factory=lambda _key: api, browser_runner=browser)
    key = derive_live_idempotency_key(request_id="session-read-failure", payment_intent_id=FACTS.payment_intent_id)
    with pytest.raises(StripeHostedCheckoutError, match="retry the exact Checkout Session"):
        await flow.execute_governed(
            facts=FACTS,
            idempotency_key=key,
            task_id="task-session-read-failure",
            step_id="step-session-read-failure",
            contract_id="contract-session-read-failure",
            organization_id="stripe-test-tenant",
            session_factory=store,
            integrity_secret="stripe-governed-hmac",
        )

    assert calls == 1
    assert store.attempts[0].status == ExecutionAttemptStatus.UNKNOWN.value
    assert store.attempts[0].result_probe["resource_id"] is None
    assert store.attempts[0].result_probe["metadata"]["checkout_session_id"] == "cs_test_123"
    assert FACTS.payment_intent_id not in str(store.attempts[0].result_probe)

    with pytest.raises(StripeHostedCheckoutError, match="exact persisted Checkout Session"):
        await _persist_probe_context(
            store,
            checkpoint=_checkpoint_for_attempt(store.attempts[0]),
            evidence=ResultProbeEvidence(
                probe_ref="stripe.payment.submit.result-probe.v1",
                status=ResultProbeStatus.CONFIRMED,
                resource_id=FACTS.payment_intent_id,
                checked_at=datetime.now(timezone.utc),
            ),
        )

    store.attempts[0].result_probe["metadata"].pop("checkout_session_id")
    with pytest.raises(StripeHostedCheckoutError, match="exact persisted Checkout Session"):
        await _persist_probe_context(
            store,
            checkpoint=_checkpoint_for_attempt(store.attempts[0]),
            evidence=ResultProbeEvidence(
                probe_ref="stripe.payment.submit.result-probe.v1",
                status=ResultProbeStatus.CONFIRMED,
                resource_id=FACTS.payment_intent_id,
                checked_at=datetime.now(timezone.utc),
            ),
            checkout_session_id="cs_test_123",
        )
    store.attempts[0].result_probe["metadata"]["checkout_session_id"] = "cs_test_123"

    api.fail_retrieve = False
    evidence = await flow.probe_governed(
        facts=FACTS,
        idempotency_key=key,
        checkpoint=_checkpoint_for_attempt(store.attempts[0]),
        checkout_session_id="cs_test_123",
        resource_id=None,
        session_factory=store,
        probe_factory=_StableProbe,
    )
    assert evidence.status is ResultProbeStatus.CONFIRMED
    assert evidence.resource_id == FACTS.payment_intent_id
    assert store.attempts[0].status == ExecutionAttemptStatus.CONFIRMED.value
    assert calls == 1


@pytest.mark.asyncio
async def test_governed_probe_rejects_idempotency_digest_mismatch_before_api_or_probe(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_123")
    store = _ExecutionStore()
    api = FakeCheckoutApi()
    runtime = _CheckoutRuntime()
    flow = StripeHostedCheckoutFlow(api_client_factory=lambda _key: api)
    key = derive_live_idempotency_key(request_id="probe-digest", payment_intent_id=FACTS.payment_intent_id)
    result = await flow.execute_governed(
        facts=FACTS,
        idempotency_key=key,
        task_id="task-probe-digest",
        step_id="step-probe-digest",
        contract_id="contract-probe-digest",
        organization_id="stripe-test-tenant",
        session_factory=store,
        integrity_secret="stripe-governed-hmac",
        runtime_factory=lambda _url: runtime,
    )
    retrieved_before = api.retrieved

    class ExplodingProbe:
        def __init__(self, **_kwargs):
            raise AssertionError("probe must not be constructed")

    with pytest.raises(StripeHostedCheckoutError, match="idempotency key"):
        await flow.probe_governed(
            facts=FACTS,
            idempotency_key="wrong-idempotency-key",
            checkpoint=result.execution_checkpoint,
            resource_id=FACTS.payment_intent_id,
            session_factory=store,
            probe_factory=ExplodingProbe,
        )
    assert api.retrieved == retrieved_before


@pytest.mark.asyncio
async def test_governed_probe_rejects_mismatched_checkout_session_before_api_or_probe(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_123")
    store = _ExecutionStore()
    api = FakeCheckoutApi()
    runtime = _CheckoutRuntime()
    flow = StripeHostedCheckoutFlow(api_client_factory=lambda _key: api)
    key = derive_live_idempotency_key(request_id="probe-session", payment_intent_id=FACTS.payment_intent_id)
    result = await flow.execute_governed(
        facts=FACTS,
        idempotency_key=key,
        task_id="task-probe-session",
        step_id="step-probe-session",
        contract_id="contract-probe-session",
        organization_id="stripe-test-tenant",
        session_factory=store,
        integrity_secret="stripe-governed-hmac",
        runtime_factory=lambda _url: runtime,
    )
    retrieved_before = api.retrieved

    class ExplodingProbe:
        def __init__(self, **_kwargs):
            raise AssertionError("probe must not be constructed")

    with pytest.raises(StripeHostedCheckoutError, match="exact persisted Attempt"):
        await flow.probe_governed(
            facts=FACTS,
            idempotency_key=key,
            checkpoint=result.execution_checkpoint,
            resource_id=FACTS.payment_intent_id,
            checkout_session_id="cs_wrong_session",
            session_factory=store,
            probe_factory=ExplodingProbe,
        )
    assert api.retrieved == retrieved_before


@pytest.mark.asyncio
async def test_unknown_probe_context_rejects_payment_intent_rebinding(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_123")
    store = _ExecutionStore()
    runtime = _CheckoutRuntime()
    flow = StripeHostedCheckoutFlow(api_client_factory=lambda _key: FakeCheckoutApi())
    key = derive_live_idempotency_key(request_id="probe-rebind", payment_intent_id=FACTS.payment_intent_id)
    result = await flow.execute_governed(
        facts=FACTS,
        idempotency_key=key,
        task_id="task-probe-rebind",
        step_id="step-probe-rebind",
        contract_id="contract-probe-rebind",
        organization_id="stripe-test-tenant",
        session_factory=store,
        integrity_secret="stripe-governed-hmac",
        runtime_factory=lambda _url: runtime,
    )
    assert store.attempts[0].result_probe["status"] == ResultProbeStatus.UNKNOWN.value

    with pytest.raises(StripeHostedCheckoutError, match="resource changed"):
        await _persist_probe_context(
            store,
            checkpoint=result.execution_checkpoint,
            evidence=ResultProbeEvidence(
                probe_ref="stripe.payment.submit.result-probe.v1",
                status=ResultProbeStatus.UNKNOWN,
                resource_id="pi_other_payment_intent",
                checked_at=datetime.now(timezone.utc),
            ),
            checkout_session_id="cs_test_123",
        )
    assert store.attempts[0].result_probe["resource_id"] == FACTS.payment_intent_id


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


def test_success_requires_the_configured_redirect_not_hosted_page_text():
    hosted_url = "https://checkout.stripe.com/c/pay/cs_test_123"
    success_url = "https://example.com/agentpact-stripe-success?session_id={CHECKOUT_SESSION_ID}"
    assert not _browser_completion_detected(hosted_url, success_url)
    assert _browser_completion_detected(
        "https://example.com/agentpact-stripe-success?session_id=cs_test_123",
        success_url,
    )
    # A hosted page may display this text before Stripe has completed the Session;
    # text is intentionally not an input to the completion predicate.
    assert not _browser_completion_detected("Payment successful", success_url)


def test_retrieve_checkout_session_preserves_open_unpaid_null_payment_intent(monkeypatch: pytest.MonkeyPatch):
    class Response:
        status_code = 200

        def json(self):
            return {
                "id": "cs_test_open_123",
                "status": "open",
                "payment_status": "unpaid",
                "payment_intent": None,
            }

    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return Response()

    monkeypatch.setattr("enterprise.domains.stripe_payment.live_browser.httpx.request", request)
    session = StripeTestApiClient(secret_key="sk_test_123").retrieve_checkout_session(session_id="cs_test_open_123")

    assert session.payment_intent_id is None
    assert session.status == "open"
    assert session.payment_status == "unpaid"
    assert calls[0][2]["params"] == {"expand[]": "payment_intent"}


@pytest.mark.asyncio
async def test_open_checkout_session_after_misleading_browser_text_fails_closed_without_probe(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_123")

    class OpenSessionApi(FakeCheckoutApi):
        def retrieve_checkout_session(self, *, session_id: str):
            self.retrieved += 1
            return StripeHostedCheckoutSession(
                session_id=session_id,
                checkout_url="https://checkout.stripe.com/c/pay/cs_test_123",
                payment_intent_id=None,
                status="open",
                payment_status="unpaid",
            )

    async def misleading_browser(_url: str, *, success_url: str) -> str:
        # This simulates a page that says success without Stripe redirecting.
        return "completed"

    class ExplodingProbe:
        def __init__(self, **_kwargs):
            raise AssertionError("PaymentIntent probe must not run without Stripe's PaymentIntent id")

    monkeypatch.setattr("enterprise.domains.stripe_payment.live_browser.StripeApiResultProbe", ExplodingProbe)
    api = OpenSessionApi()
    flow = StripeHostedCheckoutFlow(api_client_factory=lambda _key: api, browser_runner=misleading_browser)

    with pytest.raises(StripeHostedCheckoutError, match="did not return a PaymentIntent"):
        await flow.execute(facts=FACTS, idempotency_key="stripe-payment-live-v1:open-session")
    assert api.created == 1
    assert api.retrieved == 1


@pytest.mark.asyncio
async def test_governed_open_unpaid_session_keeps_unknown_attempt_bound_to_session(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_123")
    store = _ExecutionStore()

    class OpenSessionApi(FakeCheckoutApi):
        def retrieve_checkout_session(self, *, session_id: str):
            self.retrieved += 1
            return StripeHostedCheckoutSession(
                session_id=session_id,
                checkout_url="https://checkout.stripe.com/c/pay/cs_test_123",
                status="open",
                payment_status="unpaid",
            )

    async def browser(_url: str, *, success_url: str) -> str:
        return "completed"

    api = OpenSessionApi()
    flow = StripeHostedCheckoutFlow(api_client_factory=lambda _key: api, browser_runner=browser)
    key = derive_live_idempotency_key(request_id="governed-open", payment_intent_id=FACTS.payment_intent_id)

    with pytest.raises(StripeHostedCheckoutError, match="retry the exact Checkout Session"):
        await flow.execute_governed(
            facts=FACTS,
            idempotency_key=key,
            task_id="task-governed-open",
            step_id="step-governed-open",
            contract_id="contract-governed-open",
            organization_id="stripe-test-tenant",
            session_factory=store,
            integrity_secret="stripe-governed-hmac",
            runtime_factory=lambda _url: _CheckoutRuntime(),
        )

    assert store.attempts[0].status == ExecutionAttemptStatus.UNKNOWN.value
    assert store.attempts[0].result_probe["resource_id"] is None
    assert store.attempts[0].result_probe["metadata"]["checkout_session_id"] == "cs_test_123"
    assert api.retrieved == 1
