"""Observation evidence and field-control contracts without collection side effects."""

from enum import StrEnum

from pydantic import BaseModel, Field

from .classification import DataClassification, ModelEgressPolicy
from .contracts import ExecutionEffect


class ObservationMode(StrEnum):
    DOM_ONLY = "dom_only"
    VISION_ONLY = "vision_only"
    HYBRID = "hybrid"


class EvidenceConsistency(StrEnum):
    CONSISTENT = "consistent"
    CONFLICTING = "conflicting"
    INSUFFICIENT = "insufficient"


class ArtifactKind(StrEnum):
    DOM = "dom"
    SCREENSHOT = "screenshot"
    PROMPT = "prompt"
    AUDIT = "audit"


class FieldControl(BaseModel):
    field_name: str
    classification: DataClassification
    redact_before_egress: bool = True
    retention_days: int = Field(ge=0)
    access_roles: set[str] = Field(default_factory=set)


class ObservationArtifact(BaseModel):
    artifact_id: str
    kind: ArtifactKind
    fields: list[FieldControl] = Field(default_factory=list)
    model_policy: ModelEgressPolicy | None = None
    redacted_field_names: set[str] = Field(default_factory=set)

    def permits_egress(self) -> bool:
        if self.model_policy is None:
            return False
        return all(
            self.model_policy.allows(control.classification)
            or (control.redact_before_egress and control.field_name in self.redacted_field_names)
            for control in self.fields
        )


class ObservationEvidenceBundle(BaseModel):
    observation_id: str
    mode: ObservationMode
    consistency: EvidenceConsistency
    artifacts: list[ObservationArtifact] = Field(default_factory=list)


class EvidenceDecision(BaseModel):
    allow_automatic_progress: bool
    requires_human: bool
    reason: str


def assess_evidence(*, effect: ExecutionEffect, bundle: ObservationEvidenceBundle) -> EvidenceDecision:
    """Fail safe for state changes when evidence is conflicting or insufficient."""

    if effect in {ExecutionEffect.INTERNAL_WRITE, ExecutionEffect.EXTERNAL_WRITE} and bundle.consistency is not EvidenceConsistency.CONSISTENT:
        return EvidenceDecision(
            allow_automatic_progress=False,
            requires_human=True,
            reason="State-changing action requires consistent observation evidence",
        )
    if any(not artifact.permits_egress() for artifact in bundle.artifacts):
        return EvidenceDecision(
            allow_automatic_progress=False,
            requires_human=True,
            reason="Observation artifact violates its model-egress policy",
        )
    return EvidenceDecision(
        allow_automatic_progress=True,
        requires_human=False,
        reason="Observation evidence satisfies the declared policy",
    )
