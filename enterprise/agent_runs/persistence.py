"""AgentPact-owned persistence contracts for Agent Run native state.

The Agent Run core deliberately deals in these snapshots rather than Skyvern
ORM objects.  A compatibility adapter is responsible for translating the
snapshots to the currently deployed native task/step tables.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field


class AgentRunTaskStatus(StrEnum):
    """Task statuses understood by the Agent Run projection boundary."""

    UNKNOWN = "unknown"
    CREATED = "created"
    QUEUED = "queued"
    RUNNING = "running"
    PENDING_APPROVAL = "pending_approval"
    RESUMING = "resuming"
    NEEDS_HUMAN = "needs_human"
    PENDING_RESULT_PROBE = "pending_result_probe"
    TIMED_OUT = "timed_out"
    FAILED = "failed"
    TERMINATED = "terminated"
    COMPLETED = "completed"
    CANCELED = "canceled"

    # Lower-case aliases mirror the legacy enum spelling without importing it.
    unknown = UNKNOWN
    created = CREATED
    queued = QUEUED
    running = RUNNING
    pending_approval = PENDING_APPROVAL
    resuming = RESUMING
    needs_human = NEEDS_HUMAN
    pending_result_probe = PENDING_RESULT_PROBE
    timed_out = TIMED_OUT
    failed = FAILED
    terminated = TERMINATED
    completed = COMPLETED
    canceled = CANCELED

class AgentRunStepStatus(StrEnum):
    """Step statuses understood by the Agent Run projection boundary."""

    UNKNOWN = "unknown"
    CREATED = "created"
    RUNNING = "running"
    PENDING_APPROVAL = "pending_approval"
    RESUMING = "resuming"
    NEEDS_HUMAN = "needs_human"
    PENDING_RESULT_PROBE = "pending_result_probe"
    FAILED = "failed"
    COMPLETED = "completed"
    CANCELED = "canceled"

    # Lower-case aliases mirror the legacy enum spelling without importing it.
    unknown = UNKNOWN
    created = CREATED
    running = RUNNING
    pending_approval = PENDING_APPROVAL
    resuming = RESUMING
    needs_human = NEEDS_HUMAN
    pending_result_probe = PENDING_RESULT_PROBE
    failed = FAILED
    completed = COMPLETED
    canceled = CANCELED

class AgentRunTaskSnapshot(BaseModel):
    """Stable, redacted view of a native task row used by Agent Run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str = Field(min_length=1)
    organization_id: str = Field(min_length=1)
    status: AgentRunTaskStatus
    application: str | None = None
    created_at: datetime
    modified_at: datetime


class AgentRunStepSnapshot(BaseModel):
    """Stable, redacted view of a native step row used by Agent Run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    step_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    organization_id: str = Field(min_length=1)
    status: AgentRunStepStatus


AgentRunNativePair = tuple[AgentRunTaskSnapshot | None, AgentRunStepSnapshot | None]


class AgentRunNativeStore(Protocol):
    """Persistence boundary consumed by Agent Run service and journal code.

    ``session`` is intentionally opaque here.  The application owns the
    transaction and passes the active session to the adapter, which keeps
    locking and flush semantics identical to the legacy implementation.
    """

    async def get_root(
        self,
        session: Any,
        *,
        run_id: str,
        organization_id: str,
        lock: bool = False,
    ) -> AgentRunTaskSnapshot | None: ...

    async def list_roots(
        self,
        session: Any,
        *,
        organization_id: str,
        boundary: tuple[datetime, str] | None = None,
        limit: int = 21,
    ) -> Sequence[AgentRunTaskSnapshot]: ...

    async def get_native_pair(
        self,
        session: Any,
        *,
        task_id: str,
        step_id: str,
        organization_id: str,
    ) -> AgentRunNativePair: ...

    async def cancel_native_pair(
        self,
        session: Any,
        *,
        task_id: str,
        step_id: str,
        organization_id: str,
    ) -> bool: ...

    async def verify_checkpoint_native_state(
        self,
        session: Any,
        checkpoint: Any,
        *,
        transition: Any,
        organization_id: str,
    ) -> None: ...

# Short aliases make the contract convenient for integrations and fixtures
# while retaining the explicit Agent Run names as the canonical API.
NativeTaskStatus = AgentRunTaskStatus
NativeStepStatus = AgentRunStepStatus
NativeTaskSnapshot = AgentRunTaskSnapshot
NativeStepSnapshot = AgentRunStepSnapshot
AgentPactTaskStatus = AgentRunTaskStatus
AgentPactStepStatus = AgentRunStepStatus
AgentPactTaskSnapshot = AgentRunTaskSnapshot
AgentPactStepSnapshot = AgentRunStepSnapshot
