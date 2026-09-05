"""Frozen, offline-only protocol records for the Stripe browser benchmark.

This module deliberately contains no Stripe client, browser runner, or synthetic
benchmark code.  It freezes the inputs to a paired experiment and provides a
small adapter/artefact boundary around the legacy quantitative benchmark API.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any, Literal, Self

from pydantic import Field, model_validator

from enterprise.evaluation.quantitative_benchmark import (
    PROTOCOL_CASE_SCHEMA_VERSION,
    Arm,
    BenchmarkCaseResult,
    CaseOpportunity,
    PairedBenchmarkCaseResult,
    QuantitativeBenchmarkReport,
    aggregate_paired_quantitative_benchmark,
)
from enterprise.evaluation.quantitative_benchmark import _StrictModel as _BenchmarkStrictModel

MANIFEST_SCHEMA_VERSION = "agentpact.stripe-browser-benchmark.manifest.v1"
ARTIFACT_SCHEMA_VERSION = "agentpact.stripe-browser-benchmark.artifact.v1"
Digest = str


class ExecutionProfile(_BenchmarkStrictModel):
    """Frozen dimensions shared by all arms of a case."""

    profile_id: str = Field(min_length=1, max_length=128)
    model_version: str = Field(min_length=1, max_length=256)
    browser_runtime_version: str = Field(min_length=1, max_length=256)
    provider_mode: str = Field(min_length=1, max_length=128)
    headless: bool = True
    viewport_width: int = Field(default=1280, ge=1, le=10000)
    viewport_height: int = Field(default=720, ge=1, le=10000)
    locale: str = Field(default="en-US", min_length=2, max_length=32)
    timezone: str = Field(default="UTC", min_length=1, max_length=64)


class RunBudget(_BenchmarkStrictModel):
    """Finite, pre-registered execution limits; never inferred from outcomes."""

    timeout_ms: int = Field(ge=1)
    max_steps: int = Field(ge=1)
    retry_budget: int = Field(default=0, ge=0)
    approval_wait_ms: int = Field(default=0, ge=0)
    max_human_interventions: int = Field(default=0, ge=0)
    concurrency: int = Field(default=1, ge=1)
    repetitions_per_case: int = Field(default=1, ge=1)
    start_window_seconds: int = Field(default=0, ge=0)


class ArmDefinition(_BenchmarkStrictModel):
    arm: Arm
    contract_id: str = Field(min_length=1, max_length=256)


class StripeBenchmarkManifest(_BenchmarkStrictModel):
    """Immutable identity and protocol inputs for one paired benchmark case."""

    schema_version: Literal["agentpact.stripe-browser-benchmark.manifest.v1"] = MANIFEST_SCHEMA_VERSION
    benchmark_version: str = Field(min_length=1, max_length=128)
    pair_id: str = Field(min_length=1, max_length=256)
    case_id: str = Field(min_length=1, max_length=256)
    corpus_version: str = Field(min_length=1, max_length=128)
    pack_id: str = Field(min_length=1, max_length=256)
    pack_version: str = Field(min_length=1, max_length=128)
    seed: int = Field(ge=0)
    profile: ExecutionProfile
    run_budget: RunBudget
    arms: tuple[ArmDefinition, ...]
    case_opportunity: CaseOpportunity
    expected_business_state: str = Field(min_length=1, max_length=512)
    invalid_fairness: bool = False
    environment_fault: bool = False

    @model_validator(mode="after")
    def validate_arms(self) -> Self:
        if tuple(item.arm for item in self.arms) != ("G", "B0", "B1"):
            raise ValueError("manifest arms must contain G, B0, and B1 in order")
        if self.invalid_fairness and self.environment_fault:
            raise ValueError("manifest cannot be both invalid_fairness and environment_fault")
        return self

    @property
    def manifest_digest(self) -> Digest:
        return _digest(self.model_dump(mode="json"))


def _digest(value: Any) -> Digest:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def build_paired_benchmark_case_result(
    manifest: StripeBenchmarkManifest,
    arm: Arm,
    outcome: BenchmarkCaseResult | PairedBenchmarkCaseResult | Mapping[str, Any],
) -> PairedBenchmarkCaseResult:
    """Adapt a legacy case outcome to the frozen manifest identity.

    Identity, expected state, and opportunity are always taken from the
    manifest.  This prevents an arm runner (or a hand-written fixture) from
    silently changing denominators or pairing keys.
    """

    if arm not in ("G", "B0", "B1"):
        raise ValueError(f"unsupported benchmark arm: {arm}")
    if isinstance(outcome, (BenchmarkCaseResult, PairedBenchmarkCaseResult)):
        values = outcome.model_dump(mode="python")
    elif isinstance(outcome, Mapping):
        values = dict(outcome)
    else:
        raise TypeError("outcome must be a BenchmarkCaseResult or mapping")

    expected_identity = {
        "pair_id": manifest.pair_id,
        "case_id": manifest.case_id,
        "corpus_version": manifest.corpus_version,
        "pack_id": manifest.pack_id,
        "pack_version": manifest.pack_version,
        "expected_business_state": manifest.expected_business_state,
        "case_opportunity": manifest.case_opportunity,
    }
    for key, expected in expected_identity.items():
        if key not in values:
            continue
        supplied = values[key]
        if key == "case_opportunity" and isinstance(supplied, Mapping):
            supplied = CaseOpportunity.model_validate(supplied, strict=True)
        if supplied != expected:
            raise ValueError("outcome identity does not match benchmark manifest")

    values.update(
        schema_version=PROTOCOL_CASE_SCHEMA_VERSION,
        pair_id=manifest.pair_id,
        case_id=manifest.case_id,
        corpus_version=manifest.corpus_version,
        pack_id=manifest.pack_id,
        pack_version=manifest.pack_version,
        model_version=manifest.profile.model_version,
        browser_runtime_version=manifest.profile.browser_runtime_version,
        provider_mode=manifest.profile.provider_mode,
        expected_business_state=manifest.expected_business_state,
        arm=arm,
        case_opportunity=manifest.case_opportunity,
        invalid_fairness=manifest.invalid_fairness,
        environment_fault=manifest.environment_fault,
    )
    return PairedBenchmarkCaseResult.model_validate(values, strict=True)


# A concise alias for callers that prefer an adapter-shaped name.
adapt_arm_outcome = build_paired_benchmark_case_result


class OfflineBenchmarkReport(_BenchmarkStrictModel):
    """Self-contained report artefact with digest-backed traceability."""

    schema_version: Literal["agentpact.stripe-browser-benchmark.artifact.v1"] = ARTIFACT_SCHEMA_VERSION
    manifest: StripeBenchmarkManifest
    manifest_digest: Digest
    outcomes: tuple[PairedBenchmarkCaseResult, ...]
    report: QuantitativeBenchmarkReport
    outcome_digest: Digest

    @model_validator(mode="after")
    def validate_traceability(self) -> Self:
        if self.manifest_digest != self.manifest.manifest_digest:
            raise ValueError("manifest_digest does not match manifest")
        expected_outcomes = _ordered_outcomes(self.outcomes)
        if self.outcomes != expected_outcomes:
            raise ValueError("outcomes must be in deterministic arm order")
        if self.outcome_digest != _digest([item.model_dump(mode="json") for item in self.outcomes]):
            raise ValueError("outcome_digest does not match outcomes")
        for outcome in self.outcomes:
            if (
                outcome.pair_id != self.manifest.pair_id
                or outcome.case_id != self.manifest.case_id
                or outcome.corpus_version != self.manifest.corpus_version
                or outcome.case_opportunity != self.manifest.case_opportunity
            ):
                raise ValueError("outcome is not traceable to manifest identity/opportunity")
        expected_report = aggregate_paired_quantitative_benchmark(self.outcomes)
        if self.report != expected_report:
            raise ValueError("report does not match outcomes")
        return self

    def to_json(self) -> str:
        return self.model_dump_json()

    @classmethod
    def from_json(cls, payload: str) -> "OfflineBenchmarkReport":
        return cls.model_validate_json(payload, strict=True)


def build_offline_benchmark_report(
    manifest: StripeBenchmarkManifest,
    outcomes: tuple[PairedBenchmarkCaseResult, ...] | list[PairedBenchmarkCaseResult],
) -> OfflineBenchmarkReport:
    ordered = _ordered_outcomes(outcomes)
    return OfflineBenchmarkReport(
        manifest=manifest,
        manifest_digest=manifest.manifest_digest,
        outcomes=ordered,
        report=aggregate_paired_quantitative_benchmark(ordered),
        outcome_digest=_digest([item.model_dump(mode="json") for item in ordered]),
    )


# Names kept intentionally discoverable for downstream protocol consumers.
BenchmarkManifest = StripeBenchmarkManifest
BenchmarkRunBudget = RunBudget
BenchmarkArmDefinition = ArmDefinition
BenchmarkArmOutcome = BenchmarkCaseResult
StripeArmOutcome = BenchmarkCaseResult
OfflineBenchmarkReportArtifact = OfflineBenchmarkReport

__all__ = [
    "ARTIFACT_SCHEMA_VERSION",
    "MANIFEST_SCHEMA_VERSION",
    "ArmDefinition",
    "BenchmarkArmDefinition",
    "BenchmarkArmOutcome",
    "BenchmarkManifest",
    "BenchmarkRunBudget",
    "ExecutionProfile",
    "OfflineBenchmarkReport",
    "OfflineBenchmarkReportArtifact",
    "RunBudget",
    "StripeBenchmarkManifest",
    "StripeArmOutcome",
    "adapt_arm_outcome",
    "build_offline_benchmark_report",
    "build_paired_benchmark_case_result",
]


def _ordered_outcomes(
    outcomes: tuple[PairedBenchmarkCaseResult, ...] | list[PairedBenchmarkCaseResult],
) -> tuple[PairedBenchmarkCaseResult, ...]:
    arm_order = {"G": 0, "B0": 1, "B1": 2}
    return tuple(sorted(outcomes, key=lambda item: (item.pair_id, item.case_id, arm_order[item.arm])))
