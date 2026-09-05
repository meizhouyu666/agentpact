"""Typed contracts for the AgentPact browser operation boundary."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from enterprise.governance.contracts import ExecutionAuthorization, ExecutionEffect
from enterprise.governance.execution_profiles import ExecutionProfile
from enterprise.governance.pack_runtime import ExecutionCheckpoint, JsonValue


class ActionKind(StrEnum):
    CLICK = "click"
    INPUT_TEXT = "input_text"
    SELECT_OPTION = "select_option"
    GOTO_URL = "goto_url"
    KEYPRESS = "keypress"
    SCROLL = "scroll"
    WAIT = "wait"


class DecisionKind(StrEnum):
    ACTION = "action"
    SUCCESS = "success"
    FAILURE = "failure"


class DecisionSource(StrEnum):
    DOMAIN_PACK = "domain_pack"
    MODEL = "model"


class PolicyDisposition(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"
    REOBSERVE = "reobserve"


class VerificationDisposition(StrEnum):
    SUCCEEDED = "succeeded"
    CONTINUE = "continue"
    RETRY = "retry"
    FAILED = "failed"
    UNKNOWN = "unknown"


class BrowserLoopStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"


class BrowserSessionMode(StrEnum):
    """How a browser session is rendered and exposed to an operator."""

    HEADLESS = "headless"
    HEADED = "headed"
    REMOTE_INTERACTIVE = "remote_interactive"


class BrowserSessionPolicy(BaseModel):
    """Explicit browser-session capability requested by a Pack composition."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: BrowserSessionMode = BrowserSessionMode.HEADLESS
    allow_human_takeover: bool = False
    persistent_context: bool = False
    reason: str | None = Field(default=None, max_length=240)

    @model_validator(mode="after")
    def validate_mode(self) -> "BrowserSessionPolicy":
        if self.mode is BrowserSessionMode.REMOTE_INTERACTIVE and not self.allow_human_takeover:
            raise ValueError("remote_interactive browser sessions require human takeover")
        return self


class BrowserLoopRunContext(BaseModel):
    """Execution input supplied by the existing orchestration layer."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    step_id: str = Field(min_length=1)
    goal: str = Field(min_length=1)
    pack_id: str | None = None
    pack_version: str | None = None
    capability_id: str | None = None
    contract_id: str | None = None
    metadata: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_pack_binding(self) -> "BrowserLoopRunContext":
        values = (self.pack_id, self.pack_version, self.capability_id)
        if any(value is not None for value in values) and not all(value is not None for value in values):
            raise ValueError("Domain Pack execution requires pack_id, pack_version, and capability_id")
        return self


class BrowserElement(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    element_id: str
    tag_name: str
    role: str | None = None
    name: str | None = None
    text: str | None = None
    enabled: bool = True


class BrowserFrame(BaseModel):
    """Stable, non-executable metadata for a child browser frame."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    frame_id: str = Field(min_length=1)
    url: str
    name: str | None = None
    parent_frame_id: str | None = None


class BrowserPageState(BaseModel):
    """Current page state captured by an AgentPact-owned runtime."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    url: str
    title: str | None = None
    page_html: str


class RawBrowserObservation(BaseModel):
    """Ephemeral browser material returned by a runtime adapter."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    url: str
    title: str | None = None
    page_html: str
    model_dom: str
    screenshots: tuple[bytes, ...] = ()
    elements: tuple[BrowserElement, ...] = ()
    iframes: tuple[BrowserFrame, ...] = ()
    captured_at: datetime


class BrowserObservation(BaseModel):
    """Integrity-bound observation visible to decision and policy ports."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    observation_id: str
    snapshot_hash: str
    sequence: int = Field(ge=1)
    url: str
    title: str | None = None
    model_dom: str
    screenshots: tuple[bytes, ...] = ()
    elements: tuple[BrowserElement, ...] = ()
    iframes: tuple[BrowserFrame, ...] = ()
    captured_at: datetime


class ModelInput(BaseModel):
    """Exact policy-approved payload sent to the injected model provider."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    observation_id: str
    goal: str
    url: str
    dom: str
    screenshots: tuple[bytes, ...] = ()
    iframes: tuple[BrowserFrame, ...] = ()
    allowed_action_kinds: tuple[ActionKind, ...] = tuple(ActionKind)


