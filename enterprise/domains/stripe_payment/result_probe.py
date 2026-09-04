"""Independent Stripe API result probe for the governed pack.

This probe is the pack's authoritative business-result channel, deliberately
separate from the browser transport: it reads ``GET /v1/payment_intents/{id}``
with test-mode credentials and NEVER performs a side effect. Its status
mapping is what resolves the framework's UNKNOWN state:

- Stripe ``succeeded``            -> CONFIRMED
- ``processing`` / ``requires_*`` -> UNKNOWN   (outcome not decidable yet)
- ``canceled``                    -> NOT_CONFIRMED
- HTTP 404                        -> NOT_CONFIRMED (resource absent)
- network error / 5xx / timeout   -> UNKNOWN     (side effect MAY have happened;
                                                 the governed path must NOT replay)
- HTTP 401/403 or malformed 4xx   -> raise (probe identity/contract broken:
                                                 fail closed, never look like
                                                 an ambiguous business outcome)
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from enum import StrEnum
from typing import Protocol

import httpx
from pydantic import BaseModel, Field

from enterprise.governance.result_probes import (
    BusinessResultProbe,
    ResultProbeEvidence,
    ResultProbeStatus,
)

from .constants import RESULT_PROBE_REF

STRIPE_API_BASE = "https://api.stripe.com/v1"
STRIPE_SECRET_KEY_ENV = "STRIPE_SECRET_KEY"
_PAYMENT_INTENT_ID = re.compile(r"^pi_[A-Za-z0-9_]+$")


def validate_payment_intent_id(payment_intent_id: str) -> str:
    """Reject path-like values before interpolating an ID into a Stripe URL."""
    if not _PAYMENT_INTENT_ID.fullmatch(payment_intent_id):
        raise StripeProbeError("Stripe PaymentIntent id has an invalid format")
    return payment_intent_id


class StripePaymentIntentStatus(StrEnum):
    """The full PaymentIntent status enum from the Stripe API reference.

    Verified against docs.stripe.com/api/payment_intents (2025-05-28.basil):
    ``requires_payment_method``, ``requires_confirmation``, ``requires_action``,
    ``processing``, ``requires_capture``, ``canceled``, ``succeeded``.
    """

    REQUIRES_PAYMENT_METHOD = "requires_payment_method"
    REQUIRES_CONFIRMATION = "requires_confirmation"
    REQUIRES_ACTION = "requires_action"
    PROCESSING = "processing"
    REQUIRES_CAPTURE = "requires_capture"
    SUCCEEDED = "succeeded"
    CANCELED = "canceled"


_CONFIRMED_STATUSES = frozenset({StripePaymentIntentStatus.SUCCEEDED})
_NOT_CONFIRMED_STATUSES = frozenset({StripePaymentIntentStatus.CANCELED})
# Everything else (requires_*, processing, or an unknown future status) is
# UNKNOWN: the governed loop must stop and never replay. This includes
# requires_capture: the payment exists but is not settled, so the business
# outcome is not decidable.
_UNKNOWN_STATUSES = frozenset(
    status for status in StripePaymentIntentStatus if status not in _CONFIRMED_STATUSES | _NOT_CONFIRMED_STATUSES
)


class StripePaymentIntentRead(BaseModel):
    """Typed, read-only projection of one Stripe PaymentIntent."""

    model_config = {"extra": "forbid"}

    payment_intent_id: str = Field(min_length=1)
    status: str
    amount_minor: int
    currency: str
    failure_code: str | None = None
    failure_message: str | None = None


class StripeProbeClient(Protocol):
    """Minimal read surface; the live client only ever issues GET requests."""

    def get_payment_intent(self, *, payment_intent_id: str, idempotency_key: str) -> StripePaymentIntentRead:
        """Return the authoritative PaymentIntent read or raise a transport error."""


class StripeProbeError(RuntimeError):
    """Probe identity or API contract problem: fail closed, never UNKNOWN."""


class StripeProbeUnavailable(RuntimeError):
    """Transient inability to read authoritative state (network, 5xx, timeout)."""


def classify_payment_intent(read: StripePaymentIntentRead) -> ResultProbeStatus:
    """Deterministic, fail-closed mapping from a Stripe read to probe status."""
    try:
        status = StripePaymentIntentStatus(read.status)
    except ValueError:
        # Unknown future status: treat as UNKNOWN rather than guessing success.
        return ResultProbeStatus.UNKNOWN
    if status in _CONFIRMED_STATUSES:
        return ResultProbeStatus.CONFIRMED
    if status in _NOT_CONFIRMED_STATUSES:
        return ResultProbeStatus.NOT_CONFIRMED
    return ResultProbeStatus.UNKNOWN


def build_probe_evidence(
    *,
    resource_id: str,
    idempotency_key: str,
    status: ResultProbeStatus,
    read: StripePaymentIntentRead | None,
    reasons: list[str],
    reason_code: str | None = None,
    checked_at: datetime | None = None,
) -> ResultProbeEvidence:
    """Build redacted evidence; the idempotency key is stored only as a digest."""
    return ResultProbeEvidence(
        probe_ref=RESULT_PROBE_REF,
        status=status,
        resource_id=resource_id,
        checked_at=checked_at or datetime.now(timezone.utc),
        business_reference=read.payment_intent_id if read is not None else None,
        facts_hash=_facts_digest(read) if read is not None else None,
        reasons=reasons,
        metadata={
            "idempotency_key_digest": hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest(),
            "reason_code": reason_code or _probe_reason_code(status=status, reasons=reasons),
            **({"stripe_status": read.status, "failure_code": read.failure_code} if read is not None else {}),
        },
    )


def _probe_reason_code(*, status: ResultProbeStatus, reasons: list[str]) -> str:
    """Return a stable, low-cardinality diagnostic code without copying errors."""
    text = " ".join(reasons).lower()
    if "does not exist" in text or "404" in text:
        return "payment_intent_absent"
    if "unavailable" in text or "transport" in text or "returned http" in text:
        return "probe_network_or_stripe_api_error"
    if status is ResultProbeStatus.CONFIRMED:
        return "payment_intent_succeeded"
    if status is ResultProbeStatus.NOT_CONFIRMED:
        return "payment_intent_not_confirmed"
    return "payment_intent_status_unknown"


def _facts_digest(read: StripePaymentIntentRead) -> str:
    canonical = f"{read.payment_intent_id}|{read.status}|{read.amount_minor}|{read.currency}"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class StripeApiResultProbe(BusinessResultProbe):
    """Live probe against api.stripe.com using test-mode credentials.

    Credentials come only from the ``STRIPE_SECRET_KEY`` environment variable
    (``sk_test_*``). Construction fails closed when the key is absent — the
    probe never silently falls back to recorded mode.
    """

    def __init__(self, *, secret_key: str | None = None, api_base: str = STRIPE_API_BASE) -> None:
        self._secret_key = secret_key if secret_key is not None else os.environ.get(STRIPE_SECRET_KEY_ENV)
        if not self._secret_key:
            raise StripeProbeError(
                f"Missing {STRIPE_SECRET_KEY_ENV}; live Stripe probe requires test-mode credentials."
            )
        if not self._secret_key.startswith("sk_test_") or len(self._secret_key) <= len("sk_test_"):
            raise StripeProbeError("Stripe probe accepts only sk_test_* credentials")
        self._api_base = api_base.rstrip("/")

    def probe(self, *, resource_id: str, idempotency_key: str) -> ResultProbeEvidence:
        validate_payment_intent_id(resource_id)
        url = f"{self._api_base}/payment_intents/{resource_id}"
        headers = {
            "Authorization": f"Bearer {self._secret_key}",
        }
        try:
            response = httpx.get(url, headers=headers, timeout=10.0)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            # The authoritative read itself failed: the browser side effect may
            # or may not have happened. This is exactly the UNKNOWN case.
            return build_probe_evidence(
                resource_id=resource_id,
                idempotency_key=idempotency_key,
                status=ResultProbeStatus.UNKNOWN,
                read=None,
                reasons=[f"authoritative read unavailable: {type(exc).__name__}"],
            )

        if response.status_code == 404:
            return build_probe_evidence(
                resource_id=resource_id,
                idempotency_key=idempotency_key,
                status=ResultProbeStatus.NOT_CONFIRMED,
                read=None,
                reasons=["PaymentIntent does not exist: submission did not land"],
            )
        if response.status_code in {401, 403}:
            raise StripeProbeError("Stripe probe credentials were rejected; probe identity is broken")
        if response.status_code in {408, 409, 429} or response.status_code >= 500:
            return build_probe_evidence(
                resource_id=resource_id,
                idempotency_key=idempotency_key,
                status=ResultProbeStatus.UNKNOWN,
                read=None,
                reasons=[f"authoritative read returned HTTP {response.status_code}"],
            )
        if response.status_code != 200:
            raise StripeProbeError(f"Unexpected Stripe read HTTP {response.status_code}: API contract broken")

        try:
            payload = response.json()
        except ValueError as exc:
            raise StripeProbeError("Stripe returned malformed PaymentIntent JSON") from exc
        try:
            read = StripePaymentIntentRead(
                payment_intent_id=str(payload["id"]),
                status=str(payload["status"]),
                amount_minor=int(payload["amount"]),
                currency=str(payload["currency"]),
                failure_code=payload.get("failure_code"),
                failure_message=payload.get("failure_message"),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise StripeProbeError(f"Unexpected Stripe PaymentIntent payload: {exc}") from exc

        reasons = [
            f"stripe_status={read.status}",
            *(f"failure_code={read.failure_code}" for _ in [0] if read.failure_code),
        ]
        return build_probe_evidence(
            resource_id=resource_id,
            idempotency_key=idempotency_key,
            status=classify_payment_intent(read),
            read=read,
            reasons=reasons,
        )


class RecordedStripeProbe(BusinessResultProbe):
    """Deterministic, network-free probe for unit tests and recorded demo mode.

    Fixtures map a PaymentIntent id to either a typed read or a transport
    failure (e.g. ``httpx.TimeoutException``) to exercise the UNKNOWN path
    without touching the network.
    """

    def __init__(self, fixtures: Mapping[str, StripePaymentIntentRead | BaseException]) -> None:
        self._fixtures = dict(fixtures)

    def probe(self, *, resource_id: str, idempotency_key: str) -> ResultProbeEvidence:
        fixture = self._fixtures.get(resource_id)
        if isinstance(fixture, BaseException):
            return build_probe_evidence(
                resource_id=resource_id,
                idempotency_key=idempotency_key,
                status=ResultProbeStatus.UNKNOWN,
                read=None,
                reasons=[f"recorded transport failure: {type(fixture).__name__}"],
            )
        if fixture is None:
            raise StripeProbeError(f"Recorded probe has no fixture for {resource_id}")
        return build_probe_evidence(
            resource_id=resource_id,
            idempotency_key=idempotency_key,
            status=classify_payment_intent(fixture),
            read=fixture,
            reasons=[f"stripe_status={fixture.status}"],
        )
