"""Trusted active manifest for the Stripe test-mode sandbox candidate.

Kind is PRODUCTION because this pack targets a real external system; it is NOT
production eligible until the adoption gates in ``PACK.md`` are closed.
"""

from enterprise.governance.capabilities import (
    AuthorizationDimension,
    CapabilityAccessPolicy,
    CapabilityDefinition,
    ScopeDimension,
)
from enterprise.governance.domain_packs import DomainPackKind, DomainPackManifest

from .constants import (
    ACCESS_POLICY_REF,
    CAPABILITY_ID,
    PACK_ID,
    PACK_VERSION,
    RESULT_PROBE_REF,
    RISK_POLICY_REF,
    WORK_ORDER_REF,
)
from .models import StripePaymentFacts


def build_manifest() -> DomainPackManifest:
    capability = CapabilityDefinition(
        capability_id=CAPABILITY_ID,
        version=PACK_VERSION,
        domain=PACK_ID,
        display_name="Submit a Stripe test-mode payment",
        intent_examples=["Submit the approved test-mode payment exactly once"],
        input_schema=StripePaymentFacts.model_json_schema(),
        state_transition={"from": "draft", "to": "submitted"},
        access_policy_ref=ACCESS_POLICY_REF,
        risk_policy_ref=RISK_POLICY_REF,
        work_order_template_ref=WORK_ORDER_REF,
        result_probe_ref=RESULT_PROBE_REF,
        access_policy=CapabilityAccessPolicy(
            required_scope_dimensions={ScopeDimension.DEPARTMENT, ScopeDimension.BUSINESS_LINE},
            role_dimensions={
                "operator": {
                    AuthorizationDimension.DISCOVER,
                    AuthorizationDimension.READ_RECORD,
                    AuthorizationDimension.REQUEST_TRANSITION,
                    AuthorizationDimension.EXECUTE_TRANSITION,
                    AuthorizationDimension.REQUEST_APPROVAL,
                },
                "approver": {
                    AuthorizationDimension.DISCOVER,
                    AuthorizationDimension.READ_RECORD,
                    AuthorizationDimension.ADJUDICATE_APPROVAL,
                },
            },
        ),
    )
    return DomainPackManifest(
        pack_id=PACK_ID,
        version=PACK_VERSION,
        kind=DomainPackKind.PRODUCTION,
        display_name="Stripe Payment (Test Mode) Sandbox",
        owner="AgentPact Stripe pack adopter (pending assignment)",
        capabilities=[capability],
        canonical_fact_schema=StripePaymentFacts.model_json_schema(),
        state_transitions={"draft": ["submitted"], "submitted": []},
        policy_refs={ACCESS_POLICY_REF, RISK_POLICY_REF},
        result_probe_refs={RESULT_PROBE_REF},
        production_eligible=False,
    )
