"""Test-only FastAPI console for the isolated synthetic payment harness."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from enterprise.domains.synthetic_payment.accounts import SYNTHETIC_ACCOUNTS, require_synthetic_account
from enterprise.domains.synthetic_payment.harness import SyntheticPaymentEnforceHarness
from enterprise.domains.synthetic_payment.models import FaultMode, PaymentFacts, SyntheticPaymentError
from enterprise.governance.admission_persistence import TaskAdmissionConflict
from tests.fixtures.synthetic_payment_admission import SyntheticPaymentTaskAdmissionEntry


class CreateChallengeRequest(BaseModel):
    payment_id: str
    beneficiary_id: str
    amount: Decimal
    currency: str = "CNY"
    reference: str
    requester_account: str = "operator"


class ApprovalDecisionRequest(BaseModel):
    approver_account: str
    approved: bool = True


class ExecuteRequest(BaseModel):
    fault_mode: FaultMode = FaultMode.NONE


class CreateTaskAdmissionRequest(BaseModel):
    request_id: str
    facts: PaymentFacts
    requester_account: str = "operator"


def create_app(
    harness: SyntheticPaymentEnforceHarness | None = None,
    task_admission_entry: SyntheticPaymentTaskAdmissionEntry | None = None,
) -> FastAPI:
    runtime = harness or SyntheticPaymentEnforceHarness(hmac_secret="synthetic-demo-only-hmac")
    application = FastAPI(title="Synthetic Payment Sandbox", version="1.0.0")
    application.state.harness = runtime
    application.state.task_admission_entry = task_admission_entry

    @application.exception_handler(SyntheticPaymentError)
    async def synthetic_error_handler(_request, exc: SyntheticPaymentError):
        return _error_response(str(exc), 409)

    @application.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        return HTMLResponse(_HTML)

    @application.get("/health")
    async def health() -> dict[str, str | bool]:
        return {"status": "ready", "domain_pack": "synthetic.payment", "production_eligible": False}

    @application.get("/api/accounts")
    async def accounts() -> list[dict[str, str]]:
        return [
            {
                "account": name,
                "user_id": user.user_id,
                "role": user.department_roles[0].role,
                "department_id": user.department_roles[0].department_id,
            }
            for name, user in SYNTHETIC_ACCOUNTS.items()
        ]

    @application.post("/api/challenges")
    async def create_challenge(request: CreateChallengeRequest):
        if request.currency not in {"CNY", "USD", "EUR"}:
            raise HTTPException(status_code=422, detail="Unsupported synthetic currency")
        challenge = runtime.prepare_submission(
            requester=require_synthetic_account(request.requester_account),
            facts=PaymentFacts(
                payment_id=request.payment_id,
                beneficiary_id=request.beneficiary_id,
                amount=request.amount,
                currency=request.currency,
                reference=request.reference,
            ),
        )
        return challenge.model_dump(mode="json")

    @application.post("/api/task-admissions")
    async def create_task_admission(request: CreateTaskAdmissionRequest) -> dict[str, Any]:
        if task_admission_entry is None:
            raise HTTPException(status_code=503, detail="Synthetic Task admission repository is not configured")
        try:
            receipt = await task_admission_entry.admit(
                request_id=request.request_id,
                facts=request.facts,
                requester_account=request.requester_account,
            )
        except TaskAdmissionConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return receipt.model_dump(mode="json")

    @application.get("/api/challenges/{challenge_id}")
    async def get_challenge(challenge_id: str):
        return runtime.get_challenge(challenge_id).model_dump(mode="json")

    @application.post("/api/challenges/{challenge_id}/approval")
    async def decide_approval(challenge_id: str, request: ApprovalDecisionRequest):
        challenge = runtime.get_challenge(challenge_id)
        requester = next(
            (account for account in SYNTHETIC_ACCOUNTS.values() if account.user_id == challenge.requester_user_id),
            None,
        )
        if requester is None:
            raise HTTPException(status_code=409, detail="Synthetic requester snapshot is unavailable")
        decided = runtime.decide_approval(
            challenge_id=challenge_id,
            requester=requester,
            approver=require_synthetic_account(request.approver_account),
            approved=request.approved,
        )
        return decided.model_dump(mode="json")

    @application.post("/api/challenges/{challenge_id}/execute")
    async def execute(challenge_id: str, request: ExecuteRequest):
        return runtime.execute_submission(
            challenge_id=challenge_id,
            fault_mode=request.fault_mode,
        ).model_dump(mode="json")

    @application.post("/api/challenges/{challenge_id}/probe")
    async def probe(challenge_id: str):
        return runtime.resolve_unknown(challenge_id).model_dump(mode="json")

    @application.post("/api/payments/{payment_id}/clear-probe-fault")
    async def clear_probe_fault(payment_id: str) -> dict[str, bool]:
        runtime.store.clear_probe_fault(payment_id)
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
  <title>Synthetic Payment Console</title>
  <style>
    :root { color-scheme: light; font-family: Inter, Segoe UI, Arial, sans-serif; letter-spacing: 0; }
    * { box-sizing: border-box; }
    body { margin: 0; background: #f4f6f8; color: #1d252c; }
    header { min-height: 64px; display: flex; align-items: center; justify-content: space-between; padding: 0 28px; background: #172126; color: #fff; border-bottom: 3px solid #2f8f67; }
    h1 { margin: 0; font-size: 20px; font-weight: 650; }
    .badge { color: #172126; background: #d7f2e5; border: 1px solid #7ec5a4; padding: 5px 8px; border-radius: 4px; font-size: 12px; font-weight: 700; }
    main { width: min(1180px, 100%); margin: 0 auto; padding: 24px; display: grid; grid-template-columns: minmax(300px, 420px) minmax(0, 1fr); gap: 20px; }
    section { background: #fff; border: 1px solid #d6dde2; border-radius: 6px; padding: 20px; }
    h2 { margin: 0 0 18px; font-size: 16px; }
    label { display: block; margin: 12px 0 5px; color: #46545d; font-size: 13px; font-weight: 650; }
    input, select { width: 100%; min-height: 40px; padding: 8px 10px; border: 1px solid #aeb9c0; border-radius: 4px; background: #fff; color: #1d252c; font-size: 14px; }
    input:focus, select:focus { outline: 2px solid #78bfa0; outline-offset: 1px; }
    .row { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
    .actions { display: flex; flex-wrap: wrap; gap: 9px; margin-top: 18px; }
    button { min-height: 38px; border: 1px solid #247654; border-radius: 4px; padding: 8px 13px; background: #2f8f67; color: #fff; font-weight: 700; cursor: pointer; }
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
  <header data-governance-page="synthetic-payment-console" data-governance-domain-pack="synthetic.payment" data-governance-readiness="ready"><h1>Synthetic Payment Console</h1><span class="badge">NON-PRODUCTION</span></header>
  <main>
    <section>
      <h2>Payment draft</h2>
      <label for="paymentId">Payment ID</label><input id="paymentId" name="payment_id" data-testid="payment-id" data-governance-field="payment_id" aria-label="Synthetic payment ID" value="pay-demo-001">
      <label for="beneficiary">Beneficiary ID</label><input id="beneficiary" name="beneficiary_id" data-testid="beneficiary-id" data-governance-field="beneficiary_id" aria-label="Synthetic beneficiary ID" value="vendor-demo-001">
      <div class="row"><div><label for="amount">Amount</label><input id="amount" name="amount" data-testid="amount" data-governance-field="amount" aria-label="Synthetic payment amount" type="number" min="0.01" step="0.01" value="5000.00"></div><div><label for="currency">Currency</label><select id="currency" name="currency" data-testid="currency" data-governance-field="currency" aria-label="Synthetic payment currency"><option>CNY</option><option>USD</option><option>EUR</option></select></div></div>
      <label for="reference">Reference</label><input id="reference" name="reference" data-testid="reference" data-governance-field="reference" aria-label="Synthetic payment reference" value="Synthetic invoice 001">
      <div class="actions"><button id="create" data-testid="create-challenge" data-governance-action="create_challenge" aria-label="Create synthetic payment challenge">Create challenge</button></div>
      <h2 style="margin-top:24px">Approval and execution</h2>
      <label for="approver">Approver</label><select id="approver" name="approver_account" data-testid="approver-account" data-governance-field="approver_account" aria-label="Synthetic approver account"><option value="approver">Payments approver</option><option value="compliance">Compliance approver</option></select>
      <label for="fault">Fault mode</label><select id="fault" name="fault_mode" data-testid="fault-mode" data-governance-field="fault_mode" aria-label="Synthetic execution fault mode"><option value="none">Normal</option><option value="fail_before_commit">Fail before commit</option><option value="commit_then_timeout">Commit then timeout</option><option value="commit_then_inconclusive">Commit then inconclusive</option></select>
      <div class="actions"><button id="approve" data-testid="approve-payment" data-governance-action="approve_payment" aria-label="Approve synthetic payment" class="secondary" disabled>Approve</button><button id="execute" data-testid="execute-payment" data-governance-action="execute_payment" aria-label="Execute synthetic payment once" disabled>Execute once</button><button id="probe" data-testid="probe-result" data-governance-action="probe_payment_result" aria-label="Probe synthetic payment result" class="secondary" disabled>Probe result</button><button id="clear" data-testid="clear-probe-fault" data-governance-action="clear_probe_fault" aria-label="Clear synthetic probe fault" class="danger" disabled>Clear probe fault</button></div>
    </section>
    <section>
      <h2>Governance state</h2>
      <div class="status" data-governance-state="ready"><div>Challenge</div><div id="challenge" data-governance-field="challenge_id">-</div><div>State</div><div id="state" data-governance-field="state">-</div><div>Risk</div><div id="risk" data-governance-field="risk_level">-</div><div>Permit</div><div id="permit" data-governance-field="permit_id">-</div><div>Attempt</div><div id="attempt" data-governance-field="attempt_status">-</div><div>Business reference</div><div id="businessRef" data-governance-field="business_reference">-</div></div>
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
    byId('create').onclick = async () => { try { byId('output').classList.remove('error'); render(await request('/api/challenges', {payment_id:byId('paymentId').value, beneficiary_id:byId('beneficiary').value, amount:byId('amount').value, currency:byId('currency').value, reference:byId('reference').value, requester_account:'operator'})); } catch(e) { showError(e); } };
    byId('approve').onclick = async () => { try { render(await request(`/api/challenges/${current.challenge_id}/approval`, {approver_account:byId('approver').value, approved:true})); } catch(e) { showError(e); } };
    byId('execute').onclick = async () => { try { render(await request(`/api/challenges/${current.challenge_id}/execute`, {fault_mode:byId('fault').value})); } catch(e) { showError(e); } };
    byId('probe').onclick = async () => { try { render(await request(`/api/challenges/${current.challenge_id}/probe`, {})); } catch(e) { showError(e); } };
    byId('clear').onclick = async () => { try { await request(`/api/payments/${current.facts.payment_id}/clear-probe-fault`, {}); render(await request(`/api/challenges/${current.challenge_id}/probe`, {})); } catch(e) { showError(e); } };
  </script>
</body>
</html>"""
