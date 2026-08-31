"""Injected AgentPact browser-loop ports and explicit runtime errors."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from enterprise.governance.pack_runtime import PackRuntimeBinding

from .contracts import (
    ActionDecision,
    AuthorizedAction,
    BrowserAction,
    BrowserActionResult,
    BrowserLoopEvent,
    BrowserLoopRunContext,
    BrowserObservation,
    ModelInput,
    PolicyAuthorization,
    RawBrowserObservation,
    VerificationRequest,
    VerificationResult,
)


class BrowserRuntimeError(RuntimeError):
    def __init__(self, message: str, *, effect_may_have_started: bool) -> None:
        self.effect_may_have_started = effect_may_have_started
        super().__init__(message)


class StaleObservationError(BrowserRuntimeError):
    def __init__(self, message: str = "Browser page changed after observation") -> None:
        super().__init__(message, effect_may_have_started=False)


@runtime_checkable
class BrowserRuntime(Protocol):
    async def observe(self) -> RawBrowserObservation: ...

    async def execute(self, command: AuthorizedAction) -> BrowserActionResult: ...


@runtime_checkable
class PreflightBrowserRuntime(BrowserRuntime, Protocol):
    async def preflight(self, command: AuthorizedAction) -> None: ...

    async def execute_preflighted(self, command: AuthorizedAction) -> BrowserActionResult: ...


@runtime_checkable
class PersistedExecutionPort(Protocol):
    async def execute(self, command: AuthorizedAction) -> BrowserActionResult: ...


@runtime_checkable
class BrowserActionModel(Protocol):
    async def decide(self, model_input: ModelInput) -> ActionDecision: ...


@runtime_checkable
class BrowserLoopPolicy(Protocol):
    async def prepare_model_input(
        self,
        *,
        run: BrowserLoopRunContext,
        observation: BrowserObservation,
    ) -> ModelInput: ...

    async def authorize_action(
        self,
        *,
        run: BrowserLoopRunContext,
        observation: BrowserObservation,
        action: BrowserAction,
        action_fingerprint: str,
    ) -> PolicyAuthorization: ...


@runtime_checkable
class BrowserLoopVerifier(Protocol):
    async def verify(self, request: VerificationRequest) -> VerificationResult: ...


@runtime_checkable
class BrowserLoopEventSink(Protocol):
    async def emit(self, event: BrowserLoopEvent) -> None: ...


@runtime_checkable
class DomainPackActionProvider(Protocol):
    @property
    def binding(self) -> PackRuntimeBinding: ...

    async def decide(
        self,
        *,
        run: BrowserLoopRunContext,
        observation: BrowserObservation,
    ) -> ActionDecision | None: ...
