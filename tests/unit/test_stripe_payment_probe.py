"""Deterministic tests for the Stripe result probe: classification, recorded
mode, fail-closed construction, and evidence redaction. No network, no keys.
"""

from __future__ import annotations

import httpx
import pytest

from enterprise.domains.stripe_payment.result_probe import (
    STRIPE_SECRET_KEY_ENV,
    RecordedStripeProbe,
    StripeApiResultProbe,
    StripePaymentIntentRead,
    StripeProbeError,
    build_probe_evidence,
    classify_payment_intent,
)
from enterprise.governance.result_probes import ResultProbeEvidence, ResultProbeStatus


def _read(status: str, **overrides: object) -> StripePaymentIntentRead:
    values: dict[str, object] = {
        "payment_intent_id": "pi_test_123",
        "status": status,
        "amount_minor": 1000,
        "currency": "usd",
    }
    values.update(overrides)
    return StripePaymentIntentRead.model_validate(values)


@pytest.mark.parametrize(
    ("stripe_status", "expected"),
    [
        ("succeeded", ResultProbeStatus.CONFIRMED),
        ("processing", ResultProbeStatus.UNKNOWN),
        ("requires_action", ResultProbeStatus.UNKNOWN),
        ("requires_confirmation", ResultProbeStatus.UNKNOWN),
        ("requires_payment_method", ResultProbeStatus.UNKNOWN),
        ("requires_capture", ResultProbeStatus.UNKNOWN),
        ("canceled", ResultProbeStatus.NOT_CONFIRMED),
        ("brand_new_future_status", ResultProbeStatus.UNKNOWN),
    ],
)
def test_classify_payment_intent(stripe_status: str, expected: ResultProbeStatus):
    assert classify_payment_intent(_read(stripe_status)) is expected


def test_recorded_probe_confirmed_path():
    probe = RecordedStripeProbe({"pi_confirmed": _read("succeeded")})
    evidence = probe.probe(resource_id="pi_confirmed", idempotency_key="stripe:pi_confirmed")

    assert isinstance(evidence, ResultProbeEvidence)
    assert evidence.status is ResultProbeStatus.CONFIRMED
    assert evidence.resource_id == "pi_confirmed"
    assert evidence.business_reference == "pi_test_123"
    assert evidence.metadata["stripe_status"] == "succeeded"


def test_recorded_probe_transport_failure_is_unknown():
    probe = RecordedStripeProbe({"pi_timeout": httpx.TimeoutException("timed out")})
    evidence = probe.probe(resource_id="pi_timeout", idempotency_key="stripe:pi_timeout")

    assert evidence.status is ResultProbeStatus.UNKNOWN
    assert "recorded transport failure" in evidence.reasons[0]
    assert evidence.metadata["reason_code"] == "probe_network_or_stripe_api_error"


def test_recorded_probe_missing_fixture_fails_closed():
    probe = RecordedStripeProbe({})
    with pytest.raises(StripeProbeError):
        probe.probe(resource_id="pi_unknown", idempotency_key="stripe:pi_unknown")


def test_live_probe_construction_fails_closed_without_key(monkeypatch):
    monkeypatch.delenv(STRIPE_SECRET_KEY_ENV, raising=False)
    with pytest.raises(StripeProbeError):
        StripeApiResultProbe()


def test_live_probe_construction_accepts_explicit_test_key():
    probe = StripeApiResultProbe(secret_key="sk_test_deterministic_placeholder")
    assert probe is not None


def test_live_probe_rejects_path_like_payment_intent_ids_before_network():
    probe = StripeApiResultProbe(secret_key="sk_test_deterministic_placeholder")
    with pytest.raises(StripeProbeError, match="invalid format"):
        probe.probe(resource_id="pi_valid/../../customers", idempotency_key="probe-key")


def test_evidence_redacts_idempotency_key_value():
    evidence = build_probe_evidence(
        resource_id="pi_x",
        idempotency_key="stripe:pi_x",
        status=ResultProbeStatus.CONFIRMED,
        read=_read("succeeded"),
        reasons=["ok"],
    )

    assert "stripe:pi_x" not in evidence.metadata
    assert "idempotency_key_digest" in evidence.metadata
    assert evidence.metadata["idempotency_key_digest"] != "stripe:pi_x"
    assert evidence.facts_hash is not None
    assert evidence.metadata["reason_code"] == "payment_intent_succeeded"
