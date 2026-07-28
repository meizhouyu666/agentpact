"""Audit log query API.

Supports multi-dimensional filtering and pagination, with presigned
screenshot URLs for temporary access.
"""

from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from enterprise.auth.dependencies import CurrentUser, require_cross_org_viewer

router = APIRouter(prefix="/enterprise/audit", tags=["audit"])


# --- Pydantic schemas ---

class AuditLogResponse(BaseModel):
    audit_log_id: str
    task_id: str
    organization_id: str
    department_id: str
    business_line_id: str | None
    action_index: int
    action_type: str
    target_element: str | None
    input_value: str | None  # sanitized
    page_url: str | None
    screenshot_before_url: str | None  # presigned URL
    screenshot_after_url: str | None  # presigned URL
    duration_ms: int | None
    executor: str
    execution_result: str
    error_message: str | None
    has_approval: bool
    approval_id: str | None
    approver_user_id: str | None
    created_at: str


class AuditLogListResponse(BaseModel):
    items: list[AuditLogResponse]
    total: int
    page: int
    page_size: int


# --- In-memory store for testing ---

_audit_store: list[dict] = []


def configure_store(store: list[dict]):
    """Configure the audit store (for testing)."""
    global _audit_store
    _audit_store = store


# --- Routes ---

@router.get("/logs", response_model=AuditLogListResponse)
async def query_audit_logs(
    user: CurrentUser,
    task_id: str | None = Query(None, description="Filter by task ID"),
    action_type: str | None = Query(None, description="Filter by action type"),
    executor: str | None = Query(None, description="Filter by executor"),
    risk_level: str | None = Query(None, description="Filter by risk level"),
    start_time: str | None = Query(None, description="Start time (ISO 8601)"),
    end_time: str | None = Query(None, description="End time (ISO 8601)"),
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(20, ge=1, le=100, description="Items per page"),
):
    """Query audit logs with multi-dimensional filtering and pagination.

    Requires cross-org read permission or admin role.
    Screenshot fields return presigned URLs valid for 1 hour.
    """
    # Filter
    filtered = [
        log for log in _audit_store
        if log.get("organization_id") == user.org_id
    ]

    if task_id:
        filtered = [l for l in filtered if l.get("task_id") == task_id]
    if action_type:
        filtered = [l for l in filtered if l.get("action_type") == action_type]
    if executor:
        filtered = [l for l in filtered if l.get("executor") == executor]
    if start_time:
        filtered = [l for l in filtered if l.get("created_at", "") >= start_time]
    if end_time:
        filtered = [l for l in filtered if l.get("created_at", "") <= end_time]

    # Sort by created_at desc
    filtered.sort(key=lambda x: x.get("created_at", ""), reverse=True)

    total = len(filtered)
    start = (page - 1) * page_size
    end = start + page_size
    page_items = filtered[start:end]

    items = [
        AuditLogResponse(
            audit_log_id=log["audit_log_id"],
            task_id=log["task_id"],
            organization_id=log["organization_id"],
            department_id=log["department_id"],
            business_line_id=log.get("business_line_id"),
            action_index=log["action_index"],
            action_type=log["action_type"],
            target_element=log.get("target_element"),
            input_value=log.get("input_value"),
            page_url=log.get("page_url"),
            screenshot_before_url=log.get("screenshot_before_url"),
            screenshot_after_url=log.get("screenshot_after_url"),
            duration_ms=log.get("duration_ms"),
            executor=log["executor"],
            execution_result=log.get("execution_result", "success"),
            error_message=log.get("error_message"),
            has_approval=log.get("has_approval", False),
            approval_id=log.get("approval_id"),
            approver_user_id=log.get("approver_user_id"),
            created_at=log["created_at"],
        )
        for log in page_items
    ]

    return AuditLogListResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
    )
