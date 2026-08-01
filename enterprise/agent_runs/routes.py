"""Authenticated redacted HTTP surface for governed Agent Runs."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any, Literal

from fastapi import APIRouter, FastAPI, HTTPException, Query, Response, status

from enterprise.auth.dependencies import CurrentUser

from .service import (
    AgentRunCommandRequest,
    AgentRunCreateRequest,
    AgentRunDecisionTrace,
    AgentRunError,
    AgentRunPage,
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
    provider_endpoint: str | None = None,
    provider_model: str | None = None,
    provider_api_key_env: str = "OPENAI_COMPATIBLE_API_KEY",
    provider_timeout_seconds: float = 30.0,
    prefix: str = "/api/v1",
) -> AgentRunService:
    """Install the exact trusted M10 composition used by the application boot."""

    from enterprise.domains.synthetic_payment.m6_runtime import SYNTHETIC_RUNTIME_CONTRACT
    from enterprise.domains.synthetic_payment.m10_runtime import (
        SyntheticPaymentRuntimeAdapter,
        TrustedSyntheticM10Driver,
        build_m10_provider_factory,
    )
    from enterprise.governance.pack_runtime import PackRuntimeRegistry

    registry = PackRuntimeRegistry([SYNTHETIC_RUNTIME_CONTRACT])
    provider_factory = build_m10_provider_factory(
        provider_mode,
        endpoint=provider_endpoint,
        model=provider_model,
        api_key_env=provider_api_key_env,
    )
    registry.register(
        SyntheticPaymentRuntimeAdapter(
            session_factory,
            driver=TrustedSyntheticM10Driver(
                session_factory,
                target_url=target_url,
                hmac_secret=hmac_secret,
            ),
            provider_mode=provider_mode,
            provider_factory=provider_factory,
        )
    )
    service = AgentRunService(
        session_factory,
        runtime_registry=registry,
        target_url=target_url,
        provider_timeout_seconds=provider_timeout_seconds,
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


@router.get("/", response_model=AgentRunPage)
async def list_agent_runs(
    user: CurrentUser,
    cursor: str | None = None,
    limit: int = Query(default=20, ge=1, le=50),
) -> AgentRunPage:
    try:
        return await _configured_service().list_runs(user=user, cursor=cursor, limit=limit)
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


@router.get("/{run_id}/decision-trace", response_model=AgentRunDecisionTrace)
async def get_agent_run_decision_trace(run_id: str, user: CurrentUser) -> AgentRunDecisionTrace:
    try:
        return await _configured_service().decision_trace(run_id, user=user)
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
