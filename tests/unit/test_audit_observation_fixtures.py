"""Synthetic audit-capture fixtures without policy or execution decisions."""

import asyncio
import json
from pathlib import Path

from enterprise.governance.audit import record_action_candidates


class ScenarioAction:
    def __init__(self, payload):
        self.element_id = payload.get("element_id")
        self.payload = payload

    def model_dump(self, **_kwargs):
        return self.payload


class FakeSession:
    def __init__(self):
        self.entries = []
        self.committed = False

    def add(self, entry):
        self.entries.append(entry)

    async def commit(self):
        self.committed = True

    async def rollback(self):
        raise AssertionError("audit fixture should not roll back")


SCENARIOS = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "audit_observation_scenarios.json").read_text(encoding="utf-8")
)


def test_audit_observation_fixtures_record_only_redacted_candidates_and_evidence_refs():
    for scenario in SCENARIOS:
        session = FakeSession()
        asyncio.run(
            record_action_candidates(
                db_session=session,
                task_id="synthetic_task",
                step_id=scenario["id"],
                organization_id="synthetic_org",
                actions=[ScenarioAction(action) for action in scenario["actions"]],
                page_url=scenario["page_url"],
                page_html=scenario["page_html"],
                mode="audit",
                hmac_secret="fixture-audit-key",
                element_lookup=scenario["elements"],
                screenshots=[marker.encode("utf-8") for marker in scenario["screenshot_markers"]],
            )
        )

        assert session.committed
        assert len(session.entries) == scenario["expected_candidate_count"]
        for event in session.entries:
            payload = event.payload
            assert payload["schema_version"] == "phase2-audit-candidate-v1"
            assert payload["evidence_refs"]["observation_hash"] == event.observation_hash
            assert payload["evidence_refs"]["screenshot_fingerprints"]
            assert "intent" not in payload
            assert "proposed_policy_decision" not in payload
            assert scenario["page_html"] not in str(payload)

    overlay_payload = next(
        event.payload for event in session.entries if event.step_id == "same_observation_multiple_candidates"
    )
    assert overlay_payload["evidence_refs"]["element_fingerprint"]
