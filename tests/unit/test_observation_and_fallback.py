"""Task 4/5 evidence, profile, and batch policy tests."""

from datetime import datetime, timedelta, timezone

import pytest

from enterprise.governance.classification import DataClassification, ModelEgressPolicy
from enterprise.governance.contracts import ActionIntent, ExecutionEffect
from enterprise.governance.execution_profiles import (
    CUAEngine,
    CUAExecutionEvidence,
    ExecutionMechanism,
    ExecutionProfile,
    ExecutionProfileRejected,
    decide_profile,
    execution_mechanism_is_allowed,
    governed_execution_profile,
    require_allowed_profile,
    require_cua_execution_evidence,
    require_execution_mechanism,
    require_single_state_change,
)
from enterprise.governance.observation_evidence import (
    ArtifactKind,
    EvidenceConsistency,
    FieldControl,
    ObservationArtifact,
    ObservationEvidenceBundle,
    ObservationMode,
    assess_evidence,
)


def test_high_risk_conflicting_evidence_fails_safe_and_sensitive_artifacts_require_redaction():
    bundle = ObservationEvidenceBundle(
        observation_id="obs_1", mode=ObservationMode.HYBRID, consistency=EvidenceConsistency.CONFLICTING,
        artifacts=[ObservationArtifact(artifact_id="dom_1", kind=ArtifactKind.DOM, fields=[FieldControl(field_name="amount", classification=DataClassification.FINANCIAL, retention_days=7)])],
    )
    decision = assess_evidence(effect=ExecutionEffect.EXTERNAL_WRITE, bundle=bundle)
    assert decision.requires_human


def test_disallowed_sensitive_egress_requires_recorded_redaction_evidence():
    artifact = ObservationArtifact(
        artifact_id="dom_1",
        kind=ArtifactKind.DOM,
        fields=[FieldControl(field_name="amount", classification=DataClassification.FINANCIAL, retention_days=7)],
        model_policy=ModelEgressPolicy(model_id="internal", region="cn"),
    )
    bundle = ObservationEvidenceBundle(
        observation_id="obs_1", mode=ObservationMode.DOM_ONLY, consistency=EvidenceConsistency.CONSISTENT, artifacts=[artifact]
    )
    assert assess_evidence(effect=ExecutionEffect.READ, bundle=bundle).requires_human

    artifact.redacted_field_names.add("amount")
    assert assess_evidence(effect=ExecutionEffect.READ, bundle=bundle).allow_automatic_progress


def test_coordinate_fallback_cannot_auto_cross_external_commit_and_batch_has_one_state_change():
    assert not decide_profile(effect=ExecutionEffect.EXTERNAL_WRITE, profile=ExecutionProfile(mechanism=ExecutionMechanism.COORDINATE, fallback_rank=2, evidence_refs=["vision"])).allowed
    intent = ActionIntent(intent_id="i", task_id="t", step_id="s", action_fingerprint="f", observation_id="o", operation="input", effect=ExecutionEffect.INTERNAL_WRITE)
    with pytest.raises(ValueError, match="Re-observation"):
        require_single_state_change([intent, intent.model_copy(update={"intent_id": "i2"})])


def test_cua_evidence_is_fresh_and_bound_to_one_action_observation():
    now = datetime.now(timezone.utc)
    profile = ExecutionProfile(
        mechanism=ExecutionMechanism.CUA_COORDINATE,
        evidence_refs=["screenshot:current"],
    )
    evidence = CUAExecutionEvidence(
        engine=CUAEngine.UI_TARS,
        action_fingerprint="action_fp",
        observation_hash="observation_fp",
        evidence_refs=["screenshot:current"],
        captured_at=now,
    )

    require_cua_execution_evidence(
        profile=profile,
        evidence=evidence,
        action_fingerprint="action_fp",
        observation_hash="observation_fp",
        now=now,
    )
    with pytest.raises(ExecutionProfileRejected, match="does not match"):
        require_cua_execution_evidence(
            profile=profile,
            evidence=evidence,
            action_fingerprint="substituted_action",
            observation_hash="observation_fp",
            now=now,
        )
    with pytest.raises(ExecutionProfileRejected, match="stale"):
        require_cua_execution_evidence(
            profile=profile,
            evidence=evidence.model_copy(update={"captured_at": now - timedelta(seconds=31)}),
            action_fingerprint="action_fp",
            observation_hash="observation_fp",
            now=now,
        )
    with pytest.raises(ExecutionProfileRejected, match="requires fresh engine evidence"):
        require_cua_execution_evidence(
            profile=profile,
            evidence=None,
            action_fingerprint="action_fp",
            observation_hash="observation_fp",
            now=now,
        )


def test_handler_profile_limits_the_fallback_chain_and_resets_context():
    with pytest.raises(ValueError, match="fallback_rank for label must be 1"):
        ExecutionProfile(mechanism=ExecutionMechanism.LABEL, evidence_refs=["dom:button"])

    profile = ExecutionProfile(
        mechanism=ExecutionMechanism.LABEL,
        fallback_rank=1,
        evidence_refs=["dom:button"],
    )

    with governed_execution_profile(profile):
        assert execution_mechanism_is_allowed(ExecutionMechanism.LOCATOR)
        assert execution_mechanism_is_allowed(ExecutionMechanism.LABEL)
        assert not execution_mechanism_is_allowed(ExecutionMechanism.COORDINATE)
        with pytest.raises(ExecutionProfileRejected, match="coordinate exceeds"):
            require_execution_mechanism(ExecutionMechanism.COORDINATE)

    assert execution_mechanism_is_allowed(ExecutionMechanism.JAVASCRIPT)


def test_external_write_profile_is_rejected_before_weak_fallback_is_eligible():
    profile = ExecutionProfile(
        mechanism=ExecutionMechanism.JAVASCRIPT,
        fallback_rank=3,
        evidence_refs=["dom:button", "vision:button"],
    )

    with pytest.raises(ExecutionProfileRejected, match="external commit boundary"):
        require_allowed_profile(effect=ExecutionEffect.EXTERNAL_WRITE, profile=profile)
