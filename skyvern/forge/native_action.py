"""Typed scoped-governance contract at the native Agent action boundary."""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from enterprise.governance.contracts import ExecutionAuthorization
from enterprise.governance.execution_profiles import ExecutionProfile

if TYPE_CHECKING:
    from skyvern.forge.sdk.models import Step
    from skyvern.forge.sdk.schemas.tasks import Task
    from skyvern.webeye.actions.actions import Action
    from skyvern.webeye.scraper.scraped_page import ScrapedPage


M7_APPLICATION_MARKER = "agentpact:m7:v1"


def carries_m7_native_binding(*, task: "Task", step: "Step") -> bool:
    """Return whether the in-memory native pair advertises any M7 binding marker."""

    return task.application == M7_APPLICATION_MARKER or step.created_by == M7_APPLICATION_MARKER


class NativeActionDisposition(StrEnum):
    UNBOUND_COMPATIBILITY = "unbound_compatibility"
    BOUND_NON_EFFECT = "bound_non_effect"
    BOUND_AUTHORIZED_EFFECT = "bound_authorized_effect"
    BOUND_DENIED = "bound_denied"


class PostActionControl(StrEnum):
    CONTINUE = "continue"
    SUSPEND_FOR_PROBE = "suspend_for_probe"


class NativeActionResolution(BaseModel):
    """Exact resolver result consumed once by the native handler call."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    disposition: NativeActionDisposition
    operation: str | None = None
    binding_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    observation_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    action_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    execution_authorization: ExecutionAuthorization | None = None
    execution_profile: ExecutionProfile | None = None
    denial_code: str | None = None

    @model_validator(mode="after")
    def validate_disposition_contract(self) -> "NativeActionResolution":
        complete_authority = self.execution_authorization is not None and self.execution_profile is not None
        if self.disposition is NativeActionDisposition.UNBOUND_COMPATIBILITY:
            if any(
                value is not None
                for value in (
                    self.operation,
                    self.binding_digest,
                    self.observation_hash,
                    self.action_fingerprint,
                    self.execution_authorization,
                    self.execution_profile,
                    self.denial_code,
                )
            ):
                raise ValueError("Unbound compatibility cannot carry M7 authority or denial state")
        elif self.disposition is NativeActionDisposition.BOUND_NON_EFFECT:
            if not self.operation or not self.binding_digest or complete_authority or self.denial_code is not None:
                raise ValueError("Bound non-effect requires verified binding context without execution authority")
        elif self.disposition is NativeActionDisposition.BOUND_AUTHORIZED_EFFECT:
            if not (
                self.operation
                and self.binding_digest
                and self.observation_hash
                and self.action_fingerprint
                and complete_authority
            ) or self.denial_code is not None:
                raise ValueError("Bound effect requires complete fresh Permit-backed authority")
            assert self.execution_authorization is not None
            if (
                self.execution_authorization.observation_hash != self.observation_hash
                or self.execution_authorization.action_fingerprint != self.action_fingerprint
            ):
                raise ValueError("Bound effect authorization does not match the resolved action and observation")
        elif not self.denial_code or any(
            value is not None
            for value in (
                self.execution_authorization,
                self.execution_profile,
                self.observation_hash,
                self.action_fingerprint,
            )
        ):
            raise ValueError("Bound denial must carry only a stable denial code")
        return self


class NativeActionHandlerOutcome(BaseModel):
    """Backward-compatible list payload plus native post-action control."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    results: list[Any]
    post_action_control: PostActionControl = PostActionControl.CONTINUE
    attempt_id: str | None = None


class NativeActionContextProvider(Protocol):
    async def resolve(
        self,
        *,
        task: "Task",
        step: "Step",
        scraped_page: "ScrapedPage",
        action: "Action",
    ) -> NativeActionResolution: ...

    async def suspend_for_probe(
        self,
        *,
        task: "Task",
        step: "Step",
        resolution: NativeActionResolution,
        attempt_id: str,
    ) -> "Step": ...


class NativeGovernanceDenied(PermissionError):
    """A bound native action failed closed before ActionHandler invocation."""
