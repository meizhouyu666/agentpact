"""Stable Phase 2 contracts shared by governance, audit, and future planning.

These models intentionally do not execute browser actions.  They make the
business and security context explicit while Phase 2 operates in audit-only
mode.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class GovernanceMode(StrEnum):
    OFF = "off"
    AUDIT = "audit"
    ENFORCE = "enforce"


class PageReadiness(StrEnum):
    LOADING = "loading"
    READY = "ready"
    TRANSITIONING = "transitioning"
    BLOCKED = "blocked"
    UNKNOWN = "unknown"


class ExecutionEffect(StrEnum):
    NONE = "none"
    READ = "read"
    INTERNAL_WRITE = "internal_write"
    EXTERNAL_WRITE = "external_write"


class DecisionOutcome(StrEnum):
    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"
    NEEDS_HUMAN = "needs_human"


class ExecutionAttemptStatus(StrEnum):
    AUTHORIZED = "authorized"
    EXECUTING = "executing"
    CONFIRMED = "confirmed"
    UNKNOWN = "unknown"
    FAILED = "failed"


class PendingActionStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    INVALIDATED = "invalidated"


class TaskContract(BaseModel):
    """Persistent task-level authorization and policy snapshot."""

    contract_id: str
    task_id: str
    organization_id: str
    initiator_id: str | None = None
    service_principal_id: str | None = None
    department_id: str | None = None
    business_line_id: str | None = None
    goal: str
    allowed_operations: set[str] = Field(default_factory=set)
    data_scope: dict[str, Any] = Field(default_factory=dict)
    authorization_snapshot: dict[str, Any] = Field(default_factory=dict)
    policy_profile: str = "financial-default"
    policy_version: str = "phase2-v1"
    success_criteria: list[str] = Field(default_factory=list)
    expires_at: datetime | None = None
    version: int = 1
    mode: GovernanceMode = GovernanceMode.AUDIT


class ObservationContext(BaseModel):
    """A redacted, versioned description of what the agent can currently observe."""

    observation_id: str
    task_id: str
    step_id: str
    page_url: str
    snapshot_hash: str
    page_title: str | None = None
    screenshot_artifact_id: str | None = None
    page_type: str | None = None
    semantic_summary: str = ""
    readiness: PageReadiness = PageReadiness.UNKNOWN
    readiness_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    dom_signals: dict[str, Any] = Field(default_factory=dict)
    visual_signals: dict[str, Any] = Field(default_factory=dict)
    target_evidence: dict[str, Any] = Field(default_factory=dict)
    captured_at: datetime


class ActionIntent(BaseModel):
    """Business interpretation of one technical browser Action."""

    intent_id: str
    task_id: str
    step_id: str
    action_fingerprint: str
    observation_id: str
    operation: str
    effect: ExecutionEffect
    target: dict[str, Any] = Field(default_factory=dict)
    sensitive_data_types: set[str] = Field(default_factory=set)
    extracted_facts: dict[str, Any] = Field(default_factory=dict)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    evidence: list[str] = Field(default_factory=list)
    expected_outcome: dict[str, Any] = Field(default_factory=dict)


class PolicyDecision(BaseModel):
    """Deterministic policy result; LLM reasoning is evidence, never authority."""

    decision_id: str
    intent_id: str
    outcome: DecisionOutcome
    risk_level: Literal["low", "medium", "high", "critical", "unknown"]
    reasons: list[str] = Field(default_factory=list)
    matched_rules: list[str] = Field(default_factory=list)
    required_approver: dict[str, Any] | None = None
    policy_version: str


class ExecutionPermit(BaseModel):
    """Short-lived one-time permit for a future enforce-mode execution."""

    permit_id: str
    task_id: str
    step_id: str
    action_fingerprint: str
    observation_id: str
    policy_decision_id: str
    issued_at: datetime
    expires_at: datetime
    used_at: datetime | None = None

    def matches(self, *, action_fingerprint: str, observation_id: str, now: datetime) -> bool:
        return (
            self.used_at is None
            and self.action_fingerprint == action_fingerprint
            and self.observation_id == observation_id
            and now <= self.expires_at
        )


class ExecutionAuthorization(BaseModel):
    """Opaque permit reference supplied only to the governed handler boundary."""

    permit_id: str
    action_fingerprint: str
    observation_hash: str
    idempotency_key: str
    effect: ExecutionEffect


class ExecutionAttempt(BaseModel):
    """Durable record around a potentially non-idempotent browser commit."""

    attempt_id: str
    task_id: str
    step_id: str
    contract_id: str
    action_fingerprint: str
    observation_hash: str
    idempotency_key: str
    status: ExecutionAttemptStatus
    started_at: datetime | None = None
    completed_at: datetime | None = None
    result_probe: dict[str, Any] | None = None
    error_message: str | None = None


class PendingAction(BaseModel):
    """Persistent approval pause; it never authorizes replay of its action payload."""

    pending_action_id: str
    task_id: str
    step_id: str
    contract_id: str
    organization_id: str
    action_fingerprint: str
    observation_hash: str
    status: PendingActionStatus
    approval_id: str | None = None
    row_version: int
    expires_at: datetime | None = None
