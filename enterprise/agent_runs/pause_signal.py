"""Platform-neutral AgentPact pause outcomes.

This module is a pure contract boundary.  It does not execute, persist, or
translate adapter-specific state; adapters must construct these redacted
signals before crossing into the platform.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from enterprise.governance.input_contracts import InputRequest


class RunPauseOutcome(StrEnum):
    """Non-terminal outcomes which pause an AgentPact run."""

    AWAITING_INPUT = "awaiting_input"
    NEEDS_HUMAN = "needs_human"


class RunPauseAction(StrEnum):
    """Platform actions a caller may offer for a paused run."""

    SUBMIT_INPUT = "submit_input"
    RESUME = "resume"
    TAKE_OVER = "take_over"
    CANCEL = "cancel"


class RunResumePolicy(StrEnum):
    """How a paused run may become runnable again."""

    INPUT_SUBMISSION = "input_submission"
    HUMAN_TAKEOVER = "human_takeover"
    MANUAL_REVIEW = "manual_review"
    NO_RESUME = "no_resume"


class RunPausePromptMetadata(BaseModel):
    """Small, already-redacted metadata suitable for a user-facing prompt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    title: str = Field(min_length=1, max_length=120)
    message: str = Field(min_length=1, max_length=2000)
    locale: str | None = Field(default=None, min_length=2, max_length=32)
    redacted: Literal[True] = True


class RunPauseSignal(BaseModel):
    """Generic redacted pause signal emitted at the AgentPact boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome: RunPauseOutcome
    reason_code: str = Field(min_length=2, max_length=64, pattern=r"^[A-Z][A-Z0-9_]*$")
    run_id: str = Field(min_length=1)
    task_id: str | None = Field(default=None, min_length=1)
    step_id: str | None = Field(default=None, min_length=1)
    checkpoint_id: str | None = Field(default=None, min_length=1)
    input_request: InputRequest | None = None
    prompt: RunPausePromptMetadata | None = None
    allowed_actions: tuple[RunPauseAction, ...] = Field(default=())
    resume_policy: RunResumePolicy
    external_effect_started: bool = False
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def validate_invariants(self) -> RunPauseSignal:
        if len(self.allowed_actions) != len(set(self.allowed_actions)):
            raise ValueError("Pause signal allowed actions must be unique")
        if self.expires_at is not None and self.expires_at.tzinfo is None:
            raise ValueError("Pause signal expiry must be timezone-aware")
        if self.outcome is RunPauseOutcome.AWAITING_INPUT:
            if self.external_effect_started:
                raise ValueError("AWAITING_INPUT is permitted only before an external effect")
            if self.input_request is None:
                raise ValueError("AWAITING_INPUT requires an input request")
            if RunPauseAction.SUBMIT_INPUT not in self.allowed_actions:
                raise ValueError("AWAITING_INPUT requires submit-input action")
            if self.resume_policy is not RunResumePolicy.INPUT_SUBMISSION:
                raise ValueError("AWAITING_INPUT requires input-submission resume policy")
        elif self.resume_policy is RunResumePolicy.INPUT_SUBMISSION:
            raise ValueError("Input-submission resume policy is only valid for AWAITING_INPUT")
        if self.input_request is not None:
            if self.input_request.external_effect_started != self.external_effect_started:
                raise ValueError("Input request and pause signal external-effect state must match")
            if self.input_request.recovery and self.outcome is not RunPauseOutcome.AWAITING_INPUT:
                raise ValueError("Input recovery is only valid for AWAITING_INPUT")
            if self.input_request.recovery and self.external_effect_started:
                raise ValueError("Input recovery cannot replay after an external effect")
        if self.external_effect_started and RunPauseAction.SUBMIT_INPUT in self.allowed_actions:
            raise ValueError("Input submission is not allowed after an external effect")
        return self


# Friendly aliases keep the contract vocabulary discoverable without creating
# duplicate models or versioned representations.
PauseOutcome = RunPauseOutcome
PauseAction = RunPauseAction
ResumePolicy = RunResumePolicy
PromptMetadata = RunPausePromptMetadata

__all__ = [
    "PauseAction",
    "PauseOutcome",
    "PromptMetadata",
    "ResumePolicy",
    "RunPauseAction",
    "RunPauseOutcome",
    "RunPausePromptMetadata",
    "RunPauseSignal",
    "RunResumePolicy",
]
