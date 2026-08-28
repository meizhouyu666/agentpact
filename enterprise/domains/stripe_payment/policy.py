"""Deterministic Stripe test-mode policy. No model output can alter these rules.

Rules mirror the synthetic reference pack (always-approve dual control,
separation of duties, tenant scope) so the whole governance chain is exercised
identically, while the side effect and the independent confirmation both come
from a real external system instead of the loopback console.
"""

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
from .models import StripePaymentError, StripePaymentFacts, SubmissionRisk

# USD 10,000.00 in minor units. Currency-dependent thresholds for eur/gbp/cny
# are a TODO(P3) for the adopting tenant's policy authority.
CRITICAL_AMOUNT_MINOR = 1_000_000


def assess_submission(facts: StripePaymentFacts) -> SubmissionRisk:
    if facts.amount_minor <= 0:
        raise StripePaymentError("Stripe payment amount must be positive minor units")
    if facts.amount_minor >= CRITICAL_AMOUNT_MINOR:
        return SubmissionRisk(
            risk_level="critical",
            approver_department_id=COMPLIANCE_DEPARTMENT_ID,
            reasons=["Amount meets the critical approval threshold"],
        )
    return SubmissionRisk(
        risk_level="high",
        approver_department_id=PAYMENTS_DEPARTMENT_ID,
        reasons=["Every Stripe payment submission requires dual control"],
    )


def require_approval_decision(intent: ActionIntent, facts: StripePaymentFacts) -> PolicyDecision:
    risk = assess_submission(facts)
    return PolicyDecision(
        decision_id=f"decision_{uuid4().hex}",
        intent_id=intent.intent_id,
        outcome=DecisionOutcome.REQUIRE_APPROVAL,
        risk_level=risk.risk_level,
        reasons=risk.reasons,
        matched_rules=["stripe.payment.submit.always-approve", f"stripe.payment.{risk.risk_level}"],
        required_approver={"department_id": risk.approver_department_id, "role": "approver"},
        policy_version=POLICY_VERSION,
    )


def approval_route(facts: StripePaymentFacts) -> ApprovalRoute:
    risk = assess_submission(facts)
    return ApprovalRoute(
        requires_approval=True,
        approver_department_id=risk.approver_department_id,
        approver_role="approver",
        description=f"Stripe {risk.risk_level} payment approval",
    )


def authorize_after_approval(
    *,
    intent: ActionIntent,
    facts: StripePaymentFacts,
    requester: UserContext,
    approver: UserContext,
) -> PolicyDecision:
    risk = assess_submission(facts)
    if requester.user_id == approver.user_id:
        raise StripePaymentError("Requester cannot approve the same Stripe payment")
    if requester.org_id != TENANT_ID or approver.org_id != TENANT_ID:
        raise StripePaymentError("Stripe identities must belong to the sandbox tenant")
    role = approver.get_role_in_department(risk.approver_department_id)
    if role not in {"approver", "org_admin", "super_admin"}:
        raise StripePaymentError("Approver lacks the required Stripe department role")
    if not approver.is_org_admin and BUSINESS_LINE_ID not in approver.business_line_ids:
        raise StripePaymentError("Approver lacks the Stripe business-line scope")
    return PolicyDecision(
        decision_id=f"decision_{uuid4().hex}",
        intent_id=intent.intent_id,
        outcome=DecisionOutcome.ALLOW,
        risk_level=risk.risk_level,
        reasons=["Independent Stripe approver accepted the current facts and observation"],
        matched_rules=["stripe.payment.separation-of-duties", "stripe.payment.approved"],
        policy_version=POLICY_VERSION,
    )
