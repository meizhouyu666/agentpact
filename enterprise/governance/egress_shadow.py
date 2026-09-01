"""Local-only evidence scan for future model-egress policy evaluation."""

from __future__ import annotations

import re
from enum import StrEnum
from html.parser import HTMLParser

from pydantic import BaseModel, Field

from .classification import DataClassification, ModelEgressPolicy, classify_value


class ShadowArtifactKind(StrEnum):
    DOM = "dom"
    PROMPT = "prompt"
    SCREENSHOT = "screenshot"


class EgressShadowFinding(BaseModel):
    artifact_kind: ShadowArtifactKind
    field_name: str
    classification: DataClassification
    redacted_value: str


class EgressShadowReport(BaseModel):
    findings: list[EgressShadowFinding] = Field(default_factory=list)

    @property
    def finding_count(self) -> int:
        return len(self.findings)


def conservative_shadow_policy() -> ModelEgressPolicy:
    """Use a local, non-configuring baseline for future egress evaluation."""

    return ModelEgressPolicy(model_id="shadow-only", region="local")


def scan_egress_shadow(
    *,
    policy: ModelEgressPolicy,
    dom: str | None = None,
    prompt: str | None = None,
    screenshots: list[bytes] | None = None,
) -> EgressShadowReport:
    """Report only redacted fields that a future model policy would reject."""

    findings: list[EgressShadowFinding] = []
    if dom is not None:
        _append_if_disallowed(
            findings=findings,
            policy=policy,
            artifact_kind=ShadowArtifactKind.DOM,
            field_name="dom",
            value=dom,
        )
        for field_name, value in _dom_fields(dom):
            _append_if_disallowed(
                findings=findings,
                policy=policy,
                artifact_kind=ShadowArtifactKind.DOM,
                field_name=field_name,
                value=value,
                classification_field_name=f"{field_name}:{value}",
            )
    if prompt is not None:
        _append_if_disallowed(
            findings=findings,
            policy=policy,
            artifact_kind=ShadowArtifactKind.PROMPT,
            field_name="prompt",
            value=prompt,
        )

    if screenshots and not policy.allows(DataClassification.RESTRICTED):
        findings.append(
            EgressShadowFinding(
                artifact_kind=ShadowArtifactKind.SCREENSHOT,
                field_name="screenshots",
                classification=DataClassification.RESTRICTED,
                redacted_value="[REDACTED_BINARY]",
            )
        )
    return EgressShadowReport(findings=findings)


def _append_if_disallowed(
    *,
    findings: list[EgressShadowFinding],
    policy: ModelEgressPolicy,
    artifact_kind: ShadowArtifactKind,
    field_name: str,
    value: str,
    classification_field_name: str | None = None,
) -> None:
    classification = _classify_artifact(classification_field_name or field_name, value)
    if policy.allows(classification):
        return
    findings.append(
        EgressShadowFinding(
            artifact_kind=artifact_kind,
            field_name=field_name,
            classification=classification,
            redacted_value=f"[REDACTED_{classification.upper()}]",
        )
    )


class _DOMFieldParser(HTMLParser):
    """Collect opaque DOM field references and values without retaining them."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._tag_counts: dict[str, int] = {}
        self.fields: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        index = self._tag_counts.get(tag, 0)
        self._tag_counts[tag] = index + 1
        for name, value in attrs:
            if value:
                self.fields.append((f"{tag}[{index}].{name.lower()}", value))

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.fields.append(("text", data))


def _dom_fields(dom: str) -> list[tuple[str, str]]:
    parser = _DOMFieldParser()
    parser.feed(dom)
    parser.close()
    return parser.fields


def _classify_artifact(field_name: str, value: str) -> DataClassification:
    """Classify an opaque artifact without retaining any extracted source value."""

    classification = classify_value(field_name, value)
    if classification is not DataClassification.INTERNAL:
        return classification
    if re.search(r"(?:password|passwd|pwd|secret|token)\s*[:=]", value, re.IGNORECASE):
        return DataClassification.CREDENTIAL
    if re.search(r"(?:account|amount|beneficiary|card)\s*[:=]", value, re.IGNORECASE):
        return DataClassification.FINANCIAL
    return classification
