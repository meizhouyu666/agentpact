"""Simulated Stripe test backend and its authoritative result probe.

This is the recorded-mode counterpart of the live ``StripeApiResultProbe``:
the browser acts on a checkout page, and this store plays the role of the
Stripe API that decides the PaymentIntent outcome and answers authoritative
reads. Fault injection produces exactly the ambiguity the governance chain
exists for: a commit that happened but whose response timed out (UNKNOWN),
or an inconclusive authoritative read (UNKNOWN). Replay protection lives in
the harness; this store only guarantees idempotent commits and version-safe
state transitions.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import datetime, timezone
from enum import StrEnum

from enterprise.governance.result_probes import ResultProbeEvidence, ResultProbeStatus

from .constants import RESULT_PROBE_REF
from .models import (
    AmbiguousSubmissionFailure,
    DefiniteSubmissionFailure,
    StripeOutcome,
    StripePaymentError,
    StripePaymentFacts,
    StripePaymentRecord,
    StripePaymentStatus,
)


class StripeFaultMode(StrEnum):
    NONE = "none"
    FAIL_BEFORE_COMMIT = "fail_before_commit"
    COMMIT_THEN_TIMEOUT = "commit_then_timeout"
    COMMIT_THEN_INCONCLUSIVE = "commit_then_inconclusive"


class StripePaymentStore:
    def __init__(self) -> None:
        self._records: dict[str, StripePaymentRecord] = {}
        self._outcomes: dict[str, StripeOutcome] = {}
        self._inconclusive_probe_ids: set[str] = set()

    def create_draft(self, *, facts: StripePaymentFacts, requester_user_id: str) -> StripePaymentRecord:
        if facts.payment_intent_id in self._records:
            raise StripePaymentError("Stripe PaymentIntent already exists")
        record = StripePaymentRecord(facts=facts, requester_user_id=requester_user_id)
        self._records[facts.payment_intent_id] = record
        return record.model_copy(deep=True)

    def require(self, payment_intent_id: str) -> StripePaymentRecord:
        try:
            return self._records[payment_intent_id].model_copy(deep=True)
        except KeyError as exc:
            raise StripePaymentError("Stripe PaymentIntent does not exist") from exc

    def submit(
        self,
        *,
        payment_intent_id: str,
        expected_version: int,
        approval_id: str,
        idempotency_key: str,
        outcome: StripeOutcome = StripeOutcome.SUCCEEDED,
        fault_mode: StripeFaultMode = StripeFaultMode.NONE,
    ) -> StripePaymentRecord:
        record = self._records.get(payment_intent_id)
        if record is None:
            raise StripePaymentError("Stripe PaymentIntent does not exist")
        if record.idempotency_key == idempotency_key:
            return record.model_copy(deep=True)
        if record.status is not StripePaymentStatus.DRAFT:
            raise StripePaymentError("Stripe PaymentIntent is no longer a draft")
        if record.facts.object_version != expected_version:
            raise StripePaymentError("Stripe PaymentIntent object version changed")
        if fault_mode is StripeFaultMode.FAIL_BEFORE_COMMIT:
            raise DefiniteSubmissionFailure("Stripe failure occurred before commit")

        record.status = StripePaymentStatus.SUBMITTED
        record.facts = record.facts.model_copy(update={"object_version": expected_version + 1})
        record.approval_id = approval_id
        record.idempotency_key = idempotency_key
        record.confirmation_reference = f"pi_confirm_{payment_intent_id}_{record.facts.object_version}"
        record.commit_count += 1
        self._outcomes[payment_intent_id] = outcome
        if fault_mode is StripeFaultMode.COMMIT_THEN_INCONCLUSIVE:
            self._inconclusive_probe_ids.add(payment_intent_id)
            raise AmbiguousSubmissionFailure("Stripe commit response and result probe are unavailable")
        if fault_mode is StripeFaultMode.COMMIT_THEN_TIMEOUT:
            raise AmbiguousSubmissionFailure("Stripe commit completed before the response timed out")
        return record.model_copy(deep=True)

    def outcome(self, payment_intent_id: str) -> StripeOutcome:
        return self._outcomes.get(payment_intent_id, StripeOutcome.SUCCEEDED)

    def probe_is_inconclusive(self, payment_intent_id: str) -> bool:
        return payment_intent_id in self._inconclusive_probe_ids

    def clear_probe_fault(self, payment_intent_id: str) -> None:
        self._inconclusive_probe_ids.discard(payment_intent_id)


class StripePaymentResultProbe:
    """Recorded-mode authoritative probe over the simulated Stripe backend.

    Status mapping is identical to the live probe: ``succeeded`` -> CONFIRMED,
    ``processing`` -> UNKNOWN, ``canceled`` -> NOT_CONFIRMED, missing resource
    -> NOT_CONFIRMED, unavailable authoritative read -> UNKNOWN.
    """

    def __init__(
        self,
        store: StripePaymentStore,
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
                reasons=["Stripe authoritative state is temporarily unavailable"],
            )
        try:
            record = self._store.require(resource_id)
        except StripePaymentError:
            return ResultProbeEvidence(
                probe_ref=RESULT_PROBE_REF,
                status=ResultProbeStatus.NOT_CONFIRMED,
                resource_id=resource_id,
                checked_at=checked_at,
                reasons=["Stripe PaymentIntent does not exist"],
            )

        facts_hash = _facts_hash(record.facts)
        if record.status is StripePaymentStatus.DRAFT:
            status = ResultProbeStatus.NOT_CONFIRMED
            reasons = ["Stripe PaymentIntent remains in draft state"]
        elif record.idempotency_key != idempotency_key:
            status = ResultProbeStatus.UNKNOWN
            reasons = ["Payment was submitted by a different idempotency key"]
        else:
            outcome = self._store.outcome(resource_id)
            if outcome is StripeOutcome.SUCCEEDED:
                status = ResultProbeStatus.CONFIRMED
                reasons = ["Authoritative PaymentIntent state confirms the submission"]
            elif outcome is StripeOutcome.CANCELED:
                status = ResultProbeStatus.NOT_CONFIRMED
                reasons = ["Authoritative PaymentIntent state is canceled"]
            else:
                status = ResultProbeStatus.UNKNOWN
                reasons = ["PaymentIntent is still processing"]
        return ResultProbeEvidence(
            probe_ref=RESULT_PROBE_REF,
            status=status,
            resource_id=resource_id,
            checked_at=checked_at,
            observed_version=record.facts.object_version,
            business_reference=record.confirmation_reference,
            facts_hash=facts_hash,
            reasons=reasons,
            metadata={"status": record.status.value, "outcome": self._store.outcome(resource_id).value},
        )


def _facts_hash(facts: StripePaymentFacts) -> str:
    canonical = json.dumps(facts.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
