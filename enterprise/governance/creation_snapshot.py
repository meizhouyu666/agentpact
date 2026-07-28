"""Trusted creation-time task identity contracts, with no runtime wiring."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field, model_validator


class TaskCreationPath(StrEnum):
    NATIVE = "native_task"
    WORKFLOW = "workflow_task"
    TEMPLATE = "template_task"
    SDK_API = "sdk_api_task"
    DIRECT_INTERNAL = "direct_internal_task"


class TrustedTaskCreationSnapshot(BaseModel):
    """Identity and provenance supplied by a task creator, never page observation."""

    task_id: str
    organization_id: str
    creation_path: TaskCreationPath
    initiator_id: str
    service_principal_id: str | None = None
    department_id: str | None = None
    business_line_id: str | None = None
    authorization_snapshot: dict[str, object] = Field(default_factory=dict)
    policy_version: str
    contract_version: int = Field(ge=1)
    created_at: datetime
    request_id: str | None = None
    workflow_id: str | None = None
    workflow_run_id: str | None = None
    template_id: str | None = None
    template_version: str | None = None
    template_run_id: str | None = None
    caller_id: str | None = None

    @model_validator(mode="after")
    def validate_creation_provenance(self) -> "TrustedTaskCreationSnapshot":
        if self.creation_path is TaskCreationPath.NATIVE and not self.request_id:
            raise ValueError("Native task snapshots require request_id")
        if self.creation_path is TaskCreationPath.WORKFLOW and not (self.workflow_id and self.workflow_run_id):
            raise ValueError("Workflow task snapshots require workflow_id and workflow_run_id")
        if self.creation_path is TaskCreationPath.TEMPLATE and not (
            self.template_id and self.template_version and self.template_run_id
        ):
            raise ValueError("Template task snapshots require template_id, template_version, and template_run_id")
        if self.creation_path is TaskCreationPath.SDK_API and not (self.request_id and self.caller_id):
            raise ValueError("SDK/API task snapshots require request_id and caller_id")
        if self.creation_path is TaskCreationPath.DIRECT_INTERNAL and not (
            self.request_id and self.caller_id and self.service_principal_id
        ):
            raise ValueError("Direct internal task snapshots require request_id, caller_id, and service_principal_id")
        return self
