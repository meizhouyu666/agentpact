from pathlib import Path


def test_synthetic_submit_uses_agentpact_persisted_executor_only() -> None:
    source = (
        Path(__file__).resolve().parents[2]
        / "enterprise"
        / "domains"
        / "synthetic_payment"
        / "m10_runtime.py"
    ).read_text(encoding="utf-8")

    assert "BrowserAction(" in source
    assert "PersistedBrowserExecutor(" in source
    assert "suspend_unknown_execution_for_probe(" in source
    for forbidden in (
        "ActionHandler",
        "ClickAction",
        "NativeActionHandlerOutcome",
        "PostActionControl",
    ):
        assert forbidden not in source
