from __future__ import annotations

import json

import pytest

from enterprise.evaluation import HardGateViolation
from scripts.stripe_gate2_pair import _manifest, _outcome
from scripts.stripe_gate3_batch import (
    BASELINE_CONTINUATION_POLICY_ID,
    STRICT_STOP_POLICY_ID,
    _run_batch,
    _write_batch_index,
    main,
)


def _artifact(
    pair_id: str,
    *,
    environment_fault: bool = False,
    invalid_fairness: bool = False,
    hard_gate: tuple[str, str] | None = None,
):
    manifest = _manifest(pair_id).model_copy(
        update={
            "environment_fault": environment_fault,
            "invalid_fairness": invalid_fairness,
        }
    )
    outcomes = []
    for arm in ("G", "B0", "B1"):
        confirmed = arm == "G"
        violation = HardGateViolation(code=hard_gate[1]) if hard_gate and arm == hard_gate[0] else None
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
            unauthorized_effect_count=(
                1 if violation and violation.code == "unauthorized_effect" else 0
            ),
            hard_gate_violations=(violation,) if violation else (),
        )
        outcome.update(
            pair_id=pair_id,
            arm=arm,
            case_opportunity=manifest.case_opportunity.model_dump(mode="python"),
            invalid_fairness=invalid_fairness,
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
    assert index["stop_policy"] == {
        "policy_id": STRICT_STOP_POLICY_ID,
        "stripe_mode": "test",
        "continue_after": [],
        "violations_remain_hard_gate_failures": True,
        "violations_preserved_in_safety_ledger": True,
    }


@pytest.mark.asyncio
async def test_batch_stops_after_first_hard_gate_violation(tmp_path) -> None:
    calls = 0

    async def runner(pair_id, output):
        nonlocal calls
        calls += 1
        artifact = _artifact(pair_id, hard_gate=("B1", "unauthorized_effect"))
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
async def test_explicit_test_mode_policy_continues_only_b1_unauthorized_effects(tmp_path) -> None:
    calls = 0

    async def runner(pair_id, output):
        nonlocal calls
        calls += 1
        artifact = _artifact(pair_id, hard_gate=("B1", "unauthorized_effect"))
        output.write_text(json.dumps(artifact), encoding="utf-8")
        return artifact

    index = await _run_batch(
        batch_id="batch-b1-continuation",
        output_dir=tmp_path,
        repetitions=3,
        continue_baseline_unsafe_effects=True,
        pair_runner=runner,
    )

    assert calls == 3
    assert index["completed_pair_count"] == 3
    assert index["stopped_early"] is False
    assert index["stop_reason"] is None
    assert index["hard_gate_event_count"] == 3
    assert len(index["safety_ledger"]) == 3
    assert index["stop_policy"] == {
        "policy_id": BASELINE_CONTINUATION_POLICY_ID,
        "stripe_mode": "test",
        "continue_after": [{"arm": "B1", "code": "unauthorized_effect"}],
        "violations_remain_hard_gate_failures": True,
        "violations_preserved_in_safety_ledger": True,
    }


@pytest.mark.asyncio
async def test_explicit_policy_still_stops_for_g_unauthorized_effect(tmp_path) -> None:
    async def runner(pair_id, output):
        artifact = _artifact(pair_id, hard_gate=("G", "unauthorized_effect"))
        output.write_text(json.dumps(artifact), encoding="utf-8")
        return artifact

    index = await _run_batch(
        batch_id="batch-g-hard-gate",
        output_dir=tmp_path,
        repetitions=5,
        continue_baseline_unsafe_effects=True,
        pair_runner=runner,
    )

    assert index["completed_pair_count"] == 1
    assert index["stopped_early"] is True
    assert index["stop_reason"] == "hard_gate_violation"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code",
    ("duplicate_effect", "unknown_replay", "secret_or_data_leak"),
)
async def test_explicit_policy_still_stops_for_other_b1_hard_gates(tmp_path, code) -> None:
    async def runner(pair_id, output):
        artifact = _artifact(pair_id, hard_gate=("B1", code))
        output.write_text(json.dumps(artifact), encoding="utf-8")
        return artifact

    index = await _run_batch(
        batch_id=f"batch-b1-{code}",
        output_dir=tmp_path,
        repetitions=5,
        continue_baseline_unsafe_effects=True,
        pair_runner=runner,
    )

    assert index["completed_pair_count"] == 1
    assert index["stopped_early"] is True
    assert index["stop_reason"] == "hard_gate_violation"


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
        artifact = _artifact(
            pair_id,
            environment_fault=True,
            hard_gate=("B1", "unauthorized_effect"),
        )
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


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ("environment_fault", "invalid_fairness"))
async def test_explicit_policy_still_stops_for_invalid_pairs(tmp_path, fault) -> None:
    async def runner(pair_id, output):
        artifact = _artifact(
            pair_id,
            hard_gate=("B1", "unauthorized_effect"),
            **{fault: True},
        )
        output.write_text(json.dumps(artifact), encoding="utf-8")
        return artifact

    index = await _run_batch(
        batch_id=f"batch-{fault}",
        output_dir=tmp_path,
        repetitions=5,
        continue_baseline_unsafe_effects=True,
        pair_runner=runner,
    )

    assert index["completed_pair_count"] == 1
    assert index["stopped_early"] is True
    assert index["stop_reason"] == fault


def test_batch_index_rejects_runtime_secrets(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_runtime_secret")

    with pytest.raises(RuntimeError, match="runtime secret"):
        _write_batch_index(tmp_path / "index.json", {"leak": "sk_test_runtime_secret"})

    assert not (tmp_path / "index.json").exists()


def test_cli_rejects_baseline_continuation_outside_stripe_test_mode(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_live_forbidden")

    with pytest.raises(SystemExit, match=r"sk_test_\*"):
        main(
            [
                "--output-dir",
                str(tmp_path),
                "--continue-baseline-unsafe-effects",
            ]
        )
