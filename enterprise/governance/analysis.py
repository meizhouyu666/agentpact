"""Deterministic audit-only interpretation of browser action candidates."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from .classification import action_fingerprint
from .contracts import (
    ActionIntent,
    DecisionOutcome,
    ExecutionEffect,
    ObservationContext,
    PageReadiness,
    PolicyDecision,
    TaskContract,
)

_CRITICAL_WORDS = ("transfer", "payment", "wire", "delete")
_READ_WORDS = ("download", "export", "extract", "read", "search", "navigate", "query")
_SENSITIVE_DOWNLOAD_WORDS = ("customer", "statement", "account", "transaction", "beneficiary")
MIN_READINESS_CONFIDENCE = 0.6
SUPPORTED_TYPED_ACTION_TYPES = frozenset(
    {
        "checkbox",
        "click",
        "close_page",
        "complete",
        "download_file",
        "drag",
        "extract",
        "goto_url",
        "hover",
        "input_text",
        "keypress",
        "left_mouse",
        "move",
        "null_action",
        "reload_page",
        "scroll",
        "select_option",
        "solve_captcha",
        "terminate",
        "upload_file",
        "verification_code",
        "wait",
    }
)


def build_observation(
    *,
    task_id: str,
    step_id: str,
    url: str,
    html: str,
    readiness: PageReadiness = PageReadiness.UNKNOWN,
    readiness_confidence: float | None = None,
    snapshot_hash: str | None = None,
) -> ObservationContext:
    snapshot_hash = snapshot_hash or hashlib.sha256(f"{url}\n{html}".encode("utf-8")).hexdigest()
    if readiness_confidence is None:
        readiness_confidence = 1.0 if readiness is PageReadiness.READY else 0.0
    return ObservationContext(
        observation_id=f"obs_{uuid4().hex}",
        task_id=task_id,
        step_id=step_id,
        page_url=url,
        snapshot_hash=snapshot_hash,
        readiness=readiness,
        readiness_confidence=readiness_confidence,
        captured_at=datetime.now(timezone.utc),
    )


def analyze_action(
    *,
    task_id: str,
    step_id: str,
    action: Any,
    observation: ObservationContext,
    element: dict[str, Any] | None = None,
    hmac_secret: str | bytes,
) -> ActionIntent:
    payload = action.model_dump(mode="json", exclude_none=True)
    action_type = normalize_typed_action_type(payload.get("action_type"))
    text = _element_text(element).lower()
    operation = _operation(action_type, text)
    effect = _effect(operation)
    sensitive_data_types = _sensitive_data_types(operation, text)
    fingerprint = action_fingerprint(
        task_id=task_id,
        step_id=step_id,
        action_payload=payload,
        observation_hash=observation.snapshot_hash,
        secret=hmac_secret,
    )

    return ActionIntent(
        intent_id=f"intent_{uuid4().hex}",
        task_id=task_id,
        step_id=step_id,
        action_fingerprint=fingerprint,
        observation_id=observation.observation_id,
        operation=operation,
        effect=effect,
        target={"element_id": payload.get("element_id"), "text": _element_text(element)},
        sensitive_data_types=sensitive_data_types,
        extracted_facts=_element_facts(element),
        confidence=0.7 if element else 0.45,
        evidence=[f"action_type={action_type}", f"target_text={_element_text(element)[:120]}"],
        expected_outcome={"page_url": observation.page_url},
    )


def evaluate_audit_policy(
    intent: ActionIntent,
    *,
    observation: ObservationContext | None = None,
    task_contract: TaskContract | None = None,
    policy_version: str = "phase2-v1",
    now: datetime | None = None,
) -> PolicyDecision:
    """Return the decision that enforce mode would consider; never block in audit mode."""

    now = now or datetime.now(timezone.utc)
    if task_contract and task_contract.expires_at and now >= task_contract.expires_at:
        outcome = DecisionOutcome.DENY
        risk = "high"
        reasons = ["Task contract has expired"]
    elif task_contract and task_contract.allowed_operations and intent.operation not in task_contract.allowed_operations:
        outcome = DecisionOutcome.DENY
        risk = "high"
        reasons = [f"Operation {intent.operation} is outside the task contract"]
    elif observation and observation.readiness in {
        PageReadiness.LOADING,
        PageReadiness.TRANSITIONING,
        PageReadiness.BLOCKED,
    }:
        outcome = DecisionOutcome.NEEDS_HUMAN
        risk = "unknown"
        reasons = [f"Page readiness is {observation.readiness.value}"]
    elif observation and observation.readiness is PageReadiness.UNKNOWN:
        outcome = DecisionOutcome.NEEDS_HUMAN
        risk = "unknown"
        reasons = ["Page readiness is unknown"]
    elif observation and observation.readiness_confidence < MIN_READINESS_CONFIDENCE:
        outcome = DecisionOutcome.NEEDS_HUMAN
        risk = "unknown"
        reasons = [
            "Page readiness confidence is below the governed minimum "
            f"of {MIN_READINESS_CONFIDENCE:.2f}"
        ]
    elif intent.effect == ExecutionEffect.EXTERNAL_WRITE and intent.confidence < 0.6:
        outcome = DecisionOutcome.NEEDS_HUMAN
        risk = "unknown"
        reasons = ["External state change has insufficient grounded target evidence"]
    elif intent.operation in {"payment", "approve", "delete"}:
        outcome = DecisionOutcome.REQUIRE_APPROVAL
        risk = "critical" if intent.operation in {"payment", "delete"} else "high"
        reasons = [f"High-impact operation classified as {intent.operation}"]
    elif intent.operation == "submit":
        outcome = DecisionOutcome.REQUIRE_APPROVAL
        risk = "high"
        reasons = ["Submit operation may cross a business commit boundary"]
    elif intent.operation == "download" and "financial" in intent.sensitive_data_types:
        outcome = DecisionOutcome.REQUIRE_APPROVAL
        risk = "high"
        reasons = ["Download target appears to contain financial customer data"]
    else:
        outcome = DecisionOutcome.ALLOW
        risk = "low"
        reasons = ["Audit-only baseline rule"]

    return PolicyDecision(
        decision_id=f"decision_{uuid4().hex}",
        intent_id=intent.intent_id,
        outcome=outcome,
        risk_level=risk,
        reasons=reasons,
        matched_rules=[f"audit:{intent.operation}:{intent.effect.value}"],
        policy_version=policy_version,
    )


def normalize_typed_action_type(value: Any) -> str:
    """Return only a known Skyvern typed-action value or the safe sentinel."""

    normalized = str(value or "").strip().lower()
    return normalized if normalized in SUPPORTED_TYPED_ACTION_TYPES else "unknown"


def _operation(action_type: str, target_text: str) -> str:
    combined = f"{action_type} {target_text}"
    # Preserve the semantic distinction between approving a payment and
    # initiating the payment itself.  Both labels may contain the word
    # ``payment``, but approval is an internal governance transition.
    if "approve" in combined:
        return "approve"
    if any(word in combined for word in _CRITICAL_WORDS):
        return "payment" if any(word in combined for word in ("transfer", "payment", "wire", "pay")) else "delete"
    if "submit" in combined or "confirm" in combined:
        return "submit"
    if any(word in combined for word in _READ_WORDS):
        return "download" if any(word in combined for word in ("download", "export")) else "read"
    return action_type


def _effect(operation: str) -> ExecutionEffect:
    if operation in {"payment", "delete"}:
        return ExecutionEffect.EXTERNAL_WRITE
    if operation in {"approve", "submit", "input_text", "click", "select_option"}:
        return ExecutionEffect.INTERNAL_WRITE
    if operation in {"download", "read", "navigate", "extract"}:
        return ExecutionEffect.READ
    return ExecutionEffect.NONE


def _element_text(element: dict[str, Any] | None) -> str:
    if not element:
        return ""
    attributes = element.get("attributes") or {}
    return " ".join(
        str(value)
        for value in (
            element.get("text"),
            attributes.get("aria-label"),
            attributes.get("title"),
            attributes.get("value"),
        )
        if value
    )


def _element_facts(element: dict[str, Any] | None) -> dict[str, Any]:
    """Expose stable non-governance data attributes to a semantic adapter."""

    if not element:
        return {}
    attributes = element.get("attributes") or {}
    facts: dict[str, Any] = {}
    for key, value in attributes.items():
        normalized_key = str(key).lower()
        if not normalized_key.startswith("data-"):
            continue
        field_name = normalized_key.removeprefix("data-").replace("-", "_")
        if field_name.startswith("governance_"):
            continue
        facts[field_name] = value
    return facts


def _sensitive_data_types(operation: str, target_text: str) -> set[str]:
    if operation == "download" and any(word in target_text.lower() for word in _SENSITIVE_DOWNLOAD_WORDS):
        return {"financial"}
    if "password" in target_text.lower():
        return {"credential"}
    return set()
