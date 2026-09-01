"""Test-only composition for the synthetic.payment Agent Run evidence path."""

from __future__ import annotations

import os
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any, Literal

from fastapi import FastAPI

from enterprise.agent.constrained_planner import OpenAICompatiblePlanner, PlannerTransport
from enterprise.agent_runs.routes import mount_agent_run_api
from enterprise.agent_runs.service import AgentRunService
from enterprise.domains.synthetic_payment.m6_runtime import SYNTHETIC_RUNTIME_CONTRACT
from enterprise.domains.synthetic_payment.m9_runtime import OpenAICompatibleM9Provider
from enterprise.domains.synthetic_payment.m10_runtime import (
    M9ProviderFactory,
    SyntheticPaymentRuntimeAdapter,
    TrustedSyntheticM10Driver,
    recorded_m10_provider,
)
from enterprise.governance.pack_runtime import PackRuntimeRegistry


def build_m10_provider_factory(
    provider_mode: Literal["recorded", "live"],
    *,
    endpoint: str | None = None,
    model: str | None = None,
    api_key_env: str = "OPENAI_COMPATIBLE_API_KEY",
    transport: PlannerTransport | None = None,
) -> M9ProviderFactory:
    """Build the configured provider edge; live mode never falls back."""

    if provider_mode == "recorded":
        return recorded_m10_provider
    if provider_mode != "live":
        raise ValueError("Agent Run provider mode must be recorded or live")
    if not endpoint or not model or not api_key_env or not os.environ.get(api_key_env):
        raise ValueError("Live Agent Run provider configuration is incomplete")
    planner = OpenAICompatiblePlanner(
        endpoint=endpoint,
        model=model,
        api_key_env=api_key_env,
        transport=transport,
    )
    return lambda _planner_input: OpenAICompatibleM9Provider(planner)


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
