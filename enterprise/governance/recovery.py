"""Pure L0--L4 failure classification and recovery-policy contracts.

The module does not retry browser actions.  It only classifies evidence and
returns a deterministic decision for a future coordinator or recovery service.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from .contracts import ExecutionAttemptStatus, ExecutionEffect


class ExecutionFailureClass(StrEnum):
    TECHNICAL_TRANSIENT = "technical_transient"
    PAGE_LOCATOR = "page_locator"
    BROWSER_ENVIRONMENT = "browser_environment"
    BUSINESS_STATE_MISMATCH = "business_state_mismatch"
    POLICY_OR_PERMISSION = "policy_or_permission"
    UNKNOWN = "unknown"
    MANUAL_BLOCKER = "manual_blocker"


class RecoveryLevel(StrEnum):
    L0 = "L0"
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"
    L4 = "L4"


class ExecutionFailureEvent(BaseModel):
    task_id: str
    step_id: str
    action_fingerprint: str | None = None
    observation_id: str | None = None
    attempt_status: ExecutionAttemptStatus | None = None
    effect: ExecutionEffect = ExecutionEffect.NONE
    failure_class: ExecutionFailureClass
    message: str = ""
    contract_scope_unchanged: bool = True
    result_evidence_available: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class RecoveryDecision(BaseModel):
    failure_class: ExecutionFailureClass
    level: RecoveryLevel
    action: str
    max_attempts: int = Field(ge=0)
    requires_reauthorization: bool = False
    requires_result_probe: bool = False
    reason: str


def decide_recovery(event: ExecutionFailureEvent) -> RecoveryDecision:
    """Return a fail-safe decision without performing side effects."""

    if event.failure_class is ExecutionFailureClass.UNKNOWN or event.attempt_status is ExecutionAttemptStatus.UNKNOWN:
        return RecoveryDecision(
            failure_class=ExecutionFailureClass.UNKNOWN,
            level=RecoveryLevel.L4,
            action="pause_for_result_probe",
            max_attempts=0,
            requires_result_probe=True,
            reason="Unknown side-effect outcome must be resolved by result evidence, never replayed",
        )
    if event.failure_class in {ExecutionFailureClass.POLICY_OR_PERMISSION, ExecutionFailureClass.MANUAL_BLOCKER}:
        return RecoveryDecision(
            failure_class=event.failure_class,
            level=RecoveryLevel.L4,
            action="pause_for_human_or_reauthorization",
            max_attempts=0,
            requires_reauthorization=event.failure_class is ExecutionFailureClass.POLICY_OR_PERMISSION,
            reason="Policy, permission, or manual blockers cannot be retried by the browser",
        )
    if event.failure_class is ExecutionFailureClass.BUSINESS_STATE_MISMATCH:
        if event.contract_scope_unchanged:
            return RecoveryDecision(
                failure_class=event.failure_class,
                level=RecoveryLevel.L3,
                action="request_constrained_replan",
                max_attempts=0,
                reason="Business state changed within the existing Contract scope",
            )
        return RecoveryDecision(
            failure_class=event.failure_class,
            level=RecoveryLevel.L4,
            action="invalidate_and_reauthorize",
            max_attempts=0,
            requires_reauthorization=True,
            reason="Business state change expands or changes Contract scope",
        )
    if event.failure_class is ExecutionFailureClass.TECHNICAL_TRANSIENT:
        return RecoveryDecision(
            failure_class=event.failure_class,
            level=RecoveryLevel.L0,
            action="retry_same_action",
            max_attempts=1,
            reason="Bounded technical retry",
        )
    if event.failure_class is ExecutionFailureClass.PAGE_LOCATOR:
        return RecoveryDecision(
            failure_class=event.failure_class,
            level=RecoveryLevel.L1,
            action="reobserve_and_redecide",
            max_attempts=0,
            reason="Page evidence must be refreshed",
        )
    return RecoveryDecision(
        failure_class=event.failure_class,
        level=RecoveryLevel.L2,
        action="create_new_step_after_rescrape",
        max_attempts=1,
        reason="Browser environment recovery is delegated to the Skyvern step boundary",
    )
