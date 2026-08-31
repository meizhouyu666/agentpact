"""Tests for bridges to existing AgentPact governance contracts."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from enterprise.browser_loop.contracts import (
    ActionDecision,
    BrowserLoopRunContext,
    BrowserObservation,
    DecisionKind,
    DecisionSource,
    VerificationDisposition,
    VerificationRequest,
)
from enterprise.browser_loop.integrations import BusinessResultProbeVerifier, ResultProbeBinding
from enterprise.governance.result_probes import ResultProbeEvidence, ResultProbeStatus


class Probe:
    def __init__(self, status: ResultProbeStatus) -> None:
        self.status = status
        self.calls = []

    def probe(self, *, resource_id: str, idempotency_key: str) -> ResultProbeEvidence:
        self.calls.append((resource_id, idempotency_key))
        return ResultProbeEvidence(
            probe_ref="enterprise.work.result-probe.v1",
            status=self.status,
            resource_id=resource_id,
            checked_at=datetime(2026, 8, 31, tzinfo=timezone.utc),
            facts_hash="c" * 64,
        )


def _request() -> VerificationRequest:
    now = datetime(2026, 8, 31, tzinfo=timezone.utc)
    observation = BrowserObservation(
        observation_id="a" * 64,
        snapshot_hash="b" * 64,
        sequence=1,
        url="https://enterprise.example.test/result",
        model_dom="result",
        captured_at=now,
    )
    return VerificationRequest(
        run=BrowserLoopRunContext(
            run_id="run-probe",
            task_id="task-probe",
            step_id="step-probe",
            goal="Verify the governed work item",
        ),
        before=observation,
        after=observation,
        decision=ActionDecision(
            kind=DecisionKind.SUCCESS,
            observation_id=observation.observation_id,
            reason_code="SUCCESS_CLAIM",
        ),
        source=DecisionSource.MODEL,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("probe_status", "expected"),
    [
        (ResultProbeStatus.CONFIRMED, VerificationDisposition.SUCCEEDED),
        (ResultProbeStatus.NOT_CONFIRMED, VerificationDisposition.FAILED),
        (ResultProbeStatus.UNKNOWN, VerificationDisposition.UNKNOWN),
    ],
)
async def test_business_result_probe_status_is_authoritative(
    probe_status: ResultProbeStatus,
    expected: VerificationDisposition,
) -> None:
    probe = Probe(probe_status)
    verifier = BusinessResultProbeVerifier(
        probe=probe,
        resolve_binding=lambda _request: ResultProbeBinding(
            resource_id="resource-001",
            idempotency_key="idem-001",
        ),
    )

    result = await verifier.verify(_request())

    assert result.disposition is expected
    assert result.evidence_refs == ("enterprise.work.result-probe.v1", "c" * 64)
    assert probe.calls == [("resource-001", "idem-001")]
