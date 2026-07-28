"""Audit log writer with graceful degradation.

Writes audit log entries to the database with sanitized inputs and
screenshot references. Failures are caught and logged as system warnings
without interrupting the main task execution flow.
"""

import logging
from datetime import datetime

from .models import AuditLogModel, generate_audit_log_id
from .sanitizer import sanitize_input, hash_raw_value

logger = logging.getLogger(__name__)


async def write_audit_log(
    db_session,
    task_id: str,
    org_id: str,
    department_id: str,
    action_index: int,
    action_type: str,
    executor: str,
    business_line_id: str | None = None,
    target_element: str | None = None,
    input_value: str | None = None,
    page_url: str | None = None,
    screenshot_before_key: str | None = None,
    screenshot_after_key: str | None = None,
    duration_ms: int | None = None,
    execution_result: str = "success",
    error_message: str | None = None,
    has_approval: bool = False,
    approval_id: str | None = None,
    approver_user_id: str | None = None,
) -> AuditLogModel | None:
    """Write a single audit log entry with input sanitization.

    Returns the created AuditLogModel on success, None on failure.
    Failures are logged but never raised — main flow is not interrupted.
    """
    try:
        sanitized_value = sanitize_input(input_value)
        raw_hash = hash_raw_value(input_value)

        entry = AuditLogModel(
            audit_log_id=generate_audit_log_id(),
            task_id=task_id,
            organization_id=org_id,
            department_id=department_id,
            business_line_id=business_line_id,
            action_index=action_index,
            action_type=action_type,
            target_element=target_element,
            input_value=sanitized_value,
            input_value_raw_hash=raw_hash,
            page_url=page_url,
            screenshot_before_key=screenshot_before_key,
            screenshot_after_key=screenshot_after_key,
            duration_ms=duration_ms,
            executor=executor,
            execution_result=execution_result,
            error_message=error_message,
            has_approval=has_approval,
            approval_id=approval_id,
            approver_user_id=approver_user_id,
        )

        db_session.add(entry)
        await db_session.commit()
        await db_session.refresh(entry)

        logger.debug(
            "Audit log written: task=%s action=%d type=%s",
            task_id, action_index, action_type,
        )
        return entry

    except Exception as e:
        logger.warning(
            "AUDIT_LOG_FAILURE: Failed to write audit log for task=%s action=%d: %s. "
            "This does NOT affect task execution but requires investigation.",
            task_id, action_index, e,
        )
        try:
            await db_session.rollback()
        except Exception:
            pass
        return None
