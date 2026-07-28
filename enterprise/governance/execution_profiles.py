"""Fallback mechanism and one-observation batch policy contracts."""

from __future__ import annotations

import hmac
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from enum import StrEnum

from pydantic import BaseModel, Field, model_validator

from .contracts import ActionIntent, ExecutionEffect


class ExecutionMechanism(StrEnum):
    LOCATOR = "locator"
    LABEL = "label"
    COORDINATE = "coordinate"
    JAVASCRIPT = "javascript"
    CUA_COORDINATE = "cua_coordinate"


class CUAEngine(StrEnum):
    OPENAI = "openai-cua"
    ANTHROPIC = "anthropic-cua"
    UI_TARS = "ui-tars"


class CUAExecutionEvidence(BaseModel):
    """Fresh engine evidence bound to one authorized CUA Action and page."""

    engine: CUAEngine
    action_fingerprint: str = Field(min_length=1)
    observation_hash: str = Field(min_length=1)
    evidence_refs: list[str] = Field(min_length=1)
    captured_at: datetime


_HANDLER_FALLBACK_ORDER = {
    ExecutionMechanism.LOCATOR: 0,
    ExecutionMechanism.LABEL: 1,
    ExecutionMechanism.COORDINATE: 2,
    ExecutionMechanism.JAVASCRIPT: 3,
}
_PROFILE_FALLBACK_RANK = {
    **_HANDLER_FALLBACK_ORDER,
    ExecutionMechanism.CUA_COORDINATE: 0,
}


class ExecutionProfile(BaseModel):
    """The weakest mechanism one governed handler attempt may reach.

    Locator/label/coordinate/JavaScript form the existing ActionHandler fallback
    chain. A profile for a later mechanism also permits the earlier mechanisms;
    CUA is an independent path and never inherits the DOM fallback chain.
    """

    mechanism: ExecutionMechanism
    fallback_rank: int = Field(default=0, ge=0)
    evidence_refs: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_fallback_rank(self) -> ExecutionProfile:
        expected = _PROFILE_FALLBACK_RANK[self.mechanism]
        if self.fallback_rank != expected:
            raise ValueError(f"fallback_rank for {self.mechanism.value} must be {expected}")
        return self


class ProfileDecision(BaseModel):
    allowed: bool
    requires_reobservation: bool = False
    reason: str


class ExecutionProfileRejected(PermissionError):
    """Raised before Permit consumption when a profile is policy-ineligible."""


_ACTIVE_EXECUTION_PROFILE: ContextVar[ExecutionProfile | None] = ContextVar(
    "phase2_active_execution_profile",
    default=None,
)
CUA_EVIDENCE_MAX_AGE = timedelta(seconds=30)


def decide_profile(*, effect: ExecutionEffect, profile: ExecutionProfile) -> ProfileDecision:
    """Keep weak fallback mechanisms out of high-impact automatic paths."""

    weak = {ExecutionMechanism.COORDINATE, ExecutionMechanism.JAVASCRIPT, ExecutionMechanism.CUA_COORDINATE}
    if effect is ExecutionEffect.EXTERNAL_WRITE and profile.mechanism in weak:
        return ProfileDecision(
            allowed=False,
            requires_reobservation=True,
            reason="Weak fallback cannot auto-cross an external commit boundary",
        )
    if not profile.evidence_refs:
        return ProfileDecision(
            allowed=False,
            requires_reobservation=True,
            reason="ExecutionProfile requires evidence references",
        )
    return ProfileDecision(allowed=True, requires_reobservation=False, reason="ExecutionProfile is policy-eligible")


def require_allowed_profile(*, effect: ExecutionEffect, profile: ExecutionProfile) -> ProfileDecision:
    """Fail closed before a Permit is consumed for a disallowed mechanism."""

    decision = decide_profile(effect=effect, profile=profile)
    if not decision.allowed:
        raise ExecutionProfileRejected(decision.reason)
    return decision


def require_cua_execution_evidence(
    *,
    profile: ExecutionProfile,
    evidence: CUAExecutionEvidence | None,
    action_fingerprint: str,
    observation_hash: str,
    now: datetime | None = None,
) -> None:
    """Require fresh, exact engine evidence only for the CUA mechanism."""

    if profile.mechanism is not ExecutionMechanism.CUA_COORDINATE:
        if evidence is not None:
            raise ExecutionProfileRejected("CUA evidence cannot authorize a non-CUA execution profile")
        return
    if evidence is None:
        raise ExecutionProfileRejected("CUA execution profile requires fresh engine evidence")
    if not (
        hmac.compare_digest(evidence.action_fingerprint, action_fingerprint)
        and hmac.compare_digest(evidence.observation_hash, observation_hash)
    ):
        raise ExecutionProfileRejected("CUA engine evidence does not match the authorized Action and observation")

    current = now or datetime.now(timezone.utc)
    captured = evidence.captured_at
    if captured.tzinfo is None:
        captured = captured.replace(tzinfo=timezone.utc)
    if captured > current + timedelta(seconds=5):
        raise ExecutionProfileRejected("CUA engine evidence timestamp is in the future")
    if current - captured > CUA_EVIDENCE_MAX_AGE:
        raise ExecutionProfileRejected("CUA engine evidence is stale and requires fresh observation")


@contextmanager
def governed_execution_profile(profile: ExecutionProfile) -> Iterator[None]:
    """Bind a validated profile to one asynchronous browser-attempt context."""

    token = _ACTIVE_EXECUTION_PROFILE.set(profile)
    try:
        yield
    finally:
        _ACTIVE_EXECUTION_PROFILE.reset(token)


def execution_mechanism_is_allowed(mechanism: ExecutionMechanism) -> bool:
    """Return whether the active governed attempt may use ``mechanism``.

    No active profile means the unchanged Phase 1 ``off``/``audit`` path.
    """

    profile = _ACTIVE_EXECUTION_PROFILE.get()
    if profile is None:
        return True
    if profile.mechanism is ExecutionMechanism.CUA_COORDINATE:
        return mechanism in {ExecutionMechanism.CUA_COORDINATE, ExecutionMechanism.COORDINATE}
    if mechanism is ExecutionMechanism.CUA_COORDINATE:
        return False
    return _HANDLER_FALLBACK_ORDER[mechanism] <= _HANDLER_FALLBACK_ORDER[profile.mechanism]


def require_execution_mechanism(mechanism: ExecutionMechanism) -> None:
    """Reject a mechanism outside the active governed profile."""

    if not execution_mechanism_is_allowed(mechanism):
        profile = _ACTIVE_EXECUTION_PROFILE.get()
        assert profile is not None
        raise ExecutionProfileRejected(
            f"Execution mechanism {mechanism.value} exceeds governed profile {profile.mechanism.value}"
        )


def require_single_state_change(intents: list[ActionIntent]) -> None:
    """A single observation may contain at most one page or external state change."""

    changing = [intent for intent in intents if intent.effect in {ExecutionEffect.INTERNAL_WRITE, ExecutionEffect.EXTERNAL_WRITE}]
    if len(changing) > 1:
        raise ValueError("Re-observation and reauthorization are required after one state-changing action")
