import asyncio
import os
import subprocess
import time
from urllib.request import urlopen
from pathlib import Path

import pytest

from enterprise.governance.browser_audit import collect_browser_audit_evidence


def _installed_chromium() -> str | None:
    root = Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright"
    candidates = sorted(root.glob("chromium-*/chrome-win/chrome.exe"), reverse=True)
    return str(candidates[0]) if candidates else None


@pytest.mark.e2e
def test_real_synthetic_page_produces_audit_manifest_without_execution():
    executable = _installed_chromium()
    if executable is None:
        pytest.skip("Playwright Chromium binary is not installed")

    repository = Path(__file__).resolve().parents[2]
    python = repository / ".venv" / "Scripts" / "python.exe"
    port = "18083"
    process = subprocess.Popen(
        [
            str(python),
            "-m",
            "uvicorn",
            "enterprise.domains.synthetic_payment.app:app",
            "--host",
            "127.0.0.1",
            "--port",
            port,
        ],
        cwd=repository,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(2)
        with urlopen(f"http://127.0.0.1:{port}/api/audit") as response:
            audit_before = response.read().decode("utf-8")
        manifest = asyncio.run(
            collect_browser_audit_evidence(
                page_url=f"http://127.0.0.1:{port}/",
                scenario_id="synthetic-payment-browser-audit",
                task_id="task-browser-e2e",
                step_id="step-observe",
                hmac_secret="synthetic-browser-e2e-secret",
                executable_path=executable,
            )
        )
        with urlopen(f"http://127.0.0.1:{port}/api/audit") as response:
            audit_after = response.read().decode("utf-8")
    finally:
        process.terminate()
        process.wait(timeout=10)

    assert manifest.redaction_summary["raw_html_persisted"] is False
    assert manifest.redaction_summary["raw_screenshot_persisted"] is False
    assert len(manifest.dom_field_refs) >= 4
    assert all(len(field.field_ref) == 64 for field in manifest.dom_field_refs)
    assert {action.semantic_action for action in manifest.action_candidates} >= {
        "create_challenge",
        "approve_payment",
        "execute_payment",
    }
    assert len(manifest.screenshot_fingerprint) == 64
    assert all(decision.outcome.value != "deny" for decision in manifest.policy_decisions)
    assert audit_before == "[]"
    assert audit_after == "[]"
