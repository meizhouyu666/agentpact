"""Isolated end-to-end enforce harness for the synthetic payment Domain Pack.

This module intentionally has no Skyvern, Playwright, ActionHandler, or global
governance-mode integration. It validates the control protocol only.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from enum import StrEnum
import json
from uuid import uuid4

from pydantic import BaseModel, Field

from enterprise.agent.work_orders import (
    BusinessPlan,
    BusinessPlanStep,
    ExecutionWorkOrder,
    RecoveryLevel,
    validate_business_plan,
    validate_work_order,
)
from enterprise.auth.schemas import UserContext
from enterprise.governance.capabilities import (
    CapabilityDataScope,
    CapabilityResolutionContext,
    CapabilityResolver,
)
from enterprise.governance.classification import action_fingerprint, hmac_fingerprint
from enterprise.governance.contracts import (
    ActionIntent,
    ExecutionAttempt,
    ExecutionAttemptStatus,
    ExecutionEffect,
    ExecutionPermit,
    GovernanceMode,
    PolicyDecision,
    TaskContract,
)
from enterprise.governance.domain_packs import DomainPackRegistry
from enterprise.governance.result_probes import ResultProbeEvidence, ResultProbeStatus

from .accounts import SYNTHETIC_ACCOUNTS
from .constants import (
    BUSINESS_LINE_ID,
    CAPABILITY_ID,
    PACK_ID,
    PAYMENTS_DEPARTMENT_ID,
    POLICY_VERSION,
    RESULT_PROBE_REF,
    TENANT_ID,
)
from .definition import build_manifest
from .models import (
    AmbiguousSubmissionFailure,
    DefiniteSubmissionFailure,
    FaultMode,
    PaymentFacts,
    SyntheticPaymentError,
    SyntheticPaymentRecord,
)
from .policy import authorize_after_approval, require_approval_decision
from .store import SyntheticPaymentResultProbe, SyntheticPaymentStore


class ChallengeState(StrEnum):
    PENDING_APPROVAL = "pending_approval"
    REJECTED = "rejected"
    READY = "ready"
    INVALIDATED = "invalidated"
    CONFIRMED = "confirmed"
    UNKNOWN = "unknown"
    FAILED = "failed"


class SyntheticApproval(BaseModel):
    approval_id: str
    requester_user_id: str
    approver_user_id: str
    approved_at: datetime


class SyntheticSubmissionChallenge(BaseModel):
    challenge_id: str
    state: ChallengeState
    facts: PaymentFacts
    requester_user_id: str
    contract: TaskContract
    plan: BusinessPlan
    work_order: ExecutionWorkOrder
    intent: ActionIntent
    observation_hash: str
    decision: PolicyDecision
    approval: SyntheticApproval | None = None
    permit: ExecutionPermit | None = None
    attempt: ExecutionAttempt | None = None
    result_probe: ResultProbeEvidence | None = None


class SyntheticAuditEvent(BaseModel):
    event_type: str
    challenge_id: str
    payment_id: str
    state: ChallengeState
    created_at: datetime
    metadata: dict[str, str | int | bool] = Field(default_factory=dict)


class SyntheticPaymentEnforceHarness:
    def __init__(
        self,
        *,
        hmac_secret: str,
        store: SyntheticPaymentStore | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not hmac_secret:
            raise ValueError("Synthetic enforce harness requires an HMAC secret")
        self._secret = hmac_secret
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.store = store or SyntheticPaymentStore()
        self.probe = SyntheticPaymentResultProbe(self.store, clock=self._clock)
        self._registry = DomainPackRegistry([build_manifest()])
        self._challenges: dict[str, SyntheticSubmissionChallenge] = {}
        self.audit_events: list[SyntheticAuditEvent] = []

    def prepare_submission(
        self,
        *,
        requester: UserContext,
        facts: PaymentFacts,
    ) -> SyntheticSubmissionChallenge:
        now = self._now()
        self._require_operator(requester)
        scope = CapabilityDataScope(
            department_id=PAYMENTS_DEPARTMENT_ID,
            business_line_id=BUSINESS_LINE_ID,
            resource_ids={facts.payment_id},
        )
        grants = CapabilityResolver(self._registry.capability_registry()).resolve(
            CapabilityResolutionContext(
                user=requester,
                tenant_id=TENANT_ID,
                data_scope=scope,
                installed_capability_ids={CAPABILITY_ID},
                policy_snapshot_version=POLICY_VERSION,
                resolved_at=now,
            )
        )
        grant = grants.grants[0]
        grants.require_executable(
            capability_id=CAPABILITY_ID,
            grant_id=grant.grant_id,
            now=now,
        )

        record = self.store.create_draft(facts=facts, requester_user_id=requester.user_id)
        task_id = f"synthetic_task_{uuid4().hex}"
        step_id = f"synthetic_step_{uuid4().hex}"
        contract = TaskContract(
            contract_id=f"synthetic_contract_{uuid4().hex}",
            task_id=task_id,
            organization_id=TENANT_ID,
            initiator_id=requester.user_id,
            department_id=PAYMENTS_DEPARTMENT_ID,
            business_line_id=BUSINESS_LINE_ID,
            goal="Submit one synthetic payment after independent approval",
            allowed_operations={CAPABILITY_ID},
            data_scope=scope.model_dump(mode="json"),
            authorization_snapshot={
                "grant_id": grant.grant_id,
                "grant_expires_at": grant.expires_at.isoformat(),
                "principal_id": requester.user_id,
                "tenant_id": TENANT_ID,
                "pack_id": PACK_ID,
            },
            policy_profile=PACK_ID,
            policy_version=POLICY_VERSION,
            success_criteria=["Canonical result probe confirms the submitted payment"],
            expires_at=now + timedelta(minutes=15),
            mode=GovernanceMode.ENFORCE,
        )
        plan_step = BusinessPlanStep(
            capability_id=CAPABILITY_ID,
            grant_id=grant.grant_id,
            contract_id=contract.contract_id,
            inputs=facts.model_dump(mode="json"),
            expected_transition={"from": "draft", "to": "submitted", "version": facts.object_version + 1},
            success_criteria=contract.success_criteria,
        )
        plan = BusinessPlan(
            task_id=task_id,
            contract_id=contract.contract_id,
            data_scope=scope,
            steps=[plan_step],
        )
        validate_business_plan(plan, grants, now=now)
        work_order = ExecutionWorkOrder(
            business_plan_step_id=plan_step.step_id,
            task_id=task_id,
            contract_id=contract.contract_id,
            grant_id=grant.grant_id,
            navigation_goal="Submit only the declared synthetic payment draft",
            allowed_operations={"read", "input", CAPABILITY_ID},
            prohibited_operations={"delete", "change_beneficiary_after_approval"},
            success_criteria=contract.success_criteria,
            required_evidence=["canonical_payment_version", "approval", "result_probe"],
            max_recovery_level=RecoveryLevel.L1,
            result_probe_ref=RESULT_PROBE_REF,
        )
        validate_work_order(work_order, plan, plan_step, grants, now=now)

        observation_hash = self._observation_hash(record)
        action_payload = self._action_payload(record)
        fingerprint = action_fingerprint(
            task_id=task_id,
            step_id=step_id,
            action_payload=action_payload,
            observation_hash=observation_hash,
            secret=self._secret,
        )
        intent = ActionIntent(
            intent_id=f"synthetic_intent_{uuid4().hex}",
            task_id=task_id,
            step_id=step_id,
            action_fingerprint=fingerprint,
            observation_id=observation_hash,
            operation=CAPABILITY_ID,
            effect=ExecutionEffect.EXTERNAL_WRITE,
            target={"payment_id": facts.payment_id},
            extracted_facts={"object_version": facts.object_version, "status": record.status.value},
            confidence=1.0,
            evidence=["trusted synthetic canonical state"],
            expected_outcome={"status": "submitted", "object_version": facts.object_version + 1},
        )
        decision = require_approval_decision(intent, facts)
        challenge = SyntheticSubmissionChallenge(
            challenge_id=f"synthetic_challenge_{uuid4().hex}",
            state=ChallengeState.PENDING_APPROVAL,
            facts=facts,
            requester_user_id=requester.user_id,
            contract=contract,
            plan=plan,
            work_order=work_order,
            intent=intent,
            observation_hash=observation_hash,
            decision=decision,
        )
        self._challenges[challenge.challenge_id] = challenge
        self._audit(challenge, "approval_requested", risk_level=decision.risk_level)
        return challenge.model_copy(deep=True)

    def decide_approval(
        self,
        *,
        challenge_id: str,
        requester: UserContext,
        approver: UserContext,
        approved: bool,
    ) -> SyntheticSubmissionChallenge:
        challenge = self._require_challenge(challenge_id)
        if challenge.state is not ChallengeState.PENDING_APPROVAL:
            raise SyntheticPaymentError("Synthetic challenge is not awaiting approval")
        if requester.user_id != challenge.requester_user_id:
            raise SyntheticPaymentError("Requester identity does not match the challenge snapshot")
        if not approved:
            challenge.state = ChallengeState.REJECTED
            self._audit(challenge, "approval_rejected")
            return challenge.model_copy(deep=True)

        now = self._now()
        try:
            self._require_current_authorization(challenge, requester=requester, now=now)
            current = self.store.require(challenge.facts.payment_id)
            self._require_current_bindings(challenge, current)
        except SyntheticPaymentError:
            challenge.state = ChallengeState.INVALIDATED
            self._audit(challenge, "authorization_invalidated")
            raise
        decision = authorize_after_approval(
            intent=challenge.intent,
            facts=challenge.facts,
            requester=requester,
            approver=approver,
        )
        approval = SyntheticApproval(
            approval_id=f"synthetic_approval_{uuid4().hex}",
            requester_user_id=requester.user_id,
            approver_user_id=approver.user_id,
            approved_at=now,
        )
        permit = ExecutionPermit(
            permit_id=f"synthetic_permit_{uuid4().hex}",
            task_id=challenge.contract.task_id,
            step_id=challenge.intent.step_id,
            action_fingerprint=challenge.intent.action_fingerprint,
            observation_id=challenge.observation_hash,
            policy_decision_id=decision.decision_id,
            issued_at=now,
            expires_at=now + timedelta(seconds=60),
        )
        challenge.approval = approval
        challenge.decision = decision
        challenge.permit = permit
        challenge.state = ChallengeState.READY
        self._audit(challenge, "permit_issued")
        return challenge.model_copy(deep=True)

    def execute_submission(
        self,
        *,
        challenge_id: str,
        fault_mode: FaultMode = FaultMode.NONE,
    ) -> SyntheticSubmissionChallenge:
        challenge = self._require_challenge(challenge_id)
        if challenge.state is not ChallengeState.READY or challenge.permit is None or challenge.approval is None:
            raise SyntheticPaymentError("Synthetic challenge has no executable approval and permit")
        now = self._now()
        current = self.store.require(challenge.facts.payment_id)
        try:
            self._require_current_authorization(challenge, now=now)
            self._require_current_bindings(challenge, current)
        except SyntheticPaymentError:
            challenge.state = ChallengeState.INVALIDATED
            self._audit(challenge, "authorization_invalidated")
            raise
        if not challenge.permit.matches(
            action_fingerprint=challenge.intent.action_fingerprint,
            observation_id=challenge.observation_hash,
            now=now,
        ):
            challenge.state = ChallengeState.INVALIDATED
            self._audit(challenge, "permit_rejected")
            raise SyntheticPaymentError("Synthetic permit is expired, consumed, or binding-mismatched")

        idempotency_key = f"synthetic:{challenge.challenge_id}"
        challenge.permit = challenge.permit.model_copy(update={"used_at": now})
        challenge.attempt = ExecutionAttempt(
            attempt_id=f"synthetic_attempt_{uuid4().hex}",
            task_id=challenge.contract.task_id,
            step_id=challenge.intent.step_id,
            contract_id=challenge.contract.contract_id,
            action_fingerprint=challenge.intent.action_fingerprint,
            observation_hash=challenge.observation_hash,
            idempotency_key=idempotency_key,
            status=ExecutionAttemptStatus.EXECUTING,
            started_at=now,
        )
        self._audit(challenge, "attempt_executing")
        try:
            self.store.submit(
                payment_id=challenge.facts.payment_id,
                expected_version=challenge.facts.object_version,
                approval_id=challenge.approval.approval_id,
                idempotency_key=idempotency_key,
                fault_mode=fault_mode,
            )
        except DefiniteSubmissionFailure as exc:
            evidence = self.probe.probe(resource_id=challenge.facts.payment_id, idempotency_key=idempotency_key)
            self._finish(challenge, ChallengeState.FAILED, evidence, str(exc))
        except AmbiguousSubmissionFailure as exc:
            evidence = self.probe.probe(resource_id=challenge.facts.payment_id, idempotency_key=idempotency_key)
            final_state = (
                ChallengeState.CONFIRMED
                if evidence.status is ResultProbeStatus.CONFIRMED
                else ChallengeState.UNKNOWN
            )
            self._finish(challenge, final_state, evidence, str(exc))
        else:
            evidence = self.probe.probe(resource_id=challenge.facts.payment_id, idempotency_key=idempotency_key)
            final_state = (
                ChallengeState.CONFIRMED
                if evidence.status is ResultProbeStatus.CONFIRMED
                else ChallengeState.UNKNOWN
            )
            self._finish(challenge, final_state, evidence, None)
        return challenge.model_copy(deep=True)

    def resolve_unknown(self, challenge_id: str) -> SyntheticSubmissionChallenge:
        challenge = self._require_challenge(challenge_id)
        if challenge.state is not ChallengeState.UNKNOWN or challenge.attempt is None:
            raise SyntheticPaymentError("Only an unknown synthetic attempt can be probed")
        evidence = self.probe.probe(
            resource_id=challenge.facts.payment_id,
            idempotency_key=challenge.attempt.idempotency_key,
        )
        if evidence.status is ResultProbeStatus.CONFIRMED:
            self._finish(challenge, ChallengeState.CONFIRMED, evidence, None)
        elif evidence.status is ResultProbeStatus.NOT_CONFIRMED:
            self._finish(challenge, ChallengeState.FAILED, evidence, "Result probe confirmed no submission")
        else:
            challenge.result_probe = evidence
            self._audit(challenge, "result_still_unknown")
        return challenge.model_copy(deep=True)

    def get_challenge(self, challenge_id: str) -> SyntheticSubmissionChallenge:
        return self._require_challenge(challenge_id).model_copy(deep=True)

    def _finish(
        self,
        challenge: SyntheticSubmissionChallenge,
        state: ChallengeState,
        evidence: ResultProbeEvidence,
        error_message: str | None,
    ) -> None:
        if challenge.attempt is None:
            raise SyntheticPaymentError("Synthetic attempt is missing")
        status = {
            ChallengeState.CONFIRMED: ExecutionAttemptStatus.CONFIRMED,
            ChallengeState.FAILED: ExecutionAttemptStatus.FAILED,
            ChallengeState.UNKNOWN: ExecutionAttemptStatus.UNKNOWN,
        }[state]
        challenge.state = state
        challenge.result_probe = evidence
        challenge.attempt = challenge.attempt.model_copy(
            update={
                "status": status,
                "completed_at": self._now(),
                "result_probe": evidence.model_dump(mode="json"),
                "error_message": error_message,
            }
        )
        self._audit(challenge, f"attempt_{state.value}")

    def _require_current_bindings(
        self,
        challenge: SyntheticSubmissionChallenge,
        record: SyntheticPaymentRecord,
    ) -> None:
        observation_hash = self._observation_hash(record)
        fingerprint = action_fingerprint(
            task_id=challenge.contract.task_id,
            step_id=challenge.intent.step_id,
            action_payload=self._action_payload(record),
            observation_hash=observation_hash,
            secret=self._secret,
        )
        if observation_hash != challenge.observation_hash or fingerprint != challenge.intent.action_fingerprint:
            raise SyntheticPaymentError("Synthetic payment changed after authorization; replan is required")

    def _require_current_authorization(
        self,
        challenge: SyntheticSubmissionChallenge,
        *,
        now: datetime,
        requester: UserContext | None = None,
    ) -> None:
        """Revalidate the task contract and current operator grant at each transition."""

        if challenge.contract.expires_at and now >= challenge.contract.expires_at:
            raise SyntheticPaymentError("Synthetic task contract has expired; reauthorization is required")

        snapshot = challenge.contract.authorization_snapshot
        raw_grant_expiry = snapshot.get("grant_expires_at")
        if not raw_grant_expiry:
            raise SyntheticPaymentError("Synthetic authorization snapshot has no grant expiry")
        try:
            grant_expires_at = (
                raw_grant_expiry
                if isinstance(raw_grant_expiry, datetime)
                else datetime.fromisoformat(str(raw_grant_expiry))
            )
        except ValueError as exc:
            raise SyntheticPaymentError("Synthetic authorization snapshot has an invalid grant expiry") from exc
        if grant_expires_at.tzinfo is None:
            grant_expires_at = grant_expires_at.replace(tzinfo=timezone.utc)
        if now >= grant_expires_at:
            raise SyntheticPaymentError("Synthetic capability grant has expired; reauthorization is required")

        if requester is None:
            requester = next(
                (user for user in SYNTHETIC_ACCOUNTS.values() if user.user_id == challenge.requester_user_id),
                None,
            )
        if requester is None or requester.user_id != challenge.requester_user_id:
            raise SyntheticPaymentError("Synthetic requester identity is no longer available")
        if snapshot.get("principal_id") != requester.user_id or snapshot.get("tenant_id") != requester.org_id:
            raise SyntheticPaymentError("Synthetic authorization principal snapshot changed")

        scope = CapabilityDataScope(
            department_id=PAYMENTS_DEPARTMENT_ID,
            business_line_id=BUSINESS_LINE_ID,
            resource_ids={challenge.facts.payment_id},
        )
        grants = CapabilityResolver(self._registry.capability_registry()).resolve(
            CapabilityResolutionContext(
                user=requester,
                tenant_id=TENANT_ID,
                data_scope=scope,
                installed_capability_ids={CAPABILITY_ID},
                policy_snapshot_version=POLICY_VERSION,
                resolved_at=now,
            )
        )
        current_grant = next(
            (grant for grant in grants.grants if grant.capability_id == CAPABILITY_ID),
            None,
        )
        if current_grant is None:
            raise SyntheticPaymentError("Synthetic capability is no longer installed")
        try:
            grants.require_executable(
                capability_id=CAPABILITY_ID,
                grant_id=current_grant.grant_id,
                now=now,
            )
        except ValueError as exc:
            raise SyntheticPaymentError("Synthetic requester authorization is no longer executable") from exc

    def _observation_hash(self, record: SyntheticPaymentRecord) -> str:
        canonical = json.dumps(record.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hmac_fingerprint(canonical, self._secret)

    @staticmethod
    def _action_payload(record: SyntheticPaymentRecord) -> dict[str, str | int]:
        return {
            "operation": CAPABILITY_ID,
            "payment_id": record.facts.payment_id,
            "object_version": record.facts.object_version,
            "status": record.status.value,
        }

    @staticmethod
    def _require_operator(requester: UserContext) -> None:
        if requester.org_id != TENANT_ID:
            raise SyntheticPaymentError("Requester is outside the synthetic tenant")
        if requester.get_role_in_department(PAYMENTS_DEPARTMENT_ID) != "operator":
            raise SyntheticPaymentError("Only the synthetic payments operator may request submission")
        if BUSINESS_LINE_ID not in requester.business_line_ids:
            raise SyntheticPaymentError("Requester lacks the synthetic business-line scope")

    def _require_challenge(self, challenge_id: str) -> SyntheticSubmissionChallenge:
        try:
            return self._challenges[challenge_id]
        except KeyError as exc:
            raise SyntheticPaymentError("Synthetic challenge does not exist") from exc

    def _audit(self, challenge: SyntheticSubmissionChallenge, event_type: str, **metadata: str | int | bool) -> None:
        self.audit_events.append(
            SyntheticAuditEvent(
                event_type=event_type,
                challenge_id=challenge.challenge_id,
                payment_id=challenge.facts.payment_id,
                state=challenge.state,
                created_at=self._now(),
                metadata=metadata,
            )
        )

    def _now(self) -> datetime:
        value = self._clock()
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
