"""Audit log data model.

Records every action step with before/after screenshots, input sanitization,
approval status, and execution timing for regulatory compliance.
"""

import datetime
import enum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)

from skyvern.forge.sdk.db.models import Base
from skyvern.forge.sdk.db.id import generate_id


class ActionType(str, enum.Enum):
    """Types of browser actions recorded in audit logs."""

    CLICK = "click"
    INPUT_TEXT = "input_text"
    SELECT_OPTION = "select_option"
    UPLOAD_FILE = "upload_file"
    NAVIGATE = "navigate"
    DOWNLOAD = "download"
    SCREENSHOT = "screenshot"
    WAIT = "wait"
    SCROLL = "scroll"
    CUSTOM = "custom"


AUDIT_LOG_PREFIX = "aud"


def generate_audit_log_id() -> str:
    return f"{AUDIT_LOG_PREFIX}_{generate_id()}"


class AuditLogModel(Base):
    """Full-chain audit log entry for a single action step."""

    __tablename__ = "audit_logs"

    audit_log_id = Column(String, primary_key=True, default=generate_audit_log_id)
    task_id = Column(String, nullable=False, index=True)
    organization_id = Column(
        String,
        ForeignKey("organizations.organization_id"),
        nullable=False,
        index=True,
    )
    department_id = Column(
        String,
        ForeignKey("departments.department_id"),
        nullable=False,
    )
    business_line_id = Column(
        String,
        ForeignKey("business_lines.business_line_id"),
        nullable=True,
    )

    # Action details
    action_index = Column(Integer, nullable=False)
    action_type = Column(String, nullable=False)
    target_element = Column(Text, nullable=True)
    input_value = Column(Text, nullable=True)  # sanitized
    input_value_raw_hash = Column(String, nullable=True)  # SHA-256 of original
    page_url = Column(Text, nullable=True)

    # Screenshots (MinIO object keys)
    screenshot_before_key = Column(String, nullable=True)
    screenshot_after_key = Column(String, nullable=True)

    # Execution
    duration_ms = Column(Integer, nullable=True)
    executor = Column(String, nullable=False)  # "agent" or user_id
    execution_result = Column(String, default="success", nullable=False)
    error_message = Column(Text, nullable=True)

    # Approval info
    has_approval = Column(Boolean, default=False, nullable=False)
    approval_id = Column(String, nullable=True)
    approver_user_id = Column(String, nullable=True)

    # Timestamps
    created_at = Column(
        DateTime, default=datetime.datetime.utcnow, nullable=False, index=True
    )

    __table_args__ = (
        Index("idx_aud_task_action", "task_id", "action_index"),
        Index("idx_aud_org_time", "organization_id", "created_at"),
        Index("idx_aud_dept_time", "department_id", "created_at"),
        CheckConstraint(
            "action_index >= 0",
            name="ck_non_negative_action_index",
        ),
    )
