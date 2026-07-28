"""Versioned serialization and invalidation contracts for future governance use."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel


class GovernanceArtifactKind(StrEnum):
    CONTRACT = "task_contract"
    GRANT = "capability_grant"
    CAPABILITY_REQUEST = "capability_request"
    GRANT_PROJECTION = "grant_projection"
    BUSINESS_PLAN = "business_plan"
    WORK_ORDER = "execution_work_order"
    OBSERVATION = "observation"
    RECOVERY_DECISION = "recovery_decision"


class VersionedGovernanceArtifact(BaseModel):
    """A deterministic envelope that carries no execution authority."""

    artifact_kind: GovernanceArtifactKind
    schema_version: str
    invalidation_keys: dict[str, str]
    payload: dict[str, Any]


_SCHEMA_VERSIONS = {kind: f"phase2-{kind.value}-v1" for kind in GovernanceArtifactKind}


def serialize_governance_artifact(*, kind: GovernanceArtifactKind, value: BaseModel) -> VersionedGovernanceArtifact:
    """Serialize a known interface with its stable invalidation inputs."""

    payload = value.model_dump(mode="json")
    return VersionedGovernanceArtifact(
        artifact_kind=kind,
        schema_version=_SCHEMA_VERSIONS[kind],
        invalidation_keys=_invalidation_keys(kind=kind, payload=payload),
        payload=payload,
    )


def requires_invalidation(
    previous: VersionedGovernanceArtifact,
    current: VersionedGovernanceArtifact,
) -> bool:
    """Return whether a future owner must discard a prior serialized artifact."""

    return (
        previous.artifact_kind != current.artifact_kind
        or previous.schema_version != current.schema_version
        or previous.invalidation_keys != current.invalidation_keys
    )


def _invalidation_keys(*, kind: GovernanceArtifactKind, payload: dict[str, Any]) -> dict[str, str]:
    fields_by_kind = {
        GovernanceArtifactKind.CONTRACT: ("contract_id", "task_id", "version", "policy_version"),
        GovernanceArtifactKind.GRANT: (
            "grant_id",
            "capability_id",
            "capability_version",
            "business_principal_id",
            "workload_principal_id",
            "tenant_id",
            "policy_snapshot_version",
            "revocation_epoch",
            "purpose",
            "not_before",
            "expires_at",
        ),
        GovernanceArtifactKind.CAPABILITY_REQUEST: (
            "request_id",
            "principal_ref",
            "tenant_id",
            "capability_ref",
            "capability_version",
            "grant_ref",
            "request_kind",
        ),
        GovernanceArtifactKind.GRANT_PROJECTION: (
            "principal_id",
            "workload_principal_id",
            "tenant_id",
            "revocation_epoch",
            "generated_at",
        ),
        GovernanceArtifactKind.BUSINESS_PLAN: ("plan_id", "task_id", "contract_id", "version"),
        GovernanceArtifactKind.WORK_ORDER: (
            "work_order_id",
            "task_id",
            "business_plan_step_id",
            "contract_id",
            "grant_id",
        ),
        GovernanceArtifactKind.OBSERVATION: ("observation_id", "task_id", "step_id", "snapshot_hash"),
        GovernanceArtifactKind.RECOVERY_DECISION: (
            "failure_class",
            "level",
            "action",
            "max_attempts",
            "requires_reauthorization",
            "requires_result_probe",
        ),
    }
    return {field: str(payload[field]) for field in fields_by_kind[kind] if field in payload}
