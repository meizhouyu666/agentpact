"""Read-only tests for audit replay and completeness reporting."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from enterprise.governance.audit import AuditCandidatePayload, CandidateEvidenceRefs
from enterprise.governance.audit_reporting import list_audit_replay_events, summarize_audit_completeness


def _payload(observation_hash: str = "observation-hmac") -> dict:
    return AuditCandidatePayload(
        candidate_action={"action_type": "click", "element_id": "element_1"},
        evidence_refs=CandidateEvidenceRefs(observation_hash=observation_hash),
    ).model_dump(mode="json")


class _ScalarResult:
    def __init__(self, models):
        self._models = models

    def all(self):
        return self._models


class _ReadOnlySession:
    def __init__(self, models):
        self.models = models
        self.statement = None

    async def scalars(self, statement):
        self.statement = statement
        return _ScalarResult(self.models)


def _event(**overrides):
    values = {
        "event_id": "gae_1",
        "task_id": "task_1",
        "step_id": "step_1",
        "organization_id": "org_1",
        "action_fingerprint": "action-hmac",
        "observation_hash": "observation-hmac",
        "created_at": datetime(2026, 7, 22, tzinfo=timezone.utc),
        "payload": _payload(),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_audit_replay_query_returns_only_versioned_redacted_payloads():
    session = _ReadOnlySession([_event()])

    page = asyncio.run(
        list_audit_replay_events(
            db_session=session,
            organization_id="org_1",
            task_id="task_1",
            step_id="step_1",
            limit=5,
        )
    )

    assert len(page.events) == 1
    assert page.invalid_event_ids == []
    assert page.replayable_event_count == 1
    assert page.scanned_event_count == 1
    assert page.events[0].payload.schema_version == "phase2-audit-candidate-v1"
    assert page.events[0].payload.candidate_action == {"action_type": "click", "element_id": "element_1"}
    compiled = str(session.statement.compile(compile_kwargs={"literal_binds": True}))
    assert "action_candidate" in compiled
    assert "audit" in compiled
    assert "org_1" in compiled
    assert "task_1" in compiled
    assert "step_1" in compiled


def test_audit_replay_skips_invalid_history_and_returns_only_opaque_event_identifier():
    session = _ReadOnlySession(
        [
            _event(),
            _event(event_id="gae_invalid", payload={"schema_version": "unexpected"}),
        ]
    )

    page = asyncio.run(list_audit_replay_events(db_session=session, organization_id="org_1"))

    assert [event.event_id for event in page.events] == ["gae_1"]
    assert page.invalid_event_ids == ["gae_invalid"]
    assert page.invalid_payload_count == 1
    assert page.replayable_event_count == 1
    assert page.scanned_event_count == 2


def test_audit_replay_rejects_rows_detached_from_their_observation_or_action_fingerprint():
    session = _ReadOnlySession(
        [
            _event(event_id="gae_observation_drift", observation_hash="other-observation"),
            _event(event_id="gae_missing_action", action_fingerprint=None),
        ]
    )

    page = asyncio.run(list_audit_replay_events(db_session=session, organization_id="org_1"))

    assert page.events == []
    assert page.invalid_event_ids == ["gae_observation_drift", "gae_missing_action"]
    assert page.invalid_payload_count == 2


def test_audit_replay_rejects_non_positive_limit_without_touching_session():
    session = _ReadOnlySession([])

    with pytest.raises(ValueError, match="between 1 and 1000"):
        asyncio.run(list_audit_replay_events(db_session=session, organization_id="org_1", limit=0))

    assert session.statement is None


def test_audit_replay_rejects_unbounded_page_size_without_touching_session():
    session = _ReadOnlySession([])

    with pytest.raises(ValueError, match="between 1 and 1000"):
        asyncio.run(list_audit_replay_events(db_session=session, organization_id="org_1", limit=1001))

    assert session.statement is None


def test_audit_completeness_counts_only_parseable_replay_payloads_and_write_failures():
    metrics = summarize_audit_completeness(
        [
            _event(observation_hash="observation-a", payload=_payload("observation-a")),
            _event(
                event_id="gae_2",
                observation_hash="observation-a",
                payload=_payload("observation-a"),
            ),
            _event(event_id="gae_3", payload={"schema_version": "unexpected"}),
        ],
        write_failure_events=1,
    )

    assert metrics.total_events == 3
    assert metrics.replayable_events == 2
    assert metrics.invalid_payload_events == 1
    assert metrics.write_failure_events == 1
    assert metrics.distinct_observations == 1
    assert metrics.replay_completeness_rate == pytest.approx(2 / 4)


def test_audit_completeness_excludes_detached_or_incomplete_evidence_rows():
    metrics = summarize_audit_completeness(
        [
            _event(),
            _event(event_id="gae_observation_drift", observation_hash="other-observation"),
            _event(event_id="gae_missing_action", action_fingerprint=None),
        ]
    )

    assert metrics.total_events == 3
    assert metrics.replayable_events == 1
    assert metrics.invalid_payload_events == 2
    assert metrics.distinct_observations == 1


def test_audit_completeness_has_no_business_or_policy_output():
    metrics = summarize_audit_completeness([])

    assert metrics.model_dump() == {
        "total_events": 0,
        "replayable_events": 0,
        "invalid_payload_events": 0,
        "write_failure_events": 0,
        "distinct_observations": 0,
    }
    assert metrics.replay_completeness_rate == 0.0


def test_audit_completeness_rejects_negative_external_failure_count():
    with pytest.raises(ValueError, match="cannot be negative"):
        summarize_audit_completeness([], write_failure_events=-1)
