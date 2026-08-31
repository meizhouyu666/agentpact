"""Bridges from browser-loop ports to existing AgentPact governance contracts."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from enterprise.governance.models import GovernanceAuditEventModel
from enterprise.governance.result_probes import BusinessResultProbe, ResultProbeStatus

from .contracts import BrowserLoopEvent, VerificationDisposition, VerificationRequest, VerificationResult


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


class SqlAlchemyBrowserLoopEventSink:
    """Persist already-redacted loop events in the governance audit journal."""

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[Any]],
        *,
        organization_id: str,
        contract_id: str,
        mode: str = "audit",
        policy_version: str | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._organization_id = organization_id
        self._contract_id = contract_id
        self._mode = mode
        self._policy_version = policy_version

    async def emit(self, event: BrowserLoopEvent) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                session.add(
                    GovernanceAuditEventModel(
                        task_id=event.task_id,
                        step_id=event.step_id,
                        contract_id=self._contract_id,
                        organization_id=self._organization_id,
                        event_type=f"browser.loop.{event.stage}",
                        mode=self._mode,
                        action_fingerprint=event.action_fingerprint,
                        observation_hash=event.observation_id,
                        policy_version=self._policy_version,
                        payload=event.model_dump(mode="json"),
                        created_at=event.occurred_at,
                    )
                )