class BrowserAction(BaseModel):
    """One proposed technical action; policy remains authoritative for effect."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: ActionKind
    operation: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    element_id: str | None = None
    text: str | None = Field(default=None, repr=False)
    option_value: str | None = Field(default=None, repr=False)
    url: str | None = None
    keys: tuple[str, ...] = ()
    scroll_x: int = 0
    scroll_y: int = 0
    wait_seconds: float = Field(default=0.0, ge=0.0, le=30.0)

    @model_validator(mode="after")
    def validate_shape(self) -> "BrowserAction":
        element_actions = {ActionKind.CLICK, ActionKind.INPUT_TEXT, ActionKind.SELECT_OPTION, ActionKind.KEYPRESS}
        if self.kind in element_actions and not self.element_id:
            raise ValueError(f"{self.kind.value} requires element_id")
        if self.kind is ActionKind.INPUT_TEXT and self.text is None:
            raise ValueError("input_text requires text")
        if self.kind is ActionKind.SELECT_OPTION and self.option_value is None:
            raise ValueError("select_option requires option_value")
        if self.kind is ActionKind.GOTO_URL and not self.url:
            raise ValueError("goto_url requires url")
        if self.kind is ActionKind.KEYPRESS and not self.keys:
            raise ValueError("keypress requires keys")
        if self.kind is ActionKind.SCROLL and self.scroll_x == 0 and self.scroll_y == 0:
            raise ValueError("scroll requires a non-zero delta")
        if self.kind is ActionKind.WAIT and self.wait_seconds <= 0:
            raise ValueError("wait requires a positive duration")
        return self


class ActionDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: DecisionKind
    observation_id: str
    action: BrowserAction | None = None
    reason_code: str = Field(min_length=1, max_length=128, pattern=r"^[A-Z][A-Z0-9_]*$")

    @model_validator(mode="after")
    def validate_decision(self) -> "ActionDecision":
        if (self.kind is DecisionKind.ACTION) != (self.action is not None):
            raise ValueError("Only an action decision may contain an action")
        return self


class PolicyAuthorization(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    disposition: PolicyDisposition
    reason_code: str = Field(min_length=1, max_length=128, pattern=r"^[A-Z][A-Z0-9_]*$")
    authorization: ExecutionAuthorization | None = None
    execution_profile: ExecutionProfile | None = None
    approval_ref: str | None = Field(default=None, min_length=1, max_length=256)

    @model_validator(mode="after")
    def validate_authority(self) -> "PolicyAuthorization":
        if self.disposition is PolicyDisposition.ALLOW:
            if self.authorization is None or self.execution_profile is None:
                raise ValueError("Allowed actions require authorization and an execution profile")
            if self.approval_ref is not None:
                raise ValueError("Allowed actions cannot retain a pending approval reference")
        elif self.authorization is not None or self.execution_profile is not None:
            raise ValueError("Non-allowed actions cannot carry execution authority")
        if (self.disposition is PolicyDisposition.REQUIRE_APPROVAL) != (self.approval_ref is not None):
            raise ValueError("Approval disposition requires exactly one approval reference")
        return self


class AuthorizedAction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    action: BrowserAction
    action_fingerprint: str
    observation_id: str
    expected_snapshot_hash: str
    authorization: ExecutionAuthorization
    execution_profile: ExecutionProfile


class BrowserActionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    completed: bool
    effect_may_have_started: bool = False
    detail_code: str = Field(min_length=1, max_length=128, pattern=r"^[A-Z][A-Z0-9_]*$")
    pending_result_probe: bool = False
    execution_checkpoint: ExecutionCheckpoint | None = None

    @model_validator(mode="after")
    def validate_checkpoint(self) -> "BrowserActionResult":
        if self.pending_result_probe != (self.execution_checkpoint is not None):
            raise ValueError("Pending result probe requires exactly one execution checkpoint")
        return self


class VerificationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run: BrowserLoopRunContext
    before: BrowserObservation
    after: BrowserObservation
    decision: ActionDecision
    source: DecisionSource
    action_result: BrowserActionResult | None = None
    authorized_effect: ExecutionEffect | None = None


class VerificationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    disposition: VerificationDisposition
    reason_code: str = Field(min_length=1, max_length=128, pattern=r"^[A-Z][A-Z0-9_]*$")
    evidence_refs: tuple[str, ...] = ()


class BrowserLoopEvent(BaseModel):
    """Redacted event contract suitable for an Agent Run event sink."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sequence: int = Field(ge=1)
    run_id: str
    task_id: str
    step_id: str
    stage: Literal["observation", "decision", "policy", "action", "verification", "terminal"]
    code: str = Field(min_length=1, max_length=128, pattern=r"^[A-Z][A-Z0-9_]*$")
    occurred_at: datetime
    observation_id: str | None = None
    action_fingerprint: str | None = None
    details: dict[str, JsonValue] = Field(default_factory=dict)


class BrowserLoopReport(BaseModel):
    """Terminal or approval-pause result returned to the Agent Run owner."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["agentpact-browser-loop/v1"] = "agentpact-browser-loop/v1"
    run_id: str
    task_id: str
    step_id: str
    status: BrowserLoopStatus
    reason_code: str
    iterations: int = Field(ge=0)
    retries_used: int = Field(ge=0)
    observations: int = Field(ge=0)
    actions_executed: int = Field(ge=0)
    last_observation_id: str | None = None
    approval_ref: str | None = None
    execution_checkpoint: ExecutionCheckpoint | None = None
    events: tuple[BrowserLoopEvent, ...] = ()


class BrowserLoopConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_iterations: int = Field(default=20, ge=1, le=200)
    max_retries: int = Field(default=3, ge=0, le=20)
    max_observation_age_seconds: float = Field(default=30.0, gt=0.0, le=300.0)
