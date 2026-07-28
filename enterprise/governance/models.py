"""Database models for Phase 2.0 governance state.

These tables are additive.  They record contracts and audit-only observations now,
while PendingAction and ExecutionAttempt become active in later gated phases.
"""

from __future__ import annotations

import datetime

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)

from skyvern.forge.sdk.db.id import generate_id
from skyvern.forge.sdk.db.models import Base


def _governance_id(prefix: str) -> str:
    return f"{prefix}_{generate_id()}"


class TaskContractModel(Base):
    __tablename__ = "task_contracts"

    contract_id = Column(String, primary_key=True, default=lambda: _governance_id("tc"))
    task_id = Column(String, ForeignKey("tasks.task_id"), nullable=False, unique=True, index=True)
    organization_id = Column(String, ForeignKey("organizations.organization_id"), nullable=False, index=True)
    initiator_id = Column(String, nullable=True)
    service_principal_id = Column(String, nullable=True)
    department_id = Column(String, nullable=True)
    business_line_id = Column(String, nullable=True)
    goal = Column(Text, nullable=False)
    allowed_operations = Column(JSON, nullable=False, default=list)
    data_scope = Column(JSON, nullable=False, default=dict)
    authorization_snapshot = Column(JSON, nullable=False, default=dict)
    policy_profile = Column(String, nullable=False, default="financial-default")
    policy_version = Column(String, nullable=False, default="phase2-v1")
    success_criteria = Column(JSON, nullable=False, default=list)
    mode = Column(String, nullable=False, default="audit")
    version = Column(Integer, nullable=False, default=1)
    expires_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.datetime.utcnow)
    modified_at = Column(DateTime, nullable=False, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    __table_args__ = (
        CheckConstraint("mode IN ('off', 'audit', 'enforce')", name="ck_task_contract_mode"),
        CheckConstraint("version > 0", name="ck_task_contract_version"),
        Index("idx_tc_org_mode", "organization_id", "mode"),
    )


class GovernanceAuditEventModel(Base):
    __tablename__ = "governance_audit_events"

    event_id = Column(String, primary_key=True, default=lambda: _governance_id("gae"))
    task_id = Column(String, ForeignKey("tasks.task_id"), nullable=False, index=True)
    step_id = Column(String, ForeignKey("steps.step_id"), nullable=True, index=True)
    contract_id = Column(String, ForeignKey("task_contracts.contract_id"), nullable=True, index=True)
    organization_id = Column(String, ForeignKey("organizations.organization_id"), nullable=False, index=True)
    event_type = Column(String, nullable=False)
    mode = Column(String, nullable=False)
    action_fingerprint = Column(String, nullable=True, index=True)
    observation_hash = Column(String, nullable=True)
    policy_version = Column(String, nullable=True)
    payload = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime, nullable=False, default=datetime.datetime.utcnow, index=True)

    __table_args__ = (
        CheckConstraint("mode IN ('off', 'audit', 'enforce')", name="ck_governance_event_mode"),
        Index("idx_gae_task_time", "task_id", "created_at"),
    )


class GovernedTaskAdmissionModel(Base):
    """Non-runnable aggregate persisted before any future Task publication."""

    __tablename__ = "governed_task_admissions"

    admission_id = Column(String, primary_key=True)
    organization_id = Column(String, nullable=False, index=True)
    request_id = Column(String, nullable=False)
    task_id = Column(String, nullable=False)
    contract_id = Column(String, nullable=False)
    bundle_schema_version = Column(String, nullable=False)
    admission_fingerprint = Column(String, nullable=False)
    bundle_fingerprint = Column(String, nullable=False)
    bundle_payload = Column(JSON, nullable=False)
    mode = Column(String, nullable=False, default="audit")
    committed_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("mode = 'audit'", name="ck_governed_task_admission_audit_only"),
        UniqueConstraint("organization_id", "request_id", name="uq_gta_org_request"),
        UniqueConstraint("organization_id", "task_id", name="uq_gta_org_task"),
        Index("idx_gta_org_committed", "organization_id", "committed_at"),
    )


