"""Deterministic tests for the stripe M10 runtime adapter and its registry
conformance. No network, no credentials, no database: the adapter runs the
recorded harness in memory, exactly like the standalone M10 proof.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from enterprise.auth.schemas import UserContext
from enterprise.domains.stripe_payment.accounts import require_stripe_account
from enterprise.domains.stripe_payment.constants import (
    CAPABILITY_ID,
    TENANT_ID,
)
from enterprise.domains.stripe_payment.m6_runtime import STRIPE_RUNTIME_CONTRACT
from enterprise.domains.stripe_payment.m10_runtime import (
    M10_ADAPTER_ID,
    StripeM10NotWired,
    StripeM10PreparedRun,
    StripePaymentRuntimeAdapter,
    build_stripe_provider_factory,
    derive_stripe_agent_run_id,
)
from enterprise.governance.pack_runtime import (
    PackRuntimeBinding,
    PackRuntimeRegistry,
)

NOW = datetime(2026, 7, 29, 11, 30, tzinfo=timezone.utc)
TENANT = TENANT_ID
REQUEST_ID = "request-stripe-m10-001"
INPUTS = {
    "payment_intent_id": "pi_m10_001",
    "customer_id": "cus_m10_001",
    "amount_minor": 5000,
    "currency": "usd",
    "description": "Stripe M10 test payment",
    "object_version": 1,
}


def _user() -> UserContext:
    return require_stripe_account("operator")


def _adapter(*, provider_mode: str = "recorded") -> StripePaymentRuntimeAdapter:
    return StripePaymentRuntimeAdapter(
        provider_mode=provider_mode,  # type: ignore[arg-type]
        hmac_secret="stripe-m10-test-hmac",
        clock=lambda: NOW,
    )


def _prepared(adapter: StripePaymentRuntimeAdapter) -> StripeM10PreparedRun:
    return adapter.prepare_run(
        user=_user().model_dump(mode="json"),
        tenant_id=TENANT,
        request_id=REQUEST_ID,
        intent_digest="intent-token-m10",
        business_inputs=INPUTS,
        target_url="http://127.0.0.1:61000/",
        now=NOW,
    )


def test_registry_accepts_the_stripe_adapter_and_rejects_mismatched_capabilities():
    registry = PackRuntimeRegistry([STRIPE_RUNTIME_CONTRACT])
    adapter = _adapter()
    registry.register(adapter)

    assert registry.require(pack_id="stripe.payment", pack_version="0.1.0-draft.1") is adapter
    metadata = registry.public_metadata(pack_id="stripe.payment", pack_version="0.1.0-draft.1")
    assert metadata.pack_id == "stripe.payment"
    assert metadata.display_name == STRIPE_RUNTIME_CONTRACT.display_name
    assert registry.registered_bindings == (
        PackRuntimeBinding(
            pack_id="stripe.payment",
            pack_version="0.1.0-draft.1",
            capability_ids=("stripe.payment.read", "stripe.payment.submit"),
            adapter_id=M10_ADAPTER_ID,
        ),
    )

    class MismatchedAdapter:
        binding = PackRuntimeBinding(
            pack_id="stripe.payment",
            pack_version="0.1.0-draft.1",
            capability_ids=("stripe.payment.read",),
            adapter_id="forged.v1",
        )

    with pytest.raises(ValueError, match="capabilities do not exactly match"):
        PackRuntimeRegistry([STRIPE_RUNTIME_CONTRACT]).register(MismatchedAdapter())


def test_prepare_run_compiles_one_trusted_plan_with_immutable_digests():
    adapter = _adapter()
    prepared = _prepared(adapter)

    assert prepared.run_id == derive_stripe_agent_run_id(tenant_id=TENANT, request_id=REQUEST_ID)
    assert prepared.run_id.startswith("run_m10_")
    assert prepared.compilation.proposal.capability_id == CAPABILITY_ID
    assert prepared.compilation.work_order.result_probe_ref == "stripe.payment.submit.result-probe.v1"
    assert prepared.business_inputs_digest
    assert prepared.admission_bundle.request.capability_ref == CAPABILITY_ID
    assert prepared.admission_bundle.task.task_id == prepared.run_id

    projection = adapter.model_safe_projection(prepared.compilation)
    assert projection.pack_id == "stripe.payment"
    assert CAPABILITY_ID in projection.capability_ids
    assert "payment_intent_id" in projection.input_slot_names
    assert "amount_minor" in projection.input_slot_names


async def test_recorded_m10_lifecycle_pauses_then_advances_to_confirmed():
    adapter = _adapter()
    prepared = _prepared(adapter)

    paused = await adapter.admit_run(prepared)
    assert paused == {"state": "pending_approval", "challenge_id": paused["challenge_id"]}

    advanced = await adapter.advance_run(prepared)
    assert advanced["state"] == "confirmed"
    assert advanced["attempt_status"] == "confirmed"
    assert advanced["probe_status"] == "confirmed"

    # probe is legal only for an UNKNOWN attempt: fail closed on confirmed.
    with pytest.raises(ValueError, match="Only an unknown Stripe attempt can be probed"):
        await adapter.probe_run(prepared)


async def test_admit_run_invokes_the_pause_handler_with_verified_context():
    adapter = _adapter()
    prepared = _prepared(adapter)
    captured: dict[str, object] = {}

    async def pause_handler(*, prepared: object, challenge_id: str, operation_key: str | None) -> object:
        captured["challenge_id"] = challenge_id
        captured["operation_key"] = operation_key
        return {"paused": True, "challenge_id": challenge_id}

    result = await adapter.admit_run(prepared, pause_handler=pause_handler, operation_key="op-1")
    assert result == {"paused": True, "challenge_id": captured["challenge_id"]}
    assert captured["operation_key"] == "op-1"
    assert str(captured["challenge_id"]).startswith("stripe_challenge_")


def test_restore_run_rebuilds_identical_trusted_state_from_admission():
    adapter = _adapter()
    prepared = _prepared(adapter)

    restored = adapter.restore_run(prepared.admission_bundle, target_url=prepared.target_url)
    assert restored.run_id == prepared.run_id
    assert restored.business_inputs == prepared.business_inputs
    assert restored.business_inputs_digest == prepared.business_inputs_digest
    assert restored.admission_bundle == prepared.admission_bundle
    assert restored.compilation.trace == prepared.compilation.trace


def test_recorded_provider_factory_returns_deterministic_planner():
    factory = build_stripe_provider_factory("recorded")
    planner = factory(INPUTS)
    from enterprise.agent.constrained_planner import DeterministicPlanner

    assert isinstance(planner, DeterministicPlanner)


def test_live_provider_factory_fails_closed_without_complete_configuration(monkeypatch):
    monkeypatch.delenv("OPENAI_COMPATIBLE_API_KEY", raising=False)
    with pytest.raises(ValueError, match="configuration is incomplete"):
        build_stripe_provider_factory("live", endpoint="https://provider.invalid/v1", model="m")

    monkeypatch.setenv("OPENAI_COMPATIBLE_API_KEY", "env-only-secret")
    factory = build_stripe_provider_factory("live", endpoint="https://provider.invalid/v1", model="m")
    assert factory(INPUTS) is not None


async def test_live_mode_advance_and_probe_fail_closed_until_governed_browser_is_wired(monkeypatch):
    monkeypatch.setenv("OPENAI_COMPATIBLE_API_KEY", "env-only-secret")
    from enterprise.agent.constrained_planner import DeterministicPlanner

    adapter = StripePaymentRuntimeAdapter(
        provider_mode="live",
        hmac_secret="stripe-m10-test-hmac",
        provider_factory=lambda business_inputs: DeterministicPlanner(business_inputs),
        live_browser=object(),  # type: ignore[arg-type]
        clock=lambda: NOW,
    )
    prepared = _prepared(adapter)

    with pytest.raises(StripeM10NotWired, match="Attempt/Permit"):
        await adapter.advance_run(prepared)
    with pytest.raises(StripeM10NotWired, match="durable Attempt"):
        await adapter.probe_run(prepared)
