"""Explicit synthetic Agent Run composition kept outside platform core."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timedelta
from typing import Any, Literal

from fastapi import FastAPI

from enterprise.agent_runs.routes import mount_agent_run_api
from enterprise.agent_runs.service import AgentRunService
from enterprise.browser_loop.persisted_executor import (
    PersistedExecutionRecoveryReport,
    recover_abandoned_persisted_executions,
)
from enterprise.governance.pack_runtime import PackRuntimeRegistry

from .m6_runtime import SYNTHETIC_RUNTIME_CONTRACT
from .m10_runtime import SyntheticPaymentRuntimeAdapter, TrustedSyntheticM10Driver, build_m10_provider_factory


async def recover_abandoned_agent_run_executions(
    session_factory: Callable[[], AbstractAsyncContextManager[Any]],
    *,
    minimum_age: timedelta,
    now: datetime | None = None,
) -> PersistedExecutionRecoveryReport:
    """Owner-invoked application recovery hook for abandoned browser effects.

    The application worker or scheduler owns when this boundary runs. The
    generic scanner row-locks stale attempts, fails AUTHORIZED attempts, and
    moves EXECUTING attempts to UNKNOWN for probing; it never replays a write.
    """

    return await recover_abandoned_persisted_executions(
        session_factory,
        minimum_age=minimum_age,
        now=now,
    )


def compose_synthetic_agent_run_service(
    *,
    session_factory: Callable[[], AbstractAsyncContextManager[Any]],
    target_url: str,
    hmac_secret: str | None,
    provider_mode: Literal["recorded", "live"] = "recorded",
    provider_endpoint: str | None = None,
    provider_model: str | None = None,
    provider_api_key_env: str = "OPENAI_COMPATIBLE_API_KEY",
    provider_timeout_seconds: float = 30.0,
) -> AgentRunService:
    registry = PackRuntimeRegistry([SYNTHETIC_RUNTIME_CONTRACT])
    adapter = SyntheticPaymentRuntimeAdapter(
        session_factory,
        driver=TrustedSyntheticM10Driver(
            session_factory,
            target_url=target_url,
            hmac_secret=hmac_secret,
        ),
        provider_mode=provider_mode,
        provider_factory=build_m10_provider_factory(
            provider_mode,
            endpoint=provider_endpoint,
            model=provider_model,
            api_key_env=provider_api_key_env,
        ),
    )
    registry.register(adapter)
    return AgentRunService(
        session_factory,
        runtime_registry=registry,
        default_pack_binding=adapter.binding,
        target_url=target_url,
        provider_timeout_seconds=provider_timeout_seconds,
    )


def mount_synthetic_agent_run_api(
    application: FastAPI,
    *,
    session_factory: Callable[[], AbstractAsyncContextManager[Any]],
    target_url: str,
    hmac_secret: str | None,
    provider_mode: Literal["recorded", "live"] = "recorded",
    provider_endpoint: str | None = None,
    provider_model: str | None = None,
    provider_api_key_env: str = "OPENAI_COMPATIBLE_API_KEY",
    provider_timeout_seconds: float = 30.0,
    prefix: str = "/api/v1",
) -> AgentRunService:
    service = compose_synthetic_agent_run_service(
        session_factory=session_factory,
        target_url=target_url,
        hmac_secret=hmac_secret,
        provider_mode=provider_mode,
        provider_endpoint=provider_endpoint,
        provider_model=provider_model,
        provider_api_key_env=provider_api_key_env,
        provider_timeout_seconds=provider_timeout_seconds,
    )
    return mount_agent_run_api(application, service=service, prefix=prefix)
