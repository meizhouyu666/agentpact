"""Authenticated redacted HTTP surface for governed Agent Runs."""

from __future__ import annotations

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request, Response, status

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
_SERVICE_STATE_KEY = "agentpact_agent_run_service"


def mount_agent_run_api(
    application: FastAPI,
    *,
    service: AgentRunService,
    prefix: str = "/api/v1",
) -> AgentRunService:
    """Mount an already composed generic Agent Run service."""

    if getattr(application.state, _SERVICE_STATE_KEY, None) is not None:
        raise ValueError("Agent Run API is already mounted on this application")
    setattr(application.state, _SERVICE_STATE_KEY, service)
    application.include_router(router, prefix=prefix)
    return service


def _configured_service(request: Request) -> AgentRunService:
    service = getattr(request.app.state, _SERVICE_STATE_KEY, None)
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "AGENT_RUN_SERVICE_UNAVAILABLE"},
        )
    return service


def _raise_http(exc: AgentRunError) -> None:
    raise HTTPException(status_code=exc.status_code, detail={"code": exc.code}) from exc


@router.post("/", response_model=AgentRunProjection)
async def create_agent_run(request: Request, body: AgentRunCreateRequest, user: CurrentUser) -> AgentRunProjection:
    try:
        return await _configured_service(request).create(body, user=user)
    except AgentRunError as exc:
        _raise_http(exc)


@router.get("/", response_model=AgentRunPage)
async def list_agent_runs(
    request: Request,
    user: CurrentUser,
    cursor: str | None = None,
    limit: int = Query(default=20, ge=1, le=50),
) -> AgentRunPage:
    try:
        return await _configured_service(request).list_runs(user=user, cursor=cursor, limit=limit)
    except AgentRunError as exc:
        _raise_http(exc)


@router.get("/{run_id}", response_model=AgentRunProjection)
async def get_agent_run(request: Request, run_id: str, user: CurrentUser) -> AgentRunProjection:
    try:
        return await _configured_service(request).get(run_id, user=user)
    except AgentRunError as exc:
        _raise_http(exc)


@router.get("/{run_id}/events", response_model=tuple[AgentRunTimelineEvent, ...])
async def get_agent_run_events(request: Request, run_id: str, user: CurrentUser) -> tuple[AgentRunTimelineEvent, ...]:
    try:
        return await _configured_service(request).events(run_id, user=user)
    except AgentRunError as exc:
        _raise_http(exc)


@router.get("/{run_id}/decision-trace", response_model=AgentRunDecisionTrace)
async def get_agent_run_decision_trace(request: Request, run_id: str, user: CurrentUser) -> AgentRunDecisionTrace:
    try:
        return await _configured_service(request).decision_trace(run_id, user=user)
    except AgentRunError as exc:
        _raise_http(exc)


@router.post("/{run_id}/approve", response_model=AgentRunProjection)
async def approve_agent_run(
    request: Request,
    run_id: str,
    body: AgentRunCommandRequest,
    user: CurrentUser,
) -> AgentRunProjection:
    try:
        return await _configured_service(request).approve(run_id, body, user=user)
    except AgentRunError as exc:
        _raise_http(exc)


@router.post("/{run_id}/reject", response_model=AgentRunProjection)
async def reject_agent_run(
    request: Request,
    run_id: str,
    body: AgentRunCommandRequest,
    user: CurrentUser,
) -> AgentRunProjection:
    try:
        return await _configured_service(request).reject(run_id, body, user=user)
    except AgentRunError as exc:
        _raise_http(exc)


@router.post("/{run_id}/probe", response_model=AgentRunProjection)
async def probe_agent_run(
    request: Request,
    run_id: str,
    body: AgentRunCommandRequest,
    user: CurrentUser,
) -> AgentRunProjection:
    try:
        return await _configured_service(request).probe(run_id, body, user=user)
    except AgentRunError as exc:
        _raise_http(exc)


@router.post("/{run_id}/cancel", response_model=AgentRunProjection)
async def cancel_agent_run(
    request: Request,
    run_id: str,
    body: AgentRunCommandRequest,
    user: CurrentUser,
) -> AgentRunProjection:
    try:
        return await _configured_service(request).cancel(run_id, body, user=user)
    except AgentRunError as exc:
        _raise_http(exc)


@router.get("/{run_id}/report", response_model=AgentRunReport)
async def download_agent_run_report(
    request: Request,
    run_id: str,
    user: CurrentUser,
    response: Response,
) -> AgentRunReport:
    try:
        report = await _configured_service(request).report(run_id, user=user)
    except AgentRunError as exc:
        _raise_http(exc)
    response.headers["Content-Disposition"] = f'attachment; filename="agent-run-{run_id}.json"'
    return report
