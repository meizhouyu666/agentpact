from datetime import datetime, timezone

from fastapi.testclient import TestClient

from enterprise.domains.synthetic_payment.admission_entry import SyntheticPaymentTaskAdmissionEntry
from enterprise.domains.synthetic_payment.app import create_app
from enterprise.domains.synthetic_payment.harness import SyntheticPaymentEnforceHarness
from enterprise.governance.admission import (
    GovernedTaskAdmissionService,
    TaskAdmissionReceipt,
)
from enterprise.governance.admission_persistence import TaskAdmissionConflict


class _ApiAdmissionRepository:
    def __init__(self):
        self.bundle = None
        self.receipt = None

    async def persist_atomic(self, bundle):
        if self.bundle is not None:
            if self.bundle.request.typed_inputs != bundle.request.typed_inputs:
                raise TaskAdmissionConflict("Capability request ID was reused with different admission semantics")
            return self.receipt.model_copy(update={"duplicate": True})
        self.bundle = bundle
        self.receipt = TaskAdmissionReceipt(
            admission_id=bundle.admission_id,
            task_id=bundle.task.task_id,
            contract_id=bundle.contract.contract_id,
            committed_at=datetime(2026, 7, 25, tzinfo=timezone.utc),
        )
        return self.receipt


def _client() -> TestClient:
    harness = SyntheticPaymentEnforceHarness(hmac_secret="synthetic-api-test-secret")
    return TestClient(create_app(harness))


def _admission_client() -> tuple[TestClient, _ApiAdmissionRepository]:
    repository = _ApiAdmissionRepository()
    entry = SyntheticPaymentTaskAdmissionEntry(
        GovernedTaskAdmissionService(repository),
        clock=lambda: datetime(2026, 7, 25, tzinfo=timezone.utc),
    )
    harness = SyntheticPaymentEnforceHarness(hmac_secret="synthetic-api-test-secret")
    return TestClient(create_app(harness, task_admission_entry=entry)), repository


def _create(client: TestClient, payment_id: str = "api-pay-1", amount: str = "5000.00") -> dict:
    response = client.post(
        "/api/challenges",
        json={
            "payment_id": payment_id,
            "beneficiary_id": "api-vendor-1",
            "amount": amount,
            "currency": "CNY",
            "reference": "Synthetic API invoice",
            "requester_account": "operator",
        },
    )
    assert response.status_code == 200
    return response.json()


def test_health_explicitly_disclaims_production_eligibility():
    response = _client().get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "domain_pack": "synthetic.payment",
        "production_eligible": False,
    }


def test_task_admission_endpoint_is_disabled_without_an_injected_repository():
    response = _client().post(
        "/api/task-admissions",
        json={"request_id": "request-disabled", "facts": _admission_facts()},
    )

    assert response.status_code == 503


def test_task_admission_endpoint_persists_once_replays_receipt_and_rejects_conflict():
    client, repository = _admission_client()
    payload = {"request_id": "request-api-admission", "facts": _admission_facts()}

    first = client.post("/api/task-admissions", json=payload)
    duplicate = client.post("/api/task-admissions", json=payload)
    conflict_payload = {
        **payload,
        "facts": {**payload["facts"], "amount": "126.00"},
    }
    conflict = client.post("/api/task-admissions", json=conflict_payload)

    assert first.status_code == 200
    assert first.json()["duplicate"] is False
    assert duplicate.status_code == 200
    assert duplicate.json()["duplicate"] is True
    assert duplicate.json()["admission_id"] == first.json()["admission_id"]
    assert conflict.status_code == 409
    assert repository.bundle.task.mode.value == "audit"
    assert repository.bundle.audit_record.event_type == "governed_task_admitted"
    assert "typed_inputs" not in str(repository.bundle.audit_record.model_dump(mode="json"))


def _admission_facts() -> dict[str, str | int]:
    return {
        "payment_id": "api-admission-pay-1",
        "beneficiary_id": "api-vendor-1",
        "amount": "125.00",
        "currency": "CNY",
        "reference": "Synthetic admission invoice",
        "object_version": 1,
    }


def test_api_runs_approved_payment_to_confirmed_business_result():
    client = _client()
    challenge = _create(client)

    approved = client.post(
        f"/api/challenges/{challenge['challenge_id']}/approval",
        json={"approver_account": "approver", "approved": True},
    )
    assert approved.status_code == 200
    assert approved.json()["state"] == "ready"

    executed = client.post(
        f"/api/challenges/{challenge['challenge_id']}/execute",
        json={"fault_mode": "none"},
    )
    assert executed.status_code == 200
    assert executed.json()["state"] == "confirmed"
    assert executed.json()["result_probe"]["business_reference"].startswith("SYN-api-pay-1-")
    assert [event["event_type"] for event in client.get("/api/audit").json()] == [
        "approval_requested",
        "permit_issued",
        "attempt_executing",
        "attempt_confirmed",
    ]


def test_api_unknown_path_requires_probe_and_never_reexecutes():
    client = _client()
    challenge = _create(client, payment_id="api-pay-unknown")
    challenge_id = challenge["challenge_id"]
    assert (
        client.post(
            f"/api/challenges/{challenge_id}/approval",
            json={"approver_account": "approver", "approved": True},
        ).status_code
        == 200
    )

    unknown = client.post(
        f"/api/challenges/{challenge_id}/execute",
        json={"fault_mode": "commit_then_inconclusive"},
    )
    assert unknown.status_code == 200
    assert unknown.json()["state"] == "unknown"
    assert (
        client.post(
            f"/api/challenges/{challenge_id}/execute",
            json={"fault_mode": "none"},
        ).status_code
        == 409
    )

    assert (
        client.post(
            "/api/payments/api-pay-unknown/clear-probe-fault",
            json={},
        ).status_code
        == 200
    )
    resolved = client.post(f"/api/challenges/{challenge_id}/probe", json={})
    assert resolved.status_code == 200
    assert resolved.json()["state"] == "confirmed"


def test_critical_api_payment_rejects_non_compliance_approver():
    client = _client()
    challenge = _create(client, payment_id="api-pay-critical", amount="100000.00")

    denied = client.post(
        f"/api/challenges/{challenge['challenge_id']}/approval",
        json={"approver_account": "approver", "approved": True},
    )

    assert denied.status_code == 409
    assert "department role" in denied.json()["detail"]
