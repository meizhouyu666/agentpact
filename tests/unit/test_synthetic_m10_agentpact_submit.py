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
    for forbidden in (
        "ActionHandler",
        "ClickAction",
        "NativeActionHandlerOutcome",
        "PostActionControl",
    ):
        assert forbidden not in source
