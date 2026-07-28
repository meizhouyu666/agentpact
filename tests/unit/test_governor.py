"""Tests for one-observation governance planning."""

import pytest

from enterprise.governance.governor import GovernanceBatchError, build_governance_batch_plan


class FakeAction:
    def __init__(self, element_id: str):
        self.element_id = element_id

    def model_dump(self, **_kwargs):
        return {"action_type": "click", "element_id": self.element_id}


def _build(actions, elements):
    return build_governance_batch_plan(
        task_id="task_1",
        step_id="step_1",
        actions=actions,
        page_url="https://bank.example/confirm",
        page_html="<button>Confirm</button>",
        element_lookup=elements,
        hmac_secret="test-key",
    )


def test_governor_uses_one_shared_observation_for_all_candidates():
    plan = _build(
        [FakeAction("read"), FakeAction("pay")],
        {
            "read": {"text": "View account balance", "attributes": {}},
            "pay": {"text": "Confirm transfer", "attributes": {}},
        },
    )

    assert len(plan.candidates) == 2
    assert {candidate.intent.observation_id for candidate in plan.candidates} == {plan.observation.observation_id}
    assert plan.candidates[1].intent.operation == "payment"


def test_governor_rejects_two_external_writes_from_one_observation():
    with pytest.raises(GovernanceBatchError, match="multiple external writes"):
        _build(
            [FakeAction("transfer"), FakeAction("delete")],
            {
                "transfer": {"text": "Confirm transfer", "attributes": {}},
                "delete": {"text": "Delete beneficiary", "attributes": {}},
            },
        )


def test_governor_requires_hmac_secret():
    with pytest.raises(GovernanceBatchError, match="HMAC"):
        build_governance_batch_plan(
            task_id="task_1",
            step_id="step_1",
            actions=[],
            page_url="https://bank.example",
            page_html="<html></html>",
            element_lookup=None,
            hmac_secret=None,
        )
