"""Offline SDK manifest for the Stripe Payment (test-mode) pack candidate.

This module is permanently contract-catalog-only, exactly like the synthetic
reference: it must never import runtime roots (aiohttp, httpx, fastapi,
playwright, sqlalchemy, ...) and is never used as an execution entry point.
The conformance test suite enforces both properties.
"""

from enterprise.governance.capabilities import AuthorizationDimension
from enterprise.governance.domain_pack_contracts import (
    ContractOwnerReference,
    ContractOwnerRole,
)
from enterprise.governance.pack_sdk import (
    PackCanonicalFact,
    PackCapability,
    PackContractKind,
    PackDataClassification,
    PackEffectClass,
    PackEvidenceKind,
    PackEvidenceRequirement,
    PackLifecycle,
    PackLifecycleTransition,
    PackSdkManifest,
)

from .constants import (
    AUTHORITATIVE_READ_EVIDENCE_ID,
    AUTHORITATIVE_SOURCE_REF,
    CAPABILITY_ID,
    PACK_DISPLAY_NAME,
    PACK_ID,
    PACK_VERSION,
    READ_CAPABILITY_ID,
    RESULT_PROBE_REF,
    RESULT_PROBE_SCHEMA_REF,
    RISK_POLICY_REF,
)
from .models import StripePaymentFacts, StripePaymentStatus

__all__ = ["build_pack_sdk_manifest"]

_LIFECYCLE_ID = "stripe.payment.lifecycle.v1"
_SUBMIT_TRANSITION_ID = "stripe.payment.submit.transition.v1"
_FACT_FIELD_NAMES = (*StripePaymentFacts.model_fields, "status")


def build_pack_sdk_manifest() -> PackSdkManifest:
    """Build the immutable, offline-only Stripe test-mode contract."""

    fact_ids = tuple(f"{PACK_ID}.{field_name}" for field_name in _FACT_FIELD_NAMES)
    canonical_facts = tuple(
        PackCanonicalFact(
            fact_id=fact_id,
            schema_ref=f"stripe.payment.facts/{field_name}/v1",
            data_classification=PackDataClassification.INTERNAL,
            source_ref=AUTHORITATIVE_SOURCE_REF,
            evidence_requirement_id=AUTHORITATIVE_READ_EVIDENCE_ID,
        )
        for field_name, fact_id in zip(_FACT_FIELD_NAMES, fact_ids, strict=True)
    )
    evidence_requirements = (
        PackEvidenceRequirement(
            evidence_id=AUTHORITATIVE_READ_EVIDENCE_ID,
            kind=PackEvidenceKind.AUTHORITATIVE_READ,
            source_schema_ref=AUTHORITATIVE_SOURCE_REF,
            maximum_age_seconds=300,
        ),
        PackEvidenceRequirement(
            evidence_id=RESULT_PROBE_REF,
            kind=PackEvidenceKind.RESULT_PROBE,
            source_schema_ref=RESULT_PROBE_SCHEMA_REF,
            maximum_age_seconds=60,
        ),
    )
    lifecycle = PackLifecycle(
        lifecycle_id=_LIFECYCLE_ID,
        states=(StripePaymentStatus.DRAFT.value, StripePaymentStatus.SUBMITTED.value),
        terminal_states=(StripePaymentStatus.SUBMITTED.value,),
        transitions=(
            PackLifecycleTransition(
                transition_id=_SUBMIT_TRANSITION_ID,
                source_state=StripePaymentStatus.DRAFT.value,
                target_state=StripePaymentStatus.SUBMITTED.value,
            ),
        ),
    )
    capabilities = (
        PackCapability(
            capability_id=READ_CAPABILITY_ID,
            pack_version=PACK_VERSION,
            display_name="Read a Stripe test-mode payment",
            effect_class=PackEffectClass.READ_ONLY,
            authorization_dimensions=(
                AuthorizationDimension.DISCOVER,
                AuthorizationDimension.READ_RECORD,
            ),
            canonical_fact_ids=fact_ids,
            evidence_requirement_ids=(AUTHORITATIVE_READ_EVIDENCE_ID,),
        ),
        PackCapability(
            capability_id=CAPABILITY_ID,
            pack_version=PACK_VERSION,
            display_name="Submit a Stripe test-mode payment",
            effect_class=PackEffectClass.EXTERNAL_WRITE,
            authorization_dimensions=(
                AuthorizationDimension.DISCOVER,
                AuthorizationDimension.READ_RECORD,
                AuthorizationDimension.REQUEST_TRANSITION,
                AuthorizationDimension.EXECUTE_TRANSITION,
                AuthorizationDimension.REQUEST_APPROVAL,
                AuthorizationDimension.ADJUDICATE_APPROVAL,
            ),
            canonical_fact_ids=fact_ids,
            evidence_requirement_ids=(
                AUTHORITATIVE_READ_EVIDENCE_ID,
                RESULT_PROBE_REF,
            ),
            lifecycle_transition_id=_SUBMIT_TRANSITION_ID,
            result_evidence_id=RESULT_PROBE_REF,
            approval_policy_ref=RISK_POLICY_REF,
        ),
    )
    return PackSdkManifest(
        pack_id=PACK_ID,
        pack_version=PACK_VERSION,
        kind=PackContractKind.EXTERNAL_CANDIDATE,
        display_name=PACK_DISPLAY_NAME,
        owner_refs=tuple(
            # TODO(P3): an adopting tenant must replace these placeholder
            # owner refs with real accountable owners before installation.
            ContractOwnerReference(
                role=role,
                owner_ref=f"stripe-owner:{role.value}",
            )
            for role in ContractOwnerRole
        ),
        freshness_ceiling_seconds=300,
        canonical_facts=canonical_facts,
        evidence_requirements=evidence_requirements,
        lifecycle=lifecycle,
        capabilities=capabilities,
    )
