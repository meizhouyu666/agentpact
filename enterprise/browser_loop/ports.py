"""Injected AgentPact browser-loop ports and explicit runtime errors."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from enterprise.governance.pack_runtime import PackRuntimeBinding

from .contracts import (
    ActionDecision,
    AuthorizedAction,
    BrowserAction,
    BrowserActionResult,
    BrowserFrame,
    BrowserLoopEvent,
    BrowserLoopRunContext,
    BrowserObservation,
    BrowserPageState,
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
class AgentPactBrowserRuntime(BrowserRuntime, Protocol):
    """Full runtime capability owned by AgentPact.

    ``BrowserRuntime`` remains intentionally small for existing injected fakes and
    callers. New session owners can opt into this stronger contract without making
    the operation loop or compatibility adapters implement lifecycle methods.
    """

    async def close(self) -> None: ...

    async def fresh_observation(self) -> RawBrowserObservation: ...

    async def page_state(self) -> BrowserPageState: ...

    async def screenshot(self) -> bytes: ...

    async def enumerate_iframes(self) -> tuple[BrowserFrame, ...]: ...

    async def normalized_interactable_tree(self) -> str: ...


@runtime_checkable
class BrowserSession(Protocol):
    """AgentPact-owned browser session lifecycle."""

    @property
    def session_id(self) -> str: ...

    @property
    def runtime(self) -> AgentPactBrowserRuntime: ...

    async def close(self) -> None: ...


@runtime_checkable
class BrowserSessionFactory(Protocol):
    async def open(self, *, session_id: str | None = None) -> BrowserSession: ...


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
