"""In-memory synthetic business system and canonical result probe."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
import hashlib
import json

from enterprise.governance.result_probes import ResultProbeEvidence, ResultProbeStatus

from .constants import RESULT_PROBE_REF
from .models import (
    AmbiguousSubmissionFailure,
    DefiniteSubmissionFailure,
    FaultMode,
    PaymentFacts,
    PaymentStatus,
    SyntheticPaymentError,
    SyntheticPaymentRecord,
)


class SyntheticPaymentStore:
    def __init__(self) -> None:
        self._records: dict[str, SyntheticPaymentRecord] = {}
        self._inconclusive_probe_ids: set[str] = set()

    def create_draft(self, *, facts: PaymentFacts, requester_user_id: str) -> SyntheticPaymentRecord:
        if facts.payment_id in self._records:
            raise SyntheticPaymentError("Synthetic payment already exists")
        record = SyntheticPaymentRecord(facts=facts, requester_user_id=requester_user_id)
        self._records[facts.payment_id] = record
        return record.model_copy(deep=True)

    def require(self, payment_id: str) -> SyntheticPaymentRecord:
        try:
            return self._records[payment_id].model_copy(deep=True)
        except KeyError as exc:
            raise SyntheticPaymentError("Synthetic payment does not exist") from exc

    def submit(
        self,
        *,
        payment_id: str,
        expected_version: int,
        approval_id: str,
        idempotency_key: str,
        fault_mode: FaultMode = FaultMode.NONE,
    ) -> SyntheticPaymentRecord:
        record = self._records.get(payment_id)
        if record is None:
            raise SyntheticPaymentError("Synthetic payment does not exist")
        if record.idempotency_key == idempotency_key:
            return record.model_copy(deep=True)
        if record.status is not PaymentStatus.DRAFT:
            raise SyntheticPaymentError("Synthetic payment is no longer a draft")
        if record.facts.object_version != expected_version:
            raise SyntheticPaymentError("Synthetic payment object version changed")
        if fault_mode is FaultMode.FAIL_BEFORE_COMMIT:
            raise DefiniteSubmissionFailure("Synthetic failure occurred before commit")

        record.status = PaymentStatus.SUBMITTED
        record.facts = record.facts.model_copy(update={"object_version": expected_version + 1})
        record.approval_id = approval_id
        record.idempotency_key = idempotency_key
        record.confirmation_reference = f"SYN-{payment_id}-{record.facts.object_version}"
        record.commit_count += 1
        if fault_mode is FaultMode.COMMIT_THEN_INCONCLUSIVE:
            self._inconclusive_probe_ids.add(payment_id)
            raise AmbiguousSubmissionFailure("Synthetic commit response and result probe are unavailable")
        if fault_mode is FaultMode.COMMIT_THEN_TIMEOUT:
            raise AmbiguousSubmissionFailure("Synthetic commit completed before the response timed out")
        return record.model_copy(deep=True)

    def probe_is_inconclusive(self, payment_id: str) -> bool:
        return payment_id in self._inconclusive_probe_ids

    def clear_probe_fault(self, payment_id: str) -> None:
        self._inconclusive_probe_ids.discard(payment_id)


class SyntheticPaymentResultProbe:
    def __init__(
        self,
        store: SyntheticPaymentStore,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def probe(self, *, resource_id: str, idempotency_key: str) -> ResultProbeEvidence:
        checked_at = self._clock()
        if self._store.probe_is_inconclusive(resource_id):
            return ResultProbeEvidence(
                probe_ref=RESULT_PROBE_REF,
                status=ResultProbeStatus.UNKNOWN,
                resource_id=resource_id,
                checked_at=checked_at,
                reasons=["Synthetic canonical state is temporarily unavailable"],
            )
        try:
            record = self._store.require(resource_id)
        except SyntheticPaymentError:
            return ResultProbeEvidence(
                probe_ref=RESULT_PROBE_REF,
                status=ResultProbeStatus.NOT_CONFIRMED,
                resource_id=resource_id,
                checked_at=checked_at,
                reasons=["Synthetic payment does not exist"],
            )

        facts_hash = _facts_hash(record.facts)
        if record.status is PaymentStatus.DRAFT:
            status = ResultProbeStatus.NOT_CONFIRMED
            reasons = ["Synthetic payment remains in draft state"]
        elif record.idempotency_key == idempotency_key:
            status = ResultProbeStatus.CONFIRMED
            reasons = ["Canonical payment state and idempotency key confirm submission"]
        else:
            status = ResultProbeStatus.UNKNOWN
            reasons = ["Payment was submitted by a different idempotency key"]
        return ResultProbeEvidence(
            probe_ref=RESULT_PROBE_REF,
            status=status,
            resource_id=resource_id,
            checked_at=checked_at,
            observed_version=record.facts.object_version,
            business_reference=record.confirmation_reference,
            facts_hash=facts_hash,
            reasons=reasons,
            metadata={"status": record.status.value},
        )


def _facts_hash(facts: PaymentFacts) -> str:
    canonical = json.dumps(facts.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
