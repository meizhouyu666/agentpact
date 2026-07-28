"""Deterministic synthetic policy. No model output can alter these rules."""

from decimal import Decimal
from uuid import uuid4

from enterprise.approval.routing import ApprovalRoute
from enterprise.auth.schemas import UserContext
from enterprise.governance.contracts import ActionIntent, DecisionOutcome, PolicyDecision

from .constants import (
    BUSINESS_LINE_ID,
    COMPLIANCE_DEPARTMENT_ID,
    PAYMENTS_DEPARTMENT_ID,
    POLICY_VERSION,
    TENANT_ID,
)
from .models import PaymentFacts, SubmissionRisk, SyntheticPaymentError


CRITICAL_AMOUNT = Decimal("100000.00")


def assess_submission(facts: PaymentFacts) -> SubmissionRisk:
    if facts.amount >= CRITICAL_AMOUNT:
        return SubmissionRisk(
            risk_level="critical",
            approver_department_id=COMPLIANCE_DEPARTMENT_ID,
            reasons=["Synthetic amount meets the critical approval threshold"],
        )
    return SubmissionRisk(
        risk_level="high",
        approver_department_id=PAYMENTS_DEPARTMENT_ID,
        reasons=["Every synthetic payment submission requires dual control"],
    )


def require_approval_decision(intent: ActionIntent, facts: PaymentFacts) -> PolicyDecision:
    risk = assess_submission(facts)
    return PolicyDecision(
        decision_id=f"decision_{uuid4().hex}",
        intent_id=intent.intent_id,
        outcome=DecisionOutcome.REQUIRE_APPROVAL,
        risk_level=risk.risk_level,
        reasons=risk.reasons,
        matched_rules=["synthetic.payment.submit.always-approve", f"synthetic.payment.{risk.risk_level}"],
        required_approver={"department_id": risk.approver_department_id, "role": "approver"},
        policy_version=POLICY_VERSION,
    )


def approval_route(facts: PaymentFacts) -> ApprovalRoute:
    risk = assess_submission(facts)
    return ApprovalRoute(
        requires_approval=True,
        approver_department_id=risk.approver_department_id,
        approver_role="approver",
        description=f"Synthetic {risk.risk_level} payment approval",
    )


def authorize_after_approval(
    *,
    intent: ActionIntent,
    facts: PaymentFacts,
    requester: UserContext,
    approver: UserContext,
) -> PolicyDecision:
    risk = assess_submission(facts)
    if requester.user_id == approver.user_id:
        raise SyntheticPaymentError("Requester cannot approve the same synthetic payment")
    if requester.org_id != TENANT_ID or approver.org_id != TENANT_ID:
        raise SyntheticPaymentError("Synthetic identities must belong to the sandbox tenant")
    role = approver.get_role_in_department(risk.approver_department_id)
    if role not in {"approver", "org_admin", "super_admin"}:
        raise SyntheticPaymentError("Approver lacks the required synthetic department role")
    if not approver.is_org_admin and BUSINESS_LINE_ID not in approver.business_line_ids:
        raise SyntheticPaymentError("Approver lacks the synthetic business-line scope")
    return PolicyDecision(
        decision_id=f"decision_{uuid4().hex}",
        intent_id=intent.intent_id,
        outcome=DecisionOutcome.ALLOW,
        risk_level=risk.risk_level,
        reasons=["Independent synthetic approver accepted the current facts and observation"],
        matched_rules=["synthetic.payment.separation-of-duties", "synthetic.payment.approved"],
        policy_version=POLICY_VERSION,
    )
