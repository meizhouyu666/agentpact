"""Audit-only governance observation for the Phase 2 main-agent path."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Any, Literal

from pydantic import BaseModel, Field

from .classification import DataClassification, action_fingerprint, classify_value, hmac_fingerprint, redact_for_egress
from .egress_shadow import EgressShadowFinding, conservative_shadow_policy, scan_egress_shadow
from .models import GovernanceAuditEventModel

logger = logging.getLogger(__name__)


class CandidateEvidenceRefs(BaseModel):
    """Opaque references to evidence already observed by Skyvern."""

    observation_hash: str
    element_id: str | None = None
    element_fingerprint: str | None = None
    screenshot_fingerprints: list[str] = Field(default_factory=list)


class AuditCandidatePayload(BaseModel):
    """Versioned, redacted replay payload for an audit-only action candidate."""

    schema_version: Literal["phase2-audit-candidate-v1"] = "phase2-audit-candidate-v1"
    candidate_action: dict[str, Any]
    evidence_refs: CandidateEvidenceRefs
    egress_shadow_findings: list[EgressShadowFinding] = Field(default_factory=list)


def observation_hash(*, url: str, html: str, secret: str | None) -> str:
    """Return a stable page fingerprint without persisting page contents."""

    payload = f"{url}\n{html}".encode("utf-8")
    if secret:
        return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return hashlib.sha256(payload).hexdigest()


def redacted_action_payload(action: Any) -> dict[str, Any]:
    """Serialize an Action while redacting values whose key or shape is sensitive."""

    raw = action.model_dump(mode="json", exclude_none=True)
    return _redact_mapping(raw)


def _redact_mapping(value: Any, field_name: str = "") -> Any:
    if isinstance(value, dict):
        return {key: _redact_mapping(item, key) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_mapping(item, field_name) for item in value]
    if isinstance(value, str):
        classification = classify_value(field_name, value)
        if classification not in {DataClassification.PUBLIC, DataClassification.INTERNAL}:
            return redact_for_egress(value, classification)
    return value


def _element_evidence_fingerprint(
    *,
    observation_hash: str,
    element_id: str | None,
    runtime_fingerprint: str | None,
    element: dict[str, Any] | None,
    secret: str,
) -> str | None:
    """Re-key element evidence before persistence, including Skyvern's hash."""

    if runtime_fingerprint is None and element is None:
        return None

    material: dict[str, Any] = {
        "schema": "phase2-element-evidence-ref-v1",
        "observation_hash": observation_hash,
        "element_id": element_id,
    }
    if runtime_fingerprint is not None:
        material["runtime_fingerprint"] = runtime_fingerprint
    else:
        material["redacted_element"] = _redact_mapping(element)
    return hmac_fingerprint(
        json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")), secret
    )


async def record_action_candidates(
    *,
    db_session: Any,
    task_id: str,
    step_id: str,
    organization_id: str,
    actions: list[Any],
    page_url: str,
    page_html: str,
    mode: str,
    hmac_secret: str | None,
    element_lookup: dict[str, dict[str, Any]] | None = None,
    element_fingerprints: dict[str, str] | None = None,
    screenshots: list[bytes] | None = None,
    prompt: str | None = None,
) -> None:
    """Persist redacted candidates and opaque page evidence without policy evaluation."""

    if mode != "audit":
        raise ValueError("Action candidate recording is available only in audit mode")
    if not hmac_secret:
        raise ValueError("GOVERNANCE_AUDIT_HMAC_SECRET is required in audit mode")

    page_fingerprint = observation_hash(url=page_url, html=page_html, secret=hmac_secret)
    shadow_report = scan_egress_shadow(
        policy=conservative_shadow_policy(),
        dom=page_html,
        prompt=prompt,
        screenshots=screenshots,
    )
    try:
        for action in actions:
            element_id = getattr(action, "element_id", None)
            element = (element_lookup or {}).get(str(element_id)) if element_id is not None else None
            raw_action = action.model_dump(mode="json", exclude_none=True)
            runtime_element_fingerprint = (
                (element_fingerprints or {}).get(str(element_id)) if element_id is not None else None
            )
            element_fingerprint = _element_evidence_fingerprint(
                observation_hash=page_fingerprint,
                element_id=str(element_id) if element_id is not None else None,
                runtime_fingerprint=runtime_element_fingerprint,
                element=element,
                secret=hmac_secret,
            )
            payload = AuditCandidatePayload(
                candidate_action=_redact_mapping(raw_action),
                evidence_refs=CandidateEvidenceRefs(
                    observation_hash=page_fingerprint,
                    element_id=str(element_id) if element_id is not None else None,
                    element_fingerprint=element_fingerprint,
                    screenshot_fingerprints=[hmac_fingerprint(screenshot, hmac_secret) for screenshot in screenshots or []],
                ),
                egress_shadow_findings=shadow_report.findings,
            ).model_dump(mode="json")
            fingerprint = action_fingerprint(
                task_id=task_id,
                step_id=step_id,
                action_payload=raw_action,
                observation_hash=page_fingerprint,
                secret=hmac_secret,
            )
            db_session.add(
                GovernanceAuditEventModel(
                    task_id=task_id,
                    step_id=step_id,
                    organization_id=organization_id,
                    event_type="action_candidate",
                    mode=mode,
                    action_fingerprint=fingerprint,
                    observation_hash=page_fingerprint,
                    payload=payload,
                )
            )
        await db_session.commit()
    except Exception:
        await db_session.rollback()
        raise
