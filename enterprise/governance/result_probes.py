"""Business-result evidence contracts independent from browser success."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, Field


class ResultProbeStatus(StrEnum):
    CONFIRMED = "confirmed"
    NOT_CONFIRMED = "not_confirmed"
    UNKNOWN = "unknown"


class ResultProbeEvidence(BaseModel):
    probe_ref: str
    status: ResultProbeStatus
    resource_id: str
    checked_at: datetime
    observed_version: int | None = None
    business_reference: str | None = None
    facts_hash: str | None = None
    reasons: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class BusinessResultProbe(Protocol):
    def probe(self, *, resource_id: str, idempotency_key: str) -> ResultProbeEvidence:
        """Read canonical business state without performing a side effect."""

