from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SUPPORT = ROOT / "tests/e2e/m4_synthetic_support.py"
E2E = ROOT / "tests/e2e/test_synthetic_payment_governed_browser.py"
DOC = ROOT / "docs/phase-2/synthetic-payment-domain-pack.md"
HANDLER = ROOT / "skyvern/webeye/actions/handler.py"
AUTHORIZED_PATHS = {
    "tests/e2e/m4_synthetic_support.py",
    "tests/e2e/test_synthetic_payment_governed_browser.py",
    "tests/unit/test_m4_synthetic_governed_browser_contract.py",
    "docs/phase-2/synthetic-payment-domain-pack.md",
}


def test_m4_support_accepts_only_numeric_loopback_http() -> None:
    support = SUPPORT.read_text(encoding="utf-8")
    assert 'parsed.scheme != "http"' in support
    assert 'parsed.hostname != "127.0.0.1"' in support
    assert "parsed.username" in support
    assert "parsed.password" in support
    assert "parsed.query" in support
    assert "parsed.fragment" in support
    assert '"http://127.0.0.1:{console_port}/"' in support


def test_m4_browser_effect_has_no_direct_playwright_click() -> None:
    for path in (SUPPORT, E2E):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        direct_clicks = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"click", "dblclick"}
        ]
        assert direct_clicks == [], f"Direct browser click found in {path.relative_to(ROOT)}"

    source = E2E.read_text(encoding="utf-8")
    assert source.count("run_handler_action(") == 4
    assert "ActionHandler.handle_action(" in source
    assert "ExecutionAttemptRecoveryRequired" in source
    assert "commit_then_inconclusive" in source


def test_m4_source_keeps_enforce_and_production_runtime_closed() -> None:
    combined = SUPPORT.read_text(encoding="utf-8") + E2E.read_text(encoding="utf-8")
    assert '"GOVERNANCE_MODE": "off"' in combined
    assert '"GOVERNANCE_MODE": "enforce"' not in combined
    assert "GOVERNANCE_MODE=enforce" not in combined
    assert "payments.example.com" not in combined
    assert "ForgeAgent" not in combined
    assert "DomainPackRegistry" not in combined
    assert "alembic upgrade heads" not in combined
    assert '"-m", "alembic", "upgrade", "heads"' in combined


def test_m4_contract_contains_durable_order_unknown_probe_and_no_replay() -> None:
    support = SUPPORT.read_text(encoding="utf-8")
    e2e = E2E.read_text(encoding="utf-8")
    handler = HANDLER.read_text(encoding="utf-8")

    assert "install_execute_order_probe" in support
    assert "ExecutionAttemptModel.idempotency_key == idempotency_key" in support
    assert "resolve_unknown_execution_attempt" in support
    assert "status_before_execute_request == [ExecutionAttemptStatus.EXECUTING.value]" in e2e
    assert "synthetic_unknown[\"state\"] == \"unknown\"" in e2e
    assert "len(execute_request_urls) == 1" in e2e
    assert "independently_probed[\"result_probe\"][\"status\"] == \"confirmed\"" in e2e

    governed_handler = handler[
        handler.index("    async def _handle_governed_action") : handler.index(
            "    async def _mark_governed_attempt_unknown"
        )
    ]
    assert governed_handler.index("mark_execution_attempt_executing") < governed_handler.index(
        "ActionHandler._handle_action_ungoverned"
    )
    assert governed_handler.index("ActionHandler._handle_action_ungoverned") < governed_handler.index(
        "ActionHandler._mark_governed_attempt_unknown"
    )


def test_m4_review_scope_is_exact_and_documentation_retains_prior_milestones() -> None:
    assert AUTHORIZED_PATHS == {
        path.relative_to(ROOT).as_posix() for path in (SUPPORT, E2E, Path(__file__).resolve(), DOC)
    }
    documentation = DOC.read_text(encoding="utf-8")
    assert "## Status and Boundary" in documentation
    assert "## Browser audit perception (audit-only)" in documentation
    assert "## M3 offline SDK conformance" in documentation


def test_m4_discovery_remains_windows_and_posix_aware() -> None:
    support = SUPPORT.read_text(encoding="utf-8")
    assert 'return f"{name}.exe" if os.name == "nt" else name' in support
    assert '"chromium-*/chrome-win/chrome.exe"' in support
    assert '"chromium-*/chrome-linux/chrome"' in support
    assert 'shutil.which(postgres_executable("initdb"))' in support
    assert 'postgres_bin / "initdb.exe"' not in support
    assert 'postgres_bin / "pg_ctl.exe"' not in support
    assert 'postgres_socket = root / "postgres-socket"' in support
    assert 'postgres_options += f" -k {postgres_socket}"' in support
    assert 'postgres log:\\n{server_log}' in support
    assert "capture_output=False" in support
