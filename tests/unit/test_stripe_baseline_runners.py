from __future__ import annotations

import pytest

from benchmarks.stripe_browser import (
    BaselineRunnerNotConfigured,
    InMemoryResultSink,
    StripeMatchedToolsBenchmarkRunner,
    StripePromptOnlyBenchmarkRunner,
)
from benchmarks.stripe_browser.protocol import build_paired_benchmark_case_result
from tests.unit.test_benchmark_runner import _manifest, _outcome


def test_prompt_only_runner_is_executable_and_never_attempts_a_write() -> None:
    sink = InMemoryResultSink()
    result = StripePromptOnlyBenchmarkRunner(result_sink=sink).run(_manifest())

    assert result.arm == "B0"
    assert result.final_run_state == "BLOCKED"
    assert result.actual_business_state == "NOT_ATTEMPTED:prompt_only_no_write_capability"
    assert result.action_count == 0
    assert result.probe_count == 0
    assert result.business_state_correct is False
    assert result.safety.unauthorized_effect.event_count == 0
    assert next(iter(sink.results.values()))["arm"] == "B0"


def test_matched_tools_runner_requires_an_explicit_executor() -> None:
    with pytest.raises(BaselineRunnerNotConfigured, match="matched-tools browser executor"):
        StripeMatchedToolsBenchmarkRunner().run(_manifest())


def test_matched_tools_runner_binds_injected_outcome_to_b1() -> None:
    sink = InMemoryResultSink()

    def execute(manifest):
        return _outcome()

    result = StripeMatchedToolsBenchmarkRunner(execute, result_sink=sink).run(_manifest())

    assert result.arm == "B1"
    assert result.case_id == "checkout"
    assert next(iter(sink.results.values()))["arm"] == "B1"


def test_matched_tools_runner_rejects_an_outcome_from_another_arm() -> None:
    manifest = _manifest()
    other_arm = build_paired_benchmark_case_result(manifest, "B0", _outcome())
    with pytest.raises(ValueError, match="crossed the B1 arm boundary"):
        StripeMatchedToolsBenchmarkRunner(lambda _manifest: other_arm).run(manifest)


def test_gate2_result_mapping_retains_b1_unauthorized_effect() -> None:
    from enterprise.evaluation import HardGateViolation
    from scripts.stripe_gate2_pair import _manifest as gate2_manifest
    from scripts.stripe_gate2_pair import _outcome as gate2_outcome

    manifest = gate2_manifest("pair-gate2-test")
    mapping = gate2_outcome(
        manifest,
        actual_business_state=manifest.expected_business_state,
        final_run_state="SUCCEEDED",
        task_success=True,
        business_state_correct=True,
        action_count=1,
        approval_count=0,
        probe_count=1,
        latency_ms=10,
        unknown_stopped=False,
        unauthorized_effect_count=1,
        hard_gate_violations=(HardGateViolation(code="unauthorized_effect"),),
    )
    result = build_paired_benchmark_case_result(manifest, "B1", mapping)
    assert result.hard_gate_violations[0].code == "unauthorized_effect"
    assert result.safety.unauthorized_effect.event_count == 1
    assert result.safe_business_completion is False
