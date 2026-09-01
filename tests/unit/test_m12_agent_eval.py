"""Focused M12 deterministic and live-provider planning tests."""

from __future__ import annotations

import json

import pytest

from tests.support.synthetic_agent_eval import canonical_json, evaluate


def test_recorded_eval_is_byte_identical_and_digest_stable() -> None:
    first = evaluate("recorded")
    second = evaluate("recorded")

    assert first == second
    assert canonical_json(first) == canonical_json(second)
    assert first.passed_case_count == first.case_count
    assert all(item.trusted_compile_result != "rejected" for item in first.cases)


def test_live_eval_fails_closed_without_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("M12_LIVE_KEY", raising=False)
    with pytest.raises(ValueError, match="configuration is incomplete"):
        evaluate(
            "live",
            endpoint="https://provider.invalid/v1",
            model="m12-model",
            api_key_env="M12_LIVE_KEY",
        )


def test_fake_live_eval_is_model_safe_and_planning_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("M12_LIVE_KEY", "credential-canary")
    requests: list[dict[str, object]] = []

    def transport(*, endpoint: str, api_key: str, payload: dict[str, object]) -> object:
        assert endpoint == "https://provider.invalid/v1/chat/completions"
        assert api_key == "credential-canary"
        requests.append(payload)
        name = payload["response_format"]["json_schema"]["name"]  # type: ignore[index]
        if name == "authority_minimized_plan_proposal":
            content = {
                "capability_id": "synthetic.payment.submit",
                "input_slots": ["payment_id", "beneficiary_id", "amount", "currency", "reference", "object_version"],
                "step_roles": ["precheck", "submit", "confirm"],
            }
        else:
            content = {"step_roles": ["submit", "confirm"]}
        return {
            "choices": [{"message": {"content": json.dumps(content)}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
        }

    report = evaluate(
        "live",
        endpoint="https://provider.invalid/v1",
        model="m12-model",
        api_key_env="M12_LIVE_KEY",
        transport=transport,
    )

    assert report.passed_case_count == report.case_count == 2
    assert len(requests) == 2
    encoded_requests = json.dumps(requests, sort_keys=True)
    encoded_report = canonical_json(report).decode()
    for canary in (
        "trusted-eval-payment",
        "trusted-eval-beneficiary",
        "trusted-eval-reference",
        "credential-canary",
        "tenant-synthetic-payment",
    ):
        assert canary not in encoded_requests
        assert canary not in encoded_report
    assert "business_inputs" not in encoded_report
    assert "legal_actions" not in encoded_report
