"""Stripe test-mode business facts and records for the governed pack.

Amounts follow the Stripe API convention: integer minor units (e.g. cents),
never floats. The idempotency key is derived by trusted code from the payment
identity and never enters the model projection (see PACK.md P1).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

# ISO 4217 currencies accepted by the pack policy in test mode.
AllowedCurrency = Literal["usd", "eur", "gbp", "cny"]


class StripePaymentStatus(StrEnum):
    """Governed lifecycle states (not a mirror of every Stripe API status).

    Business terminality is decided by the Result Probe, not by the
    capability: Stripe's ``succeeded`` maps to CONFIRMED, ``processing`` /
    ``requires_*`` to UNKNOWN, ``canceled`` to NOT_CONFIRMED.
    """

    DRAFT = "draft"
    SUBMITTED = "submitted"


class StripeOutcome(StrEnum):
    """Authoritative outcome the simulated Stripe backend assigns on submit.

    Mirrors the subset of PaymentIntent statuses the pack treats as terminal
    or pending: ``succeeded`` confirms the submission, ``processing`` leaves
    the outcome undecidable (UNKNOWN), ``canceled`` proves no submission.
    """

    SUCCEEDED = "succeeded"
    PROCESSING = "processing"
    CANCELED = "canceled"


class StripePaymentFacts(BaseModel):
    """Canonical business facts for one governed test-mode payment."""

    payment_intent_id: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^pi_[A-Za-z0-9_]+$",
        description="Stripe PaymentIntent id (pi_...)",
    )
    customer_id: str | None = Field(default=None, max_length=100, description="Stripe Customer id (cus_...), optional")
    amount_minor: int = Field(gt=0, le=1_000_000_000, description="Amount in minor units (e.g. cents)")
    currency: AllowedCurrency = "usd"
    description: str = Field(default="", max_length=500)
    object_version: int = Field(default=1, ge=1)


class StripePaymentRecord(BaseModel):
    """Persisted governed record: facts + attempt/approval/probe correlation."""

    facts: StripePaymentFacts
    status: StripePaymentStatus = StripePaymentStatus.DRAFT
    requester_user_id: str
    approval_id: str | None = None
    idempotency_key: str | None = None
    confirmation_reference: str | None = None
    commit_count: int = 0


class SubmissionRisk(BaseModel):
    risk_level: Literal["high", "critical"]
    approver_department_id: str
    reasons: list[str]


class StripePaymentError(ValueError):
    pass


class DefiniteSubmissionFailure(StripePaymentError):
    """Stripe confirmed the submission did not happen (e.g. 404)."""


class AmbiguousSubmissionFailure(StripePaymentError):
    """Outcome is unconfirmed; the governed path must enter UNKNOWN, not replay."""
