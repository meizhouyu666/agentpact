"""Trusted manifest for the non-production payment sandbox."""

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
from .models import PaymentFacts


def build_manifest() -> DomainPackManifest:
    capability = CapabilityDefinition(
        capability_id=CAPABILITY_ID,
        version=PACK_VERSION,
        domain=PACK_ID,
        display_name="Submit synthetic payment",
        intent_examples=["Submit the synthetic payment draft"],
        input_schema=PaymentFacts.model_json_schema(),
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
        kind=DomainPackKind.SYNTHETIC,
        display_name="Synthetic Payment Sandbox",
        owner="FinRPA Phase 2 test harness",
        capabilities=[capability],
        canonical_fact_schema=PaymentFacts.model_json_schema(),
        state_transitions={"draft": ["submitted"], "submitted": []},
        policy_refs={ACCESS_POLICY_REF, RISK_POLICY_REF},
        result_probe_refs={RESULT_PROBE_REF},
        production_eligible=False,
    )
