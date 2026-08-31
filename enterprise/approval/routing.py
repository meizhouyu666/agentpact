"""Approval routing contract supplied by policy and Domain Packs."""

from dataclasses import dataclass, field


@dataclass
class ApprovalRoute:
    """Describes who needs to approve and who should be notified."""

    requires_approval: bool
    approver_department_id: str | None = None
    approver_role: str = "approver"
    notify_department_ids: list[str] = field(default_factory=list)
    notify_roles: list[str] = field(default_factory=list)
    description: str = ""
