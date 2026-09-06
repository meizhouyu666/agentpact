from __future__ import annotations

import json

import pytest

from enterprise.evaluation import HardGateViolation
from scripts.stripe_gate2_pair import _manifest, _outcome
from scripts.stripe_gate3_batch import _run_batch, _write_batch_index


def _artifact(pair_id: str, *, environment_fault: bool = False, hard_gate: bool = False):
    manifest = _manifest(pair_id).model_copy(update={"environment_fault": environment_fault})
    outcomes = []
    for arm in ("G", "B0", "B1"):
        confirmed = arm == "G"
        outcome = _outcome(
            manifest,
            actual_business_state=manifest.expected_business_state if confirmed else "NOT_CONFIRMED:unknown",
            final_run_state="SUCCEEDED" if confirmed else "UNKNOWN",
            task_success=confirmed,
            business_state_correct=confirmed,
            action_count=1,
            approval_count=1 if arm == "G" else 0,
            probe_count=1,
            latency_ms=10,
            unknown_stopped=arm == "G",
            unauthorized_effect_count=1 if hard_gate and arm == "B1" else 0,
            hard_gate_violations=(
                (HardGateViolation(code="unauthorized_effect"),)
                if hard_gate and arm == "B1"
                else ()
            ),
        )
        outcome.update(
            pair_id=pair_id,
            arm=arm,
            case_opportunity=manifest.case_opportunity.model_dump(mode="python"),
            invalid_fairness=False,
            environment_fault=environment_fault,
        )
        outcomes.append(outcome)
    return {
        "benchmark_artifact": {
            "manifest": manifest.model_dump(mode="json"),
            "outcomes": outcomes,
        }
    }


@pytest.mark.asyncio
async def test_batch_runs_requested_pairs_without_a_stop_signal(tmp_path) -> None:
    async def runner(pair_id, output):
        artifact = _artifact(pair_id)
        output.write_text(json.dumps(artifact), encoding="utf-8")
        return artifact

    index = await _run_batch(
        batch_id="batch-test",
        output_dir=tmp_path,
        repetitions=3,
        pair_runner=runner,
    )

    assert index["completed_pair_count"] == 3
    assert index["valid_pair_count"] == 3
    assert index["discarded_pair_count"] == 0
    assert index["stopped_early"] is False
    assert index["stop_reason"] is None


@pytest.mark.asyncio
async def test_batch_stops_after_first_hard_gate_violation(tmp_path) -> None:
    calls = 0

    async def runner(pair_id, output):
        nonlocal calls
        calls += 1
        artifact = _artifact(pair_id, hard_gate=True)
        output.write_text(json.dumps(artifact), encoding="utf-8")
        return artifact

    index = await _run_batch(
        batch_id="batch-hard-gate",
        output_dir=tmp_path,
        repetitions=5,
        pair_runner=runner,
    )

    assert calls == 1
    assert index["completed_pair_count"] == 1
    assert index["valid_pair_count"] == 1
    assert index["stopped_early"] is True
    assert index["stop_reason"] == "hard_gate_violation"
    assert index["hard_gate_event_count"] == 1
    assert index["pairs"][0]["hard_gate_violations"] == (
        {"arm": "B1", "code": "unauthorized_effect", "event_count": 1},
    )
    assert index["safety_ledger"] == [
        {
            "pair_id": index["pairs"][0]["pair_id"],
            "artifact": index["pairs"][0]["artifact"],
            "arm": "B1",
            "code": "unauthorized_effect",
            "event_count": 1,
        }
    ]


@pytest.mark.asyncio
async def test_environment_fault_is_discarded_and_stops_batch(tmp_path) -> None:
    async def runner(pair_id, output):
        artifact = _artifact(pair_id, environment_fault=True)
        output.write_text(json.dumps(artifact), encoding="utf-8")
        return artifact

    index = await _run_batch(
        batch_id="batch-environment",
        output_dir=tmp_path,
        repetitions=5,
        pair_runner=runner,
    )

    assert index["completed_pair_count"] == 1
    assert index["valid_pair_count"] == 0
    assert index["discarded_pair_count"] == 1
    assert index["stop_reason"] == "environment_fault"


@pytest.mark.asyncio
async def test_hard_gate_is_primary_even_when_pair_has_environment_fault(tmp_path) -> None:
    async def runner(pair_id, output):
        artifact = _artifact(pair_id, environment_fault=True, hard_gate=True)
        output.write_text(json.dumps(artifact), encoding="utf-8")
        return artifact

    index = await _run_batch(
        batch_id="batch-mixed-stop",
        output_dir=tmp_path,
        repetitions=5,
        pair_runner=runner,
    )

    assert index["valid_pair_count"] == 0
    assert index["discarded_pair_count"] == 1
    assert index["hard_gate_event_count"] == 1
    assert index["stop_reason"] == "hard_gate_violation"


def test_batch_index_rejects_runtime_secrets(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_runtime_secret")

    with pytest.raises(RuntimeError, match="runtime secret"):
        _write_batch_index(tmp_path / "index.json", {"leak": "sk_test_runtime_secret"})

    assert not (tmp_path / "index.json").exists()
