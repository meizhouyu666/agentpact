from __future__ import annotations

import ast
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pytest

from enterprise.auth.schemas import DepartmentRole, UserContext
from enterprise.domains.stripe_payment.accounts import require_stripe_account
from enterprise.domains.stripe_payment.constants import TENANT_ID as STRIPE_TENANT_ID
from enterprise.domains.stripe_payment.m6_runtime import STRIPE_RUNTIME_CONTRACT
from enterprise.domains.stripe_payment.m10_runtime import StripePaymentRuntimeAdapter
from enterprise.domains.synthetic_payment import constants as synthetic_constants
from enterprise.domains.synthetic_payment.m6_runtime import SYNTHETIC_RUNTIME_CONTRACT
from enterprise.domains.synthetic_payment.m10_runtime import SyntheticPaymentRuntimeAdapter
from enterprise.governance.admission import TaskAdmissionBundle
from enterprise.governance.pack_runtime import (
    PackRunRequest,
    PackRunRestoreRequest,
    PackRuntimeAdapter,
    PackRuntimeContract,
    PackRuntimeRegistry,
    PreparedRunReference,
)

NOW = datetime(2026, 8, 31, 14, 0, tzinfo=timezone.utc)


@dataclass(frozen=True)
class _AdapterFixture:
    name: str
    contract: PackRuntimeContract
    adapter_factory: Callable[[], PackRuntimeAdapter]
    request: PackRunRequest


def _synthetic_adapter() -> PackRuntimeAdapter:
    def no_session():
        raise AssertionError("Pack conformance preparation must not touch persistence")

    class NoopDriver:
        async def execute(self, **trusted_inputs):
            raise AssertionError(trusted_inputs)

        async def probe(self, **trusted_inputs):
            raise AssertionError(trusted_inputs)

    return SyntheticPaymentRuntimeAdapter(no_session, driver=NoopDriver())


def _synthetic_user() -> UserContext:
    return UserContext(
        user_id="pack-conformance-operator",
        org_id=synthetic_constants.TENANT_ID,
        department_roles=[
            DepartmentRole(
                department_id=synthetic_constants.PAYMENTS_DEPARTMENT_ID,
                department_name="Synthetic payments",
                role="operator",
            )
        ],
        business_line_ids=[synthetic_constants.BUSINESS_LINE_ID],
    )


FIXTURES = (
    _AdapterFixture(
        name="synthetic",
        contract=SYNTHETIC_RUNTIME_CONTRACT,
        adapter_factory=_synthetic_adapter,
        request=PackRunRequest(
            tenant_id=synthetic_constants.TENANT_ID,
            request_id="pack-conformance-synthetic",
            intent_digest="a" * 64,
            business_inputs={
                "payment_id": "conformance-payment",
                "beneficiary_id": "conformance-beneficiary",
                "amount": "25.00",
                "currency": "CNY",
                "reference": "conformance-reference",
                "object_version": 1,
            },
            target_url="http://127.0.0.1:18080",
            principal=_synthetic_user(),
            now=NOW,
        ),
    ),
    _AdapterFixture(
        name="stripe",
        contract=STRIPE_RUNTIME_CONTRACT,
        adapter_factory=lambda: StripePaymentRuntimeAdapter(
            hmac_secret="stripe-conformance-hmac",
            clock=lambda: NOW,
        ),
        request=PackRunRequest(
            tenant_id=STRIPE_TENANT_ID,
            request_id="pack-conformance-stripe",
            intent_digest="b" * 64,
            business_inputs={
                "payment_intent_id": "pi_pack_conformance",
                "customer_id": "cus_pack_conformance",
                "amount_minor": 2500,
                "currency": "usd",
                "description": "Pack conformance",
                "object_version": 1,
            },
            target_url="http://127.0.0.1:61000",
            principal=require_stripe_account("operator"),
            now=NOW,
        ),
    ),
)


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda item: item.name)
def test_real_pack_adapters_share_the_typed_prepare_restore_contract(fixture: _AdapterFixture) -> None:
    adapter = fixture.adapter_factory()
    registry = PackRuntimeRegistry([fixture.contract])
    registry.register(adapter)

    prepared = adapter.prepare_run(fixture.request)
    assert isinstance(prepared, PreparedRunReference)
    assert prepared.pack_id == fixture.contract.pack_id
    assert prepared.pack_version == fixture.contract.pack_version
    assert prepared.adapter_id == fixture.contract.adapter_id
    assert prepared.tenant_id == fixture.request.tenant_id
    assert prepared.request_id == fixture.request.request_id
    assert prepared.admission_id
    assert prepared.contract_id

    admission_payload = prepared.opaque_payload["admission_bundle"]
    assert admission_payload["runtime_binding"] == adapter.binding.model_dump(mode="json")
    assert all(item["result_probe_ref"] for item in admission_payload["work_orders"])

    restored = adapter.restore_run(
        PackRunRestoreRequest(
            run_id=prepared.run_id,
            tenant_id=prepared.tenant_id,
            request_id=prepared.request_id,
            binding=adapter.binding,
            provider_mode=prepared.provider_mode,
            target_url=fixture.request.target_url,
            admission_payload=admission_payload,
        )
    )
    assert isinstance(restored, PreparedRunReference)
    assert restored.model_dump(exclude={"opaque_payload"}) == prepared.model_dump(exclude={"opaque_payload"})
    assert TaskAdmissionBundle.model_validate(
        restored.opaque_payload["admission_bundle"]
    ) == TaskAdmissionBundle.model_validate(admission_payload)
    assert restored.opaque_payload["business_inputs_digest"] == prepared.opaque_payload["business_inputs_digest"]


def test_stripe_runtime_has_no_synthetic_dependency() -> None:
    path = Path(__file__).resolve().parents[2] / "enterprise" / "domains" / "stripe_payment" / "m10_runtime.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports = [
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    ] + [
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    ]
    assert not any(name.startswith("enterprise.domains.synthetic_payment") for name in imports)
