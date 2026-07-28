from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class PaymentStatus(StrEnum):
    DRAFT = "draft"
    SUBMITTED = "submitted"


class FaultMode(StrEnum):
    NONE = "none"
    FAIL_BEFORE_COMMIT = "fail_before_commit"
    COMMIT_THEN_TIMEOUT = "commit_then_timeout"
    COMMIT_THEN_INCONCLUSIVE = "commit_then_inconclusive"


class PaymentFacts(BaseModel):
    payment_id: str = Field(min_length=1, max_length=100)
    beneficiary_id: str = Field(min_length=1, max_length=100)
    amount: Decimal = Field(gt=0, max_digits=18, decimal_places=2)
    currency: Literal["CNY", "USD", "EUR"] = "CNY"
    reference: str = Field(min_length=1, max_length=200)
    object_version: int = Field(default=1, ge=1)


class SyntheticPaymentRecord(BaseModel):
    facts: PaymentFacts
    status: PaymentStatus = PaymentStatus.DRAFT
    requester_user_id: str
    approval_id: str | None = None
    idempotency_key: str | None = None
    confirmation_reference: str | None = None
    commit_count: int = 0


class SubmissionRisk(BaseModel):
    risk_level: Literal["high", "critical"]
    approver_department_id: str
    reasons: list[str]


class SyntheticPaymentError(ValueError):
    pass


class DefiniteSubmissionFailure(SyntheticPaymentError):
    pass


class AmbiguousSubmissionFailure(SyntheticPaymentError):
    pass