class GovernanceOutboxModel(Base):
    """Pending redacted event written atomically with an admission aggregate."""

    __tablename__ = "governance_outbox"

    outbox_id = Column(String, primary_key=True)
    admission_id = Column(
        String,
        ForeignKey("governed_task_admissions.admission_id", ondelete="CASCADE"),
        nullable=False,
    )
    organization_id = Column(String, nullable=False, index=True)
    event_type = Column(String, nullable=False)
    payload = Column(JSON, nullable=False)
    status = Column(String, nullable=False, default="pending")
    attempt_count = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), nullable=False)
    published_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint("status IN ('pending', 'published')", name="ck_governance_outbox_status"),
        CheckConstraint("attempt_count >= 0", name="ck_governance_outbox_attempt_count"),
        UniqueConstraint("admission_id", "event_type", name="uq_governance_outbox_admission_event"),
        Index("idx_governance_outbox_pending", "status", "created_at"),
    )


class ExecutionPermitModel(Base):
    __tablename__ = "execution_permits"

    permit_id = Column(String, primary_key=True, default=lambda: _governance_id("permit"))
    task_id = Column(String, ForeignKey("tasks.task_id"), nullable=False, index=True)
    step_id = Column(String, ForeignKey("steps.step_id"), nullable=False, index=True)
    contract_id = Column(String, ForeignKey("task_contracts.contract_id"), nullable=False, index=True)
    action_fingerprint = Column(String, nullable=False)
    observation_hash = Column(String, nullable=False)
    policy_decision_id = Column(String, nullable=False)
    decision_payload = Column(JSON, nullable=False)
    status = Column(String, nullable=False, default="issued")
    issued_at = Column(DateTime, nullable=False, default=datetime.datetime.utcnow)
    expires_at = Column(DateTime, nullable=False)
    used_at = Column(DateTime, nullable=True)

    __table_args__ = (
        CheckConstraint("status IN ('issued', 'consumed', 'revoked', 'expired')", name="ck_execution_permit_status"),
        Index("idx_permit_task_status", "task_id", "status"),
    )


class PendingActionModel(Base):
    __tablename__ = "pending_actions"

    pending_action_id = Column(String, primary_key=True, default=lambda: _governance_id("pa"))
    task_id = Column(String, ForeignKey("tasks.task_id"), nullable=False, index=True)
    step_id = Column(String, ForeignKey("steps.step_id"), nullable=False, index=True)
    contract_id = Column(String, ForeignKey("task_contracts.contract_id"), nullable=False)
    organization_id = Column(String, ForeignKey("organizations.organization_id"), nullable=False, index=True)
    action_fingerprint = Column(String, nullable=False)
    observation_hash = Column(String, nullable=False)
    action_payload = Column(JSON, nullable=False)
    intent_payload = Column(JSON, nullable=False)
    decision_payload = Column(JSON, nullable=False)
    approval_id = Column(String, nullable=True, unique=True)
    status = Column(String, nullable=False, default="pending")
    row_version = Column(Integer, nullable=False, default=1)
    expires_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.datetime.utcnow)
    modified_at = Column(DateTime, nullable=False, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'expired', 'invalidated')",
            name="ck_pending_action_status",
        ),
        # Terminal records are approval history.  Only an unresolved approval
        # may be unique for a task step; approved remains active until fresh
        # observation invalidates it.
        Index(
            "uq_pending_action_active_step",
            "task_id",
            "step_id",
            unique=True,
            postgresql_where=text("status IN ('pending', 'approved')"),
        ),
        Index("idx_pa_org_status", "organization_id", "status"),
    )


class ExecutionAttemptModel(Base):
    __tablename__ = "execution_attempts"

    attempt_id = Column(String, primary_key=True, default=lambda: _governance_id("ea"))
    task_id = Column(String, ForeignKey("tasks.task_id"), nullable=False, index=True)
    step_id = Column(String, ForeignKey("steps.step_id"), nullable=False, index=True)
    contract_id = Column(String, ForeignKey("task_contracts.contract_id"), nullable=False)
    pending_action_id = Column(String, ForeignKey("pending_actions.pending_action_id"), nullable=True)
    action_fingerprint = Column(String, nullable=False)
    observation_hash = Column(String, nullable=False)
    status = Column(String, nullable=False, default="authorized")
    idempotency_key = Column(String, nullable=True)
    result_probe = Column(JSON, nullable=True)
    error_message = Column(Text, nullable=True)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.datetime.utcnow)
    modified_at = Column(DateTime, nullable=False, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    __table_args__ = (
        CheckConstraint(
            "status IN ('authorized', 'executing', 'confirmed', 'unknown', 'failed')",
            name="ck_execution_attempt_status",
        ),
        UniqueConstraint("task_id", "idempotency_key", name="uq_execution_attempt_idempotency"),
        Index("idx_ea_task_status", "task_id", "status"),
    )
