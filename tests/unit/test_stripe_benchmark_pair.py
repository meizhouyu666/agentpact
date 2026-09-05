from __future__ import annotations

import json

from benchmarks.stripe_browser.protocol import OfflineBenchmarkReport
from scripts.stripe_benchmark_pair import build_pair_artifact, main


def test_dry_run_writes_one_pair_with_explicit_blocked_reasons(tmp_path):
    output = tmp_path / "stripe-pair.json"
    assert main(["--output", str(output)]) == 0
    artifact = json.loads(output.read_text(encoding="utf-8"))
    outcomes = artifact["benchmark_artifact"]["outcomes"]
    assert [item["arm"] for item in outcomes] == ["G", "B0", "B1"]
    assert len({item["pair_id"] for item in outcomes}) == 1
    assert artifact["arm_execution"]["B0"]["reason"] == "b0_runner_not_implemented"
    assert artifact["arm_execution"]["B1"]["reason"] == "b1_runner_not_implemented"
    assert artifact["arm_execution"]["G"]["reason"] == "dry_run_no_execution"
    assert artifact["redaction"]["secrets_written"] is False


def test_live_mode_never_fabricates_g_success_without_test_key(tmp_path, monkeypatch):
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    output = tmp_path / "stripe-live-pair.json"
    assert main(["--live", "--output", str(output)]) == 0
    artifact = json.loads(output.read_text(encoding="utf-8"))
    assert artifact["arm_execution"]["G"] == {"status": "blocked", "reason": "missing_test_mode_stripe_secret"}
    g = next(item for item in artifact["benchmark_artifact"]["outcomes"] if item["arm"] == "G")
    assert g["task_success"] is False
    assert g["final_run_state"] == "BLOCKED"
    assert OfflineBenchmarkReport.from_json(json.dumps(artifact["benchmark_artifact"]))


def test_builder_is_deterministically_pairable():
    artifact = build_pair_artifact(pair_id="pair-x")
    assert artifact["benchmark_artifact"]["manifest"]["case_id"] == "stripe_checkout_success"
    assert {item["arm"] for item in artifact["benchmark_artifact"]["outcomes"]} == {"G", "B0", "B1"}
