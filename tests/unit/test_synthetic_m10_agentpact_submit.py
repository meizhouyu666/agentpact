from pathlib import Path


def test_synthetic_submit_uses_agentpact_persisted_executor_only() -> None:
    source = (
        Path(__file__).resolve().parents[2]
        / "tests"
        / "fixtures"
        / "synthetic_payment_runtime"
        / "m10_runtime.py"
    ).read_text(encoding="utf-8")

    assert "BrowserAction(" in source
    submit_resume = source.split("async def _execute_after_approval", 1)[1].split("async def _fresh_submit_context", 1)[0]
    assert "AgentPactBrowserLoop(" in submit_resume
    assert "PersistedBrowserExecutor(" in submit_resume
    assert "loop.run(" in submit_resume
    assert "PersistedBrowserExecutor(" in source
    assert "suspend_unknown_execution_for_probe(" in source
    assert "async def _fresh_submit_context(" in source
    assert "issue_permit(" in source
    for forbidden in (
        "ActionHandler",
        "ClickAction",
        "NativeActionHandlerOutcome",
        "PostActionControl",
    ):
        assert forbidden not in source


def test_submit_boundary_markers_preserve_authority_before_effect_and_probe_only_recovery() -> None:
    source = (
        Path(__file__).resolve().parents[2]
        / "tests"
        / "fixtures"
        / "synthetic_payment_runtime"
        / "m10_runtime.py"
    ).read_text(encoding="utf-8")
    persisted_executor = (
        Path(__file__).resolve().parents[2] / "enterprise" / "browser_loop" / "persisted_executor.py"
    ).read_text(encoding="utf-8")

    assert source.index("async def _fresh_submit_context(") < source.index("class _SyntheticSubmitPolicy")
    assert persisted_executor.index("executing = await mark_execution_attempt_executing") < persisted_executor.index(
        "browser_result = await self._runtime.execute_preflighted"
    )
    assert persisted_executor.index("unknown = await mark_execution_attempt_unknown") > persisted_executor.index(
        "browser_result = await self._runtime.execute_preflighted"
    )
    assert "pending_result_probe=True" in persisted_executor
    assert "async def probe(" in source
