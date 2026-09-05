"""Regression coverage for the task-contract timestamp persistence boundary."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.dialects import postgresql

from enterprise.approval.models import ApprovalRequestModel
from enterprise.governance.models import (
    ExecutionAttemptModel,
    ExecutionPermitModel,
    GovernanceAuditEventModel,
    PendingActionModel,
    TaskContractModel,
)


def _bind(model: type, column_name: str, value: datetime | None) -> datetime | None:
    processor = model.__table__.c[column_name].type.bind_processor(postgresql.dialect())
    assert processor is not None
    return processor(value)


@pytest.mark.parametrize(
    "model,column_name",
    [
        (TaskContractModel, "expires_at"),
        (ExecutionPermitModel, "issued_at"),
        (ExecutionPermitModel, "expires_at"),
        (ExecutionPermitModel, "used_at"),
        (PendingActionModel, "expires_at"),
        (GovernanceAuditEventModel, "created_at"),
        (ExecutionAttemptModel, "started_at"),
        (ExecutionAttemptModel, "completed_at"),
        (ApprovalRequestModel, "requested_at"),
        (ApprovalRequestModel, "decided_at"),
    ],
)
def test_governance_naive_timestamps_normalize_aware_values_at_bind_boundary(model, column_name: str) -> None:
    aware = datetime(2026, 9, 6, 12, 30, 45, 123456, tzinfo=timezone(timedelta(hours=8)))

    bound = _bind(model, column_name, aware)

    assert bound == datetime(2026, 9, 6, 4, 30, 45, 123456)
    assert bound.tzinfo is None


def test_task_contract_expiry_preserves_naive_and_null_values() -> None:
    naive = datetime(2026, 9, 6, 4, 30, 45, 123456)

    assert _bind(TaskContractModel, "expires_at", naive) is naive
    assert _bind(TaskContractModel, "expires_at", None) is None
