"""Action-governor planning primitives for a single browser observation."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from .analysis import analyze_action, build_observation, evaluate_audit_policy
from .audit import observation_hash
from .contracts import ActionIntent, ExecutionEffect, ObservationContext, PageReadiness, PolicyDecision, TaskContract


class GovernanceBatchError(ValueError):
    pass


class GovernedActionCandidate(BaseModel):
    """The deterministic governance interpretation of one typed action."""

    action_index: int
    intent: ActionIntent
    decision: PolicyDecision


class GovernanceBatchPlan(BaseModel):
    """All candidates proposed from one immutable page observation."""

    observation: ObservationContext
    candidates: list[GovernedActionCandidate] = Field(default_factory=list)

    def external_write_candidates(self) -> list[GovernedActionCandidate]:
        return [
            candidate
            for candidate in self.candidates
            if candidate.intent.effect == ExecutionEffect.EXTERNAL_WRITE
        ]

    def require_single_external_write(self) -> None:
        """Prevent a stale page snapshot from authorizing two business commits."""

        external_writes = self.external_write_candidates()
        if len(external_writes) > 1:
            indices = ", ".join(str(candidate.action_index) for candidate in external_writes)
            raise GovernanceBatchError(
                "A single observation cannot authorize multiple external writes; "
                f"re-observe before candidates [{indices}]"
            )


def build_governance_batch_plan(
    *,
    task_id: str,
    step_id: str,
    actions: list[Any],
    page_url: str,
    page_html: str,
    element_lookup: dict[str, dict[str, Any]] | None,
    hmac_secret: str | bytes | None,
    readiness: PageReadiness = PageReadiness.UNKNOWN,
    readiness_confidence: float = 0.0,
    task_contract: TaskContract | None = None,
    now: datetime | None = None,
) -> GovernanceBatchPlan:
    """Analyze all actions against the same HMAC-bound page observation.

    This function has no browser or database side effect.  The future enforce
    path must call ``require_single_external_write`` before issuing any permit
    from this plan.
    """

    if not hmac_secret:
        raise GovernanceBatchError("Governance planning requires GOVERNANCE_AUDIT_HMAC_SECRET")

    snapshot_hash = observation_hash(url=page_url, html=page_html, secret=hmac_secret)
    observation = build_observation(
        task_id=task_id,
        step_id=step_id,
        url=page_url,
        html=page_html,
        snapshot_hash=snapshot_hash,
        readiness=readiness,
        readiness_confidence=readiness_confidence,
    )
    candidates: list[GovernedActionCandidate] = []
    for action_index, action in enumerate(actions):
        element_id = getattr(action, "element_id", None)
        element = (element_lookup or {}).get(str(element_id)) if element_id is not None else None
        intent = analyze_action(
            task_id=task_id,
            step_id=step_id,
            action=action,
            observation=observation,
            element=element,
            hmac_secret=hmac_secret,
        )
        candidates.append(
            GovernedActionCandidate(
                action_index=action_index,
                intent=intent,
                decision=evaluate_audit_policy(
                    intent,
                    observation=observation,
                    task_contract=task_contract,
                    now=now,
                ),
            )
        )

    plan = GovernanceBatchPlan(observation=observation, candidates=candidates)
    plan.require_single_external_write()
    return plan
