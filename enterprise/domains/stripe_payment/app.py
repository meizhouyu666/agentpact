"""FastAPI console: Stripe-style test checkout target and authoritative read.

Two deliberately separate channels, mirroring the real architecture:

- Checkout channel (browser target): the agent fills the payment form and
  submits it; the effect lands in the simulated Stripe backend.
- Authoritative channel: ``GET /v1/payment_intents/{id}`` answers the read
  that the result probe uses. The browser result is never trusted directly.

The console is NON-PRODUCTION and only exists for recorded-mode demos and
tests; the live pack reads the real Stripe API instead.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from .accounts import STRIPE_ACCOUNTS, require_stripe_account
from .harness import StripePaymentEnforceHarness
from .models import StripeOutcome, StripePaymentError, StripePaymentFacts
from .store import StripeFaultMode


class CreateCheckoutSessionRequest(BaseModel):
    payment_intent_id: str
    customer_id: str | None = None
    amount_minor: int
    currency: str = "usd"
    description: str = ""
    requester_account: str = "operator"


class ApprovalDecisionRequest(BaseModel):
    approver_account: str
    approved: bool = True


class ExecuteRequest(BaseModel):
    fault_mode: StripeFaultMode = StripeFaultMode.NONE
    outcome: StripeOutcome = StripeOutcome.SUCCEEDED


def create_app(harness: StripePaymentEnforceHarness | None = None) -> FastAPI:
    runtime = harness or StripePaymentEnforceHarness(hmac_secret="stripe-demo-only-hmac")
    application = FastAPI(title="Stripe Test Checkout Sandbox", version="0.1.0-draft.1")
    application.state.harness = runtime

    @application.exception_handler(StripePaymentError)
    async def stripe_error_handler(_request, exc: StripePaymentError):
        return _error_response(str(exc), 409)

    @application.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        return HTMLResponse(_HTML)

    @application.get("/health")
    async def health() -> dict[str, str | bool]:
        return {"status": "ready", "domain_pack": "stripe.payment", "production_eligible": False}

    @application.get("/api/accounts")
    async def accounts() -> list[dict[str, str]]:
        return [
            {
                "account": name,
                "user_id": user.user_id,
                "role": user.department_roles[0].role,
                "department_id": user.department_roles[0].department_id,
            }
            for name, user in STRIPE_ACCOUNTS.items()
        ]

    @application.post("/api/checkout/sessions")
    async def create_session(request: CreateCheckoutSessionRequest):
        if request.currency not in {"usd", "eur", "gbp", "cny"}:
            raise HTTPException(status_code=422, detail="Unsupported Stripe currency")
        challenge = runtime.prepare_submission(
            requester=require_stripe_account(request.requester_account),
            facts=StripePaymentFacts(
                payment_intent_id=request.payment_intent_id,
                customer_id=request.customer_id,
                amount_minor=request.amount_minor,
                currency=request.currency,
                description=request.description,
            ),
        )
        return challenge.model_dump(mode="json")

    @application.get("/api/checkout/sessions/{challenge_id}")
    async def get_session(challenge_id: str):
        return runtime.get_challenge(challenge_id).model_dump(mode="json")

    @application.post("/api/checkout/sessions/{challenge_id}/approval")
    async def decide_approval(challenge_id: str, request: ApprovalDecisionRequest):
        challenge = runtime.get_challenge(challenge_id)
        requester = next(
            (account for account in STRIPE_ACCOUNTS.values() if account.user_id == challenge.requester_user_id),
            None,
        )
        if requester is None:
            raise HTTPException(status_code=409, detail="Stripe requester snapshot is unavailable")
        decided = runtime.decide_approval(
            challenge_id=challenge_id,
            requester=requester,
            approver=require_stripe_account(request.approver_account),
            approved=request.approved,
        )
        return decided.model_dump(mode="json")

    @application.post("/api/checkout/sessions/{challenge_id}/execute")
    async def execute(challenge_id: str, request: ExecuteRequest):
        return runtime.execute_submission(
            challenge_id=challenge_id,
            fault_mode=request.fault_mode,
            outcome=request.outcome,
        ).model_dump(mode="json")

    @application.post("/api/checkout/sessions/{challenge_id}/probe")
    async def probe(challenge_id: str):
        return runtime.resolve_unknown(challenge_id).model_dump(mode="json")

    @application.get("/v1/payment_intents/{payment_intent_id}")
    async def authoritative_read(payment_intent_id: str) -> dict[str, Any]:
        """The independent read channel the result probe answers from."""
        try:
            record = runtime.store.require(payment_intent_id)
        except StripePaymentError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {
            "id": record.facts.payment_intent_id,
            "status": record.status.value,
            "outcome": runtime.store.outcome(payment_intent_id).value,
            "amount": record.facts.amount_minor,
            "currency": record.facts.currency,
            "object_version": record.facts.object_version,
            "confirmation_reference": record.confirmation_reference,
            "commit_count": record.commit_count,
        }

    @application.post("/api/payment_intents/{payment_intent_id}/clear-probe-fault")
    async def clear_probe_fault(payment_intent_id: str) -> dict[str, bool]:
        runtime.store.clear_probe_fault(payment_intent_id)
        return {"cleared": True}

    @application.get("/api/audit")
    async def audit():
        return [event.model_dump(mode="json") for event in runtime.audit_events]

    return application


def _error_response(detail: str, status_code: int):
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=status_code, content={"detail": detail})


app = create_app()


_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Stripe Test Checkout</title>
  <style>
    :root { color-scheme: light; font-family: Inter, Segoe UI, Arial, sans-serif; letter-spacing: 0; }
    * { box-sizing: border-box; }
    body { margin: 0; background: #f6f8f9; color: #1d252c; }
    header { min-height: 64px; display: flex; align-items: center; justify-content: space-between; padding: 0 28px; background: #1a1f2e; color: #fff; border-bottom: 3px solid #635bff; }
    h1 { margin: 0; font-size: 20px; font-weight: 650; }
    .badge { color: #1a1f2e; background: #e3e0ff; border: 1px solid #a39dff; padding: 5px 8px; border-radius: 4px; font-size: 12px; font-weight: 700; }
    main { width: min(1180px, 100%); margin: 0 auto; padding: 24px; display: grid; grid-template-columns: minmax(300px, 420px) minmax(0, 1fr); gap: 20px; }
    section { background: #fff; border: 1px solid #d8dde3; border-radius: 6px; padding: 20px; }
    h2 { margin: 0 0 18px; font-size: 16px; }
    label { display: block; margin: 12px 0 5px; color: #46545d; font-size: 13px; font-weight: 650; }
    input, select { width: 100%; min-height: 40px; padding: 8px 10px; border: 1px solid #aeb9c0; border-radius: 4px; background: #fff; color: #1d252c; font-size: 14px; }
    input:focus, select:focus { outline: 2px solid #8f89ff; outline-offset: 1px; }
    .row { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
    .actions { display: flex; flex-wrap: wrap; gap: 9px; margin-top: 18px; }
    button { min-height: 38px; border: 1px solid #4a44d6; border-radius: 4px; padding: 8px 13px; background: #635bff; color: #fff; font-weight: 700; cursor: pointer; }
    button.secondary { background: #fff; color: #26343b; border-color: #9aa7af; }
    button.danger { background: #a8413a; border-color: #8f332e; }
    button:disabled { cursor: not-allowed; opacity: .45; }
    .status { display: grid; grid-template-columns: 150px minmax(0, 1fr); border-top: 1px solid #e1e6e9; }
    .status div { padding: 10px 4px; border-bottom: 1px solid #e1e6e9; overflow-wrap: anywhere; }
    .status div:nth-child(odd) { color: #5a6870; font-size: 13px; }
    pre { min-height: 180px; max-height: 360px; overflow: auto; padding: 14px; border: 1px solid #d6dde2; border-radius: 4px; background: #f7f9fa; color: #243139; white-space: pre-wrap; overflow-wrap: anywhere; }
    .error { color: #9b302a; font-weight: 650; }
    @media (max-width: 800px) { main { grid-template-columns: 1fr; padding: 14px; } header { padding: 0 14px; } .row { grid-template-columns: 1fr; } }
  </style>
</head>
<body>
  <header data-governance-page="stripe-test-checkout" data-governance-domain-pack="stripe.payment" data-governance-readiness="ready"><h1>Stripe Test Checkout</h1><span class="badge">TEST MODE</span></header>
  <main>
    <section>
      <h2>Payment details</h2>
      <label for="pi">PaymentIntent ID</label><input id="pi" name="payment_intent_id" data-testid="payment-intent-id" data-governance-field="payment_intent_id" aria-label="Stripe PaymentIntent ID" value="pi_demo_001">
      <label for="customer">Customer ID (optional)</label><input id="customer" name="customer_id" data-testid="customer-id" data-governance-field="customer_id" aria-label="Stripe customer ID" value="cus_demo_001">
      <div class="row"><div><label for="amount">Amount (minor units)</label><input id="amount" name="amount_minor" data-testid="amount" data-governance-field="amount_minor" aria-label="Stripe payment amount minor units" type="number" min="1" step="1" value="5000"></div><div><label for="currency">Currency</label><select id="currency" name="currency" data-testid="currency" data-governance-field="currency" aria-label="Stripe payment currency"><option>usd</option><option>eur</option><option>gbp</option><option>cny</option></select></div></div>
      <label for="description">Description</label><input id="description" name="description" data-testid="description" data-governance-field="description" aria-label="Stripe payment description" value="Stripe test invoice 001">
      <div class="actions"><button id="create" data-testid="create-session" data-governance-action="create_session" aria-label="Create Stripe checkout session">Create session</button></div>
      <h2 style="margin-top:24px">Approval and execution</h2>
      <label for="approver">Approver</label><select id="approver" name="approver_account" data-testid="approver-account" data-governance-field="approver_account" aria-label="Stripe approver account"><option value="approver">Payments approver</option><option value="compliance">Compliance approver</option></select>
      <label for="fault">Fault mode</label><select id="fault" name="fault_mode" data-testid="fault-mode" data-governance-field="fault_mode" aria-label="Stripe execution fault mode"><option value="none">Normal</option><option value="fail_before_commit">Fail before commit</option><option value="commit_then_timeout">Commit then timeout</option><option value="commit_then_inconclusive">Commit then inconclusive</option></select>
      <label for="outcome">Backend outcome</label><select id="outcome" name="outcome" data-testid="outcome" data-governance-field="outcome" aria-label="Stripe backend outcome"><option value="succeeded">Succeeded</option><option value="processing">Processing</option><option value="canceled">Canceled</option></select>
      <div class="actions"><button id="approve" data-testid="approve-payment" data-governance-action="approve_payment" aria-label="Approve Stripe payment" class="secondary" disabled>Approve</button><button id="execute" data-testid="execute-payment" data-governance-action="execute_payment" aria-label="Execute Stripe payment once" disabled>Execute once</button><button id="probe" data-testid="probe-result" data-governance-action="probe_payment_result" aria-label="Probe Stripe payment result" class="secondary" disabled>Probe result</button><button id="clear" data-testid="clear-probe-fault" data-governance-action="clear_probe_fault" aria-label="Clear Stripe probe fault" class="danger" disabled>Clear probe fault</button></div>
    </section>
    <section>
      <h2>Governance state</h2>
      <div class="status" data-governance-state="ready"><div>Session</div><div id="challenge" data-governance-field="challenge_id">-</div><div>State</div><div id="state" data-governance-field="state">-</div><div>Risk</div><div id="risk" data-governance-field="risk_level">-</div><div>Permit</div><div id="permit" data-governance-field="permit_id">-</div><div>Attempt</div><div id="attempt" data-governance-field="attempt_status">-</div><div>Business reference</div><div id="businessRef" data-governance-field="business_reference">-</div></div>
      <pre id="output" data-governance-field="audit_output">Ready.</pre>
    </section>
  </main>
  <script>
    let current = null;
    const byId = id => document.getElementById(id);
    async function request(path, body) {
      const response = await fetch(path, { method: body ? 'POST' : 'GET', headers: {'Content-Type':'application/json'}, body: body ? JSON.stringify(body) : undefined });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || 'Request failed');
      return data;
    }
    function render(data) {
      current = data;
      byId('output').classList.remove('error');
      byId('challenge').textContent = data.challenge_id || '-';
      byId('state').textContent = data.state || '-';
      byId('risk').textContent = data.decision?.risk_level || '-';
      byId('permit').textContent = data.permit?.permit_id || '-';
      byId('attempt').textContent = data.attempt?.status || '-';
      byId('businessRef').textContent = data.result_probe?.business_reference || '-';
      byId('output').textContent = JSON.stringify(data, null, 2);
      byId('approve').disabled = data.state !== 'pending_approval';
      byId('execute').disabled = data.state !== 'ready';
      byId('probe').disabled = data.state !== 'unknown';
      byId('clear').disabled = data.state !== 'unknown';
    }
    function showError(error) { byId('output').textContent = error.message; byId('output').classList.add('error'); }
    byId('create').onclick = async () => { try { byId('output').classList.remove('error'); render(await request('/api/checkout/sessions', {payment_intent_id:byId('pi').value, customer_id:byId('customer').value, amount_minor:Number(byId('amount').value), currency:byId('currency').value, description:byId('description').value, requester_account:'operator'})); } catch(e) { showError(e); } };
    byId('approve').onclick = async () => { try { render(await request(`/api/checkout/sessions/${current.challenge_id}/approval`, {approver_account:byId('approver').value, approved:true})); } catch(e) { showError(e); } };
    byId('execute').onclick = async () => { try { render(await request(`/api/checkout/sessions/${current.challenge_id}/execute`, {fault_mode:byId('fault').value, outcome:byId('outcome').value})); } catch(e) { showError(e); } };
    byId('probe').onclick = async () => { try { render(await request(`/api/checkout/sessions/${current.challenge_id}/probe`, {})); } catch(e) { showError(e); } };
    byId('clear').onclick = async () => { try { await request(`/api/payment_intents/${current.facts.payment_intent_id}/clear-probe-fault`, {}); render(await request(`/api/checkout/sessions/${current.challenge_id}/probe`, {})); } catch(e) { showError(e); } };
  </script>
</body>
</html>"""
