"""Formal Agent Run application composition.

Concrete Domain Packs are composed outside this module and supplied through a
validated runtime registry. The default formal application deliberately mounts
an empty registry so that the HTTP surface exists without making any Pack
executable by accident.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from fastapi import FastAPI

from enterprise.agent_runs.persistence import AgentRunNativeStore
from enterprise.agent_runs.routes import mount_agent_run_api
from enterprise.agent_runs.service import AgentRunService, CreateGateFactory
from enterprise.governance.pack_runtime import PackRuntimeBinding, PackRuntimeRegistry

_UNCONFIGURED_TARGET_URL = "about:blank"


@dataclass(frozen=True, slots=True)
class AgentRunComposition:
    """Optional Pack-aware inputs for the formal application factory.

    Browser and session dependencies remain encapsulated by the adapters
    already registered in ``runtime_registry``.
    """

    runtime_registry: PackRuntimeRegistry
    target_url: str | None = None
    default_pack_binding: PackRuntimeBinding | None = None
    native_store: AgentRunNativeStore | None = None


def compose_agent_run_service(
    session_factory: Callable[[], AbstractAsyncContextManager[Any]],
    *,
    runtime_registry: PackRuntimeRegistry | None = None,
    target_url: str | None = None,
    default_pack_binding: PackRuntimeBinding | None = None,
    provider_timeout_seconds: float = 30.0,
    create_gate_factory: CreateGateFactory | None = None,
    clock: Callable[[], datetime] | None = None,
    native_store: AgentRunNativeStore | None = None,
) -> AgentRunService:
    """Build the generic service from explicit platform dependencies.

    Omitting ``runtime_registry`` is intentional fail-closed composition: the
    resulting service exposes the API but cannot select or execute a Pack.
    A target URL becomes mandatory as soon as an adapter is registered.
    """

    registry = runtime_registry if runtime_registry is not None else PackRuntimeRegistry(())
    if target_url is None:
        if registry.registered_bindings:
            raise ValueError("A target URL is required when Agent Run adapters are registered")
        target_url = _UNCONFIGURED_TARGET_URL
    elif not target_url.strip():
        raise ValueError("Agent Run target URL must be non-empty")
    if provider_timeout_seconds <= 0:
        raise ValueError("Agent Run provider timeout must be positive")
    if native_store is None:
        from enterprise.integrations.skyvern_agent_run_store import SkyvernAgentRunStore

        native_store = SkyvernAgentRunStore()

    return AgentRunService(
        session_factory,
        runtime_registry=registry,
        native_store=native_store,
        default_pack_binding=default_pack_binding,
        target_url=target_url,
        provider_timeout_seconds=provider_timeout_seconds,
        create_gate_factory=create_gate_factory,
        clock=clock,
    )


def mount_agent_run_application(
    application: FastAPI,
    *,
    session_factory: Callable[[], AbstractAsyncContextManager[Any]],
    runtime_registry: PackRuntimeRegistry | None = None,
    target_url: str | None = None,
    default_pack_binding: PackRuntimeBinding | None = None,
    provider_timeout_seconds: float = 30.0,
    create_gate_factory: CreateGateFactory | None = None,
    clock: Callable[[], datetime] | None = None,
    native_store: AgentRunNativeStore | None = None,
    prefix: str = "/api/v1",
) -> AgentRunService:
    """Compose and mount one app-scoped Agent Run service."""

    service = compose_agent_run_service(
        session_factory,
        runtime_registry=runtime_registry,
        target_url=target_url,
        default_pack_binding=default_pack_binding,
        provider_timeout_seconds=provider_timeout_seconds,
        create_gate_factory=create_gate_factory,
        clock=clock,
        native_store=native_store,
    )
    return mount_agent_run_api(application, service=service, prefix=prefix)
