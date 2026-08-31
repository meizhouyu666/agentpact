"""Bridges from browser-loop ports to existing AgentPact governance contracts."""

from __future__ import annotations

from collections.abc import Callable

from pydantic import BaseModel, ConfigDict, Field

from enterprise.governance.result_probes import BusinessResultProbe, ResultProbeStatus

from .contracts import VerificationDisposition, VerificationRequest, VerificationResult


class ResultProbeBinding(BaseModel):
    """Trusted identifiers resolved outside the model/browser observation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    resource_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)


class BusinessResultProbeVerifier:
    """Map a Domain Pack's authoritative business probe into loop verification."""

    def __init__(
        self,
        *,
        probe: BusinessResultProbe,
        resolve_binding: Callable[[VerificationRequest], ResultProbeBinding],
    ) -> None:
        self._probe = probe
        self._resolve_binding = resolve_binding

    async def verify(self, request: VerificationRequest) -> VerificationResult:
        binding = self._resolve_binding(request)
        evidence = self._probe.probe(
            resource_id=binding.resource_id,
            idempotency_key=binding.idempotency_key,
        )
        disposition, reason_code = {
            ResultProbeStatus.CONFIRMED: (
                VerificationDisposition.SUCCEEDED,
                "BUSINESS_RESULT_CONFIRMED",
            ),
            ResultProbeStatus.NOT_CONFIRMED: (
                VerificationDisposition.FAILED,
                "BUSINESS_RESULT_NOT_CONFIRMED",
            ),
            ResultProbeStatus.UNKNOWN: (
                VerificationDisposition.UNKNOWN,
                "BUSINESS_RESULT_UNKNOWN",
            ),
        }[evidence.status]
        refs = [evidence.probe_ref]
        if evidence.facts_hash:
            refs.append(evidence.facts_hash)
        return VerificationResult(
            disposition=disposition,
            reason_code=reason_code,
            evidence_refs=tuple(refs),
        )
