"""Offline M2 SDK manifest for the non-production synthetic payment reference."""

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
    CAPABILITY_ID,
    PACK_ID,
    PACK_VERSION,
    RESULT_PROBE_REF,
    RISK_POLICY_REF,
)
from .models import PaymentFacts, PaymentStatus

__all__ = ["build_pack_sdk_manifest"]

_AUTHORITATIVE_READ_EVIDENCE_ID = "synthetic.payment.authoritative-read.v1"
_AUTHORITATIVE_SOURCE_REF = "synthetic.payment.store/v1"
_LIFECYCLE_ID = "synthetic.payment.lifecycle.v1"
_READ_CAPABILITY_ID = "synthetic.payment.read"
_SUBMIT_TRANSITION_ID = "synthetic.payment.submit.transition.v1"
_FACT_FIELD_NAMES = (*PaymentFacts.model_fields, "status")


def build_pack_sdk_manifest() -> PackSdkManifest:
    """Build the immutable, offline-only normative synthetic reference contract."""

    fact_ids = tuple(f"{PACK_ID}.{field_name}" for field_name in _FACT_FIELD_NAMES)
    canonical_facts = tuple(
        PackCanonicalFact(
            fact_id=fact_id,
            schema_ref=f"synthetic.payment.facts/{field_name}/v1",
            data_classification=PackDataClassification.INTERNAL,
            source_ref=_AUTHORITATIVE_SOURCE_REF,
            evidence_requirement_id=_AUTHORITATIVE_READ_EVIDENCE_ID,
        )
        for field_name, fact_id in zip(_FACT_FIELD_NAMES, fact_ids, strict=True)
    )
    evidence_requirements = (
        PackEvidenceRequirement(
            evidence_id=_AUTHORITATIVE_READ_EVIDENCE_ID,
            kind=PackEvidenceKind.AUTHORITATIVE_READ,
            source_schema_ref=_AUTHORITATIVE_SOURCE_REF,
            maximum_age_seconds=300,
        ),
        PackEvidenceRequirement(
            evidence_id=RESULT_PROBE_REF,
            kind=PackEvidenceKind.RESULT_PROBE,
            source_schema_ref="synthetic.payment.result-probe/v1",
            maximum_age_seconds=60,
        ),
    )
    lifecycle = PackLifecycle(
        lifecycle_id=_LIFECYCLE_ID,
        states=(PaymentStatus.DRAFT.value, PaymentStatus.SUBMITTED.value),
        terminal_states=(PaymentStatus.SUBMITTED.value,),
        transitions=(
            PackLifecycleTransition(
                transition_id=_SUBMIT_TRANSITION_ID,
                source_state=PaymentStatus.DRAFT.value,
                target_state=PaymentStatus.SUBMITTED.value,
            ),
        ),
    )
    capabilities = (
        PackCapability(
            capability_id=_READ_CAPABILITY_ID,
            pack_version=PACK_VERSION,
            display_name="Read synthetic payment",
            effect_class=PackEffectClass.READ_ONLY,
            authorization_dimensions=(
                AuthorizationDimension.DISCOVER,
                AuthorizationDimension.READ_RECORD,
            ),
            canonical_fact_ids=fact_ids,
            evidence_requirement_ids=(_AUTHORITATIVE_READ_EVIDENCE_ID,),
        ),
        PackCapability(
            capability_id=CAPABILITY_ID,
            pack_version=PACK_VERSION,
            display_name="Submit synthetic payment",
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
                _AUTHORITATIVE_READ_EVIDENCE_ID,
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
        kind=PackContractKind.SYNTHETIC_REFERENCE,
        display_name="Synthetic Payment Reference Pack",
        owner_refs=tuple(
            ContractOwnerReference(
                role=role,
                owner_ref=f"synthetic-owner:{role.value}",
            )
            for role in ContractOwnerRole
        ),
        freshness_ceiling_seconds=300,
        canonical_facts=canonical_facts,
        evidence_requirements=evidence_requirements,
        lifecycle=lifecycle,
        capabilities=capabilities,
    )
