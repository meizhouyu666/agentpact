"""Application-owned scheduling hook for persisted Agent Run recovery."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timedelta
from typing import Any

from enterprise.browser_loop.persisted_executor import (
    PersistedExecutionRecoveryReport,
    recover_abandoned_persisted_executions,
)


async def recover_abandoned_agent_run_executions(
    session_factory: Callable[[], AbstractAsyncContextManager[Any]],
    *,
    minimum_age: timedelta,
    now: datetime | None = None,
) -> PersistedExecutionRecoveryReport:
    """Run the generic no-replay scanner at an application-owned boundary.

    The application worker or scheduler owns when this hook runs. The generic
    scanner row-locks stale attempts, fails AUTHORIZED attempts, and moves
    EXECUTING attempts to UNKNOWN for probing; it never replays a write.
    """

    return await recover_abandoned_persisted_executions(
        session_factory,
        minimum_age=minimum_age,
        now=now,
    )
