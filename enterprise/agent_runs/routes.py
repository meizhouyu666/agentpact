"""Authenticated redacted HTTP surface for governed Agent Runs."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any, Literal

from fastapi import APIRouter, FastAPI, HTTPException, Response, status

from enterprise.auth.dependencies import CurrentUser

from .service import (
    AgentRunCommandRequest,
    AgentRunCreateRequest,
    AgentRunError,
    AgentRunProjection,
    AgentRunReport,
    AgentRunService,
    AgentRunTimelineEvent,
)

router = APIRouter(prefix="/enterprise/agent-runs", tags=["enterprise-agent-runs"])
_service: AgentRunService | None = None


def configure_agent_run_service(service: AgentRunService) -> None:
    """Application-composition hook; configured services never hold run state."""

    global _service
    _service = service


def mount_agent_run_api(
    application: FastAPI,
    *,
    session_factory: Callable[[], AbstractAsyncContextManager[Any]],
    target_url: str,
    hmac_secret: str | None,
    provider_mode: Literal["recorded", "live"] = "recorded",
    prefix: str = "/api/v1",
) -> AgentRunService:
    """Install the exact trusted M10 composition used by the application boot."""

    from enterprise.domains.synthetic_payment.m10_runtime import (
        SyntheticPaymentRuntimeAdapter,
        TrustedSyntheticM10Driver,
    )
    from enterprise.domains.synthetic_payment.sdk_manifest import build_pack_sdk_manifest
    from enterprise.governance.pack_runtime import PackRuntimeRegistry

    registry = PackRuntimeRegistry([build_pack_sdk_manifest()])
    registry.register(
        SyntheticPaymentRuntimeAdapter(
            session_factory,
            driver=TrustedSyntheticM10Driver(
                session_factory,
                target_url=target_url,
                hmac_secret=hmac_secret,
            ),
        )
    )
    service = AgentRunService(
        session_factory,
        runtime_registry=registry,
        target_url=target_url,
        provider_mode=provider_mode,
    )
    configure_agent_run_service(service)
    application.include_router(router, prefix=prefix)
    return service


def reset_agent_run_service() -> None:
    """Test-only composition reset."""

    global _service
    _service = None


def _configured_service() -> AgentRunService:
    if _service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "AGENT_RUN_SERVICE_UNAVAILABLE"},
        )
    return _service


def _raise_http(exc: AgentRunError) -> None:
    raise HTTPException(status_code=exc.status_code, detail={"code": exc.code}) from exc


@router.post("/", response_model=AgentRunProjection)
async def create_agent_run(body: AgentRunCreateRequest, user: CurrentUser) -> AgentRunProjection:
    try:
        return await _configured_service().create(body, user=user)
    except AgentRunError as exc:
        _raise_http(exc)


@router.get("/{run_id}", response_model=AgentRunProjection)
async def get_agent_run(run_id: str, user: CurrentUser) -> AgentRunProjection:
    try:
        return await _configured_service().get(run_id, user=user)
    except AgentRunError as exc:
        _raise_http(exc)


@router.get("/{run_id}/events", response_model=tuple[AgentRunTimelineEvent, ...])
async def get_agent_run_events(run_id: str, user: CurrentUser) -> tuple[AgentRunTimelineEvent, ...]:
    try:
        return await _configured_service().events(run_id, user=user)
    except AgentRunError as exc:
        _raise_http(exc)


@router.post("/{run_id}/approve", response_model=AgentRunProjection)
async def approve_agent_run(
    run_id: str,
    body: AgentRunCommandRequest,
    user: CurrentUser,
) -> AgentRunProjection:
    try:
        return await _configured_service().approve(run_id, body, user=user)
    except AgentRunError as exc:
        _raise_http(exc)


@router.post("/{run_id}/reject", response_model=AgentRunProjection)
async def reject_agent_run(
    run_id: str,
    body: AgentRunCommandRequest,
    user: CurrentUser,
) -> AgentRunProjection:
    try:
        return await _configured_service().reject(run_id, body, user=user)
    except AgentRunError as exc:
        _raise_http(exc)


@router.post("/{run_id}/probe", response_model=AgentRunProjection)
async def probe_agent_run(
    run_id: str,
    body: AgentRunCommandRequest,
    user: CurrentUser,
) -> AgentRunProjection:
    try:
        return await _configured_service().probe(run_id, body, user=user)
    except AgentRunError as exc:
        _raise_http(exc)


@router.post("/{run_id}/cancel", response_model=AgentRunProjection)
async def cancel_agent_run(
    run_id: str,
    body: AgentRunCommandRequest,
    user: CurrentUser,
) -> AgentRunProjection:
    try:
        return await _configured_service().cancel(run_id, body, user=user)
    except AgentRunError as exc:
        _raise_http(exc)


@router.get("/{run_id}/report", response_model=AgentRunReport)
async def download_agent_run_report(run_id: str, user: CurrentUser, response: Response) -> AgentRunReport:
    try:
        report = await _configured_service().report(run_id, user=user)
    except AgentRunError as exc:
        _raise_http(exc)
    response.headers["Content-Disposition"] = f'attachment; filename="agent-run-{run_id}.json"'
    return report
